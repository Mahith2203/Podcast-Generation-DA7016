"""Write one segment's dialogue, fact-check it against the chapter, repair it, and (in translate mode) translate it."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from .. import tuning
from ..config import Settings
from ..models import CheckReport, Issue, Project, Segment, SegmentVersion, SourceUnit, Turn
from ..prompts import polish_prompt, write_prompt
from ..text import clean_spoken
from .checks import llm_check, merge_reports, rule_checks

log = logging.getLogger("podcast.writer")


def _speaker(raw: Any, names: dict[str, str]) -> Optional[str]:
    s = str(raw or "").strip()
    if s.upper() in {"A", "B"}:
        return s.upper()
    low = s.lower()
    if low in {"host a", "a:", "host_a"}:
        return "A"
    if low in {"host b", "b:", "host_b"}:
        return "B"
    for k, name in names.items():
        if name and low == name.lower():
            return k
    return None


def make_turn_validator(*, lang: str, names: dict[str, str], allowed_styles: list[str]) -> Callable[[Any], dict]:
    """Build the function that parses a model's script reply: a list of turns with a valid speaker, cleaned speakable
    text and an allowed style. The client shows the model its mistake and retries when it raises.
    """
    labels = tuple(n for n in names.values() if n)

    def validate(obj: Any) -> dict:
        """Parse one reply into `{"turns": [...]}`; raises ValueError if the shape is wrong or fewer than two turns remain."""
        raw = obj.get("turns") if isinstance(obj, dict) else obj
        if not isinstance(raw, list) or not raw:
            raise ValueError("expected {\"turns\": [ ... ]} with at least one turn")
        turns: list[Turn] = []
        for i, t in enumerate(raw):
            if not isinstance(t, dict):
                raise ValueError(f"turn {i} is not an object")
            sp = _speaker(t.get("speaker"), names)
            if sp is None:
                raise ValueError(f"turn {i}: 'speaker' must be \"A\" or \"B\"")
            text = clean_spoken(str(t.get("text") or ""), lang, labels=labels)
            if not text or not any(c.isalnum() for c in text):
                continue
            style = t.get("style")
            style = style if isinstance(style, str) and style in allowed_styles else None
            reaction = bool(t.get("reaction")) and len(text.split()) <= 8
            turns.append(Turn(speaker=sp, text=text, style=style, reaction=reaction))
        if len(turns) < 2:
            raise ValueError("a segment needs at least two non-empty turns")
        return {"turns": turns}

    return validate


class ScriptWriter:
    """Writes one segment's dialogue: drafts it with the writer model, checks it (number rule and fact-checker) and
    repairs it up to `max_revisions` times. A segment that still fails is flagged `needs_review`, not hidden.
    """
    def __init__(self, llm, bodhan, app: Settings, say: Callable[[str], None] | None = None):
        self.llm, self.bodhan, self.app = llm, bodhan, app
        self.say = say or (lambda m: None)

    # ------------------------------------------------------------------ public
    def write(self, project: Project, seg: Segment, *, units: list[SourceUnit], instruction: Optional[str] = None,
              base: Optional[SegmentVersion] = None) -> SegmentVersion:
        """Write or rewrite one segment and return it as a new `SegmentVersion`: draft, rule checks and fact-check, the
        repair loop, and (in translate mode) a translation that keeps the English as `text_en`.
        """
        st = project.settings
        translate = st.writing_mode == "translate" and st.language != "en"
        write_lang = "en" if translate else st.language
        wmodel = st.writer_model or self.app.model_writer
        cmodel = st.checker_model or self.app.model_checker
        by_id = {u.id: u for u in units}
        seg_units = [by_id[i] for i in seg.unit_ids if i in by_id]
        all_text = " ".join(u.text for u in units) + " " + project.plan.episode_title + " " + project.plan.hook
        names = {"A": project.settings.host_a.name, "B": project.settings.host_b.name}
        validate = make_turn_validator(lang=write_lang, names=names, allowed_styles=st.allowed_styles)

        prev_turns = self._previous_turns(project, seg)
        covered = [f"{s.title}: " + "; ".join(s.key_points[:3]) for s in project.segments if s.index < seg.index]
        nxt = project.segments[seg.index + 1].title if seg.index + 1 < len(project.segments) else None

        def call(mode: str, draft: Optional[list[Turn]], issues: Optional[list[Issue]], missing: Optional[list[str]]) -> list[Turn]:
            """One model call: a fresh draft, or a repair of the previous draft given the issues and missing points found."""
            system, user = write_prompt(
                show=st.show_name, audience=st.audience, write_lang=write_lang, final_lang=st.language, translate_mode=translate,
                hosts={"A": st.host_a.model_dump(), "B": st.host_b.model_dump()},
                segment={"index": seg.index, "kind": seg.kind, "title": seg.title, "goal": seg.goal,
                         "key_points": seg.key_points, "target_words": seg.target_words},
                n_segments=len(project.segments), next_title=nxt, episode_title=project.plan.episode_title,
                episode_hook=project.plan.hook, already_covered=covered, previous_turns=prev_turns,
                source_units=[{"id": u.id, "section": u.section, "text": u.text} for u in seg_units],
                allowed_styles=st.allowed_styles, extra=st.extra_instructions, mode=mode, reviewer_instruction=instruction,
                draft=[{"i": i, "speaker": t.speaker, "text": t.text} for i, t in enumerate(draft)] if draft else None,
                issues=[i.model_dump(include={"turn", "severity", "type", "detail", "suggested_fix"}) for i in issues] if issues else None,
                missing_points=missing or None)
            out = self.llm.json(task="write", system=system, user=user, model=wmodel, temperature=tuning.current().writer_temperature,
                                max_tokens=max(2500, int(seg.target_words * 9)), validate=validate)
            return out["turns"]

        mode0 = "reviewer_request" if (instruction and base) else "fresh"
        turns = call(mode0, base.turns if base else None, None, None)
        attempts, report = 1, CheckReport(verdict="unchecked")
        for rev in range(st.max_revisions + 1):
            rule = rule_checks(turns, seg, all_source_text=all_text, lang=write_lang, translate_master=translate)
            llm_rep = None
            if st.fact_check:
                llm_rep = llm_check(self.llm, turns=turns, seg=seg, seg_units=seg_units, all_units=units, lang=write_lang,
                                    translate_master=False, model=cmodel)
            report = merge_reports(rule, llm_rep, attempts)
            if not report.has_blockers:
                break
            if rev == st.max_revisions:
                report.needs_review = True
                self.say(f"Segment {seg.index + 1}: still has {len(report.issues)} issue(s) after {attempts} attempt(s) - flagged for review")
                break
            self.say(f"Segment {seg.index + 1}: fixing {sum(1 for i in report.issues if i.severity == 'major')} problem(s) found by the checker")
            turns = call("revise_issues", turns, report.issues, report.missing_points)
            attempts += 1
        report.attempts = attempts

        if translate:
            turns = self.translate_turns(turns, st.language, st.polish_translation, names)

        v = SegmentVersion(version=seg.next_version_number(), origin="rewrite" if base else "generated",
                           instruction=instruction, turns=turns, check=report)
        return v

    # ------------------------------------------------------------------ helpers
    def _previous_turns(self, project: Project, seg: Segment) -> list[dict]:
        if seg.index == 0:
            return []
        prev = project.segments[seg.index - 1].version()
        if not prev:
            return []
        src = [(t.text_en or t.text) if project.settings.writing_mode == "translate" else t.text for t in prev.turns[-6:]]
        return [{"speaker": t.speaker, "text": txt} for t, txt in zip(prev.turns[-6:], src)]

    def translate_turns(self, turns: list[Turn], lang: str, polish: bool, names: dict[str, str]) -> list[Turn]:
        """Translate lines with Bodhan indic-translate (several at a time), keep the English as `text_en`, and clean the
        result so it can be spoken.
        """
        labels = tuple(n for n in names.values() if n)

        def one(t: Turn) -> str:
            return self.bodhan.translate(t.text, lang)

        with ThreadPoolExecutor(max_workers=self.app.mt_concurrency) as ex:
            translated = list(ex.map(one, turns))
        out = [t.model_copy(update={"text_en": t.text, "text": clean_spoken(tr, lang, labels=labels)}) for t, tr in zip(turns, translated)]
        if polish:
            try:
                system, user = polish_prompt(language=lang, items=[{"i": i, "text": t.text_en, "translation": t.text} for i, t in enumerate(out)])
                obj = self.llm.json(task="polish", system=system, user=user, model=self.app.model_writer, temperature=0.3,
                                    max_tokens=4000, validate=lambda o: {int(x["i"]): str(x["text"]) for x in o["turns"]})
                for i, t in enumerate(out):
                    new = clean_spoken(obj.get(i, ""), lang, labels=labels)
                    if new:
                        t.text = new
            except Exception as e:  # noqa: BLE001 - polishing is best-effort; the plain translation is still usable
                log.warning("polish pass skipped: %s", e)
        return out
