"""Tuning, validation, remix, setup wizard, fine-tuning trials and the matching API endpoints."""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from podcast_studio import tuning
from podcast_studio.api import create_app
from podcast_studio.cli import main as cli_main
from podcast_studio.config import Settings
from podcast_studio.models import ProjectSettings
from podcast_studio.pipeline.orchestrator import Orchestrator
from podcast_studio.setup_wizard import run_wizard
from podcast_studio.store import ProjectStore
from podcast_studio.util import update_env_file
from podcast_studio.validate import format_report, validate_project

from .conftest import noprog
from .fake_servers import BD_KEY, OR_KEY


@pytest.fixture(autouse=True)
def tuning_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tuning, "TUNING_FILE", tmp_path / "tuning.json")
    tuning._cache.update(mtime=None, path=None, value=None)
    yield tmp_path / "tuning.json"
    tuning._cache.update(mtime=None, path=None, value=None)


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    """One finished mock episode shared by the read-only tests in this file."""
    s = Settings.from_env(mock=True, data_dir=tmp_path_factory.mktemp("made"))
    store = ProjectStore(s.data_dir)
    orch = Orchestrator(s, store)
    p = orch.create_project(title="", settings=ProjectSettings(minutes=4), text=(__import__("pathlib").Path(__file__).parent.parent / "samples" / "water_cycle.txt").read_text(encoding="utf-8"))
    orch.run_generate(p.id, noprog)
    return s, store, orch, p.id


def fresh_copy(made, tmp_path):
    """A private copy of the shared project so mutating tests do not interfere."""
    import shutil
    s, store, _, pid = made
    s2 = Settings.from_env(mock=True, data_dir=tmp_path / "copy")
    shutil.copytree(s.data_dir, s2.data_dir)
    st2 = ProjectStore(s2.data_dir)
    return s2, st2, Orchestrator(s2, st2), pid


# ------------------------------------------------------------------ tuning
def test_tuning_defaults_clamp_and_unknown_keys(tuning_file):
    assert tuning.current().writer_temperature == 0.85
    tuning_file.write_text(json.dumps({"writer_temperature": 99, "segment_gap": -5, "bogus": 1, "jingle_gain": "x"}), encoding="utf-8")
    t = tuning.current()
    assert t.writer_temperature == 1.5 and t.segment_gap == 0.1 and t.jingle_gain == 1.0


def test_tuning_broken_file_falls_back_to_defaults(tuning_file):
    tuning_file.write_text("{not json", encoding="utf-8")
    assert tuning.current() == tuning.Tuning()


def test_tuning_save_preset_and_reload(tuning_file):
    tuning.save(tuning.PRESETS["calm"])
    assert json.loads(tuning_file.read_text(encoding="utf-8"))["segment_gap"] == 0.8
    assert tuning.current().segment_gap == 0.8
    tuning_file.write_text(json.dumps({"preset": "lively", "gap_jitter": 0.01}), encoding="utf-8")
    t = tuning.current()
    assert t.reaction_overlap == 0.22 and t.gap_jitter == 0.01


def test_tuning_override_is_temporary():
    with tuning.override(writer_temperature=0.2):
        assert tuning.current().writer_temperature == 0.2
    assert tuning.current().writer_temperature == 0.85


def test_pacing_tuning_really_changes_the_episode(made, tmp_path):
    s, store, orch, pid = fresh_copy(made, tmp_path)
    d0 = store.load(pid).final.duration
    tuning.save({"segment_gap": 2.0, "gap_speaker_change": 0.9, "gap_same_speaker": 0.8})
    n_before = store.load(pid).usage.tts_calls
    orch.run_remix(pid, noprog)
    p = store.load(pid)
    assert p.status == "ready" and p.final.duration > d0 + 8
    assert p.usage.tts_calls == n_before  # re-mix reuses the cache: zero new voice requests
    tuning.save({"segment_gap": 0.1, "gap_speaker_change": 0.05, "gap_same_speaker": 0.05, "gap_jitter": 0})
    orch.run_remix(pid, noprog)
    assert store.load(pid).final.duration < d0 - 3


def test_remix_requires_audio(made, tmp_path):
    s, store, orch, _ = made
    p = orch.create_project(title="", settings=ProjectSettings(minutes=3, auto_voice=False), text="Plants make food. " * 60)
    with pytest.raises(ValueError):
        orch.run_remix(p.id, noprog)


# ------------------------------------------------------------------ validation
def test_validate_healthy_episode_has_no_faithfulness_failures(made):
    s, store, _, pid = made
    rep = validate_project(store, pid)
    bad = [c for c in rep["checks"] if c["status"] == "fail" and c["area"] in {"structure", "faithfulness", "outputs"}]
    assert not bad, bad
    assert 0 <= rep["score"] <= 100 and (store.dir(pid) / "reports" / "validation.json").exists()
    assert "Validation of" in format_report(rep)


def test_validate_catches_an_invented_number_after_hand_edit(made, tmp_path):
    s, store, orch, pid = fresh_copy(made, tmp_path)
    orch.run_edit_turn(pid, 1, 1, "Scientists measured exactly 4,812 litres in 1987.", None, noprog)
    rep = validate_project(store, pid)
    nums = next(c for c in rep["checks"] if c["name"] == "Numbers match the chapter")
    assert nums["status"] == "fail" and "4812" in nums["detail"].replace(",", "")
    assert any(c["name"] == "Hand-edited lines" for c in rep["checks"])
    assert rep["verdict"] == "fail"


def test_validate_flags_needs_review_and_missing_audio(made, tmp_path):
    s, store, orch, pid = fresh_copy(made, tmp_path)
    store.update(pid, lambda p: setattr(p.segments[1].version().check, "needs_review", True))
    rep = validate_project(store, pid)
    assert next(c for c in rep["checks"] if c["name"] == "Fact-check status")["status"] == "fail"
    p2 = orch.create_project(title="", settings=ProjectSettings(minutes=3, auto_voice=False), text=(store.load_source_text(pid)))
    orch.run_generate(p2.id, noprog)
    rep2 = validate_project(store, p2.id)
    assert not any(c["area"] == "audio" for c in rep2["checks"])  # script-only: audio checks are skipped, not failed


def test_validate_deep_catches_unsupported_claim_the_rules_miss(made, tmp_path):
    s, store, orch, pid = fresh_copy(made, tmp_path)
    from podcast_studio.clients import make_clients
    orch.run_edit_turn(pid, 1, 1, "Scientists proved this was caused by moon dust.", None, noprog)
    llm = make_clients(s)[0]
    rep = validate_project(store, pid, deep=True, llm=llm)
    deep = [c for c in rep["checks"] if c["name"].startswith("Fresh fact-check")]
    assert len(deep) == 5 and deep[1]["status"] == "fail" and "moon dust" in deep[1]["detail"].lower()
    assert all(c["status"] == "pass" for i, c in enumerate(deep) if i != 1)


def test_validate_detects_robotic_script(made, tmp_path):
    s, store, orch, pid = fresh_copy(made, tmp_path)

    def robot(p):
        v = p.segments[1].version()
        for t in v.turns:
            t.speaker, t.reaction = "B", False
            t.text = t.text.replace("?", ".")
    store.update(pid, robot)
    rep = validate_project(store, pid)
    assert next(c for c in rep["checks"] if c["name"] == "Back-and-forth")["status"] in {"warn", "fail"} or \
        next(c for c in rep["checks"] if c["name"] == "Questions")["status"] != "pass"


# ------------------------------------------------------------------ env file + wizard
def test_update_env_file_preserves_comments_and_other_lines(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# my notes\nFOO=bar\nOPENROUTER_API_KEY=\n# BODHAN_API_KEY=commented\nहिंदी=ठीक\n", encoding="utf-8")
    update_env_file(p, {"OPENROUTER_API_KEY": "abc1234567890", "BODHAN_API_KEY": "zzz1234567890"})
    txt = p.read_text(encoding="utf-8")
    assert "# my notes" in txt and "FOO=bar" in txt and "OPENROUTER_API_KEY=abc1234567890" in txt
    assert txt.count("BODHAN_API_KEY=zzz1234567890") == 1 and "हिंदी=ठीक" in txt and "# BODHAN_API_KEY=commented" in txt


def _scripted(answers):
    it = iter(answers)
    return lambda prompt="": next(it)


def test_setup_wizard_end_to_end_against_fake_services(tmp_path, fake, be, monkeypatch):
    for k, v in {"OPENROUTER_BASE_URL": fake.or_url, "BODHAN_BASE_URL": fake.bd_url, "OPENROUTER_API_KEY": "", "BODHAN_API_KEY": "", "BODHAN_TTS_API_KEY": "", "BODHAN_MT_API_KEY": "", "BODHAN_OCR_API_KEY": "",
                 "LLM_MODEL_WRITER": "", "LLM_MODEL_CHECKER": "", "MOCK_APIS": "0"}.items():
        monkeypatch.setenv(k, v)
    env_path = tmp_path / ".env"
    lines: list[str] = []
    s = Settings.from_env(data_dir=tmp_path / "d")
    rc = run_wizard(s, ask_secret=_scripted(["short", OR_KEY, BD_KEY, ""]),            # first answer is rejected as too short
                    ask=_scripted(["good/model", "good/model", "n"]), out=lines.append, env_path=env_path)
    out = "\n".join(lines)
    assert rc == 0, out
    saved = env_path.read_text(encoding="utf-8")
    assert f"OPENROUTER_API_KEY={OR_KEY}" in saved and f"BODHAN_TTS_API_KEY={BD_KEY}" in saved and "MOCK_APIS=0" in saved
    assert OR_KEY not in out and BD_KEY not in out            # never echoed
    assert "does not look like a key" in out and "All set" in out and "[OK ] Bodhan TTS" in out


def test_setup_wizard_reports_a_wrong_key_clearly(tmp_path, fake, be, monkeypatch):
    for k, v in {"OPENROUTER_BASE_URL": fake.or_url, "BODHAN_BASE_URL": fake.bd_url, "OPENROUTER_API_KEY": "", "BODHAN_API_KEY": "", "BODHAN_TTS_API_KEY": "", "BODHAN_MT_API_KEY": "", "BODHAN_OCR_API_KEY": "",
                 "LLM_MODEL_WRITER": "", "LLM_MODEL_CHECKER": "", "MOCK_APIS": "0"}.items():
        monkeypatch.setenv(k, v)
    lines: list[str] = []
    rc = run_wizard(Settings.from_env(data_dir=tmp_path / "d"), ask_secret=_scripted([OR_KEY, "bd-WRONGWRONGWRONG", ""]),
                    ask=_scripted(["good/model", "good/model", "n"]), out=lines.append, env_path=tmp_path / ".env")
    out = "\n".join(lines)
    assert rc == 1 and "[FAIL] Bodhan TTS" in out and "WRONGWRONG" not in out


def test_setup_wizard_keeps_existing_keys(tmp_path, monkeypatch):
    for k in ("OPENROUTER_API_KEY", "BODHAN_API_KEY", "BODHAN_TTS_API_KEY", "BODHAN_MT_API_KEY", "LLM_MODEL_WRITER", "LLM_MODEL_CHECKER"):
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("MOCK_APIS", "0")
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=keepme-1234567890\nBODHAN_API_KEY=keepme-0987654321\n", encoding="utf-8")
    rc = run_wizard(Settings.from_env(data_dir=tmp_path / "d"), ask_secret=_scripted([""]), ask=_scripted(["y", "y", "", "", "y"]),
                    out=lambda *_: None, env_path=env, test=False)
    t = env.read_text(encoding="utf-8")
    assert rc == 0 and "keepme-1234567890" in t and "MOCK_APIS=1" in t


# ------------------------------------------------------------------ fine-tuning trials and voice samples
def test_trials_rank_configurations_and_clean_up(tmp_path):
    from podcast_studio import tune
    s = Settings.from_env(mock=True, data_dir=tmp_path / "d")
    text = (__import__("pathlib").Path(__file__).parent.parent / "samples" / "water_cycle.txt").read_text(encoding="utf-8")
    rows = tune.run_trials(s, text, models=[None], temperatures=[0.5, 1.0], say=lambda m: None)
    assert len(rows) == 2 and all(r["score"] > 50 and not r["error"] for r in rows)
    assert ProjectStore(s.data_dir).list() == []          # trial projects deleted
    assert "Best:" in tune.format_trials(rows)


def test_trials_survive_a_failing_model(tmp_path, fake, be):
    from podcast_studio import tune
    s = Settings.from_env(mock=False, data_dir=tmp_path / "d", openrouter_api_key=OR_KEY, openrouter_base_url=fake.or_url, bodhan_api_key=BD_KEY,
                          bodhan_base_url=fake.bd_url, model_fallbacks=(), http_retries=1, model_planner="good/model", model_checker="good/model")
    rows = tune.run_trials(s, "Plants make food from light. " * 40, models=["bad/model", "good/model"], temperatures=[0.8], say=lambda m: None)
    assert rows[0]["model"] == "good/model" and rows[0]["error"] == "" and rows[-1]["error"] and rows[-1]["score"] == 0


def test_voice_samples_page(tmp_path):
    from podcast_studio import tune
    s = Settings.from_env(mock=True, data_dir=tmp_path / "d")
    page = tune.render_voice_samples(s, tmp_path / "vs", voices=["Kavya", "Amit"], lang="hi", say=lambda m: None)
    html = page.read_text(encoding="utf-8")
    assert "Kavya.wav" in html and "Amit.wav" in html and (tmp_path / "vs" / "Kavya.wav").stat().st_size > 1000
    with pytest.raises(ValueError):
        tune.render_voice_samples(s, tmp_path / "vs2", voices=["Nobody"])


# ------------------------------------------------------------------ CLI
def test_cli_tune_and_validate(made, tmp_path, monkeypatch, capsys):
    s, store, _, pid = made
    monkeypatch.setenv("DATA_DIR", str(s.data_dir))
    assert cli_main(["--mock", "tune", "set", "writer_temperature=0.6", "segment_gap=0.7"]) == 0
    assert tuning.current().writer_temperature == 0.6 and tuning.current().segment_gap == 0.7
    assert cli_main(["--mock", "tune", "set", "nope=1"]) == 2
    assert cli_main(["--mock", "tune", "set", "segment_gap=abc"]) == 2
    assert cli_main(["--mock", "tune", "preset", "lively"]) == 0 and tuning.current().writer_temperature == 1.0
    assert cli_main(["--mock", "tune", "show"]) == 0
    rc = cli_main(["--mock", "validate", pid, "--json"])
    out = capsys.readouterr().out
    assert rc in (0, 1) and json.loads(out[out.index("{"):])["project"] == pid


# ------------------------------------------------------------------ API
@pytest.fixture
def api(tmp_path):
    s = Settings.from_env(mock=True, data_dir=tmp_path / "d", openrouter_api_key="", bodhan_api_key="")
    with TestClient(create_app(s, env_path=tmp_path / ".env")) as c:
        c.settings, c.env = s, tmp_path / ".env"
        yield c


def _wait(c, jid):
    for _ in range(600):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in {"done", "error", "cancelled"}:
            return j
        time.sleep(0.2)
    raise AssertionError("timeout")


def test_api_save_keys_applies_immediately_and_never_leaks(api):
    api.settings.mock = False
    assert set(api.get("/api/config").json()["missing_keys"]) == {"OPENROUTER_API_KEY", "BODHAN_TTS_API_KEY"}
    r = api.post("/api/setup/keys", json={"openrouter_api_key": "or-SECRETSECRET1", "bodhan_tts_api_key": "bd-SECRETSECRET2, bd-SECRETSECRET3", "model_writer": "x/y", "mock": False})
    assert r.status_code == 200 and "SECRET" not in r.text and r.json()["missing_keys"] == []
    saved = api.env.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=or-SECRETSECRET1" in saved and "BODHAN_TTS_API_KEY=bd-SECRETSECRET2,bd-SECRETSECRET3" in saved and "LLM_MODEL_WRITER=x/y" in saved
    assert api.get("/api/config").json()["missing_keys"] == [] and "SECRET" not in api.get("/api/config").text
    assert api.get("/api/doctor").status_code == 200


def test_api_save_keys_validation(api):
    assert api.post("/api/setup/keys", json={"openrouter_api_key": "short"}).status_code == 400
    assert api.post("/api/setup/keys", json={"bodhan_tts_api_key": "tooshort"}).status_code == 400
    assert api.post("/api/setup/keys", json={}).status_code == 400
    assert not api.env.exists()


def test_api_tuning_roundtrip(api):
    t = api.get("/api/tuning").json()
    assert t["values"]["segment_gap"] == 0.55 and "calm" in t["presets"]
    assert api.put("/api/tuning", json={"values": {"segment_gap": 1.2}}).json()["values"]["segment_gap"] == 1.2
    assert api.get("/api/tuning").json()["values"]["segment_gap"] == 1.2
    assert api.put("/api/tuning", json={"values": {"nonsense": 1}}).status_code == 400
    assert api.put("/api/tuning", json={"preset": "nope"}).status_code == 400
    assert api.put("/api/tuning", json={"preset": "faithful"}).json()["values"]["writer_temperature"] == 0.5
    assert api.put("/api/tuning", json={"values": {"segment_gap": 99}}).json()["values"]["segment_gap"] == 2.5  # clamped


def test_api_validate_and_remix(api, sample_text):
    r = api.post("/api/projects", data={"text": sample_text, "settings_json": json.dumps({"minutes": 4})}).json()
    pid = r["project"]["id"]
    assert _wait(api, r["job"]["id"])["status"] == "done"
    rep = api.post(f"/api/projects/{pid}/validate").json()
    assert rep["checks"] and rep["verdict"] in {"pass", "warn", "fail"}
    deep = api.post(f"/api/projects/{pid}/validate?deep=1").json()
    assert any(c["name"].startswith("Fresh fact-check") for c in deep["checks"])
    d0 = api.get(f"/api/projects/{pid}").json()["project"]["final"]["duration"]
    api.put("/api/tuning", json={"values": {"segment_gap": 2.5, "gap_speaker_change": 1.0}})
    j = api.post(f"/api/projects/{pid}/remix").json()["job"]
    assert _wait(api, j["id"])["status"] == "done"
    assert api.get(f"/api/projects/{pid}").json()["project"]["final"]["duration"] > d0 + 5
    assert api.post("/api/projects/nope/validate").status_code == 404
