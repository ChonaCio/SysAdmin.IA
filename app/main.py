"""FastAPI application: auth, monitoring API, chat WebSocket, static frontend."""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import functools
import os
import re
import shutil
import signal
import json
import logging
import time
import urllib.parse
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.concurrency import iterate_in_threadpool
from pydantic import BaseModel


from . import ai_connections as ai
from . import agent, auth, console, db, drives, google_oauth, jobs, mail, monitor, providers, server_name, updater
from . import containers, firewall, lan_names, local_ai, model_search, network, publish, routers, user_prefs, variables
from . import dags, device_pyenvs, devices, procs, pyenvs, ssh_keys
from .auth import User, admin_user, current_user
from .config import settings

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="SysAdmin.IA", docs_url=None, redoc_url=None, openapi_url=None)

# Turn the old single Claude key (Admin → Claude API) into an AI connection, once.
ai.migrate_legacy()
log = logging.getLogger("sysadmin_ia")


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    """A bug or an unexpected answer from another system: log it (journalctl -u sysadmin-ia) and show what
    happened instead of a bare "HTTP 500"."""
    log.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse({"detail": f"Unexpected error ({type(exc).__name__}): {exc}"[:500]}, status_code=500)


DEVICE_PATHS = ("/api/docker/", "/api/localai/", "/api/drives", "/api/files", "/api/pyenvs", "/api/sysprocs")


class DeviceTarget:
    """`?device=ID` on /api/docker/…, /api/localai/… (and Drives, files, Python environments, the Processes page's
    system processes): the request is for a device from Server → Devices
    (containers.py / local_ai.py then talk to it through an SSH tunnel). Pure ASGI, so the context variable
    reaches the endpoint and the threads it starts with asyncio.to_thread."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(DEVICE_PATHS):
            from urllib.parse import parse_qs
            dev = (parse_qs(scope.get("query_string", b"").decode("latin-1")).get("device") or [""])[0]
            if dev.isdigit() and int(dev) > 0:
                token = devices.TARGET.set(int(dev))
                try:
                    return await self.app(scope, receive, send)
                finally:
                    devices.TARGET.reset(token)
        await self.app(scope, receive, send)


app.add_middleware(DeviceTarget)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp: Response = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    # HSTS applies to the whole host (all ports), so it would block the HTTP listener.
    # Only send it when HTTPS is the only way in.
    if auth.is_secure(request) and not settings.http_enabled:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.url.path.startswith(("/static/", "/api/")) or request.url.path in ("/", "/login"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- pages & static

def _page(name: str) -> FileResponse:
    return FileResponse(STATIC / name, media_type="text/html")


@app.get("/")
async def index(request: Request):
    if not auth.user_from_token(auth.session_token(request)):
        return RedirectResponse("/login", status_code=303)
    return _page("index.html")


@app.get("/login")
async def login_page():
    return _page("login.html")


@app.get("/static/{path:path}")
async def static(path: str):
    target = (STATIC / path).resolve()
    if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)


# ---------------------------------------------------------------- auth API

class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(body: LoginBody, request: Request):
    ip = auth.client_ip(request)
    key = f"{ip}:{body.username.lower()}"
    if auth.throttled(key) or auth.throttled(ip):
        db.audit("login_throttled", body.username, ip)
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    user = await asyncio.to_thread(auth.authenticate, body.username, body.password)
    if not user:
        auth.record_failure(key)
        auth.record_failure(ip)
        db.audit("login_failed", body.username, ip)
        await asyncio.sleep(0.5)
        raise HTTPException(401, "Invalid username or password.")
    auth.clear_failures(key)
    token = auth.create_session(user, ip)
    db.audit("login", user.username, ip)
    resp = JSONResponse({"ok": True})
    secure = auth.is_secure(request)
    resp.set_cookie(auth.cookie_name(request), token, httponly=True, samesite="strict",
                    secure=secure, max_age=settings.session_hours * 3600, path="/")
    return resp


@app.post("/api/logout")
async def logout(request: Request):
    auth.destroy_session(auth.session_token(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.cookie_name(request), path="/", secure=auth.is_secure(request))
    return resp


@app.get("/api/connection")
async def connection(request: Request):
    """Lets the login page warn when the connection is unencrypted."""
    https_url = None
    if settings.tls_enabled and not auth.is_secure(request):
        host = (request.url.hostname or "").strip("[]")
        host = f"[{host}]" if ":" in host else host
        https_url = f"https://{host}:{settings.port}/"
    return {"secure": auth.is_secure(request), "https_url": https_url}


@app.get("/api/setup-status")
async def setup_status():
    return {"has_users": auth.user_count() > 0}


@app.get("/api/me")
async def me(user: User = Depends(current_user)):
    p = auth.get_profile(user.id) or {}
    return {"id": user.id, "username": user.username, "role": user.role,
            "display_name": p.get("display_name", ""), "email": p.get("email", ""),
            "avatar_url": p.get("avatar_url"),
            "allow_auto_approve": settings.allow_auto_approve,
            "console": settings.allow_console and user.is_admin,
            "ai_ready": ai.any_enabled(),
            "prefs": user_prefs.get(user.id)}


@app.put("/api/me/prefs")
async def update_my_prefs(body: dict, request: Request, user: User = Depends(current_user)):
    """Appearance & language. Any signed-in user, for their own account."""
    p = user_prefs.save(user.id, body if isinstance(body, dict) else {})
    db.audit("prefs_updated", user.username, auth.client_ip(request), p)
    return p


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/me/password")
async def change_password(body: PasswordBody, request: Request, user: User = Depends(current_user)):
    if not await asyncio.to_thread(auth.authenticate, user.username, body.current_password):
        raise HTTPException(400, "Current password is incorrect.")
    try:
        auth.set_password(user.id, body.new_password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("password_changed", user.username, auth.client_ip(request))
    return {"ok": True}


# ---------------------------------------------------------------- profile (any signed-in user)

class ProfileBody(BaseModel):
    username: str | None = None
    display_name: str | None = None
    email: str | None = None


class AvatarBody(BaseModel):
    data: str | None = None      # base64 image; None removes the picture
    type: str | None = None      # image/png, image/jpeg or image/webp


@app.put("/api/me/profile")
async def update_my_profile(body: ProfileBody, request: Request, user: User = Depends(current_user)):
    try:
        p = auth.update_profile(user.id, body.username, body.display_name, body.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    changed = {k: getattr(body, k) for k in ("username", "display_name", "email") if getattr(body, k) is not None}
    db.audit("profile_updated", p["username"], auth.client_ip(request), changed)
    return p


@app.put("/api/me/avatar")
async def update_my_avatar(body: AvatarBody, request: Request, user: User = Depends(current_user)):
    try:
        raw = None if body.data is None else base64.b64decode(body.data, validate=True)
        auth.set_avatar(user.id, raw, body.type)
    except (ValueError, binascii.Error) as e:
        raise HTTPException(400, str(e) or "Invalid picture.")
    db.audit("avatar_" + ("removed" if raw is None else "updated"), user.username, auth.client_ip(request))
    return auth.get_profile(user.id)


@app.get("/api/users/{uid}/avatar")
async def user_avatar(uid: int, user: User = Depends(current_user)):
    found = auth.get_avatar(uid)
    if not found:
        raise HTTPException(404)
    data, mime = found
    return Response(data, media_type=mime, headers={"Content-Disposition": "inline"})


# ---------------------------------------------------------------- users (admin)

class NewUser(BaseModel):
    username: str
    password: str
    role: str = "admin"


class UserPatch(BaseModel):
    role: str | None = None
    password: str | None = None   # set a new password for this user


@app.get("/api/users")
async def users(user: User = Depends(admin_user)):
    return auth.list_users()


@app.patch("/api/users/{uid}")
async def patch_user(uid: int, body: UserPatch, request: Request, user: User = Depends(admin_user)):
    target = auth.get_profile(uid)
    if not target:
        raise HTTPException(404)
    try:
        if body.role is not None:
            if uid == user.id and body.role != "admin":
                raise ValueError("You cannot remove your own admin role.")
            auth.set_role(uid, body.role)
            db.audit("user_role", user.username, auth.client_ip(request), {"username": target["username"], "role": body.role})
        if body.password:
            auth.set_password(uid, body.password)
            db.audit("user_password_set", user.username, auth.client_ip(request), {"username": target["username"]})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return auth.get_profile(uid)


@app.post("/api/users")
async def add_user(body: NewUser, request: Request, user: User = Depends(admin_user)):
    try:
        uid = auth.create_user(body.username, body.password, body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("user_created", user.username, auth.client_ip(request), {"username": body.username, "role": body.role})
    return {"id": uid}


@app.delete("/api/users/{uid}")
async def remove_user(uid: int, request: Request, user: User = Depends(admin_user)):
    if uid == user.id:
        raise HTTPException(400, "You cannot delete yourself.")
    try:
        auth.delete_user(uid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("user_deleted", user.username, auth.client_ip(request), {"id": uid})
    return {"ok": True}


# ---------------------------------------------------------------- AI connections (admin)

class ConnectionBody(BaseModel):
    name: str | None = None
    provider: str | None = None
    method: str | None = None
    config: dict = {}            # non-secret fields (+ model, effort)
    secrets: dict = {}           # new secret values; blank/missing = keep the saved one
    clear_secrets: list[str] = []
    enabled: bool = True
    is_default: bool = False


class ConnectionProbe(BaseModel):
    id: int | None = None                 # a saved connection …
    draft: ConnectionBody | None = None   # … or the editor's current (unsaved) values


def _probe_conn(body: ConnectionProbe) -> dict:
    if body.draft is not None:
        return ai.draft(body.draft.model_dump() if hasattr(body.draft, "model_dump") else body.draft.dict(), body.id)
    c = ai.get(body.id)
    if not c:
        raise ai.ConnectionError_("That connection no longer exists.")
    return c


def _body_dict(body: ConnectionBody) -> dict:
    return body.model_dump() if hasattr(body, "model_dump") else body.dict()


@app.get("/api/ai/providers")
async def ai_providers(user: User = Depends(admin_user)):
    return ai.catalog()


@app.get("/api/ai/connections")
async def ai_connections_list(user: User = Depends(admin_user)):
    return [ai.public(c) for c in ai.list_all()]


@app.post("/api/ai/connections")
async def ai_connection_create(body: ConnectionBody, request: Request, user: User = Depends(admin_user)):
    try:
        c = ai.create(_body_dict(body))
    except ai.ConnectionError_ as e:
        raise HTTPException(400, str(e))
    db.audit("ai_connection_created", user.username, auth.client_ip(request),
             {"id": c["id"], "name": c["name"], "provider": c["provider"], "method": c["method"]})
    return ai.public(c)


@app.put("/api/ai/connections/{cid}")
async def ai_connection_update(cid: int, body: ConnectionBody, request: Request, user: User = Depends(admin_user)):
    try:
        c = ai.update(cid, _body_dict(body))
    except ai.ConnectionError_ as e:
        raise HTTPException(400, str(e))
    db.audit("ai_connection_updated", user.username, auth.client_ip(request),
             {"id": cid, "name": c["name"], "provider": c["provider"], "method": c["method"],
              "secrets_changed": sorted(k for k, v in body.secrets.items() if v) + list(body.clear_secrets)})
    return ai.public(c)


@app.delete("/api/ai/connections/{cid}")
async def ai_connection_delete(cid: int, request: Request, user: User = Depends(admin_user)):
    c = ai.get(cid)
    if not c:
        raise HTTPException(404)
    ai.delete(cid)
    db.audit("ai_connection_deleted", user.username, auth.client_ip(request), {"id": cid, "name": c["name"]})
    return {"ok": True}


@app.post("/api/ai/connections/{cid}/default")
async def ai_connection_default(cid: int, request: Request, user: User = Depends(admin_user)):
    if not ai.get(cid):
        raise HTTPException(404)
    ai.set_default(cid)
    db.audit("ai_connection_default", user.username, auth.client_ip(request), {"id": cid})
    return {"ok": True}


@app.post("/api/ai/test")
async def ai_test(body: ConnectionProbe, user: User = Depends(admin_user)):
    """Check that a connection works (a tiny request; it may use a few tokens)."""
    try:
        conn = _probe_conn(body)
        message = await asyncio.wait_for(providers.for_connection(conn).test(), 45)
        return {"ok": True, "message": message}
    except ai.ConnectionError_ as e:
        return {"ok": False, "message": str(e)}
    except providers.ProviderError as e:
        return {"ok": False, "message": str(e)}
    except asyncio.TimeoutError:
        return {"ok": False, "message": "The AI service didn't answer within 45 seconds."}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


@app.post("/api/ai/models")
async def ai_models(body: ConnectionProbe, user: User = Depends(admin_user)):
    """Models the connection can use, as reported by the service."""
    try:
        conn = _probe_conn(body)
        return {"models": await asyncio.wait_for(providers.for_connection(conn).models(), 30)}
    except (ai.ConnectionError_, providers.ProviderError) as e:
        raise HTTPException(400, str(e))
    except asyncio.TimeoutError:
        raise HTTPException(400, "The AI service didn't answer within 30 seconds.")
    except Exception as e:
        raise HTTPException(400, f"Couldn't list models: {type(e).__name__}: {e}")


class GoogleStartBody(BaseModel):
    client_id: str
    client_secret: str | None = None      # blank = the one saved in this connection
    connection_id: int | None = None


class GoogleFinishBody(BaseModel):
    url: str                              # the address Google redirected the browser to


@app.post("/api/ai/google/start")
async def ai_google_start(body: GoogleStartBody, user: User = Depends(admin_user)):
    """Begin “Sign in with Google” (Claude on Vertex AI). Returns the Google sign-in address."""
    secret = (body.client_secret or "").strip()
    if not secret and body.connection_id:
        saved = ai.get(body.connection_id)
        secret = (saved or {}).get("secrets", {}).get("google_client_secret", "")
    try:
        return google_oauth.start(body.client_id, secret)
    except google_oauth.GoogleSignInError as e:
        raise HTTPException(400, str(e))


@app.post("/api/ai/google/finish")
async def ai_google_finish(body: GoogleFinishBody, request: Request, user: User = Depends(admin_user)):
    try:
        r = await asyncio.to_thread(google_oauth.finish, body.url)
    except google_oauth.GoogleSignInError as e:
        raise HTTPException(400, str(e))
    db.audit("ai_google_signin", user.username, auth.client_ip(request), {"google_account": r["email"]})
    return r   # {"grant": "grant:…", "email": …}; the refresh token itself stays on the server


@app.get("/api/ai/usage")
async def ai_usage(user: User = Depends(admin_user)):
    """Token usage per connection (today / this month / total, last 30 days), monthly limit
    and the latest rate limits the service reported."""
    return {str(c["id"]): ai.usage_summary(c) for c in ai.list_all()}


@app.get("/api/chat/connections")
async def chat_connections(user: User = Depends(admin_user)):
    """Enabled connections for the chat's picker."""
    return [{"id": c["id"], "name": c["name"], "short": ai.public(c)["short"], "model": ai.public(c)["model"],
             "is_default": c["is_default"]} for c in ai.list_all() if c["enabled"]]


# ---------------------------------------------------------------- local AI / Ollama (admin)

class ModelBody(BaseModel):
    name: str


class OllamaServiceBody(BaseModel):
    action: str


class OllamaContextBody(BaseModel):
    context_length: int


class OllamaUninstallBody(BaseModel):
    delete_models: bool = False


def _lai(fn, *a):
    try:
        return fn(*a)
    except local_ai.LocalAIError as e:
        raise HTTPException(400, str(e))


@app.get("/api/localai/status")
async def localai_status(user: User = Depends(admin_user)):
    return await asyncio.to_thread(local_ai.status)


@app.get("/api/localai/models")
async def localai_models(user: User = Depends(admin_user)):
    data = await asyncio.to_thread(local_ai.models)
    dev = devices.current_id()
    used = {c["config"].get("model"): c["name"] for c in ai.list_all()
            if c["provider"] == "ollama_managed" and (c["config"].get("device") or None) == dev}
    for m in data["models"]:
        m["connection"] = used.get(m["name"])
    return data


@app.get("/api/localai/catalog")
async def localai_catalog(user: User = Depends(admin_user)):
    return await asyncio.to_thread(local_ai.catalog)


@app.get("/api/localai/search")
async def localai_search(source: str = "recommended", q: str = "", use: str = "", params: str = "",
                         fits: bool = False, sort: str = "", offset: int = 0, refresh: bool = False,
                         user: User = Depends(admin_user)):
    """Find models: the recommended list, the whole Ollama library, or Hugging Face (GGUF)."""
    return await asyncio.to_thread(model_search.search, source, q[:100], use, params, fits, sort, offset,
                                   None, refresh)


@app.post("/api/localai/pull")
async def localai_pull(body: ModelBody, request: Request, user: User = Depends(admin_user)):
    r = await asyncio.to_thread(_lai, local_ai.pull, body.name)
    db.audit("localai_pull", user.username, auth.client_ip(request), {"model": r["name"]})
    return r


@app.post("/api/localai/pull/cancel")
async def localai_pull_cancel(body: ModelBody, user: User = Depends(admin_user)):
    local_ai.cancel_pull(body.name)
    return {"ok": True}


@app.post("/api/localai/unload")
async def localai_unload(body: ModelBody, user: User = Depends(admin_user)):
    await asyncio.to_thread(_lai, local_ai.unload, body.name)
    return {"ok": True}


@app.post("/api/localai/delete")
async def localai_delete(body: ModelBody, request: Request, user: User = Depends(admin_user)):
    dev = devices.current_id()
    users = [c["name"] for c in ai.list_all() if c["provider"] == "ollama_managed" and c["config"].get("model") == body.name
             and (c["config"].get("device") or None) == dev]
    await asyncio.to_thread(_lai, local_ai.delete, body.name)
    db.audit("localai_delete", user.username, auth.client_ip(request), {"model": body.name})
    return {"ok": True, "connections": users}


@app.post("/api/localai/use")
async def localai_use(body: ModelBody, request: Request, user: User = Depends(admin_user)):
    """Create (or return) the AI connection for an installed model, so it can be picked in chats."""
    dev = devices.current()      # a model on a device: the connection reaches it through an SSH tunnel
    for c in ai.list_all():
        if (c["provider"] == "ollama_managed" and c["config"].get("model") == body.name
                and (c["config"].get("device") or None) == (dev["id"] if dev else None)):
            return ai.public(c)
    try:
        cfg = {"model": body.name, **({"device": dev["id"]} if dev else {})}
        c = await asyncio.to_thread(ai.create, {"name": f"{body.name} ({dev['name'] if dev else 'local'})", "provider": "ollama_managed",
                                                "method": "local", "config": cfg, "is_default": not ai.any_enabled()})
    except ai.ConnectionError_ as e:
        raise HTTPException(400, str(e))
    db.audit("ai_connection_created", user.username, auth.client_ip(request),
             {"id": c["id"], "name": c["name"], "provider": "ollama_managed"})
    return ai.public(c)


@app.post("/api/localai/install")
async def localai_install(request: Request, user: User = Depends(admin_user)):
    out = await asyncio.to_thread(_lai, local_ai.helper, "ollama-install")
    db.audit("localai_install", user.username, auth.client_ip(request))
    return {"ok": True, "message": out}


@app.get("/api/localai/install/progress")
async def localai_install_progress(user: User = Depends(admin_user)):
    return local_ai.install_progress()


@app.post("/api/localai/service")
async def localai_service(body: OllamaServiceBody, request: Request, user: User = Depends(admin_user)):
    if body.action not in ("start", "stop", "restart"):
        raise HTTPException(400, "Unknown action.")
    out = await asyncio.to_thread(_lai, local_ai.helper, "ollama-service", body.action)
    db.audit("localai_service", user.username, auth.client_ip(request), body.action)
    return {"ok": True, "message": out}


@app.post("/api/localai/context")
async def localai_context(body: OllamaContextBody, request: Request, user: User = Depends(admin_user)):
    if body.context_length not in local_ai.CONTEXT_CHOICES:
        raise HTTPException(400, "Unsupported context length.")
    out = await asyncio.to_thread(_lai, local_ai.helper, "ollama-context", str(body.context_length))
    db.audit("localai_context", user.username, auth.client_ip(request), body.context_length)
    return {"ok": True, "message": out}


@app.post("/api/localai/uninstall")
async def localai_uninstall(body: OllamaUninstallBody, request: Request, user: User = Depends(admin_user)):
    args = ["ollama-uninstall"] + (["--delete-models"] if body.delete_models else [])
    out = await asyncio.to_thread(_lai, local_ai.helper, *args)
    db.audit("localai_uninstall", user.username, auth.client_ip(request), {"delete_models": body.delete_models})
    return {"ok": True, "message": out}


# ---------------------------------------------------------------- containers (Docker, admin)

def _dk(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except containers.DockerError as e:
        raise HTTPException(400, str(e))


class DockerActionBody(BaseModel):
    action: str


class DockerRemoveBody(BaseModel):
    volumes: bool = False
    force: bool = False


class DockerSpecBody(BaseModel):
    spec: dict


class DockerNameBody(BaseModel):
    name: str
    internal: bool = False


class DockerPruneBody(BaseModel):
    what: list[str]


class DockerUninstallBody(BaseModel):
    delete_data: bool = False


class StackBody(BaseModel):
    compose: str
    env: str = ""


class NetConnectBody(BaseModel):
    container: str
    connect: bool = True


@app.get("/api/docker/status")
async def docker_status(user: User = Depends(admin_user)):
    return await asyncio.to_thread(containers.status)


@app.post("/api/docker/install")
async def docker_install(request: Request, user: User = Depends(admin_user)):
    out = await asyncio.to_thread(_dk, containers.helper, "docker-install")
    db.audit("docker_install", user.username, auth.client_ip(request))
    return {"ok": True, "message": out}


@app.post("/api/docker/access")
async def docker_access(body: DockerActionBody, request: Request, user: User = Depends(admin_user)):
    if body.action not in ("grant", "revoke"):
        raise HTTPException(400, "Unknown action.")
    out = await asyncio.to_thread(_dk, containers.helper, "docker-access", body.action)
    db.audit("docker_access", user.username, auth.client_ip(request), body.action)
    return {"ok": True, "message": out}


@app.post("/api/docker/restart-app")
async def docker_restart_app(request: Request, user: User = Depends(admin_user)):
    """Restart SysAdmin.IA so its new docker group membership applies."""
    db.audit("docker_restart_app", user.username, auth.client_ip(request))
    _schedule_restart()
    return {"ok": True}


@app.post("/api/docker/service")
async def docker_service(body: DockerActionBody, request: Request, user: User = Depends(admin_user)):
    if body.action not in ("start", "stop", "restart"):
        raise HTTPException(400, "Unknown action.")
    out = await asyncio.to_thread(_dk, containers.helper, "docker-service", body.action)
    db.audit("docker_service", user.username, auth.client_ip(request), body.action)
    return {"ok": True, "message": out}


@app.post("/api/docker/uninstall")
async def docker_uninstall(body: DockerUninstallBody, request: Request, user: User = Depends(admin_user)):
    args = ["docker-uninstall"] + (["--delete-data"] if body.delete_data else [])
    out = await asyncio.to_thread(_dk, containers.helper, *args, timeout=900)
    db.audit("docker_uninstall", user.username, auth.client_ip(request), {"delete_data": body.delete_data})
    return {"ok": True, "message": out}


@app.get("/api/docker/containers")
async def docker_containers(stats: bool = False, user: User = Depends(admin_user)):
    return await asyncio.to_thread(_dk, containers.list_containers, stats)


@app.get("/api/docker/containers/{cid}")
async def docker_container(cid: str, user: User = Depends(admin_user)):
    return await asyncio.to_thread(_dk, containers.inspect, cid)


@app.get("/api/docker/containers/{cid}/logs")
async def docker_container_logs(cid: str, tail: int = 300, timestamps: bool = False, user: User = Depends(admin_user)):
    return {"logs": await asyncio.to_thread(_dk, containers.logs, cid, tail, timestamps)}


@app.post("/api/docker/containers/{cid}/action")
async def docker_container_action(cid: str, body: DockerActionBody, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_dk, containers.container_action, cid, body.action)
    db.audit("docker_container_" + body.action, user.username, auth.client_ip(request), {"container": cid})
    return {"ok": True}


@app.post("/api/docker/containers/{cid}/remove")
async def docker_container_remove(cid: str, body: DockerRemoveBody, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_dk, containers.remove_container, cid, body.volumes)
    db.audit("docker_container_remove", user.username, auth.client_ip(request), {"container": cid, "volumes": body.volumes})
    return {"ok": True}


@app.post("/api/docker/containers")
async def docker_container_create(body: DockerSpecBody, request: Request, user: User = Depends(admin_user)):
    t = await asyncio.to_thread(_dk, containers.create_container, body.spec)
    db.audit("docker_container_create", user.username, auth.client_ip(request),
             {"name": body.spec.get("name"), "image": body.spec.get("image"), "device": devices.current_id()})
    return t


@app.put("/api/docker/containers/{cid}")
async def docker_container_recreate(cid: str, body: DockerSpecBody, request: Request, user: User = Depends(admin_user)):
    t = await asyncio.to_thread(_dk, containers.recreate_container, cid, body.spec)
    db.audit("docker_container_update", user.username, auth.client_ip(request),
             {"container": cid, "name": body.spec.get("name"), "image": body.spec.get("image")})
    return t


@app.get("/api/docker/prepare")
async def docker_prepare(image: str = "", name: str = "", app_id: str = "", user: User = Depends(admin_user)):
    return await asyncio.to_thread(_dk, containers.prepare, image, name, app_id)


@app.get("/api/docker/apps")
async def docker_apps(user: User = Depends(admin_user)):
    """Common apps (PostgreSQL, Jellyfin, Nextcloud…) with ready-made settings."""
    return await asyncio.to_thread(containers.apps_catalog)


@app.get("/api/docker/host")
async def docker_host(user: User = Depends(admin_user)):
    """Drives (from the Drives module) and used ports, for sharing folders and ports with containers."""
    def run():
        used = containers.used_host_ports()
        return {**containers.host_folders(), "used_ports": {str(k): v for k, v in sorted(used.items())}}
    return await asyncio.to_thread(run)


@app.get("/api/docker/tasks")
async def docker_tasks(all: bool = False, user: User = Depends(admin_user)):
    return containers.tasks(all)


@app.get("/api/docker/hosts")
async def docker_hosts(user: User = Depends(admin_user)):
    """Where containers can run: this server + devices with Docker (no connection is made)."""
    return await asyncio.to_thread(containers.hosts)


@app.get("/api/docker/everywhere")
async def docker_everywhere(stats: bool = False, user: User = Depends(admin_user)):
    """Containers of this server and of every device with Docker, each tagged with where it runs."""
    return await asyncio.to_thread(_dk, containers.list_everywhere, stats)


@app.get("/api/docker/images/where")
async def docker_image_where(name: str, user: User = Depends(admin_user)):
    return await asyncio.to_thread(_dk, containers.image_everywhere, name)


@app.get("/api/docker/images/everywhere")
async def docker_images_everywhere(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_dk, containers.images_everywhere)


class ImageCopyBody(BaseModel):
    name: str
    source: int = 0
    target: int = 0


@app.post("/api/docker/images/copy")
async def docker_image_copy(body: ImageCopyBody, request: Request, user: User = Depends(admin_user)):
    """Copy an image from one computer to another (docker save → docker load through the app)."""
    t = await asyncio.to_thread(_dk, containers.copy_image, body.name, body.source, body.target)
    db.audit("docker_image_copy", user.username, auth.client_ip(request),
             {"image": body.name, "from": body.source or "this server", "to": body.target or "this server"})
    return t


@app.get("/api/docker/images")
async def docker_images(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_dk, containers.list_images)


@app.post("/api/docker/images/pull")
async def docker_image_pull(body: ModelBody, request: Request, user: User = Depends(admin_user)):
    t = await asyncio.to_thread(_dk, containers.pull_image, body.name)
    db.audit("docker_image_pull", user.username, auth.client_ip(request), {"image": body.name})
    return t


@app.post("/api/docker/images/remove")
async def docker_image_remove(body: ModelBody, request: Request, force: bool = False, user: User = Depends(admin_user)):
    await asyncio.to_thread(_dk, containers.remove_image, body.name, force)
    db.audit("docker_image_remove", user.username, auth.client_ip(request), {"image": body.name})
    return {"ok": True}


@app.get("/api/docker/search")
async def docker_search(q: str, user: User = Depends(admin_user)):
    return await asyncio.to_thread(_dk, containers.search_hub, q)


@app.get("/api/docker/volumes")
async def docker_volumes(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_dk, containers.list_volumes)


@app.post("/api/docker/volumes")
async def docker_volume_create(body: DockerNameBody, request: Request, user: User = Depends(admin_user)):
    r = await asyncio.to_thread(_dk, containers.create_volume, body.name)
    db.audit("docker_volume_create", user.username, auth.client_ip(request), {"name": body.name})
    return r


@app.delete("/api/docker/volumes/{name}")
async def docker_volume_remove(name: str, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_dk, containers.remove_volume, name)
    db.audit("docker_volume_remove", user.username, auth.client_ip(request), {"name": name})
    return {"ok": True}


@app.get("/api/docker/networks")
async def docker_networks(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_dk, containers.list_networks)


@app.post("/api/docker/networks")
async def docker_network_create(body: DockerNameBody, request: Request, user: User = Depends(admin_user)):
    r = await asyncio.to_thread(_dk, containers.create_network, body.name, body.internal)
    db.audit("docker_network_create", user.username, auth.client_ip(request), {"name": body.name})
    return r


@app.delete("/api/docker/networks/{name}")
async def docker_network_remove(name: str, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_dk, containers.remove_network, name)
    db.audit("docker_network_remove", user.username, auth.client_ip(request), {"name": name})
    return {"ok": True}


@app.post("/api/docker/networks/{name}/connect")
async def docker_network_connect(name: str, body: NetConnectBody, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_dk, containers.connect_network, name, body.container, body.connect)
    db.audit("docker_network_" + ("connect" if body.connect else "disconnect"), user.username, auth.client_ip(request),
             {"network": name, "container": body.container})
    return {"ok": True}


@app.get("/api/docker/disk")
async def docker_disk(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_dk, containers.disk_usage)


@app.post("/api/docker/prune")
async def docker_prune(body: DockerPruneBody, request: Request, user: User = Depends(admin_user)):
    r = await asyncio.to_thread(_dk, containers.prune, body.what)
    db.audit("docker_prune", user.username, auth.client_ip(request), {"what": body.what, **r})
    return r


@app.get("/api/docker/stacks")
async def docker_stacks(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_dk, containers.list_stacks)


@app.get("/api/docker/stacks/{name}")
async def docker_stack(name: str, user: User = Depends(admin_user)):
    return await asyncio.to_thread(_dk, containers.get_stack, name)


@app.put("/api/docker/stacks/{name}")
async def docker_stack_save(name: str, body: StackBody, request: Request, user: User = Depends(admin_user)):
    r = await asyncio.to_thread(_dk, containers.save_stack, name, body.compose, body.env)
    db.audit("docker_stack_save", user.username, auth.client_ip(request), {"name": name})
    return r


@app.post("/api/docker/stacks/{name}/action")
async def docker_stack_action(name: str, body: DockerActionBody, request: Request, user: User = Depends(admin_user)):
    t = await asyncio.to_thread(_dk, containers.stack_action, name, body.action)
    db.audit("docker_stack_" + body.action, user.username, auth.client_ip(request), {"name": name})
    return t


@app.delete("/api/docker/stacks/{name}")
async def docker_stack_delete(name: str, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_dk, containers.delete_stack, name)
    db.audit("docker_stack_delete", user.username, auth.client_ip(request), {"name": name})
    return {"ok": True}


@app.get("/api/docker/stacks/{name}/logs")
async def docker_stack_logs(name: str, tail: int = 200, user: User = Depends(admin_user)):
    return {"logs": await asyncio.to_thread(_dk, containers.stack_logs, name, tail)}


# ---------------------------------------------------------------- variables (admin)

class VariableBody(BaseModel):
    name: str
    value: str | None = None      # for a secret: empty = keep the saved value
    description: str = ""
    secret: bool = False


@app.get("/api/variables")
async def variables_list(user: User = Depends(admin_user)):
    return {"variables": variables.list_all(), "builtins": await asyncio.to_thread(variables.system_values),
            "monitor": await asyncio.to_thread(variables.monitor_values)}


@app.get("/api/variables/monitor")
async def variables_monitor(user: User = Depends(admin_user)):
    """Live monitor values only (the Variables page refreshes them every few seconds)."""
    return await asyncio.to_thread(variables.monitor_values)


@app.post("/api/variables")
async def variables_create(body: VariableBody, request: Request, user: User = Depends(admin_user)):
    if variables.get(body.name.strip()) is not None:
        raise HTTPException(400, f"A variable called {body.name.strip()} already exists.")
    try:
        v = variables.save(body.name, body.value, body.description, body.secret)
    except variables.VariableError as e:
        raise HTTPException(400, str(e))
    db.audit("variable_created", user.username, auth.client_ip(request), {"name": v["name"], "secret": v["secret"]})
    return v


@app.put("/api/variables/{name}")
async def variables_update(name: str, body: VariableBody, request: Request, user: User = Depends(admin_user)):
    if variables.get(name) is None:
        raise HTTPException(404, "That variable no longer exists.")
    try:
        v = variables.save(body.name, body.value, body.description, body.secret, original_name=name)
    except variables.VariableError as e:
        raise HTTPException(400, str(e))
    db.audit("variable_updated", user.username, auth.client_ip(request), {"name": v["name"], "was": name})
    return v


@app.delete("/api/variables/{name}")
async def variables_delete(name: str, request: Request, user: User = Depends(admin_user)):
    used = [d["dag_id"] for d in dags.list_all() if f"var.{name}" in json.dumps(d["tasks"])
            or any(t["operator"] == "set_variable" and t["params"].get("name") == name for t in d["tasks"])]
    variables.delete(name)
    db.audit("variable_deleted", user.username, auth.client_ip(request), {"name": name})
    return {"ok": True, "used_by": used}


# ---------------------------------------------------------------- mail accounts (admin)

class MailBody(BaseModel):
    name: str | None = None
    config: dict = {}
    password: str | None = None        # blank = keep the saved one
    clear_password: bool = False


class MailTestBody(BaseModel):
    id: int | None = None
    draft: MailBody | None = None
    send_to: str = ""


def _mail_dict(b: MailBody) -> dict:
    return b.model_dump() if hasattr(b, "model_dump") else b.dict()


def _jobs_using_mail(acc_id: int) -> list[str]:
    return [d["dag_id"] for d in dags.list_all()
            if any(str(x.get("account")) == str(acc_id) for x in d["triggers"] + [t["params"] for t in d["tasks"]])]


@app.get("/api/mail/accounts")
async def mail_list(user: User = Depends(admin_user)):
    return {"accounts": [dict(mail.public(a), used_by=_jobs_using_mail(a["id"])) for a in mail.list_all()],
            "presets": mail.PRESETS}


@app.post("/api/mail/accounts")
async def mail_create(body: MailBody, request: Request, user: User = Depends(admin_user)):
    try:
        a = mail.create(_mail_dict(body))
    except mail.MailError as e:
        raise HTTPException(400, str(e))
    db.audit("mail_account_created", user.username, auth.client_ip(request), {"id": a["id"], "name": a["name"]})
    return mail.public(a)


@app.put("/api/mail/accounts/{aid}")
async def mail_update(aid: int, body: MailBody, request: Request, user: User = Depends(admin_user)):
    try:
        a = mail.update(aid, _mail_dict(body))
    except mail.MailError as e:
        raise HTTPException(400, str(e))
    db.audit("mail_account_updated", user.username, auth.client_ip(request),
             {"id": aid, "name": a["name"], "password_changed": bool(body.password or body.clear_password)})
    return mail.public(a)


@app.delete("/api/mail/accounts/{aid}")
async def mail_delete(aid: int, request: Request, user: User = Depends(admin_user)):
    used = _jobs_using_mail(aid)
    if used:
        raise HTTPException(400, f"This account is used by: {', '.join(used)}. Change those jobs first.")
    mail.delete(aid)
    db.audit("mail_account_deleted", user.username, auth.client_ip(request), {"id": aid})
    return {"ok": True}


@app.post("/api/mail/test")
async def mail_test(body: MailTestBody, user: User = Depends(admin_user)):
    try:
        a = mail.draft(_mail_dict(body.draft), body.id) if body.draft else mail.get(body.id)
        if not a:
            return {"ok": False, "message": "That account no longer exists."}
        msg = await asyncio.wait_for(asyncio.to_thread(mail.test, a, body.send_to.strip()), 60)
        return {"ok": True, "message": msg}
    except mail.MailError as e:
        return {"ok": False, "message": str(e)}
    except asyncio.TimeoutError:
        return {"ok": False, "message": "The mail server didn't answer within 60 seconds."}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- jobs (admin)

# Jobs are DAGs (app/dags.py): edited here, run by the jobs process.
class DagBody(BaseModel):
    dag_id: str = ""
    description: str = ""
    paused: bool = False
    triggers: list = []
    tasks: list = []
    layout: dict = {}
    options: dict = {}
    code: str | None = None          # when saved from the Code view: the code wins over the fields above


class DagCodeBody(BaseModel):
    code: str
    layout: dict = {}


class PausedBody(BaseModel):
    paused: bool


def _dag_data(b: DagBody) -> dict:
    data = _dump(b)
    code = data.pop("code", None)
    if code is not None:
        parsed = dags.from_code(code)
        parsed["layout"] = data.get("layout") or {}
        return parsed
    return data


def _dags(fn, *a):
    try:
        return fn(*a)
    except jobs.JobError as e:          # DagError is a JobError
        raise HTTPException(400, str(e))


@app.get("/api/dags/catalog")
async def dags_catalog(user: User = Depends(admin_user)):
    return await asyncio.to_thread(dags.catalog)


@app.get("/api/dags")
async def dags_list(user: User = Depends(admin_user)):
    return await asyncio.to_thread(lambda: [dags.public(d) for d in dags.list_all()])


@app.get("/api/dags/{pk}")
async def dags_get(pk: int, user: User = Depends(admin_user)):
    d = dags.get(pk)
    if not d:
        raise HTTPException(404, "That DAG no longer exists.")
    return {**dags.public(d), "code": dags.to_code(d)}


def _lenient(data: dict) -> tuple[dict, str | None]:
    """Check a DAG, but let the editor switch views while it isn't valid yet (saving still checks)."""
    try:
        return dags.clean_dag(data), None
    except jobs.JobError as e:
        return dags.normalize(data), str(e)


@app.post("/api/dags/code")
async def dags_from_code(body: DagCodeBody, user: User = Depends(admin_user)):
    """Code view → Graph: read the code (nothing is saved or run). Syntax errors are refused; settings that aren't
    valid yet come back as a warning."""
    parsed = _dags(lambda: dags.from_code(body.code))
    d, warn = _lenient({**parsed, "layout": body.layout})
    return {"dag": d, "warning": warn}


@app.post("/api/dags/to-code")
async def dags_to_code(body: DagBody, user: User = Depends(admin_user)):
    """Graph → Code view."""
    d, warn = _lenient(_dump(body))
    return {"code": dags.to_code(d), "dag": d, "warning": warn}


@app.post("/api/dags")
async def dags_create(body: DagBody, request: Request, user: User = Depends(admin_user)):
    d = _dags(lambda: dags.create(_dag_data(body)))
    db.audit("dag_created", user.username, auth.client_ip(request), {"id": d["id"], "dag_id": d["dag_id"]})
    return dags.public(d)


@app.put("/api/dags/{pk}")
async def dags_update(pk: int, body: DagBody, request: Request, user: User = Depends(admin_user)):
    d = _dags(lambda: dags.update(pk, _dag_data(body)))
    db.audit("dag_updated", user.username, auth.client_ip(request), {"id": pk, "dag_id": d["dag_id"]})
    return dags.public(d)


@app.post("/api/dags/{pk}/paused")
async def dags_pause(pk: int, body: PausedBody, request: Request, user: User = Depends(admin_user)):
    if not dags.get(pk):
        raise HTTPException(404)
    dags.set_paused(pk, body.paused)
    db.audit("dag_paused" if body.paused else "dag_unpaused", user.username, auth.client_ip(request), {"id": pk})
    return {"ok": True}


@app.delete("/api/dags/{pk}")
async def dags_delete(pk: int, request: Request, user: User = Depends(admin_user)):
    d = dags.get(pk)
    if not d:
        raise HTTPException(404)
    dags.delete(pk)
    db.audit("dag_deleted", user.username, auth.client_ip(request), {"id": pk, "dag_id": d["dag_id"]})
    return {"ok": True}


@app.post("/api/dags/{pk}/trigger")
async def dags_trigger(pk: int, request: Request, user: User = Depends(admin_user)):
    """Queue a manual run; the jobs process starts it within seconds."""
    r = _dags(dags.trigger, pk, user.username)
    db.audit("dag_triggered", user.username, auth.client_ip(request), {"id": pk, "run": r["run_id"]})
    warn = None
    try:
        s = procs.summary()["workers"].get("jobs")
        if s and not s["ok"]:
            warn = "The jobs process isn't running, so the run waits until it's started (Server → Processes)."
    except Exception:  # noqa: BLE001
        pass
    return {**r, "warning": warn}


@app.post("/api/dags/runs/{run_pk}/stop")
async def dags_stop_run(run_pk: int, request: Request, user: User = Depends(admin_user)):
    _dags(dags.stop, run_pk)
    db.audit("dag_run_stopped", user.username, auth.client_ip(request), {"run": run_pk})
    return {"ok": True}


@app.get("/api/dags/{pk}/runs")
async def dags_runs(pk: int, limit: int = 25, user: User = Depends(admin_user)):
    return await asyncio.to_thread(dags.runs, pk, min(max(limit, 1), 100))


# ---- Python environments (Automation → Python environments; the "Run Python" operator)
class PyEnvBody(BaseModel):
    name: str
    python: str = ""          # "" = the server's python3, an interpreter path, or a version like 3.12 (uv)
    packages: str = ""
    description: str = ""


class PyEnvEditBody(BaseModel):
    description: str = ""


class PyEnvPkgBody(BaseModel):
    packages: str = ""
    upgrade: bool = False


class PyEnvUninstallBody(BaseModel):
    package: str


def _py(fn, *a):
    try:
        return fn(*a)
    except pyenvs.PyEnvError as e:
        raise HTTPException(400, str(e))


def _pyx(fn: str):
    """pyenvs.<fn> for this server, or device_pyenvs.<fn> for the device of the request (`?device=ID`)."""
    dev = devices.current_id()
    return functools.partial(getattr(device_pyenvs, fn), dev) if dev else getattr(pyenvs, fn)


@app.get("/api/pyenvs")
async def pyenvs_status(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_py, _pyx("status"))


@app.post("/api/pyenvs")
async def pyenvs_create(body: PyEnvBody, request: Request, user: User = Depends(admin_user)):
    t = await asyncio.to_thread(_py, _pyx("create"), body.name, body.python, body.packages, body.description)
    db.audit("pyenv_created", user.username, auth.client_ip(request), {"name": body.name, "python": body.python or "base", "device": devices.current_id(),
                                                                      "packages": body.packages[:500]})
    return t


@app.post("/api/pyenvs/uv")
async def pyenvs_install_uv(request: Request, user: User = Depends(admin_user)):
    t = await asyncio.to_thread(_py, _pyx("install_uv"))
    db.audit("uv_installed", user.username, auth.client_ip(request))
    return t


class PythonVersionBody(BaseModel):
    version: str


class PythonKeyBody(BaseModel):
    key: str


@app.get("/api/pyenvs/pythons")
async def pyenvs_pythons(refresh: bool = False, user: User = Depends(admin_user)):
    return await asyncio.to_thread(_py, _pyx("python_versions"), refresh)


@app.post("/api/pyenvs/pythons")
async def pyenvs_python_install(body: PythonVersionBody, request: Request, user: User = Depends(admin_user)):
    t = await asyncio.to_thread(_py, _pyx("install_python"), body.version)
    db.audit("python_downloaded", user.username, auth.client_ip(request), {"version": body.version})
    return t


@app.post("/api/pyenvs/pythons/remove")
async def pyenvs_python_remove(body: PythonKeyBody, request: Request, user: User = Depends(admin_user)):
    t = await asyncio.to_thread(_py, _pyx("uninstall_python"), body.key)
    db.audit("python_removed", user.username, auth.client_ip(request), {"key": body.key})
    return t


@app.get("/api/pyenvs/tasks/{tid}")
async def pyenvs_task(tid: str, user: User = Depends(admin_user)):
    return _py(pyenvs.task, tid)


@app.put("/api/pyenvs/{name}")
async def pyenvs_edit(name: str, body: PyEnvEditBody, request: Request, user: User = Depends(admin_user)):
    e = await asyncio.to_thread(_py, _pyx("update"), name, body.description)
    db.audit("pyenv_updated", user.username, auth.client_ip(request), {"name": name})
    return e


@app.delete("/api/pyenvs/{name}")
async def pyenvs_delete(name: str, request: Request, user: User = Depends(admin_user)):
    used = device_pyenvs.used_by(devices.current_id(), name) if devices.current_id() else pyenvs.used_by(name)
    await asyncio.to_thread(_py, _pyx("delete"), name)
    db.audit("pyenv_deleted", user.username, auth.client_ip(request), {"name": name, "used_by": used})
    return {"ok": True, "used_by": used}


@app.get("/api/pyenvs/{name}/packages")
async def pyenvs_packages(name: str, user: User = Depends(admin_user)):
    return await asyncio.to_thread(_py, _pyx("packages"), name)


@app.post("/api/pyenvs/{name}/install")
async def pyenvs_install(name: str, body: PyEnvPkgBody, request: Request, user: User = Depends(admin_user)):
    t = await asyncio.to_thread(_py, _pyx("install"), name, body.packages, body.upgrade)
    db.audit("pyenv_packages_installed", user.username, auth.client_ip(request), {"name": name, "packages": body.packages[:500]})
    return t


@app.post("/api/pyenvs/{name}/uninstall")
async def pyenvs_uninstall(name: str, body: PyEnvUninstallBody, request: Request, user: User = Depends(admin_user)):
    t = await asyncio.to_thread(_py, _pyx("uninstall"), name, body.package)
    db.audit("pyenv_package_removed", user.username, auth.client_ip(request), {"name": name, "package": body.package})
    return t


# ---- SSH keys (Server → SSH keys)
class SshKeyBody(BaseModel):
    name: str
    type: str = "ed25519"
    comment: str = ""
    description: str = ""


class SshImportBody(BaseModel):
    name: str
    private_key: str
    public_key: str = ""
    description: str = ""


class SshEditBody(BaseModel):
    new_name: str = ""
    description: str | None = None


class SshDefaultBody(BaseModel):
    name: str = ""


class SshHostBody(BaseModel):
    host: str


def _ssh(fn, *a):
    try:
        return fn(*a)
    except ssh_keys.SshKeyError as e:
        raise HTTPException(400, str(e))


@app.get("/api/ssh")
async def ssh_status(user: User = Depends(admin_user)):
    return await asyncio.to_thread(ssh_keys.status)


@app.post("/api/ssh/keys")
async def ssh_generate(body: SshKeyBody, request: Request, user: User = Depends(admin_user)):
    k = await asyncio.to_thread(_ssh, ssh_keys.generate, body.name, body.type, body.comment, body.description)
    db.audit("ssh_key_created", user.username, auth.client_ip(request), {"name": k["name"], "type": body.type,
                                                                         "fingerprint": k.get("fingerprint")})
    return k


@app.post("/api/ssh/keys/import")
async def ssh_import(body: SshImportBody, request: Request, user: User = Depends(admin_user)):
    k = await asyncio.to_thread(_ssh, ssh_keys.import_key, body.name, body.private_key, body.public_key, body.description)
    db.audit("ssh_key_imported", user.username, auth.client_ip(request), {"name": k["name"], "fingerprint": k.get("fingerprint")})
    return k


@app.put("/api/ssh/default")
async def ssh_default(body: SshDefaultBody, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_ssh, ssh_keys.set_default, body.name)
    db.audit("ssh_key_default", user.username, auth.client_ip(request), {"name": body.name})
    return {"ok": True, "default": body.name}


@app.post("/api/ssh/known-hosts/remove")
async def ssh_forget_host(body: SshHostBody, request: Request, user: User = Depends(admin_user)):
    n = await asyncio.to_thread(_ssh, ssh_keys.forget_host, body.host)
    db.audit("ssh_known_host_removed", user.username, auth.client_ip(request), {"host": body.host})
    return {"ok": True, "removed": n}


@app.put("/api/ssh/keys/{name}")
async def ssh_edit(name: str, body: SshEditBody, request: Request, user: User = Depends(admin_user)):
    k = await asyncio.to_thread(_ssh, ssh_keys.update, name, body.new_name, body.description)
    db.audit("ssh_key_updated", user.username, auth.client_ip(request), {"name": name, "new_name": body.new_name or None})
    return k


@app.delete("/api/ssh/keys/{name}")
async def ssh_delete(name: str, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_ssh, ssh_keys.delete, name)
    db.audit("ssh_key_deleted", user.username, auth.client_ip(request), {"name": name})
    return {"ok": True}


# ---------------------------------------------------------------- devices (other computers controlled over SSH)

def _dev(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except devices.DeviceError as e:
        raise HTTPException(400, str(e))


class DeviceBody(BaseModel):
    name: str = ""
    host: str = ""
    port: int = 22
    user: str = ""
    key_name: str = ""
    description: str = ""


@app.get("/api/devices")
async def devices_list(live: bool = False, user: User = Depends(admin_user)):
    """The devices (live=1: connect to each one and read its resources now)."""
    def run():
        rows = devices.overview() if live else [devices.public(d) for d in devices.list_devices()]
        return {"devices": rows, "modules": devices.modules_catalog(), "ssh": bool(shutil.which("ssh")),
                "keys": [k["name"] for k in ssh_keys.list_keys()], "default_key": ssh_keys.default_key()}
    return await asyncio.to_thread(run)


@app.post("/api/devices")
async def devices_create(body: DeviceBody, request: Request, user: User = Depends(admin_user)):
    d = await asyncio.to_thread(_dev, devices.create, _mail_dict(body))
    db.audit("device_added", user.username, auth.client_ip(request), {"name": d["name"], "host": d["host"], "user": d["user"]})
    return devices.public(d)


@app.post("/api/devices/test")
async def devices_test(body: DeviceBody, user: User = Depends(admin_user)):
    return await asyncio.to_thread(_dev, devices.test, _mail_dict(body))


@app.get("/api/devices/{dev_id}")
async def devices_one(dev_id: int, user: User = Depends(admin_user)):
    d = await asyncio.to_thread(_dev, devices.stats, dev_id)
    d["used_by"] = devices.used_by(dev_id)
    return d


@app.put("/api/devices/{dev_id}")
async def devices_update(dev_id: int, body: DeviceBody, request: Request, user: User = Depends(admin_user)):
    d = await asyncio.to_thread(_dev, devices.update, dev_id, _mail_dict(body))
    db.audit("device_updated", user.username, auth.client_ip(request), {"id": dev_id, "name": d["name"], "host": d["host"]})
    return devices.public(d)


@app.delete("/api/devices/{dev_id}")
async def devices_delete(dev_id: int, request: Request, user: User = Depends(admin_user)):
    name = _dev(devices.get, dev_id)["name"]
    await asyncio.to_thread(_dev, devices.delete, dev_id)
    db.audit("device_deleted", user.username, auth.client_ip(request), {"id": dev_id, "name": name})
    return {"ok": True}


@app.post("/api/devices/{dev_id}/refresh")
async def devices_refresh(dev_id: int, user: User = Depends(admin_user)):
    """Reconnect (after installing a module: a new docker group needs a new login) and read everything again."""
    def run():
        devices.reset_connections(devices.get(dev_id))
        return devices.probe(dev_id, refresh_info=True)
    return await asyncio.to_thread(_dev, run)


@app.get("/api/ssh/keys/{name}/public")
async def ssh_public(name: str, user: User = Depends(admin_user)):
    text = _ssh(ssh_keys.public_key, name)
    return Response(text + "\n", media_type="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="{name}.pub"'})


@app.get("/api/ssh/keys/{name}/private")
async def ssh_private(name: str, request: Request, user: User = Depends(admin_user)):
    text = _ssh(ssh_keys.private_key, name)
    db.audit("ssh_key_downloaded", user.username, auth.client_ip(request), {"name": name})
    return Response(text, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# ---- processes (Server → Processes)
@app.get("/api/processes/summary")
async def processes_summary(user: User = Depends(current_user)):
    """Which background processes work (for every signed-in user: modules show a notice when theirs is down)."""
    return {**await asyncio.to_thread(procs.summary), "is_admin": user.is_admin}


@app.get("/api/processes")
async def processes_status(user: User = Depends(admin_user)):
    try:
        return await asyncio.to_thread(procs.status)
    except procs.ProcessError as e:
        raise HTTPException(503, str(e))


@app.post("/api/processes/restart-all")
async def processes_restart_all(request: Request, user: User = Depends(admin_user)):
    db.audit("processes_restart_all", user.username, auth.client_ip(request))
    procs.restart_app(delay=1.0)
    return {"ok": True}


@app.post("/api/processes/{name}/{action}")
async def processes_control(name: str, action: str, request: Request, user: User = Depends(admin_user)):
    try:
        st = await asyncio.to_thread(procs.control, name, action)
    except procs.ProcessError as e:
        raise HTTPException(400, str(e))
    db.audit("process_" + action, user.username, auth.client_ip(request), {"process": name})
    return st


@app.get("/api/processes/{name}/logs")
async def processes_logs(name: str, lines: int = 200, user: User = Depends(admin_user)):
    try:
        return await asyncio.to_thread(procs.logs, name, lines)
    except procs.ProcessError as e:
        raise HTTPException(400, str(e))


@app.on_event("startup")
async def _start_background() -> None:
    """The jobs scheduler and the network watcher run in their own processes (app.supervisor); only in
    single-process mode (`python -m app --single`) does the web process run them too."""
    dags.migrate_jobs()
    if procs.owns("jobs"):
        dags.SCHEDULER.start()
    if procs.owns("network"):
        network.ENGINE.start()
    if procs.owns("names"):
        lan_names.ENGINE.start()
    if procs.supervised():
        asyncio.get_running_loop().create_task(procs.heartbeat_loop())


# ---------------------------------------------------------------- network (admin)

def _dump(b, **kw) -> dict:
    return b.model_dump(**kw) if hasattr(b, "model_dump") else b.dict(**kw)


def _net(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except (network.NetworkError, routers.RouterError, publish.PublishError, containers.DockerError, firewall.FirewallError,
            lan_names.NamesError) as e:
        raise HTTPException(400, str(e))


class NetScanBody(BaseModel):
    deep: bool = True


class NetDeviceBody(BaseModel):
    label: str | None = None
    kind: str | None = None
    notes: str | None = None
    known: bool | None = None


class NetSettingsBody(BaseModel):
    watch: bool = True
    interval: int = 15
    extra: str = ""


class RouterBody(BaseModel):
    type: str
    address: str = ""
    username: str = ""
    password: str | None = None      # None = keep the saved one
    verify_tls: bool = False


class StaticLeaseBody(BaseModel):
    mac: str
    ip: str
    name: str = ""


class IdBody(BaseModel):
    id: str


class DhcpBody(BaseModel):
    start: str
    end: str
    lease_time: str
    dns: str = ""


class ForwardBody(BaseModel):
    name: str = ""
    proto: str = "tcp"
    ext_port: int
    int_ip: str
    int_port: int


class CfBody(BaseModel):
    token: str
    zone_id: str = ""


class CaddyBody(BaseModel):
    email: str = ""
    forward_ports: bool = True


class PublishServiceBody(BaseModel):
    hostname: str
    target: str
    method: str
    allow_admin: bool = False


class DdnsBody(BaseModel):
    provider: str = ""
    hostnames: str = ""
    token: str | None = None


@app.get("/api/network")
async def net_overview(user: User = Depends(admin_user)):
    return await asyncio.to_thread(network.overview)


@app.get("/api/network/devices")
async def net_devices(user: User = Depends(admin_user)):
    return await asyncio.to_thread(network.devices)


@app.get("/api/network/scan")
async def net_scan_progress(user: User = Depends(admin_user)):
    return network.progress()


@app.post("/api/network/scan")
async def net_scan(body: NetScanBody, request: Request, user: User = Depends(admin_user)):
    db.audit("network_scan", user.username, auth.client_ip(request))
    return await asyncio.to_thread(_net, network.scan, body.deep)


@app.patch("/api/network/devices/{mac}")
async def net_device_update(mac: str, body: NetDeviceBody, request: Request, user: User = Depends(admin_user)):
    d = await asyncio.to_thread(_net, network.update_device, mac, body.label, body.kind, body.notes, body.known)
    db.audit("network_device_updated", user.username, auth.client_ip(request), {"mac": mac, **_dump(body, exclude_none=True)})
    return d


@app.delete("/api/network/devices/{mac}")
async def net_device_forget(mac: str, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(network.forget, mac)
    db.audit("network_device_forgotten", user.username, auth.client_ip(request), {"mac": mac})
    return {"ok": True}


@app.post("/api/network/devices/{mac}/ports")
async def net_device_port_scan(mac: str, request: Request, user: User = Depends(admin_user)):
    db.audit("network_port_scan", user.username, auth.client_ip(request), {"mac": mac})
    return await asyncio.to_thread(_net, network.scan_all_ports, mac)


@app.get("/api/network/devices/{mac}/ports")
async def net_device_port_scan_status(mac: str, user: User = Depends(admin_user)):
    return network.port_scan_status(mac)


def _protected_macs(request: Request) -> dict[str, str]:
    """Devices that must not be kicked or blocked: this server, the router, and the admin's own device."""
    me = auth.client_ip(request)
    out = {}
    for d in network.devices():
        if d["kind"] == "this_server":
            out[d["mac"]] = "That's this server: blocking it would cut SysAdmin.IA off the network."
        elif d["kind"] == "router":
            out[d["mac"]] = "That's the router itself."
        elif d["ip"] and d["ip"] == me:
            out[d["mac"]] = "That's the device you're using right now: you would lock yourself out."
    return out


class MacBody(BaseModel):
    mac: str
    name: str = ""


@app.get("/api/network/router/blocked")
async def net_blocked(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_net, routers.blocked)


@app.post("/api/network/router/block")
async def net_block(body: MacBody, request: Request, user: User = Depends(admin_user)):
    prot = await asyncio.to_thread(_protected_macs, request)
    await asyncio.to_thread(_net, routers.block, body.mac, body.name, prot)
    db.audit("device_blocked", user.username, auth.client_ip(request), _dump(body))
    return {"ok": True}


@app.post("/api/network/router/unblock")
async def net_unblock(body: MacBody, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_net, routers.unblock, body.mac)
    db.audit("device_unblocked", user.username, auth.client_ip(request), _dump(body))
    return {"ok": True}


@app.post("/api/network/router/kick")
async def net_kick(body: MacBody, request: Request, user: User = Depends(admin_user)):
    prot = await asyncio.to_thread(_protected_macs, request)
    await asyncio.to_thread(_net, routers.kick, body.mac, prot)
    db.audit("device_kicked", user.username, auth.client_ip(request), _dump(body))
    return {"ok": True}


# ---- names on the LAN and websites on shared ports (Network → Names)
class LanNameBody(BaseModel):
    name: str
    ip: str
    port: int | None = None          # with target_port: name:port → ip:target_port through the proxy
    target_port: int | None = None
    https: bool = False


class LanNameRemoveBody(BaseModel):
    id: str


class LanSiteBody(BaseModel):
    hostname: str
    target: str
    https: bool = False
    make_name: bool = True
    port: int | None = None          # where the proxy listens (default 80, or 443 for HTTPS)


@app.get("/api/network/names")
async def net_names(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_net, lan_names.list_names)


@app.post("/api/network/names")
async def net_names_add(body: LanNameBody, request: Request, user: User = Depends(admin_user)):
    out = await asyncio.to_thread(_net, lan_names.add_name_with_ports, body.name, body.ip, body.port, body.target_port, body.https)
    db.audit("lan_name_added", user.username, auth.client_ip(request), _dump(body))
    return out


class LanNameEditBody(BaseModel):
    id: str
    name: str
    ip: str


@app.put("/api/network/names")
async def net_names_edit(body: LanNameEditBody, request: Request, user: User = Depends(admin_user)):
    out = await asyncio.to_thread(_net, lan_names.update_name, body.id, body.name, body.ip)
    db.audit("lan_name_changed", user.username, auth.client_ip(request), _dump(body))
    return out


@app.post("/api/network/names/remove")
async def net_names_remove(body: LanNameRemoveBody, request: Request, user: User = Depends(admin_user)):
    out = await asyncio.to_thread(_net, lan_names.remove_name, body.id)
    db.audit("lan_name_removed", user.username, auth.client_ip(request), {"id": body.id})
    return out


@app.get("/api/network/sites")
async def net_sites(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_net, lan_names.sites_status)


@app.post("/api/network/sites")
async def net_sites_add(body: LanSiteBody, request: Request, user: User = Depends(admin_user)):
    out = await asyncio.to_thread(_net, lan_names.add_site, body.hostname, body.target, body.https, body.make_name, body.port)
    db.audit("lan_site_added", user.username, auth.client_ip(request), _dump(body))
    return out


@app.put("/api/network/sites/{sid}")
async def net_sites_edit(sid: int, body: LanSiteBody, request: Request, user: User = Depends(admin_user)):
    out = await asyncio.to_thread(_net, lan_names.update_site, sid, body.hostname, body.target, body.https, body.port)
    db.audit("lan_site_changed", user.username, auth.client_ip(request), {"id": sid, **_dump(body)})
    return out


@app.delete("/api/network/sites/{sid}")
async def net_sites_remove(sid: int, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_net, lan_names.remove_site, sid)
    db.audit("lan_site_removed", user.username, auth.client_ip(request), {"id": sid})
    return {"ok": True}


@app.get("/api/network/sites/ca.crt")
async def net_sites_ca(user: User = Depends(admin_user)):
    pem = await asyncio.to_thread(publish.local_ca_cert)
    if not pem:
        raise HTTPException(404, "The local certificate authority doesn't exist yet (add an HTTPS website first).")
    return Response(pem, media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="sysadmin-ia-local-ca.crt"'})


# ---- this server's firewall (Network → Ports)
class FirewallPortBody(BaseModel):
    port: str
    proto: str = "tcp"
    note: str = ""


class FirewallRuleBody(BaseModel):
    id: str


class FirewallFlagBody(BaseModel):
    value: bool


@app.get("/api/network/firewall")
async def net_firewall(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_net, firewall.status)


@app.post("/api/network/firewall/open")
async def net_firewall_open(body: FirewallPortBody, request: Request, user: User = Depends(admin_user)):
    out = await asyncio.to_thread(_net, firewall.open_port, body.port, body.proto, body.note)
    db.audit("firewall_port_opened", user.username, auth.client_ip(request), _dump(body))
    return out


@app.post("/api/network/firewall/remove")
async def net_firewall_remove(body: FirewallRuleBody, request: Request, user: User = Depends(admin_user)):
    out = await asyncio.to_thread(_net, firewall.remove_rule, body.id)
    db.audit("firewall_rule_removed", user.username, auth.client_ip(request), {"rule": body.id})
    return out


@app.post("/api/network/firewall/default")
async def net_firewall_default(body: FirewallFlagBody, request: Request, user: User = Depends(admin_user)):
    out = await asyncio.to_thread(_net, firewall.set_default, body.value)
    db.audit("firewall_default", user.username, auth.client_ip(request), {"closed": body.value})
    return out


@app.post("/api/network/firewall/enabled")
async def net_firewall_enabled(body: FirewallFlagBody, request: Request, user: User = Depends(admin_user)):
    out = await asyncio.to_thread(_net, firewall.set_enabled, body.value)
    db.audit("firewall_enabled", user.username, auth.client_ip(request), {"on": body.value})
    return out


@app.post("/api/network/devices/known-all")
async def net_devices_known(request: Request, user: User = Depends(admin_user)):
    n = await asyncio.to_thread(network.mark_all_known)
    db.audit("network_devices_known", user.username, auth.client_ip(request), {"count": n})
    return {"ok": True, "count": n}


@app.put("/api/network/settings")
async def net_settings(body: NetSettingsBody, request: Request, user: User = Depends(admin_user)):
    s = _net(network.save_settings, body.watch, body.interval, body.extra)
    db.audit("network_settings", user.username, auth.client_ip(request), s)
    return s


@app.get("/api/network/router")
async def net_router(user: User = Depends(admin_user)):
    return await asyncio.to_thread(routers.status)


@app.get("/api/network/router/detect")
async def net_router_detect(user: User = Depends(admin_user)):
    return await asyncio.to_thread(routers.detect)


@app.put("/api/network/router")
async def net_router_save(body: RouterBody, request: Request, user: User = Depends(admin_user)):
    _net(routers.save_config, body.type, body.address, body.username, body.password, body.verify_tls)
    db.audit("router_settings", user.username, auth.client_ip(request), {"type": body.type, "address": body.address,
                                                                         "username": body.username, "password_changed": body.password is not None})
    return await asyncio.to_thread(routers.status)


@app.delete("/api/network/router")
async def net_router_forget(request: Request, user: User = Depends(admin_user)):
    routers.forget_config()
    db.audit("router_forgotten", user.username, auth.client_ip(request))
    return {"ok": True}


@app.get("/api/network/router/leases")
async def net_leases(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_net, routers.leases)


@app.post("/api/network/router/static")
async def net_static_add(body: StaticLeaseBody, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_net, routers.add_static, body.mac, body.ip, body.name)
    db.audit("dhcp_reservation_added", user.username, auth.client_ip(request), _dump(body))
    return {"ok": True}


@app.post("/api/network/router/static/remove")
async def net_static_remove(body: IdBody, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_net, routers.remove_static, body.id)
    db.audit("dhcp_reservation_removed", user.username, auth.client_ip(request), {"id": body.id})
    return {"ok": True}


@app.get("/api/network/router/dhcp")
async def net_dhcp(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_net, routers.dhcp_settings)


@app.put("/api/network/router/dhcp")
async def net_dhcp_save(body: DhcpBody, request: Request, user: User = Depends(admin_user)):
    r = await asyncio.to_thread(_net, routers.set_dhcp_settings, body.start, body.end, body.lease_time, body.dns)
    db.audit("dhcp_settings", user.username, auth.client_ip(request), _dump(body))
    return r


@app.get("/api/network/router/forwards")
async def net_forwards(user: User = Depends(admin_user)):
    return await asyncio.to_thread(_net, routers.forwards)


@app.post("/api/network/router/forwards")
async def net_forward_add(body: ForwardBody, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_net, routers.add_forward, body.name, body.proto, body.ext_port, body.int_ip, body.int_port)
    db.audit("port_forward_added", user.username, auth.client_ip(request), _dump(body))
    return {"ok": True}


@app.post("/api/network/router/forwards/remove")
async def net_forward_remove(body: IdBody, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_net, routers.remove_forward, body.id)
    db.audit("port_forward_removed", user.username, auth.client_ip(request), {"id": body.id})
    return {"ok": True}


@app.get("/api/network/publish")
async def net_publish(check: bool = False, user: User = Depends(admin_user)):
    return await asyncio.to_thread(publish.status, check)


@app.get("/api/network/publish/targets")
async def net_publish_targets(user: User = Depends(admin_user)):
    return await asyncio.to_thread(publish.targets)


@app.post("/api/network/publish/cloudflare/check")
async def net_cf_check(body: CfBody, user: User = Depends(admin_user)):
    return await asyncio.to_thread(_net, publish.cf_check, body.token)


@app.post("/api/network/publish/cloudflare")
async def net_cf_connect(body: CfBody, request: Request, user: User = Depends(admin_user)):
    r = await asyncio.to_thread(_net, publish.cf_connect, body.token, body.zone_id)
    db.audit("publish_cloudflare_connected", user.username, auth.client_ip(request), {"zone": r["cloudflare"].get("zone_name")})
    return r


@app.delete("/api/network/publish/cloudflare")
async def net_cf_disconnect(request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_net, publish.cf_disconnect)
    db.audit("publish_cloudflare_disconnected", user.username, auth.client_ip(request))
    return {"ok": True}


@app.post("/api/network/publish/caddy")
async def net_caddy(body: CaddyBody, request: Request, user: User = Depends(admin_user)):
    r = await asyncio.to_thread(_net, publish.caddy_setup, body.email, body.forward_ports)
    db.audit("publish_caddy_setup", user.username, auth.client_ip(request), _dump(body))
    return r


@app.delete("/api/network/publish/caddy")
async def net_caddy_remove(request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_net, publish.caddy_remove)
    db.audit("publish_caddy_removed", user.username, auth.client_ip(request))
    return {"ok": True}


@app.post("/api/network/publish/services")
async def net_publish_add(body: PublishServiceBody, request: Request, user: User = Depends(admin_user)):
    s = await asyncio.to_thread(_net, publish.add_service, body.hostname, body.target, body.method, body.allow_admin)
    db.audit("service_published", user.username, auth.client_ip(request), _dump(body))
    return s


@app.delete("/api/network/publish/services/{sid}")
async def net_publish_remove(sid: int, request: Request, user: User = Depends(admin_user)):
    await asyncio.to_thread(_net, publish.remove_service, sid)
    db.audit("service_unpublished", user.username, auth.client_ip(request), {"id": sid})
    return {"ok": True}


@app.put("/api/network/publish/ddns")
async def net_ddns(body: DdnsBody, request: Request, user: User = Depends(admin_user)):
    r = await asyncio.to_thread(_net, publish.ddns_save, body.provider, body.hostnames, body.token)
    db.audit("dynamic_dns_settings", user.username, auth.client_ip(request), {"provider": body.provider, "hostnames": body.hostnames})
    return r


# ---------------------------------------------------------------- drives (admin)

class MountBody(BaseModel):
    device: str
    name: str
    persist: bool = False
    readonly: bool = False


class UnmountBody(BaseModel):
    mountpoint: str
    forget: bool = True


# Drives and files: this server, or a device (`?device=ID`, see DeviceTarget) through its agent over SSH.
@app.get("/api/drives")
async def get_drives(user: User = Depends(admin_user)):
    dev = devices.current_id()
    if dev:
        return await asyncio.to_thread(_dev, devices.drives, dev)
    try:
        return await asyncio.to_thread(drives.list_drives)
    except drives.DriveError as e:
        raise HTTPException(500, str(e))


@app.post("/api/drives/mount")
async def mount_drive(body: MountBody, request: Request, user: User = Depends(admin_user)):
    dev = devices.current_id()
    try:
        if dev:
            r = await asyncio.to_thread(devices.mount, dev, body.device, body.name, body.persist, body.readonly)
        else:
            r = await asyncio.to_thread(drives.mount, body.device, body.name, body.persist, body.readonly)
    except (ValueError, RuntimeError) as e:
        db.audit("drive_mount_failed", user.username, auth.client_ip(request), {"device": body.device, "error": str(e), "on": dev})
        raise HTTPException(400, str(e))
    db.audit("drive_mount", user.username, auth.client_ip(request),
             {"device": body.device, "mountpoint": r["mountpoint"], "persist": body.persist, "readonly": body.readonly, "on": dev})
    return r


@app.post("/api/drives/unmount")
async def unmount_drive(body: UnmountBody, request: Request, user: User = Depends(admin_user)):
    dev = devices.current_id()
    try:
        if dev:
            r = await asyncio.to_thread(devices.unmount, dev, body.mountpoint, body.forget)
        else:
            r = await asyncio.to_thread(drives.unmount, body.mountpoint, body.forget)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    db.audit("drive_unmount", user.username, auth.client_ip(request), {"mountpoint": r["mountpoint"], "forget": body.forget, "on": dev})
    return r


@app.get("/api/files")
async def list_files(path: str = "/", hidden: bool = False, user: User = Depends(admin_user)):
    dev = devices.current_id()
    if dev:
        return await asyncio.to_thread(_dev, devices.list_dir, dev, path, hidden)
    try:
        return await asyncio.to_thread(drives.list_dir, path, hidden)
    except drives.DriveError as e:
        raise HTTPException(400, str(e))


@app.post("/api/files/sizes")
async def file_sizes(path: str, user: User = Depends(admin_user)):
    dev = devices.current_id()
    if dev:
        return await asyncio.to_thread(_dev, devices.folder_sizes, dev, path)
    try:
        return await asyncio.to_thread(drives.folder_sizes, path)
    except drives.DriveError as e:
        raise HTTPException(400, str(e))


@app.get("/api/files/download")
async def download_file(path: str, request: Request, user: User = Depends(admin_user)):
    dev = devices.current_id()
    if dev:
        name, chunks = await asyncio.to_thread(_dev, devices.file_stream, dev, path)
        db.audit("file_download", user.username, auth.client_ip(request), {"path": path, "device": dev})
        return StreamingResponse(iterate_in_threadpool(chunks), media_type="application/octet-stream",
                                 headers={"Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(name)})
    try:
        p = drives.file_for_download(path)
    except drives.DriveError as e:
        raise HTTPException(400, str(e))
    db.audit("file_download", user.username, auth.client_ip(request), str(p))
    return FileResponse(p, filename=p.name, media_type="application/octet-stream")


# ---------------------------------------------------------------- server name (admin)

class ServerNameBody(BaseModel):
    name: str


@app.get("/api/server-name")
async def get_server_name(user: User = Depends(admin_user)):
    return await asyncio.to_thread(server_name.status)


@app.put("/api/server-name")
async def put_server_name(body: ServerNameBody, request: Request, user: User = Depends(admin_user)):
    ip = auth.client_ip(request)
    old = server_name.current()
    try:
        result = await asyncio.to_thread(server_name.set_name, body.name)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    db.audit("server_name", user.username, ip, {"from": old, "to": result["name"]})
    # A new certificate is only loaded at startup.
    result["restarting"] = result["cert_changed"] and updater.under_supervisor()
    if result["restarting"]:
        _schedule_restart()
    return {**result, "status": await asyncio.to_thread(server_name.status)}


class TimezoneBody(BaseModel):
    timezone: str


@app.get("/api/server/timezone")
async def get_timezone(user: User = Depends(admin_user)):
    return await asyncio.to_thread(server_name.timezone_status)


@app.put("/api/server/timezone")
async def put_timezone(body: TimezoneBody, request: Request, user: User = Depends(admin_user)):
    old = await asyncio.to_thread(server_name.get_timezone)
    try:
        r = await asyncio.to_thread(server_name.set_timezone, body.timezone)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    db.audit("server_timezone", user.username, auth.client_ip(request), {"from": old, "to": r["timezone"]})
    return {**r, "status": await asyncio.to_thread(server_name.timezone_status)}


@app.post("/api/server/reboot")
async def reboot_server(request: Request, user: User = Depends(admin_user)):
    try:
        out = await asyncio.to_thread(server_name.reboot)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    db.audit("server_reboot", user.username, auth.client_ip(request), out)
    return {"ok": True, "output": out}


# ---------------------------------------------------------------- updates (admin)

class UpdateSettingsBody(BaseModel):
    github_token: str | None = None   # new token; omitted/None = unchanged
    clear_github_token: bool = False
    channel: str | None = None        # "public" (releases) or "developer" (version tags); None = unchanged


@app.get("/api/version")
async def version(user: User = Depends(current_user)):
    return {"version": updater.current_version()}


@app.get("/api/update")
async def update_status(user: User = Depends(admin_user)):
    """Configuration and the last check result (does not contact GitHub)."""
    return await asyncio.to_thread(updater.status)


@app.post("/api/update/check")
async def update_check(force: bool = True, user: User = Depends(admin_user)):
    return await updater.check(force=force)


@app.put("/api/update/settings")
async def update_settings(body: UpdateSettingsBody, request: Request, user: User = Depends(admin_user)):
    try:
        if body.clear_github_token:
            updater.set_token(None)
            db.audit("update_settings", user.username, auth.client_ip(request), "github_token cleared")
        elif body.github_token:
            updater.set_token(body.github_token)
            db.audit("update_settings", user.username, auth.client_ip(request), "github_token set")
        if body.channel is not None and body.channel != updater.channel():
            updater.set_channel(body.channel)
            db.audit("update_settings", user.username, auth.client_ip(request), f"channel {body.channel}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await asyncio.to_thread(updater.status)


class UpdateApplyBody(BaseModel):
    version: str | None = None      # a published version; None = the newest
    downgrade: bool = False         # required to install an older version


@app.post("/api/update/apply")
async def update_apply(request: Request, body: UpdateApplyBody | None = None, user: User = Depends(admin_user)):
    ip = auth.client_ip(request)
    before = updater.current_version()
    body = body or UpdateApplyBody()
    try:
        result = await updater.apply(body.version, allow_downgrade=body.downgrade)
    except updater.UpdateError as e:
        db.audit("update_failed", user.username, ip, {"from": before, "to": body.version, "error": str(e)})
        raise HTTPException(400, str(e))
    kind = "downgrade" if result.get("downgrade") else "update"
    if result["mode"] == "full":
        # The root helper runs the new install.sh in the background; it restarts the app.
        db.audit(f"{kind}_started", user.username, ip, {"from": result["from"], "to": result["to"]})
        return result
    db.audit(kind, user.username, ip, {"from": result["from"], "to": result["to"],
                                       "files": len(result["changed"]), "mode": "code"})
    if result["restart"]:
        _schedule_restart()
    return result


@app.get("/api/update/progress")
async def update_progress(user: User = Depends(admin_user)):
    return {**await asyncio.to_thread(updater.progress), "current": updater.current_version()}


@app.get("/api/audit")
async def audit_log(limit: int = 200, user: User = Depends(admin_user)):
    rows = db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 1000),))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- monitoring

@app.get("/api/host")
async def host(user: User = Depends(current_user)):
    return monitor.host_info()


@app.get("/api/stats")
async def stats(user: User = Depends(current_user)):
    return await asyncio.to_thread(monitor.stats)


@app.get("/api/processes")
async def processes(sort: str = "cpu", limit: int = 25, user: User = Depends(current_user)):
    return await asyncio.to_thread(monitor.processes, sort, min(max(limit, 1), 200))


@app.get("/api/services")
async def services(user: User = Depends(current_user)):
    return await monitor.services()


# System processes (Server → Processes, "Runs on"): this server's, or a device's (`?device=ID`) over SSH.
@app.get("/api/sysprocs")
async def system_processes(sort: str = "cpu", limit: int = 60, user: User = Depends(admin_user)):
    dev = devices.current_id()
    if dev:
        return await asyncio.to_thread(_dev, devices.processes, dev, sort, limit)
    return await asyncio.to_thread(monitor.system_processes, sort, limit)


class SignalBody(BaseModel):
    signal: str = "TERM"


@app.post("/api/sysprocs/{pid}/signal")
async def system_process_signal(pid: int, body: SignalBody, request: Request, user: User = Depends(admin_user)):
    dev = devices.current_id()
    try:
        r = await asyncio.to_thread(devices.signal if dev else monitor.signal_process, *((dev,) if dev else ()), pid, body.signal)
    except (ValueError, devices.DeviceError) as e:
        raise HTTPException(400, str(e))
    db.audit("process_signal", user.username, auth.client_ip(request), {"pid": pid, "signal": body.signal.upper(), "device": dev})
    return r


# ---------------------------------------------------------------- conversations

class ConvBody(BaseModel):
    title: str = "New chat"


@app.get("/api/conversations")
async def conversations(user: User = Depends(admin_user)):
    return db.list_conversations(user.id)


@app.post("/api/conversations")
async def new_conversation(body: ConvBody, user: User = Depends(admin_user)):
    cid = db.create_conversation(user.id, body.title or "New chat")
    return {"id": cid, "title": body.title}


@app.get("/api/conversations/{cid}")
async def get_conversation(cid: int, user: User = Depends(admin_user)):
    conv = db.get_conversation(cid, user.id)
    if not conv:
        raise HTTPException(404)
    return {"id": cid, "title": conv["title"], "connection_id": conv.get("connection_id"),
            "items": agent.render_history(conv["messages"])}


@app.patch("/api/conversations/{cid}")
async def rename_conversation(cid: int, body: ConvBody, user: User = Depends(admin_user)):
    db.rename_conversation(cid, user.id, body.title)
    return {"ok": True}


@app.delete("/api/conversations/{cid}")
async def delete_conversation(cid: int, user: User = Depends(admin_user)):
    db.delete_conversation(cid, user.id)
    await agent.drop_bash(cid)
    return {"ok": True}


# ---------------------------------------------------------------- chat websocket

def _schedule_restart() -> None:
    """Restart every process with the new code: the supervisor stops them all and exits, and systemd
    (Restart=always) starts it again (new group memberships apply too)."""
    procs.restart_app(delay=1.0)


@app.websocket("/ws/console")
async def console_ws(ws: WebSocket, mode: str = "shell", id: str = "", host: str = "", port: int = 22, user_name: str = "",
                     key: str = "", install_key: str = "", device: int = 0, module: str = ""):
    user = auth.websocket_user(ws)
    if not user or not user.is_admin or not settings.allow_console:
        await ws.close(code=4401 if not user else 4403)
        return
    mode = mode if mode in ("update", "container", "ssh", "device") else "shell"
    if mode == "container" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", id):
        await ws.close(code=4400)
        return
    if mode == "device" or (mode == "container" and device):
        # a device from Server → Devices: a shell, a module install (fixed script) or a shell in one of its containers
        try:
            argv = devices.console_argv(device, module if mode == "device" else "", id if mode == "container" else "")
            d = devices.get(device)
        except devices.DeviceError:
            await ws.close(code=4400)
            return
        await ws.accept()
        ip, started = auth.client_ip(ws), time.time()
        detail = {"device": d["name"], "ssh": f"{d['user']}@{d['host']}:{d['port']}"}
        detail.update({"module": module} if module else {"container": id} if mode == "container" else {})
        db.audit(("device_setup" if module in devices.SETUP_STEPS else "device_module_install") if module else "console_open",
                 user.username, ip, detail)
        code = await console.run(ws, "ssh", argv_override=argv)
        db.audit("console_close", user.username, ip, {"exit_code": code, "seconds": round(time.time() - started), **detail})
        with contextlib.suppress(Exception):
            await ws.close()
        return
    ssh = key_path = ssh_cmd = None
    if mode == "ssh":
        try:
            ssh = console.ssh_target(host, port, user_name)
            if key:
                key_path = ssh_keys.private_path(key)
            if install_key:
                ssh_cmd = ssh_keys.install_command(install_key)
        except (ValueError, ssh_keys.SshKeyError):
            await ws.close(code=4400)
            return
    await ws.accept()
    ip = auth.client_ip(ws)
    started = time.time()
    detail = {"container": id} if mode == "container" else {"ssh": f"{ssh[2]}@{ssh[0]}:{ssh[1]}"} if ssh else None
    if detail and key:
        detail["key"] = key
    if detail and install_key:
        detail["install_key"] = install_key
    if mode != "update":
        db.audit("ssh_key_install" if install_key else "console_open", user.username, ip, detail)
    code = await console.run(ws, mode, id, ssh, key_path, ssh_cmd)
    if mode != "update":
        db.audit("console_close", user.username, ip, {"exit_code": code, "seconds": round(time.time() - started), **(detail or {})})
    with contextlib.suppress(Exception):
        await ws.close()


@app.websocket("/ws/chat")
async def chat_ws(ws: WebSocket):
    user = auth.websocket_user(ws)
    if not user or not user.is_admin:
        await ws.close(code=4401)
        return
    await ws.accept()
    ip = auth.client_ip(ws)
    send_lock = asyncio.Lock()

    async def send(msg: dict) -> None:
        async with send_lock:
            try:
                await ws.send_json(msg)
            except Exception:
                pass

    runner = agent.ChatRunner(user, ip, send)
    task: asyncio.Task | None = None

    async def run(conv_id: int, text: str, connection_id: int | None) -> None:
        try:
            await runner.run_turn(conv_id, text, connection_id)
        except Exception as e:  # pragma: no cover - last-resort guard
            await send({"type": "error", "message": f"Internal error: {type(e).__name__}: {e}"})
            await send({"type": "done", "conversation_id": conv_id})
        if agent.restart_requested["pending"]:
            agent.restart_requested["pending"] = False
            db.audit("app_restart", user.username, ip, agent.restart_requested["reason"])
            await send({"type": "restarting", "reason": agent.restart_requested["reason"]})
            _schedule_restart()

    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "chat":
                if task and not task.done():
                    await send({"type": "error", "message": "The assistant is still working; stop it or wait."})
                    continue
                text = str(msg.get("text", "")).strip()
                if not text:
                    continue
                conv_id = msg.get("conversation_id")
                if not conv_id or not db.get_conversation(int(conv_id), user.id):
                    conv_id = db.create_conversation(user.id, agent.title_from(text))
                    await send({"type": "conversation", "id": conv_id, "title": agent.title_from(text)})
                else:
                    conv = db.get_conversation(int(conv_id), user.id)
                    if conv and not conv["messages"] and conv["title"] == "New chat":
                        db.rename_conversation(int(conv_id), user.id, agent.title_from(text))
                        await send({"type": "conversation", "id": int(conv_id), "title": agent.title_from(text)})
                try:
                    connection_id = int(msg["connection_id"]) if msg.get("connection_id") else None
                except (TypeError, ValueError):
                    connection_id = None
                task = asyncio.create_task(run(int(conv_id), text, connection_id))
            elif kind == "approve":
                runner.resolve_approval(str(msg.get("id")), {
                    "approved": bool(msg.get("approved")),
                    "note": str(msg.get("note") or "")[:2000],
                    "command": msg.get("command"),
                })
            elif kind == "stop":
                runner.stop()
            elif kind == "set_auto_approve":
                runner.auto_approve = bool(msg.get("value")) and settings.allow_auto_approve
                db.audit("auto_approve", user.username, ip, runner.auto_approve)
                await send({"type": "auto_approve", "value": runner.auto_approve})
            elif kind == "ping":
                await send({"type": "pong", "ts": time.time()})
    except WebSocketDisconnect:
        pass
    finally:
        runner.stop()
