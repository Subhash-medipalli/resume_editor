import pytest

from tests.helpers import SAMPLE_JD, SAMPLE_RESUME


@pytest.fixture(autouse=True)
def _no_real_provider_settings(monkeypatch):
    """Tests never read the developer's .env or provider environment."""
    monkeypatch.setattr("resume_tailor.cli.load_dotenv", lambda *_: None)
    monkeypatch.setattr("resume_tailor.server.load_dotenv", lambda *_: None)
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_MAX_TOKENS",
                "OPENAI_TIMEOUT", "OPENAI_TOTAL_TIMEOUT", "NVIDIA_ENABLE_THINKING"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def sample_resume() -> str:
    return SAMPLE_RESUME


@pytest.fixture
def sample_jd() -> str:
    return SAMPLE_JD
