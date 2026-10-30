"""Shared LLM client behaviour: structured-JSON calls with validation + self-repair retries."""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from .jsonx import extract_json

log = logging.getLogger("podcast.llm")


class LLMError(RuntimeError):
    """A language-model call failed. `status` is the HTTP status if known; `fatal` means a bad key or no credit (retrying
    or switching model will not help); `model_problem` means this model cannot serve the request, so a fallback model
    should be tried.
    """
    def __init__(self, message: str, *, status: int | None = None, fatal: bool = False, model_problem: bool = False):
        super().__init__(message)
        self.status = status
        self.fatal = fatal  # bad key / no credits - retrying or switching model will not help
        self.model_problem = model_problem  # this model cannot serve the request; try a fallback model


class LLMBase:
    """Subclasses implement chat_full(); everything else is shared (and exercised by the mock too)."""

    def chat_full(self, messages: list[dict], *, model: Optional[str] = None, temperature: float = 0.7,
                  max_tokens: int = 4096, json_mode: bool = False, task: str = "") -> tuple[str, str]:
        raise NotImplementedError

    def chat(self, messages: list[dict], **kw) -> str:
        return self.chat_full(messages, **kw)[0]

    def json(self, *, task: str, system: str, user: str, model: Optional[str] = None, temperature: float = 0.5,
             max_tokens: int = 4096, validate: Optional[Callable[[Any], Any]] = None, retries: int = 2) -> Any:
        """Ask for a JSON object. On bad JSON / failed validation, show the model its mistake and retry."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last: Exception | None = None
        for attempt in range(retries + 1):
            text, finish = self.chat_full(messages, model=model, temperature=temperature, max_tokens=max_tokens,
                                          json_mode=True, task=task)
            try:
                obj = extract_json(text)
                return validate(obj) if validate else obj
            except (ValueError, json.JSONDecodeError, KeyError, TypeError) as e:
                last = e
                log.warning("task=%s attempt=%d rejected: %s", task, attempt + 1, e)
                if finish == "length":
                    max_tokens = int(max_tokens * 1.7)
                messages = messages + [
                    {"role": "assistant", "content": (text or "")[:8000]},
                    {"role": "user", "content": (
                        f"That response was rejected: {e}. Reply again with ONLY the corrected JSON object "
                        "- no commentary, no code fences.")},
                ]
        raise LLMError(f"The model did not return usable JSON for '{task}' after {retries + 1} tries: {last}")

    def vision_ocr(self, png_bytes: bytes, *, prompt: str, model: Optional[str] = None) -> str:
        raise NotImplementedError
