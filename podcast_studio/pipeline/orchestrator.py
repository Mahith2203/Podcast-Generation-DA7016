"""High-level operations. Every public run_* method is a job body: it takes a progress callback, is safe to
re-run (completed work is skipped), and only ever touches the segment(s) it was asked to change."""
from __future__ import annotations

import logging
import traceback
from typing import Callable, Optional

from ..audio import AudioError
from ..clients import BodhanError, LLMError, make_clients
from ..config import Settings
from ..ingest import IngestError, default_title, ingest_file
from ..jobs import JobCancelled
from ..models import CheckReport, Project, ProjectSettings, Segment, SegmentVersion, UsageTotals, VideoCfg
from ..prompts import OCR_PROMPT
from ..store import ProjectStore
from ..text import clean_spoken
from ..util import new_id
from ..video import pipeline as video_pipeline
from ..video.mux import MuxError
from ..video.mux import require_tools as mux_require_tools
from ..video.render import RenderError
from ..video.timeline import TimelineError
from ..voices import STYLES
from .assemble import AssembleError, build_episode
from .planner import auto_minutes, make_plan
from .voice import Voicer
from .writer import ScriptWriter

log = logging.getLogger("podcast.orchestrator")

MAX_VERSIONS = 25
VIDEO_FRIENDLY = (video_pipeline.VideoError, RenderError, MuxError, TimelineError, LLMError)
FRIENDLY = (LLMError, BodhanError, IngestError, AssembleError, AudioError) + VIDEO_FRIENDLY[:-1]


class Ctx:
    """Everything one job needs: its own API clients (and cost meter), progress reporting and logging."""

    def __init__(self, orch: "Orchestrator", pid: str, progress: Callable[[float, str], None]):
        self.orch, self.pid, self.progress = orch, pid, progress
        self.store = orch.store
        self.llm, self.bodhan, self.meter = orch.clients_factory(orch.settings)
        self._flushed = UsageTotals()
        self.frac = 0.0

    def say(self, msg: str, frac: Optional[float] = None) -> None:
        if frac is not None:
            self.frac = frac
        self.store.log(self.pid, msg)
        self.progress(self.frac, msg)

    def tick(self, frac: float, msg: str = "") -> None:
        self.frac = frac
        self.progress(frac, msg)

    def sub(self, lo: float, hi: float) -> Callable[[float], None]:
        def f(x: float) -> None:
            self.tick(lo + (hi - lo) * max(0.0, min(1.0, x)))
        return f

    def set_status(self, status: str, message: str = "") -> None:
        def _f(p: Project) -> None:
            p.status = status  # type: ignore[assignment]
            p.message = message

        self.store.update(self.pid, _f)

    def flush_usage(self) -> None:
        """Add the usage counted since the last call (model calls, tokens, cost, voice requests) to the project's saved
        totals.
        """
        now = self.meter.snapshot()
        d = self._flushed

        def _f(p: Project) -> None:
            u = p.usage
            u.llm_calls += now.llm_calls - d.llm_calls
            u.llm_prompt_tokens += now.llm_prompt_tokens - d.llm_prompt_tokens
            u.llm_completion_tokens += now.llm_completion_tokens - d.llm_completion_tokens
            u.llm_cost_usd = round(u.llm_cost_usd + now.llm_cost_usd - d.llm_cost_usd, 6)
            u.tts_calls += now.tts_calls - d.tts_calls
            u.tts_chars += now.tts_chars - d.tts_chars
            u.tts_cache_hits += now.tts_cache_hits - d.tts_cache_hits
            u.mt_calls += now.mt_calls - d.mt_calls
            u.mt_chars += now.mt_chars - d.mt_chars

        self.store.update(self.pid, _f)
        self._flushed = now


class Orchestrator:
    """Runs the stages for one project: ingest, plan, write and check every segment, voice, assemble the episode and
    build the video. It also does the per-segment redo operations (rewrite, re-voice, edit a line, revert). Each
    `run_*` method is the body of a background job: it reports progress, saves after every step, and can be resumed.
    """
    def __init__(self, settings: Settings, store: ProjectStore, clients_factory=make_clients):
        self.settings = settings
        self.store = store
        self.clients_factory = clients_factory

    # ------------------------------------------------------------------ project creation
    def create_project(self, *, title: str, settings: ProjectSettings, filename: str = "",
                       data: Optional[bytes] = None, text: Optional[str] = None) -> Project:
        """Check the input (not empty, within the size limit), create the project folder and save the uploaded file or
        pasted text. Ingestion and writing happen later, in `run_generate`.
        """
        if data is None and not (text or "").strip():
            raise IngestError("Upload a PDF/text file or paste the chapter text.")
        if data is not None and len(data) > self.settings.max_upload_mb * 1024 * 1024:
            raise IngestError(f"The file is larger than {self.settings.max_upload_mb} MB.")
        pid = new_id()
        fname = filename or ("pasted-text.txt" if data is None else "upload")
        p = Project(id=pid, title=(title or "").strip(), settings=settings)
        p.source.filename = fname
        self.store.create(p)
        if data is not None:
            self.store.save_upload(pid, fname, data)
        else:
            self.store.save_upload(pid, "pasted-text.txt" if not fname.endswith((".txt", ".md")) else fname, (text or "").encode("utf-8"))
            p.source.filename = fname if fname.endswith((".txt", ".md")) else "pasted-text.txt"
            self.store.save(p)
        return p

    def recover_interrupted(self) -> None:
        """After a server restart, projects that were mid-run are marked resumable."""
        for row in self.store.list():
            if row["status"] in {"ingesting", "planning", "writing", "voicing", "assembling"}:
                def _f(p: Project) -> None:
                    p.status = "interrupted"
                    p.message = "The server restarted while this was running. Press Resume to continue."
                    for s in p.segments:
                        if s.status in {"writing", "voicing"}:
                            s.status = "pending" if not s.versions else "written"
                self.store.update(row["id"], _f)

    # ------------------------------------------------------------------ job bodies
    def run_generate(self, pid: str, progress: Callable[[float, str], None]) -> None:
        """Ingest -> plan -> write all scripts -> (voice all -> assemble). Resumable: finished steps are skipped."""
        ctx = Ctx(self, pid, progress)
        try:
            self._ingest(ctx)
            self._plan(ctx)
            self._write_all(ctx)
            p = self.store.load(pid)
            if p.settings.auto_voice:
                self._voice_all(ctx, 0.45, 0.93)
                self._assemble(ctx)
                ctx.set_status("ready", "Your podcast is ready.")
            else:
                ctx.set_status("script_ready", "Scripts are ready to review. Generate the audio when you are happy with them.")
            ctx.say("Done.", 1.0)
        except JobCancelled:
            ctx.set_status("interrupted", "Stopped. Press Resume to continue where it left off.")
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(ctx, e, status="error")
            raise
        finally:
            ctx.flush_usage()
        # optional video: runs after the audio episode is safely finished; a video failure never undoes the audio
        p = self.store.load(pid)
        if p.settings.make_video and p.status == "ready" and p.final:
            self.run_video(pid, progress)

    def run_voice(self, pid: str, progress: Callable[[float, str], None]) -> None:
        """Voice every segment that has a script but no audio, then build the episode."""
        ctx = Ctx(self, pid, progress)
        try:
            self._voice_all(ctx, 0.0, 0.92)
            self._assemble(ctx)
            ctx.set_status("ready", "Your podcast is ready.")
            ctx.say("Done.", 1.0)
        except JobCancelled:
            ctx.set_status("interrupted", "Stopped. Press 'Generate audio' to continue.")
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(ctx, e, status="error")
            raise
        finally:
            ctx.flush_usage()
        p = self.store.load(pid)
        if p.settings.make_video and p.status == "ready" and p.final:
            self.run_video(pid, progress)

    def run_regenerate(self, pid: str, index: int, instruction: Optional[str], mode: str,
                       progress: Callable[[float, str], None]) -> None:
        """Redo one segment as a new saved version. `rewrite` writes new lines (optionally following `instruction`,
        starting from the current version); `revoice` records the same words again with fresh takes. If the episode
        already has audio the segment is re-voiced and the episode rebuilt.
        """
        ctx = Ctx(self, pid, progress)
        prev = self.store.load(pid).status
        try:
            p = self.store.load(pid)
            seg = self._seg(p, index)
            base = seg.version()
            if base is None:
                raise ValueError("This segment has no script yet.")
            audio_mode = self._has_audio(p)
            if mode == "revoice":
                ctx.set_status("voicing", f"Re-voicing segment {index + 1}")
                nv = base.model_copy(deep=True)
                nv.version = seg.next_version_number()
                nv.origin = "revoiced"
                nv.instruction = None
                for t in nv.turns:
                    t.take += 1
                nv.audio_file, nv.duration, nv.turn_times = None, None, []
            else:
                ctx.set_status("writing", f"Rewriting segment {index + 1}")
                ctx.say(f"Rewriting segment {index + 1}: {seg.title}", 0.05)
                units = self.store.load_units(pid)
                nv = ScriptWriter(ctx.llm, ctx.bodhan, self.settings, say=lambda m: ctx.say(m)).write(
                    p, seg, units=units, instruction=(instruction or "").strip() or None, base=base)
                ctx.tick(0.4)
            if audio_mode or mode == "revoice":
                ctx.set_status("voicing", f"Voicing segment {index + 1}")
                self._voice_version(ctx, p, index, nv, 0.4 if mode != "revoice" else 0.05, 0.85)
            self._commit_version(pid, index, nv)
            if audio_mode or mode == "revoice":
                self._assemble(ctx)
            ctx.set_status("ready" if self._has_audio(self.store.load(pid)) else "script_ready", f"Segment {index + 1} updated.")
            ctx.say(f"Segment {index + 1} updated.", 1.0)
        except JobCancelled:
            ctx.set_status(prev, "Stopped.")
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(ctx, e, status=prev if prev in {"ready", "script_ready"} else "error")
            raise
        finally:
            ctx.flush_usage()

    def run_edit_turn(self, pid: str, index: int, turn_index: int, text: Optional[str], style: Optional[str],
                      progress: Callable[[float, str], None]) -> None:
        """Hand-edit one line (text and/or delivery style). Only that line is re-synthesised; the rest come from cache."""
        ctx = Ctx(self, pid, progress)
        prev = self.store.load(pid).status
        try:
            p = self.store.load(pid)
            seg = self._seg(p, index)
            base = seg.version()
            if base is None or not (0 <= turn_index < len(base.turns)):
                raise ValueError("That line does not exist.")
            nv = base.model_copy(deep=True)
            nv.version = seg.next_version_number()
            nv.origin, nv.instruction = "edited", f"Edited line {turn_index + 1} by hand"
            nv.check = CheckReport(verdict="unchecked", notes="Edited by hand: not re-checked against the chapter.")
            t = nv.turns[turn_index]
            if text is not None:
                cleaned = clean_spoken(text, p.settings.language, labels=(p.settings.host_a.name, p.settings.host_b.name))
                if not cleaned:
                    raise ValueError("The line cannot be empty.")
                t.text, t.text_en = cleaned, None
            if style is not None:
                t.style = style if style in STYLES else None
            t.take += 1
            t.edited = True
            nv.audio_file, nv.duration, nv.turn_times = None, None, []
            ctx.set_status("voicing", f"Re-voicing line {turn_index + 1} of segment {index + 1}")
            self._voice_version(ctx, p, index, nv, 0.05, 0.85)
            self._commit_version(pid, index, nv)
            self._assemble(ctx)
            ctx.set_status("ready", f"Line {turn_index + 1} updated.")
            ctx.say(f"Line {turn_index + 1} of segment {index + 1} updated.", 1.0)
        except JobCancelled:
            ctx.set_status(prev, "Stopped.")
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(ctx, e, status=prev if prev in {"ready", "script_ready"} else "error")
            raise
        finally:
            ctx.flush_usage()

    def run_revert(self, pid: str, index: int, version: int, progress: Callable[[float, str], None]) -> None:
        """Make an earlier version of a segment the current one (re-voicing it first if its audio is gone) and rebuild
        the episode if it has audio.
        """
        ctx = Ctx(self, pid, progress)
        prev = self.store.load(pid).status
        try:
            p = self.store.load(pid)
            seg = self._seg(p, index)
            target = seg.version(version)
            if target is None:
                raise ValueError("That version does not exist.")
            audio_mode = self._has_audio(p)
            if audio_mode and not (target.audio_file and (self.store.dir(pid) / target.audio_file).exists()):
                ctx.set_status("voicing", f"Re-voicing segment {index + 1}")
                self._voice_version(ctx, p, index, target, 0.05, 0.85)
                self.store.update(pid, lambda q: self._apply_audio(q, index, target))

            def _f(q: Project) -> None:
                q.segments[index].current = version

            self.store.update(pid, _f)
            if audio_mode:
                self._assemble(ctx)
            ctx.set_status("ready" if audio_mode else "script_ready", f"Segment {index + 1} reverted to version {version}.")
            ctx.say(f"Segment {index + 1} reverted to version {version}.", 1.0)
        except JobCancelled:
            ctx.set_status(prev, "Stopped.")
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(ctx, e, status=prev if prev in {"ready", "script_ready"} else "error")
            raise
        finally:
            ctx.flush_usage()

    def run_remix(self, pid: str, progress: Callable[[float, str], None]) -> None:
        """Re-mix every segment with the *current* tuning (pacing, levels). Recordings come from the cache, so no TTS credits are used."""
        ctx = Ctx(self, pid, progress)
        prev = self.store.load(pid).status
        try:
            p = self.store.load(pid)
            if not self._has_audio(p):
                raise ValueError("Nothing to re-mix yet: generate the audio first.")
            ctx.set_status("voicing", "Re-mixing with the new tuning")
            n = len(p.segments)
            for seg in p.segments:
                v = seg.version()
                if v is None:
                    continue
                a, b = 0.9 * seg.index / n, 0.9 * (seg.index + 1) / n
                ctx.say(f"Re-mixing segment {seg.index + 1} of {n}", a)
                self._voice_version(ctx, self.store.load(pid), seg.index, v, a, b)
                self.store.update(pid, lambda q, i=seg.index, vv=v: self._apply_audio(q, i, vv))
            self._assemble(ctx)
            ctx.set_status("ready", "Episode re-mixed.")
            ctx.say("Episode re-mixed.", 1.0)
        except JobCancelled:
            ctx.set_status(prev, "Stopped.")
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(ctx, e, status=prev if prev in {"ready", "script_ready"} else "error")
            raise
        finally:
            ctx.flush_usage()

    def run_assemble(self, pid: str, progress: Callable[[float, str], None]) -> None:
        """Rebuild the final episode (audio, transcripts, chapter marks) from every segment's current version."""
        ctx = Ctx(self, pid, progress)
        try:
            self._assemble(ctx)
            ctx.set_status("ready", "Episode rebuilt.")
            ctx.say("Episode rebuilt.", 1.0)
        except Exception as e:  # noqa: BLE001
            self._fail(ctx, e, status="error")
            raise

    # ------------------------------------------------------------------ video
    def _video_status(self, pid: str, status: str, message: str = "", cfg: Optional[VideoCfg] = None) -> None:
        def _f(q: Project) -> None:
            q.video.status = status  # type: ignore[assignment]
            q.video.message = message
            if cfg is not None:
                q.video.cfg = cfg

        self.store.update(pid, _f)

    def run_video(self, pid: str, progress: Callable[[float, str], None], cfg: Optional[VideoCfg] = None, *,
                  segments: Optional[set[int]] = None, instruction: Optional[str] = None, redo_visuals: bool = False) -> None:
        """Narrated explainer video from the finished episode. Re-runs only plan visuals for segments whose script changed and only
        draw frames that do not exist yet. `segments`+`instruction` = reviewer asks for different visuals in those segments."""
        ctx = Ctx(self, pid, progress)
        p0 = self.store.load(pid)
        cfg = cfg or p0.video.cfg
        had_video = bool(p0.video.final)
        try:
            video_pipeline.check_ready(p0)
            mux_require_tools()
            self._video_status(pid, "directing", "Planning the visuals", cfg)
            if cfg.use_figures:
                video_pipeline._figures(self.settings, self.store, pid, ctx.say)
            ctx.tick(0.04, "Planning the visuals")
            did = video_pipeline.direct(self.settings, self.store, ctx.llm, pid, say=ctx.say, tick=lambda f, m="": ctx.tick(0.04 + 0.3 * f, m),
                                        segments=segments, instruction=instruction, force=redo_visuals)
            ctx.say(f"Visuals planned for {len(did)} segment(s)" if did else "Visuals already match the script", 0.34)
            self._video_status(pid, "rendering", "Drawing and encoding the video")
            fv = video_pipeline.make_video(self.settings, self.store, ctx.llm, pid, cfg,
                                           progress=lambda f, m="": ctx.tick(0.34 + 0.65 * f, m), say=ctx.say)

            def _f(q: Project) -> None:
                q.video.final, q.video.status, q.video.message = fv, "ready", "Video ready."

            self.store.update(pid, _f)
            ctx.say("Video ready.", 1.0)
        except JobCancelled:
            self._video_status(pid, "ready" if had_video else "none", "Stopped.")
            raise
        except Exception as e:  # noqa: BLE001
            self._video_fail(ctx, e, "ready" if had_video else "error")
            raise
        finally:
            ctx.flush_usage()

    def _video_fail(self, ctx: Ctx, e: BaseException, status: str) -> None:
        if isinstance(e, VIDEO_FRIENDLY) or isinstance(e, (ValueError, AudioError)):
            msg = str(e)
        else:
            log.error("unexpected video failure: %s\n%s", e, traceback.format_exc())
            msg = f"Unexpected error: {e.__class__.__name__}: {e}"
        msg = msg[:700]
        self.store.log(ctx.pid, f"ERROR (video): {msg}")
        self._video_status(ctx.pid, "error", msg)  # an earlier video, if any, stays downloadable

    # ------------------------------------------------------------------ stages
    def _ingest(self, ctx: Ctx) -> None:
        if self.store.has_units(ctx.pid):
            return
        ctx.set_status("ingesting", "Reading the chapter")
        ctx.say("Reading the chapter...", 0.02)
        path = self.store.source_upload(ctx.pid)
        if path is None:
            raise IngestError("The uploaded file is missing from disk.")
        p = self.store.load(ctx.pid)

        def ocr(png: bytes) -> str:
            return ctx.llm.vision_ocr(png, prompt=OCR_PROMPT, model=self.settings.model_vision)

        doc = ingest_file(path, p.source.filename or path.name, ocr=ocr, max_chars=self.settings.max_source_chars,
                          progress=lambda m: ctx.say(m))
        self.store.save_units(ctx.pid, doc.text, doc.units)

        def _f(q: Project) -> None:
            q.source = doc.info
            if not q.title:
                q.title = default_title(doc.info, doc.info.filename)

        self.store.update(ctx.pid, _f)
        ctx.say(f"Chapter read: {doc.info.words:,} words in {doc.info.unit_count} parts.", 0.06)

    def _plan(self, ctx: Ctx) -> None:
        p = self.store.load(ctx.pid)
        if p.segments:
            return
        ctx.set_status("planning", "Planning the episode")
        ctx.say("Planning the episode structure...", 0.08)
        st = p.settings
        translate = st.writing_mode == "translate" and st.language != "en"
        minutes = st.minutes or auto_minutes(p.source.words)
        units = self.store.load_units(ctx.pid)
        plan, segs = make_plan(ctx.llm, units=units, title_hint=p.title or p.source.title_guess, audience=st.audience,
                               language="en" if translate else st.language, minutes=minutes, extra=st.extra_instructions,
                               model=st.writer_model or self.settings.model_planner)

        def _f(q: Project) -> None:
            q.plan, q.segments = plan, segs

        self.store.update(ctx.pid, _f)
        ctx.say(f"Planned {len(segs)} segments (~{minutes:g} min target).", 0.12)

    def _write_all(self, ctx: Ctx) -> None:
        p = self.store.load(ctx.pid)
        units = self.store.load_units(ctx.pid)
        n = len(p.segments)
        ctx.set_status("writing", "Writing the script")
        for seg in p.segments:
            lo = 0.12 + 0.33 * seg.index / n
            hi = 0.12 + 0.33 * (seg.index + 1) / n
            cur = self.store.load(ctx.pid)
            if cur.segments[seg.index].version() is not None:
                ctx.tick(hi)
                continue
            ctx.say(f"Writing segment {seg.index + 1} of {n}: {seg.title}", lo)
            self._set_seg_status(ctx.pid, seg.index, "writing")
            try:
                v = ScriptWriter(ctx.llm, ctx.bodhan, self.settings, say=lambda m: ctx.say(m)).write(cur, cur.segments[seg.index], units=units)
            except BaseException as e:
                self._set_seg_status(ctx.pid, seg.index, "error", None if isinstance(e, JobCancelled) else str(e))
                raise
            self._commit_version(ctx.pid, seg.index, v, status="written")
            ctx.flush_usage()
            ctx.tick(hi)

    def _voice_all(self, ctx: Ctx, lo: float, hi: float) -> None:
        p = self.store.load(ctx.pid)
        n = len(p.segments)
        ctx.set_status("voicing", "Recording the voices")
        for seg in p.segments:
            a = lo + (hi - lo) * seg.index / n
            b = lo + (hi - lo) * (seg.index + 1) / n
            cur = self.store.load(ctx.pid)
            v = cur.segments[seg.index].version()
            if v is None:
                raise AssembleError(f"Segment {seg.index + 1} has no script yet.")
            if v.audio_file and (self.store.dir(ctx.pid) / v.audio_file).exists():
                ctx.tick(b)
                continue
            ctx.say(f"Voicing segment {seg.index + 1} of {n}: {seg.title}", a)
            self._set_seg_status(ctx.pid, seg.index, "voicing")
            try:
                self._voice_version(ctx, cur, seg.index, v, a, b)
            except BaseException as e:
                self._set_seg_status(ctx.pid, seg.index, "error", None if isinstance(e, JobCancelled) else str(e))
                raise
            self.store.update(ctx.pid, lambda q, i=seg.index, vv=v: self._apply_audio(q, i, vv, status="ready"))
            ctx.flush_usage()
            ctx.tick(b)

    def _voice_version(self, ctx: Ctx, project: Project, index: int, version: SegmentVersion, lo: float, hi: float) -> None:
        voicer = Voicer(ctx.bodhan, self.store, self.settings, ctx.meter)
        voicer.voice_segment(project, index, version, progress=lambda x: ctx.tick(lo + (hi - lo) * x))

    def _assemble(self, ctx: Ctx) -> None:
        ctx.set_status("assembling", "Mixing the episode")
        ctx.say("Mixing the final episode...", max(ctx.frac, 0.94))
        p = self.store.load(ctx.pid)
        fin = build_episode(p, self.store)
        def _f(q: Project) -> None:
            q.final = fin
            if q.video.final:  # the pictures were timed to the old audio
                q.video.final.stale = True

        self.store.update(ctx.pid, _f)
        ctx.tick(0.99)

    # ------------------------------------------------------------------ small helpers
    @staticmethod
    def _seg(p: Project, index: int) -> Segment:
        if not (0 <= index < len(p.segments)):
            raise ValueError("That segment does not exist.")
        return p.segments[index]

    @staticmethod
    def _has_audio(p: Project) -> bool:
        return p.final is not None or any((s.version() and s.version().audio_file) for s in p.segments)  # type: ignore[union-attr]

    def _set_seg_status(self, pid: str, index: int, status: str, error: Optional[str] = None) -> None:
        def _f(q: Project) -> None:
            q.segments[index].status = status  # type: ignore[assignment]
            q.segments[index].error = error

        self.store.update(pid, _f)

    @staticmethod
    def _apply_audio(q: Project, index: int, v: SegmentVersion, status: Optional[str] = None) -> None:
        seg = q.segments[index]
        stored = seg.version(v.version)
        if stored is not None:
            stored.audio_file, stored.duration, stored.turn_times = v.audio_file, v.duration, v.turn_times
        if status:
            seg.status = status  # type: ignore[assignment]
            seg.error = None

    def _commit_version(self, pid: str, index: int, v: SegmentVersion, status: str = "ready") -> None:
        def _f(q: Project) -> None:
            seg = q.segments[index]
            seg.versions.append(v)
            seg.current = v.version
            seg.status = status if (status != "ready" or v.audio_file) else "written"  # type: ignore[assignment]
            seg.error = None
            # keep history bounded; never drop the current version
            while len(seg.versions) > MAX_VERSIONS:
                drop = next((x for x in seg.versions if x.version != seg.current), None)
                if drop is None:
                    break
                seg.versions.remove(drop)
                if drop.audio_file:
                    (self.store.dir(pid) / drop.audio_file).unlink(missing_ok=True)

        self.store.update(pid, _f)

    def _fail(self, ctx: Ctx, e: BaseException, status: str) -> None:
        if isinstance(e, FRIENDLY) or isinstance(e, ValueError):
            msg = str(e)
        else:
            log.error("unexpected failure: %s\n%s", e, traceback.format_exc())
            msg = f"Unexpected error: {e.__class__.__name__}: {e}"
        msg = msg[:600]
        self.store.log(ctx.pid, f"ERROR: {msg}")

        def _f(q: Project) -> None:
            q.status = status  # type: ignore[assignment]
            q.message = msg

        self.store.update(ctx.pid, _f)
