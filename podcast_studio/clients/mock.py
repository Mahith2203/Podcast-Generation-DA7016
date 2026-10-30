"""Offline stand-ins for OpenRouter and Bodhan.

Used for (a) demo mode (MOCK_APIS=1) so the whole app can be tried without keys or credits, and (b) the test
suite, where the same brain sits behind a fake HTTP server so the *real* client code is exercised.

The mock "writer" assembles dialogue from the source text itself, so the output is faithful by construction.
Test hooks (put these words in the project's extra instructions):
  MOCK_INJECT_ERROR   first draft of body segments contains an unsupported claim only the LLM-check can catch
  MOCK_INJECT_NUMBER  first draft contains a number that is not in the source (caught by the rule check)
  MOCK_DROP_POINT     first draft forgets its last key point
  MOCK_ALWAYS_BAD     every draft contains the unsupported claim (exercises the 'needs review' path)
"""
from __future__ import annotations

import json
import re
from typing import Optional

from ..audio import array_to_wav_bytes, synth_babble
from ..config import Settings
from ..text import split_sentences, unsupported_numbers
from ..voices import LANGS, STYLES, VOICE_NAMES
from .base import LLMBase
from .bodhan import BodhanError
from .usage import UsageMeter

BAD_CLAIM = "Scientists proved this was caused by moon dust."
BAD_NUMBER = "Researchers measured this in 1987 and again in 2043."


def _inp(messages: list[dict]) -> dict:
    for m in messages:
        if m["role"] == "user" and "INPUT_JSON:" in str(m["content"]):
            body = str(m["content"]).split("INPUT_JSON:", 1)[1]
            mt = re.search(r"```json\s*(.*?)```", body, re.S)
            return json.loads(mt.group(1) if mt else body)
    return {}


def _task(messages: list[dict]) -> str:
    for m in messages:
        if m["role"] == "system":
            mt = re.search(r"### TASK:\s*(\w+)", str(m["content"]))
            if mt:
                return mt.group(1)
    return ""


def _first_sentence(text: str, limit: int = 220) -> str:
    s = split_sentences(text)
    out = s[0] if s else text
    return out[:limit].rstrip()


class MockBrain:
    """Deterministic task router: plan / write / check / polish."""

    def respond(self, messages: list[dict]) -> str:
        """Answer a chat request offline: pick the task from the prompt and return its canned-but-structured JSON."""
        task = _task(messages)
        inp = _inp(messages)
        fn = getattr(self, f"_{task}", None)
        if fn is None:
            return json.dumps({"ok": True})
        return json.dumps(fn(inp), ensure_ascii=False)

    # ---------------------------------------------------------------- plan
    def _plan(self, inp: dict) -> dict:
        units = inp["units"]
        subst = [u for u in units if u["kind"] != "heading"] or units
        n_body = max(1, int(inp.get("approx_body_segments") or 3))
        part = inp.get("part", {"index": 1, "count": 1})
        first, last = part["index"] == 1, part["index"] == part["count"]
        title = inp.get("chapter_title_hint") or "The Chapter"
        groups: list[list[dict]] = [[] for _ in range(min(n_body, len(subst)))]
        for i, u in enumerate(subst):
            groups[min(len(groups) - 1, i * len(groups) // len(subst))].append(u)
        segs = []
        if first:
            segs.append({"kind": "intro", "title": f"Welcome: {title}"[:60], "goal": "Introduce the topic",
                         "unit_ids": [subst[0]["id"]], "key_points": [f"This episode is about {title}."], "share": 0.08})
        body_share = 1.0 - (0.08 if first else 0) - (0.15 if last else 0)
        for i, g in enumerate(groups):
            sec = g[0].get("section") or f"Part {i + 1}"
            segs.append({"kind": "body", "title": sec[:60], "goal": f"Understand {sec}", "unit_ids": [u["id"] for u in g],
                         "key_points": [_first_sentence(u["text"]) for u in g][:5], "share": body_share / len(groups)})
        if last:
            segs.append({"kind": "recap", "title": "Quick recap", "goal": "Review", "unit_ids": [subst[-1]["id"]],
                         "key_points": [_first_sentence(subst[-1]["text"])], "share": 0.10})
            segs.append({"kind": "outro", "title": "Goodbye", "goal": "Sign off", "unit_ids": [subst[-1]["id"]],
                         "key_points": ["Thank the listeners and say goodbye."], "share": 0.05})
        return {"episode_title": title[:80], "hook": f"Have you ever wondered about {title.lower()}?", "segments": segs}

    # ---------------------------------------------------------------- write
    def _write(self, inp: dict) -> dict:
        seg, hosts, mode = inp["segment"], inp["hosts"], inp["mode"]
        kind, show = seg["kind"], inp["show_name"]
        extra = inp.get("extra_instructions") or ""
        a, b = hosts["A"]["name"], hosts["B"]["name"]
        kps = list(seg["key_points"])
        fresh = mode == "fresh"
        if "MOCK_DROP_POINT" in extra and fresh and len(kps) > 1:
            kps = kps[:-1]
        turns: list[dict] = []

        def T(sp, text, style=None, reaction=False):
            turns.append({"speaker": sp, "text": text, "style": style, "reaction": reaction})

        native = inp["write_language"]["code"] != "en"

        def L(en: str, nat: str) -> str:
            return nat if native else en

        if kind == "intro":
            T("A", L(f"Welcome to {show}! I'm {a}.", "आपका स्वागत है!"), "happy")
            T("B", L(f"And I'm {b}. {inp['episode_hook']}", "आज हम एक नया विषय सीखेंगे।"))
            for kp in kps:
                T("B", kp)
            T("A", L("Let's get started.", "चलिए शुरू करते हैं।"), None, True)
        elif kind == "recap":
            T("A", L("Time for a quick recap.", "अब एक छोटा सा दोहराव।"))
            T("B", L("Here is what to remember. ", "याद रखने की बातें। ") + " ".join(kps))
            T("A", L("Got it.", "समझ गया।"), None, True)
        elif kind == "outro":
            T("A", L(f"That's all for this episode of {show}.", "आज के लिए बस इतना ही।"))
            T("B", L("Thanks for listening, and goodbye!", "सुनने के लिए धन्यवाद, नमस्ते!"), "happy")
        else:
            T("A", L(f"So, let's talk about {seg['title']}. Where do we begin?", f"तो, {seg['title']} के बारे में बात करें। शुरुआत कहाँ से करें?"))
            for i, kp in enumerate(kps):
                T("B", kp, "educational lecture" if i == 0 else None)
                if i % 2 == 0:
                    T("A", L("Hmm, okay.", "अच्छा, ठीक है।"), None, True)
                else:
                    T("A", L(f"Interesting, {b}. Can you say more?", "दिलचस्प है। थोड़ा और बताइए?"))
            # pad towards the word budget with further sentences from the assigned source units (still faithful)
            used = " ".join(t["text"] for t in turns)
            words = len(used.split())
            extra_sents = [s for u in inp["source_units"] for s in split_sentences(u["text"]) if s not in used]
            k = 0
            while words < 0.75 * seg["target_words"] and k < len(extra_sents):
                s = extra_sents[k][:240]
                T("B", s)
                T("A", L("Right, that makes sense.", "हाँ, यह समझ में आता है।"), None, True) if k % 2 == 0 else T("A", L(f"Okay, and then, {b}?", "ठीक है, फिर क्या होता है?"))
                words += len(s.split()) + 4
                k += 1
            if inp.get("next_segment_title"):
                T("B", L(f"Next up: {inp['next_segment_title']}.", f"आगे: {inp['next_segment_title']}।"))
        if kind == "body":
            if "MOCK_ALWAYS_BAD" in extra or ("MOCK_INJECT_ERROR" in extra and fresh):
                turns.insert(2, {"speaker": "B", "text": BAD_CLAIM, "style": None, "reaction": False})
            if "MOCK_INJECT_NUMBER" in extra and fresh:
                turns.insert(2, {"speaker": "B", "text": BAD_NUMBER, "style": None, "reaction": False})
        if mode == "reviewer_request" and inp.get("reviewer_instruction"):
            turns.insert(1, {"speaker": "B", "text": f"Reviewer asked: {inp['reviewer_instruction']}", "style": None, "reaction": False})
        return {"turns": turns}

    # ---------------------------------------------------------------- direct (video storyboard)
    def _direct(self, inp: dict) -> dict:
        seg, lines = inp["segment"], inp["script_lines"]
        n = len(lines)
        instr = inp.get("producer_instruction") or ""
        mode = inp.get("mode")
        native = inp["language"]["code"] != "en"
        bad = "MOCK_ALWAYS_BAD_VISUAL" in instr or ("MOCK_BAD_VISUAL" in instr and mode in ("fresh", "reviewer_request"))

        def short(t: str, w: int = 9) -> str:
            ws = t.split()
            return " ".join(ws[:w]).strip(" ,;:.!?।-") + ("…" if len(ws) > w else "")

        def sent(i: int, w: int = 9) -> str:
            s = split_sentences(lines[i]["text"])
            return short(s[0] if s else lines[i]["text"], w)

        kps = [k for k in seg.get("key_points", []) if k.strip()]
        if native or len(kps) < 2:  # a native-script video must not show English plan text: use the spoken lines
            longest = sorted(range(n), key=lambda i: -len(lines[i]["text"]))[:4]
            items_src = [sent(i) for i in sorted(longest)]
        else:
            items_src = [short(k) for k in kps[:5]]
        title = short(seg.get("title") or inp["episode_title"], 7)
        sc: list[dict] = []

        def add(layout: str, a: int, b: int, **kw):
            sc.append({"layout": layout, "from_turn": a, "to_turn": b, **kw})

        kind = seg.get("kind", "body")
        if kind == "intro":
            e = min(n - 1, 3)
            add("title", 0, e, title=inp["episode_title"][:80], text=inp["show_name"])
            if e < n - 1:
                add("bullets", e + 1, n - 1, title=title, items=items_src[:3] or [title, title])
        elif kind == "recap":
            add("recap", 0, n - 1, title=title, items=items_src[:4] if len(items_src) >= 2 else items_src * 2)
        elif kind == "outro":
            add("title", 0, n - 1, title=inp["episode_title"][:80], text=inp["show_name"])
        else:
            cuts = sorted({0, n // 3, (2 * n) // 3, n})
            spans = [(a, b - 1) for a, b in zip(cuts, cuts[1:]) if b > a]
            for k, (a, b) in enumerate(spans):
                if k == 0:
                    add("definition", a, b, term=title, text=items_src[0] if items_src else title)
                elif k == 1 and inp.get("figures"):
                    f = inp["figures"][0]
                    add("figure", a, b, title=title, figure=f["id"])
                elif k == 1 and len(items_src) >= 3:
                    il = instr.lower()  # a reviewer's wording wins over the segment title
                    layout = "steps" if "step" in il else ("cycle" if ("cycle" in il or "cycle" in title.lower()) else "steps")
                    add(layout, a, b, title=title, items=items_src[:5])
                else:
                    add("bullets", a, b, title=title, items=items_src[:4] if len(items_src) >= 2 else items_src * 2)
        if bad and sc:
            tgt = next((s for s in sc if s["layout"] in ("bullets", "steps", "cycle", "recap")), sc[-1])
            if tgt["layout"] in ("bullets", "steps", "cycle", "recap"):
                tgt["items"] = list(tgt["items"]) + ["Measured 4,812 litres in 1987 by moon dust"]
            else:
                tgt["text"] = "Scientists proved this in 1987 with 4,812 litres of moon dust"
        return {"scenes": sc}

    # ---------------------------------------------------------------- check
    def _check(self, inp: dict) -> dict:
        turns, seg = inp["draft"], inp["segment"]
        src = " ".join(u["text"] for u in inp["source_units"]) + " " + (inp.get("rest_of_chapter") or "")
        issues = []
        for t in turns:
            if "moon dust" in t["text"].lower():
                issues.append({"turn": t["i"], "severity": "major", "type": "unsupported",
                               "detail": "Claim about moon dust is not in the source.", "evidence_unit": None,
                               "suggested_fix": "Remove it."})
            bad = unsupported_numbers(t["text"], src)
            if bad:
                issues.append({"turn": t["i"], "severity": "major", "type": "numeric",
                               "detail": f"Numbers {bad} do not appear in the source.", "evidence_unit": None,
                               "suggested_fix": "Remove them."})
        joined = " ".join(t["text"] for t in turns).lower()
        missing = []
        for kp in seg["key_points"]:
            words = [w for w in re.findall(r"\w+", kp.lower()) if len(w) > 3]
            if words and sum(1 for w in words if w in joined) / len(words) < 0.5:
                missing.append(kp)
        return {"verdict": "pass" if not issues and not missing else "revise", "issues": issues,
                "missing_points": missing, "notes": "mock checker"}

    # ---------------------------------------------------------------- polish
    def _polish(self, inp: dict) -> dict:
        return {"turns": [{"i": t["i"], "text": t.get("translation") or t.get("text", "")} for t in inp["turns"]]}


class MockLLM(LLMBase):
    """Offline stand-in for the language model (demo mode and tests). It never touches the network."""
    def __init__(self, meter: UsageMeter | None = None):
        self.meter = meter or UsageMeter()
        self.brain = MockBrain()

    def chat_full(self, messages, *, model=None, temperature=0.7, max_tokens=4096, json_mode=False, task=""):
        out = self.brain.respond(messages)
        self.meter.llm(sum(len(str(m["content"])) for m in messages) // 4, len(out) // 4, 0.0)
        return out, "stop"

    def vision_ocr(self, png_bytes: bytes, *, prompt: str, model: Optional[str] = None) -> str:
        self.meter.llm(500, 100, 0.0)
        return "Mock OCR page text. This sentence was recognised from a scanned page."


class MockBodhan:
    """Same public surface as BodhanClient, validating inputs the way the real API documents them."""

    def __init__(self, settings: Settings | None = None, meter: UsageMeter | None = None):
        self.meter = meter or UsageMeter()

    def tts(self, text: str, voice: str, lang: str, style: str | None = None) -> bytes:
        """Return a synthetic voice-like WAV (no speech) for `text`, after the same input checks the real service makes."""
        if not text.strip():
            raise BodhanError("input was empty", status=422)
        if voice not in VOICE_NAMES:
            raise BodhanError(f"unknown voice {voice}", status=422)
        if lang not in LANGS:
            raise BodhanError(f"unknown lang {lang}", status=422)
        if style and style not in STYLES:
            style = None
        self.meter.tts(len(text))
        return array_to_wav_bytes(synth_babble(text, voice, style))

    def translate(self, text: str, target_lang: str, script: str = "native") -> str:
        self.meter.mt(len(text))
        return f"«{target_lang}» {text}"
