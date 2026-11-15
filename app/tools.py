"""Client-side tool implementations for Claude: a persistent bash session and a text editor.

Commands run as the service's OS user. Whether that user can change the system
(e.g. via passwordless sudo) is decided at install time; see install.sh.
Every action is approved in the UI (unless the user opted into auto-approve) and audited.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import time
from pathlib import Path

from .config import settings


def truncate(text: str, limit: int | None = None) -> str:
    limit = limit or settings.max_tool_output
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n\n…[{len(text) - limit} characters truncated]…\n\n{tail}"


# ---------------------------------------------------------------- bash

class BashSession:
    """A long-lived bash process so `cd`, exported variables, etc. persist between calls."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        env = dict(os.environ)
        # Never leak the app's own secrets into the shell environment by default.
        for k in ("SAI_SECRET_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(k, None)
        env.setdefault("TERM", "dumb")
        env["PAGER"] = "cat"
        env["SYSTEMD_PAGER"] = ""
        env["DEBIAN_FRONTEND"] = "noninteractive"
        self.proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "--noprofile", "--norc",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(settings.app_dir),
            env=env,
            start_new_session=True,
            limit=16 * 1024 * 1024,  # allow very long output lines (minified files, JSON)
        )

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, 9)
            except (ProcessLookupError, PermissionError, AttributeError):
                self.proc.kill()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        self.proc = None

    async def restart(self) -> str:
        await self.stop()
        await self.start()
        return "Bash session restarted."

    async def run(self, command: str, timeout: int | None = None) -> tuple[str, bool]:
        """Run a command. Returns (output, is_error)."""
        timeout = timeout or settings.command_timeout
        async with self._lock:
            if not self.proc or self.proc.returncode is not None:
                await self.start()
            assert self.proc and self.proc.stdin and self.proc.stdout
            nonce = secrets.token_hex(8)
            marker = f"__SAI_DONE_{nonce}__"
            # The command is written to a file and `source`d: a syntax error then fails only that
            # command instead of killing the shell, and stdin from /dev/null makes interactive
            # prompts fail fast instead of swallowing our end marker.
            tmp_dir = settings.data_dir / "tmp"
            tmp_dir.mkdir(exist_ok=True)
            cmd_file = tmp_dir / f"cmd-{nonce}.sh"
            fd = os.open(cmd_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(command + "\n")
            script = (
                f"source '{cmd_file}' < /dev/null 2>&1; __sai_rc=$?; rm -f '{cmd_file}'; "
                f"echo \"\"; echo \"{marker}$__sai_rc\"\n"
            )
            self.proc.stdin.write(script.encode())
            await self.proc.stdin.drain()

            chunks: list[str] = []
            size = 0
            cap = 2_000_000
            exit_code: int | None = None
            deadline = time.monotonic() + timeout
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=remaining)
                    if not line:  # shell exited (e.g. `exit` was run)
                        self.proc = None
                        chunks.append("\n[bash session exited; it will be restarted on next command]")
                        break
                    text = line.decode(errors="replace")
                    if text.startswith(marker):
                        try:
                            exit_code = int(text[len(marker):].strip())
                        except ValueError:
                            exit_code = None
                        break
                    if size < cap:
                        chunks.append(text)
                        size += len(text)
            except asyncio.TimeoutError:
                await self.stop()
                out = "".join(chunks)
                return truncate(out) + f"\n[command timed out after {timeout}s; bash session was restarted]", True

            out = "".join(chunks).rstrip("\n")
            if exit_code not in (0, None):
                out += f"\n[exit code {exit_code}]"
            return truncate(out) or "(no output)", False


# ---------------------------------------------------------------- text editor

class EditorError(Exception):
    pass


def _resolve(path: str) -> Path:
    if not path or not path.startswith("/"):
        raise EditorError("Path must be absolute (start with /).")
    if "\x00" in path:
        raise EditorError("Invalid path.")
    return Path(os.path.normpath(path))


def _backup(p: Path) -> str | None:
    if not p.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = settings.backup_dir / stamp / str(p).lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dest)
    return str(dest)


def _read_text(p: Path) -> str:
    if not p.exists():
        raise EditorError(f"{p} does not exist.")
    if p.stat().st_size > 5_000_000:
        raise EditorError(f"{p} is larger than 5 MB; use bash (head/tail/sed) instead.")
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise EditorError(f"{p} is not a UTF-8 text file.")
    except PermissionError:
        raise EditorError(f"Permission denied reading {p}. Use bash with sudo if appropriate.")


def _write_text(p: Path, content: str) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except PermissionError:
        raise EditorError(
            f"Permission denied writing {p}. For system files, use bash with sudo "
            f"(e.g. `sudo tee`), after making a backup."
        )


def text_editor(inp: dict) -> str:
    cmd = inp.get("command")
    p = _resolve(inp.get("path", ""))

    if cmd == "view":
        if p.is_dir():
            lines = []
            base_depth = len(p.parts)
            for root, dirs, files in os.walk(p):
                depth = len(Path(root).parts) - base_depth
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".venv", "venv"))
                if depth >= 2:
                    dirs[:] = []
                for name in sorted(dirs):
                    lines.append(str(Path(root, name)) + "/")
                for name in sorted(files):
                    if not name.startswith("."):
                        lines.append(str(Path(root, name)))
                if len(lines) > 1000:
                    lines.append("…[listing truncated]")
                    break
            return "\n".join(lines) or "(empty directory)"
        text = _read_text(p)
        all_lines = text.split("\n")
        start, end = 1, len(all_lines)
        vr = inp.get("view_range")
        if vr and len(vr) == 2:
            start = max(1, int(vr[0]))
            end = len(all_lines) if int(vr[1]) == -1 else min(len(all_lines), int(vr[1]))
        numbered = "\n".join(f"{i:6d}\t{all_lines[i - 1]}" for i in range(start, end + 1))
        return truncate(numbered)

    if cmd == "create":
        content = inp.get("file_text")
        if content is None:
            raise EditorError("file_text is required for create.")
        bk = _backup(p)
        _write_text(p, content)
        return f"Wrote {p}" + (f" (previous version backed up to {bk})" if bk else "")

    if cmd == "str_replace":
        old = inp.get("old_str")
        new = inp.get("new_str", "")
        if old is None:
            raise EditorError("old_str is required.")
        text = _read_text(p)
        count = text.count(old)
        if count == 0:
            raise EditorError("old_str was not found in the file. Re-view the file and try again.")
        if count > 1:
            raise EditorError(f"old_str matches {count} times; include more context so it is unique.")
        bk = _backup(p)
        _write_text(p, text.replace(old, new, 1))
        return f"Edited {p} (backup: {bk})"

    if cmd == "insert":
        line_no = int(inp.get("insert_line", 0))
        new = inp.get("insert_text", "")
        text = _read_text(p)
        lines = text.split("\n")
        if line_no < 0 or line_no > len(lines):
            raise EditorError(f"insert_line must be between 0 and {len(lines)}.")
        bk = _backup(p)
        lines[line_no:line_no] = new.split("\n")
        _write_text(p, "\n".join(lines))
        return f"Inserted text into {p} after line {line_no} (backup: {bk})"

    raise EditorError(f"Unsupported command: {cmd}")


def is_read_only(name: str, inp: dict) -> bool:
    """Actions that never need approval."""
    return name == "str_replace_based_edit_tool" and inp.get("command") == "view"


# ---------------------------------------------------------------- app restart

async def check_app_importable() -> tuple[bool, str]:
    """Before restarting, make sure the modified app still imports, so we don't brick the UI."""
    import sys
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import app.main",
        cwd=str(settings.app_dir),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "Import check timed out."
    return proc.returncode == 0, out.decode(errors="replace")[-4000:]
