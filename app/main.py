"""FastAPI application: auth, monitoring API, chat WebSocket, static frontend."""
from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

import anthropic

from . import agent, auth, claude_settings, db, monitor, updater
from .auth import User, admin_user, current_user
from .config import settings

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="SysAdmin.IA", docs_url=None, redoc_url=None, openapi_url=None)


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
    return {"username": user.username, "role": user.role, "model": claude_settings.model(),
            "allow_auto_approve": settings.allow_auto_approve,
            "api_key_configured": bool(claude_settings.api_key()[0])}


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


# ---------------------------------------------------------------- users (admin)

class NewUser(BaseModel):
    username: str
    password: str
    role: str = "admin"


@app.get("/api/users")
async def users(user: User = Depends(admin_user)):
    return auth.list_users()


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
    auth.delete_user(uid)
    db.audit("user_deleted", user.username, auth.client_ip(request), {"id": uid})
    return {"ok": True}


# ---------------------------------------------------------------- Claude API settings (admin)

class ClaudeSettingsBody(BaseModel):
    api_key: str | None = None        # new key; omitted/None = unchanged
    clear_api_key: bool = False       # remove the key saved in the web UI
    model: str | None = None          # "" = reset to config-file default
    effort: str | None = None         # "" = reset to config-file default


class TestKeyBody(BaseModel):
    api_key: str | None = None        # test this key instead of the saved one


@app.get("/api/claude-settings")
async def get_claude_settings(user: User = Depends(admin_user)):
    return claude_settings.summary()


@app.put("/api/claude-settings")
async def put_claude_settings(body: ClaudeSettingsBody, request: Request, user: User = Depends(admin_user)):
    changed = []
    try:
        if body.clear_api_key:
            claude_settings.set_api_key(None)
            changed.append("api_key cleared")
        elif body.api_key:
            claude_settings.set_api_key(body.api_key)
            changed.append("api_key set")
        if body.model is not None:
            claude_settings.set_model(body.model)
            changed.append(f"model={body.model or 'default'}")
        if body.effort is not None:
            claude_settings.set_effort(body.effort)
            changed.append(f"effort={body.effort or 'default'}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("claude_settings", user.username, auth.client_ip(request), ", ".join(changed))
    return claude_settings.summary()


@app.post("/api/claude-settings/test")
async def test_claude_settings(body: TestKeyBody, user: User = Depends(admin_user)):
    """Check the key and model with a free Models API lookup (no tokens are used)."""
    key = (body.api_key or "").strip() or claude_settings.api_key()[0]
    if not key:
        return {"ok": False, "message": "No API key to test."}
    model_name = claude_settings.model()
    try:
        async with anthropic.AsyncAnthropic(api_key=key, max_retries=0, timeout=20) as client:
            info = await client.models.retrieve(model_name)
        return {"ok": True, "message": f"Key works. Model: {info.display_name} ({info.id})."}
    except anthropic.AuthenticationError:
        return {"ok": False, "message": "The API key was rejected (invalid or revoked)."}
    except anthropic.PermissionDeniedError:
        return {"ok": False, "message": "The key is valid but lacks permission for this request."}
    except anthropic.NotFoundError:
        return {"ok": False, "message": f"The key works, but model '{model_name}' was not found."}
    except anthropic.APIStatusError as e:
        return {"ok": False, "message": f"API error {e.status_code}: {e.message}"}
    except anthropic.APIConnectionError:
        return {"ok": False, "message": "Could not reach api.anthropic.com from this server."}


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
    db.audit("update", user.username, ip, {"from": result["from"], "to": result["to"],
                                           "files": len(result["changed"])})
    if result["restart"]:
        _schedule_restart()
    return result


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
    return {"id": cid, "title": conv["title"], "items": agent.render_history(conv["messages"])}


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

    async def run(conv_id: int, text: str) -> None:
        try:
            await runner.run_turn(conv_id, text)
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
                    await send({"type": "error", "message": "Claude is still working; stop it or wait."})
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
                task = asyncio.create_task(run(int(conv_id), text))
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
