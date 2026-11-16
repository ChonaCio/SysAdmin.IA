"""Per-user appearance and language preferences (Edit profile → Appearance & language).

The browser applies them (static/prefs.js); they are stored here so they follow the user to other
devices. The language also tells the chat assistant which language to answer in.
"""
from __future__ import annotations

import json

from . import db

LANGS = {"en": "English", "es": "Spanish"}       # keep in sync with LANGS in static/prefs.js
DEFAULTS = {"theme": "system", "accent": "orange", "text": "100", "contrast": False, "motion": True, "lang": "en"}
CHOICES = {
    "theme": ("system", "light", "dark", "black"),
    "accent": ("orange", "blue", "green", "purple", "pink", "teal", "graphite"),
    "text": ("90", "100", "112", "125"),
    "lang": tuple(LANGS),
}


def clean(p: dict | None) -> dict:
    """Known keys with valid values only; anything else falls back to the default."""
    out = dict(DEFAULTS)
    for k, default in DEFAULTS.items():
        v = (p or {}).get(k)
        if v is None:
            continue
        if k in CHOICES:
            if str(v) in CHOICES[k]:
                out[k] = str(v)
        elif isinstance(v, bool):
            out[k] = v
    return out


def get(user_id: int) -> dict:
    """The user's saved preferences, or {} if they never saved any (the browser's then apply)."""
    row = db.query_one("SELECT prefs FROM users WHERE id=?", (user_id,))
    if not row or not row["prefs"]:
        return {}
    try:
        return clean(json.loads(row["prefs"]))
    except (ValueError, TypeError):
        return {}


def save(user_id: int, p: dict) -> dict:
    p = clean(p)
    db.execute("UPDATE users SET prefs=? WHERE id=?", (json.dumps(p), user_id))
    return p


def language(user_id: int) -> str:
    return get(user_id).get("lang", "en")


def chat_language_note(lang: str) -> str:
    """Appended to the assistant's system prompt (one fixed text per language, so prompt caching still works)."""
    if lang == "en" or lang not in LANGS:
        return ""
    name = LANGS[lang]
    return (f"\n\n# Language\nThe user's interface is in {name}. Write your replies in {name}, unless the user "
            f"writes to you in another language. Keep commands, file paths, code and log output as they are.")
