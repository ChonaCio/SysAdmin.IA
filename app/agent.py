"""AI chat + tool-use loop.

The AI (whichever AI connection the chat uses: Claude, ChatGPT, Grok, Gemini, ...) gets
three tools:
  * bash                         – persistent shell on this server
  * str_replace_based_edit_tool  – view/create/edit files
  * restart_app                  – restart this web app after self-modification

Every state-changing tool call is sent to the browser for approval first (unless the
user switched the chat to auto-approve). Everything is written to the audit log.
"""
from __future__ import annotations

import asyncio
import getpass
import shutil
import subprocess
from typing import Any, Awaitable, Callable

from . import ai_connections as ai
from . import db, monitor, providers
from .auth import User
from .config import settings
from .tools import BashSession, EditorError, check_app_importable, is_read_only, text_editor

Send = Callable[[dict], Awaitable[None]]

NO_CONNECTION_MESSAGE = ("No AI connection is set up yet. An admin can add one in the AI connections "
                         "module (or run `sudo sysadmin-ia set-api-key` on the server for Claude).")


def _detect_sudo() -> str:
    if not shutil.which("sudo"):
        return "sudo is not installed"
    try:
        r = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5)
        return "passwordless sudo is AVAILABLE" if r.returncode == 0 else "sudo is NOT available without a password"
    except Exception:
        return "sudo availability unknown"


def _system_prompt() -> str:
    info = monitor.host_info()
    try:
        run_user = getpass.getuser()
    except Exception:
        run_user = "unknown"
    return f"""You are the administration assistant built into "SysAdmin.IA", a web app running on this Linux server. Authenticated administrators chat with you to inspect, troubleshoot, and change the server, and to modify this web app itself.

## Server
- Hostname: {info['hostname']}
- OS: {info['os']} (kernel {info['kernel']}, {info['arch']}), {info['cpu_count']} logical CPUs
- Your commands run as OS user `{run_user}`; {_detect_sudo()}.
- Write portable commands: detect the package manager (apt, dnf, yum, zypper, pacman, apk) and init system rather than assuming Ubuntu.

## Tools
- `bash`: a persistent bash session (cwd and variables persist). Stdin is /dev/null, so always use non-interactive flags (`-y`, `DEBIAN_FRONTEND=noninteractive`, `--no-pager`). Each command times out after {settings.command_timeout}s; run long jobs in the background (systemd-run, nohup) and poll. Don't start foreground servers.
- `str_replace_based_edit_tool`: view/create/edit files by absolute path. Previous versions are backed up automatically to {settings.backup_dir}. It runs without sudo; for root-owned files, back them up with `sudo cp file file.bak.$(date +%s)` and edit via bash (`sudo tee`, `sudo sed -i`).
- `restart_app`: restart this web app after changing its backend code.

## Human approval
Every state-changing action is shown to the administrator, who approves, edits, or denies it. If an action is denied, do not retry it in a different form; ask what they would prefer. Before a risky or destructive step (deleting data, restarting networking/SSH, firewall changes, package removal, reboots), state the risk and the rollback plan in one or two sentences first. Inspect before you change things, and verify the result after.

## Modifying this app
- Source code: {settings.app_dir} (a git repository). Backend: FastAPI in app/*.py; frontend: plain HTML/CSS/JS in app/static/ (served from disk, so frontend edits are live on reload).
- Workflow: read the relevant code first; `git add -A && git commit -m "snapshot before <change>"` if there are uncommitted changes; make the change; check with `python -m py_compile` (use the interpreter at {settings.app_dir}/.venv/bin/python if it exists); commit; then call `restart_app` for backend changes.
- Python dependencies go in requirements.txt and are installed with the venv's pip.
- Never weaken authentication, remove the approval step, or expose the app without auth unless the administrator explicitly asks and understands the risk.

## Secrets
Never print API keys, password hashes, private keys, or the contents of {settings.service_name} environment files in your replies. Refer to them by path instead.

## Style
Be concise. Use Markdown; put commands, paths and config in code formatting. After finishing a task, summarize what changed and how to undo it."""


SYSTEM_PROMPT: str | None = None  # built lazily (stable for the life of the process => cacheable)

# One bash session per conversation, kept across turns.
_bash_sessions: dict[int, BashSession] = {}

restart_requested: dict[str, Any] = {"pending": False, "reason": ""}


def bash_for(conv_id: int) -> BashSession:
    if conv_id not in _bash_sessions:
        _bash_sessions[conv_id] = BashSession()
    return _bash_sessions[conv_id]


async def drop_bash(conv_id: int) -> None:
    s = _bash_sessions.pop(conv_id, None)
    if s:
        await s.stop()


# ---------------------------------------------------------------- history helpers

def _sanitize_fallback(content: list[dict]) -> list[dict]:
    """After a mid-output refusal fallback, blocks before the last `fallback` boundary that are
    model-internal (thinking, tool_use, …) must not be echoed back; text blocks are kept."""
    last = max((i for i, b in enumerate(content) if b.get("type") == "fallback"), default=-1)
    if last < 0:
        return content
    drop = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}
    return [b for i, b in enumerate(content) if i >= last or b.get("type") not in drop]


def _pending_tool_ids(messages: list[dict]) -> list[str]:
    """tool_use ids in the last assistant message that never got a tool_result."""
    if not messages or messages[-1]["role"] != "assistant":
        return []
    content = messages[-1]["content"]
    if not isinstance(content, list):
        return []
    return [b["id"] for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _close_dangling(messages: list[dict], reason: str) -> list[dict]:
    """Tool results for any unanswered tool_use (e.g. after a crash or Stop)."""
    return [
        {"type": "tool_result", "tool_use_id": tid, "content": reason, "is_error": True}
        for tid in _pending_tool_ids(messages)
    ]


def render_history(messages: list[dict]) -> list[dict]:
    """Convert stored API messages into a simple list for the UI."""
    out: list[dict] = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "kind": "text", "text": content})
            continue
        for b in content:
            t = b.get("type")
            if t == "text" and b.get("text"):
                out.append({"role": m["role"], "kind": "text", "text": b["text"]})
            elif t == "thinking" and b.get("thinking"):
                out.append({"role": "assistant", "kind": "thinking", "text": b["thinking"]})
            elif t == "reasoning" and b.get("text"):  # reasoning text from non-Claude models
                out.append({"role": "assistant", "kind": "thinking", "text": b["text"]})
            elif t == "tool_use":
                out.append({"role": "assistant", "kind": "tool_use", "id": b["id"], "name": b["name"],
                            "input": b.get("input", {})})
            elif t == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                out.append({"role": "tool", "kind": "tool_result", "id": b["tool_use_id"],
                            "output": c or "", "is_error": bool(b.get("is_error"))})
    return out


# ---------------------------------------------------------------- runner

class ChatRunner:
    """Drives one chat turn (possibly many API calls) for one WebSocket connection."""

    def __init__(self, user: User, ip: str, send: Send) -> None:
        self.user = user
        self.ip = ip
        self.send = send
        self.pending: dict[str, asyncio.Future] = {}
        self.stop_event = asyncio.Event()
        self.auto_approve = False

    def resolve_approval(self, tool_use_id: str, decision: dict) -> None:
        fut = self.pending.get(tool_use_id)
        if fut and not fut.done():
            fut.set_result(decision)

    def stop(self) -> None:
        self.stop_event.set()
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result({"approved": False, "note": "Stopped by user."})

    async def _await_approval(self, tool_use_id: str) -> dict:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[tool_use_id] = fut
        try:
            return await fut
        finally:
            self.pending.pop(tool_use_id, None)

    async def run_turn(self, conv_id: int, user_text: str, connection_id: int | None = None) -> None:
        global SYSTEM_PROMPT
        if SYSTEM_PROMPT is None:
            SYSTEM_PROMPT = await asyncio.to_thread(_system_prompt)

        conv = db.get_conversation(conv_id, self.user.id)
        if conv is None:
            await self.send({"type": "error", "message": "Conversation not found."})
            await self.send({"type": "done", "conversation_id": conv_id})
            return
        # The chat's AI connection: the one picked in the composer, else the chat's own, else the default.
        if connection_id and connection_id != conv.get("connection_id") and ai.get(connection_id):
            db.set_conversation_connection(conv_id, self.user.id, connection_id)
            conv["connection_id"] = connection_id
        conn = ai.resolve(conv.get("connection_id"))
        if conn is None:
            await self.send({"type": "error", "message": NO_CONNECTION_MESSAGE})
            await self.send({"type": "done", "conversation_id": conv_id})
            return
        try:
            provider = providers.for_connection(conn)
        except providers.ProviderError as e:
            await self.send({"type": "error", "message": str(e)})
            await self.send({"type": "done", "conversation_id": conv_id})
            return
        await self.send({"type": "connection", "conversation_id": conv_id, "id": conn["id"], "name": conn["name"]})
        messages: list[dict] = conv["messages"]
        self.stop_event.clear()

        # New user message (merged with results for any dangling tool calls).
        dangling = _close_dangling(messages, "Interrupted: this tool call was never executed.")
        messages.append({"role": "user", "content": dangling + [{"type": "text", "text": user_text}]})
        db.save_messages(conv_id, messages)
        db.audit("chat", self.user.username, self.ip,
                 {"conversation": conv_id, "connection": conn["name"], "text": user_text})

        turn_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "requests": 0}
        try:
            while not self.stop_event.is_set():
                blocked = ai.limit_reached(conn)
                if blocked:
                    await self.send({"type": "error", "message": blocked})
                    break
                await self.send({"type": "assistant_start"})
                result = await provider.turn(messages, SYSTEM_PROMPT, self.send, self.stop_event.is_set)
                if result is None:  # stopped mid-stream
                    break
                ai.set_rate(conn["id"], result.rate)
                if result.usage:
                    ai.record_usage(conn["id"], result.usage)
                    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                        turn_usage[k] += result.usage.get(k) or 0
                    turn_usage["requests"] += 1

                content = _sanitize_fallback(result.content)
                messages.append({"role": "assistant", "content": content})
                db.save_messages(conv_id, messages)

                stop = result.stop
                if stop == "refusal":
                    why = result.note
                    await self.send({"type": "notice", "message": f"{conn['name']} declined this request.{(' ' + why) if why else ''}"})
                    break
                if stop == "max_tokens":
                    await self.send({"type": "notice", "message": "Response hit the max_tokens limit and was cut off."})
                    if _pending_tool_ids(messages):
                        messages.append({"role": "user", "content": _close_dangling(
                            messages, "Not executed: the response was truncated by max_tokens.")})
                        db.save_messages(conv_id, messages)
                    break
                if stop == "pause_turn":
                    continue
                if stop != "tool_use":
                    break

                results = await self._run_tools(conv_id, content)
                messages.append({"role": "user", "content": results})
                db.save_messages(conv_id, messages)
        except providers.ProviderError as e:
            await self.send({"type": "error", "message": str(e)})
        finally:
            if _pending_tool_ids(messages):
                messages.append({"role": "user", "content": _close_dangling(messages, "Stopped by user before execution.")})
            db.save_messages(conv_id, messages)
            if turn_usage["requests"]:
                limit = conn["config"].get("monthly_token_limit")
                await self.send({"type": "usage", "conversation_id": conv_id, "connection": conn["name"], **turn_usage,
                                 "month_tokens": ai.month_tokens(conn["id"]), "monthly_limit": limit})
            await self.send({"type": "done", "conversation_id": conv_id})

    async def _run_tools(self, conv_id: int, content: list[dict]) -> list[dict]:
        results: list[dict] = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            tid, name, inp = block["id"], block["name"], block.get("input") or {}

            if self.stop_event.is_set():
                results.append({"type": "tool_result", "tool_use_id": tid, "content": "Stopped by user.", "is_error": True})
                continue

            # Eager input streaming means inputs are not validated server-side: validate here.
            err = _validate_input(name, inp)
            if err:
                results.append({"type": "tool_result", "tool_use_id": tid, "content": err, "is_error": True})
                continue

            needs_approval = not (is_read_only(name, inp) or self.auto_approve)
            await self.send({"type": "tool_request", "id": tid, "name": name, "input": inp,
                             "needs_approval": needs_approval})

            note = ""
            if needs_approval:
                decision = await self._await_approval(tid)
                if not decision.get("approved"):
                    msg = "The administrator denied this action."
                    if decision.get("note"):
                        msg += f" Their note: {decision['note']}"
                    db.audit("tool_denied", self.user.username, self.ip, {"tool": name, "input": inp})
                    await self.send({"type": "tool_result", "id": tid, "output": msg, "is_error": True})
                    results.append({"type": "tool_result", "tool_use_id": tid, "content": msg, "is_error": True})
                    continue
                edited = decision.get("command")
                if name == "bash" and isinstance(edited, str) and edited.strip() and edited != inp.get("command"):
                    note = f"[The administrator edited the command before running it. Command actually run:\n{edited}]\n\n"
                    inp = {**inp, "command": edited}
                if decision.get("note"):
                    note += f"[Administrator note: {decision['note']}]\n\n"

            output, is_error = await self._execute(conv_id, name, inp)
            output = note + output
            db.audit("tool_exec", self.user.username, self.ip,
                     {"conversation": conv_id, "tool": name, "input": inp, "is_error": is_error,
                      "auto_approved": not needs_approval})
            await self.send({"type": "tool_result", "id": tid, "output": output, "is_error": is_error})
            results.append({"type": "tool_result", "tool_use_id": tid, "content": output, "is_error": is_error})
        return results

    async def _execute(self, conv_id: int, name: str, inp: dict) -> tuple[str, bool]:
        try:
            if name == "bash":
                if inp.get("restart"):
                    return await bash_for(conv_id).restart(), False
                return await bash_for(conv_id).run(inp["command"])
            if name == "str_replace_based_edit_tool":
                return await asyncio.to_thread(text_editor, inp), False
            if name == "restart_app":
                ok, out = await check_app_importable()
                if not ok:
                    return f"Restart refused: the app fails to import, so restarting would take it offline.\n{out}", True
                restart_requested.update(pending=True, reason=inp.get("reason", ""))
                return "Import check passed. The app will restart when this reply finishes.", False
            return f"Unknown tool: {name}", True
        except EditorError as e:
            return str(e), True
        except Exception as e:  # never let a tool crash the loop
            return f"{type(e).__name__}: {e}", True


def _validate_input(name: str, inp: Any) -> str | None:
    if not isinstance(inp, dict):
        return "Invalid tool input: expected an object."
    if name == "bash":
        if not inp.get("restart") and not isinstance(inp.get("command"), str):
            return "Invalid input: `command` (string) is required."
    elif name == "str_replace_based_edit_tool":
        if inp.get("command") not in {"view", "create", "str_replace", "insert"}:
            return "Invalid input: unknown editor command."
        if not isinstance(inp.get("path"), str):
            return "Invalid input: `path` (string) is required."
    elif name == "restart_app":
        if not isinstance(inp.get("reason"), str):
            return "Invalid input: `reason` (string) is required."
    return None


def title_from(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "New chat"
    return line[:60] + ("…" if len(line) > 60 else "")


__all__ = ["ChatRunner", "render_history", "restart_requested", "drop_bash", "title_from"]
