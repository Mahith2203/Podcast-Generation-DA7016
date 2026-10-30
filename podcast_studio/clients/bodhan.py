"""Bodhan gateway client: text-to-speech (indic-speak) and machine translation (indic-translate).

Request shapes follow the Bodhan API reference exactly:
  TTS: POST {base}/audio/speech  {"model": "indic-speak", "input": text, "voice": name,
                                   "instructions": "<JSON string: {\"lang\": \"hi\", \"style\": \"happy\"}>"}
       -> audio/wav (PCM16, 24 kHz, mono)
  MT : POST {base}/chat/completions  {"model": "indic-translate", "messages": [{"role": "user", "content": text}],
                                      "target_language_code": "kn", "target_script": "native"}
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time

import requests

from .. import keys
from ..audio import AudioError, wav_bytes_to_array
from ..config import Settings
from ..util import redact
from .usage import UsageMeter

log = logging.getLogger("podcast.bodhan")


class BodhanError(RuntimeError):
    """A Bodhan (voice or translation) call failed. `status` is the HTTP status if known; `fatal` marks a bad key or
    exhausted credit.
    """
    def __init__(self, message: str, *, status: int | None = None, fatal: bool = False):
        super().__init__(message)
        self.status = status
        self.fatal = fatal


class BodhanClient:
    """Client for Bodhan's `indic-speak` voices (`tts`) and `indic-translate` (`translate`). Requests retry with backoff,
    move on to the next key when one is rejected, and report their usage to the meter.
    """
    BACKOFF = 1.5

    def __init__(self, settings: Settings, meter: UsageMeter | None = None):
        self.s = settings
        self.meter = meter or UsageMeter()
        self._local = threading.local()
        self._bad_styles: set[str] = set()

    # ------------------------------------------------------------------ http
    def _session(self) -> requests.Session:
        sess = getattr(self._local, "sess", None)
        if sess is None:
            sess = requests.Session()
            self._local.sess = sess
        return sess

    def _keys(self, mt: bool) -> list[str]:
        return self.s.mt_keys if mt else self.s.tts_keys

    @staticmethod
    def _headers(key: str) -> dict:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _sleep(self, attempt: int, retry_after: float | None = None) -> None:
        delay = retry_after if retry_after is not None else min(30.0, self.BACKOFF * (2 ** attempt))
        if delay:
            time.sleep(delay * (0.85 + 0.3 * random.random()))

    @staticmethod
    def _message(resp: requests.Response, secrets: tuple[str, ...]) -> str:
        try:
            d = resp.json()
            if isinstance(d, dict):
                for k in ("detail", "message", "error"):
                    v = d.get(k)
                    if isinstance(v, dict):
                        v = v.get("message") or json.dumps(v, ensure_ascii=False)
                    if isinstance(v, list):
                        v = "; ".join(str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in v)
                    if v:
                        return redact(str(v), *secrets)[:600]
            return redact(json.dumps(d, ensure_ascii=False), *secrets)[:600]
        except ValueError:
            return redact(resp.text or "", *secrets)[:300]

    def _post(self, path: str, body: dict, timeout: int, mt: bool = False) -> requests.Response:
        """POST with retry on 429/5xx/network errors and failover between keys. Returns the final response (may be a 4xx the caller handles)."""
        env = "BODHAN_MT_API_KEY" if mt else "BODHAN_TTS_API_KEY"
        model = "indic-translate" if mt else "indic-speak"
        ring = self._keys(mt)
        if not ring:
            raise BodhanError(f"{env} is not set. Put it in the .env file (see .env.example).", fatal=True)
        url = f"{self.s.bodhan_base_url}{path}"
        last = ""
        for attempt in range(self.s.http_retries + max(0, len(ring) - 1)):
            key = keys.pick(ring)
            if key is None:
                raise self._no_key_left(env, model, len(ring), None)
            try:
                resp = self._session().post(url, headers=self._headers(key), json=body, timeout=timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                last = f"network error: {redact(str(e), *self.s.secrets)[:160]}"
                self._sleep(attempt)
                continue
            st = resp.status_code
            if st in (401, 402, 403):
                keys.mark_bad(key, keys.OUT_OF_CREDIT_S if st == 402 else keys.REJECTED_S)
                if keys.pick(ring):
                    log.warning("Bodhan key ending %s failed with %s; switching key", key[-4:], st)
                    continue
                raise self._no_key_left(env, model, len(ring), st)
            if st in (408, 429, 500, 502, 503, 504):
                last = f"HTTP {st}: {self._message(resp, self.s.secrets)}"
                ra = resp.headers.get("Retry-After")
                self._sleep(attempt, float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
                continue
            return resp
        raise BodhanError(f"Bodhan request to {path} failed after {self.s.http_retries} tries ({last})")

    @staticmethod
    def _no_key_left(env: str, model: str, n: int, status: int | None) -> "BodhanError":
        many = f" All {n} keys in {env} were tried." if n > 1 else ""
        if status == 402:
            return BodhanError(f"Bodhan reports no credits left (HTTP 402).{many} Check your balance in the Bodhan console or add another key.", status=402, fatal=True)
        return BodhanError(f"Bodhan rejected the API key (HTTP {status or 401}).{many} Check {env} in your .env (the key must include the {model} model).",
                           status=status or 401, fatal=True)

    # ------------------------------------------------------------------ TTS
    def tts(self, text: str, voice: str, lang: str, style: str | None = None) -> bytes:
        """Return WAV bytes for one short piece of text (keep it to a sentence or two)."""
        use_style = style if style and style not in self._bad_styles else None
        for _ in range(2):
            instr = {"lang": lang}
            if use_style:
                instr["style"] = use_style
            body = {"model": self.s.tts_model, "input": text, "voice": voice,
                    "instructions": json.dumps(instr, ensure_ascii=False)}
            resp = self._post("/audio/speech", body, self.s.tts_timeout)
            if resp.status_code == 422:
                msg = self._message(resp, self.s.secrets)
                if use_style and "style" in msg.lower():
                    log.warning("Bodhan rejected style %r (%s); retrying without a style", use_style, msg[:120])
                    self._bad_styles.add(use_style)
                    use_style = None
                    continue
                raise BodhanError(f"Bodhan TTS rejected the request: {msg}", status=422)
            if resp.status_code >= 400:
                raise BodhanError(f"Bodhan TTS error HTTP {resp.status_code}: {self._message(resp, self.s.secrets)}", status=resp.status_code)
            data = resp.content
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype.lower() and data[:4] != b"RIFF":
                raise BodhanError(f"Bodhan TTS returned JSON instead of audio: {redact(data[:200].decode('utf-8', 'replace'), *self.s.secrets)}")
            try:
                wav_bytes_to_array(data)  # validates the payload really is decodable audio
            except AudioError as e:
                raise BodhanError(f"Bodhan TTS returned unusable audio: {e}") from e
            self.meter.tts(len(text))
            return data
        raise BodhanError("Bodhan TTS failed")  # pragma: no cover

    # ------------------------------------------------------------------ MT
    def translate(self, text: str, target_lang: str, script: str = "native") -> str:
        """Translate `text` into `target_lang` with indic-translate, written in `script` (native script by default).
        Blank text is returned unchanged.
        """
        if not text.strip():
            return text
        body = {"model": self.s.mt_model, "messages": [{"role": "user", "content": text}],
                "target_language_code": target_lang, "target_script": script}
        resp = self._post("/chat/completions", body, self.s.llm_timeout, mt=True)
        if resp.status_code >= 400:
            raise BodhanError(f"Bodhan translate error HTTP {resp.status_code}: {self._message(resp, self.s.secrets)}", status=resp.status_code)
        try:
            out = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise BodhanError("Bodhan translate returned an unexpected response shape.") from None
        out = (out or "").strip()
        if not out:
            raise BodhanError("Bodhan translate returned an empty translation.")
        self.meter.mt(len(text))
        return out
