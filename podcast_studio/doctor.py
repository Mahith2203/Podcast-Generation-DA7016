"""Setup diagnostics: verifies every key, the models, TTS, translation and ffmpeg with tiny, cheap calls.
Never prints or returns secrets (keys are shown only as the last 4 characters)."""
from __future__ import annotations

import dataclasses
import time

from . import audio, keys
from .clients import BodhanClient, BodhanError, LLMError, make_clients
from .clients.openrouter import OpenRouterClient
from .config import Settings


def _row(name: str, ok: bool, detail: str, hint: str = "") -> dict:
    return {"name": name, "ok": ok, "detail": detail, "hint": hint}


def is_optional(row: dict) -> bool:
    """Rows whose failure must not stop you (translation key, ffmpeg)."""
    return row["name"] == "ffmpeg" or row["name"].startswith(("Bodhan translate", "Video"))


def _one_key(settings: Settings, **kw) -> Settings:
    """A copy of the settings that uses exactly one key per service, so each key is tested on its own."""
    return dataclasses.replace(settings, **kw)


def run_doctor(settings: Settings) -> list[dict]:
    """Check the whole setup and return one row per check (`name`, `ok`, `detail`, `hint`): required keys, every
    OpenRouter and Bodhan key separately, each model, voice synthesis, translation, ffmpeg, and the video tools. Stops
    early if a required key is missing.
    """
    keys.reset()  # a diagnostic run should judge every key fresh
    rows: list[dict] = []
    llm, bodhan, _ = make_clients(settings)
    if settings.mock:
        rows.append(_row("Mode", True, "MOCK_APIS is on: offline demo with synthetic 'beep speech'. No keys are used.",
                         "Set MOCK_APIS=0 in .env to use the real services."))
    missing = settings.missing_keys()
    if not settings.mock:
        st = settings.key_status()
        for name, info in st.items():
            if info["required"]:
                rows.append(_row(name, info["count"] > 0, f"{info['count']} key(s): {', '.join(info['tails'])}" if info["count"] else "missing",
                                 "Paste it into the .env file (see .env.example) or use the Keys button / `setup`." if not info["count"] else ""))
    if missing:
        return rows + [_row("Stopped", False, "Fix the missing keys above, then run the check again.")]

    # ---- OpenRouter
    if isinstance(llm, OpenRouterClient):
        for i, k in enumerate(settings.openrouter_keys, 1):
            label = f"OpenRouter key {i}/{len(settings.openrouter_keys)} ({keys.mask(k)})" if len(settings.openrouter_keys) > 1 else "OpenRouter key"
            try:
                info = OpenRouterClient(_one_key(settings, openrouter_api_key=k)).key_info()
                rem = info.get("limit_remaining")
                rows.append(_row(label, True, "valid" + (f", ${rem:.2f} credit remaining" if isinstance(rem, (int, float)) else "")))
            except LLMError as e:
                rows.append(_row(label, False, str(e), "Check this key in OPENROUTER_API_KEY."))
            except Exception as e:  # noqa: BLE001
                rows.append(_row(label, False, f"could not verify ({e.__class__.__name__}); will test a model call instead"))
        for label, model in (("Writer model", settings.model_writer), ("Planner model", settings.model_planner), ("Checker model", settings.model_checker)):
            if label == "Planner model" and model == settings.model_writer:
                continue
            t0 = time.time()
            try:
                llm._chat_one(model, [{"role": "user", "content": "Reply with the single word OK."}], 0.0, 16, False)
                rows.append(_row(label, True, f"{model} answered in {time.time() - t0:.1f}s"))
            except LLMError as e:
                rows.append(_row(label, False, str(e)[:240],
                                 "Pick a model your credits allow: run `python -m podcast_studio.cli models` and set LLM_MODEL_* in .env."))
    else:
        rows.append(_row("LLM", True, "mock"))

    # ---- Bodhan TTS (every key)
    tts_keys = settings.tts_keys
    for i, k in enumerate(tts_keys, 1):
        label = f"Bodhan TTS key {i}/{len(tts_keys)} ({keys.mask(k)})" if len(tts_keys) > 1 else "Bodhan TTS"
        one = BodhanClient(_one_key(settings, bodhan_tts_api_key=k, bodhan_api_key=""))
        try:
            t0 = time.time()
            wav = one.tts("Hello, this is a quick voice test.", "Kavya", "en") if not settings.mock else bodhan.tts("Hello, this is a quick voice test.", "Kavya", "en")
            dur = audio.duration(audio.wav_bytes_to_array(wav))
            rows.append(_row(label, True, f"indic-speak returned {dur:.1f}s of audio in {time.time() - t0:.1f}s (voice Kavya, English)"))
            if i == 1:
                try:
                    one.tts("That is wonderful news!", "Amit", "en", "happy")
                    bad = getattr(one, "_bad_styles", set())
                    rows.append(_row("TTS styles", "happy" not in bad, "style 'happy' accepted" if "happy" not in bad
                                     else "style 'happy' was rejected; styles will be skipped automatically"))
                except BodhanError as e:
                    rows.append(_row("TTS styles", False, str(e)[:200]))
        except BodhanError as e:
            rows.append(_row(label, False, str(e)[:300],
                             "This key must include the indic-speak model, and your console.bodhan.ai account must use the course e-mail."))

    # ---- Bodhan translate (optional)
    mt_keys = settings.mt_keys
    if not mt_keys:
        rows.append(_row("Bodhan translate", False, "no translation key set",
                         "Optional (only for 'translate' writing mode). Create a Bodhan key with indic-translate and put it in BODHAN_MT_API_KEY."))
    for i, k in enumerate(mt_keys, 1):
        label = f"Bodhan translate key {i}/{len(mt_keys)} ({keys.mask(k)})" if len(mt_keys) > 1 else "Bodhan translate"
        try:
            one = BodhanClient(_one_key(settings, bodhan_mt_api_key=k, bodhan_api_key="")) if not settings.mock else bodhan
            out = one.translate("The meeting has been postponed.", "hi")
            rows.append(_row(label, True, f"indic-translate works: {out[:60]}"))
        except BodhanError as e:
            rows.append(_row(label, False, str(e)[:300],
                             "Optional (only for 'translate' writing mode). The key must include the indic-translate model; set it in BODHAN_MT_API_KEY."))

    if settings.ocr_keys and not settings.mock and settings.bodhan_ocr_api_key:
        rows.append(_row("BODHAN_OCR_API_KEY", True, f"{len(settings.ocr_keys)} key(s) saved (not used yet: scanned pages use the OpenRouter vision model)"))

    # ---- local tools
    rows.append(_row("ffmpeg", bool(audio.ffmpeg_path()),
                     "found (MP3 export + loudness normalisation enabled)" if audio.ffmpeg_path() else "not found: episodes are WAV only",
                     "" if audio.ffmpeg_path() else "Optional. Windows: `winget install Gyan.FFmpeg`, then restart the terminal."))
    # ---- video (optional): the slides are drawn by a browser engine, encoded by ffmpeg
    from .video import mux, render

    if audio.ffmpeg_path():
        x264 = mux.has_x264()
        rows.append(_row("Video encoder", x264, "ffmpeg has H.264 (libx264)" if x264 else "this ffmpeg build has no H.264 encoder",
                         "" if x264 else "Optional (only for the video). Install the full build: `winget install Gyan.FFmpeg`."))
    ok, detail = render.browser_status()
    rows.append(_row("Video renderer", ok, f"{detail} is ready to draw slides" if ok else detail.splitlines()[0],
                     "" if ok else "Optional (only for the video). Run: pip install playwright   then   python -m playwright install chromium"))
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        rows.append(_row("Data folder", True, str(settings.data_dir)))
    except OSError as e:
        rows.append(_row("Data folder", False, str(e)))
    return rows
