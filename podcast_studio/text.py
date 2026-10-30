"""Text utilities: cleaning script text for the voice engine, sentence splitting, TTS chunking,
number checks used for faithfulness, and script detection."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

_TERMINATORS = ".!?।॥؟۔"
_CLOSERS = "\"”’')]»"

# A "sentence" ends at a terminator that is followed (after optional closing quotes/brackets) by whitespace or end.
# Terminators inside numbers ("3.5") or abbreviations without a following space are left alone.
_SENT_RE = re.compile(
    r"(?:[^" + re.escape(_TERMINATORS) + r"]|[" + re.escape(_TERMINATORS) + r"](?![" + re.escape(_CLOSERS) + r"]*(?:\s|$)))+"
    r"[" + re.escape(_TERMINATORS) + r"]*[" + re.escape(_CLOSERS) + r"]*",
    re.S,
)

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0000FE0F\U0000200D]+",
    flags=re.UNICODE,
)

_ABBREV_EN = [
    (re.compile(r"\be\.g\.,?", re.I), "for example,"),
    (re.compile(r"\bi\.e\.,?", re.I), "that is,"),
    (re.compile(r"\betc\.", re.I), "and so on."),
    (re.compile(r"\bvs\.?\b", re.I), "versus"),
    (re.compile(r"\bapprox\.", re.I), "approximately"),
    (re.compile(r"\bDr\.\s"), "Doctor "),
    (re.compile(r"\bProf\.\s"), "Professor "),
    (re.compile(r"\bMr\.\s"), "Mister "),
    (re.compile(r"\bMrs\.\s"), "Missus "),
]


def word_count(text: str) -> int:
    return len(text.split())


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    return [m.group(0).strip() for m in _SENT_RE.finditer(text) if m.group(0).strip()]


def ends_with_terminator(text: str) -> bool:
    t = text.rstrip(_CLOSERS + " ")
    return bool(t) and t[-1] in _TERMINATORS + "…"


def clean_spoken(text: str, lang: str = "en", add_terminator: bool = True, labels: tuple[str, ...] = ()) -> str:
    """Make LLM output safe for a TTS engine: no markdown, stage directions, emoji or speaker labels."""
    t = text or ""
    t = unicodedata.normalize("NFC", t)
    names = "|".join(re.escape(x) for x in labels if x)
    label_re = r"(?:host\s*)?[AB]" + (f"|{names}" if names else "")
    t = re.sub(r"^\s*(?:\*\*)?(?:" + label_re + r")(?:\*\*)?\s*[:：]\s*(?:\*\*)?", "", t, flags=re.I)  # "A: ..." labels
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"[\[\{][^\]\}]{0,80}[\]\}]", " ", t)  # [laughs] {pause}
    t = re.sub(r"\((?:laughs?|laughing|chuckles?|pauses?|smiles?|sighs?|excited|whispers?)[^)]*\)", " ", t, flags=re.I)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[*_`#~^|<>]+", " ", t)
    t = re.sub(r"^\s*[-•▪●]\s+", "", t, flags=re.M)
    t = _EMOJI_RE.sub(" ", t)
    if lang == "en":
        for pat, rep in _ABBREV_EN:
            t = pat.sub(rep, t)
    t = t.replace("—", ", ").replace("–", ", ").replace(" - ", ", ")
    t = t.replace("…", "...")
    t = re.sub(r"\.{4,}", "...", t)
    t = re.sub(r"\s*\(\s*", ", ", t)
    t = re.sub(r"\s*\)\s*", ", ", t)
    t = re.sub(r"\s*&\s*", " and " if lang == "en" else " ", t)
    t = re.sub(r"([!?])\1{1,}", r"\1", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([,.;:!?।॥])", r"\1", t)
    t = re.sub(r",\s*,+", ",", t)
    t = re.sub(r",\s*([.!?।])", r"\1", t)
    t = t.strip(" ,;:-")
    if add_terminator and t and not ends_with_terminator(t):
        t += _default_terminator(t)
    return t


def _default_terminator(t: str) -> str:
    script = dominant_script(t)
    return "।" if script in {"Devanagari", "Bengali", "Odia", "Gurmukhi"} else "."


def _split_clauses(sentence: str) -> list[str]:
    parts = re.split(r"(?<=[,;:])\s+", sentence)
    return [p for p in parts if p.strip()]


def _split_by_words(sentence: str, max_words: int) -> list[str]:
    words = sentence.split()
    if len(words) <= max_words:
        return [sentence]
    n = -(-len(words) // max_words)  # ceil
    size = -(-len(words) // n)
    return [" ".join(words[i : i + size]) for i in range(0, len(words), size)]


def chunk_for_tts(text: str, max_words: int = 40, max_chars: int = 260, max_sentences: int = 2) -> list[str]:
    """Split a spoken turn into pieces the TTS API handles well (the API says ~30 s / a sentence or two)."""
    sentences = split_sentences(text)
    pieces: list[str] = []
    for s in sentences:
        if word_count(s) <= max_words and len(s) <= max_chars:
            pieces.append(s)
            continue
        clauses = _split_clauses(s)
        buf = ""
        for c in clauses:
            if word_count(c) > max_words or len(c) > max_chars:
                if buf:
                    pieces.append(buf)
                    buf = ""
                pieces.extend(_split_by_words(c, min(max_words, max(8, max_chars // 7))))
                continue
            cand = (buf + " " + c).strip()
            if buf and (word_count(cand) > max_words or len(cand) > max_chars):
                pieces.append(buf)
                buf = c
            else:
                buf = cand
        if buf:
            pieces.append(buf)
    # Greedily pack up to `max_sentences` short pieces together so prosody carries across sentences.
    chunks: list[str] = []
    cur: list[str] = []
    for p in pieces:
        cand = " ".join(cur + [p])
        if cur and (len(cur) >= max_sentences or word_count(cand) > max_words or len(cand) > max_chars):
            chunks.append(" ".join(cur))
            cur = [p]
        else:
            cur.append(p)
    if cur:
        chunks.append(" ".join(cur))
    return chunks


# --------------------------------------------------------------------------- numbers (faithfulness)


def digits_to_ascii(s: str) -> str:
    """Replace digits of any script (Devanagari, Tamil, Arabic-Indic...) with ASCII digits so numbers can be compared."""
    out = []
    for ch in s:
        if unicodedata.category(ch) == "Nd":
            out.append(str(unicodedata.digit(ch)))
        else:
            out.append(ch)
    return "".join(out)


_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def extract_numbers(text: str) -> set[str]:
    """All numbers in `text` as ASCII strings without thousands separators. A decimal such as 2.50 is kept as both '2.50' and '2.5'."""
    t = digits_to_ascii(text or "")
    nums = set()
    for m in _NUM_RE.finditer(t):
        n = m.group(0).replace(",", "")
        n = n.rstrip(".")
        nums.add(n)
        if "." in n:
            nums.add(n.rstrip("0").rstrip("."))
    return nums


def unsupported_numbers(spoken: str, source: str, ignore_up_to: int = 20) -> list[str]:
    """Numbers said in the script that never occur in the source. Small whole numbers are ignored
    (hosts legitimately say 'three types' after counting a list)."""
    src = extract_numbers(source)
    bad = []
    for n in sorted(extract_numbers(spoken)):
        if n in src:
            continue
        try:
            if "." not in n and int(n) <= ignore_up_to:
                continue
        except ValueError:
            pass
        bad.append(n)
    return bad


# --------------------------------------------------------------------------- scripts


_SCRIPT_RANGES = [
    ("Devanagari", 0x0900, 0x097F), ("Bengali", 0x0980, 0x09FF), ("Gurmukhi", 0x0A00, 0x0A7F),
    ("Gujarati", 0x0A80, 0x0AFF), ("Odia", 0x0B00, 0x0B7F), ("Tamil", 0x0B80, 0x0BFF),
    ("Telugu", 0x0C00, 0x0C7F), ("Kannada", 0x0C80, 0x0CFF), ("Malayalam", 0x0D00, 0x0D7F),
    ("Perso-Arabic", 0x0600, 0x06FF), ("Perso-Arabic", 0x0750, 0x077F), ("Perso-Arabic", 0xFB50, 0xFDFF),
    ("Perso-Arabic", 0xFE70, 0xFEFF), ("Ol Chiki", 0x1C50, 0x1C7F), ("Meetei Mayek", 0xABC0, 0xABFF),
    ("Latin", 0x0041, 0x024F),
]

_SCRIPT_TO_LANG = {
    "Devanagari": "hi", "Bengali": "bn", "Gurmukhi": "pa", "Gujarati": "gu", "Odia": "or", "Tamil": "ta",
    "Telugu": "te", "Kannada": "kn", "Malayalam": "ml", "Perso-Arabic": "ur", "Latin": "en",
    "Ol Chiki": "sat", "Meetei Mayek": "mni",
}


def dominant_script(text: str) -> str:
    """Name of the writing system most letters in `text` belong to (for example Devanagari or Latin)."""
    counts: Counter[str] = Counter()
    for ch in text:
        if not ch.isalpha():
            continue
        cp = ord(ch)
        for name, lo, hi in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                counts[name] += 1
                break
    return counts.most_common(1)[0][0] if counts else "Latin"


def guess_language(text: str) -> tuple[str, str]:
    """(language code guess, script). Only a hint for prompts - Devanagari could be Hindi/Marathi/etc."""
    script = dominant_script(text[:20000])
    return _SCRIPT_TO_LANG.get(script, "en"), script


def contains_latin_letters(text: str) -> bool:
    return any(("a" <= c <= "z") or ("A" <= c <= "Z") for c in text)
