"""Fine-tuning helpers.

  trials  : write the same chapter with several (model, creativity) configurations, script only (no voice credits),
            score each with the validator, and recommend the best one.
  voices  : render the same sentence in many voices into a folder with a play-all HTML page, so you can pick by ear.
"""
from __future__ import annotations

import html
import itertools
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from . import audio, tuning
from .clients import make_clients
from .config import Settings
from .models import ProjectSettings
from .pipeline.orchestrator import Orchestrator
from .store import ProjectStore
from .validate import validate_project
from .voices import LANGS, SAMPLE_TEXT, VOICE_GENDER, VOICE_NAMES

Progress = Callable[[str], None]


def _run_trial(settings: Settings, store: ProjectStore, text: str, base: ProjectSettings, model: Optional[str], temperature: float,
               say: Progress) -> dict:
    orch = Orchestrator(settings, store)
    st = base.model_copy(update={"writer_model": model, "auto_voice": False})
    p = orch.create_project(title="tune-trial", settings=st, text=text)
    err = ""
    with tuning.override(writer_temperature=temperature):
        try:
            orch.run_generate(p.id, lambda f, m="": None)
        except Exception as e:  # noqa: BLE001
            err = str(e)[:200]
    q = store.load(p.id)
    row = {"model": model or settings.model_writer, "temperature": temperature, "project": p.id, "error": err,
           "cost_usd": round(q.usage.llm_cost_usd, 4), "llm_calls": q.usage.llm_calls}
    if err or any(not s.version() for s in q.segments):
        row.update(score=0, fails=99, warns=0, attempts=0, review=0, naturalness={})
        return row
    rep = validate_project(store, p.id)
    attempts = [s.version().check.attempts for s in q.segments if s.version() and s.version().check]  # type: ignore[union-attr]
    nat = next((c["data"]["all"] for c in rep["checks"] if c["area"] == "naturalness" and "all" in c["data"]), {})
    row.update(score=rep["score"], fails=rep["counts"].get("fail", 0), warns=rep["counts"].get("warn", 0),
               attempts=round(sum(attempts) / max(1, len(attempts)), 2),
               review=sum(1 for s in q.segments if s.version() and s.version().check and s.version().check.needs_review),  # type: ignore[union-attr]
               naturalness=nat)
    return row


def run_trials(settings: Settings, text: str, *, models: list[Optional[str]], temperatures: list[float], base: Optional[ProjectSettings] = None,
               keep: bool = False, say: Progress = print) -> list[dict]:
    """Returns the rows best-first. Best = fewest failures/needs-review, then highest score, then cheapest."""
    store = ProjectStore(settings.data_dir)
    base = base or ProjectSettings(minutes=3)
    rows: list[dict] = []
    combos = list(itertools.product(models or [None], temperatures or [0.85]))
    for i, (m, t) in enumerate(combos, 1):
        say(f"Trial {i}/{len(combos)}: model={m or settings.model_writer}  temperature={t}")
        rows.append(_run_trial(settings, store, text, base, m, t, say))
        if not keep:
            try:
                store.delete(rows[-1]["project"])
            except Exception:  # noqa: BLE001
                pass
    rows.sort(key=lambda r: (r["fails"] + r["review"] * 3, -r["score"], r["cost_usd"]))
    return rows


def format_trials(rows: list[dict]) -> str:
    """Format the rows of a model/temperature trial as a table: score, failures, warnings, review flags, repairs, cost and naturalness figures."""
    head = f"{'#':>2} {'model':<38} {'temp':>5} {'score':>5} {'fail':>4} {'warn':>4} {'review':>6} {'repairs':>7} {'cost$':>7}  words/turn  questions  reactions"
    out = [head, "-" * len(head)]
    for i, r in enumerate(rows, 1):
        n = r.get("naturalness") or {}
        out.append(f"{i:>2} {str(r['model'])[:38]:<38} {r['temperature']:>5} {r['score']:>5} {r['fails']:>4} {r['warns']:>4} {r['review']:>6} "
                   f"{r['attempts']:>7} {r['cost_usd']:>7}  {n.get('avg_words_per_turn', '-'):>10}  {n.get('question_share', '-'):>9}  {n.get('reaction_share', '-'):>9}"
                   + (f"   ERROR: {r['error']}" if r["error"] else ""))
    if rows and not rows[0]["error"]:
        b = rows[0]
        out += ["", f"Best: model={b['model']}  writer_temperature={b['temperature']}",
                "Apply it with:  python -m podcast_studio.cli tune set writer_temperature=" + str(b["temperature"])
                + f"   and in .env:  LLM_MODEL_WRITER={b['model']}"]
    return "\n".join(out)


# ------------------------------------------------------------------------------------------------- voices
DEFAULT_SAMPLE_VOICES = ["Kavya", "Amit", "Ishita", "Sourav", "Anjali", "Arun", "Deepika", "Parth"]


def render_voice_samples(settings: Settings, out_dir: Path, *, voices: Optional[list[str]] = None, lang: str = "en",
                         text: Optional[str] = None, style: Optional[str] = None, say: Progress = print) -> Path:
    """Record the same sentence in several voices and write `index.html` with a player for each, so voices can be
    compared by ear. Returns the folder.
    """
    if lang not in LANGS:
        raise ValueError(f"Unknown language {lang}")
    voices = voices or DEFAULT_SAMPLE_VOICES
    bad = [v for v in voices if v not in VOICE_NAMES]
    if bad:
        raise ValueError(f"Unknown voice(s): {', '.join(bad)}. Run `voices` to list them.")
    text = text or SAMPLE_TEXT.get(lang) or SAMPLE_TEXT["en"]
    out_dir.mkdir(parents=True, exist_ok=True)
    _, bodhan, _ = make_clients(settings)
    rows = []
    for v in voices:
        say(f"Recording {v}...")
        arr = audio.process_clip(audio.wav_bytes_to_array(bodhan.tts(text, v, lang, style)))
        audio.write_wav(out_dir / f"{v}.wav", arr)
        rows.append(f'<tr><td><b>{html.escape(v)}</b><br><small>{VOICE_GENDER.get(v, "")}</small></td>'
                    f'<td><audio controls preload="none" src="{html.escape(v)}.wav"></audio></td></tr>')
    page = out_dir / "index.html"
    page.write_text(
        '<!doctype html><meta charset="utf-8"><title>Voice samples</title>'
        '<style>body{font:16px system-ui;margin:30px;max-width:720px}td{padding:8px 14px;vertical-align:middle}</style>'
        f"<h2>Voice samples: {html.escape(LANGS[lang]['name'])}</h2><p>{html.escape(text)}</p><table>{''.join(rows)}</table>", encoding="utf-8")
    return page


def describe_tuning() -> str:
    t = tuning.current()
    return "\n".join(f"  {k:<26} {v}" for k, v in asdict(t).items())
