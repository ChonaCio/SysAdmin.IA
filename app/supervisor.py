"""Process supervisor: starts the worker processes (web, jobs, network), restarts them when they crash, and lets
the web interface start, stop and restart them (Server → Processes).

It is the systemd service's main process (`python -m app`). Each worker is `python -m app.worker NAME` with
SAI_PROCESS=NAME. A worker that crashes is restarted after a short pause (1 s, 2 s, 5 s, … up to a minute); after
5 crashes in 2 minutes it's left stopped ("failed") until someone starts it. A worker stopped on purpose stays
stopped, also after a reboot (saved in <data>/run/processes.json) – except the web interface, which always runs.

Control: one JSON line per TCP connection on 127.0.0.1:<port>, with the token from <data>/run/supervisor.json.
Commands: status, start/stop/restart NAME, logs NAME, heartbeat NAME, restart_app (stop everything and exit, so
systemd starts fresh processes with the current code).
"""
from __future__ import annotations

import collections
import hmac
import json
import logging
import os
import secrets
import signal
import socketserver
import subprocess
import sys
import threading
import time

from . import procs
from .config import settings

log = logging.getLogger("sysadmin-ia.supervisor")

BACKOFF = [1, 2, 5, 10, 30, 60]
CRASH_WINDOW, CRASH_LIMIT = 120, 5
HEARTBEAT_LATE = 60          # seconds without a heartbeat → "not responding"
STOP_TIMEOUT = 10


class Worker:
    def __init__(self, name: str, desired: str = "running") -> None:
        self.name = name
        self.spec = procs.WORKERS[name]
        self.proc: subprocess.Popen | None = None
        self.state = "stopped"            # starting | running | stopped | crashed | failed
        self.desired = desired            # running | stopped
        self.started_at: float | None = None
        self.restarts = 0
        self.last_exit: dict | None = None
        self.crashes: collections.deque = collections.deque(maxlen=CRASH_LIMIT)
        self.retry_at = 0.0
        self.stopping = False
        self.heartbeat_at: float | None = None
        self.info: dict = {}
        self.logs: collections.deque = collections.deque(maxlen=2000)
        self._ps = None

    def public(self) -> dict:
        cpu = rss = None
        if self.proc and self.proc.poll() is None:
            try:
                import psutil
                if not self._ps or self._ps.pid != self.proc.pid:
                    self._ps = psutil.Process(self.proc.pid)
                    self._ps.cpu_percent(interval=None)
                cpu, rss = self._ps.cpu_percent(interval=None), self._ps.memory_info().rss
            except Exception:  # noqa: BLE001
                pass
        late = (self.state == "running" and self.started_at and time.time() - self.started_at > HEARTBEAT_LATE
                and (not self.heartbeat_at or time.time() - self.heartbeat_at > HEARTBEAT_LATE))
        return {"name": self.name, **self.spec, "state": self.state, "desired": self.desired,
                "pid": self.proc.pid if self.proc and self.proc.poll() is None else None,
                "started_at": self.started_at, "restarts": self.restarts, "last_exit": self.last_exit,
                "responding": False if late else (True if self.state == "running" else None),
                "heartbeat_at": self.heartbeat_at, "retry_at": self.retry_at if self.state == "crashed" else None,
                "cpu": cpu, "rss": rss, "info": self.info}


class Supervisor:
    def __init__(self, python: str | None = None, argv_for=None) -> None:
        self.python = python or sys.executable
        self.argv_for = argv_for or (lambda name: [self.python, "-m", "app.worker", name])
        self.started_at = time.time()
        self.lock = threading.RLock()
        desired = self._load_desired()
        self.workers = {n: Worker(n, "running" if n == "web" else desired.get(n, "running")) for n in procs.WORKERS}
        self.token = secrets.token_hex(24)
        self.server: socketserver.TCPServer | None = None
        self.exiting = False

    # ---- persistence of "stopped on purpose"
    def _desired_file(self):
        return procs.run_dir() / "processes.json"

    def _load_desired(self) -> dict:
        try:
            d = json.loads(self._desired_file().read_text(encoding="utf-8"))
            return {k: v for k, v in d.items() if v in ("running", "stopped")}
        except (OSError, ValueError):
            return {}

    def _save_desired(self) -> None:
        try:
            self._desired_file().write_text(json.dumps({n: w.desired for n, w in self.workers.items()}), encoding="utf-8")
        except OSError:
            pass

    # ---- starting / stopping
    def _spawn(self, w: Worker) -> None:
        env = {**os.environ, "SAI_PROCESS": w.name, "SAI_SUPERVISED": "1", "PYTHONUNBUFFERED": "1"}
        w.proc = subprocess.Popen(self.argv_for(w.name), env=env, cwd=str(settings.app_dir), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                  encoding="utf-8", errors="replace")
        w.state, w.started_at, w.stopping, w.heartbeat_at, w.info = "running", time.time(), False, None, {}
        self._log(w, f"started (pid {w.proc.pid})")
        threading.Thread(target=self._pump, args=(w, w.proc), daemon=True, name=f"log-{w.name}").start()

    def _pump(self, w: Worker, proc: subprocess.Popen) -> None:
        for line in proc.stdout:   # type: ignore[union-attr]
            self._log(w, line.rstrip("\n"), echo=True)

    def _log(self, w: Worker, text: str, echo: bool = False) -> None:
        w.logs.append({"t": time.time(), "line": text[:4000]})
        print(f"[{w.name}] {text}" if echo else f"[supervisor] {w.name}: {text}", flush=True)

    def _stop(self, w: Worker) -> None:
        p = w.proc
        if not p or p.poll() is not None:
            w.state = "stopped"
            return
        w.stopping = True
        self._log(w, "stopping")
        try:
            p.terminate()
            p.wait(STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._log(w, f"didn't stop within {STOP_TIMEOUT} s: killing it")
            p.kill()
            p.wait(5)
        w.last_exit = {"code": p.returncode, "at": time.time(), "by": "request"}
        w.state = "stopped"

    def start(self, name: str) -> None:
        with self.lock:
            w = self.workers[name]
            w.desired = "running"
            w.crashes.clear()
            w.retry_at = 0
            if not (w.proc and w.proc.poll() is None):
                self._spawn(w)
            self._save_desired()

    def stop(self, name: str) -> None:
        with self.lock:
            w = self.workers[name]
            w.desired = "stopped"
            self._stop(w)
            self._save_desired()

    def restart(self, name: str) -> None:
        with self.lock:
            w = self.workers[name]
            self._stop(w)
            w.desired = "running"
            w.crashes.clear()
            w.restarts += 1
            self._spawn(w)
            self._save_desired()

    def stop_all(self) -> None:
        with self.lock:
            for w in reversed(list(self.workers.values())):
                self._stop(w)

    # ---- watching
    def check(self) -> None:
        """Notice exits, restart crashed workers with a pause, start what should run."""
        now = time.time()
        with self.lock:
            for w in self.workers.values():
                p = w.proc
                if p is not None and p.poll() is not None and not w.stopping and w.state == "running":
                    w.last_exit = {"code": p.returncode, "at": now, "by": "crash"}
                    w.crashes.append(now)
                    recent = [t for t in w.crashes if now - t < CRASH_WINDOW]
                    if len(recent) >= CRASH_LIMIT:
                        w.state = "failed"
                        self._log(w, f"exited with code {p.returncode}; crashed {len(recent)} times in "
                                     f"{CRASH_WINDOW // 60} minutes, not restarting it automatically")
                    else:
                        w.state = "crashed"
                        w.retry_at = now + BACKOFF[min(len(recent) - 1, len(BACKOFF) - 1)]
                        self._log(w, f"exited with code {p.returncode}; restarting in {int(w.retry_at - now)} s")
                    w.proc = None
                if w.desired == "running" and w.proc is None and w.state in ("stopped", "crashed") and now >= w.retry_at:
                    if w.state == "crashed":
                        w.restarts += 1
                    self._spawn(w)

    # ---- control requests
    def handle(self, req: dict) -> dict:
        if not hmac.compare_digest(str(req.get("token") or ""), self.token):
            return {"ok": False, "error": "Not allowed."}
        cmd, name = req.get("cmd"), req.get("name")
        if name is not None and name not in self.workers:
            return {"ok": False, "error": "Unknown process."}
        if cmd == "status":
            return {"ok": True, "status": self.status()}
        if cmd in ("start", "stop", "restart"):
            if not name:
                return {"ok": False, "error": "Which process?"}
            if cmd == "stop" and name == "web" and not req.get("force"):
                return {"ok": False, "error": "The web interface can only be stopped from the command line."}
            getattr(self, cmd)(name)
            return {"ok": True, "status": self.status()}
        if cmd == "logs":
            n = max(1, min(int(req.get("lines") or 200), 2000))
            return {"ok": True, "lines": list(self.workers[name].logs)[-n:]}
        if cmd == "heartbeat":
            w = self.workers[name]
            w.heartbeat_at = time.time()
            w.info = req.get("info") if isinstance(req.get("info"), dict) else {}
            return {"ok": True}
        if cmd == "restart_app":
            threading.Thread(target=self.shutdown, args=(0,), daemon=True).start()
            return {"ok": True}
        return {"ok": False, "error": "Unknown command."}

    def status(self) -> dict:
        with self.lock:
            return {"supervised": True, "started_at": self.started_at, "pid": os.getpid(),
                    "workers": [w.public() for w in self.workers.values()]}

    def serve_control(self) -> None:
        sup = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    line = self.rfile.readline(1 << 20)
                    resp = sup.handle(json.loads(line or b"{}"))
                except Exception as e:  # noqa: BLE001
                    resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                self.wfile.write((json.dumps(resp) + "\n").encode())

        class S(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = S(("127.0.0.1", 0), H)
        f = procs.run_dir() / procs.CONTROL_FILE
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"port": self.server.server_address[1], "token": self.token, "pid": os.getpid()}),
                       encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, f)
        threading.Thread(target=self.server.serve_forever, daemon=True, name="control").start()

    def shutdown(self, code: int = 0) -> None:
        if self.exiting:
            return
        self.exiting = True
        print("[supervisor] stopping all processes", flush=True)
        self.stop_all()
        if self.server:
            self.server.shutdown()
        try:
            (procs.run_dir() / procs.CONTROL_FILE).unlink()
        except OSError:
            pass
        os._exit(code)   # systemd (Restart=always) starts the service again when this was a restart

    def run(self) -> None:
        self.serve_control()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_: threading.Thread(target=self.shutdown, args=(0,), daemon=True).start())
            except (ValueError, OSError):
                pass
        print(f"[supervisor] SysAdmin.IA supervisor running (pid {os.getpid()})", flush=True)
        while not self.exiting:
            try:
                self.check()
            except Exception:  # noqa: BLE001 - keep supervising
                log.exception("supervisor check failed")
            time.sleep(0.5)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    Supervisor().run()


if __name__ == "__main__":
    main()
