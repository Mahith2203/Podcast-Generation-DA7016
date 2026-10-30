"""FastAPI app: REST endpoints + the single-page UI. Bind to localhost; there is no auth (it is a local tool)."""
from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import audio, tuning
from .clients import BodhanError, LLMError, make_clients
from .config import ROOT, Settings
from .doctor import run_doctor
from .extras import mount_extras
from .ingest import IngestError
from .jobs import JobBusy, JobManager
from .keys import split_keys
from .models import ProjectSettings, VideoCfg
from .pipeline.orchestrator import Orchestrator
from .pipeline.voice import Voicer
from .store import ProjectNotFound, ProjectStore
from .util import slugify, update_env_file
from .validate import validate_project
from .voices import LANGS, SAMPLE_TEXT, STYLES, VOICE_NAMES, catalog, default_hosts

log = logging.getLogger("podcast.api")

mimetypes.add_type("text/vtt", ".vtt")
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/wav", ".wav")


class _FreshStatic(StaticFiles):
    """Serves the UI with `Cache-Control: no-cache`: the browser re-checks (cheaply, by ETag) on every load,
    so an updated script or stylesheet is never shadowed by an old cached copy."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


class RegenerateBody(BaseModel):
    """Body for redoing one segment: `rewrite` (new script, optionally following an instruction) or `revoice` (same
    words, new recording).
    """
    mode: str = Field("rewrite", pattern="^(rewrite|revoice)$")
    instruction: Optional[str] = Field(None, max_length=1500)


class EditTurnBody(BaseModel):
    """Body for hand-editing one spoken line of a segment: new text and/or a new delivery style."""
    turn: int = Field(ge=0)
    text: Optional[str] = Field(None, max_length=1500)
    style: Optional[str] = None


class RevertBody(BaseModel):
    """Body for switching a segment back to one of its earlier saved versions."""
    version: int = Field(ge=1)


class VideoBody(BaseModel):
    """Optional overrides for the narrated video (size, frame rate, look, captions, speaker names, chapter figures) and
    whether to redraw every slide.
    """
    resolution: Optional[str] = Field(None, pattern="^(480p|720p|1080p)$")
    fps: Optional[int] = Field(None, ge=10, le=30)
    theme: Optional[str] = Field(None, pattern="^(classic|dark|warm)$")
    captions: Optional[bool] = None
    speakers: Optional[bool] = None
    use_figures: Optional[bool] = None
    redo_visuals: bool = False


class VideoRegenBody(BaseModel):
    """Body for redoing the visuals of one segment, with an optional instruction such as 'show it as a cycle'."""
    instruction: Optional[str] = Field(None, max_length=1500)


class KeysBody(BaseModel):
    """Keys and model names saved from the Keys dialog. They are written to `.env` and never returned by the API."""
    openrouter_api_key: Optional[str] = Field(None, max_length=600)
    bodhan_api_key: Optional[str] = Field(None, max_length=600)      # old name: saved as the TTS key
    bodhan_tts_api_key: Optional[str] = Field(None, max_length=600)
    bodhan_mt_api_key: Optional[str] = Field(None, max_length=600)
    bodhan_ocr_api_key: Optional[str] = Field(None, max_length=600)
    model_writer: Optional[str] = Field(None, max_length=120)
    model_checker: Optional[str] = Field(None, max_length=120)
    mock: Optional[bool] = None


class TuningBody(BaseModel):
    """Either the name of a tuning preset or individual tuning values (see `podcast_studio.tuning.Tuning`)."""
    preset: Optional[str] = None
    values: dict[str, float] = Field(default_factory=dict)


class PreviewBody(BaseModel):
    """Request for a short sample of one voice: which voice, language and style, and optional text (at most 300 characters)."""
    voice: str
    lang: str = "en"
    style: Optional[str] = None
    text: Optional[str] = Field(None, max_length=300)


def create_app(settings: Settings | None = None, clients_factory=make_clients, env_path: Path | None = None) -> FastAPI:
    """Build the FastAPI application: the REST endpoints under `/api`, the web UI at `/`, and the study-pack, Ask and
    quality routes. `clients_factory(settings)` returns `(llm, bodhan, meter)`, so tests can inject offline fakes;
    `env_path` is the `.env` file the Keys dialog writes to.
    """
    settings = settings or Settings.from_env()
    store = ProjectStore(settings.data_dir)
    orch = Orchestrator(settings, store, clients_factory)
    jobs = JobManager(max_workers=2)
    orch.recover_interrupted()

    app = FastAPI(title="Podcast Studio", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.settings, app.state.store, app.state.orch, app.state.jobs = settings, store, orch, jobs

    # ------------------------------------------------------------------ errors
    @app.exception_handler(ProjectNotFound)
    async def _nf(_: Request, __: ProjectNotFound):
        return JSONResponse({"detail": "Project not found."}, status_code=404)

    @app.exception_handler(JobBusy)
    async def _busy(_: Request, e: JobBusy):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(IngestError)
    async def _ing(_: Request, e: IngestError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(ValueError)
    async def _val(_: Request, e: ValueError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    def project_view(pid: str) -> dict:
        p = store.load(pid)
        job = jobs.active_for(pid) or jobs.latest_for(pid)
        return {"project": p.model_dump(), "job": job.public() if job else None}

    def submit(pid: str, kind: str, fn):
        store.load(pid)  # 404 early
        return jobs.submit(pid, kind, fn)

    # ------------------------------------------------------------------ meta
    @app.get("/api/health")
    def health():
        """Is the server up? `mock` says whether demo mode is on."""
        return {"ok": True, "mock": settings.mock}

    @app.get("/api/config")
    def config():
        """Everything the UI needs to build its forms: languages, voices, delivery styles, defaults, models, upload
        limit, key status and whether ffmpeg is installed.
        """
        fem, mal = default_hosts("en")
        return {**catalog(), "mock": settings.mock, "missing_keys": settings.missing_keys(), "key_status": settings.key_status(), "ffmpeg": bool(audio.ffmpeg_path()),
                "defaults": {**ProjectSettings().model_dump(), "host_a": fem, "host_b": mal},
                "models": {"writer": settings.model_writer, "checker": settings.model_checker},
                "limits": {"max_upload_mb": settings.max_upload_mb}}

    @app.get("/api/doctor")
    def doctor():
        """Run the setup check and return one row per check (see `doctor.run_doctor`)."""
        return {"checks": run_doctor(settings)}

    @app.post("/api/preview")
    def preview(body: PreviewBody):
        """Synthesise a short sample of one voice (WAV) so a voice can be heard before it is chosen as a host."""
        if body.voice not in VOICE_NAMES:
            raise HTTPException(400, "Unknown voice.")
        if body.lang not in LANGS:
            raise HTTPException(400, "Unknown language.")
        if body.style and body.style not in STYLES:
            raise HTTPException(400, "Unknown style.")
        text = (body.text or "").strip() or SAMPLE_TEXT.get(body.lang) or SAMPLE_TEXT["en"]
        lang = body.lang if (body.text or body.lang in SAMPLE_TEXT) else "en"
        llm, bodhan, meter = clients_factory(settings)
        try:
            arr = Voicer(bodhan, store, settings, meter).synth_chunk(text, body.voice, lang, body.style)
        except (BodhanError, audio.AudioError) as e:
            raise HTTPException(502, str(e)) from e
        return Response(audio.array_to_wav_bytes(audio.process_clip(arr)), media_type="audio/wav")

    # ------------------------------------------------------------------ setup (keys) and tuning
    @app.post("/api/setup/keys")
    def save_keys(body: KeysBody, request: Request):
        """Save keys to the local .env and apply them immediately. Only accepted from this computer; keys are never returned."""
        if (request.client.host if request.client else "") not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(403, "Keys can only be saved from the computer the app runs on.")
        upd: dict[str, str] = {}
        fields = (("openrouter_api_key", "OPENROUTER_API_KEY", "openrouter_api_key"),
                  ("bodhan_tts_api_key", "BODHAN_TTS_API_KEY", "bodhan_tts_api_key"),
                  ("bodhan_api_key", "BODHAN_TTS_API_KEY", "bodhan_tts_api_key"),
                  ("bodhan_mt_api_key", "BODHAN_MT_API_KEY", "bodhan_mt_api_key"),
                  ("bodhan_ocr_api_key", "BODHAN_OCR_API_KEY", "bodhan_ocr_api_key"),
                  ("model_writer", "LLM_MODEL_WRITER", "model_writer"),
                  ("model_checker", "LLM_MODEL_CHECKER", "model_checker"))
        for field_name, env_name, attr in fields:
            v = getattr(body, field_name)
            if v is None or not v.strip():
                continue
            if "key" in field_name:
                parts = split_keys(v)
                if not parts or any(len(x) < 10 for x in parts):
                    raise HTTPException(400, "That does not look like a key (too short). Several keys can be separated by commas.")
                v = ",".join(parts)
            else:
                v = v.strip().strip('"').strip("'")
            upd[env_name] = v
            setattr(settings, attr, v)
        if body.mock is not None:
            upd["MOCK_APIS"] = "1" if body.mock else "0"
            settings.mock = body.mock
        if not upd:
            raise HTTPException(400, "Nothing to save.")
        update_env_file(env_path or ROOT / ".env", upd)
        return {"ok": True, "saved": sorted(upd), "missing_keys": settings.missing_keys(), "mock": settings.mock}

    @app.get("/api/tuning")
    def get_tuning():
        """The current tuning values with their defaults, the preset names and the tuning file path."""
        from dataclasses import asdict
        return {"values": asdict(tuning.current()), "defaults": asdict(tuning.Tuning()), "presets": list(tuning.PRESETS),
                "file": str(tuning.TUNING_FILE)}

    @app.put("/api/tuning")
    def put_tuning(body: TuningBody):
        """Apply a tuning preset or individual values. Unknown names are rejected and values are clamped to safe ranges."""
        from dataclasses import asdict
        if body.preset:
            if body.preset not in tuning.PRESETS:
                raise HTTPException(400, "Unknown preset.")
            vals = dict(tuning.PRESETS[body.preset])
        else:
            unknown = [k for k in body.values if k not in asdict(tuning.Tuning())]
            if unknown:
                raise HTTPException(400, f"Unknown setting: {', '.join(unknown)}")
            vals = {**asdict(tuning.current()), **body.values}
        t = tuning.save(vals)
        return {"values": asdict(t)}

    # ------------------------------------------------------------------ projects
    @app.get("/api/projects")
    def list_projects():
        """Summaries of all projects, newest first."""
        return {"projects": store.list()}

    @app.post("/api/projects")
    async def create_project(file: Optional[UploadFile] = File(None), text: str = Form(""), title: str = Form(""),
                             settings_json: str = Form("{}")):
        """Create a project from an uploaded PDF/text file or pasted text plus its settings (multipart form), then start
        the generation job.
        """
        try:
            raw = json.loads(settings_json or "{}")
            pset = ProjectSettings(**raw)
        except (json.JSONDecodeError, ValidationError, TypeError) as e:
            msg = e.errors()[0]["msg"] if isinstance(e, ValidationError) and e.errors() else str(e)
            raise HTTPException(400, f"Invalid settings: {msg}") from e
        data = None
        fname = ""
        if file is not None and file.filename:
            data = await file.read()
            fname = file.filename
        if data is not None and not data:
            raise HTTPException(400, "The uploaded file is empty.")
        p = orch.create_project(title=title, settings=pset, filename=fname, data=data, text=text if data is None else None)
        job = jobs.submit(p.id, "generate", lambda prog: orch.run_generate(p.id, prog))
        return {"project": p.model_dump(), "job": job.public()}

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        """One project together with its active or latest job (the UI polls this to show progress)."""
        return project_view(pid)

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        """Delete a project and its files; refused while one of its jobs is running."""
        store.load(pid)
        if jobs.active_for(pid):
            raise HTTPException(409, "Stop the running job before deleting this project.")
        store.delete(pid)
        return {"ok": True}

    @app.post("/api/projects/{pid}/resume")
    def resume(pid: str):
        """Continue generating from where an interrupted or failed run stopped; finished steps are skipped."""
        job = submit(pid, "generate", lambda prog: orch.run_generate(pid, prog))
        return {"job": job.public()}

    @app.post("/api/projects/{pid}/voice")
    def voice(pid: str):
        """Record the audio and assemble the episode for a project whose script is waiting for review."""
        job = submit(pid, "voice", lambda prog: orch.run_voice(pid, prog))
        return {"job": job.public()}

    @app.post("/api/projects/{pid}/remix")
    def remix(pid: str):
        """Rebuild the final mix with the current tuning, re-using the cached voice clips (no voice credits)."""
        job = submit(pid, "remix", lambda prog: orch.run_remix(pid, prog))
        return {"job": job.public()}

    @app.post("/api/projects/{pid}/validate")
    def validate(pid: str, deep: bool = False):
        """Quality report for an episode. With `deep=true` every segment is also re-checked by the model."""
        store.load(pid)
        llm = clients_factory(settings)[0] if deep else None
        try:
            return validate_project(store, pid, deep=deep, llm=llm)
        except LLMError as e:
            raise HTTPException(502, str(e)) from e

    @app.post("/api/projects/{pid}/assemble")
    def assemble(pid: str):
        """Rebuild the final audio, transcripts and chapter marks from every segment's current version."""
        job = submit(pid, "assemble", lambda prog: orch.run_assemble(pid, prog))
        return {"job": job.public()}

    @app.post("/api/projects/{pid}/segments/{index}/regenerate")
    def regenerate(pid: str, index: int, body: RegenerateBody):
        """Rewrite or re-voice one segment (background job; see `RegenerateBody`)."""
        job = submit(pid, f"regenerate:{index}", lambda prog: orch.run_regenerate(pid, index, body.instruction, body.mode, prog))
        return {"job": job.public()}

    @app.post("/api/projects/{pid}/segments/{index}/edit-turn")
    def edit_turn(pid: str, index: int, body: EditTurnBody):
        """Hand-edit one line of a segment's script (background job; only that line is re-recorded)."""
        if body.text is None and body.style is None:
            raise HTTPException(400, "Nothing to change.")
        if body.style and body.style not in STYLES:
            raise HTTPException(400, "Unknown style.")
        job = submit(pid, f"edit:{index}", lambda prog: orch.run_edit_turn(pid, index, body.turn, body.text, body.style, prog))
        return {"job": job.public()}

    @app.post("/api/projects/{pid}/segments/{index}/revert")
    def revert(pid: str, index: int, body: RevertBody):
        """Make an earlier version of a segment the current one (background job)."""
        job = submit(pid, f"revert:{index}", lambda prog: orch.run_revert(pid, index, body.version, prog))
        return {"job": job.public()}

    @app.post("/api/projects/{pid}/video")
    def make_video(pid: str, body: Optional[VideoBody] = None):
        """Start building the narrated MP4 (background job). The body may override the video settings."""
        p = store.load(pid)
        b = body or VideoBody()
        cfg = p.video.cfg.model_copy(update={k: v for k, v in b.model_dump(exclude={"redo_visuals"}).items() if v is not None})
        cfg = VideoCfg.model_validate(cfg.model_dump())
        job = submit(pid, "video", lambda prog: orch.run_video(pid, prog, cfg, redo_visuals=b.redo_visuals))
        return {"job": job.public()}

    @app.post("/api/projects/{pid}/video/segments/{index}/regenerate")
    def regenerate_visuals(pid: str, index: int, body: VideoRegenBody):
        """Redraw the visuals of a single segment, optionally following an instruction (background job)."""
        p = store.load(pid)
        if not (0 <= index < len(p.segments)):
            raise HTTPException(404, "That segment does not exist.")
        job = submit(pid, f"video:{index}", lambda prog: orch.run_video(pid, prog, None, segments={index}, instruction=body.instruction or None,
                                                                         redo_visuals=True))
        return {"job": job.public()}

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        """Status and progress of a background job."""
        j = jobs.get(job_id)
        if not j:
            raise HTTPException(404, "Job not found.")
        return j.public()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        """Ask a running job to stop at its next safe point."""
        if not jobs.cancel(job_id):
            raise HTTPException(404, "No running job with that id.")
        return {"ok": True}

    # ------------------------------------------------------------------ files
    @app.get("/api/projects/{pid}/files/{relpath:path}")
    def get_file(pid: str, relpath: str, download: int = 0):
        """Serve a file of a project (segment audio, final audio, transcripts, video). Only those folders are public, and
        `download=1` sends the file as an attachment with a readable name.
        """
        path = store.resolve_public(pid, relpath)
        if path is None:
            raise HTTPException(404, "File not found.")
        mt = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in {".md", ".srt"}:
            mt += "; charset=utf-8"
        headers = {"Cache-Control": "no-cache"}
        if download:
            p = store.load(pid)
            nice = slugify(p.plan.episode_title or p.title, "podcast")
            headers["Content-Disposition"] = f'attachment; filename="{nice}{path.suffix}"'
        return FileResponse(path, media_type=mt, headers=headers)

    # ------------------------------------------------------------------ UI
    static_dir = ROOT / "static"
    if static_dir.exists():
        app.mount("/static", _FreshStatic(directory=str(static_dir)), name="static")

        @app.get("/")
        def index():
            """The single-page web UI."""
            return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ study pack, Ask the hosts, quality (podcast_studio/extras)
    mount_extras(app, settings, store, clients_factory)

    return app
