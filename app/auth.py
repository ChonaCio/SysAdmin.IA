"""Users, password hashing (scrypt, stdlib), server-side sessions, login throttling."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, WebSocket

from . import db
from .config import settings

# Separate cookies for HTTPS and HTTP: cookies aren't port-specific, and a browser refuses to let
# an http:// page overwrite a Secure cookie set over https://, which would break HTTP logins.
COOKIE_HTTPS = "sai_session_s"
COOKIE_HTTP = "sai_session"


def is_secure(conn: Request | WebSocket) -> bool:
    return conn.url.scheme in ("https", "wss")


def cookie_name(conn: Request | WebSocket) -> str:
    return COOKIE_HTTPS if is_secure(conn) else COOKIE_HTTP


def session_token(conn: Request | WebSocket) -> str | None:
    return conn.cookies.get(cookie_name(conn))
ROLES = ("admin", "viewer")

# ---------------------------------------------------------------- passwords

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                        maxmem=128 * 1024 * 1024, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, expected = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r),
                            p=int(p), maxmem=128 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(expected))
    except Exception:
        return False


def validate_password_strength(password: str) -> None:
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters.")


# ---------------------------------------------------------------- users

@dataclass
class User:
    id: int
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def create_user(username: str, password: str, role: str = "admin") -> int:
    username = username.strip()
    if not username or len(username) > 64:
        raise ValueError("Invalid username.")
    if role not in ROLES:
        raise ValueError(f"Role must be one of {ROLES}.")
    validate_password_strength(password)
    if db.query_one("SELECT id FROM users WHERE username=?", (username,)):
        raise ValueError("User already exists.")
    return db.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
        (username, hash_password(password), role, time.time()),
    )


def set_password(user_id: int, password: str) -> None:
    validate_password_strength(password)
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user_id))
    db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))  # log out everywhere


def list_users() -> list[dict]:
    return [dict(r) for r in db.query("SELECT id, username, role, created_at FROM users ORDER BY id")]


def delete_user(user_id: int) -> None:
    db.execute("DELETE FROM users WHERE id=?", (user_id,))


def user_count() -> int:
    return db.query_one("SELECT COUNT(*) AS c FROM users")["c"]


# ---------------------------------------------------------------- login throttling

_fail_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
MAX_FAILURES = 5
WINDOW = 15 * 60


def throttled(key: str) -> bool:
    now = time.time()
    with _fail_lock:
        hits = [t for t in _failures.get(key, []) if now - t < WINDOW]
        _failures[key] = hits
        return len(hits) >= MAX_FAILURES


def record_failure(key: str) -> None:
    with _fail_lock:
        _failures.setdefault(key, []).append(time.time())


def clear_failures(key: str) -> None:
    with _fail_lock:
        _failures.pop(key, None)


def authenticate(username: str, password: str) -> User | None:
    row = db.query_one("SELECT * FROM users WHERE username=?", (username.strip(),))
    if not row:
        hash_password(password)  # equalize timing
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return User(row["id"], row["username"], row["role"])


# ---------------------------------------------------------------- sessions

def _token_hash(token: str) -> str:
    return hmac.new(settings.secret_key.encode(), token.encode(), hashlib.sha256).hexdigest()


def create_session(user: User, ip: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, ip) VALUES (?,?,?,?,?)",
        (_token_hash(token), user.id, now, now + settings.session_hours * 3600, ip),
    )
    return token


def destroy_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def user_from_token(token: str | None) -> User | None:
    if not token:
        return None
    row = db.query_one(
        "SELECT u.id, u.username, u.role, s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=?",
        (_token_hash(token),),
    )
    if not row or row["expires_at"] < time.time():
        return None
    return User(row["id"], row["username"], row["role"])


def client_ip(request: Request | WebSocket) -> str:
    return request.client.host if request.client else "?"


# ---------------------------------------------------------------- FastAPI dependencies

def current_user(request: Request) -> User:
    user = user_from_token(session_token(request))
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # CSRF defence for state-changing requests: SameSite=Strict cookie + custom header
    # (a cross-site form post cannot set custom headers).
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-sai-csrf") != "1":
        raise HTTPException(status_code=403, detail="Missing CSRF header")
    return user


def admin_user(request: Request) -> User:
    user = current_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def websocket_user(ws: WebSocket) -> User | None:
    # Reject cross-origin WebSocket handshakes (cookies are sent cross-origin for WS).
    origin = ws.headers.get("origin", "")
    host = ws.headers.get("host", "")
    if origin and origin.split("://", 1)[-1] != host:
        return None
    return user_from_token(session_token(ws))
