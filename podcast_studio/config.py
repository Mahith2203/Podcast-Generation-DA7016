"""Runtime configuration. Secrets only ever come from environment variables / the local .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .keys import split_keys

ROOT = Path(__file__).resolve().parent.parent

# Load .env (UTF-8) once at import time. Real environment variables win over the file.
load_dotenv(ROOT / ".env", encoding="utf-8-sig", override=False)  # utf-8-sig: Notepad may add a BOM


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _list(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    return tuple(x.strip() for x in raw.split(",") if x.strip())


@dataclass
class Settings:
    """Everything configurable: API keys, model names, timeouts, concurrency and folders. Build it with
    `Settings.from_env()`; secrets are kept out of error messages.
    """
    # --- OpenRouter (LLM) -------------------------------------------------
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_writer: str = "anthropic/claude-sonnet-4.5"
    model_planner: str = "anthropic/claude-sonnet-4.5"
    model_checker: str = "google/gemini-2.5-flash"
    model_vision: str = "google/gemini-2.5-flash"
    model_fallbacks: tuple[str, ...] = ("google/gemini-2.5-flash", "openai/gpt-4o-mini")
    llm_timeout: int = 180
    # --- Bodhan (TTS / MT) -----------------------------------------------
    bodhan_api_key: str = ""
    # Bodhan keys can be tied to one model each, so there is one setting per model. Each may hold several keys
    # separated by commas (failover). BODHAN_API_KEY is the old "one key for everything" setting and still works.
    bodhan_tts_api_key: str = ""   # indic-speak
    bodhan_mt_api_key: str = ""    # indic-translate (optional: only for the "translate" writing mode)
    bodhan_ocr_api_key: str = ""   # indic-ocr (reserved; scanned pages currently use the OpenRouter vision model)
    bodhan_base_url: str = "https://api.bodhan.ai/v1"
    tts_model: str = "indic-speak"
    mt_model: str = "indic-translate"
    tts_timeout: int = 120
    tts_concurrency: int = 3
    mt_concurrency: int = 4
    # --- App --------------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    mock: bool = False
    max_upload_mb: int = 40
    max_source_chars: int = 400_000
    http_retries: int = 5

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        """Read the settings from environment variables (loaded from `.env`), apply any keyword `overrides` and return a
        `Settings`.
        """
        e = os.environ.get
        s = cls(
            openrouter_api_key=(e("OPENROUTER_API_KEY") or "").strip(),
            openrouter_base_url=(e("OPENROUTER_BASE_URL") or cls.openrouter_base_url).rstrip("/"),
            model_writer=e("LLM_MODEL_WRITER") or e("LLM_MODEL") or cls.model_writer,
            model_planner=e("LLM_MODEL_PLANNER") or e("LLM_MODEL") or cls.model_planner,
            model_checker=e("LLM_MODEL_CHECKER") or cls.model_checker,
            model_vision=e("LLM_MODEL_VISION") or cls.model_vision,
            model_fallbacks=_list("LLM_FALLBACK_MODELS", ",".join(cls.model_fallbacks)),
            llm_timeout=_int("LLM_TIMEOUT", cls.llm_timeout),
            bodhan_api_key=(e("BODHAN_API_KEY") or "").strip(),
            bodhan_tts_api_key=(e("BODHAN_TTS_API_KEY") or "").strip(),
            bodhan_mt_api_key=(e("BODHAN_MT_API_KEY") or "").strip(),
            bodhan_ocr_api_key=(e("BODHAN_OCR_API_KEY") or "").strip(),
            bodhan_base_url=(e("BODHAN_BASE_URL") or cls.bodhan_base_url).rstrip("/"),
            tts_model=e("BODHAN_TTS_MODEL") or cls.tts_model,
            mt_model=e("BODHAN_MT_MODEL") or cls.mt_model,
            tts_timeout=_int("TTS_TIMEOUT", cls.tts_timeout),
            tts_concurrency=max(1, _int("TTS_CONCURRENCY", cls.tts_concurrency)),
            mt_concurrency=max(1, _int("MT_CONCURRENCY", cls.mt_concurrency)),
            data_dir=Path(e("DATA_DIR") or (ROOT / "data")),
            mock=_bool("MOCK_APIS", False),
            max_upload_mb=_int("MAX_UPLOAD_MB", cls.max_upload_mb),
            max_source_chars=_int("MAX_SOURCE_CHARS", cls.max_source_chars),
            http_retries=max(1, _int("HTTP_RETRIES", cls.http_retries)),
        )
        for k, v in overrides.items():
            if not hasattr(s, k):
                raise TypeError(f"Unknown setting {k}")
            setattr(s, k, v)
        s.data_dir = Path(s.data_dir)
        return s

    # ---- key lists (a setting may contain several keys separated by commas)
    @property
    def openrouter_keys(self) -> list[str]:
        return split_keys(self.openrouter_api_key)

    @property
    def tts_keys(self) -> list[str]:
        return split_keys(self.bodhan_tts_api_key) or split_keys(self.bodhan_api_key)

    @property
    def mt_keys(self) -> list[str]:
        return split_keys(self.bodhan_mt_api_key) or split_keys(self.bodhan_api_key)

    @property
    def ocr_keys(self) -> list[str]:
        return split_keys(self.bodhan_ocr_api_key) or split_keys(self.bodhan_api_key)

    @property
    def secrets(self) -> tuple[str, ...]:
        every = (self.openrouter_keys + split_keys(self.bodhan_api_key) + split_keys(self.bodhan_tts_api_key)
                 + split_keys(self.bodhan_mt_api_key) + split_keys(self.bodhan_ocr_api_key))
        return tuple(dict.fromkeys(every))

    def missing_keys(self) -> list[str]:
        """Settings that are required but empty (translation and OCR keys are optional)."""
        if self.mock:
            return []
        out = []
        if not self.openrouter_keys:
            out.append("OPENROUTER_API_KEY")
        if not self.tts_keys:
            out.append("BODHAN_TTS_API_KEY")
        return out

    def key_status(self) -> dict:
        """Safe-to-show summary: how many keys each setting has and the last 4 characters of each."""
        from .keys import mask
        rows = {"OPENROUTER_API_KEY": self.openrouter_keys, "BODHAN_TTS_API_KEY": self.tts_keys,
                "BODHAN_MT_API_KEY": self.mt_keys, "BODHAN_OCR_API_KEY": self.ocr_keys}
        return {k: {"count": len(v), "tails": [mask(x) for x in v], "required": k in {"OPENROUTER_API_KEY", "BODHAN_TTS_API_KEY"}}
                for k, v in rows.items()}
