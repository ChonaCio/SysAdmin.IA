"""Global variables: named values that jobs (and other modules) can use.

In any job text field, write {{var.NAME}} to insert a variable. Built-in values are also
available: {{sys.hostname}}, {{sys.date}}, {{sys.time}}, {{sys.datetime}}, {{sys.ip}},
{{sys.os}}, {{sys.version}}, {{sys.url}}, plus whatever the job's trigger and earlier actions
provide ({{trigger.summary}}, {{trigger.value}}, …).

Secret variables (passwords, tokens) can be used the same way but are never shown again in
the UI, and their values are masked in job logs.
"""
from __future__ import annotations

import re
import socket
import time

from . import db

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*\}\}")


class VariableError(ValueError):
    pass


def list_all(include_secret_values: bool = False) -> list[dict]:
    rows = db.query("SELECT * FROM variables ORDER BY name COLLATE NOCASE")
    out = []
    for r in rows:
        d = dict(r)
        d["secret"] = bool(d["secret"])
        if d["secret"] and not include_secret_values:
            d["value"] = ""
            d["value_set"] = bool(r["value"])
        out.append(d)
    return out


def get(name: str) -> str | None:
    r = db.query_one("SELECT value FROM variables WHERE name=?", (name,))
    return r["value"] if r else None


def save(name: str, value: str | None, description: str = "", secret: bool = False,
         original_name: str | None = None) -> dict:
    """Create or update. For a secret, an empty value keeps the saved one."""
    name = (name or "").strip()
    if not NAME_RE.fullmatch(name):
        raise VariableError("Names start with a letter and use only letters, digits and _ (e.g. alert_email).")
    if len(value or "") > 20000:
        raise VariableError("The value is too long (max 20,000 characters).")
    original_name = original_name or name
    existing = db.query_one("SELECT * FROM variables WHERE name=?", (original_name,))
    if name != original_name and db.query_one("SELECT 1 FROM variables WHERE name=?", (name,)):
        raise VariableError(f"A variable called {name} already exists.")
    if not existing and db.query_one("SELECT 1 FROM variables WHERE name=?", (name,)):
        raise VariableError(f"A variable called {name} already exists.")
    if secret and not value and existing:
        value = existing["value"]
    with db.tx() as c:
        if existing:
            c.execute("UPDATE variables SET name=?, value=?, description=?, secret=?, updated_at=? WHERE name=?",
                      (name, value or "", (description or "").strip()[:300], int(secret), time.time(), original_name))
        else:
            c.execute("INSERT INTO variables (name, value, description, secret, updated_at) VALUES (?,?,?,?,?)",
                      (name, value or "", (description or "").strip()[:300], int(secret), time.time()))
    return next(v for v in list_all() if v["name"] == name)


def set_value(name: str, value: str) -> None:
    """Used by the "Set a variable" job action (creates the variable if needed)."""
    if not NAME_RE.fullmatch(name or ""):
        raise VariableError(f"Invalid variable name: {name}")
    db.execute("INSERT INTO variables (name, value, description, secret, updated_at) VALUES (?,?,'',0,?) "
               "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
               (name, str(value)[:20000], time.time()))


def delete(name: str) -> None:
    db.execute("DELETE FROM variables WHERE name=?", (name,))


def secret_values() -> list[str]:
    return [r["value"] for r in db.query("SELECT value FROM variables WHERE secret=1 AND value<>''")]


# ---------------------------------------------------------------- templates

def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def system_values() -> dict:
    from . import monitor, updater
    from .config import settings
    host = socket.gethostname().split(".")[0]
    scheme = "https" if settings.tls_enabled else "http"
    return {
        "hostname": host, "ip": _lan_ip(), "os": monitor.host_info().get("os", ""),
        "date": time.strftime("%Y-%m-%d"), "time": time.strftime("%H:%M"), "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": time.strftime("%A"), "version": updater.current_version(),
        "url": f"{scheme}://{host}.local:{settings.port}/",
    }


def render(text: str, context: dict) -> str:
    """Replace {{var.x}}, {{sys.x}}, {{trigger.x}}, … in text. Unknown placeholders are left as they are."""
    if not text or "{{" not in text:
        return text or ""
    variables = {r["name"]: r["value"] for r in db.query("SELECT name, value FROM variables")}
    sysvals: dict | None = None

    def lookup(path: str):
        nonlocal sysvals
        parts = path.split(".")
        if parts[0] == "var" and len(parts) == 2:
            return variables.get(parts[1])
        if parts[0] == "sys" and len(parts) == 2:
            if sysvals is None:
                sysvals = system_values()
            return sysvals.get(parts[1])
        cur = context
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return None
        return cur

    def sub(m: re.Match) -> str:
        v = lookup(m.group(1))
        return m.group(0) if v is None else str(v)

    return PLACEHOLDER_RE.sub(sub, text)


def mask(text: str) -> str:
    """Hide secret variable values (for logs)."""
    for s in secret_values():
        if len(s) >= 3:
            text = text.replace(s, "••••••")
    return text
