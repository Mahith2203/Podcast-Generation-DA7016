"""The video job: figures -> storyboards (only where needed) -> frames -> MP4. Re-runs reuse everything that is unchanged:
storyboards of untouched segments, every already-drawn frame, so a one-segment edit only costs that segment's visuals."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from ..config import Settings
from ..models import Figure, FinalVideo, Project, SegmentStoryboard, VideoCfg
from ..store import ProjectStore
from ..util import now_iso
from . import mux
from .director import Director, figures_for
from .figures import extract_figures
from .render import RES, render_video_frames, thumbnail
from .timeline import build_shots

VIDEO_DIR = "video"


class VideoError(RuntimeError):
    """Raised when the video cannot be made."""
    pass


def fonts_dir(settings: Settings) -> Path:
    return settings.data_dir.parent / "fonts"


def is_stale(p: Project) -> bool:
    v = p.video.final
    return bool(v and (v.stale or not p.final or v.audio_built_at != p.final.built_at))


def check_ready(p: Project) -> None:
    """Raise `VideoError` unless the audio episode exists and every segment has been recorded."""
    if not p.final or not p.final.wav:
        raise VideoError("Make the audio episode first: the video is built on top of it.")
    for seg in p.segments:
        v = seg.version()
        if not v or not v.turn_times:
            raise VideoError(f"Segment {seg.index + 1} has no recording yet.")


def _same_words(a, b) -> bool:
    return bool(a and b) and [t.text for t in a.turns] == [t.text for t in b.turns]


def storyboard_state(p: Project, seg_index: int) -> str:
    """'ok' | 'stale' | 'missing' for one segment's storyboard."""
    seg = p.segments[seg_index]
    sb = next((s for s in p.video.storyboards if s.index == seg.index), None)
    if sb is None:
        return "missing"
    if sb.version == seg.current or _same_words(seg.version(sb.version), seg.version()):
        return "ok"
    return "stale"


def _figures(settings: Settings, store: ProjectStore, pid: str, say) -> None:
    p = store.load(pid)
    if p.video.figures_done:
        return
    figs: list[Figure] = []
    src = store.source_upload(pid)
    if src is not None and src.suffix.lower() == ".pdf":
        try:
            say("Looking for the chapter's own pictures...")
            figs = extract_figures(src, store.dir(pid) / VIDEO_DIR / "figures", store.load_units(pid), project_dir=store.dir(pid))
            say(f"Found {len(figs)} picture(s) in the chapter" if figs else "No usable pictures in the chapter")
        except Exception as e:  # noqa: BLE001  (figures are a bonus: never fail the video because of them)
            say(f"Could not read pictures from the PDF ({e.__class__.__name__}); continuing without them")
            figs = []

    def _f(q: Project) -> None:
        q.video.figures, q.video.figures_done = figs, True

    store.update(pid, _f)


def direct(settings: Settings, store: ProjectStore, llm, pid: str, *, say, tick, segments: Optional[set[int]] = None,
           instruction: Optional[str] = None, force: bool = False) -> list[int]:
    """Make (or remake) storyboards. Returns the segment indices that were (re)directed."""
    p = store.load(pid)
    units = store.load_units(pid)
    figs = p.video.figures if p.video.cfg.use_figures else []
    director = Director(llm, say=say)
    done: list[int] = []
    sbs = {x.index: x for x in p.video.storyboards}

    def needs(s) -> bool:
        if force or instruction or storyboard_state(p, s.index) != "ok":
            return True
        sb = sbs.get(s.index)  # pictures switched off since these visuals were planned
        return bool(sb and not figs and any(sc.layout == "figure" for sc in sb.scenes))

    todo = [s for s in p.segments if (segments is None or s.index in segments) and needs(s)]
    for k, seg in enumerate(todo):
        tick(k / max(1, len(todo)), f"Planning the visuals for segment {seg.index + 1} of {len(p.segments)}")
        base = next((s for s in p.video.storyboards if s.index == seg.index), None)
        sb = director.storyboard(p, seg, units, figures_for(seg, figs), instruction=instruction, base=base if instruction else None)

        def _f(q: Project, sb=sb) -> None:
            q.video.storyboards = [s for s in q.video.storyboards if s.index != sb.index] + [sb]
            q.video.storyboards.sort(key=lambda s: s.index)

        store.update(pid, _f)
        done.append(seg.index)
    # storyboards whose words did not change just move to the new script version (a re-recording does not need new visuals)
    p = store.load(pid)

    def _bump(q: Project) -> None:
        for sb in q.video.storyboards:
            seg = q.segments[sb.index] if sb.index < len(q.segments) else None
            if seg and sb.version != seg.current and _same_words(seg.version(sb.version), seg.version()):
                sb.version = seg.current

    store.update(pid, _bump)
    return done


def _prune(frames_dir: Path, keep: set[Path], max_age_days: int = 14) -> None:
    now = time.time()
    for f in list(frames_dir.glob("*.png")) + list((frames_dir.parent / "pages").glob("*.html")):
        try:
            if f in keep:
                os.utime(f, None)
            elif now - f.stat().st_mtime > max_age_days * 86400:
                f.unlink()
        except OSError:
            pass


def make_video(settings: Settings, store: ProjectStore, llm, pid: str, cfg: VideoCfg, *,
               progress: Callable[[float, str], None], say: Callable[[str], None]) -> FinalVideo:
    """Everything after the storyboards: shots -> frames -> MP4. Storyboards must already exist for every segment."""
    p = store.load(pid)
    check_ready(p)
    pdir = store.dir(pid)
    vdir = pdir / VIDEO_DIR
    vdir.mkdir(exist_ok=True)
    sbs: dict[int, SegmentStoryboard] = {s.index: s for s in p.video.storyboards}
    missing = [s.index + 1 for s in p.segments if s.index not in sbs]
    if missing:
        raise VideoError(f"No visuals planned yet for segment(s) {', '.join(map(str, missing))}.")
    total = float(p.final.duration)  # type: ignore[union-attr]
    shots = build_shots(p, sbs, total=total, captions=cfg.captions)
    figs = {f.id: f for f in p.video.figures}
    progress(0.0, "Drawing the slides")
    frames, fr, info = render_video_frames(
        p, cfg, shots, sbs, figs, cache_dir=store.data_dir / "cache", project_dir=pdir, fonts_dir=fonts_dir(settings),
        progress=lambda f, m="": progress(0.7 * f, m))
    say(f"Drew {info['rendered']} new frame(s), reused {info['reused']}" + (f" ({info['browser']})" if info["browser"] else ""))

    # thumbnails + poster
    progress(0.72, "Scene previews")
    scenes_dir = vdir / "scenes"
    shutil.rmtree(scenes_dir, ignore_errors=True)
    for (sp, si), png in info["finals"].items():
        thumbnail(png, scenes_dir / f"s{p.segments[sp].index}_{si}.jpg")
    poster_rel = None
    first = info["finals"].get((shots[0].seg, shots[0].scene))
    if first:
        with Image.open(first) as x:
            x.convert("RGB").save(vdir / "poster.jpg", "JPEG", quality=86)
        poster_rel = f"{VIDEO_DIR}/poster.jpg"

    progress(0.75, "Encoding the video (ffmpeg)")
    wav = pdir / p.final.wav  # type: ignore[union-attr]
    out = vdir / "episode.mp4"
    mux.make_mp4(frames, wav, out, fps=cfg.fps, duration=total, frames_dir=fr.frames_dir)
    pr = mux.probe(out)
    warns = list(fr.warnings)
    if not pr["video"] or not pr["audio"]:
        raise VideoError("The finished file is missing its picture or its sound track; run `doctor` to check ffmpeg.")
    if abs(pr["duration"] - total) > 0.5:
        warns.append(f"Video is {pr['duration']:.1f}s but the audio is {total:.1f}s.")
    _prune(fr.frames_dir, {f for f, _ in frames})
    w, h = RES[cfg.resolution]
    fv = FinalVideo(mp4=f"{VIDEO_DIR}/episode.mp4", poster=poster_rel, duration=round(pr["duration"] or total, 2),
                    width=pr["width"] or w, height=pr["height"] or h, frames=len(frames), built_at=now_iso(),
                    audio_built_at=p.final.built_at, stale=False, warnings=warns)  # type: ignore[union-attr]
    progress(1.0, "Video ready")
    return fv
