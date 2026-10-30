"""Tolerant JSON extraction for LLM output (code fences, chatter around the object, trailing commas,
raw newlines inside strings)."""
from __future__ import annotations

import json
import re
from typing import Any


def _escape_control_chars_in_strings(s: str) -> str:
    out = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
                out.append(ch)
            elif ch == "\\":
                esc = True
                out.append(ch)
            elif ch == '"':
                in_str = False
                out.append(ch)
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    return "".join(out)


def _find_balanced(s: str) -> str | None:
    start = None
    for i, ch in enumerate(s):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None
    open_ch = s[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def extract_json(text: str) -> Any:
    """Pull one JSON value out of a model reply. It accepts a bare document, a fenced code block, or JSON embedded in
    other text, and tolerates trailing commas and raw control characters inside strings. Raises ValueError when
    nothing parses.
    """
    if text is None:
        raise ValueError("empty response")
    t = text.strip()
    if not t:
        raise ValueError("empty response")
    candidates = []
    fence = re.search(r"```(?:json|JSON)?\s*(.*?)```", t, re.S)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(t)
    bal = _find_balanced(t)
    if bal:
        candidates.append(bal)
    last: Exception | None = None
    for c in candidates:
        for variant in (c, _escape_control_chars_in_strings(c)):
            for fix in (variant, re.sub(r",\s*([}\]])", r"\1", variant)):
                try:
                    return json.loads(fix)
                except json.JSONDecodeError as e:
                    last = e
        b = _find_balanced(c)
        if b and b != c:
            for fix in (b, re.sub(r",\s*([}\]])", r"\1", _escape_control_chars_in_strings(b))):
                try:
                    return json.loads(fix)
                except json.JSONDecodeError as e:
                    last = e
    raise ValueError(f"not valid JSON ({last})")
