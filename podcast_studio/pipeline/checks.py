"""Faithfulness and quality checks for a draft segment: cheap deterministic rules + an LLM fact-checker."""
from __future__ import annotations

from typing import Any, Optional

from .. import tuning
from ..models import CheckReport, Issue, Segment, SourceUnit, Turn
from ..prompts import check_prompt
from ..text import contains_latin_letters, unsupported_numbers, word_count

REST_OF_CHAPTER_MAX = 60_000


def rule_checks(turns: list[Turn], seg: Segment, *, all_source_text: str, lang: str, translate_master: bool) -> list[Issue]:
    """The model-free checks on one segment's lines. A number that is not in the chapter (or the segment's key points) is
    a major issue. Also flagged: turns over 95 words, Latin letters in a non-English episode (the voice needs native
    script), and a segment far over or under its target length.
    """
    issues: list[Issue] = []
    for i, t in enumerate(turns):
        text = t.text
        bad = unsupported_numbers(text, all_source_text + " " + " ".join(seg.key_points))
        if bad:
            issues.append(Issue(turn=i, severity="major", type="numeric", source="rule",
                                detail=f"The number(s) {', '.join(bad)} do not appear anywhere in the chapter.",
                                suggested_fix="Remove the figure or say it in general terms."))
        wc = word_count(text)
        if wc > 95:
            issues.append(Issue(turn=i, severity="minor", type="style", source="rule",
                                detail=f"This turn is {wc} words - too long for natural conversation.",
                                suggested_fix="Split it into two turns with a short reaction or question between."))
        if lang != "en" and not translate_master:
            letters = [c for c in text if c.isalpha()]
            if letters:
                latin = sum(1 for c in letters if contains_latin_letters(c))
                if latin / len(letters) > 0.25:
                    issues.append(Issue(turn=i, severity="major", type="style", source="rule",
                                        detail="Contains Latin letters; the voice engine needs native script.",
                                        suggested_fix="Rewrite in the native script, transliterating English terms."))
                elif latin:
                    issues.append(Issue(turn=i, severity="minor", type="style", source="rule",
                                        detail="Contains some Latin letters; prefer native script.",
                                        suggested_fix="Transliterate the English words."))
    total = sum(word_count(t.text) for t in turns)
    if seg.target_words:
        if total > 2.0 * seg.target_words:
            issues.append(Issue(severity="major", type="style", source="rule",
                                detail=f"Segment is {total} words but the target is about {seg.target_words}.",
                                suggested_fix="Tighten: keep every key point but cut repetition."))
        elif total < 0.4 * seg.target_words and seg.target_words >= 120:
            issues.append(Issue(severity="major", type="style", source="rule",
                                detail=f"Segment is only {total} words but the target is about {seg.target_words}.",
                                suggested_fix="Develop the explanation with examples and more back-and-forth."))
    return issues


def _validate_report(obj: Any) -> dict:
    if not isinstance(obj, dict):
        raise ValueError("expected an object")
    verdict = str(obj.get("verdict", "")).lower()
    if verdict not in {"pass", "revise"}:
        raise ValueError("'verdict' must be 'pass' or 'revise'")
    issues = []
    for it in obj.get("issues") or []:
        if not isinstance(it, dict):
            continue
        sev = str(it.get("severity", "minor")).lower()
        try:
            turn = int(it["turn"]) if it.get("turn") is not None else None
        except (TypeError, ValueError):
            turn = None
        try:
            ev = int(it["evidence_unit"]) if it.get("evidence_unit") is not None else None
        except (TypeError, ValueError):
            ev = None
        issues.append(Issue(turn=turn, severity="major" if sev == "major" else "minor", type=str(it.get("type", "other"))[:30],
                            detail=str(it.get("detail", ""))[:500], evidence_unit=ev,
                            suggested_fix=str(it.get("suggested_fix", ""))[:500], source="llm"))
    missing = [str(m) for m in (obj.get("missing_points") or []) if str(m).strip()]
    return {"verdict": verdict, "issues": issues, "missing_points": missing, "notes": str(obj.get("notes", ""))[:500]}


def llm_check(llm, *, turns: list[Turn], seg: Segment, seg_units: list[SourceUnit], all_units: list[SourceUnit], lang: str,
              translate_master: bool, model: Optional[str]) -> CheckReport:
    """Ask the checker model whether every claim in the lines is supported by the segment's chapter paragraphs (or
    elsewhere in the chapter), and which key points are missing. Very long chapters send only the neighbouring
    paragraphs to keep the check affordable.
    """
    assigned_ids = {u.id for u in seg_units}
    rest = [u for u in all_units if u.id not in assigned_ids and u.kind != "heading"]
    rest_text = "\n".join(f"[{u.id}] {u.text}" for u in rest)
    if len(rest_text) > REST_OF_CHAPTER_MAX:
        # very long chapters: only neighbouring units, to keep the check affordable
        lo, hi = (min(assigned_ids), max(assigned_ids)) if assigned_ids else (0, 0)
        near = [u for u in rest if lo - 3 <= u.id <= hi + 3]
        rest_text = "\n".join(f"[{u.id}] {u.text}" for u in near)
    system, user = check_prompt(
        language="en" if translate_master else lang,
        segment={"title": seg.title, "kind": seg.kind, "goal": seg.goal, "key_points": seg.key_points},
        turns=[{"i": i, "speaker": t.speaker, "text": (t.text_en or t.text) if translate_master else t.text} for i, t in enumerate(turns)],
        source_units=[{"id": u.id, "text": u.text} for u in seg_units],
        rest_of_chapter=rest_text or None,
    )
    obj = llm.json(task="check", system=system, user=user, model=model, temperature=tuning.current().checker_temperature, max_tokens=3000,
                   validate=_validate_report)
    return CheckReport(verdict=obj["verdict"], issues=obj["issues"], missing_points=obj["missing_points"], notes=obj["notes"])


def merge_reports(rule_issues: list[Issue], llm_report: Optional[CheckReport], attempts: int) -> CheckReport:
    """Combine the rule issues and the model's report into one `CheckReport`. Any major issue or missing point makes the
    verdict `revise`; with no model report the verdict is `unchecked`.
    """
    if llm_report is None:
        rep = CheckReport(verdict="unchecked", issues=list(rule_issues), attempts=attempts)
    else:
        rep = CheckReport(verdict=llm_report.verdict, issues=list(rule_issues) + list(llm_report.issues),
                          missing_points=list(llm_report.missing_points), notes=llm_report.notes, attempts=attempts)
    if rep.has_blockers:
        rep.verdict = "revise"
    elif llm_report is not None:
        rep.verdict = "pass"
    return rep
