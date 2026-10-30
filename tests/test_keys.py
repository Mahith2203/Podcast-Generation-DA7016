"""Several keys per service, failover, masking, .env handling and the `keys` / `doctor` commands."""
from __future__ import annotations

import json

import pytest

from podcast_studio import keys
from podcast_studio.cli import main as cli_main
from podcast_studio.clients.base import LLMError
from podcast_studio.clients.bodhan import BodhanClient, BodhanError
from podcast_studio.clients.openrouter import OpenRouterClient
from podcast_studio.clients.usage import UsageMeter
from podcast_studio.config import Settings
from podcast_studio.doctor import run_doctor

from .fake_servers import BD_BROKE, BD_DEAD, BD_KEY, BD_KEY2, BD_MT_KEY, OR_BROKE, OR_DEAD, OR_KEY, OR_KEY2

MSGS = [{"role": "system", "content": "### TASK: ping"}, {"role": "user", "content": "hi"}]


def test_split_keys_and_mask():
    assert keys.split_keys(" a1234567890 , 'b1234567890';c1234567890\n a1234567890") == ["a1234567890", "b1234567890", "c1234567890"]
    assert keys.split_keys("") == [] and keys.split_keys(None) == []
    assert keys.mask("sk-or-v1-abcdef123456") == "••••3456" and "abcdef" not in keys.mask("sk-or-v1-abcdef123456")


def test_settings_key_lists_and_fallbacks(tmp_path):
    s = Settings.from_env(mock=False, data_dir=tmp_path, openrouter_api_key="o1234567890,o2234567890",
                          bodhan_api_key="legacy-1234567", bodhan_mt_api_key="mt-12345678901")
    assert len(s.openrouter_keys) == 2
    assert s.tts_keys == ["legacy-1234567"]                  # old single key still voices
    assert s.mt_keys == ["mt-12345678901"]                    # dedicated key wins for translation
    s.bodhan_tts_api_key = "tts-1234567890"
    assert s.tts_keys == ["tts-1234567890"] and s.missing_keys() == []
    assert "legacy-1234567" in s.secrets and "o2234567890" in s.secrets
    st = s.key_status()
    assert st["OPENROUTER_API_KEY"]["count"] == 2 and "o1234567890" not in json.dumps(st)


def test_missing_keys_names(tmp_path):
    s = Settings.from_env(mock=False, data_dir=tmp_path, openrouter_api_key="", bodhan_api_key="")
    assert s.missing_keys() == ["OPENROUTER_API_KEY", "BODHAN_TTS_API_KEY"]
    s.bodhan_mt_api_key = "mt-12345678901"       # optional keys never count as missing
    assert s.missing_keys() == ["OPENROUTER_API_KEY", "BODHAN_TTS_API_KEY"]


def test_env_file_with_bom_is_read(tmp_path, monkeypatch):
    from dotenv import dotenv_values
    p = tmp_path / ".env"
    p.write_bytes("﻿OPENROUTER_API_KEY=abc1234567890\nBODHAN_TTS_API_KEY = xyz1234567890\n".encode())
    vals = dotenv_values(p, encoding="utf-8-sig")
    assert vals["OPENROUTER_API_KEY"] == "abc1234567890" and vals["BODHAN_TTS_API_KEY"] == "xyz1234567890"


# ------------------------------------------------------------------ OpenRouter failover
def test_openrouter_skips_dead_and_broke_keys(http_settings, be):
    http_settings.openrouter_api_key = f"{OR_DEAD},{OR_BROKE},{OR_KEY2}"
    c = OpenRouterClient(http_settings, UsageMeter())
    assert c.chat_full(MSGS, model="good/model")[0]
    auths = [r["headers"]["Authorization"] for r in be.or_requests]
    assert auths == [f"Bearer {OR_DEAD}", f"Bearer {OR_BROKE}", f"Bearer {OR_KEY2}"]
    be.reset_counts()
    c.chat_full(MSGS, model="good/model")                     # known-bad keys are not tried again
    assert [r["headers"]["Authorization"] for r in be.or_requests] == [f"Bearer {OR_KEY2}"]


def test_openrouter_all_keys_bad_is_a_clear_fatal_error(http_settings, be):
    http_settings.openrouter_api_key = f"{OR_BROKE},{OR_DEAD}"
    http_settings.model_fallbacks = ()
    with pytest.raises(LLMError) as ei:
        OpenRouterClient(http_settings, UsageMeter()).chat_full(MSGS, model="good/model")
    assert ei.value.fatal and "All 2 keys" in str(ei.value) and OR_DEAD not in str(ei.value)


def test_openrouter_works_with_http_retries_one_and_two_keys(http_settings, be):
    http_settings.http_retries = 1
    http_settings.openrouter_api_key = f"{OR_DEAD},{OR_KEY}"
    assert OpenRouterClient(http_settings, UsageMeter()).chat_full(MSGS, model="good/model")[0]


def test_openrouter_out_of_credit_message(http_settings, be):
    http_settings.openrouter_api_key = OR_BROKE
    http_settings.model_fallbacks = ()
    with pytest.raises(LLMError) as ei:
        OpenRouterClient(http_settings, UsageMeter()).chat_full(MSGS, model="good/model")
    assert ei.value.status == 402 and "credits" in str(ei.value)


# ------------------------------------------------------------------ Bodhan failover
def test_tts_fails_over_to_the_next_key_and_remembers(http_settings, be):
    http_settings.bodhan_api_key = ""
    http_settings.bodhan_tts_api_key = f"{BD_BROKE}, {BD_DEAD}, {BD_KEY2}"
    c = BodhanClient(http_settings, UsageMeter())
    assert c.tts("Hello there.", "Ishita", "en")
    assert [r["headers"]["Authorization"] for r in be.tts_requests] == [f"Bearer {BD_BROKE}", f"Bearer {BD_DEAD}", f"Bearer {BD_KEY2}"]
    be.reset_counts()
    c.tts("Another one.", "Ishita", "en")
    assert [r["headers"]["Authorization"] for r in be.tts_requests] == [f"Bearer {BD_KEY2}"]


def test_tts_all_keys_out_of_credit(http_settings, be):
    http_settings.bodhan_api_key = ""
    http_settings.bodhan_tts_api_key = BD_BROKE
    with pytest.raises(BodhanError) as ei:
        BodhanClient(http_settings, UsageMeter()).tts("Hello.", "Ishita", "en")
    assert ei.value.status == 402 and ei.value.fatal and "no credits" in str(ei.value)


def test_tts_and_mt_use_their_own_keys(http_settings, be):
    be.split_keys = True
    http_settings.bodhan_api_key = ""
    http_settings.bodhan_tts_api_key, http_settings.bodhan_mt_api_key = BD_KEY, BD_MT_KEY
    c = BodhanClient(http_settings, UsageMeter())
    c.tts("Hello.", "Ishita", "en")
    c.translate("Water evaporates.", "hi")
    assert be.tts_requests[-1]["headers"]["Authorization"] == f"Bearer {BD_KEY}"
    assert be.mt_requests[-1]["headers"]["Authorization"] == f"Bearer {BD_MT_KEY}"


def test_missing_key_error_names_the_setting(http_settings):
    http_settings.bodhan_api_key = http_settings.bodhan_tts_api_key = ""
    with pytest.raises(BodhanError) as ei:
        BodhanClient(http_settings, UsageMeter()).tts("Hello.", "Ishita", "en")
    assert "BODHAN_TTS_API_KEY" in str(ei.value)


# ------------------------------------------------------------------ doctor + CLI
def test_doctor_tests_every_key_separately(http_settings, be):
    http_settings.openrouter_api_key = f"{OR_KEY},{OR_DEAD}"
    http_settings.bodhan_api_key = ""
    http_settings.bodhan_tts_api_key = f"{BD_KEY},{BD_DEAD}"
    http_settings.bodhan_mt_api_key = BD_MT_KEY
    be.split_keys = True
    rows = {r["name"]: r for r in run_doctor(http_settings)}
    ok = [n for n, r in rows.items() if r["ok"]]
    bad = [n for n, r in rows.items() if not r["ok"]]
    assert any(n.startswith("OpenRouter key 1/2") for n in ok) and any(n.startswith("OpenRouter key 2/2") for n in bad)
    assert any(n.startswith("Bodhan TTS key 1/2") for n in ok) and any(n.startswith("Bodhan TTS key 2/2") for n in bad)
    assert "Bodhan translate" in ok
    dump = json.dumps(rows)
    assert OR_KEY not in dump and BD_KEY not in dump and BD_MT_KEY not in dump and "DEAD" not in dump.replace("••••", "")


def test_doctor_stops_when_required_key_missing(tmp_path):
    s = Settings.from_env(mock=False, data_dir=tmp_path, openrouter_api_key="", bodhan_api_key="")
    rows = run_doctor(s)
    assert [r["name"] for r in rows][:2] == ["OPENROUTER_API_KEY", "BODHAN_TTS_API_KEY"] and rows[-1]["name"] == "Stopped"


def test_cli_keys_command(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert cli_main(["keys"]) == 0
    assert "MISSING" in capsys.readouterr().out
    assert cli_main(["keys", "--check"]) == 1
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secretsecret-1111")
    monkeypatch.setenv("BODHAN_TTS_API_KEY", "bd-secretsecret-2222,bd-secretsecret-3333")
    assert cli_main(["keys", "--check"]) == 0
    out = capsys.readouterr().out
    assert "••••1111" in out and "••••3333" in out and "secretsecret" not in out
    assert cli_main(["--mock", "keys", "--check"]) == 0


def test_api_config_exposes_only_masked_status(tmp_path):
    from fastapi.testclient import TestClient

    from podcast_studio.api import create_app
    s = Settings.from_env(mock=False, data_dir=tmp_path, openrouter_api_key="or-TOPSECRET-1234,or-TOPSECRET-5678", bodhan_tts_api_key="bd-TOPSECRET-9999")
    with TestClient(create_app(s, env_path=tmp_path / ".env")) as c:
        body = c.get("/api/config").text
        assert "TOPSECRET" not in body and "••••5678" in body
        assert c.get("/api/config").json()["key_status"]["OPENROUTER_API_KEY"]["count"] == 2
