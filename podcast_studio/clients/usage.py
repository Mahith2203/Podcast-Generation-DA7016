"""Thread-safe counters so the UI can show what a podcast cost (tokens, TTS characters, cache hits)."""
from __future__ import annotations

import threading

from ..models import UsageTotals


class UsageMeter:
    """Thread-safe counters for model calls, tokens, cost, voice characters and cache hits, summed into a `UsageTotals`."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._u = UsageTotals()

    def llm(self, prompt_tokens: int = 0, completion_tokens: int = 0, cost: float = 0.0) -> None:
        with self._lock:
            self._u.llm_calls += 1
            self._u.llm_prompt_tokens += int(prompt_tokens or 0)
            self._u.llm_completion_tokens += int(completion_tokens or 0)
            self._u.llm_cost_usd += float(cost or 0.0)

    def tts(self, chars: int) -> None:
        with self._lock:
            self._u.tts_calls += 1
            self._u.tts_chars += int(chars)

    def tts_cache_hit(self) -> None:
        with self._lock:
            self._u.tts_cache_hits += 1

    def mt(self, chars: int) -> None:
        with self._lock:
            self._u.mt_calls += 1
            self._u.mt_chars += int(chars)

    def snapshot(self) -> UsageTotals:
        with self._lock:
            return self._u.model_copy()
