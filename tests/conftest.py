from __future__ import annotations

from pathlib import Path

import pytest

from podcast_studio.clients.bodhan import BodhanClient
from podcast_studio.clients.openrouter import OpenRouterClient
from podcast_studio.config import Settings
from podcast_studio.models import ProjectSettings
from podcast_studio.pipeline.orchestrator import Orchestrator
from podcast_studio.store import ProjectStore

from .fake_servers import BD_KEY, OR_KEY, FakeServers

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True, scope="session")
def _fast_backoff():
    OpenRouterClient.BACKOFF = 0
    BodhanClient.BACKOFF = 0


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Tests never see (or touch) a developer's real .env keys or tuning."""
    for k in ("OPENROUTER_API_KEY", "BODHAN_API_KEY", "BODHAN_TTS_API_KEY", "BODHAN_MT_API_KEY", "BODHAN_OCR_API_KEY", "MOCK_APIS",
              "LLM_MODEL", "LLM_MODEL_WRITER", "LLM_MODEL_PLANNER", "LLM_MODEL_CHECKER", "LLM_MODEL_VISION", "LLM_FALLBACK_MODELS",
              "OPENROUTER_BASE_URL", "BODHAN_BASE_URL", "DATA_DIR"):
        monkeypatch.setenv(k, "")


@pytest.fixture(autouse=True)
def _forget_bad_keys():
    from podcast_studio import keys
    keys.reset()
    yield
    keys.reset()


@pytest.fixture(scope="session")
def fake():
    with FakeServers() as f:
        yield f


@pytest.fixture
def be(fake):
    fake.backend.reset_counts()
    fake.backend.or_script.clear(); fake.backend.tts_script.clear()
    fake.backend.reject_json_mode = False
    fake.backend.bad_styles.clear()
    fake.backend.split_keys = False
    return fake.backend


@pytest.fixture
def sample_text() -> str:
    return (ROOT / "samples" / "water_cycle.txt").read_text(encoding="utf-8")


@pytest.fixture
def mock_settings(tmp_path) -> Settings:
    return Settings.from_env(mock=True, data_dir=tmp_path / "data")


@pytest.fixture
def http_settings(tmp_path, fake, be) -> Settings:
    return Settings.from_env(
        mock=False, data_dir=tmp_path / "data_http", openrouter_api_key=OR_KEY, openrouter_base_url=fake.or_url,
        bodhan_api_key=BD_KEY, bodhan_base_url=fake.bd_url, model_writer="good/model", model_planner="good/model",
        model_checker="good/model", model_vision="good/model", model_fallbacks=("good/model",), http_retries=4,
    )


def make_orch(settings: Settings) -> tuple[ProjectStore, Orchestrator]:
    store = ProjectStore(settings.data_dir)
    return store, Orchestrator(settings, store)


def noprog(frac: float, msg: str = "") -> None:
    pass


@pytest.fixture(params=["inproc", "http"])
def env(request, mock_settings, http_settings):
    """Run pipeline tests twice: with in-process mocks and through the real HTTP clients against the fakes."""
    s = mock_settings if request.param == "inproc" else http_settings
    store, orch = make_orch(s)
    return type("Env", (), {"settings": s, "store": store, "orch": orch, "kind": request.param})


def new_project(env, text: str, **kw):
    st = ProjectSettings(**{"minutes": 6, **kw})
    return env.orch.create_project(title="", settings=st, text=text)
