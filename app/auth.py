"""Users, password hashing (scrypt, stdlib), server-side sessions, login throttling."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
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
    username = validate_username(username)
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


_PROFILE_COLS = "id, username, role, created_at, display_name, email, avatar_ver, avatar IS NOT NULL AS has_avatar"


def _profile(row) -> dict:
    d = dict(row)
    d["has_avatar"] = bool(d["has_avatar"])
    d["avatar_url"] = f"/api/users/{d['id']}/avatar?v={d['avatar_ver']}" if d["has_avatar"] else None
    d["display_name"] = d.get("display_name") or ""
    d["email"] = d.get("email") or ""
    return d


def list_users() -> list[dict]:
    return [_profile(r) for r in db.query(f"SELECT {_PROFILE_COLS} FROM users ORDER BY id")]


def get_profile(user_id: int) -> dict | None:
    row = db.query_one(f"SELECT {_PROFILE_COLS} FROM users WHERE id=?", (user_id,))
    return _profile(row) if row else None


USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{1,24}$")


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Usernames use 1–64 letters, digits, dots, dashes, underscores or @, "
                         "starting with a letter or digit.")
    return username


def update_profile(user_id: int, username: str | None = None, display_name: str | None = None,
                   email: str | None = None) -> dict:
    sets, params = [], []
    if username is not None:
        username = validate_username(username)
        clash = db.query_one("SELECT id FROM users WHERE username=? AND id<>?", (username, user_id))
        if clash:
            raise ValueError("That username is already taken.")
        sets.append("username=?"); params.append(username)
    if display_name is not None:
        display_name = " ".join(display_name.split())
        if len(display_name) > 80:
            raise ValueError("The display name can be at most 80 characters.")
        sets.append("display_name=?"); params.append(display_name or None)
    if email is not None:
        email = email.strip()
        if email and not EMAIL_RE.fullmatch(email):
            raise ValueError("That doesn't look like an email address.")
        sets.append("email=?"); params.append(email or None)
    if sets:
        db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*params, user_id))
    return get_profile(user_id)


AVATAR_MAX = 512 * 1024
_IMAGE_MAGIC = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}


def set_avatar(user_id: int, data: bytes | None, mime: str | None = None) -> None:
    if data is None:
        db.execute("UPDATE users SET avatar=NULL, avatar_type=NULL, avatar_ver=avatar_ver+1 WHERE id=?", (user_id,))
        return
    if len(data) > AVATAR_MAX:
        raise ValueError("The picture is too large (max 512 KB).")
    ok = (mime in _IMAGE_MAGIC and data.startswith(_IMAGE_MAGIC[mime])) or \
         (mime == "image/webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    if not ok:
        raise ValueError("Use a PNG, JPEG or WebP picture.")
    db.execute("UPDATE users SET avatar=?, avatar_type=?, avatar_ver=avatar_ver+1 WHERE id=?",
               (data, mime, user_id))


def get_avatar(user_id: int) -> tuple[bytes, str] | None:
    row = db.query_one("SELECT avatar, avatar_type FROM users WHERE id=?", (user_id,))
    if not row or row["avatar"] is None:
        return None
    return bytes(row["avatar"]), row["avatar_type"]


def admin_count() -> int:
    return db.query_one("SELECT COUNT(*) AS c FROM users WHERE role='admin'")["c"]


def set_role(user_id: int, role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"Role must be one of {ROLES}.")
    row = db.query_one("SELECT role FROM users WHERE id=?", (user_id,))
    if not row:
        raise ValueError("No such user.")
    if row["role"] == "admin" and role != "admin" and admin_count() <= 1:
        raise ValueError("There must be at least one admin.")
    db.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))


def delete_user(user_id: int) -> None:
    row = db.query_one("SELECT role FROM users WHERE id=?", (user_id,))
    if row and row["role"] == "admin" and admin_count() <= 1:
        raise ValueError("There must be at least one admin.")
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
