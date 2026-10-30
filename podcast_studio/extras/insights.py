"""Quality insights: everything the pipeline knows about how trustworthy and how natural an episode is, as numbers.

Feeds the "Quality" dashboard in the UI and the printable evidence report (the thing to attach to a project report).
Nothing here calls a model: it reads the saved project, the chapter and the finished audio.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from ..models import Project, SourceUnit
from ..store import ProjectStore
from ..text import dominant_script, unsupported_numbers
from ..validate import script_metrics, validate_project
from ..voices import LANGS
from . import ask as ask_mod
from . import learn as learn_mod

_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "")}


def _segment_rows(p: Project) -> list[dict]:
    rows = []
    for s in p.segments:
        v = s.version()
        row = {"index": s.index + 1, "kind": s.kind, "title": s.title, "status": s.status, "key_points": len(s.key_points),
               "units": len(s.unit_ids), "versions": len(s.versions), "words": 0, "turns": 0, "seconds": 0.0, "verdict": "none",
               "attempts": 0, "needs_review": False, "major": 0, "minor": 0, "missing": 0, "origin": "", "question_share": 0.0,
               "reaction_share": 0.0, "a_share": 0.0}
        if v:
            m = script_metrics(v.turns)
            row.update(words=v.words, turns=len(v.turns), seconds=float(v.duration or 0), origin=v.origin,
                       question_share=m["question_share"], reaction_share=m["reaction_share"], a_share=m["host_balance_a"])
            if v.check:
                row.update(verdict=v.check.verdict, attempts=v.check.attempts, needs_review=v.check.needs_review,
                           major=sum(1 for i in v.check.issues if i.severity == "major"),
                           minor=sum(1 for i in v.check.issues if i.severity == "minor"), missing=len(v.check.missing_points))
        rows.append(row)
    return rows


def _speaking(p: Project) -> dict:
    sec = {"A": 0.0, "B": 0.0}
    words = {"A": 0, "B": 0}
    turns = {"A": 0, "B": 0}
    questions = {"A": 0, "B": 0}
    reactions = {"A": 0, "B": 0}
    for s in p.segments:
        v = s.version()
        if not v:
            continue
        for i, t in enumerate(v.turns):
            words[t.speaker] += len(t.text.split())
            turns[t.speaker] += 1
            questions[t.speaker] += ("?" in t.text or "؟" in t.text)
            reactions[t.speaker] += bool(t.reaction)
            if i < len(v.turn_times):
                a, b = v.turn_times[i]
                sec[t.speaker] += max(0.0, b - a)
    tot = max(1e-9, sec["A"] + sec["B"]) if (sec["A"] + sec["B"]) > 0 else max(1, words["A"] + words["B"])
    base = sec if (sec["A"] + sec["B"]) > 0 else words
    return {"A": {"name": p.settings.host_a.name, "seconds": round(sec["A"], 1), "words": words["A"], "turns": turns["A"],
                  "questions": questions["A"], "reactions": reactions["A"], "share": round(base["A"] / tot, 3)},
            "B": {"name": p.settings.host_b.name, "seconds": round(sec["B"], 1), "words": words["B"], "turns": turns["B"],
                  "questions": questions["B"], "reactions": reactions["B"], "share": round(base["B"] / tot, 3)}}


def _coverage(p: Project, units: list[SourceUnit]) -> dict:
    translate = p.settings.writing_mode == "translate"
    seg_text = {s.index: " ".join((t.text_en or t.text) if translate else t.text for t in s.version().turns).lower()
                for s in p.segments if s.version()}
    src = " ".join(u.text for u in units)
    spoken = " ".join(seg_text.values())
    comparable = bool(spoken) and (translate and dominant_script(src) == "Latin" or dominant_script(src) == dominant_script(spoken))
    unit_seg: dict[int, int] = {}
    for s in p.segments:
        for uid in s.unit_ids:
            unit_seg.setdefault(uid, s.index)
    cells = []
    for u in units:
        if u.kind == "heading":
            cells.append({"id": u.id, "kind": "heading", "status": "heading", "section": u.section, "segment": None, "chars": len(u.text)})
            continue
        sid = unit_seg.get(u.id)
        if sid is None:
            st, ratio = "missing", 0.0
        elif not comparable:
            st, ratio = "planned", None
        else:
            uw = _words(u.text)
            ratio = (sum(1 for w in uw if w in seg_text.get(sid, "")) / len(uw)) if len(uw) >= 5 else 1.0
            st = "covered" if ratio >= 0.25 else "thin"
        cells.append({"id": u.id, "kind": u.kind, "status": st, "section": u.section, "segment": None if sid is None else sid + 1,
                      "ratio": None if ratio is None else round(ratio, 2), "chars": len(u.text)})
    body = [c for c in cells if c["status"] != "heading"]
    ok = sum(1 for c in body if c["status"] in {"covered", "planned"})
    return {"comparable": comparable, "cells": cells, "body_units": len(body),
            "covered_share": round(ok / max(1, len(body)), 3),
            "counts": dict(Counter(c["status"] for c in body))}


def _grounding(p: Project, units: list[SourceUnit]) -> dict:
    translate = p.settings.writing_mode == "translate"
    src = " ".join(u.text for u in units) + " " + " ".join(k for s in p.segments for k in s.key_points)
    turns = bad_turns = 0
    bad: list[str] = []
    for s in p.segments:
        v = s.version()
        for i, t in enumerate(v.turns if v else []):
            turns += 1
            txt = (t.text_en or t.text) if translate else t.text
            b = unsupported_numbers(txt, src)
            if b:
                bad_turns += 1
                bad.append(f"segment {s.index + 1}, line {i + 1}: {', '.join(b)}")
    return {"turns": turns, "turns_with_unsupported_numbers": bad_turns, "examples": bad[:5],
            "grounded_share": round(1 - bad_turns / max(1, turns), 3)}


def compute_insights(store: ProjectStore, pid: str, with_validation: bool = True, demo_mode: bool = False) -> dict:
    """Build the Quality dashboard for an episode from what is saved on disk, with no model calls: the validator score,
    the fact-check and repair counts, how much of the chapter is covered, how many spoken numbers appear in the
    chapter, host balance and naturalness, Ask usage, and cost per audio minute.
    """
    p = store.load(pid)
    units = store.load_units(pid) if store.has_units(pid) else []
    rows = _segment_rows(p)
    checked = [r for r in rows if r["verdict"] != "none"]
    loop = {"segments": len(rows), "checked": sum(1 for r in checked if r["verdict"] in {"pass", "revise"}),
            "passed_first_try": sum(1 for r in checked if r["verdict"] == "pass" and r["attempts"] <= 1),
            "repaired": sum(1 for r in checked if r["verdict"] == "pass" and r["attempts"] > 1),
            "needs_review": sum(1 for r in checked if r["needs_review"]), "repair_rounds": sum(max(0, r["attempts"] - 1) for r in checked),
            "fact_check_on": p.settings.fact_check, "max_revisions": p.settings.max_revisions}
    turns = [t for s in p.segments if s.version() for t in s.version().turns]  # type: ignore[union-attr]
    dur = p.final.duration if p.final else 0.0
    u = p.usage
    validation: Optional[dict] = None
    if with_validation:
        try:
            validation = validate_project(store, pid)
        except Exception:  # noqa: BLE001 - the dashboard still shows everything else
            validation = None
    learn = learn_mod.load_pack(store, pid)
    asks = ask_mod.history(store, pid)
    out = {
        "id": p.id, "title": p.plan.episode_title or p.title, "language": p.settings.language, "demo_mode": demo_mode,
        "language_name": LANGS.get(p.settings.language, {}).get("name", p.settings.language),
        "writing_mode": p.settings.writing_mode, "status": p.status, "show_name": p.settings.show_name,
        "audience": p.settings.audience, "hosts": {"A": p.settings.host_a.name, "B": p.settings.host_b.name},
        "voices": {"A": p.settings.host_a.voice, "B": p.settings.host_b.voice},
        "generated_at": p.created_at, "audio_built_at": p.final.built_at if p.final else "",
        "duration_s": round(dur, 1), "target_minutes": p.plan.target_minutes, "words": sum(r["words"] for r in rows),
        "source": {"filename": p.source.filename, "pages": p.source.pages, "words": p.source.words, "units": p.source.unit_count, "ocr_used": p.source.ocr_used},
        "segments": rows, "loop": loop, "speaking": _speaking(p),
        "script": script_metrics(turns) if turns else {}, "coverage": _coverage(p, units) if units else None,
        "grounding": _grounding(p, units) if units else None, "validation": validation,
        "models": {"writer": p.settings.writer_model or "", "checker": p.settings.checker_model or ""},
        "cost": {"llm_calls": u.llm_calls, "tokens": u.llm_prompt_tokens + u.llm_completion_tokens, "usd": round(u.llm_cost_usd, 4),
                 "usd_per_audio_minute": round(u.llm_cost_usd / (dur / 60), 4) if dur > 60 else None, "tts_calls": u.tts_calls,
                 "tts_chars": u.tts_chars, "tts_cache_hits": u.tts_cache_hits, "mt_calls": u.mt_calls, "mt_chars": u.mt_chars},
        "learn": None if not learn else {"status": learn.get("status"), "kept": (learn.get("meta") or {}).get("kept"),
                                         "generated": (learn.get("meta") or {}).get("generated"),
                                         "dropped": len((learn.get("meta") or {}).get("dropped") or []),
                                         "verified_by_model": (learn.get("meta") or {}).get("verified_by_model")},
        "ask": {"questions": len(asks), "grounded": sum(1 for a in asks if a.get("grounded")),
                "declined": sum(1 for a in asks if not a.get("grounded")), "needs_review": sum(1 for a in asks if a.get("needs_review"))},
    }
    return out
