"""OpenRouter chat-completions client (OpenAI-compatible) with retries, model fallbacks and usage metering."""
from __future__ import annotations

import base64
import logging
import random
import threading
import time
from typing import Optional

import requests

from .. import keys
from ..config import Settings
from ..util import redact
from .base import LLMBase, LLMError
from .usage import UsageMeter

log = logging.getLogger("podcast.openrouter")


class OpenRouterClient(LLMBase):
    """Chat client for OpenRouter: rotates through several API keys, falls back to other models, retries transient
    errors, can read images, and reports usage to the meter.
    """
    BACKOFF = 1.5  # seconds; tests set this to 0

    def __init__(self, settings: Settings, meter: UsageMeter | None = None):
        self.s = settings
        self.meter = meter or UsageMeter()
        self._local = threading.local()
        self._no_json_mode: set[str] = set()

    # ------------------------------------------------------------------ http
    def _session(self) -> requests.Session:
        sess = getattr(self._local, "sess", None)
        if sess is None:
            sess = requests.Session()
            self._local.sess = sess
        return sess

    def _headers(self, key: str | None = None) -> dict:
        return {
            "Authorization": f"Bearer {key or keys.pick(self.s.openrouter_keys) or (self.s.openrouter_keys or [''])[0]}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost",
            "X-Title": "Podcast Studio",
        }

    def _sleep(self, attempt: int, retry_after: float | None = None) -> None:
        delay = retry_after if retry_after is not None else min(30.0, self.BACKOFF * (2 ** attempt))
        delay = delay * (0.85 + 0.3 * random.random()) if delay else 0
        if delay:
            time.sleep(delay)

    def _err_text(self, resp: requests.Response) -> str:
        try:
            d = resp.json()
            if isinstance(d, dict):
                e = d.get("error", d)
                if isinstance(e, dict):
                    msg = e.get("message") or e.get("detail") or str(e)
                    meta = e.get("metadata")
                    if isinstance(meta, dict) and meta.get("raw"):
                        msg += f" ({str(meta['raw'])[:200]})"
                    return redact(str(msg), *self.s.secrets)[:500]
                return redact(str(e), *self.s.secrets)[:500]
        except ValueError:
            pass
        return redact(resp.text or "", *self.s.secrets)[:300]

    def _key_failed(self, key: str, status: int, all_keys: list[str]) -> LLMError | None:
        """Mark a key unusable. Returns None if another key can take over, else the (fatal) error to raise."""
        keys.mark_bad(key, keys.REJECTED_S if status == 401 else keys.OUT_OF_CREDIT_S)
        log.warning("OpenRouter key ending %s failed with %s; %s", key[-4:], status, "switching key" if keys.pick(all_keys) else "no key left")
        return None if keys.pick(all_keys) else self._no_key_left(len(all_keys), status)

    @staticmethod
    def _no_key_left(n: int, status: int | None = None) -> LLMError:
        many = f" All {n} keys in OPENROUTER_API_KEY were tried." if n > 1 else ""
        if status == 402:
            return LLMError("OpenRouter says the credits are used up (402)." + many + " Ask the TAs for more credits, add another key, or pick a cheaper model.", status=402, fatal=True)
        return LLMError("OpenRouter rejected the API key (401)." + many + " Check OPENROUTER_API_KEY in your .env.", status=401, fatal=True)

    # ------------------------------------------------------------------ one model
    def _chat_one(self, model: str, messages: list[dict], temperature: float, max_tokens: int,
                  json_mode: bool) -> tuple[str, str]:
        url = f"{self.s.openrouter_base_url}/chat/completions"
        use_json_mode = json_mode and model not in self._no_json_mode
        last_problem = ""
        rk = self.s.openrouter_keys
        for attempt in range(self.s.http_retries + max(0, len(rk) - 1)):
            key = keys.pick(rk)
            if key is None:
                raise self._no_key_left(len(rk))
            body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            if use_json_mode:
                body["response_format"] = {"type": "json_object"}
            try:
                resp = self._session().post(url, headers=self._headers(key), json=body, timeout=self.s.llm_timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_problem = f"network error: {redact(str(e), *self.s.secrets)[:160]}"
                self._sleep(attempt)
                continue
            st = resp.status_code
            if st in (401, 402):
                err = self._key_failed(key, st, rk)
                if err:
                    raise err
                continue
            if st in (429, 500, 502, 503, 504, 408):
                last_problem = f"HTTP {st}: {self._err_text(resp)}"
                ra = resp.headers.get("Retry-After")
                self._sleep(attempt, float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
                continue
            if st == 400 and use_json_mode and any(k in self._err_text(resp).lower() for k in ("response_format", "json")):
                log.info("model %s does not support json mode; retrying without it", model)
                self._no_json_mode.add(model)
                use_json_mode = False
                continue
            if st >= 400:
                raise LLMError(f"{model}: HTTP {st}: {self._err_text(resp)}", status=st, model_problem=True)
            try:
                data = resp.json()
            except ValueError:
                last_problem = "response was not JSON"
                self._sleep(attempt)
                continue
            if isinstance(data, dict) and data.get("error"):
                e = data["error"]
                code = e.get("code") if isinstance(e, dict) else None
                msg = redact(str(e.get("message") if isinstance(e, dict) else e), *self.s.secrets)[:300]
                if code in (401, 402):
                    err = self._key_failed(key, code, rk)
                    if err:
                        raise err
                    continue
                last_problem = f"provider error {code}: {msg}"
                if code in (429, 500, 502, 503, 504, 408, None):
                    self._sleep(attempt)
                    continue
                raise LLMError(f"{model}: {last_problem}", status=code if isinstance(code, int) else None, model_problem=True)
            try:
                choice = data["choices"][0]
                content = choice["message"].get("content")
                finish = choice.get("finish_reason") or ""
            except (KeyError, IndexError, TypeError):
                last_problem = "unexpected response shape"
                self._sleep(attempt)
                continue
            if isinstance(content, list):  # some providers return content parts
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            usage = data.get("usage") or {}
            self.meter.llm(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), usage.get("cost", 0.0) or 0.0)
            if not content or not str(content).strip():
                last_problem = f"empty completion (finish_reason={finish or 'n/a'})"
                self._sleep(attempt)
                continue
            return str(content), finish
        raise LLMError(f"{model}: gave up after {self.s.http_retries} tries ({last_problem})", model_problem=True)

    # ------------------------------------------------------------------ public
    def chat_full(self, messages: list[dict], *, model: Optional[str] = None, temperature: float = 0.7,
                  max_tokens: int = 4096, json_mode: bool = False, task: str = "") -> tuple[str, str]:
        """Send a chat request and return `(text, finish_reason)`. If the model cannot serve the request, each fallback
        model is tried in turn; fatal errors (bad key, no credit) stop at once.
        """
        if not self.s.openrouter_keys:
            raise LLMError("OPENROUTER_API_KEY is not set. Put it in the .env file (see .env.example).", fatal=True)
        primary = model or self.s.model_writer
        models = [primary] + [m for m in self.s.model_fallbacks if m != primary]
        errors = []
        for m in models:
            try:
                return self._chat_one(m, messages, temperature, max_tokens, json_mode)
            except LLMError as e:
                if e.fatal or not e.model_problem:
                    raise
                errors.append(str(e))
                log.warning("task=%s model %s failed (%s); trying next fallback", task, m, e)
        raise LLMError("All configured models failed: " + " | ".join(errors))

    def vision_ocr(self, png_bytes: bytes, *, prompt: str, model: Optional[str] = None) -> str:
        """Read the text on one page image (PNG bytes) with a vision-capable model. Used for scanned PDF pages."""
        b64 = base64.b64encode(png_bytes).decode("ascii")
        messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}]
        text, _ = self.chat_full(messages, model=model or self.s.model_vision, temperature=0.0, max_tokens=6000, task="ocr")
        return text

    # ------------------------------------------------------------------ diagnostics
    def key_info(self, key: str | None = None) -> dict:
        """GET /auth/key - remaining credit / limit for the key (never returns the key itself)."""
        r = self._session().get(f"{self.s.openrouter_base_url}/auth/key", headers=self._headers(key), timeout=20)
        if r.status_code == 401:
            raise LLMError("OpenRouter rejected the API key (401).", status=401, fatal=True)
        r.raise_for_status()
        d = r.json().get("data", {})
        return {k: d.get(k) for k in ("label", "usage", "limit", "limit_remaining", "is_free_tier") if k in d}

    def list_models(self, query: str = "") -> list[dict]:
        """List the models OpenRouter offers (optionally filtered by a word), with context size and price per million tokens."""
        r = self._session().get(f"{self.s.openrouter_base_url}/models", headers=self._headers(), timeout=30)
        r.raise_for_status()
        out = []
        for m in r.json().get("data", []):
            mid = m.get("id", "")
            if query and query.lower() not in mid.lower() and query.lower() not in (m.get("name", "").lower()):
                continue
            p = m.get("pricing", {}) or {}
            out.append({"id": mid, "name": m.get("name"), "context": m.get("context_length"),
                        "prompt_per_m": _per_million(p.get("prompt")), "completion_per_m": _per_million(p.get("completion"))})
        return out


def _per_million(v) -> float | None:
    try:
        return round(float(v) * 1_000_000, 3)
    except (TypeError, ValueError):
        return None
