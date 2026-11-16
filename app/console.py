"""Web console: a real terminal (PTY) on the server, streamed over a WebSocket.

Two kinds of session:
  * "shell"  – an interactive login shell as the service user (sysadmin-ia). With full
               access it can use sudo, like Claude's own commands.
  * "update" – a read-only view that follows the update log (tail -F), used by the
               "See details" button while an update runs.
  * "container" – a shell inside a Docker container (docker exec -it ID bash|sh), opened
               from the Containers module.

Protocol: the server sends terminal output as binary frames and status as JSON text frames
({"type": "exit", "code": N} / {"type": "error", "message": ...}); the client sends JSON:
{"type": "input", "data": "..."} and {"type": "resize", "cols": N, "rows": N}.

Only admins can open a console (checked by the caller) and every session is audited. What
is typed into a shell is not recorded.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import struct
import sys

from fastapi import WebSocket, WebSocketDisconnect

from . import updater
from .config import settings

try:  # POSIX only
    import fcntl
    import pwd
    import termios
except ImportError:  # pragma: no cover - Windows development machines
    fcntl = pwd = termios = None  # type: ignore[assignment]

AVAILABLE = sys.platform.startswith(("linux", "darwin")) and termios is not None


def _shell_argv() -> list[str]:
    for sh in (os.environ.get("SHELL"), "/bin/bash", "/bin/zsh", "/bin/sh"):
        if sh and sh not in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false") and shutil.which(sh):
            return [sh, "-l"]
    return ["/bin/sh"]


def _argv(mode: str, container: str = "") -> list[str]:
    if mode == "container":
        docker = shutil.which("docker") or "/usr/bin/docker"
        # A shell inside the container: bash if it has one, else sh.
        return [docker, "exec", "-it", container, "/bin/sh", "-c",
                "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"]
    if mode == "update":
        log = str(updater.UPGRADE_LOG)
        # Follow the update log; wait for it to appear if no update has run yet.
        return ["/bin/sh", "-c",
                f'echo "Following {log}"; echo; exec tail -n +1 -F "{log}" 2>/dev/null']
    return _shell_argv()


def _env() -> dict[str, str]:
    try:
        pw = pwd.getpwuid(os.getuid())
        user, home = pw.pw_name, pw.pw_dir
    except KeyError:
        user, home = "sysadmin-ia", str(settings.data_dir)
    if not os.path.isdir(home):
        home = str(settings.data_dir)
    return {
        "TERM": "xterm-256color", "COLORTERM": "truecolor",
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "PATH": os.environ.get("PATH") or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": home, "USER": user, "LOGNAME": user,
        "SHELL": _shell_argv()[0],
    }


def _set_size(fd: int, cols: int, rows: int) -> None:
    cols, rows = max(2, min(int(cols), 1000)), max(2, min(int(rows), 500))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _make_controlling_tty() -> None:  # runs in the child before exec
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


async def run(ws: WebSocket, mode: str, container: str = "") -> int | None:
    """Serve one console session on an accepted WebSocket. Returns the exit code."""
    if not AVAILABLE:
        await ws.send_json({"type": "error", "message": "The console is only available when the app runs on Linux."})
        return None

    master, slave = os.openpty()
    try:
        _set_size(slave, 120, 32)
        env = _env()
        proc = await asyncio.create_subprocess_exec(
            *_argv(mode, container), stdin=slave, stdout=slave, stderr=slave, env=env, cwd=env["HOME"],
            start_new_session=True, preexec_fn=_make_controlling_tty,
        )
    except Exception as e:
        os.close(master)
        os.close(slave)
        await ws.send_json({"type": "error", "message": f"Could not start the console: {e}"})
        return None
    os.close(slave)
    os.set_blocking(master, False)

    loop = asyncio.get_running_loop()
    out: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)

    def on_readable() -> None:
        try:
            data = os.read(master, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: the program exited and the terminal closed
            data = b""
        if not data:
            loop.remove_reader(master)
            out.put_nowait(None)
            return
        if out.full():  # client too slow: drop the oldest chunk rather than block the loop
            with contextlib.suppress(asyncio.QueueEmpty):
                out.get_nowait()
        out.put_nowait(data)

    loop.add_reader(master, on_readable)

    async def pump_output() -> None:
        while (chunk := await out.get()) is not None:
            await ws.send_bytes(chunk)

    async def pump_input() -> None:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "input" and mode in ("shell", "container"):
                data = str(msg.get("data", "")).encode("utf-8", "replace")
                while data:
                    try:
                        n = os.write(master, data)
                        data = data[n:]
                    except BlockingIOError:
                        await asyncio.sleep(0.01)
            elif kind == "resize":
                with contextlib.suppress(Exception):
                    _set_size(master, msg.get("cols", 80), msg.get("rows", 24))
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGWINCH)

    reader = asyncio.create_task(pump_output())
    writer = asyncio.create_task(pump_input())
    code = None
    try:
        await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
        # Retrieve task exceptions (usually WebSocketDisconnect: the browser closed the console).
        errors = [t.exception() for t in (reader, writer) if t.done() and not t.cancelled()]
        if reader.done() and not any(errors):  # the program exited
            code = await proc.wait()
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "exit", "code": code})
    except WebSocketDisconnect:
        pass
    finally:
        for t in (reader, writer):
            t.cancel()
        with contextlib.suppress(Exception):
            loop.remove_reader(master)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGHUP)
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
        code = proc.returncode if code is None else code
        os.close(master)
    return code
