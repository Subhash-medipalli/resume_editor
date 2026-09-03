import pytest

from tests.helpers import SAMPLE_JD, SAMPLE_RESUME


@pytest.fixture
def sample_resume() -> str:
    return SAMPLE_RESUME


@pytest.fixture
def sample_jd() -> str:
    return SAMPLE_JD
