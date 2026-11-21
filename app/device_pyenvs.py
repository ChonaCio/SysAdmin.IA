"""Python environments on a device (Automation → Python environments with "Runs on" set to a device).

The same features as pyenvs.py, on the device over SSH with the device's user (no root needed):

  * environments live in ~/.sysadmin-ia/pyenvs/<name> on the device (a normal venv + meta.json);
  * uv (optional, for any Python version) is installed into ~/.sysadmin-ia/tools/uv/bin with its official
    installer (astral.sh/uv/install.sh); the Python versions it downloads go to ~/.sysadmin-ia/pythons;
  * what's there is read by the device agent (`pyenvs` command); creating, installing and removing run as
    background tasks of pyenvs._start whose steps are `ssh … sh -c SCRIPT` (output streamed into the task log).

Run Python job tasks on the device use these environments (dags._run_python_remote).
Every name, version and package is validated with pyenvs' rules before it's put in a script, and quoted.
"""
from __future__ import annotations

import json
import re
import shlex
import time

from . import devices, pyenvs
from .pyenvs import KEY_RE, NAME_RE, VERSION_RE, PyEnvError

BASE = '"$HOME/.sysadmin-ia"'
UV_ENV = ('UV_PYTHON_INSTALL_DIR="$HOME/.sysadmin-ia/pythons" UV_CACHE_DIR="$HOME/.sysadmin-ia/cache/uv" UV_NO_PROGRESS=1 ')
_FETCH = ('fetch() { if command -v curl >/dev/null 2>&1; then curl -fsSL "$1"; elif command -v wget >/dev/null 2>&1; '
          'then wget -qO- "$1"; else echo "curl or wget is needed on the device." >&2; return 1; fi; }; ')

# env names last seen per device (the job editor's environment list for tasks on a device)
_known: dict[int, list[str]] = {}


def env_dir(name: str) -> str:
    """The environment's folder on the device, for a shell script (name already checked)."""
    return f'"$HOME/.sysadmin-ia/pyenvs/{name}"'


def env_python(name: str) -> str:
    return f'"$HOME/.sysadmin-ia/pyenvs/{name}/bin/python"'


def known(dev_id: int) -> list[str]:
    return _known.get(int(dev_id), [])


def _dev(dev_id) -> dict:
    try:
        return devices.get(dev_id)
    except devices.DeviceError as e:
        raise PyEnvError(str(e))


def _raw(d: dict, with_size: bool = True) -> dict:
    try:
        raw = devices.agent(d, "pyenvs", "1" if with_size else "0", timeout=120)
    except devices.DeviceError as e:
        raise PyEnvError(str(e))
    _known[d["id"]] = [e["name"] for e in raw.get("envs", [])]
    return raw


def _run(d: dict, script: str, timeout: float = 60) -> tuple[int, str, str]:
    try:
        return devices.run(d, "sh -c " + shlex.quote(script), timeout=timeout)
    except devices.DeviceError as e:
        raise PyEnvError(str(e))


def _step(d: dict, script: str, title: str) -> tuple:
    """A task step: a script run on the device over SSH (the task log shows the script, not the ssh command)."""
    return (devices.ssh_argv(d) + ["sh -c " + shlex.quote(script)], None, title, f"[{d['name']}] {script}")


def _uv(raw: dict) -> str | None:
    return shlex.quote(raw["uv_path"]) if raw.get("uv_path") else None


def used_by(dev_id: int, name: str) -> list[str]:
    try:
        from . import dags
        return [g["dag_id"] for g in dags.list_all()
                if any(t["operator"] == "run_python" and t.get("device") == dev_id and (t["params"].get("venv") or "") == name
                       for t in g["tasks"])]
    except Exception:  # noqa: BLE001
        return []


def status(dev_id) -> dict:
    d = _dev(dev_id)
    raw = _raw(d)
    envs = [{**{k: v for k, v in e.items() if k != "home"}, "busy": pyenvs._busy(e["name"], d["id"]),
             "used_by": used_by(d["id"], e["name"])} for e in raw["envs"]]
    return {"envs": envs, "interpreters": raw["interpreters"], "uv": raw.get("uv"), "uv_path": raw.get("uv_path"),
            "pythons": python_versions(d["id"], raw=raw), "base": raw["base"], "tasks": pyenvs.tasks(d["id"]),
            "root": raw["root"], "venv": raw.get("venv", True),
            "device": {"id": d["id"], "name": d["name"], "host": d["host"], "user": d["user"]}}


_versions: dict[int, dict] = {}


def python_versions(dev_id, refresh: bool = False, raw: dict | None = None) -> dict:
    d = _dev(dev_id)
    raw = raw or _raw(d)
    have = raw.get("downloaded") or []
    uv = _uv(raw)
    if not uv:
        return {"downloaded": have, "available": [], "uv": False, "error": None, "dir": raw.get("pythons_dir")}
    c = _versions.setdefault(d["id"], {"at": 0.0, "data": None, "error": None})
    if refresh or c["data"] is None or time.time() - c["at"] > 600:
        code, out, err = _run(d, f"{UV_ENV}UV_PYTHON_PREFERENCE=only-managed {uv} python list --output-format json 2>/dev/null "
                                 f"|| {UV_ENV}UV_PYTHON_PREFERENCE=only-managed {uv} python list", timeout=90)
        try:
            c["data"] = pyenvs.parse_uv_json(out) if out.strip().startswith("[") else pyenvs.parse_uv_text(out)
            c["error"] = None if code == 0 else f"uv couldn't list the Python versions: {(err or out).strip()[-300:]}"
        except ValueError:
            c["data"], c["error"] = [], "uv's list of Python versions can't be read."
        c["at"] = time.time()
    return {"downloaded": have, "available": pyenvs.pick_available(c["data"] or [], {h["key"] for h in have}), "uv": True,
            "error": c["error"], "dir": raw.get("pythons_dir")}


def _start(d: dict, kind: str, env: str, steps: list, on_fail=None, on_done=None) -> dict:
    if pyenvs._busy(env, d["id"]):
        raise PyEnvError("That environment is busy (creating or installing): wait until it finishes.")
    return pyenvs._start(kind, env, steps, on_fail=on_fail, on_done=on_done, device=d["id"])


def _forget(d: dict):
    return lambda: _versions.pop(d["id"], None)


def install_uv(dev_id) -> dict:
    d = _dev(dev_id)
    if pyenvs._busy("", d["id"]):
        raise PyEnvError("uv is already being installed.")
    return _start(d, "uv", "", [_step(d, _uv_step_script(), "Installing uv")], on_done=_forget(d))


def _uv_step_script() -> str:
    """uv's official installer into ~/.sysadmin-ia/tools/uv/bin, without touching the user's PATH or shell files."""
    return (_FETCH + 'echo "Downloading uv\'s installer (astral.sh/uv/install.sh)"; mkdir -p "$HOME/.sysadmin-ia/tools/uv/bin" && '
            'T="$HOME/.sysadmin-ia/tools/uv-install.sh"; fetch https://astral.sh/uv/install.sh > "$T" && '
            'env UV_INSTALL_DIR="$HOME/.sysadmin-ia/tools/uv/bin" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh "$T"; '
            'R=$?; rm -f "$T"; exit $R')


def install_python(dev_id, version: str) -> dict:
    d = _dev(dev_id)
    version = (version or "").strip()
    if not VERSION_RE.fullmatch(version):
        raise PyEnvError("Write a version such as 3.12 or 3.12.8 (3.13t for the free-threaded build).")
    raw = _raw(d, with_size=False)
    steps = []
    uv = _uv(raw)
    if not uv:
        steps.append(_step(d, _uv_step_script(), "Installing uv"))
        uv = '"$HOME/.sysadmin-ia/tools/uv/bin/uv"'
    steps.append(_step(d, f"{UV_ENV}{uv} python install {version}", f"Downloading Python {version}"))
    return _start(d, "python", version, steps, on_done=_forget(d))


def uninstall_python(dev_id, key: str) -> dict:
    d = _dev(dev_id)
    raw = _raw(d, with_size=False)
    key = (key or "").strip()
    have = {h["key"]: h for h in raw.get("downloaded") or []}
    if not KEY_RE.fullmatch(key) or key not in have:
        raise PyEnvError("That Python version isn't downloaded (any more).")
    if have[key].get("used_by"):
        users = have[key]["used_by"]
        raise PyEnvError(f"Python {have[key]['version']} is used by the environment{'s' if len(users) != 1 else ''} "
                         f"{', '.join(users)}. Delete {'them' if len(users) != 1 else 'it'} first.")
    uv = _uv(raw)
    folder = f'"$HOME/.sysadmin-ia/pythons/{key}"'
    script = (f"{UV_ENV}{uv} python uninstall {key} || rm -rf -- {folder}") if uv else f"rm -rf -- {folder}"
    return _start(d, "python-remove", key, [_step(d, script, f"Removing Python {have[key]['version']}")], on_done=_forget(d))


def _check_new(raw: dict, name: str) -> str:
    name = pyenvs._check_name(name)
    if any(e["name"] == name for e in raw.get("envs", [])):
        raise PyEnvError(f"There's already an environment called {name} on the device.")
    return name


def _pip_script(name: str, pkgs: list[str], tool: str, uv: str | None) -> str:
    q = " ".join(shlex.quote(p) for p in pkgs)
    if tool == "uv" and uv:
        return f"{UV_ENV}{uv} pip install --python {env_python(name)} {q}"
    return f"{env_python(name)} -m pip install --disable-pip-version-check {q}"


def create(dev_id, name: str, python: str = "", packages: str = "", description: str = "") -> dict:
    """python: "" (the device's python3), a path from its interpreters, or a version like 3.12 (needs uv)."""
    d = _dev(dev_id)
    raw = _raw(d, with_size=False)
    name = _check_new(raw, name)
    pkgs = pyenvs._packages_argv(packages)
    python = (python or "").strip()
    uv = _uv(raw)
    mk = 'mkdir -p "$HOME/.sysadmin-ia/pyenvs" && '
    if VERSION_RE.fullmatch(python):
        if not uv:
            raise PyEnvError(f"Python {python} isn't installed on {d['name']}. Install uv there (button above) to get any "
                             "version, or pick one of the installed versions.")
        steps = [_step(d, mk + f"{UV_ENV}{uv} venv --seed --python {python} {env_dir(name)}",
                       f"Creating the environment with Python {python} (downloaded the first time)")]
        tool = "uv"
    else:
        known_paths = {i["path"] for i in raw.get("interpreters", [])}
        if python and python not in known_paths:
            raise PyEnvError(f"Pick one of the Python versions installed on {d['name']}, or type a version like 3.12.")
        exe = python or raw["base"]["path"]
        hint = ("; R=$?; [ $R = 0 ] || echo \"If it says ensurepip/venv is missing: install the python3-venv package on the "
                "device (sudo apt install python3-venv), or install uv above and pick a version.\"; exit $R")
        steps = [_step(d, mk + f"{shlex.quote(exe)} -m venv {env_dir(name)}{hint}", "Creating the environment")]
        tool = "venv"
    meta = {"description": " ".join((description or "").split())[:300], "created": time.time(), "python": python or "base",
            "tool": tool}
    steps.append(_step(d, f"printf %s {shlex.quote(json.dumps(meta))} > {env_dir(name)}/meta.json", "Saving its settings"))
    if pkgs:
        steps.append(_step(d, _pip_script(name, pkgs, tool, uv),
                           f"Installing {len(pkgs)} package{'s' if len(pkgs) != 1 else ''}"))

    def cleanup():
        _run(d, f"rm -rf -- {env_dir(name)}", timeout=60)
    return _start(d, "create", name, steps, on_fail=cleanup)


def _existing(d: dict, name: str, raw: dict | None = None) -> dict:
    name = pyenvs._check_name(name)
    raw = raw or _raw(d, with_size=False)
    e = next((x for x in raw.get("envs", []) if x["name"] == name), None)
    if not e:
        raise PyEnvError("That environment doesn't exist on the device (any more).")
    if pyenvs._busy(name, d["id"]):
        raise PyEnvError("That environment is busy (creating or installing): wait until it finishes.")
    return {**e, "uv": _uv(raw)}


def install(dev_id, name: str, packages: str, upgrade: bool = False) -> dict:
    d = _dev(dev_id)
    e = _existing(d, name)
    pkgs = pyenvs._packages_argv(packages)
    if not pkgs:
        raise PyEnvError("Write at least one package, e.g. requests.")
    if upgrade:
        pkgs = ["--upgrade", *pkgs]
    return _start(d, "install", e["name"], [_step(d, _pip_script(e["name"], pkgs, e.get("tool"), e["uv"]),
                                                  f"Installing {', '.join(p for p in pkgs if p != '--upgrade')[:200]}")])


def uninstall(dev_id, name: str, package: str) -> dict:
    d = _dev(dev_id)
    e = _existing(d, name)
    pkg = (package or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", pkg):
        raise PyEnvError("Invalid package name.")
    if e.get("tool") == "uv" and e["uv"]:
        script = f"{UV_ENV}{e['uv']} pip uninstall --python {env_python(e['name'])} {pkg}"
    else:
        script = f"{env_python(e['name'])} -m pip uninstall -y {pkg}"
    return _start(d, "uninstall", e["name"], [_step(d, script, f"Removing {pkg}")])


def packages(dev_id, name: str) -> list[dict]:
    d = _dev(dev_id)
    name = pyenvs._check_name(name)
    raw = _raw(d, with_size=False)
    uv = _uv(raw)
    script = f"{env_python(name)} -m pip list --format json --disable-pip-version-check 2>/dev/null"
    if uv:
        script += f" || {UV_ENV}{uv} pip list --python {env_python(name)} --format json"
    code, out, err = _run(d, script, timeout=90)
    if code != 0:
        raise PyEnvError(f"Couldn't list the packages: {(err or out).strip()[-300:]}")
    try:
        return sorted(json.loads(out.strip().splitlines()[-1] if out.strip() else "[]"), key=lambda p: p["name"].lower())
    except (ValueError, IndexError):
        raise PyEnvError("Couldn't read the package list.")


def update(dev_id, name: str, description: str) -> dict:
    d = _dev(dev_id)
    e = _existing(d, name)
    meta = {"description": " ".join((description or "").split())[:300], "created": e.get("created"),
            "python": e.get("requested") or "base", "tool": e.get("tool") or "venv"}
    code, out, err = _run(d, f"printf %s {shlex.quote(json.dumps(meta))} > {env_dir(e['name'])}/meta.json")
    if code != 0:
        raise PyEnvError(f"Couldn't save it: {(err or out).strip()[-300:]}")
    return {**{k: v for k, v in e.items() if k not in ("uv", "home")}, "description": meta["description"]}


def delete(dev_id, name: str) -> None:
    d = _dev(dev_id)
    e = _existing(d, name)
    code, out, err = _run(d, f"rm -rf -- {env_dir(e['name'])}", timeout=120)
    if code != 0:
        raise PyEnvError(f"Couldn't delete it: {(err or out).strip()[-300:]}")
    _known[d["id"]] = [n for n in _known.get(d["id"], []) if n != e["name"]]
