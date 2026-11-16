"""SQLite storage: users, sessions, conversations, audit log."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin',          -- 'admin' (chat + tools) or 'viewer' (monitor only)
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    ip TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    messages TEXT NOT NULL DEFAULT '[]',          -- raw API message list (JSON)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,                       -- see ai_connections.PROVIDERS
    method TEXT NOT NULL,                         -- how it authenticates (api_key, vertex, bedrock, ...)
    config TEXT NOT NULL DEFAULT '{}',            -- non-secret settings (JSON): model, base_url, region, ...
    secrets TEXT NOT NULL DEFAULT '{}',           -- API keys / credentials (JSON); never sent to browsers
    enabled INTEGER NOT NULL DEFAULT 1,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_usage (                -- tokens used per AI connection per day
    connection_id INTEGER NOT NULL,
    day TEXT NOT NULL,                            -- YYYY-MM-DD (server local time)
    requests INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (connection_id, day)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    username TEXT,
    ip TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        try:
            settings.db_path.chmod(0o600)
        except OSError:
            pass
    return _conn


# Columns added after the first release; existing databases get them on startup.
_USER_COLUMNS = {
    "display_name": "TEXT",
    "email": "TEXT",
    "avatar": "BLOB",           # profile picture (PNG/JPEG/WebP, small)
    "avatar_type": "TEXT",
    "avatar_ver": "INTEGER NOT NULL DEFAULT 0",   # bumps on change (cache busting)
}


_CONVERSATION_COLUMNS = {
    "connection_id": "INTEGER",   # AI connection used by this chat (NULL = the default one)
}


def _migrate(c: sqlite3.Connection) -> None:
    for table, columns in (("users", _USER_COLUMNS), ("conversations", _CONVERSATION_COLUMNS)):
        have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    with _lock:
        c = conn()
        c.execute("BEGIN")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        cur = conn().execute(sql, params)
        return cur.lastrowid or cur.rowcount


# ---------------------------------------------------------------- audit

def audit(action: str, username: str | None = None, ip: str | None = None, detail: Any = None) -> None:
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, default=str)
    if detail and len(detail) > 8000:
        detail = detail[:8000] + "…[truncated]"
    execute(
        "INSERT INTO audit_log (ts, username, ip, action, detail) VALUES (?,?,?,?,?)",
        (time.time(), username, ip, action, detail),
    )


# ---------------------------------------------------------------- app settings

def get_setting(key: str) -> str | None:
    row = query_one("SELECT value FROM app_settings WHERE key=?", (key,))
    return row["value"] if row else None


def set_setting(key: str, value: str | None) -> None:
    if value is None:
        execute("DELETE FROM app_settings WHERE key=?", (key,))
    else:
        execute(
            "INSERT INTO app_settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------- conversations

def create_conversation(user_id: int, title: str) -> int:
    now = time.time()
    return execute(
        "INSERT INTO conversations (user_id, title, messages, created_at, updated_at) VALUES (?,?,?,?,?)",
        (user_id, title[:120], "[]", now, now),
    )


def get_conversation(conv_id: int, user_id: int) -> dict | None:
    row = query_one("SELECT * FROM conversations WHERE id=? AND user_id=?", (conv_id, user_id))
    if not row:
        return None
    d = dict(row)
    d["messages"] = json.loads(d["messages"])
    return d


def save_messages(conv_id: int, messages: list) -> None:
    execute(
        "UPDATE conversations SET messages=?, updated_at=? WHERE id=?",
        (json.dumps(messages, default=str), time.time(), conv_id),
    )


def set_conversation_connection(conv_id: int, user_id: int, connection_id: int | None) -> None:
    execute("UPDATE conversations SET connection_id=? WHERE id=? AND user_id=?", (connection_id, conv_id, user_id))


def list_conversations(user_id: int) -> list[dict]:
    rows = query(
        "SELECT id, title, connection_id, created_at, updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC",
        (user_id,),
    )
    return [dict(r) for r in rows]


def delete_conversation(conv_id: int, user_id: int) -> None:
    execute("DELETE FROM conversations WHERE id=? AND user_id=?", (conv_id, user_id))


def rename_conversation(conv_id: int, user_id: int, title: str) -> None:
    execute("UPDATE conversations SET title=? WHERE id=? AND user_id=?", (title[:120], conv_id, user_id))
