"""Python virtual environments for jobs (Automation → Python environments, the "Run Python" operator).

Environments live in <data>/pyenvs/<name>/ (a normal venv) with a small meta.json (description, created,
requested Python). They are made one of two ways:

  * with uv (https://docs.astral.sh/uv/) – any Python version (3.8 … 3.14): uv downloads a standalone build of
    that version into <data>/pythons the first time it's needed. uv itself is installed on request into
    <data>/tools/uv with the app's own pip (no root needed), or used from the PATH if it's already there.
  * without uv – with a Python that's installed on the server (python3, python3.11, … found on the PATH), via
    `python -m venv` (Debian/Ubuntu need the python3-venv package, which install.sh adds).

Creating, installing and removing packages run in the background; tasks() reports their output.
"Base Python" (no environment) is the server's own python3.
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
import uuid
from pathlib import Path

from .config import settings

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")
VERSION_RE = re.compile(r"^3\.\d{1,2}(\.\d{1,2})?$")
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~*+;: '\"@/]*$")   # a requirement line (no options)


class PyEnvError(RuntimeError):
    pass


def root() -> Path:
    d = settings.data_dir / "pyenvs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bin(env_dir: Path, exe: str) -> Path:
    return env_dir / ("Scripts" if os.name == "nt" else "bin") / (exe + (".exe" if os.name == "nt" else ""))


# ---------------------------------------------------------------- interpreters and uv
def uv_path() -> str | None:
    own = settings.data_dir / "tools" / "uv" / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    if own.is_file():
        return str(own)
    return shutil.which("uv")


def _uv_env() -> dict:
    return {**os.environ, "UV_PYTHON_INSTALL_DIR": str(settings.data_dir / "pythons"),
            "UV_CACHE_DIR": str(settings.data_dir / "cache" / "uv"), "UV_NO_PROGRESS": "1"}


def _version_of(exe: str) -> str | None:
    try:
        r = subprocess.run([exe, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
                           capture_output=True, text=True, timeout=10)
        v = r.stdout.strip()
        return v if r.returncode == 0 and re.fullmatch(r"\d+\.\d+\.\d+", v) else None
    except (OSError, subprocess.TimeoutExpired):
        return None


_base_cache: dict = {"at": 0.0, "path": ""}


def base_python() -> str:
    """The server's own python3 (not the app's private environment). Cached for a minute."""
    if time.time() - _base_cache["at"] < 60 and _base_cache["path"]:
        return _base_cache["path"]
    _base_cache.update(at=time.time(), path=_find_base())
    return _base_cache["path"]


def _find_base() -> str:
    app_bin = str(Path(sys.executable).parent)
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p != app_bin)
    for cand in (shutil.which("python3", path=path), shutil.which("python", path=path)):
        if cand and _version_of(cand):      # skip stubs that don't run (e.g. the Windows Store alias)
            return cand
    return getattr(sys, "_base_executable", None) or sys.executable


_interp_cache: dict = {"at": 0.0, "list": []}


def interpreters() -> list[dict]:
    """Python versions installed on the server (python3, python3.X on the PATH, /usr/bin, /usr/local/bin)."""
    if time.time() - _interp_cache["at"] < 60:
        return _interp_cache["list"]
    app_bin = str(Path(sys.executable).parent)
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and d != app_bin] + ["/usr/bin", "/usr/local/bin"]
    seen, out = set(), []
    for d in dict.fromkeys(dirs):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in sorted(names):
            if not re.fullmatch(r"python3(\.\d{1,2})?(\.exe)?|python(\.exe)?", n):
                continue
            p = os.path.join(d, n)
            try:
                real = os.path.realpath(p)
            except OSError:
                continue
            if real in seen or not os.access(p, os.X_OK):
                continue
            seen.add(real)
            v = _version_of(p)
            if v and v.startswith("3."):
                out.append({"path": p, "version": v})
    out.sort(key=lambda x: [int(n) for n in x["version"].split(".")], reverse=True)
    _interp_cache.update(at=time.time(), list=out)
    return out


def uv_version() -> str | None:
    uv = uv_path()
    if not uv:
        return None
    try:
        r = subprocess.run([uv, "--version"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip().split()[-1] if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------- background tasks
_tasks: dict[str, dict] = {}
_lock = threading.RLock()


def tasks() -> list[dict]:
    with _lock:
        cutoff = time.time() - 3600
        for k in [k for k, t in _tasks.items() if t["finished"] and t["finished"] < cutoff]:
            _tasks.pop(k)
        return [{**t, "log": t["log"][-400:]} for t in sorted(_tasks.values(), key=lambda t: t["started"], reverse=True)]


def task(tid: str) -> dict:
    with _lock:
        t = _tasks.get(tid)
        if not t:
            raise PyEnvError("That task is no longer known.")
        return {**t, "log": t["log"][-400:]}


def _busy(name: str) -> bool:
    return any(t["env"] == name and t["state"] == "running" for t in _tasks.values())


def _start(kind: str, env: str, steps, on_fail=None) -> dict:
    """steps: list of (argv, env-or-None, title); run one after the other in a thread, collecting output."""
    tid = uuid.uuid4().hex[:12]
    t = {"id": tid, "kind": kind, "env": env, "state": "running", "log": [], "started": time.time(), "finished": None,
         "error": None}
    with _lock:
        _tasks[tid] = t

    def run():
        try:
            for argv, env_vars, title in steps:
                t["log"].append(f"==> {title}")
                t["log"].append("$ " + " ".join(str(a) for a in argv))
                p = subprocess.Popen([str(a) for a in argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     env=env_vars, cwd=str(settings.data_dir), stdin=subprocess.DEVNULL, errors="replace")
                for line in p.stdout:     # type: ignore[union-attr]
                    t["log"].append(line.rstrip()[:2000])
                    if len(t["log"]) > 5000:
                        del t["log"][:1000]
                if p.wait() != 0:
                    raise PyEnvError(f"{title} failed (exit code {p.returncode}).")
            t["state"] = "done"
        except Exception as e:  # noqa: BLE001 - shown to the user
            t["log"].append(f"!! {e}")
            if on_fail:     # clean up before reporting the failure, so a retry finds a clean slate
                try:
                    on_fail()
                except Exception:  # noqa: BLE001
                    pass
            t["state"], t["error"] = "failed", str(e)
        finally:
            t["finished"] = time.time()

    threading.Thread(target=run, daemon=True, name=f"pyenv-{kind}-{env}").start()
    return {k: v for k, v in t.items() if k != "log"}


def install_uv() -> dict:
    """uv into <data>/tools/uv with the app's own pip (no root)."""
    if any(t["kind"] == "uv" and t["state"] == "running" for t in _tasks.values()):
        raise PyEnvError("uv is already being installed.")
    target = settings.data_dir / "tools" / "uv"
    return _start("uv", "", [([sys.executable, "-m", "pip", "install", "--upgrade", "--target", str(target), "uv"], None,
                              "Installing uv")])


# ---------------------------------------------------------------- environments
def _check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.fullmatch(name):
        raise PyEnvError("Names use lowercase letters, digits, . _ and - (max 40), e.g. data-tools.")
    if name in ("uv", "tasks", "base"):
        raise PyEnvError(f"“{name}” is reserved: pick another name.")
    return name


def _meta(d: Path) -> dict:
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(d: Path, meta: dict) -> None:
    (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")


def _cfg_version(d: Path) -> str | None:
    try:
        for line in (d / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() in ("version", "version_info"):
                return v.strip()
    except OSError:
        pass
    return None


def _size(d: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def env_python(name: str) -> str:
    d = root() / _check_name(name)
    py = _bin(d, "python")
    if not py.exists():
        raise PyEnvError(f"The Python environment “{name}” doesn't exist (any more). Pick another one in the task.")
    return str(py)


def exists(name: str) -> bool:
    try:
        return _bin(root() / _check_name(name), "python").exists()
    except PyEnvError:
        return False


def used_by(name: str) -> list[str]:
    try:
        from . import dags
        return [d["dag_id"] for d in dags.list_all()
                if any(t["operator"] == "run_python" and (t["params"].get("venv") or "") == name for t in d["tasks"])]
    except Exception:  # noqa: BLE001
        return []


def list_envs(with_size: bool = True) -> list[dict]:
    out = []
    for d in sorted(root().iterdir()):
        if not d.is_dir() or not NAME_RE.fullmatch(d.name):
            continue
        meta = _meta(d)
        ok = _bin(d, "python").exists()
        out.append({"name": d.name, "version": _cfg_version(d), "path": str(d), "python": str(_bin(d, "python")),
                    "ok": ok, "description": meta.get("description", ""), "created": meta.get("created"),
                    "requested": meta.get("python", ""), "tool": meta.get("tool", ""),
                    "size": _size(d) if with_size else None, "busy": _busy(d.name), "used_by": used_by(d.name)})
    return out


def status() -> dict:
    base = base_python()
    return {"envs": list_envs(), "interpreters": interpreters(), "uv": uv_version(), "uv_path": uv_path(),
            "base": {"path": base, "version": _version_of(base)}, "tasks": tasks(), "root": str(root())}


def _packages_argv(text: str) -> list[str]:
    pkgs = []
    for line in re.split(r"[\r\n]+", text or ""):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for tok in ([line] if re.search(r"[<>=!~;]", line) else line.split()):
            tok = tok.strip()
            if tok.startswith("-") or not PKG_RE.fullmatch(tok) or len(tok) > 200:
                raise PyEnvError(f"“{tok[:60]}” isn't a package name (options like -r or --index-url aren't allowed). "
                                 "Write one package per line, e.g. requests or pandas>=2.2.")
            pkgs.append(tok)
    if len(pkgs) > 100:
        raise PyEnvError("At most 100 packages at a time.")
    return pkgs


def _install_steps(d: Path, pkgs: list[str], title: str) -> list:
    if not pkgs:
        return []
    uv = uv_path()
    if uv and _meta(d).get("tool") == "uv":
        return [([uv, "pip", "install", "--python", _bin(d, "python"), *pkgs], _uv_env(), title)]
    return [([_bin(d, "python"), "-m", "pip", "install", "--disable-pip-version-check", *pkgs], None, title)]


def create(name: str, python: str = "", packages: str = "", description: str = "") -> dict:
    """python: "" (the server's python3), a path from interpreters(), or a version like 3.12 (needs uv)."""
    name = _check_name(name)
    d = root() / name
    if d.exists():
        raise PyEnvError(f"There's already an environment called {name}.")
    pkgs = _packages_argv(packages)
    python = (python or "").strip()
    uv = uv_path()
    if VERSION_RE.fullmatch(python):
        if not uv:
            raise PyEnvError(f"Python {python} isn't installed on the server. Install uv (button above) to get any version, "
                             "or pick one of the installed versions.")
        steps = [([uv, "venv", "--seed", "--python", python, d], _uv_env(), f"Creating the environment with Python {python} "
                                                                             "(downloaded the first time)")]
        tool = "uv"
    else:
        known = {i["path"] for i in interpreters()}
        exe = python or base_python()
        if python and python not in known:
            raise PyEnvError("Pick one of the Python versions installed on the server, or type a version like 3.12.")
        steps = [([exe, "-m", "venv", d], None, "Creating the environment")]
        tool = "venv"
    d.parent.mkdir(parents=True, exist_ok=True)
    meta = {"description": " ".join((description or "").split())[:300], "created": time.time(), "python": python or "base",
            "tool": tool}
    # meta.json is written once the venv exists (a tiny python step keeps it in the same task)
    meta_step = ([sys.executable, "-c", "import json,sys; open(sys.argv[1],'w').write(sys.argv[2])", d / "meta.json",
                  json.dumps(meta)], None, "Saving its settings")
    steps = steps + [meta_step]
    if pkgs:
        steps += _install_steps_for_new(d, pkgs, tool, uv)
    return _start("create", name, steps, on_fail=lambda: shutil.rmtree(d, ignore_errors=True))


def _install_steps_for_new(d: Path, pkgs: list[str], tool: str, uv: str | None) -> list:
    title = f"Installing {len(pkgs)} package{'s' if len(pkgs) != 1 else ''}"
    if tool == "uv" and uv:
        return [([uv, "pip", "install", "--python", _bin(d, "python"), *pkgs], _uv_env(), title)]
    return [([_bin(d, "python"), "-m", "pip", "install", "--disable-pip-version-check", *pkgs], None, title)]


def _env_dir(name: str) -> Path:
    d = root() / _check_name(name)
    if not d.is_dir():
        raise PyEnvError("That environment doesn't exist (any more).")
    if _busy(d.name):
        raise PyEnvError("That environment is busy (creating or installing): wait until it finishes.")
    return d


def install(name: str, packages: str, upgrade: bool = False) -> dict:
    d = _env_dir(name)
    pkgs = _packages_argv(packages)
    if not pkgs:
        raise PyEnvError("Write at least one package, e.g. requests.")
    if upgrade:
        pkgs = ["--upgrade", *pkgs]
    steps = _install_steps(d, pkgs, f"Installing {', '.join(p for p in pkgs if p != '--upgrade')[:200]}")
    return _start("install", d.name, steps)


def uninstall(name: str, package: str) -> dict:
    d = _env_dir(name)
    pkg = (package or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", pkg):
        raise PyEnvError("Invalid package name.")
    uv = uv_path()
    if uv and _meta(d).get("tool") == "uv":
        steps = [([uv, "pip", "uninstall", "--python", _bin(d, "python"), pkg], _uv_env(), f"Removing {pkg}")]
    else:
        steps = [([_bin(d, "python"), "-m", "pip", "uninstall", "-y", pkg], None, f"Removing {pkg}")]
    return _start("uninstall", d.name, steps)


def packages(name: str) -> list[dict]:
    d = root() / _check_name(name)
    py = _bin(d, "python")
    if not py.exists():
        raise PyEnvError("That environment doesn't exist (any more).")
    try:
        r = subprocess.run([str(py), "-m", "pip", "list", "--format", "json", "--disable-pip-version-check"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PyEnvError(f"Couldn't list the packages: {e}")
    if r.returncode != 0:
        uv = uv_path()      # an environment made by uv without pip
        if uv:
            r = subprocess.run([uv, "pip", "list", "--python", str(py), "--format", "json"], capture_output=True, text=True,
                               timeout=60, env=_uv_env())
        if r.returncode != 0:
            raise PyEnvError(f"Couldn't list the packages: {(r.stderr or r.stdout).strip()[-300:]}")
    try:
        return sorted(json.loads(r.stdout or "[]"), key=lambda p: p["name"].lower())
    except ValueError:
        raise PyEnvError("Couldn't read the package list.")


def update(name: str, description: str) -> dict:
    """Edit an environment's description (packages are edited with install/uninstall; a venv can't be renamed
    because its own path is written inside it)."""
    d = _env_dir(name)
    meta = _meta(d)
    meta["description"] = " ".join((description or "").split())[:300]
    _write_meta(d, meta)
    return next(e for e in list_envs(with_size=False) if e["name"] == d.name)


def delete(name: str) -> None:
    d = _env_dir(name)
    shutil.rmtree(d)
