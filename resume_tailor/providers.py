"""Which service writes the resume: Claude (main) or a Chat Completions service such as OpenRouter."""

from __future__ import annotations

import os

from resume_tailor import claude, llm

PROVIDERS = {"claude": "Claude", "openrouter": "OpenRouter"}
KEYS = {"claude": "CLAUDE_API_KEY", "openrouter": "LLM_API_KEY"}


def _key(provider: str) -> str:
    return os.environ.get(KEYS[provider], "").strip()


def default_provider() -> str:
    """Claude, unless only the OpenRouter key is set."""
    return "openrouter" if _key("openrouter") and not _key("claude") else "claude"


def models(provider: str) -> tuple[str, dict[str, str]]:
    """The provider's default model and its menu of {model: one-line note}."""
    if provider == "claude":
        menu = dict(claude.MODELS)
        default = os.environ.get("CLAUDE_MODEL", "").strip() or claude.DEFAULT_MODEL
        menu.setdefault(default, "Set by CLAUDE_MODEL in .env.")
    else:
        default = os.environ.get("LLM_MODEL", "").strip() or llm.DEFAULT_MODEL
        menu = {default: "Set by LLM_MODEL in .env."}
    return default, menu


def settings(provider: str | None = None, model: str | None = None) -> dict:
    """Key, URL, and model for one run. ValueError when the provider is unknown or its key is missing."""
    provider = provider or default_provider()
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r}; use claude or openrouter.")
    if not _key(provider):
        raise ValueError(f"{KEYS[provider]} is missing. Put it in .env in the project folder (see .env.example).")
    base_url = None if provider == "claude" else os.environ.get("LLM_BASE_URL", "").strip() or llm.DEFAULT_BASE_URL
    return {"provider": provider, "api_key": _key(provider), "base_url": base_url,
            "model": model or models(provider)[0]}


def catalog() -> dict:
    """What the page's model panel shows. Says whether each key is set, never what it is."""
    items = []
    for provider, label in PROVIDERS.items():
        default, menu = models(provider)
        items.append({"id": provider, "label": label, "key": KEYS[provider], "ready": bool(_key(provider)),
                      "default_model": default, "models": [{"id": m, "note": n} for m, n in menu.items()]})
    return {"default": default_provider(), "providers": items}
