"""OpenAI-compatible chat completion (one call). Stdlib only."""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from resume_tailor.prompt import SYSTEM_PROMPT, build_user_prompt

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 90
NVIDIA_TIMEOUT = 300
# A two-minute generation should not be thrown away because the provider was busy.
MAX_ATTEMPTS = 3
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
RETRY_BACKOFF_SECONDS = (5, 20)


class LLMError(RuntimeError):
    """Provider HTTP or payload error."""


@dataclass(frozen=True)
class TailorResult:
    resume_markdown: str
    changelog: list[str]
    match_line: str
    match_score: int | None
    raw: str
    # True when the model gave no SCORE line and the number was inferred from
    # the good/partial/poor wording rather than stated by the model.
    score_inferred: bool = False


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
    cleaned = re.sub(r'</?think>', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def complete(
    messages: Sequence[dict[str, str]],
    *,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    temperature: float | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    nvidia = _is_nvidia(base_url, model)
    if nvidia and timeout == DEFAULT_TIMEOUT:
        timeout = float(os.environ.get("OPENAI_TIMEOUT", NVIDIA_TIMEOUT))
    body: dict = {
        "model": model,
        "messages": list(messages),
    }
    # Newer models (gpt-5, o-series) reject custom temperature.
    if temperature is not None:
        body["temperature"] = temperature
    elif nvidia:
        body["temperature"] = 1
        body["top_p"] = 0.95
    max_tokens = os.environ.get("OPENAI_MAX_TOKENS", "").strip()
    if max_tokens:
        body["max_tokens"] = int(max_tokens)
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
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=_ssl_context()
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            last_error = LLMError(f"LLM request failed (HTTP {exc.code}): {detail}")
            if exc.code not in RETRY_STATUS:
                raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = LLMError(f"LLM request failed: {exc.reason}")
        except (TimeoutError, OSError) as exc:
            last_error = LLMError(f"LLM request failed: {exc}")
        except json.JSONDecodeError as exc:
            last_error = LLMError(f"LLM returned a non-JSON response: {exc}")

        if attempt == MAX_ATTEMPTS - 1:
            raise last_error
        time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])

    try:
        choice = body["choices"][0]
        message = choice["message"]
        content = message.get("content")
        if not (isinstance(content, str) and content.strip()):
            # Reasoning text is not an answer; using it would paste the model's
            # private chain of thought into the resume.
            raise LLMError(
                "LLM returned no answer content (only reasoning). "
                "Try again, or set NVIDIA_ENABLE_THINKING=0."
            )
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Unexpected LLM response shape: {body!r}") from exc
    if not content.strip():
        raise LLMError("LLM returned an empty message.")

    # A resume cut off at the token ceiling still parses as a valid document,
    # so the only place to catch it is here.
    if choice.get("finish_reason") == "length":
        raise LLMError(
            "The model hit its output limit and the resume is incomplete. "
            "Raise OPENAI_MAX_TOKENS (currently "
            f"{os.environ.get('OPENAI_MAX_TOKENS', 'unset')}) and try again."
        )
    return _strip_think(content)


def tailor_resume(
    *,
    resume_markdown: str,
    job_description: str,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    temperature: float | None = None,
    complete_fn=complete,
) -> TailorResult:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_user_prompt(
                resume_markdown=resume_markdown,
                job_description=job_description,
            ),
        },
    ]
    raw = complete_fn(
        messages,
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
    )
    return parse_model_output(raw)


def _parse_match(chunk: str) -> tuple[int | None, str, bool]:
    """Return (score, match_line, inferred).

    `inferred` is True when the model gave no SCORE line and the number was
    guessed from the good/partial/poor wording — the caller must not present
    such a number as the model's own judgement.
    """
    score: int | None = None
    rest: list[str] = []
    # Tolerates "SCORE: 88", "**SCORE:** 88", "- Score = 88", "SCORE: 88/100".
    pattern = re.compile(
        r"^[\s\-*_#>]*\**\s*score\s*\**\s*[:=]\s*\**\s*(\d{1,3})",
        re.IGNORECASE,
    )
    for raw in chunk.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = pattern.match(line)
        if match:
            score = max(0, min(100, int(match.group(1))))
            continue
        rest.append(line)
    match_line = " ".join(rest)
    inferred = False
    if score is None:
        low = match_line.lower()
        for prefix, value in (("good", 85), ("partial", 62), ("poor", 38)):
            if low.startswith(prefix):
                score, inferred = value, True
                break
    return score, match_line, inferred


def parse_model_output(text: str) -> TailorResult:
    raw = _strip_wrapping_fence(text).strip()
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
    match_score, match_line, score_inferred = _parse_match(match_chunk)
    if not match_line:
        raise LLMError("Model output had an empty MATCH section.")
    resume_markdown = resume_chunk.strip()
    if not resume_markdown:
        raise LLMError("Model output had an empty RESUME section.")
    return TailorResult(
        resume_markdown=resume_markdown + "\n",
        changelog=changelog,
        match_line=match_line,
        match_score=match_score,
        raw=text,
        score_inferred=score_inferred,
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
