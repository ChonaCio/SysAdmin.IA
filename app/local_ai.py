"""Local AI: run open models on this server with Ollama.

Installs Ollama (official installer, via the root helper), manages its service and context
length, downloads ("pulls") models from the public Ollama library with progress, and lists,
unloads and deletes them. Installed models are offered in AI connections under the provider
"Ollama (Server Managed)".

Ollama listens on 127.0.0.1:11434 (only this server can reach it); SAI_OLLAMA_URL overrides it.
On a device (Server → Devices) the same API is reached through an SSH tunnel: see devices.py; the
device's Ollama is installed from the Devices page (no root helper there).
"""
from __future__ import annotations

import contextvars
import http.client
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

# Recommended models (the Ollama library has no search API; any other name, including
# hf.co/USER/REPO:QUANT from Hugging Face, can be typed in).
# size = download size in GB, ram = memory needed to run it (both approximate; None = unknown),
# tools = supports tool calling (needed by the chat assistant to run commands; None = unknown).
CATEGORIES = [
    ("chat", "Chat & tools", "General assistants. Those marked Tools can run commands for you in the chat."),
    ("tiny", "Tiny & low-cost", "Very small models for weak servers, Raspberry Pis or quick simple tasks. Lower quality."),
    ("huge", "Huge", "The largest open models. Need a powerful server: lots of RAM or several big GPUs."),
    ("code", "Coding", "Tuned for code, scripts and configuration files."),
    ("vision", "Vision", "Understand images you give them (describe screenshots, read photos of text)."),
    ("embedding", "Embeddings", "Turn text into vectors for search and retrieval (RAG). Not for chatting."),
    ("image", "Image generation", "Create images from text. Experimental in Ollama: officially macOS only for now; "
     "Linux support is announced but may not work yet on this server."),
    ("hf", "From Hugging Face", "GGUF models hosted on Hugging Face, which Ollama downloads directly (hf.co/…). "
     "Thousands more at huggingface.co/models?library=gguf."),
]
_L, _HF = "Ollama library", "Hugging Face"
CATALOG = [
    # chat & tools
    {"cat": "chat", "name": "qwen3:8b", "title": "Qwen 3 8B", "size": 5.2, "ram": 8, "tools": True, "thinking": True,
     "about": "Strong all-rounder with tool use and step-by-step thinking. Good first choice with 16 GB RAM."},
    {"cat": "chat", "name": "qwen3:4b", "title": "Qwen 3 4B", "size": 2.6, "ram": 5, "tools": True, "thinking": True,
     "about": "Smaller Qwen 3 for modest servers; still handles tools."},
    {"cat": "chat", "name": "llama3.1:8b", "title": "Llama 3.1 8B", "size": 4.9, "ram": 8, "tools": True,
     "about": "Meta's widely used general model with reliable tool calling."},
    {"cat": "chat", "name": "llama3.2:3b", "title": "Llama 3.2 3B", "size": 2.0, "ram": 4, "tools": True,
     "about": "Small and fast; fine for simple questions and summaries."},
    {"cat": "chat", "name": "qwen2.5:7b", "title": "Qwen 2.5 7B", "size": 4.7, "ram": 8, "tools": True,
     "about": "Good at following instructions and structured output."},
    {"cat": "chat", "name": "mistral-nemo:12b", "title": "Mistral Nemo 12B", "size": 7.1, "ram": 12, "tools": True,
     "about": "Mistral AI's mid-size model with a long context."},
    {"cat": "chat", "name": "gpt-oss:20b", "title": "gpt-oss 20B", "size": 14, "ram": 16, "tools": True, "thinking": True,
     "about": "OpenAI's open-weight reasoning model. Needs a GPU with 16 GB or lots of RAM."},
    {"cat": "chat", "name": "phi4-mini", "title": "Phi-4 mini 3.8B", "size": 2.5, "ram": 4, "tools": True,
     "about": "Microsoft's compact model; good on CPU-only servers."},
    {"cat": "chat", "name": "granite3.3:8b", "title": "Granite 3.3 8B", "size": 4.9, "ram": 8, "tools": True,
     "about": "IBM's business-oriented model with tool use."},
    {"cat": "chat", "name": "deepseek-r1:8b", "title": "DeepSeek-R1 8B", "size": 5.2, "ram": 8, "tools": False, "thinking": True,
     "about": "Reasoning model that thinks before answering. No tool calling: use it for questions, not server tasks."},
    # tiny & low-cost
    {"cat": "tiny", "name": "qwen3:0.6b", "title": "Qwen 3 0.6B", "size": 0.5, "ram": 1, "tools": True, "thinking": True,
     "about": "Remarkably capable for its size; runs almost anywhere."},
    {"cat": "tiny", "name": "qwen3:1.7b", "title": "Qwen 3 1.7B", "size": 1.4, "ram": 2, "tools": True, "thinking": True,
     "about": "A bit more capable while staying very light."},
    {"cat": "tiny", "name": "llama3.2:1b", "title": "Llama 3.2 1B", "size": 1.3, "ram": 2, "tools": True,
     "about": "Meta's smallest Llama; fast summaries and classification."},
    {"cat": "tiny", "name": "gemma3:270m", "title": "Gemma 3 270M", "size": 0.3, "ram": 1, "tools": False,
     "about": "Google's tiniest model, for very simple text tasks."},
    {"cat": "tiny", "name": "smollm2:360m", "title": "SmolLM2 360M", "size": 0.7, "ram": 1, "tools": False,
     "about": "Hugging Face's compact model for low-power devices."},
    {"cat": "tiny", "name": "tinyllama", "title": "TinyLlama 1.1B", "size": 0.6, "ram": 1, "tools": False,
     "about": "Classic small model; fine for experiments."},
    # huge
    {"cat": "huge", "name": "llama3.3:70b", "title": "Llama 3.3 70B", "size": 43, "ram": 48, "tools": True,
     "about": "Near frontier-quality answers; needs about 48 GB of GPU memory or RAM."},
    {"cat": "huge", "name": "gpt-oss:120b", "title": "gpt-oss 120B", "size": 65, "ram": 80, "tools": True, "thinking": True,
     "about": "OpenAI's large open-weight reasoning model; one 80 GB GPU or lots of RAM."},
    {"cat": "huge", "name": "mistral-large:123b", "title": "Mistral Large 123B", "size": 73, "ram": 80, "tools": True,
     "about": "Mistral AI's flagship open model."},
    {"cat": "huge", "name": "qwen3:235b", "title": "Qwen 3 235B (MoE)", "size": 142, "ram": 160, "tools": True, "thinking": True,
     "about": "Mixture-of-experts flagship: fast for its size, but needs lots of memory."},
    {"cat": "huge", "name": "llama3.1:405b", "title": "Llama 3.1 405B", "size": 243, "ram": 256, "tools": True,
     "about": "Meta's largest open model. Data-centre hardware only."},
    {"cat": "huge", "name": "deepseek-r1:671b", "title": "DeepSeek-R1 671B", "size": 404, "ram": 450, "tools": False, "thinking": True,
     "about": "The full DeepSeek-R1 reasoning model. Data-centre hardware only."},
    # coding
    {"cat": "code", "name": "qwen2.5-coder:7b", "title": "Qwen 2.5 Coder 7B", "size": 4.7, "ram": 8, "tools": True,
     "about": "Tuned for code and shell scripts."},
    {"cat": "code", "name": "qwen3-coder:30b", "title": "Qwen 3 Coder 30B (MoE)", "size": 19, "ram": 24, "tools": True,
     "about": "Strong coding agent model; fast thanks to mixture-of-experts."},
    {"cat": "code", "name": "deepseek-coder-v2:16b", "title": "DeepSeek-Coder-V2 16B", "size": 8.9, "ram": 12, "tools": False,
     "about": "Code completion and explanation across many languages."},
    {"cat": "code", "name": "codellama:7b", "title": "Code Llama 7B", "size": 3.8, "ram": 8, "tools": False,
     "about": "Meta's classic code model."},
    # vision
    {"cat": "vision", "name": "gemma3:4b", "title": "Gemma 3 4B", "size": 3.3, "ram": 5, "tools": False, "vision": True,
     "about": "Google's small model that can read images."},
    {"cat": "vision", "name": "qwen2.5vl:7b", "title": "Qwen 2.5 VL 7B", "size": 6.0, "ram": 10, "tools": False, "vision": True,
     "about": "Very good at reading documents, screenshots and charts."},
    {"cat": "vision", "name": "llama3.2-vision:11b", "title": "Llama 3.2 Vision 11B", "size": 7.8, "ram": 12, "tools": False, "vision": True,
     "about": "Meta's model for describing and reasoning about images."},
    {"cat": "vision", "name": "minicpm-v:8b", "title": "MiniCPM-V 8B", "size": 5.5, "ram": 9, "tools": False, "vision": True,
     "about": "Compact, accurate image understanding and OCR."},
    {"cat": "vision", "name": "llava:7b", "title": "LLaVA 7B", "size": 4.7, "ram": 8, "tools": False, "vision": True,
     "about": "The original open vision-language model."},
    {"cat": "vision", "name": "moondream", "title": "Moondream 2", "size": 1.7, "ram": 3, "tools": False, "vision": True,
     "about": "Tiny vision model for small devices."},
    # embeddings
    {"cat": "embedding", "name": "nomic-embed-text", "title": "Nomic Embed Text", "size": 0.27, "ram": 1, "tools": False, "embedding": True,
     "about": "Popular general-purpose text embeddings."},
    {"cat": "embedding", "name": "mxbai-embed-large", "title": "mxbai Embed Large", "size": 0.67, "ram": 1, "tools": False, "embedding": True,
     "about": "High-quality English embeddings."},
    {"cat": "embedding", "name": "bge-m3", "title": "BGE-M3", "size": 1.2, "ram": 2, "tools": False, "embedding": True,
     "about": "Multilingual embeddings (100+ languages)."},
    {"cat": "embedding", "name": "all-minilm", "title": "all-MiniLM", "size": 0.05, "ram": 1, "tools": False, "embedding": True,
     "about": "Tiny and very fast embeddings."},
    # image generation (experimental in Ollama)
    {"cat": "image", "name": "x/z-image-turbo", "title": "Z-Image Turbo 6B", "size": None, "ram": None, "tools": False, "image": True,
     "experimental": True, "about": "Alibaba's fast text-to-image model. Experimental in Ollama."},
    {"cat": "image", "name": "x/flux2-klein", "title": "FLUX.2 [klein]", "size": None, "ram": None, "tools": False, "image": True,
     "experimental": True, "about": "Black Forest Labs' compact text-to-image model. Experimental in Ollama."},
    # from Hugging Face (GGUF)
    {"cat": "hf", "name": "hf.co/unsloth/Qwen3-8B-GGUF:Q4_K_M", "title": "Qwen 3 8B (Unsloth GGUF)", "size": 5.0, "ram": 8, "tools": None,
     "source": _HF, "about": "Unsloth's quantized Qwen 3; alternative to the library build."},
    {"cat": "hf", "name": "hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M", "title": "Llama 3.2 3B Instruct (bartowski)", "size": 2.0, "ram": 4,
     "tools": None, "source": _HF, "about": "Widely used community quantization."},
    {"cat": "hf", "name": "hf.co/bartowski/Phi-3.5-mini-instruct-GGUF:Q4_K_M", "title": "Phi-3.5 mini Instruct (bartowski)", "size": 2.4, "ram": 4,
     "tools": None, "source": _HF, "about": "Microsoft's small model with a long context."},
    {"cat": "hf", "name": "hf.co/bartowski/gemma-2-2b-it-GGUF:Q4_K_M", "title": "Gemma 2 2B Instruct (bartowski)", "size": 1.7, "ram": 3,
     "tools": None, "source": _HF, "about": "Small Google model, good quality for its size."},
]
for _c in CATALOG:
    _c.setdefault("source", _L)


class LocalAIError(RuntimeError):
    pass


def available() -> bool:
    return sys.platform.startswith("linux")


# ---------------------------------------------------------------- talking to Ollama

def _device():
    from . import devices
    return devices.current()


def _url() -> str:
    """Ollama's address: this server's, or the current device's through an SSH tunnel (devices.py)."""
    from . import devices
    if devices.current_id():
        try:
            return devices.ollama_url()
        except devices.DeviceError as e:
            raise LocalAIError(str(e))
    return OLLAMA_URL


def _not_running() -> str:
    d = _device()
    return f"Ollama isn't running on {d['name']}." if d else "Ollama isn't running on this server."


def _req(path: str, method: str = "GET", body: dict | None = None, timeout: float = 10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{_url()}{path}", data=data, method=method,
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
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
        raise LocalAIError(_not_running())


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


def _remote_status(d: dict) -> dict:
    from . import devices
    out = {"available": True, "installed": False, "running": False, "version": None, "service": "unknown",
           "url": None, "ram_total": None, "ram_available": None, "gpus": [], "models_dir": None, "disk_free": None,
           "context_length": None, "context_choices": list(CONTEXT_CHOICES), "can_manage": False, "setup_command": None,
           "install": {"state": None, "message": None, "log": "", "progress": None, "stage": ""},
           "device": {"id": d["id"], "name": d["name"], "host": d["host"]}}
    try:
        info = devices.fresh_info()
    except devices.DeviceError as e:
        out["error"] = str(e)
        out["device"]["state"] = e.kind
        return out
    ol = info.get("ollama") or {}
    st = d.get("stats") or {}
    out.update(installed=bool(ol.get("installed")), service=ol.get("service") or "unknown", gpus=info.get("gpus") or [],
               models_dir=ol.get("models_dir"), context_length=ol.get("context_length"),
               ram_total=(st.get("mem") or {}).get("total") or info.get("mem_total"),
               ram_available=(st.get("mem") or {}).get("available"))
    version = running_version()
    out.update(running=version is not None, version=version, installed=out["installed"] or version is not None)
    mdir = out["models_dir"] or "/"
    for dr in sorted(st.get("drives") or [], key=lambda x: -len(x["mount"])):
        if mdir == dr["mount"] or mdir.startswith(dr["mount"].rstrip("/") + "/"):
            out["disk_free"] = dr["free"]
            break
    return out


def status() -> dict:
    from . import server_name
    d = _device()
    if d:
        return _remote_status(d)
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


def catalog() -> dict:
    have = set(installed_names())
    ram = psutil.virtual_memory().total / 2**30
    d = _device()
    if d:
        ram = (((d.get("stats") or {}).get("mem") or {}).get("total") or (d.get("info") or {}).get("mem_total") or 0) / 2**30
    models = []
    for c in CATALOG:
        tag = c["name"] if ":" in c["name"].rsplit("/", 1)[-1] else f"{c['name']}:latest"
        models.append({**c, "installed": c["name"] in have or tag in have,
                       "fits": None if c["ram"] is None else c["ram"] <= ram * 0.9})
    return {"categories": [{"id": i, "title": t, "about": a} for i, t, a in CATEGORIES], "models": models,
            "notes": ["Video generation isn't supported by Ollama, so no video models are listed."]}


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

_pulls: dict[str, dict] = {}    # key: "<device id or ''>|<model>"
_pull_lock = threading.RLock()   # re-entrant: progress updates happen while it's held


def _pkey(name: str) -> str:
    from . import devices
    return f"{devices.current_id() or ''}|{name}"


def pull_progress() -> list[dict]:
    """Downloads on the current target (this server or the current device)."""
    from . import devices
    dev = devices.current_id()
    with _pull_lock:
        now = time.time()
        for k in [k for k, v in _pulls.items() if v.get("finished") and now - v["finished"] > 600]:
            _pulls.pop(k)      # forget finished downloads after 10 minutes
        return [dict(v) for v in _pulls.values() if v.get("device") == dev]


def _pull_worker(name: str) -> None:
    key = _pkey(name)

    def upd(**kw):
        with _pull_lock:
            _pulls[key].update(kw)
    try:
        base = _url()
    except LocalAIError as e:
        upd(state="failed", finished=time.time(), error=str(e))
        return
    req = urllib.request.Request(f"{base}/api/pull", method="POST",
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
                    if _pulls[key].get("cancel"):
                        upd(state="cancelled", finished=time.time(), status="Cancelled")
                        return
        upd(state="done", finished=time.time(), status="Downloaded", completed=_pulls[key].get("total", 0))
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
    from . import devices
    key = _pkey(name)
    with _pull_lock:
        cur = _pulls.get(key)
        if cur and cur.get("state") == "downloading":
            raise LocalAIError(f"{name} is already downloading.")
        _pulls[key] = {"name": name, "state": "downloading", "status": "Starting…", "total": 0, "device": devices.current_id(),
                       "completed": 0, "started": time.time(), "error": None}
    ctx = contextvars.copy_context()      # the download keeps going to the same device
    threading.Thread(target=ctx.run, args=(_pull_worker, name), daemon=True, name=f"ollama-pull-{name}").start()
    return dict(_pulls[key])


def cancel_pull(name: str) -> None:
    with _pull_lock:
        if _pkey(name) in _pulls:
            _pulls[_pkey(name)]["cancel"] = True


# ---------------------------------------------------------------- install / service (root helper)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PROGRESS = re.compile(r"(?:#{2,}|\bO=\s*#|^\s*#\s).*?(\d{1,3}(?:[.,]\d)?)\s*%|^\s*(\d{1,3}(?:[.,]\d)?)\s*%\s*$")


def parse_install_log(raw: str) -> dict:
    """Turn the installer's terminal output into clean lines + the current download progress.

    The installer draws its progress bar with carriage returns ("#####   45.2%\r######   46.0%\r…"),
    which a log file stores as one huge line. Like a terminal, keep only the latest state of each
    line, drop the progress-bar lines from the text, and report their percentage separately."""
    lines: list[str] = []
    pct: float | None = None
    stage = ""
    for line in _ANSI.sub("", raw.replace("\r\n", "\n")).split("\n"):
        if "\r" in line:
            parts = [p for p in line.split("\r") if p.strip()]
            line = parts[-1] if parts else ""
        m = _PROGRESS.search(line)
        if m:
            pct = min(100.0, float((m.group(1) or m.group(2)).replace(",", ".")))
            continue
        if not line.strip():
            continue
        txt = line.strip()
        if txt.startswith(">>>") or txt.startswith("==>"):
            stage = txt.lstrip(">=").strip()
            if pct is not None and pct >= 100:
                pct = None  # a new step starts after a finished download
        lines.append(line.rstrip())
    return {"lines": lines, "progress": pct, "stage": stage}


def install_progress() -> dict:
    out = {"state": None, "message": None, "log": "", "progress": None, "stage": ""}
    try:
        for line in INSTALL_STATUS.read_text().splitlines():
            k, _, v = line.partition("=")
            if k in ("state", "message", "updated"):
                out[k] = v
    except OSError:
        return out
    try:
        parsed = parse_install_log(INSTALL_LOG.read_text(errors="replace"))
        out["log"] = "\n".join(parsed["lines"][-60:])
        out["progress"] = parsed["progress"]
        out["stage"] = parsed["stage"]
    except OSError:
        pass
    if out["state"] == "done":
        out["progress"] = 100.0
    return out


def helper(*args: str, timeout: int = 120) -> str:
    from . import server_name
    d = _device()
    if d:
        raise LocalAIError(f"On a device, Ollama is installed and managed from Server → Devices → {d['name']}.")
    if not server_name.helper_supports("ollama-install"):
        raise LocalAIError("This needs a one-time setup as root. On the server, run:\n  " + server_name.setup_command())
    try:
        return server_name.run_helper(*args, timeout=timeout)
    except RuntimeError as e:
        raise LocalAIError(str(e))
