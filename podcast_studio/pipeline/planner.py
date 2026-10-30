"""Episode planning: chapter units -> ordered segments (intro / body... / recap / outro), each with the units it
must draw on, the key points it must make, and a word budget."""
from __future__ import annotations

import math
from typing import Any, Optional

from .. import tuning
from ..models import PlanInfo, Segment, SourceUnit
from ..prompts import plan_prompt
from ..util import clamp
from ..voices import wpm

PLAN_CHUNK_CHARS = 70_000
KINDS = {"intro", "body", "recap", "outro"}
MIN_WORDS = {"intro": 80, "body": 110, "recap": 90, "outro": 60}


def auto_minutes(source_words: int) -> float:
    """~1 minute of podcast per 230 words of chapter text, kept within a sensible range."""
    return round(clamp(source_words / 230.0, 4.0, 22.0) * 2) / 2


def body_segment_count(minutes: float) -> int:
    return int(clamp(round(minutes / 2.2), 2, 14))


def _unit_payload(u: SourceUnit) -> dict:
    return {"id": u.id, "kind": u.kind, "section": u.section, "text": u.text}


def _parts(units: list[SourceUnit]) -> list[list[SourceUnit]]:
    total = sum(len(u.text) for u in units)
    if total <= PLAN_CHUNK_CHARS:
        return [units]
    n = math.ceil(total / (PLAN_CHUNK_CHARS * 0.8))
    target = total / n
    parts, cur, size = [], [], 0
    for u in units:
        if cur and size >= target and u.kind == "heading" and len(parts) < n - 1:
            parts.append(cur)
            cur, size = [], 0
        cur.append(u)
        size += len(u.text)
    if cur:
        parts.append(cur)
    return parts


def _validator(valid_ids: set[int]):
    def validate(obj: Any) -> dict:
        """Accept the model's plan if it has at least one segment with key points; drop unknown paragraph ids, cap the
        key points per segment, and raise ValueError (which is shown to the model for a retry) otherwise.
        """
        if not isinstance(obj, dict) or not isinstance(obj.get("segments"), list) or not obj["segments"]:
            raise ValueError("expected an object with a non-empty 'segments' list")
        segs = []
        for s in obj["segments"]:
            if not isinstance(s, dict):
                continue
            title = str(s.get("title") or "").strip()
            kps = [str(k).strip() for k in (s.get("key_points") or []) if str(k).strip()][:8]
            ids = []
            for i in s.get("unit_ids") or []:
                try:
                    i = int(i)
                except (TypeError, ValueError):
                    continue
                if i in valid_ids and i not in ids:
                    ids.append(i)
            try:
                share = float(s.get("share") or 0)
            except (TypeError, ValueError):
                share = 0.0
            kind = str(s.get("kind") or "body").strip().lower()
            segs.append({"kind": kind if kind in KINDS else "body", "title": title or "Untitled", "goal": str(s.get("goal") or ""),
                         "unit_ids": ids, "key_points": kps, "share": share if share > 0 else 0.0})
        if not segs:
            raise ValueError("no valid segments")
        if not any(s["key_points"] for s in segs):
            raise ValueError("segments must contain key_points")
        return {"episode_title": str(obj.get("episode_title") or "").strip(), "hook": str(obj.get("hook") or "").strip(),
                "segments": segs}

    return validate


def _repair_structure(segs: list[dict], units: list[SourceUnit], hook: str, title: str, *, first_part: bool, last_part: bool) -> list[dict]:
    """Guarantee intro first / outro last, give every substantive unit a home, and normalise shares."""
    subst = [u for u in units if u.kind != "heading"]
    first_ids = [subst[0].id] if subst else []
    last_ids = [subst[-1].id] if subst else []
    if first_part:
        if segs[0]["kind"] != "intro":
            segs.insert(0, {"kind": "intro", "title": title or "Welcome", "goal": "Welcome the listener and set up the topic",
                            "unit_ids": first_ids, "key_points": [hook] if hook else [], "share": 0.08})
        for s in segs[1:]:
            if s["kind"] == "intro":
                s["kind"] = "body"
    else:
        for s in segs:
            if s["kind"] in {"intro"}:
                s["kind"] = "body"
    if last_part:
        if segs[-1]["kind"] != "outro":
            segs.append({"kind": "outro", "title": "Wrap-up", "goal": "Thank the listener and sign off", "unit_ids": last_ids,
                         "key_points": [], "share": 0.05})
        for s in segs[:-1]:
            if s["kind"] == "outro":
                s["kind"] = "recap"
        if len(segs) >= 4 and segs[-2]["kind"] != "recap":
            segs[-2]["kind"] = "recap" if segs[-2]["kind"] == "body" and not segs[-2]["unit_ids"] else segs[-2]["kind"]
    else:
        for s in segs:
            if s["kind"] in {"outro", "recap"}:
                s["kind"] = "body"
    # --- coverage: attach unassigned substantive units to the nearest preceding body segment
    assigned = {i for s in segs for i in s["unit_ids"]}
    bodies = [s for s in segs if s["kind"] == "body"] or [s for s in segs if s["kind"] not in {"intro", "outro"}] or segs
    for u in subst:
        if u.id in assigned:
            continue
        prev = [s for s in bodies if s["unit_ids"] and min(s["unit_ids"]) <= u.id]
        tgt = max(prev, key=lambda s: min(s["unit_ids"])) if prev else bodies[0]
        tgt["unit_ids"].append(u.id)
        assigned.add(u.id)
    for s in segs:
        s["unit_ids"] = sorted(set(s["unit_ids"]))
        if not s["unit_ids"] and subst:
            s["unit_ids"] = last_ids if s["kind"] in {"recap", "outro"} else first_ids
    total = sum(s["share"] for s in segs) or 0.0
    if total <= 0:
        for s in segs:
            s["share"] = {"intro": 0.08, "outro": 0.05, "recap": 0.10}.get(s["kind"], 0.0)
        body = [s for s in segs if s["kind"] == "body"]
        rest = max(0.0, 1.0 - sum(s["share"] for s in segs))
        for s in body:
            s["share"] = rest / max(1, len(body))
        total = sum(s["share"] for s in segs) or 1.0
    for s in segs:
        s["share"] = max(0.01, s["share"]) / total
    return segs


def make_plan(llm, *, units: list[SourceUnit], title_hint: str, audience: str, language: str, minutes: float,
              extra: str, model: Optional[str]) -> tuple[PlanInfo, list[Segment]]:
    """Plan the episode: ask the planner model for an outline (intro, body segments with key points tied to chapter
    paragraph ids, recap, outro) sized to the requested minutes. Long chapters are planned in parts. Returns the
    episode info and the list of segments.
    """
    parts = _parts(units)
    total_words = int(minutes * wpm(language) * tuning.current().words_per_minute_scale)
    all_segs: list[dict] = []
    episode_title, hook = "", ""
    total_chars = sum(len(u.text) for u in units) or 1
    for pi, part in enumerate(parts, start=1):
        frac = sum(len(u.text) for u in part) / total_chars
        part_minutes = max(2.0, minutes * frac)
        n_body = body_segment_count(part_minutes)
        system, user = plan_prompt(audience=audience, language=language, minutes=part_minutes, n_body=n_body,
                                   units=[_unit_payload(u) for u in part], title_hint=title_hint, extra=extra,
                                   part_index=pi, part_count=len(parts))
        obj = llm.json(task="plan", system=system, user=user, model=model, temperature=tuning.current().planner_temperature, max_tokens=6000,
                       validate=_validator({u.id for u in part}))
        episode_title = episode_title or obj["episode_title"]
        hook = hook or obj["hook"]
        segs = _repair_structure(obj["segments"], part, hook, episode_title or title_hint,
                                 first_part=pi == 1, last_part=pi == len(parts))
        for s in segs:
            s["share"] *= frac
        all_segs.extend(segs)
    norm = sum(s["share"] for s in all_segs) or 1.0
    segments: list[Segment] = []
    for i, s in enumerate(all_segs):
        words = int(round(total_words * s["share"] / norm))
        segments.append(Segment(index=i, kind=s["kind"], title=s["title"], goal=s["goal"], key_points=s["key_points"],
                                unit_ids=s["unit_ids"], target_words=max(MIN_WORDS[s["kind"]], words)))
    plan = PlanInfo(episode_title=episode_title or title_hint, hook=hook, target_minutes=minutes,
                    total_words=sum(s.target_words for s in segments))
    return plan, segments
