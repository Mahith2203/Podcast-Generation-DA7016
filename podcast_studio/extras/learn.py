"""Study pack: key takeaways, glossary and a quiz made from the chapter.

Same promise as the podcast itself: nothing is allowed in that the chapter does not say.
  1. the writer model only sees the chapter and must cite the paragraph (unit) each item comes from
  2. rule check: any number that is not in the chapter drops the item
  3. verifier model (the fact-checker model): does the cited paragraph support it? For quiz items it also checks
     that exactly one option is right. Items that fail are dropped, not repaired, and counted in `meta`
  4. translate mode: written in English, then translated with the same Bodhan MT used for the script
In demo mode an offline generator builds the same structures straight from the chapter's own sentences.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
from typing import Any, Callable, Optional

from ..models import Project, SourceUnit
from ..prompts import TASK_TAG, lang_block, payload_block
from ..store import ProjectStore
from ..text import split_sentences, unsupported_numbers
from ..util import atomic_write_text, now_iso, read_text
from ..voices import LANGS
from .common import (
    body_units,
    budget_units,
    extras_dir,
    first_sentences,
    flush_usage,
    is_mock,
    models_for,
    rank_units,
    tokens,
    translate_strings,
    write_mode,
)

log = logging.getLogger("podcast.extras.learn")

N_QUIZ = 8

LEARN_SYSTEM = TASK_TAG + """learn
You are the study-guide editor of an educational podcast. Using ONLY the chapter you are given, you write a short study pack that helps a student revise after listening: key takeaways, a glossary and a multiple-choice quiz.

Reply with ONE JSON object and nothing else (no markdown fences, no commentary):
{{
  "takeaways": [ {{"text": string, "unit_id": int}} ],                  // 5-8 items
  "glossary":  [ {{"term": string, "definition": string, "unit_id": int}} ],   // 6-12 items
  "quiz": [ {{"question": string, "options": [string, string, string, string], "answer": int,
              "explanation": string, "unit_id": int, "level": "easy" | "medium" | "hard"}} ]   // exactly {n_quiz} items
}}

Rules
1. FAITHFULNESS (most important). Every fact, number, name and definition comes from the chapter units. unit_id is the id of the unit that supports the item. Never add outside knowledge, even if it is true.
2. Takeaways: one complete, self-contained sentence each (at most 28 words), the most important ideas, in the chapter's order.
3. Glossary: terms the chapter itself introduces or defines. definition = the chapter's meaning in simple words (at most 30 words). No term twice.
4. Quiz: test understanding, not trivia (why, how, which, what happens when). Mix levels (about 40% easy, 40% medium, 20% hard). Exactly four options and exactly ONE correct one (answer = its 0-based index). The three wrong options must be plausible, clearly wrong according to the chapter, and similar in length and form to the right one. Never use "all of the above", "none of the above" or "both". Do not refer to option letters in the question. Spread the questions over different parts of the chapter. explanation: one or two sentences saying why the answer is right, in the chapter's terms.
5. Numbers that appear in questions, options or explanations must appear in the chapter.
6. LANGUAGE. {language_rules}
{extra}"""

VERIFY_SYSTEM = TASK_TAG + """verify_learn
You are a strict fact-checker for a student study pack. Each item comes with the "evidence": the chapter paragraph(s) it claims to come from. Decide whether each item is supported by the evidence ALONE.

Reply with ONE JSON object and nothing else:
{{"results": [ {{"id": string, "ok": boolean, "reason": string}} ]}}

- kind "takeaway" or "glossary": ok only if the evidence states or clearly implies the claim. A definition that adds details the evidence does not contain is not ok.
- kind "quiz": ok only if (a) the option marked correct is supported by the evidence, AND (b) none of the other options is also supported by the evidence, so there is no second right answer, AND (c) the question can be answered from the evidence.
- reason: at most 20 words, only when ok is false.
Judge meaning, not wording; the items may be in a different language from the evidence only if the pack was translated, otherwise they share one language."""


def _lang_rules(write_lang: str, audience: str) -> str:
    if write_lang == "en":
        return f"Write clear, simple English suitable for {audience}."
    lm = lang_block(write_lang)
    return (f"Write every string in {lm['name']} ({lm['script']} script) in the simple register of a school textbook. Use the "
            f"standard textbook term for technical words; if classrooms normally use the English word, write it transliterated "
            f"into {lm['script']}. Never use Latin letters. Digits 0-9 are fine. Suitable for {audience}.")


# --------------------------------------------------------------------------- validation of what the model returned
def _s(x: Any, n: int) -> str:
    return re.sub(r"\s+", " ", str(x or "")).strip()[:n]


def _uid(x: Any, ids: set[int]) -> Optional[int]:
    try:
        v = int(x)
    except (TypeError, ValueError):
        return None
    return v if v in ids else None


def validate_pack(obj: Any, ids: set[int]) -> dict:
    """Accept what is usable, and raise ValueError (the client shows the model its mistake and retries) when too little is."""
    if not isinstance(obj, dict):
        raise ValueError("expected a JSON object with takeaways, glossary and quiz")
    take, gloss, quiz, seen = [], [], [], set()
    for t in obj.get("takeaways") or []:
        text = _s(t.get("text") if isinstance(t, dict) else t, 320)
        if text:
            take.append({"text": text, "unit_id": _uid(t.get("unit_id") if isinstance(t, dict) else None, ids)})
    for g in obj.get("glossary") or []:
        if not isinstance(g, dict):
            continue
        term, d = _s(g.get("term"), 70), _s(g.get("definition"), 420)
        if term and d and term.lower() not in seen:
            seen.add(term.lower())
            gloss.append({"term": term, "definition": d, "unit_id": _uid(g.get("unit_id"), ids)})
    for q in obj.get("quiz") or []:
        if not isinstance(q, dict):
            continue
        opts = [_s(o, 220) for o in (q.get("options") or [])]
        question = _s(q.get("question"), 420)
        if len(opts) != 4 or not question or any(not o for o in opts) or len({o.lower() for o in opts}) != 4:
            continue
        a = q.get("answer")
        if isinstance(a, str) and a.strip()[:1].upper() in "ABCD" and len(a.strip()) <= 2:
            a = "ABCD".index(a.strip()[:1].upper())
        try:
            a = int(a)
        except (TypeError, ValueError):
            continue
        if not 0 <= a < 4 or re.search(r"all of the above|none of the above|both of the above", " ".join(opts), re.I):
            continue
        level = str(q.get("level") or "medium").lower()
        quiz.append({"question": question, "options": opts, "answer": a, "explanation": _s(q.get("explanation"), 420),
                     "unit_id": _uid(q.get("unit_id"), ids), "level": level if level in {"easy", "medium", "hard"} else "medium"})
    if len(take) < 3 or len(quiz) < 4:
        raise ValueError(f"need at least 3 takeaways and 4 valid quiz questions (each with exactly 4 different options and a 0-3 answer); got {len(take)} and {len(quiz)}")
    return {"takeaways": take[:10], "glossary": gloss[:14], "quiz": quiz[:12]}


def _shuffle_options(quiz: list[dict], seed: str) -> None:
    """Models like to put the right answer first or second; spread it evenly and reproducibly."""
    for i, q in enumerate(quiz):
        rng = random.Random(f"{seed}:{i}")
        right = q["options"][q["answer"]]
        opts = list(q["options"])
        rng.shuffle(opts)
        q["options"], q["answer"] = opts, opts.index(right)


def _item_text(kind: str, it: dict) -> str:
    if kind == "quiz":
        return " ".join([it["question"], *it["options"], it.get("explanation", "")])
    if kind == "glossary":
        return it["term"] + " " + it["definition"]
    return it["text"]


def ground_filter(pack: dict, source_text: str) -> tuple[dict, list[dict]]:
    """Rule check: a figure that is not in the chapter removes the item."""
    dropped: list[dict] = []
    out: dict[str, list] = {"takeaways": [], "glossary": [], "quiz": []}
    for kind, key in (("takeaway", "takeaways"), ("glossary", "glossary"), ("quiz", "quiz")):
        for it in pack[key]:
            bad = unsupported_numbers(_item_text("quiz" if kind == "quiz" else kind, it), source_text)
            if bad:
                dropped.append({"kind": kind, "why": f"number(s) {', '.join(bad)} not in the chapter", "text": _item_text(kind, it)[:90]})
            else:
                out[key].append(it)
    return out, dropped


# --------------------------------------------------------------------------- verifier (real models only)
def verify_pack(llm, pack: dict, units: list[SourceUnit], model: Optional[str]) -> tuple[dict, list[dict], bool]:
    """Have a second model check every takeaway, glossary entry and quiz item against the chapter text around its cited
    paragraph. Items the chapter does not support are dropped. Returns `(kept pack, dropped items, whether the check
    ran)`; if the check itself fails, the pack is kept unchecked and flagged.
    """
    by_id = {u.id: u for u in units}
    ordered = [u.id for u in units]

    def evidence(it: dict, claim: str) -> str:
        """The chapter text to judge an item by: the cited paragraph and its neighbours, or the two best-matching
        paragraphs when no paragraph is cited.
        """
        uid = it.get("unit_id")
        if uid in by_id:
            i = ordered.index(uid)
            near = [by_id[j] for j in ordered[max(0, i - 1): i + 2] if by_id[j].kind != "heading"]
            return "\n".join(u.text for u in near)
        top = rank_units(claim, units, k=2)
        return "\n".join(u.text for _, u in top)

    items = []
    for kind, key in (("takeaway", "takeaways"), ("glossary", "glossary"), ("quiz", "quiz")):
        for i, it in enumerate(pack[key]):
            iid = f"{kind[0]}{i}"
            if kind == "quiz":
                payload = {"id": iid, "kind": kind, "question": it["question"], "marked_correct": it["options"][it["answer"]],
                           "other_options": [o for j, o in enumerate(it["options"]) if j != it["answer"]], "evidence": evidence(it, it["question"])}
            elif kind == "glossary":
                payload = {"id": iid, "kind": kind, "term": it["term"], "definition": it["definition"], "evidence": evidence(it, it["term"] + " " + it["definition"])}
            else:
                payload = {"id": iid, "kind": kind, "claim": it["text"], "evidence": evidence(it, it["text"])}
            items.append(payload)

    def parse(o: Any) -> dict:
        if not isinstance(o, dict) or not isinstance(o.get("results"), list):
            raise ValueError("expected {\"results\": [...]}")
        return {str(r.get("id")): (bool(r.get("ok")), _s(r.get("reason"), 160)) for r in o["results"] if isinstance(r, dict)}

    try:
        verdicts = llm.json(task="verify_learn", system=VERIFY_SYSTEM, user=payload_block({"items": items}), model=model,
                            temperature=0.0, max_tokens=4000, validate=parse)
    except Exception as e:  # noqa: BLE001 - a failed verifier must not lose the whole pack; it is flagged instead
        log.warning("study-pack verification skipped: %s", e)
        return pack, [], False
    dropped: list[dict] = []
    out: dict[str, list] = {"takeaways": [], "glossary": [], "quiz": []}
    for kind, key in (("takeaway", "takeaways"), ("glossary", "glossary"), ("quiz", "quiz")):
        for i, it in enumerate(pack[key]):
            ok, why = verdicts.get(f"{kind[0]}{i}", (True, ""))
            if ok:
                out[key].append(it)
            else:
                dropped.append({"kind": kind, "why": why or "not supported by the chapter", "text": _item_text(kind, it)[:90]})
    return out, dropped, True


# --------------------------------------------------------------------------- offline generator (demo mode, tests)
_DEF_RE = re.compile(r"^(?P<t>[A-Za-z][\w'\-]*(?:\s+[A-Za-z][\w'\-]*){0,2})\s+(?:is|are|means|refers to|is called|are called|is known as|are known as)\s+(?P<d>.{12,})$")
_CALLED_RE = re.compile(r"\b(?:called|known as|termed)\s+(?P<t>[A-Za-z][\w'\-]*(?:\s+[A-Za-z][\w'\-]*){0,2}?)(?=[,.;:)]|\s+(?:and|or|which|that|when|because)\b|$)")
_NOT_TERMS = {"this", "that", "these", "those", "it", "they", "there", "we", "you", "he", "she", "what", "which", "who", "here", "one", "some",
              "all", "each", "because", "when", "if", "only", "so", "but", "and", "also", "then", "answer", "most", "many", "about", "nearly",
              "while", "since", "although", "however", "for", "in", "on", "at", "as", "how", "why", "where", "let", "look", "place", "mark"}


def _clean_term(t: str) -> str:
    t = re.sub(r"^(?:the|a|an)\s+", "", t.strip(), flags=re.I)
    return t.strip()


def offline_pack(p: Project, units: list[SourceUnit]) -> dict:
    """Demo-mode study pack built from the episode's key points and the chapter text with simple rules (no model)."""
    body = body_units(units) or units
    take: list[dict] = []
    pool: list[tuple[str, Optional[int]]] = []
    for s in p.segments:
        if s.kind in {"body", "recap"}:
            for kp in s.key_points:
                if 8 <= len(kp.split()) <= 32:
                    pool.append((kp.strip(), s.unit_ids[0] if s.unit_ids else None))
    step = max(1, len(pool) // 7) if pool else 1
    for kp, uid in pool[::step][:7]:
        take.append({"text": kp, "unit_id": uid})
    if len(take) < 3:
        for u in body[:5]:
            take.append({"text": first_sentences(u.text, 1, 200), "unit_id": u.id})

    gloss: list[dict] = []
    seen: set[str] = set()
    for u in body:
        for sent in split_sentences(u.text):
            if not 6 <= len(sent.split()) <= 45:
                continue
            c = _CALLED_RE.search(sent)
            term = _clean_term(c.group("t")) if c else None
            if not term:
                m = _DEF_RE.match(sent)
                if m:
                    cand = _clean_term(m.group("t"))
                    words = cand.lower().split()
                    if cand and len(words) <= 2 and not any(w in _NOT_TERMS for w in words):
                        term = cand
            if term and term.lower() not in seen and 3 <= len(term) <= 40 and not any(w in _NOT_TERMS for w in term.lower().split()[:1]):
                seen.add(term.lower())
                gloss.append({"term": term[:1].upper() + term[1:], "definition": sent, "unit_id": u.id})
    if len(gloss) < 4:  # chapters in other scripts: fall back to the chapter's most repeated long words
        freq: dict[str, int] = {}
        for u in body:
            for w in tokens(u.text):
                if len(w) >= 5:
                    freq[w] = freq.get(w, 0) + 1
        for w, c in sorted(freq.items(), key=lambda x: (-x[1], x[0])):
            if c < 2 or len(gloss) >= 8:
                break
            for u in body:
                hit = next((s for s in split_sentences(u.text) if w in s.lower() and len(s.split()) >= 6), None)
                if hit and w not in seen:
                    seen.add(w)
                    gloss.append({"term": w, "definition": hit, "unit_id": u.id})
                    break
    step = max(1, len(gloss) // 9)
    gloss = gloss[::step][:10]

    vocab: dict[str, str] = {}
    freq2: dict[str, int] = {}
    for u in body:
        for raw in re.findall(r"[^\W\d_]{6,}", u.text, flags=re.UNICODE):
            vocab.setdefault(raw.lower(), raw)
            freq2[raw.lower()] = freq2.get(raw.lower(), 0) + 1
    cands: list[tuple[int, str, str]] = []
    for u in body:
        for sent in split_sentences(u.text):
            if 8 <= len(sent.split()) <= 34:
                toks = [w for w in re.findall(r"[^\W\d_]{6,}", sent, flags=re.UNICODE) if w.lower() in vocab]
                toks = [w for w in toks if sent.find(w) > 0]
                if toks:
                    cands.append((u.id, sent, max(toks, key=lambda w: (len(w), w))))
    quiz: list[dict] = []
    if cands:
        pick = [cands[int(i * len(cands) / N_QUIZ)] for i in range(min(N_QUIZ, len(cands)))] if len(cands) > N_QUIZ else cands
        for n, (uid, sent, ans) in enumerate(pick):
            rng = random.Random(f"{uid}:{n}")
            pool = [vocab[w] for w in vocab if w not in sent.lower() and abs(len(w) - len(ans)) <= 3 and w != ans.lower()]
            pool = sorted(set(pool))
            if len(pool) < 3:
                continue
            wrong = rng.sample(pool, 3)
            opts = [ans, *wrong]
            quiz.append({"question": "Which word completes this sentence from the chapter?  “" + sent.replace(ans, "_____", 1) + "”",
                         "options": opts, "answer": 0, "explanation": f"The chapter says: “{sent}”", "unit_id": uid,
                         "level": ("easy", "medium", "hard")[n % 3]})
    return {"takeaways": take, "glossary": gloss, "quiz": quiz}


# --------------------------------------------------------------------------- build + storage
def _strings(pack: dict) -> list[tuple[str, int, str, Optional[int]]]:
    """(group, item index, field, option index) for every translatable string."""
    out = []
    for i, t in enumerate(pack["takeaways"]):
        out.append(("takeaways", i, "text", None))
    for i, g in enumerate(pack["glossary"]):
        out += [("glossary", i, "term", None), ("glossary", i, "definition", None)]
    for i, q in enumerate(pack["quiz"]):
        out += [("quiz", i, "question", None), ("quiz", i, "explanation", None)] + [("quiz", i, "options", k) for k in range(4)]
    return out


def _get(pack, ref):
    g, i, f, k = ref
    v = pack[g][i][f]
    return v[k] if k is not None else v


def _set(pack, ref, val):
    g, i, f, k = ref
    if k is None:
        pack[g][i][f] = val
    else:
        pack[g][i][f][k] = val


def _decorate(pack: dict, units: list[SourceUnit]) -> None:
    by_id = {u.id: u for u in units}
    for key in ("takeaways", "glossary", "quiz"):
        for it in pack[key]:
            u = by_id.get(it.get("unit_id"))
            it["source"] = {"unit_id": u.id, "section": u.section, "excerpt": first_sentences(u.text, 2, 220)} if u else None


def build_pack(p: Project, units: list[SourceUnit], llm, bodhan, settings, say: Callable[[str], None] = lambda m: None) -> dict:
    """Create the study pack (takeaways, glossary, quiz): the model writes it from the numbered chapter paragraphs, any item
    containing a number that is not in the chapter is dropped, a second model verifies the rest, and the pack is translated
    when the episode is. `say` receives progress messages.
    """
    translate, wl = write_mode(p)
    wmodel, cmodel = models_for(p, settings)
    ids = {u.id for u in units}
    source_text = " ".join(u.text for u in units)
    mock = is_mock(llm)
    if mock:
        raw = offline_pack(p, units)
        if len(raw["quiz"]) < 1 or len(raw["takeaways"]) < 1:
            raise ValueError("The chapter is too short to build a quiz from.")
    else:
        say("Writing takeaways, glossary and quiz...")
        system = LEARN_SYSTEM.format(n_quiz=N_QUIZ, language_rules=_lang_rules(wl, p.settings.audience), extra=p.settings.extra_instructions[:600])
        user = payload_block({"episode_title": p.plan.episode_title or p.title, "language": lang_block(wl), "audience": p.settings.audience,
                              "planned_key_points": [{"segment": s.title, "points": s.key_points} for s in p.segments if s.kind in {"body", "recap"}],
                              "units": budget_units(units)})
        raw = llm.json(task="learn", system=system, user=user, model=wmodel, temperature=0.4, max_tokens=7000, validate=lambda o: validate_pack(o, ids))
    generated = {k: len(raw[k]) for k in ("takeaways", "glossary", "quiz")}
    pack, dropped = ground_filter(raw, source_text)
    verified = False
    if not mock:
        say("Checking every item against the chapter...")
        pack, dropped2, verified = verify_pack(llm, pack, units, cmodel)
        dropped += dropped2
    if len(pack["quiz"]) < 3:
        raise ValueError("Too few quiz questions passed the chapter check. Try again, or use a model with better instruction following.")
    _shuffle_options(pack["quiz"], p.id)
    _decorate(pack, units)
    master = json.loads(json.dumps(pack)) if translate else None
    if translate:
        say(f"Translating into {LANGS[p.settings.language]['name']}...")
        refs = _strings(pack)
        for ref, tr in zip(refs, translate_strings(bodhan, [_get(pack, r) for r in refs], p.settings.language, settings.mt_concurrency)):
            _set(pack, ref, tr)
    kept = {k: len(pack[k]) for k in ("takeaways", "glossary", "quiz")}
    return {"status": "ready", "message": "", "language": p.settings.language, "translated": translate, "generated_at": now_iso(),
            "takeaways": pack["takeaways"], "glossary": pack["glossary"], "quiz": pack["quiz"],
            "master_en": master,
            "meta": {"generated": generated, "kept": kept, "dropped": dropped, "verified_by_model": verified, "offline": mock,
                     "writer_model": None if mock else wmodel, "checker_model": None if mock else cmodel}}


def _path(store: ProjectStore, pid: str):
    return extras_dir(store, pid) / "learn.json"


def load_pack(store: ProjectStore, pid: str) -> Optional[dict]:
    """The saved study pack of an episode, or None if there is none or the file is unreadable."""
    f = _path(store, pid)
    if not f.exists():
        return None
    try:
        return json.loads(read_text(f))
    except (ValueError, OSError):
        return None


def _save(store: ProjectStore, pid: str, data: dict) -> None:
    atomic_write_text(_path(store, pid), json.dumps(data, ensure_ascii=False, indent=1))


_running: set[str] = set()
_guard = threading.Lock()


def status(store: ProjectStore, pid: str) -> dict:
    """Where the study pack stands: none, working, ready or error. A job left over from a server restart is reported as
    an error.
    """
    d = load_pack(store, pid)
    if d is None:
        return {"status": "none"}
    if d.get("status") == "working":
        with _guard:
            alive = pid in _running
        if not alive:  # the server restarted while it was running
            return {"status": "error", "message": "It was interrupted. Press Create again."}
        return {"status": "working", "message": d.get("message", "")}
    return d


def start(store: ProjectStore, pid: str, settings, clients_factory) -> dict:
    """Begin building the study pack in a background thread (one at a time per episode) and return its status at once."""
    p = store.load(pid)
    if not p.segments or not store.has_units(pid):
        raise ValueError("Finish generating the episode first; the study pack is built from its chapter.")
    with _guard:
        if pid in _running:
            return {"status": "working", "message": "Already working on it..."}
        _running.add(pid)

    def say(msg: str) -> None:
        _save(store, pid, {"status": "working", "message": msg, "started_at": now_iso()})

    def work() -> None:
        """Background body of `start`: build the pack, save it, and store the error text instead of raising."""
        llm, bodhan, meter = clients_factory(settings)
        try:
            say("Reading the chapter...")
            units = store.load_units(pid)
            data = build_pack(store.load(pid), units, llm, bodhan, settings, say)
            _save(store, pid, data)
        except Exception as e:  # noqa: BLE001 - shown to the student, never a stack trace
            log.warning("study pack failed: %s", e)
            _save(store, pid, {"status": "error", "message": str(e)[:400] or e.__class__.__name__})
        finally:
            try:
                flush_usage(store, pid, meter)
            except Exception:  # noqa: BLE001 - e.g. the project was deleted meanwhile
                pass
            with _guard:
                _running.discard(pid)

    say("Starting...")
    threading.Thread(target=work, name=f"learn-{pid}", daemon=True).start()
    return {"status": "working", "message": "Starting..."}
