"""Process roles and the client for the supervisor.

SysAdmin.IA runs as several independent processes, started and watched by app.supervisor
(`python -m app`, the systemd service):

  web      – the web interface and API (uvicorn): pages, chat, console, monitor, on-demand actions
  jobs     – the jobs scheduler: checks DAG triggers and runs DAG tasks
  network  – the network watcher: periodic scans, dynamic DNS
  names    – names & websites: announcing .local names (avahi-publish), watching the websites' proxy

SAI_PROCESS tells a process which one it is. Without a supervisor (`python -m app --single`, tests,
development) one process does everything: role "all".

The supervisor listens on 127.0.0.1 (a random port) and writes the port and a random token to
<data>/run/supervisor.json, readable only by the app's user; every request must carry the token.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import threading
import time
from pathlib import Path

from .config import settings

log = logging.getLogger("sysadmin-ia.procs")

WORKERS: dict[str, dict] = {
    "web": {"label": "Web interface", "icon": "monitor",
            "description": "The pages and API you're using now, with chat, console and monitor. Restarting it reloads the page."},
    "jobs": {"label": "Jobs scheduler", "icon": "clock",
             "description": "Checks the triggers of the Jobs (DAGs) and runs their tasks. While it's stopped, no job runs."},
    "network": {"label": "Network watcher", "icon": "network",
                "description": "Periodic network scans and dynamic DNS updates. The Network pages still work while it's "
                               "stopped, but nothing happens in the background."},
    "names": {"label": "Names & websites", "icon": "globe",
              "description": "Announces the .local names of Network → Names & websites and keeps the websites' proxy "
                             "running. While it's stopped, .local names stop working on other devices."},
}
# which module of the interface depends on which background process
MODULE_PROCESS = {"jobs": "jobs", "network": "network", "names": "names"}


class ProcessError(RuntimeError):
    pass


def role() -> str:
    return os.environ.get("SAI_PROCESS") or "all"


def owns(part: str) -> bool:
    """True if this process runs `part` (jobs scheduler, network watcher…)."""
    return role() in ("all", part)


def supervised() -> bool:
    return bool(os.environ.get("SAI_PROCESS"))


def run_dir() -> Path:
    d = settings.data_dir / "run"
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


CONTROL_FILE = "supervisor.json"


def request(cmd: dict, timeout: float = 8.0) -> dict:
    """Send one command to the supervisor and return its answer (raises ProcessError)."""
    try:
        info = json.loads((run_dir() / CONTROL_FILE).read_text(encoding="utf-8"))
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=timeout) as s:
            s.sendall((json.dumps({**cmd, "token": info["token"]}) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(1 << 16)
                if not chunk:
                    break
                data += chunk
        r = json.loads(data or b"{}")
    except (OSError, ValueError, KeyError) as e:
        raise ProcessError(f"The process supervisor isn't answering ({type(e).__name__}).")
    if not r.get("ok"):
        raise ProcessError(r.get("error") or "The supervisor refused.")
    return r


_STARTED = time.time()


def _single_status() -> dict:
    """No supervisor: this one process is everything."""
    try:
        import psutil
        p = psutil.Process()
        cpu, rss = p.cpu_percent(interval=None), p.memory_info().rss
    except Exception:  # noqa: BLE001
        cpu, rss = None, None
    workers = [{"name": n, **WORKERS[n], "state": "running", "desired": "running", "pid": os.getpid(), "started_at": _STARTED,
                "restarts": 0, "last_exit": None, "responding": True, "cpu": cpu, "rss": rss, "heartbeat_at": time.time(),
                "info": {}, "shared": n != "web"} for n in WORKERS]
    return {"supervised": False, "started_at": _STARTED, "pid": os.getpid(), "workers": workers}


def status() -> dict:
    if not supervised():
        return _single_status()
    return request({"cmd": "status"})["status"]


def summary() -> dict:
    """Short status for every user: which processes work (no pids, logs or numbers)."""
    try:
        st = status()
    except ProcessError as e:
        return {"supervised": True, "error": str(e), "workers": {}}
    return {"supervised": st["supervised"], "error": None,
            "workers": {w["name"]: {"label": w["label"], "ok": w["state"] == "running" and w.get("responding") is not False,
                                    "state": w["state"] if w.get("responding") is not False else "not responding"}
                        for w in st["workers"]},
            "modules": MODULE_PROCESS}


def control(name: str, action: str) -> dict:
    if name not in WORKERS:
        raise ProcessError("Unknown process.")
    if action not in ("start", "stop", "restart"):
        raise ProcessError("Unknown action.")
    if not supervised():
        raise ProcessError("SysAdmin.IA runs as a single process here (development mode), so its parts can't be "
                           "started or stopped separately. Restart the whole app instead.")
    if name == "web" and action == "stop":
        raise ProcessError("Stopping the web interface would leave no page to start it again. Restart it instead, or "
                           "use the command line: sudo sysadmin-ia process stop web")
    return request({"cmd": action, "name": name}, timeout=30)["status"]


def logs(name: str, lines: int = 200) -> list[dict]:
    if name not in WORKERS:
        raise ProcessError("Unknown process.")
    if not supervised():
        return []
    return request({"cmd": "logs", "name": name, "lines": int(lines)})["lines"]


def restart_app(delay: float = 1.0) -> None:
    """Restart every process with fresh code (and fresh group memberships): the supervisor stops them all and
    exits, and systemd starts it again. Without a supervisor, this process exits the same way."""
    def go():
        time.sleep(delay)
        if supervised():
            try:
                request({"cmd": "restart_app"})
                return
            except ProcessError:
                log.warning("supervisor not reachable; exiting this process instead")
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=go, daemon=True, name="restart-app").start()


async def heartbeat_loop(info=None, every: float = 10.0) -> None:
    """Tell the supervisor this worker is alive (and how it's doing), so a hung process shows as not responding."""
    while True:
        try:
            extra = info() if info else {}
            await asyncio.to_thread(request, {"cmd": "heartbeat", "name": role(), "info": extra}, 5)
        except Exception:  # noqa: BLE001 - never stop the worker because of this
            pass
        await asyncio.sleep(every)
