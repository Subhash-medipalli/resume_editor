"""One Claude call through Anthropic's SDK: streamed, adaptive thinking, refusal fallback."""

from __future__ import annotations

import os
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout

import anthropic

from resume_tailor.llm import DEFAULT_TIMEOUT, RETRY_STATUS, LLMError, _EmptyReply, _Transient, with_retries

DEFAULT_MODEL = "claude-opus-5-5"
# The page's model menu. Costs are rough estimates for one resume (see README).
MODELS = {
    "claude-opus-5-5": "Recommended: follows the keep-every-fact rules most reliably. Moderate speed, about 15¢ a resume.",
    "claude-sonnet-5-5": "Faster, about 7¢ a resume.",
    "claude-haiku-5-5": "Fastest and cheapest, under 1¢ a resume. Check its changes more closely.",
    "claude-fable-5-1": "Most capable, but slow, about 35¢ a resume.",
}
# On these, a request Anthropic's safety checks decline is re-run on its recommended backup model.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MAX_TOKENS = 32000


def complete(
    messages,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    base_url: str | None = None,  # unused: same signature as llm.complete
    temperature: float | None = None,  # unused: current Claude models reject a temperature
    timeout: float = DEFAULT_TIMEOUT,
    total_timeout: float | None = None,
    progress=None,
) -> str:
    effort = os.environ.get("CLAUDE_THINKING_EFFORT", "").strip().lower() or "medium"
    if effort not in EFFORTS:
        raise LLMError(f"CLAUDE_THINKING_EFFORT must be one of {', '.join(EFFORTS)}.")
    try:
        max_tokens = int(os.environ.get("CLAUDE_MAX_TOKENS", "").strip() or DEFAULT_MAX_TOKENS)
    except ValueError:
        max_tokens = 0
    if max_tokens <= 0:
        raise LLMError("CLAUDE_MAX_TOKENS must be a positive integer.")
    request = {
        "model": model,
        "max_tokens": max_tokens,
        "system": "\n\n".join(m["content"] for m in messages if m["role"] == "system"),
        "messages": [m for m in messages if m["role"] != "system"],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }
    if model in FALLBACK_MODELS:
        request.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    # with_retries owns retries and the shared deadline, so the SDK must not retry on its own.
    client = anthropic.Anthropic(api_key=api_key, max_retries=0)
    return with_retries(lambda wait, remaining: _answer(_stream(client, request, wait, remaining), max_tokens),
                        model=model, timeout=timeout, total_timeout=total_timeout, progress=progress)


def _stream(client, request: dict, wait: float, remaining: float):
    """The final message, or an error once `remaining` seconds pass; a late stream is closed unread."""
    future, gave_up = Future(), threading.Event()

    def read():
        try:
            with client.with_options(timeout=wait).beta.messages.stream(**request) as stream:
                for _ in stream:
                    if gave_up.is_set():
                        return  # leaving the block closes the stream
                future.set_result(stream.get_final_message())
        except Exception as exc:
            future.set_exception(exc)

    threading.Thread(target=read, daemon=True).start()
    try:
        return future.result(timeout=remaining)
    except FutureTimeout as exc:
        if future.done():
            raise  # a socket timeout from the request is eligible for retry
        gave_up.set()
        raise LLMError("The model exceeded the total time limit. Its late reply will not be used.") from exc
    except anthropic.AuthenticationError as exc:
        raise LLMError("Claude rejected the API key (HTTP 401). Check CLAUDE_API_KEY in .env.") from exc
    except anthropic.APIStatusError as exc:
        error = f"Claude request failed (HTTP {exc.status_code}): {exc.message}"
        if exc.status_code in RETRY_STATUS or exc.status_code >= 500:  # 529 means overloaded
            raise _Transient(error) from exc
        raise LLMError(error) from exc
    except anthropic.APIConnectionError as exc:
        raise _Transient(f"Claude request failed: {exc}") from exc


def _answer(message, max_tokens: int) -> str:
    # A refusal arrives as a normal reply; any partial text must never be parsed as a resume.
    if message.stop_reason == "refusal":
        category = getattr(getattr(message, "stop_details", None), "category", None)
        raise LLMError(f"Claude declined this request (safety category: {category or 'not given'}). "
                       "Run again, or choose another model.")
    if message.stop_reason == "max_tokens":
        raise LLMError("Claude hit its output limit and the resume is incomplete. "
                       f"Raise CLAUDE_MAX_TOKENS (currently {max_tokens}) and try again.")
    # After a mid-answer fallback the text blocks continue one another, so they are joined.
    answer = "".join(block.text for block in message.content if block.type == "text").strip()
    if not answer:
        raise _EmptyReply(f"an empty reply (stop reason {message.stop_reason})")
    return answer
