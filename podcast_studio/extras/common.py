"""Shared helpers for the study-pack, Ask-the-hosts and insights features.

Everything in this package is additive: it reads the project the normal pipeline made (source units, plan, scripts,
audio) and stores its own results in <project>/extras/, so removing the folder never harms an episode.
"""
from __future__ import annotations

import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..models import Project, SourceUnit
from ..store import ProjectStore
from ..text import split_sentences

_TOKEN = re.compile(r"[^\W\d_]{2,}|\d+(?:\.\d+)?", re.UNICODE)
_STOP = set("""a an the is are was were be been being am do does did done have has had having can could will would shall should may might must
what which who whom whose when where why how that this these those there here it its they them their you your we our us he she his her
of in on at to for from by with about into over under and or but if then than so as not no yes tell explain please me my mean means
meant say said give gave also very just more most much many some any all each other such only own same too""".split())


# --------------------------------------------------------------------------- environment
def is_mock(llm: Any) -> bool:
    """Demo mode uses an offline brain that only knows the original tasks, so the new features have offline twins."""
    from ..clients.mock import MockLLM
    return isinstance(llm, MockLLM)


def extras_dir(store: ProjectStore, pid: str) -> Path:
    d = store.dir(pid) / "extras"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_mode(p: Project) -> tuple[bool, str]:
    """(translate_mode, language the model should write in)."""
    st = p.settings
    translate = st.writing_mode == "translate" and st.language != "en"
    return translate, ("en" if translate else st.language)


def models_for(p: Project, settings) -> tuple[str, str]:
    return (p.settings.writer_model or settings.model_writer, p.settings.checker_model or settings.model_checker)


def flush_usage(store: ProjectStore, pid: str, meter) -> None:
    """Add what this request cost to the episode's running totals (same bookkeeping the pipeline does)."""
    now = meter.snapshot()

    def _f(p: Project) -> None:
        u = p.usage
        u.llm_calls += now.llm_calls
        u.llm_prompt_tokens += now.llm_prompt_tokens
        u.llm_completion_tokens += now.llm_completion_tokens
        u.llm_cost_usd = round(u.llm_cost_usd + now.llm_cost_usd, 6)
        u.tts_calls += now.tts_calls
        u.tts_chars += now.tts_chars
        u.tts_cache_hits += now.tts_cache_hits
        u.mt_calls += now.mt_calls
        u.mt_chars += now.mt_chars

    store.update(pid, _f)


def translate_strings(bodhan, texts: list[str], lang: str, workers: int = 4) -> list[str]:
    """Machine-translate English strings into the podcast language (empty strings stay empty)."""
    def one(t: str) -> str:
        return bodhan.translate(t, lang) if t.strip() else t

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(one, texts))


# --------------------------------------------------------------------------- the chapter
def body_units(units: list[SourceUnit]) -> list[SourceUnit]:
    return [u for u in units if u.kind != "heading" and len(u.text.strip()) > 25]


def tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")]


def budget_units(units: list[SourceUnit], max_chars: int = 26000) -> list[dict]:
    """The chapter as LLM input. A long chapter is shortened evenly (every unit keeps its beginning) instead of cut off."""
    use = [u for u in units if u.kind != "heading" or u.text.strip()]
    total = sum(len(u.text) for u in use)
    cap = None
    if total > max_chars and use:
        cap = max(160, int(max_chars / len(use)))
    out = []
    for u in use:
        t = u.text if cap is None or len(u.text) <= cap else u.text[:cap].rsplit(" ", 1)[0] + " ..."
        out.append({"id": u.id, "section": u.section, "text": t})
    return out


def rank_units(question: str, units: list[SourceUnit], k: int = 6) -> list[tuple[float, SourceUnit]]:
    """Small TF-IDF ranker that works for every script (no stemmer: words match on a 5-letter prefix, which also
    catches Indic inflection). Used to pick which chapter paragraphs the hosts may answer from."""
    pool = body_units(units) or units
    q = [t for t in tokens(question) if t not in _STOP]
    if not q or not pool:
        return []
    docs = [tokens(u.text + " " + u.section) for u in pool]
    n = len(docs)
    pre = lambda w: w[:5]  # noqa: E731
    df: dict[str, int] = {}
    for d in docs:
        for w in {pre(x) for x in d}:
            df[w] = df.get(w, 0) + 1
    idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
    qset = {pre(w) for w in q}
    scored: list[tuple[float, SourceUnit]] = []
    for u, d in zip(pool, docs):
        bag: dict[str, int] = {}
        for w in d:
            bag[pre(w)] = bag.get(pre(w), 0) + 1
        s = 0.0
        for w in qset:
            tf = bag.get(w, 0)
            if tf:
                s += idf.get(w, math.log(1 + n)) * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * len(d) / max(1.0, 80.0)))
        if s > 0:
            scored.append((s, u))
    scored.sort(key=lambda x: -x[0])
    return scored[:k]


def query_overlap(question: str, text: str) -> float:
    """Share of the question's content words (5-letter prefixes) that appear in `text`, in 0..1."""
    q = {w[:5] for w in tokens(question) if len(w) > 2 and w not in _STOP}
    if not q:
        return 0.0
    t = {w[:5] for w in tokens(text)}
    return len(q & t) / len(q)


def first_sentences(text: str, n: int = 1, limit: int = 260) -> str:
    s = split_sentences(text)
    out = " ".join(s[:n]) if s else (text or "")
    return out[:limit].rstrip()


# --------------------------------------------------------------------------- small guards
class Busy(RuntimeError):
    """Raised when the same project is already answering another request."""
    pass


class OneAtATime:
    """Per-project try-lock so a double click cannot start two expensive requests for the same episode."""

    def __init__(self) -> None:
        self._g = threading.Lock()
        self._held: set[str] = set()

    def acquire(self, key: str) -> bool:
        with self._g:
            if key in self._held:
                return False
            self._held.add(key)
            return True

    def release(self, key: str) -> None:
        with self._g:
            self._held.discard(key)
