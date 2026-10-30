"""HTTP endpoints for the extras. One call (mount_extras) adds them to the app; nothing else in the app changes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .. import audio
from ..clients import BodhanError, LLMError
from ..util import slugify
from . import ask as ask_mod
from . import insights as ins_mod
from . import learn as learn_mod
from .common import OneAtATime
from .render import report_html, sheet_html

VERSION = 1


class AskBody(BaseModel):
    """A listener's question (3 to 400 characters)."""
    question: str = Field(min_length=3, max_length=400)


class LearnBody(BaseModel):
    """Whether to rebuild the study pack even if one already exists."""
    regenerate: bool = False


def mount_extras(app, settings, store, clients_factory) -> None:
    """Add the study-pack, Ask the hosts and Quality routes (under `/api`) to the app. They work on finished episodes only."""
    r = APIRouter(prefix="/api")
    one = OneAtATime()

    def ready_project(pid: str):
        """Load a project that is finished and idle; answer 409 if its script is incomplete or a job is still running."""
        p = store.load(pid)  # 404 through the app's handler
        if not p.segments or not all(s.version() for s in p.segments) or not store.has_units(pid):
            raise HTTPException(409, "The episode is not finished yet. Wait until its script is ready.")
        jobs = getattr(app.state, "jobs", None)
        if jobs is not None and jobs.active_for(pid):
            raise HTTPException(409, "The episode is still being made. Try again when it is done.")
        return p

    @r.get("/extras")
    def extras_info():
        """Tell the UI which optional features are available (it shows the Learn, Ask and Quality tabs only if this answers)."""
        return {"ok": True, "version": VERSION, "features": ["learn", "ask", "insights"], "demo": bool(settings.mock)}

    # ----------------------------------------------------------------- study pack
    @r.get("/projects/{pid}/learn")
    def learn_get(pid: str):
        """Status of the study pack: none, working, ready (with the pack) or error."""
        store.load(pid)
        return learn_mod.status(store, pid)

    @r.post("/projects/{pid}/learn")
    def learn_start(pid: str, body: LearnBody | None = None):
        """Build the study pack in the background (or return the existing one unless `regenerate` is true)."""
        ready_project(pid)
        cur = learn_mod.status(store, pid)
        if cur.get("status") == "ready" and not (body and body.regenerate):
            return cur
        return learn_mod.start(store, pid, settings, clients_factory)

    @r.get("/projects/{pid}/learn/sheet")
    def learn_sheet(pid: str, download: int = 0):
        """The printable study sheet (HTML); `download=1` sends it as a file."""
        p = store.load(pid)
        pack = learn_mod.load_pack(store, pid)
        if not pack or pack.get("status") != "ready":
            raise HTTPException(404, "There is no study pack yet.")
        html = sheet_html(p.plan.episode_title or p.title, p.settings.language, pack, p.settings.show_name)
        headers = {"Cache-Control": "no-cache"}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{slugify(p.plan.episode_title or p.title, "podcast")}-study-sheet.html"'
        return HTMLResponse(html, headers=headers)

    # ----------------------------------------------------------------- ask the hosts
    @r.get("/projects/{pid}/ask")
    def ask_list(pid: str):
        """The questions already asked about this episode, with their answers and sources."""
        store.load(pid)
        return {"answers": ask_mod.history(store, pid)}

    @r.post("/projects/{pid}/ask")
    def ask_post(pid: str, body: AskBody):
        """Answer one question (see `ask.answer_question`). Only one question per episode is answered at a time."""
        p = ready_project(pid)
        q = " ".join(body.question.split())
        if len(q) < 3:
            raise HTTPException(400, "Type a question first.")
        if not one.acquire(pid):
            raise HTTPException(409, "The hosts are still answering your last question.")
        try:
            llm, bodhan, meter = clients_factory(settings)
            return ask_mod.answer_question(p, store.load_units(pid), q, llm, bodhan, meter, settings, store)
        except (LLMError, BodhanError, audio.AudioError) as e:
            raise HTTPException(502, str(e)) from e
        finally:
            one.release(pid)

    @r.delete("/projects/{pid}/ask")
    def ask_clear(pid: str):
        """Delete the saved questions and answers of this episode."""
        store.load(pid)
        ask_mod.clear(store, pid)
        return {"ok": True}

    @r.get("/projects/{pid}/ask/{n}/audio")
    def ask_audio(pid: str, n: int):
        """The recorded answer number `n` as WAV."""
        store.load(pid)
        f = ask_mod.audio_path(store, pid, n)
        if f is None:
            raise HTTPException(404, "That answer's audio is gone.")
        return FileResponse(f, media_type="audio/wav", headers={"Cache-Control": "no-cache"})

    # ----------------------------------------------------------------- insights
    @r.get("/projects/{pid}/insights")
    def insights(pid: str, validation: int = 1):
        """The numbers behind the Quality tab (see `insights.compute_insights`); `validation=0` skips re-running the
        validator.
        """
        store.load(pid)
        return ins_mod.compute_insights(store, pid, with_validation=bool(validation), demo_mode=bool(settings.mock))

    @r.get("/projects/{pid}/insights/report")
    def insights_report(pid: str, download: int = 0):
        """The printable Quality evidence report (HTML); `download=1` sends it as a file."""
        p = store.load(pid)
        html = report_html(ins_mod.compute_insights(store, pid, demo_mode=bool(settings.mock)))
        headers = {"Cache-Control": "no-cache"}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{slugify(p.plan.episode_title or p.title, "podcast")}-quality-report.html"'
        return HTMLResponse(html, headers=headers)

    app.include_router(r)
