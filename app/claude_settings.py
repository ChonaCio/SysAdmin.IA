"""Claude connection settings that can be changed at runtime from the web UI or `sysadmin-ia`.

Values saved through the UI (stored in the SQLite database) take precedence over the
config file / environment (ANTHROPIC_API_KEY, SAI_MODEL, SAI_EFFORT). This lets the app
be installed without an API key and configured later.
"""
from __future__ import annotations

import os

from . import db
from .config import settings

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
SUGGESTED_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2")

_KEY = "anthropic_api_key"
_MODEL = "model"
_EFFORT = "effort"


def api_key() -> tuple[str | None, str | None]:
    """Return (key, source) where source is 'web', 'config' or None."""
    stored = db.get_setting(_KEY)
    if stored:
        return stored, "web"
    env = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if env:
        return env, "config"
    return None, None


def model() -> str:
    return db.get_setting(_MODEL) or settings.model


def effort() -> str:
    value = db.get_setting(_EFFORT) or settings.effort
    return value if value in EFFORT_LEVELS else "high"


def set_api_key(key: str | None) -> None:
    key = (key or "").strip() or None
    if key is not None and (len(key) < 20 or any(c.isspace() for c in key)):
        raise ValueError("That doesn't look like a valid API key.")
    db.set_setting(_KEY, key)


def set_model(name: str | None) -> None:
    name = (name or "").strip() or None
    if name is not None and (len(name) > 100 or any(c.isspace() for c in name)):
        raise ValueError("Invalid model name.")
    db.set_setting(_MODEL, name)


def set_effort(level: str | None) -> None:
    level = (level or "").strip() or None
    if level is not None and level not in EFFORT_LEVELS:
        raise ValueError(f"Effort must be one of {', '.join(EFFORT_LEVELS)}.")
    db.set_setting(_EFFORT, level)


def key_hint(key: str | None) -> str | None:
    if not key:
        return None
    return f"{key[:7]}…{key[-4:]}" if len(key) > 14 else "…"


def summary() -> dict:
    key, source = api_key()
    return {
        "api_key_set": bool(key),
        "api_key_source": source,
        "api_key_hint": key_hint(key),
        "model": model(),
        "effort": effort(),
        "effort_levels": list(EFFORT_LEVELS),
        "suggested_models": list(SUGGESTED_MODELS),
    }
