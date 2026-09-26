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


def test_tailor_resume_sends_match_first_rules_and_missing_keywords():
    def fake_complete(messages, **kwargs):
        assert "MATCH-FIRST RULES" in messages[0]["content"]
        assert "does not mention yet" in messages[1]["content"]
        assert "Kafka, Snowflake" in messages[1]["content"]
        return pack_model_output(
            changelog=["Added streaming skills"],
            match="SCORE: 50\npartial: test response",
            resume="# Test\n\nPython developer",
        )

    result = llm.tailor_resume(
        resume_markdown="# Test",
        job_description="Python, Kafka, Snowflake",
        api_key="fake",
        complete_fn=fake_complete,
        missing_keywords=["Kafka", "Snowflake"],
    )
    assert result.match_score == 50


@pytest.mark.parametrize("initial", ["unchanged", "decoration", "invalid"])
def test_tailoring_repairs_invalid_or_unchanged_output_once(initial):
    from tests.helpers import SAMPLE_RESUME, lightly_tailored

    original_reply = pack_model_output(changelog=["No changes"], match="poor: unchanged", resume=SAMPLE_RESUME)
    if initial == "decoration":
        original_reply = original_reply.replace("Python", "**Python**")
    replies = ["invalid response" if initial == "invalid" else original_reply,
               pack_model_output(changelog=["Updated summary"], match="good: aligned", resume=lightly_tailored(SAMPLE_RESUME))]
    calls, budgets = [], []
    def fake_complete(messages, **kwargs):
        calls.append(messages)
        budgets.append(kwargs["total_timeout"])
        return replies[len(calls) - 1]

    result = llm.tailor_resume(resume_markdown=SAMPLE_RESUME, job_description="Python APIs",
                               api_key="fake", complete_fn=fake_complete)
    assert result.resume_markdown == lightly_tailored(SAMPLE_RESUME)
    assert len(calls) == 2
    assert "failed validation" in calls[1][-1]["content"]
    assert 0 < budgets[1] <= budgets[0]


def test_repeated_unchanged_output_is_a_failure():
    calls = []
    def echo(*args, **kwargs):
        calls.append(1)
        return pack_model_output(changelog=["No changes"], match="good: already aligned", resume="# Test\nPython")
    with pytest.raises(llm.LLMError, match="after one corrective retry.*without any content changes"):
        llm.tailor_resume(resume_markdown="# Test\nPython", job_description="Python", api_key="fake", complete_fn=echo)
    assert len(calls) == 2


@pytest.mark.parametrize("content", ["<think>unfinished", "<think>reasoning only</think>", "reasoning</think>answer"])
def test_reasoning_only_or_unbalanced_output_is_never_an_answer(monkeypatch, content):
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: response(content))
    with pytest.raises(llm.LLMError, match="reasoning"):
        llm.complete([], api_key="fake")


@pytest.mark.parametrize("setting,value", [("OPENAI_TIMEOUT", "oops"), ("OPENAI_TOTAL_TIMEOUT", "nan"),
                                          ("OPENAI_MAX_TOKENS", "-1"), ("OPENAI_MAX_TOKENS", "many")])
def test_invalid_provider_configuration_fails_before_request(monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: pytest.fail("invalid settings must not reach provider"))
    with pytest.raises(llm.LLMError):
        llm.complete([], api_key="fake")
