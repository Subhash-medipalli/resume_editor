import threading
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from resume_tailor import claude, llm, providers
from resume_tailor.llm import LLMError

MESSAGES = [{"role": "system", "content": "Rules"}, {"role": "user", "content": "Resume and job"}]


def message(text="answer", stop_reason="end_turn", category=None):
    return SimpleNamespace(stop_reason=stop_reason, stop_details=SimpleNamespace(category=category),
                           content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)])


def status_error(cls, code):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("provider said no", response=httpx2.Response(code, request=request), body=None)


class Stream:
    def __init__(self, final, events=("event",)):
        self.final, self.events, self.closed = final, events, threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed.set()

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self):
        return self.final


class FakeClient:
    """Stands in for anthropic.Anthropic; each stream() call takes the next outcome."""

    def __init__(self, *outcomes):
        self.outcomes, self.requests, self.options, self.created = list(outcomes), [], [], []
        self.beta = self.messages = self

    def __call__(self, **kwargs):
        self.created.append(kwargs)
        return self

    def with_options(self, **options):
        self.options.append(options)
        return self

    def stream(self, **request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome if isinstance(outcome, Stream) else Stream(outcome)


@pytest.fixture
def fake(monkeypatch):
    def install(*outcomes):
        client = FakeClient(*outcomes)
        monkeypatch.setattr(claude.anthropic, "Anthropic", client)
        return client
    monkeypatch.setattr(llm, "RETRY_BACKOFF_SECONDS", (0, 0))
    return install


def test_request_streams_with_adaptive_thinking_and_the_refusal_fallback(fake):
    client = fake(message("  the resume  "))
    assert claude.complete(MESSAGES, api_key="sk-test", temperature=0.2) == "the resume"
    assert client.created == [{"api_key": "sk-test", "max_retries": 0}]
    request = client.requests[0]
    assert request["model"] == "claude-opus-5-5" and request["max_tokens"] == 32000
    assert request["system"] == "Rules" and request["messages"] == MESSAGES[1:]
    assert request["thinking"] == {"type": "adaptive"} and request["output_config"] == {"effort": "medium"}
    assert request["fallbacks"] == "default" and request["betas"] == ["server-side-fallback-2026-07-01"]
    assert "temperature" not in request
    assert 0 < client.options[0]["timeout"] <= llm.DEFAULT_TIMEOUT


def test_haiku_has_no_fallback_and_settings_come_from_the_environment(fake, monkeypatch):
    monkeypatch.setenv("CLAUDE_THINKING_EFFORT", "high")
    monkeypatch.setenv("CLAUDE_MAX_TOKENS", "9000")
    client = fake(message())
    claude.complete(MESSAGES, api_key="k", model="claude-haiku-5-5")
    request = client.requests[0]
    assert "fallbacks" not in request and "betas" not in request
    assert request["output_config"] == {"effort": "high"} and request["max_tokens"] == 9000


@pytest.mark.parametrize("name, value", [("CLAUDE_THINKING_EFFORT", "extreme"), ("CLAUDE_MAX_TOKENS", "-1"),
                                         ("CLAUDE_MAX_TOKENS", "lots")])
def test_invalid_settings_never_reach_claude(fake, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    client = fake()
    with pytest.raises(LLMError, match=name):
        claude.complete(MESSAGES, api_key="k")
    assert client.requests == []


def test_a_refusal_is_not_used_or_retried(fake):
    client = fake(message("partial", stop_reason="refusal", category="cyber"))
    with pytest.raises(LLMError, match="declined.*cyber"):
        claude.complete(MESSAGES, api_key="k")
    assert len(client.requests) == 1


def test_a_reply_cut_off_at_the_token_limit_is_not_used(fake):
    fake(message("half a resume", stop_reason="max_tokens"))
    with pytest.raises(LLMError, match="CLAUDE_MAX_TOKENS"):
        claude.complete(MESSAGES, api_key="k")


def test_overload_and_dropped_connections_are_retried(fake):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client = fake(status_error(anthropic.APIStatusError, 529), anthropic.APIConnectionError(request=request), message("ok"))
    progress = []
    assert claude.complete(MESSAGES, api_key="k", progress=progress.append) == "ok"
    assert len(client.requests) == 3
    assert "Provider unavailable — retrying in 0 seconds" in progress


def test_a_rejected_key_fails_at_once_and_names_the_setting(fake):
    client = fake(status_error(anthropic.AuthenticationError, 401))
    with pytest.raises(LLMError, match="CLAUDE_API_KEY"):
        claude.complete(MESSAGES, api_key="bad")
    assert len(client.requests) == 1


def test_an_empty_reply_is_retried(fake):
    client = fake(message("   "), message("ok"))
    assert claude.complete(MESSAGES, api_key="k") == "ok"
    assert len(client.requests) == 2


def test_a_stream_still_running_at_the_deadline_is_closed_unread(fake):
    release, read_on = threading.Event(), []

    class Slow(Stream):
        def __iter__(self):
            yield "first"
            release.wait(5)
            yield "second"
            read_on.append("third")  # only reached if reading continues after the run gave up
            yield "third"

    stream = Slow(message())
    fake(stream)
    with pytest.raises(LLMError, match="total time limit"):
        claude.complete(MESSAGES, api_key="k", total_timeout=0.2)
    release.set()
    assert stream.closed.wait(5)
    assert read_on == []


def test_claude_is_the_default_unless_only_the_openrouter_key_is_set(monkeypatch):
    assert providers.default_provider() == "claude"
    with pytest.raises(ValueError, match="CLAUDE_API_KEY"):
        providers.settings()
    monkeypatch.setenv("LLM_API_KEY", "or-key")
    assert providers.default_provider() == "openrouter"
    assert providers.settings()["base_url"] == llm.DEFAULT_BASE_URL
    monkeypatch.setenv("CLAUDE_API_KEY", "claude-key")
    assert providers.settings() == {"provider": "claude", "api_key": "claude-key", "base_url": None,
                                    "model": "claude-opus-5-5"}


def test_the_catalog_says_which_keys_are_set_but_never_shows_them(monkeypatch):
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-5")
    catalog = providers.catalog()
    assert "sk-ant-secret" not in repr(catalog)
    claude_entry, openrouter = catalog["providers"]
    assert claude_entry["ready"] and not openrouter["ready"]
    assert claude_entry["default_model"] == "claude-opus-5"
    assert {m["id"] for m in claude_entry["models"]} == {*claude.MODELS, "claude-opus-5"}
    assert openrouter["models"] == [{"id": llm.DEFAULT_MODEL, "note": "Set by LLM_MODEL in .env."}]
