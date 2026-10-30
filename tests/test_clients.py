from __future__ import annotations

import json

import pytest

from podcast_studio.audio import wav_bytes_to_array
from podcast_studio.clients.base import LLMError
from podcast_studio.clients.bodhan import BodhanClient, BodhanError
from podcast_studio.clients.openrouter import OpenRouterClient
from podcast_studio.clients.usage import UsageMeter
from podcast_studio.config import Settings
from podcast_studio.util import redact

from .fake_servers import BD_KEY, OR_KEY

MSGS = [{"role": "system", "content": "### TASK: ping"}, {"role": "user", "content": "hi"}]


def _or(http_settings, **kw) -> OpenRouterClient:
    for k, v in kw.items():
        setattr(http_settings, k, v)
    return OpenRouterClient(http_settings, UsageMeter())


def test_openrouter_basic_call_and_headers(http_settings, be):
    c = _or(http_settings)
    text, finish = c.chat_full(MSGS, model="good/model", json_mode=True)
    assert json.loads(text) and finish == "stop"
    req = be.or_requests[-1]
    assert req["headers"]["Authorization"] == f"Bearer {OR_KEY}"
    assert req["body"]["response_format"] == {"type": "json_object"}
    u = c.meter.snapshot()
    assert u.llm_calls == 1 and u.llm_prompt_tokens == 100 and u.llm_cost_usd == 0.001


def test_openrouter_retries_429_and_500_then_succeeds(http_settings, be):
    be.or_script.extend([("status", 429), ("status", 500), ("error200", 502)])
    c = _or(http_settings)
    text, _ = c.chat_full(MSGS, model="good/model")
    assert text and len(be.or_requests) == 4


def test_openrouter_empty_completion_is_retried(http_settings, be):
    be.or_script.append(("empty",))
    c = _or(http_settings)
    assert c.chat_full(MSGS, model="good/model")[0]
    assert len(be.or_requests) == 2


def test_openrouter_falls_back_when_model_missing(http_settings, be):
    c = _or(http_settings)
    text, _ = c.chat_full(MSGS, model="bad/model")
    assert text
    models = [r["body"]["model"] for r in be.or_requests]
    assert models[0] == "bad/model" and models[-1] == "good/model"


def test_openrouter_401_is_fatal_and_leaks_nothing(http_settings, be):
    http_settings.openrouter_api_key = "or-WRONG-key-1111111111"
    c = OpenRouterClient(http_settings, UsageMeter())
    with pytest.raises(LLMError) as ei:
        c.chat_full(MSGS, model="good/model")
    assert ei.value.fatal and "WRONG" not in str(ei.value)
    assert len(be.or_requests) == 1  # no pointless retries on a bad key


def test_openrouter_gives_up_after_retries(http_settings, be):
    http_settings.http_retries = 3
    be.or_script.extend([("status", 503)] * 20)
    c = _or(http_settings, model_fallbacks=())
    with pytest.raises(LLMError):
        c.chat_full(MSGS, model="good/model")
    assert len(be.or_requests) == 3


def test_openrouter_json_mode_fallback_is_remembered(http_settings, be):
    be.reject_json_mode = True
    c = _or(http_settings)
    assert c.chat_full(MSGS, model="good/model", json_mode=True)[0]
    assert "response_format" in be.or_requests[0]["body"] and "response_format" not in be.or_requests[1]
    c.chat_full(MSGS, model="good/model", json_mode=True)
    assert "response_format" not in be.or_requests[-1]["body"]  # remembered per model


def test_llm_json_repairs_malformed_output(http_settings, be):
    be.or_script.append(("content", "Sure! here you go: {'verdict': 'pass',"))  # unparseable
    be.or_script.append(("content", '```json\n{"verdict": "pass", "issues": []}\n```'))
    c = _or(http_settings)
    out = c.json(task="check", system="### TASK: check", user="x", validate=lambda d: d)
    assert out["verdict"] == "pass"


def test_key_info_and_models(http_settings, be):
    c = _or(http_settings)
    assert c.key_info()["limit_remaining"] == 8.5
    assert c.list_models("good")[0]["id"] == "good/model"


# ---------------------------------------------------------------------- Bodhan


def _bd(http_settings) -> BodhanClient:
    return BodhanClient(http_settings, UsageMeter())


def test_tts_request_shape_and_wav(http_settings, be):
    c = _bd(http_settings)
    wav = c.tts("Hello there, friends.", "Ishita", "en", "happy")
    assert len(wav_bytes_to_array(wav)) > 1000
    b = be.tts_requests[-1]["body"]
    assert b["model"] == "indic-speak" and b["voice"] == "Ishita" and b["input"] == "Hello there, friends."
    assert json.loads(b["instructions"]) == {"lang": "en", "style": "happy"}
    assert be.tts_requests[-1]["headers"]["Authorization"] == f"Bearer {BD_KEY}"


def test_tts_no_style_omits_the_key(http_settings, be):
    _bd(http_settings).tts("Namaste", "Amit", "hi")
    assert json.loads(be.tts_requests[-1]["body"]["instructions"]) == {"lang": "hi"}


def test_tts_style_422_retries_without_style(http_settings, be):
    be.bad_styles.add("happy")
    c = _bd(http_settings)
    assert c.tts("Hello.", "Ishita", "en", "happy")
    styles = [json.loads(r["body"]["instructions"]).get("style") for r in be.tts_requests]
    assert styles == ["happy", None]
    c.tts("Hello again.", "Ishita", "en", "happy")  # remembered: no second wasted call
    assert len(be.tts_requests) == 3


def test_tts_unknown_voice_error_names_the_voice(http_settings, be):
    with pytest.raises(BodhanError) as ei:
        _bd(http_settings).tts("Hello.", "NoSuchVoice", "en")
    assert "NoSuchVoice" in str(ei.value)
    assert len(be.tts_requests) == 1


def test_tts_retries_5xx_and_rejects_non_audio(http_settings, be):
    be.tts_script.extend([("status", 503), ("status", 429)])
    assert _bd(http_settings).tts("Hello.", "Ishita", "en")
    assert len(be.tts_requests) == 3
    be.reset_counts()
    http_settings.http_retries = 2
    be.tts_script.extend([("notwav",), ("notwav",), ("notwav",)])
    with pytest.raises(BodhanError):
        _bd(http_settings).tts("Hello.", "Ishita", "en")


def test_tts_200_with_json_error_body_is_an_error(http_settings, be):
    http_settings.http_retries = 1
    be.tts_script.append(("json200",))
    with pytest.raises(BodhanError):
        _bd(http_settings).tts("Hello.", "Ishita", "en")


def test_tts_bad_key_is_fatal_and_redacted(http_settings, be):
    http_settings.bodhan_api_key = "bd-WRONG-key-2222222222"
    with pytest.raises(BodhanError) as ei:
        BodhanClient(http_settings, UsageMeter()).tts("Hello.", "Ishita", "en")
    assert ei.value.fatal and "WRONG" not in str(ei.value)


def test_translate_shape(http_settings, be):
    out = _bd(http_settings).translate("Water evaporates.", "hi")
    assert out.startswith("«hi»")
    b = be.mt_requests[-1]["body"]
    assert b["model"] == "indic-translate" and b["target_language_code"] == "hi" and b["target_script"] == "native"
    assert b["messages"][-1]["content"] == "Water evaporates."


def test_redact_removes_keys_and_bearer_tokens():
    s = redact("Authorization: Bearer abc123456789 sk-or-v1-abcdefabcdef0123 and mysecretvalue", "mysecretvalue")
    assert "abc123456789" not in s and "sk-or-v1" not in s and "mysecretvalue" not in s


def test_missing_keys_reporting(tmp_path):
    s = Settings.from_env(openrouter_api_key="", bodhan_api_key="", mock=False, data_dir=tmp_path)
    assert s.missing_keys() == ["OPENROUTER_API_KEY", "BODHAN_TTS_API_KEY"]
    assert Settings.from_env(mock=True, data_dir=tmp_path).missing_keys() == []


def test_translation_can_use_its_own_key(http_settings, be):
    from .fake_servers import BD_MT_KEY
    be.split_keys = True  # keys are tied to one model each, like the Bodhan console
    c = _bd(http_settings)
    assert c.tts("Hello.", "Ishita", "en")                       # the indic-speak key still voices
    with pytest.raises(BodhanError) as ei:                         # ...but cannot translate
        c.translate("Water evaporates.", "hi")
    assert ei.value.fatal and "BODHAN_MT_API_KEY" in str(ei.value)
    http_settings.bodhan_mt_api_key = BD_MT_KEY
    assert BodhanClient(http_settings, UsageMeter()).translate("Water evaporates.", "hi").startswith("«hi»")
    assert be.mt_requests[-1]["headers"]["Authorization"] == f"Bearer {BD_MT_KEY}"
    assert be.tts_requests[-1]["headers"]["Authorization"] == f"Bearer {BD_KEY}"  # TTS never uses the MT key


def test_doctor_treats_missing_translate_key_as_optional(http_settings, be, capsys):
    from podcast_studio.cli import cmd_doctor
    be.split_keys = True
    assert cmd_doctor(None, http_settings) == 0            # exit code OK although translation is unavailable
    out = capsys.readouterr().out
    assert "[FAIL] Bodhan translate" in out and "BODHAN_MT_API_KEY" in out and "[OK ] Bodhan TTS" in out


def test_mt_key_is_redacted_everywhere(tmp_path):
    s = Settings.from_env(mock=False, data_dir=tmp_path, bodhan_mt_api_key="bd-mt-SECRET-123456")
    assert "bd-mt-SECRET-123456" in s.secrets
