"""FastAPI application: auth, monitoring API, chat WebSocket, static frontend."""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import os
import signal
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel


from . import ai_connections as ai
from . import agent, auth, console, db, drives, monitor, providers, server_name, updater
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


@app.get("/api/chat/connections")
async def chat_connections(user: User = Depends(admin_user)):
    """Enabled connections for the chat's picker."""
    return [{"id": c["id"], "name": c["name"], "short": ai.public(c)["short"], "model": ai.public(c)["model"],
             "is_default": c["is_default"]} for c in ai.list_all() if c["enabled"]]


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
async def console_ws(ws: WebSocket, mode: str = "shell"):
    user = auth.websocket_user(ws)
    if not user or not user.is_admin or not settings.allow_console:
        await ws.close(code=4401 if not user else 4403)
        return
    mode = "update" if mode == "update" else "shell"
    await ws.accept()
    ip = auth.client_ip(ws)
    started = time.time()
    if mode == "shell":
        db.audit("console_open", user.username, ip)
    code = await console.run(ws, mode)
    if mode == "shell":
        db.audit("console_close", user.username, ip, {"exit_code": code, "seconds": round(time.time() - started)})
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
