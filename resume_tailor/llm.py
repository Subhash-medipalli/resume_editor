"""One Chat Completions call, in the format OpenRouter, NVIDIA, and others share. Stdlib only."""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import time
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from resume_tailor.prompt import SYSTEM_PROMPT, build_user_prompt
from resume_tailor.guardrails import _changed_content_lines
from resume_tailor.structure import from_markdown

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "google/gemini-3.8-flash"
# Seconds per attempt. Replies are not streamed, so a full resume arrives all at once.
DEFAULT_TIMEOUT = 300
# A two-minute generation should not be thrown away because the provider was busy.
MAX_ATTEMPTS = 3
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
RETRY_BACKOFF_SECONDS = (5, 20)
DEFAULT_TOTAL_TIMEOUT = 360


class LLMError(RuntimeError):
    """Provider HTTP or payload error."""


@dataclass(frozen=True)
class TailorResult:
    resume_markdown: str
    changelog: list[str]
    match_line: str
    match_score: int | None
    raw: str


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overwriting existing vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            os.environ.setdefault(key, value)


def _ssl_context() -> ssl.SSLContext:
    """Use certifi CAs when present (macOS framework Python often has an empty store)."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    ca = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", ca)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", ca)
    return ssl.create_default_context(cafile=ca)


def _is_nvidia(base_url: str, model: str) -> bool:
    host = (base_url or '').lower()
    return 'nvidia.com' in host or model.startswith('nvidia/')


def _strip_think(text: str) -> str:
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    if re.search(r'</?think>', cleaned, flags=re.IGNORECASE):
        raise LLMError("The model returned an unfinished reasoning block instead of a complete answer.")
    return cleaned.strip()


def _positive_seconds(value) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise LLMError("Provider timeouts must be finite, positive seconds.") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise LLMError("Provider timeouts must be finite, positive seconds.")
    return seconds


def has_resume_changes(original: str, tailored: str) -> bool:
    """Ignore Markdown decoration and typography when detecting an echoed resume."""
    before = "\n".join(block.text for block in from_markdown(original))
    after = "\n".join(block.text for block in from_markdown(tailored))
    return bool(_changed_content_lines(before, after))


def complete(
    messages: Sequence[dict[str, str]],
    *,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    temperature: float | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    total_timeout: float | None = None,
    progress=None,
) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    nvidia = _is_nvidia(base_url, model)
    if timeout == DEFAULT_TIMEOUT:
        timeout = os.environ.get("LLM_TIMEOUT", DEFAULT_TIMEOUT)
    timeout = _positive_seconds(timeout)
    total_timeout = _positive_seconds(os.environ.get("LLM_TOTAL_TIMEOUT", DEFAULT_TOTAL_TIMEOUT) if total_timeout is None else total_timeout)
    deadline = time.monotonic() + total_timeout
    progress = progress or (lambda message: None)
    body: dict = {
        "model": model,
        "messages": list(messages),
    }
    # Many hosted models reject a custom temperature; only NVIDIA gets one.
    if temperature is not None:
        body["temperature"] = temperature
    elif nvidia:
        body["temperature"] = 1
        body["top_p"] = 0.95
    max_tokens = os.environ.get("LLM_MAX_TOKENS", "").strip()
    if max_tokens:
        try:
            body["max_tokens"] = int(max_tokens)
        except ValueError as exc:
            raise LLMError("LLM_MAX_TOKENS must be a positive integer.") from exc
        if body["max_tokens"] <= 0:
            raise LLMError("LLM_MAX_TOKENS must be a positive integer.")
    elif nvidia:
        body["max_tokens"] = 16384
    if nvidia:
        think = os.environ.get("NVIDIA_ENABLE_THINKING", "1").strip().lower()
        body["chat_template_kwargs"] = {
            "enable_thinking": think not in {"0", "false", "no", "off"}
        }
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LLMError("The model exceeded the total time limit. Run again when the provider is available.")
        progress(f"Waiting for model reply — attempt {attempt + 1} of {MAX_ATTEMPTS}")
        try:
            body = _request_with_deadline(request, min(timeout, remaining), remaining)
            break
        except urllib.error.HTTPError as exc:
            detail = getattr(exc, "provider_detail", exc.reason)
            last_error = LLMError(f"LLM request failed (HTTP {exc.code}): {detail}")
            if exc.code not in RETRY_STATUS:
                raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = LLMError(f"LLM request failed: {exc.reason}")
        except (TimeoutError, OSError) as exc:
            last_error = LLMError(f"LLM request failed: {exc}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_error = LLMError(f"LLM returned a non-JSON response: {exc}")

        if attempt == MAX_ATTEMPTS - 1:
            raise last_error
        delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
        remaining = deadline - time.monotonic()
        if delay >= remaining:
            raise LLMError("The model exceeded the total time limit. No more retries were started.") from last_error
        progress(f"Provider unavailable — retrying in {delay:g} seconds")
        time.sleep(delay)

    try:
        choice = body["choices"][0]
        message = choice["message"]
        content = message.get("content")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMError("Unexpected LLM response shape: an answer message was missing.") from exc
    # A resume cut off at the token ceiling still parses as a valid document,
    # so the only place to catch it is here.
    if choice.get("finish_reason") == "length":
        raise LLMError(
            "The model hit its output limit and the resume is incomplete. "
            "Raise LLM_MAX_TOKENS (currently "
            f"{os.environ.get('LLM_MAX_TOKENS', 'unset')}) and try again."
        )
    if choice.get("finish_reason") in {"content_filter", "tool_calls", "function_call"}:
        raise LLMError("The provider did not return a complete resume answer.")
    if not isinstance(content, str) or not content.strip():
        raise LLMError(
            "LLM returned no answer content (only reasoning or an empty message). "
            "Try again, or set NVIDIA_ENABLE_THINKING=0."
        )
    answer = _strip_think(content)
    if not answer:
        raise LLMError("LLM returned only reasoning and no resume answer.")
    return answer


def _request_with_deadline(request, timeout: float, remaining: float):
    # urllib's socket timeout is per operation. A daemon thread bounds the whole
    # wait, including slow streaming responses; late replies are discarded.
    # ponytail: a timed-out provider may finish remotely. Add provider-supported
    # cancellation if the API offers it; never publish a late result.
    future = Future()
    def request_json():
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
                data = json.loads(response.read().decode("utf-8"))
            future.set_result(data)
        except urllib.error.HTTPError as exc:
            # Read error bodies inside the same deadline as successful replies.
            try:
                exc.provider_detail = exc.read(2000).decode("utf-8", errors="replace")
            except Exception as read_error:
                future.set_exception(read_error)
                return
            finally:
                exc.close()
            future.set_exception(exc)
        except Exception as exc:
            future.set_exception(exc)
    threading.Thread(target=request_json, daemon=True).start()
    try:
        return future.result(timeout=remaining)
    except FutureTimeout as exc:
        if future.done():
            raise  # a socket timeout from the request is eligible for retry
        raise LLMError("The model exceeded the total time limit. Its late reply will not be used.") from exc


def tailor_resume(
    *,
    resume_markdown: str,
    job_description: str,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    temperature: float | None = None,
    complete_fn=complete,
    progress=None,
    missing_keywords: list[str] | tuple[str, ...] = (),
    polish: bool = False,
) -> TailorResult:
    """One tailoring answer; a polish pass may leave an already-tailored resume as is."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_user_prompt(
                resume_markdown=resume_markdown,
                job_description=job_description,
                missing_keywords=missing_keywords,
                polish=polish,
            ),
        },
    ]
    deadline = time.monotonic() + _positive_seconds(
        os.environ.get("LLM_TOTAL_TIMEOUT", DEFAULT_TOTAL_TIMEOUT)
    )
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LLMError("The model exceeded the total time limit before a usable resume was returned.")
        raw = complete_fn(
            messages,
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
            progress=progress,
            total_timeout=remaining,
        )
        try:
            result = parse_model_output(raw)
            if not polish and not has_resume_changes(resume_markdown, result.resume_markdown):
                raise LLMError("The model returned the original resume without any content changes.")
            return result
        except LLMError as exc:
            if attempt:
                raise LLMError(f"The model did not produce a usable tailored resume after one corrective retry: {exc}") from exc
            if progress:
                progress("Requesting a corrected response because the model returned unchanged or invalid output")
            # Reuse the original request. Do not echo a malformed response or
            # possible reasoning text back into the conversation.
            messages = [*messages, {"role": "user", "content": (
                f"The previous response failed validation: {exc} "
                "Return the complete resume in the required CHANGELOG, MATCH, and RESUME sections. "
                "Make substantive job-description-specific changes to the editable summary, skills, "
                "and relevant experience. Preserve the protected identity and history fields."
            )}]


def _parse_match(chunk: str) -> tuple[int | None, str]:
    """Keep absent, malformed, or out-of-range scores unknown."""
    score: int | None = None
    rest: list[str] = []
    # Tolerates "SCORE: 88", "**SCORE:** 88", "- Score = 88", "SCORE: 88/100".
    pattern = re.compile(
        r"^[\s\-*_#>]*\**\s*score\s*\**\s*[:=]\s*\**\s*(\d{1,3})(?:\s*/\s*100)?[\s*]*$",
        re.IGNORECASE,
    )
    for raw in chunk.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = pattern.match(line)
        if match:
            value = int(match.group(1))
            score = value if 0 <= value <= 100 else None
            continue
        rest.append(line)
    match_line = " ".join(rest)
    return score, match_line


def parse_model_output(text: str) -> TailorResult:
    if not isinstance(text, str) or not text.strip():
        raise LLMError("Model output was empty or was not text.")
    raw = _strip_wrapping_fence(_strip_think(text)).strip()
    changelog_chunk = _require_section(raw, "CHANGELOG", "MATCH")
    match_chunk = _require_section(raw, "MATCH", "RESUME")
    resume_chunk = _require_open_section(raw, "RESUME")

    changelog = [
        line[1:].strip() if line.startswith("-") else line.strip()
        for line in changelog_chunk.splitlines()
        if line.strip() and line.strip() != "-"
    ]
    if not changelog:
        raise LLMError("Model output had an empty CHANGELOG section.")
    match_score, match_line = _parse_match(match_chunk)
    if not match_line:
        raise LLMError("Model output had an empty MATCH section.")
    resume_markdown = _strip_wrapping_fence(resume_chunk).strip()
    if not resume_markdown:
        raise LLMError("Model output had an empty RESUME section.")
    return TailorResult(
        resume_markdown=resume_markdown + "\n",
        changelog=changelog,
        match_line=match_line,
        match_score=match_score,
        raw=text,
    )


def _strip_wrapping_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _require_section(text: str, name: str, next_name: str) -> str:
    start = _find_marker(text, name)
    end = _find_marker(text, next_name)
    if start is None:
        raise LLMError(f"Model output missing ==={name}=== section.")
    if end is None or end <= start:
        raise LLMError(f"Model output missing ==={next_name}=== after ==={name}===")
    marker = f"==={name}==="
    start_at = text.find(marker, start) + len(marker)
    return text[start_at:end].strip()


def _require_open_section(text: str, name: str) -> str:
    start = _find_marker(text, name)
    if start is None:
        raise LLMError(f"Model output missing ==={name}=== section.")
    marker = f"==={name}==="
    start_at = text.find(marker, start) + len(marker)
    return text[start_at:].strip()


def _find_marker(text: str, name: str) -> int | None:
    marker = f"==={name}==="
    idx = text.find(marker)
    return None if idx < 0 else idx
