"""The visual director: decides what is on screen for each segment (a storyboard of scenes).

Faithfulness is enforced the same way as for the script: the LLM may only re-use words/numbers from the script and the
chapter, deterministic checks verify that, problems go back to the model for repair, and anything still wrong is replaced
by a safe storyboard built from the script itself. A storyboard therefore never contains invented content.
"""
from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from .. import tuning
from ..clients.base import LLMError
from ..models import Figure, Project, Scene, Segment, SegmentStoryboard, SourceUnit
from ..prompts import direct_prompt
from ..text import contains_latin_letters, split_sentences, unsupported_numbers, word_count

log = logging.getLogger("podcast.director")

MAX_ITEMS = {"bullets": 5, "steps": 6, "cycle": 6, "recap": 5}
MIN_ITEMS = {"bullets": 2, "steps": 2, "cycle": 3, "recap": 2}
_SPLIT = re.compile(r"[\s\-–—,.;:!?()\[\]\"'“”‘’।॥/]+")


# ------------------------------------------------------------------------------------------------ grounding helpers
def _toks(text: str) -> list[str]:
    out = []
    for w in _SPLIT.split((text or "").casefold()):
        if not w or w.isdigit():
            continue
        latin = all(ord(c) < 0x250 for c in w)
        if len(w) >= (4 if latin else 3):
            out.append(w)
    return out


def _stem(w: str) -> str:
    return w[:5]


def grounding(scene: Scene, corpus: set[str]) -> float:
    """Share of the scene's words that also occur (by 5-letter stem) in the script/source. 1.0 when there is nothing to check."""
    toks = _toks(scene.visible_text())
    if not toks:
        return 1.0
    return sum(1 for t in toks if _stem(t) in corpus) / len(toks)


def build_corpus(*texts: str) -> set[str]:
    return {_stem(t) for text in texts for t in _toks(text)}


# ------------------------------------------------------------------------------------------------ normalisation
def _spread(n_items: int, lo: int, hi: int) -> list[int]:
    if n_items <= 0:
        return []
    span = max(0, hi - lo)
    return [lo + (span * k) // n_items for k in range(n_items)]


def normalize_scenes(scenes: list[Scene], n_turns: int) -> list[Scene]:
    """Make the scene ranges a perfect, gap-free cover of lines 0..n_turns-1 and fix reveal timings. Never raises."""
    n_turns = max(1, n_turns)
    sc = [s.model_copy(deep=True) for s in scenes if s is not None]
    for s in sc:
        s.from_turn = max(0, min(n_turns - 1, int(s.from_turn)))
        s.to_turn = max(0, min(n_turns - 1, int(s.to_turn)))
        if s.to_turn < s.from_turn:
            s.from_turn, s.to_turn = s.to_turn, s.from_turn
    sc.sort(key=lambda s: (s.from_turn, s.to_turn))
    out: list[Scene] = []
    for s in sc:
        if out and s.from_turn <= out[-1].to_turn:
            s.from_turn = out[-1].to_turn + 1
        if s.from_turn > s.to_turn:
            continue  # fully overlapped by the previous scene
        out.append(s)
    if not out:
        return []
    out[0].from_turn = 0
    for a, b in zip(out, out[1:]):
        a.to_turn = b.from_turn - 1
    out[-1].to_turn = n_turns - 1
    for s in out:
        n_items = len(s.rows) if s.layout == "compare" else len(s.items)
        if s.layout in MAX_ITEMS and len(s.items) > MAX_ITEMS[s.layout]:
            s.items = s.items[: MAX_ITEMS[s.layout]]
            n_items = len(s.items)
        if s.layout == "compare":
            s.rows = [r[:2] for r in s.rows if len(r) >= 2][:4]
            n_items = len(s.rows)
        rv = [min(s.to_turn, max(s.from_turn, int(x))) for x in s.reveal] if len(s.reveal) == n_items else _spread(n_items, s.from_turn, s.to_turn)
        for i in range(1, len(rv)):
            rv[i] = max(rv[i], rv[i - 1])
        if rv:
            rv[0] = s.from_turn
        s.reveal = rv
    return out


# ------------------------------------------------------------------------------------------------ checks
def check_scenes(scenes: list[Scene], *, n_turns: int, corpus: set[str], numbers_source: str, figure_ids: set[str], language: str,
                 translate: bool, brand: tuple[str, ...] = ()) -> list[tuple[int, str, str]]:
    """Returns [(scene_index, severity, message)]. 'major' means the scene must be repaired or replaced."""
    issues: list[tuple[int, str, str]] = []
    if not scenes:
        return [(-1, "major", "There are no scenes.")]
    used_fig: set[str] = set()
    for i, s in enumerate(scenes):
        def bad(msg: str, sev: str = "major", i: int = i, s: Scene = s) -> None:  # i, s bound now: called within this iteration
            issues.append((i, sev, f"Scene {i + 1} ({s.layout}): {msg}"))

        L = s.layout
        if L == "title" and not s.title.strip():
            bad("a title scene needs a title")
        if L in MIN_ITEMS and len([x for x in s.items if x.strip()]) < MIN_ITEMS[L]:
            bad(f"needs at least {MIN_ITEMS[L]} items")
        if L == "definition" and not (s.term.strip() and s.text.strip()):
            bad("a definition needs both a term and its meaning")
        if L == "keyfact" and not (s.term.strip() and s.text.strip()):
            bad("a keyfact needs a value and what it measures")
        if L == "compare" and not (len(s.columns) == 2 and s.rows and all(len(r) >= 2 for r in s.rows)):
            bad("a compare scene needs 2 column headings and rows of 2 cells")
        if L == "figure":
            if not s.figure or s.figure not in figure_ids:
                bad(f"figure id '{s.figure}' does not exist")
            elif s.figure in used_fig:
                bad(f"figure '{s.figure}' is already used in this storyboard", "minor")
            used_fig.add(s.figure or "")
        vis = s.visible_text()
        nums = unsupported_numbers(vis, numbers_source)
        if nums:
            bad(f"the number(s) {', '.join(nums)} are not in the script or chapter")
        if language != "en":
            shown = vis
            for b in brand:  # show name, host names, Latin titles from the plan: names are fine in any script
                if b:
                    shown = shown.replace(b, " ")
            letters = [c for c in shown if c.isalpha()]
            if letters and sum(1 for c in letters if contains_latin_letters(c)) / len(letters) > 0.25:
                bad(f"on-screen text must be in the {language} script, not Latin letters")
        g = grounding(s, corpus)
        if g < 0.4:
            bad(f"only {int(g * 100)}% of its words come from the script/chapter: show only what the hosts say")
        elif g < 0.7:
            bad(f"{int(g * 100)}% of its words come from the script/chapter; prefer the hosts' own wording", "minor")
        if word_count(vis) > 120:
            bad("too much text for one slide", "minor")
    return issues


# ------------------------------------------------------------------------------------------------ fallback
def _short(text: str, words: int = 10) -> str:
    w = text.replace("\n", " ").split()
    out = " ".join(w[:words]).strip(" ,;:-")
    return out + ("…" if len(w) > words else "")


def _first_sentence(text: str, words: int = 11) -> str:
    s = split_sentences(text)
    return _short((s[0] if s else text).rstrip(".!?।"), words)


def fallback_scenes(project: Project, seg: Segment, version, *, lo: Optional[int] = None, hi: Optional[int] = None) -> list[Scene]:
    """A safe storyboard made only from the script and the plan: no model call, no invention."""
    turns = version.turns
    lo = 0 if lo is None else lo
    hi = len(turns) - 1 if hi is None else hi
    if hi < lo or not turns:
        return []
    st = project.settings
    show, title = st.show_name, project.plan.episode_title or project.title
    def usable(point: str) -> bool:
        """Key points are written in the plan's language; only use them if they are really in the video's script."""
        if st.language == "en":
            return True
        letters = [c for c in point if c.isalpha()]
        return bool(letters) and sum(1 for c in letters if contains_latin_letters(c)) / len(letters) <= 0.25
    if seg.kind == "intro" and lo == 0:
        first_end = min(hi, max(lo, min(3, hi)))
        out = [Scene(layout="title", title=title or seg.title, text=show, from_turn=lo, to_turn=first_end)]
        lo = first_end + 1
        if lo > hi:
            return out
    elif seg.kind == "outro":
        return [Scene(layout="title", title=title or seg.title, text=show, from_turn=lo, to_turn=hi)]
    else:
        out = []
    words = sum(word_count(t.text) for t in turns[lo: hi + 1])
    per = max(25, int(tuning.current().scene_words))
    n = max(1, min(6, round(words / per)))
    span = hi - lo + 1
    n = min(n, span)
    points = [k for k in seg.key_points if k.strip() and usable(k)]
    for c in range(n):
        a = lo + (span * c) // n
        b = lo + (span * (c + 1)) // n - 1
        if c == n - 1:
            b = hi
        chunk = turns[a: b + 1]
        if seg.kind == "recap":
            items = [_short(k, 10) for k in points[:5]]
            if len(items) < 2:  # too few usable key points: use the hosts' own longest sentences, in the order they were said
                longest = sorted((t for t in chunk if word_count(t.text) >= 6), key=lambda t: -len(t.text))[:4]
                items = [_first_sentence(t.text) for t in sorted(longest, key=lambda t: chunk.index(t))]
            if len(items) < 2:
                out.append(Scene(layout="title", title=_short(seg.title, 8), text=show if c == 0 else "", from_turn=a, to_turn=b))
            else:
                out.append(Scene(layout="recap", title=seg.title, items=items[:5], from_turn=a, to_turn=b))
            continue
        share = points[(len(points) * c) // n:(len(points) * (c + 1)) // n] if points else []
        items = [_short(k, 10) for k in share][:5]
        if len(items) < 2:
            long_turns = [t for t in chunk if word_count(t.text) >= 8]
            items = [_first_sentence(t.text) for t in long_turns[:4]]
        if len(items) < 2:
            out.append(Scene(layout="title", title=_short(seg.title, 8), text=show if c == 0 else "", from_turn=a, to_turn=b))
        else:
            out.append(Scene(layout="bullets", title=_short(seg.title, 8), items=items, from_turn=a, to_turn=b))
    return out


# ------------------------------------------------------------------------------------------------ main entry
def figures_for(seg: Segment, figures: list[Figure]) -> list[Figure]:
    ids = set(seg.unit_ids)
    return [f for f in figures if f.unit_id in ids]


def _parse(obj) -> list[Scene]:
    if not isinstance(obj, dict) or not isinstance(obj.get("scenes"), list) or not obj["scenes"]:
        raise ValueError("expected an object with a non-empty 'scenes' list")
    scenes = []
    for raw in obj["scenes"]:
        if not isinstance(raw, dict):
            raise ValueError("each scene must be an object")
        d = {k: raw.get(k) for k in Scene.model_fields if raw.get(k) is not None}
        for k in ("title", "term", "text"):
            if k in d:
                d[k] = str(d[k]).strip()
        for k in ("items", "columns"):
            if k in d:
                d[k] = [str(x).strip() for x in d[k] if str(x).strip()] if isinstance(d[k], list) else []
        if "rows" in d:
            d["rows"] = [[str(c).strip() for c in r] for r in d["rows"] if isinstance(r, list)] if isinstance(d["rows"], list) else []
        if "reveal" in d:
            d["reveal"] = [int(x) for x in d["reveal"]] if isinstance(d["reveal"], list) else []
        for k in ("from_turn", "to_turn"):
            if k in d:
                d[k] = int(d[k])
        try:
            scenes.append(Scene(**d))
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"invalid scene: {str(e)[:160]}") from e
    return scenes


def brand_for(project: Project) -> tuple[str, ...]:
    """Names that may appear in Latin letters on any slide: the show, the hosts, and plan titles that are themselves Latin."""
    st = project.settings
    names = [st.show_name, st.host_a.name, st.host_b.name]
    for t in [project.plan.episode_title, project.title] + [s.title for s in project.segments]:
        letters = [c for c in (t or "") if c.isalpha()]
        if letters and sum(1 for c in letters if contains_latin_letters(c)) / len(letters) > 0.6:
            names.append(t)
    return tuple(sorted({n for n in names if n}, key=len, reverse=True))


def effective_language(project: Project, seg: Segment) -> str:
    """The language slides must be written in: the episode's language, unless the hosts' lines themselves are mostly Latin letters
    (then slides simply repeat what is said, and demanding another script would be impossible)."""
    st = project.settings
    v = seg.version()
    if st.language == "en" or not v:
        return st.language
    letters = [c for t in v.turns for c in t.text if c.isalpha()]
    if not letters:
        return st.language
    return "en" if sum(1 for c in letters if contains_latin_letters(c)) / len(letters) > 0.25 else st.language


def corpus_for(project: Project, seg: Segment, units: list[SourceUnit]) -> tuple[set[str], str]:
    """(word stems a slide may use, text its numbers may come from) for one segment."""
    v = seg.version()
    st = project.settings
    seg_units = [u for u in units if u.id in set(seg.unit_ids)]
    script = " ".join(t.text for t in v.turns) if v else ""
    same_lang = st.writing_mode == "direct" or st.language == "en"
    corpus = build_corpus(script, " ".join(u.text for u in seg_units) if same_lang else "", " ".join(seg.key_points) if same_lang else "",
                          st.show_name, project.plan.episode_title, project.title, st.host_a.name, st.host_b.name)
    numbers_source = script + " " + " ".join(u.text for u in units) + " " + " ".join(seg.key_points) + " " + st.show_name + " " + project.plan.episode_title
    return corpus, numbers_source


class Director:
    """Plans each segment's slides with the language model, checks the plan with rules, asks the model to repair it, and
    replaces anything still failing with a safe slide built from the script itself.
    """
    def __init__(self, llm, say: Callable[[str], None] = lambda m: None, max_repairs: int = 2):
        self.llm, self.say, self.max_repairs = llm, say, max_repairs

    def storyboard(self, project: Project, seg: Segment, units: list[SourceUnit], figures: list[Figure], *,
                   instruction: Optional[str] = None, base: Optional[SegmentStoryboard] = None, model: Optional[str] = None) -> SegmentStoryboard:
        """Plan the slides for one segment from the hosts' own lines. The plan is checked by rules (numbers and words
        must come from the script or chapter, native script for Indian languages, every line covered); problems go
        back to the model for up to `max_repairs` rounds, and what still fails becomes a fallback slide. Returns a
        `SegmentStoryboard`.
        """
        v = seg.version()
        if v is None:
            raise ValueError("This segment has no script yet.")
        st = project.settings
        n = len(v.turns)
        seg_units = [u for u in units if u.id in set(seg.unit_ids)]
        corpus, numbers_source = corpus_for(project, seg, units)
        figs = figures_for(seg, figures)
        fig_ids = {f.id for f in figs}
        words_per_scene = int(tuning.current().scene_words)

        def call(mode: str, previous: Optional[list[Scene]], problems: Optional[list[str]]) -> list[Scene]:
            """One director call: a fresh plan, or a repair of the previous plan given the problems found."""
            system, user = direct_prompt(
                language=effective_language(project, seg), show=st.show_name, episode_title=project.plan.episode_title or project.title,
                segment={"index": seg.index, "kind": seg.kind, "title": seg.title, "key_points": seg.key_points},
                turns=[{"i": i, "speaker": project.host(t.speaker).name, "words": word_count(t.text), "text": t.text} for i, t in enumerate(v.turns)],
                source_units=[{"id": u.id, "text": u.text[:600]} for u in seg_units][:12],
                figures=[{"id": f.id, "caption": f.caption, "near_text": f.near_text} for f in figs],
                scene_words=words_per_scene, hosts={"A": project.settings.host_a.name, "B": project.settings.host_b.name},
                mode=mode, instruction=instruction,
                previous=[s.model_dump(exclude_defaults=True) for s in previous] if previous else None, issues=problems)
            return self.llm.json(task="direct", system=system, user=user, model=model, temperature=tuning.current().director_temperature,
                                 max_tokens=max(2500, 900 + 120 * n), validate=_parse)

        translate = st.writing_mode == "translate"
        notes: list[str] = []
        try:
            mode = "reviewer_request" if (instruction and base) else "fresh"
            scenes = normalize_scenes(call(mode, base.scenes if base else None, None), n)
            for rnd in range(self.max_repairs + 1):
                found = check_scenes(scenes, n_turns=n, corpus=corpus, numbers_source=numbers_source, figure_ids=fig_ids,
                                     language=effective_language(project, seg), translate=translate, brand=brand_for(project))
                majors = [f for f in found if f[1] == "major"]
                if not majors:
                    notes = [m for _, s, m in found if s == "minor"]
                    break
                if rnd == self.max_repairs:
                    scenes, notes = self._patch_with_fallback(project, seg, v, scenes, majors), [m for _, _, m in majors]
                    self.say(f"Segment {seg.index + 1}: {len(majors)} visual problem(s) could not be fixed; those scenes use a safe layout")
                    break
                self.say(f"Segment {seg.index + 1}: fixing {len(majors)} visual problem(s)")
                scenes = normalize_scenes(call("revise_issues", scenes, [m for _, _, m in majors]), n)
            sb = SegmentStoryboard(index=seg.index, version=v.version, scenes=scenes, source="llm", instruction=instruction, issues=notes)
        except LLMError as e:
            if e.fatal:
                raise
            self.say(f"Segment {seg.index + 1}: the scene planner failed ({str(e)[:80]}); using a safe layout")
            sb = SegmentStoryboard(index=seg.index, version=v.version, scenes=normalize_scenes(fallback_scenes(project, seg, v), n),
                                   source="fallback", instruction=instruction, issues=[f"AI scene planner unavailable: {str(e)[:160]}"])
        return sb

    def _patch_with_fallback(self, project, seg, v, scenes: list[Scene], majors) -> list[Scene]:
        bad = {i for i, _, _ in majors}
        if -1 in bad:
            return normalize_scenes(fallback_scenes(project, seg, v), len(v.turns))
        out: list[Scene] = []
        for i, s in enumerate(scenes):
            if i in bad:
                out.extend(fallback_scenes(project, seg, v, lo=s.from_turn, hi=s.to_turn))
            else:
                out.append(s)
        return normalize_scenes(out, len(v.turns))


def fallback_storyboard(project: Project, seg: Segment) -> SegmentStoryboard:
    v = seg.version()
    assert v is not None
    return SegmentStoryboard(index=seg.index, version=v.version, scenes=normalize_scenes(fallback_scenes(project, seg, v), len(v.turns)),
                             source="fallback", issues=[])
