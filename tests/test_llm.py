import io
import json
import threading
import time
import urllib.error
from contextlib import closing

import pytest

from resume_tailor import llm
from tests.helpers import pack_model_output


def response(content="answer", **choice):
    return closing(io.BytesIO(json.dumps({"choices": [{"message": {"content": content}, **choice}]}).encode()))


@pytest.mark.parametrize("match,expected", [("good: matches", None), ("SCORE: 101\ngood: matches", None), ("SCORE: 88/100\ngood: matches", 88), ("SCORE: 88.5\ngood: matches", None)])
def test_scores_are_never_inferred_or_clamped(match, expected):
    result = llm.parse_model_output(pack_model_output(changelog=["No changes"], match=match, resume="# Test"))
    assert result.match_score == expected


def test_retry_progress_and_recovery(monkeypatch):
    attempts, progress = [], []
    def request(*args, **kwargs):
        attempts.append(kwargs["timeout"])
        if len(attempts) == 1:
            raise urllib.error.HTTPError("https://example.invalid", 503, "busy", {}, io.BytesIO(b"busy"))
        return response()
    monkeypatch.setattr(llm.urllib.request, "urlopen", request)
    monkeypatch.setattr(llm, "RETRY_BACKOFF_SECONDS", (0, 0))
    assert llm.complete([], api_key="fake", progress=progress.append) == "answer"
    assert len(attempts) == 2
    assert any("retrying" in text for text in progress)
    assert any("attempt 2" in text for text in progress)


def test_nonretryable_auth_failure_stops_immediately(monkeypatch):
    calls = []
    def request(*args, **kwargs):
        calls.append(1)
        raise urllib.error.HTTPError("https://example.invalid", 401, "unauthorized", {}, io.BytesIO(b"invalid key"))
    monkeypatch.setattr(llm.urllib.request, "urlopen", request)
    with pytest.raises(llm.LLMError, match="HTTP 401"):
        llm.complete([], api_key="fake")
    assert len(calls) == 1


def test_total_deadline_discards_late_response(monkeypatch):
    released, finished = threading.Event(), threading.Event()
    def request(*args, **kwargs):
        assert released.wait(3)
        finished.set()
        return response("late reply")
    monkeypatch.setattr(llm.urllib.request, "urlopen", request)
    start = time.monotonic()
    try:
        with pytest.raises(llm.LLMError, match="total time limit"):
            llm.complete([], api_key="fake", total_timeout=0.05)
        assert time.monotonic() - start < 1
    finally:
        released.set()
        assert finished.wait(3)


def test_retry_backoff_cannot_exceed_total_budget(monkeypatch):
    calls = []
    def request(*args, **kwargs):
        calls.append(1)
        raise urllib.error.URLError("unavailable")
    monkeypatch.setattr(llm.urllib.request, "urlopen", request)
    with pytest.raises(llm.LLMError, match="time limit"):
        llm.complete([], api_key="fake", total_timeout=0.1)
    assert len(calls) == 1


@pytest.mark.parametrize("data", [b"not json", b'[]', b'{"choices": []}', b'{"choices":[{"message": []}]}'])
def test_malformed_provider_payload_has_clear_failure(monkeypatch, data):
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: closing(io.BytesIO(data)))
    monkeypatch.setattr(llm, "RETRY_BACKOFF_SECONDS", (0, 0))
    with pytest.raises(llm.LLMError):
        llm.complete([], api_key="fake")


def test_truncated_output_is_not_accepted(monkeypatch):
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: response("half a resume", finish_reason="length"))
    with pytest.raises(llm.LLMError, match="incomplete"):
        llm.complete([], api_key="fake")
