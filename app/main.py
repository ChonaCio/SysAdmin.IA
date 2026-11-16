"""FastAPI application: auth, monitoring API, chat WebSocket, static frontend."""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import os
import re
import signal
import json
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel


from . import ai_connections as ai
from . import agent, auth, console, db, drives, google_oauth, jobs, mail, monitor, providers, server_name, updater
from . import containers, local_ai, model_search, variables
from .auth import User, admin_user, current_user
from .config import settings

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="SysAdmin.IA", docs_url=None, redoc_url=None, openapi_url=None)

# Turn the old single Claude key (Admin → Claude API) into an AI connection, once.
ai.migrate_legacy()


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
            "ai_ready": ai.any_enabled()}


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
    used = {c["config"].get("model"): c["name"] for c in ai.list_all() if c["provider"] == "ollama_managed"}
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
    users = [c["name"] for c in ai.list_all() if c["provider"] == "ollama_managed" and c["config"].get("model") == body.name]
    await asyncio.to_thread(_lai, local_ai.delete, body.name)
    db.audit("localai_delete", user.username, auth.client_ip(request), {"model": body.name})
    return {"ok": True, "connections": users}


@app.post("/api/localai/use")
async def localai_use(body: ModelBody, request: Request, user: User = Depends(admin_user)):
    """Create (or return) the AI connection for an installed model, so it can be picked in chats."""
    for c in ai.list_all():
        if c["provider"] == "ollama_managed" and c["config"].get("model") == body.name:
            return ai.public(c)
    try:
        c = ai.create({"name": f"{body.name} (local)", "provider": "ollama_managed", "method": "local",
                       "config": {"model": body.name}, "is_default": not ai.any_enabled()})
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
             {"name": body.spec.get("name"), "image": body.spec.get("image")})
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
async def docker_tasks(user: User = Depends(admin_user)):
    return containers.tasks()


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
    used = [j["name"] for j in jobs.list_all() if f"var.{name}" in json.dumps(j["actions"])
            or any(a.get("type") == "set_variable" and a.get("name") == name for a in j["actions"])]
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
    return [j["name"] for j in jobs.list_all()
            if any(str(x.get("account")) == str(acc_id) for x in j["triggers"] + j["actions"])]


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

class JobBody(BaseModel):
    name: str = ""
    description: str = ""
    enabled: bool = True
    triggers: list = []
    actions: list = []
    options: dict = {}


class EnabledBody(BaseModel):
    enabled: bool


def _job_dict(b: JobBody) -> dict:
    return b.model_dump() if hasattr(b, "model_dump") else b.dict()


@app.get("/api/jobs/catalog")
async def jobs_catalog(user: User = Depends(admin_user)):
    return await asyncio.to_thread(jobs.catalog)


@app.get("/api/jobs")
async def jobs_list(user: User = Depends(admin_user)):
    return [jobs.public(j) | {"running": j["id"] in jobs.ENGINE.running} for j in jobs.list_all()]


@app.post("/api/jobs")
async def jobs_create(body: JobBody, request: Request, user: User = Depends(admin_user)):
    try:
        j = jobs.create(_job_dict(body))
    except jobs.JobError as e:
        raise HTTPException(400, str(e))
    db.audit("job_created", user.username, auth.client_ip(request), {"id": j["id"], "name": j["name"]})
    return jobs.public(j)


@app.put("/api/jobs/{jid}")
async def jobs_update(jid: int, body: JobBody, request: Request, user: User = Depends(admin_user)):
    try:
        j = jobs.update(jid, _job_dict(body))
    except jobs.JobError as e:
        raise HTTPException(400, str(e))
    db.audit("job_updated", user.username, auth.client_ip(request), {"id": jid, "name": j["name"]})
    return jobs.public(j)


@app.post("/api/jobs/{jid}/enabled")
async def jobs_enable(jid: int, body: EnabledBody, request: Request, user: User = Depends(admin_user)):
    if not jobs.get(jid):
        raise HTTPException(404)
    jobs.set_enabled(jid, body.enabled)
    db.audit("job_enabled" if body.enabled else "job_disabled", user.username, auth.client_ip(request), {"id": jid})
    return {"ok": True}


@app.delete("/api/jobs/{jid}")
async def jobs_delete(jid: int, request: Request, user: User = Depends(admin_user)):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404)
    jobs.delete(jid)
    db.audit("job_deleted", user.username, auth.client_ip(request), {"id": jid, "name": j["name"]})
    return {"ok": True}


@app.post("/api/jobs/{jid}/run")
async def jobs_run_now(jid: int, user: User = Depends(admin_user)):
    """Run the actions now (as a test). Trigger placeholders get example values."""
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404)
    ctx = {"summary": f"Manual test run of “{j['name']}”", "state": "test", "value": "", "threshold": "",
           "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        return await jobs.ENGINE.run(jid, ctx, f"Run now by {user.username}", user=user.username)
    except jobs.JobError as e:
        raise HTTPException(400, str(e))


@app.get("/api/jobs/{jid}/runs")
async def jobs_runs(jid: int, user: User = Depends(admin_user)):
    return jobs.runs(jid)


@app.on_event("startup")
async def _start_jobs() -> None:
    jobs.ENGINE.start()


# ---------------------------------------------------------------- drives (admin)

class MountBody(BaseModel):
    device: str
    name: str
    persist: bool = False
    readonly: bool = False


class UnmountBody(BaseModel):
    mountpoint: str
    forget: bool = True


@app.get("/api/drives")
async def get_drives(user: User = Depends(admin_user)):
    try:
        return await asyncio.to_thread(drives.list_drives)
    except drives.DriveError as e:
        raise HTTPException(500, str(e))


@app.post("/api/drives/mount")
async def mount_drive(body: MountBody, request: Request, user: User = Depends(admin_user)):
    try:
        r = await asyncio.to_thread(drives.mount, body.device, body.name, body.persist, body.readonly)
    except (ValueError, RuntimeError) as e:
        db.audit("drive_mount_failed", user.username, auth.client_ip(request), {"device": body.device, "error": str(e)})
        raise HTTPException(400, str(e))
    db.audit("drive_mount", user.username, auth.client_ip(request),
             {"device": body.device, "mountpoint": r["mountpoint"], "persist": body.persist, "readonly": body.readonly})
    return r


@app.post("/api/drives/unmount")
async def unmount_drive(body: UnmountBody, request: Request, user: User = Depends(admin_user)):
    try:
        r = await asyncio.to_thread(drives.unmount, body.mountpoint, body.forget)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    db.audit("drive_unmount", user.username, auth.client_ip(request), {"mountpoint": r["mountpoint"], "forget": body.forget})
    return r


@app.get("/api/files")
async def list_files(path: str = "/", hidden: bool = False, user: User = Depends(admin_user)):
    try:
        return await asyncio.to_thread(drives.list_dir, path, hidden)
    except drives.DriveError as e:
        raise HTTPException(400, str(e))


@app.post("/api/files/sizes")
async def file_sizes(path: str, user: User = Depends(admin_user)):
    try:
        return await asyncio.to_thread(drives.folder_sizes, path)
    except drives.DriveError as e:
        raise HTTPException(400, str(e))


@app.get("/api/files/download")
async def download_file(path: str, request: Request, user: User = Depends(admin_user)):
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
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await asyncio.to_thread(updater.status)


@app.post("/api/update/apply")
async def update_apply(request: Request, user: User = Depends(admin_user)):
    ip = auth.client_ip(request)
    before = updater.current_version()
    try:
        result = await updater.apply()
    except updater.UpdateError as e:
        db.audit("update_failed", user.username, ip, {"from": before, "error": str(e)})
        raise HTTPException(400, str(e))
    if result["mode"] == "full":
        # The root helper runs the new install.sh in the background; it restarts the app.
        db.audit("update_started", user.username, ip, {"from": result["from"], "to": result["to"]})
        return result
    db.audit("update", user.username, ip, {"from": result["from"], "to": result["to"],
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
    """Exit cleanly; systemd (Restart=always) starts the new code."""
    loop = asyncio.get_running_loop()
    loop.call_later(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))


@app.websocket("/ws/console")
async def console_ws(ws: WebSocket, mode: str = "shell", id: str = ""):
    user = auth.websocket_user(ws)
    if not user or not user.is_admin or not settings.allow_console:
        await ws.close(code=4401 if not user else 4403)
        return
    mode = mode if mode in ("update", "container") else "shell"
    if mode == "container" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", id):
        await ws.close(code=4400)
        return
    await ws.accept()
    ip = auth.client_ip(ws)
    started = time.time()
    detail = {"container": id} if mode == "container" else None
    if mode != "update":
        db.audit("console_open", user.username, ip, detail)
    code = await console.run(ws, mode, id)
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
