"""Several API keys per service, with automatic failover.

A setting like  BODHAN_TTS_API_KEY=key1,key2  holds a list. The first usable key is used; if the service says a key is
rejected (401/403) or out of credit (402) it is skipped for a while and the next key takes over, so a run is not lost
because one of your created keys ran dry. State is process-wide, so later jobs do not retry a known-bad key.
"""
from __future__ import annotations

import re
import threading
import time

_lock = threading.Lock()
_bad: dict[str, float] = {}  # key -> monotonic time until which it is skipped

OUT_OF_CREDIT_S = 600
REJECTED_S = 3600


def split_keys(value: str | None) -> list[str]:
    """'a, b;c' -> ['a','b','c'] (commas, semicolons or whitespace; quotes stripped; duplicates removed, order kept)."""
    out: list[str] = []
    for part in re.split(r"[,;\s]+", value or ""):
        part = part.strip().strip('"').strip("'")
        if part and part not in out:
            out.append(part)
    return out


def mask(key: str) -> str:
    return "••••" + key[-4:] if len(key) >= 8 else "••••"


def pick(keys: list[str]) -> str | None:
    """First key that is not currently marked bad (if all are bad, None)."""
    now = time.monotonic()
    with _lock:
        for k in keys:
            if _bad.get(k, 0) <= now:
                return k
    return None


def mark_bad(key: str, seconds: float) -> None:
    with _lock:
        _bad[key] = time.monotonic() + seconds


def reset() -> None:
    with _lock:
        _bad.clear()
