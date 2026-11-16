"""Local AI: run open models on this server with Ollama.

Installs Ollama (official installer, via the root helper), manages its service and context
length, downloads ("pulls") models from the public Ollama library with progress, and lists,
unloads and deletes them. Installed models are offered in AI connections under the provider
"Ollama (Server Managed)".

Ollama listens on 127.0.0.1:11434 (only this server can reach it); SAI_OLLAMA_URL overrides it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

OLLAMA_URL = (os.environ.get("SAI_OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,120}$")
INSTALL_LOG = Path("/var/log/sysadmin-ia/ollama.log")
INSTALL_STATUS = Path("/var/log/sysadmin-ia/ollama.status")
CONTEXT_CHOICES = (4096, 8192, 16384, 32768, 65536, 131072)
MODEL_DIRS = ("/usr/share/ollama/.ollama/models", "/var/lib/ollama/models", "~/.ollama/models")

# Recommended models (the Ollama library has no search API; any other name can be typed in).
# size = download size in GB, ram = memory needed to run it (approx.), tools = supports tool
# calling, which the chat assistant needs to run commands.
CATALOG = [
    {"name": "qwen3:8b", "title": "Qwen 3 8B", "size": 5.2, "ram": 8, "tools": True, "thinking": True,
     "about": "Strong all-rounder with tool use and step-by-step thinking. Good first choice with 16 GB RAM."},
    {"name": "qwen3:4b", "title": "Qwen 3 4B", "size": 2.6, "ram": 5, "tools": True, "thinking": True,
     "about": "Smaller Qwen 3 for modest servers; still handles tools."},
    {"name": "llama3.1:8b", "title": "Llama 3.1 8B", "size": 4.9, "ram": 8, "tools": True,
     "about": "Meta's widely used general model with reliable tool calling."},
    {"name": "llama3.2:3b", "title": "Llama 3.2 3B", "size": 2.0, "ram": 4, "tools": True,
     "about": "Small and fast; fine for simple questions and summaries."},
    {"name": "qwen2.5:7b", "title": "Qwen 2.5 7B", "size": 4.7, "ram": 8, "tools": True,
     "about": "Good at following instructions and structured output."},
    {"name": "qwen2.5-coder:7b", "title": "Qwen 2.5 Coder 7B", "size": 4.7, "ram": 8, "tools": True,
     "about": "Tuned for code and shell scripts."},
    {"name": "mistral-nemo:12b", "title": "Mistral Nemo 12B", "size": 7.1, "ram": 12, "tools": True,
     "about": "Mistral AI's mid-size model with a long context."},
    {"name": "gpt-oss:20b", "title": "gpt-oss 20B", "size": 14, "ram": 16, "tools": True, "thinking": True,
     "about": "OpenAI's open-weight reasoning model. Needs a GPU with 16 GB or lots of RAM."},
    {"name": "phi4-mini", "title": "Phi-4 mini 3.8B", "size": 2.5, "ram": 4, "tools": True,
     "about": "Microsoft's compact model; good on CPU-only servers."},
    {"name": "granite3.3:8b", "title": "Granite 3.3 8B", "size": 4.9, "ram": 8, "tools": True,
     "about": "IBM's business-oriented model with tool use."},
    {"name": "gemma3:4b", "title": "Gemma 3 4B", "size": 3.3, "ram": 5, "tools": False, "vision": True,
     "about": "Google's small model that can read images. No tool calling: good for “Ask the AI” job actions."},
    {"name": "deepseek-r1:8b", "title": "DeepSeek-R1 8B", "size": 5.2, "ram": 8, "tools": False, "thinking": True,
     "about": "Reasoning model that thinks before answering. No tool calling: use it for questions, not server tasks."},
]


class LocalAIError(RuntimeError):
    pass


def available() -> bool:
    return sys.platform.startswith("linux")


# ---------------------------------------------------------------- talking to Ollama

def _req(path: str, method: str = "GET", body: dict | None = None, timeout: float = 10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{OLLAMA_URL}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error") or e.reason
        except Exception:
            msg = e.reason
        raise LocalAIError(f"Ollama: {msg}")
    except (urllib.error.URLError, OSError, TimeoutError):
        raise LocalAIError("Ollama isn't running on this server.")


def running_version() -> str | None:
    try:
        return _req("/api/version", timeout=2).get("version")
    except LocalAIError:
        return None


def _binary() -> str | None:
    return shutil.which("ollama") or next((p for p in ("/usr/local/bin/ollama", "/usr/bin/ollama") if os.path.exists(p)), None)


def _gpus() -> list[str]:
    out = []
    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=5)
            out += [f"{line.split(',')[0].strip()} ({line.split(',')[1].strip()})" for line in r.stdout.splitlines() if "," in line]
        except Exception:
            pass
    if not out and os.path.exists("/dev/kfd"):
        out.append("AMD GPU (ROCm)")
    return out


def _models_dir() -> str | None:
    env = os.environ.get("OLLAMA_MODELS")
    for d in ([env] if env else []) + list(MODEL_DIRS):
        p = os.path.expanduser(d)
        if os.path.isdir(p):
            return p
    return None


def _service_state() -> str:
    if not shutil.which("systemctl"):
        return "unknown"
    try:
        r = subprocess.run(["systemctl", "is-active", "ollama"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _context_length() -> int | None:
    conf = Path("/etc/systemd/system/ollama.service.d/sysadmin-ia.conf")
    try:
        m = re.search(r"OLLAMA_CONTEXT_LENGTH=(\d+)", conf.read_text())
        return int(m.group(1)) if m else None
    except OSError:
        return None


def status() -> dict:
    from . import server_name
    vm = psutil.virtual_memory()
    mdir = _models_dir()
    try:
        disk = psutil.disk_usage(mdir or "/")
        disk_free = disk.free
    except OSError:
        disk_free = None
    version = running_version()
    can_manage = server_name.helper_supports("ollama-install")
    return {
        "available": available(),
        "installed": bool(_binary()) or version is not None,
        "running": version is not None,
        "version": version,
        "service": _service_state(),
        "url": OLLAMA_URL,
        "ram_total": vm.total, "ram_available": vm.available,
        "gpus": _gpus(),
        "models_dir": mdir, "disk_free": disk_free,
        "context_length": _context_length(),
        "context_choices": list(CONTEXT_CHOICES),
        "can_manage": can_manage,
        "setup_command": None if can_manage else server_name.setup_command(),
        "install": install_progress(),
    }


_caps: dict[str, list[str]] = {}   # model digest -> capabilities


def _capabilities(name: str, digest: str) -> list[str]:
    if digest in _caps:
        return _caps[digest]
    try:
        info = _req("/api/show", "POST", {"model": name, "name": name}, timeout=10)
        caps = info.get("capabilities") or []
    except LocalAIError:
        caps = []
    _caps[digest] = caps
    return caps


def models() -> dict:
    """Installed models, what's loaded in memory, and downloads in progress."""
    out: dict = {"running": False, "models": [], "loaded": [], "pulls": pull_progress()}
    try:
        tags = _req("/api/tags")
    except LocalAIError:
        return out
    out["running"] = True
    try:
        ps = {m["name"]: m for m in _req("/api/ps").get("models", [])}
    except LocalAIError:
        ps = {}
    for m in sorted(tags.get("models", []), key=lambda x: x["name"]):
        d = m.get("details") or {}
        loaded = ps.get(m["name"])
        out["models"].append({
            "name": m["name"], "size": m.get("size", 0), "modified": m.get("modified_at"),
            "family": d.get("family", ""), "parameters": d.get("parameter_size", ""),
            "quantization": d.get("quantization_level", ""),
            "capabilities": _capabilities(m["name"], m.get("digest", m["name"])),
            "loaded": bool(loaded),
            "memory": (loaded or {}).get("size", 0), "vram": (loaded or {}).get("size_vram", 0),
            "expires": (loaded or {}).get("expires_at"),
        })
    return out


def installed_names() -> list[str]:
    try:
        return [m["name"] for m in _req("/api/tags", timeout=2).get("models", [])]
    except LocalAIError:
        return []


def catalog() -> list[dict]:
    have = set(installed_names())
    ram = psutil.virtual_memory().total / 2**30
    out = []
    for c in CATALOG:
        tag = c["name"] if ":" in c["name"] else f"{c['name']}:latest"
        out.append({**c, "installed": c["name"] in have or tag in have, "fits": c["ram"] <= ram * 0.9})
    return out


def check_name(name: str) -> str:
    name = (name or "").strip()
    if name.startswith("ollama run ") or name.startswith("ollama pull "):
        name = name.split(" ", 2)[2].strip()
    if not NAME_RE.fullmatch(name):
        raise LocalAIError("Enter a model name like llama3.1:8b (as shown on ollama.com/library).")
    return name


def delete(name: str) -> None:
    name = check_name(name)
    _req("/api/delete", "DELETE", {"model": name, "name": name})


def unload(name: str) -> None:
    name = check_name(name)
    _req("/api/generate", "POST", {"model": name, "keep_alive": 0}, timeout=30)


# ---------------------------------------------------------------- downloads (pull) in the background

_pulls: dict[str, dict] = {}
_pull_lock = threading.RLock()   # re-entrant: progress updates happen while it's held


def pull_progress() -> list[dict]:
    with _pull_lock:
        now = time.time()
        for k in [k for k, v in _pulls.items() if v.get("finished") and now - v["finished"] > 600]:
            _pulls.pop(k)      # forget finished downloads after 10 minutes
        return [dict(v) for v in _pulls.values()]


def _pull_worker(name: str) -> None:
    def upd(**kw):
        with _pull_lock:
            _pulls[name].update(kw)
    req = urllib.request.Request(f"{OLLAMA_URL}/api/pull", method="POST",
                                 data=json.dumps({"model": name, "name": name, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            for raw in r:
                line = raw.strip()
                if not line:
                    continue
                ev = json.loads(line)
                if ev.get("error"):
                    raise LocalAIError(ev["error"])
                kw = {"status": ev.get("status", "")}
                if ev.get("total"):
                    kw.update(total=ev["total"], completed=ev.get("completed", 0))
                upd(**kw)
                with _pull_lock:
                    if _pulls[name].get("cancel"):
                        upd(state="cancelled", finished=time.time(), status="Cancelled")
                        return
        upd(state="done", finished=time.time(), status="Downloaded", completed=_pulls[name].get("total", 0))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error") or e.reason
        except Exception:
            msg = e.reason
        upd(state="failed", finished=time.time(), error=str(msg))
    except Exception as e:
        upd(state="failed", finished=time.time(),
            error="Ollama isn't running." if isinstance(e, (urllib.error.URLError, ConnectionError)) else str(e))


def pull(name: str) -> dict:
    name = check_name(name)
    if running_version() is None:
        raise LocalAIError("Ollama isn't running. Install or start it first.")
    with _pull_lock:
        cur = _pulls.get(name)
        if cur and cur.get("state") == "downloading":
            raise LocalAIError(f"{name} is already downloading.")
        _pulls[name] = {"name": name, "state": "downloading", "status": "Starting…", "total": 0,
                        "completed": 0, "started": time.time(), "error": None}
    threading.Thread(target=_pull_worker, args=(name,), daemon=True, name=f"ollama-pull-{name}").start()
    return _pulls[name]


def cancel_pull(name: str) -> None:
    with _pull_lock:
        if name in _pulls:
            _pulls[name]["cancel"] = True


# ---------------------------------------------------------------- install / service (root helper)

def install_progress() -> dict:
    out = {"state": None, "message": None, "log": ""}
    try:
        for line in INSTALL_STATUS.read_text().splitlines():
            k, _, v = line.partition("=")
            if k in ("state", "message", "updated"):
                out[k] = v
    except OSError:
        return out
    try:
        out["log"] = "\n".join(INSTALL_LOG.read_text(errors="replace").splitlines()[-60:])
    except OSError:
        pass
    return out


def helper(*args: str, timeout: int = 120) -> str:
    from . import server_name
    if not server_name.helper_supports("ollama-install"):
        raise LocalAIError("This needs a one-time setup as root. On the server, run:\n  " + server_name.setup_command())
    try:
        return server_name.run_helper(*args, timeout=timeout)
    except RuntimeError as e:
        raise LocalAIError(str(e))
