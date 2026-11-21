"""Devices: other Linux computers (slave servers) that SysAdmin.IA controls over SSH.

A device is an address + port + user + one of the keys from Server → SSH keys. Everything goes
over SSH with that key (BatchMode: never a password prompt in the background):

  * monitoring – `device_agent.py` is piped into the device's python3 on every call (nothing is
    installed on the device): info (OS, CPU, memory, what's installed) and stats (CPU, memory,
    swap, load, network/disk rates, temperatures, mounted drives with usage);
  * modules    – Python 3, Docker and Ollama are installed from the app in a Console tab
    (`module_command()`: fixed scripts). The key step also sets up passwordless sudo for the device's user
    (`_SUDO_SETUP`, the password is typed once), so installs don't ask for it afterwards;
  * containers / local AI – the Containers and Local AI pages talk to the device's Docker socket and
    Ollama API through an SSH tunnel (`ssh -N -L sock:/var/run/docker.sock -L port:127.0.0.1:11434`),
    so all their features work on the device too. Which device a request is for is a context variable
    (`TARGET`, set by `use()` or by the `?device=` middleware in main.py) read by containers.py and
    local_ai.py;
  * jobs       – a DAG task can run on a device (`dags` task setting `device`): Run a command /
    Run Python / Run console run over SSH, the container and model operators use the tunnel.

SSH connections are shared per device with ControlMaster (sockets in <data>/run/ssh), so polling
a device costs one round trip. Host keys: the app user's ~/.ssh/known_hosts (accept-new), the same
file as the SSH console and Server → SSH keys.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db
from .config import settings

log = logging.getLogger("sysadmin_ia.devices")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.()-]{0,47}$")
HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$"
                     r"|^[0-9a-fA-F:]{2,39}$")
USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
AGENT = Path(__file__).with_name("device_agent.py")
REMOTE_DOCKER_SOCK = "/var/run/docker.sock"
REMOTE_OLLAMA = "127.0.0.1:11434"
INFO_MAX_AGE = 600        # seconds before the device's info (what's installed) is read again
STATS_CACHE = 4           # seconds
TUNNEL_IDLE = 300         # an unused tunnel closes after 5 minutes

# DAG operators that can run on a device (dags.py checks tasks against this).
REMOTE_OPERATORS = ("run_command", "run_python", "run_shell", "container_action", "container_update", "stack_action",
                    "docker_prune", "pull_model")

# Which device the current request / task is for (None = this server). See use().
TARGET: contextvars.ContextVar[int | None] = contextvars.ContextVar("sai_device", default=None)

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (               -- Server → Devices: other computers controlled over SSH
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 22,
    user TEXT NOT NULL,
    key_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    last_seen REAL,
    last_error TEXT,
    last_state TEXT,
    info TEXT,                                     -- JSON from the agent's "info"
    info_at REAL,
    stats TEXT                                     -- JSON: last "stats" (shown while the device is offline)
);
"""


class DeviceError(RuntimeError):
    """kind: unreachable | auth | hostkey | no_python | no_key | ssh_missing | error"""
    def __init__(self, msg: str, kind: str = "error"):
        super().__init__(msg)
        self.kind = kind


_schema_ready = False


def _ensure_schema() -> None:
    global _schema_ready
    if not _schema_ready:
        with db._lock:
            db.conn().executescript(SCHEMA)
        _schema_ready = True


# ---------------------------------------------------------------- storage

def _row(r) -> dict:
    d = dict(r)
    for k in ("info", "stats"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else None
        except ValueError:
            d[k] = None
    return d


def list_devices() -> list[dict]:
    _ensure_schema()
    return [_row(r) for r in db.query("SELECT * FROM devices ORDER BY name COLLATE NOCASE")]


def get(dev_id) -> dict:
    _ensure_schema()
    try:
        dev_id = int(dev_id)
    except (TypeError, ValueError):
        raise DeviceError("Unknown device.")
    r = db.query_one("SELECT * FROM devices WHERE id=?", (dev_id,))
    if not r:
        raise DeviceError("That device doesn't exist (any more).")
    return _row(r)


def resolve(ref) -> int:
    """A device id from an id, "12" or a device name (the DAG code uses names)."""
    _ensure_schema()
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        return get(int(ref))["id"]
    r = db.query_one("SELECT id FROM devices WHERE name=? COLLATE NOCASE", (str(ref or "").strip(),))
    if not r:
        raise DeviceError(f"There's no device called “{str(ref)[:60]}” (Server → Devices).")
    return r["id"]


def public(d: dict) -> dict:
    out = {k: d.get(k) for k in ("id", "name", "host", "port", "user", "key_name", "description", "created", "last_seen",
                                 "last_error", "last_state", "info", "info_at", "stats")}
    out["modules"] = modules_state(d.get("info"))
    return out


def _clean(data: dict, old: dict | None = None) -> dict:
    from . import ssh_keys
    name = " ".join(str(data.get("name", old["name"] if old else "") or "").split())
    if not NAME_RE.fullmatch(name):
        raise DeviceError("Name: up to 48 letters, digits, spaces and _ . - ( ), starting with a letter or digit.")
    host = str(data.get("host", old["host"] if old else "") or "").strip()
    if not HOST_RE.fullmatch(host) or host.startswith("-") or len(host) > 253:
        raise DeviceError("Address: an IP address or a host name like nas.local.")
    try:
        port = int(data.get("port", old["port"] if old else 22) or 22)
    except (TypeError, ValueError):
        raise DeviceError("The SSH port is a number (usually 22).")
    if not 1 <= port <= 65535:
        raise DeviceError("The SSH port is a number between 1 and 65535 (usually 22).")
    user = str(data.get("user", old["user"] if old else "") or "").strip()
    if not USER_RE.fullmatch(user):
        raise DeviceError("User: the account to sign in with on the device, e.g. pi or admin.")
    key = str(data.get("key_name", old["key_name"] if old else "") or "").strip()
    if not key:
        raise DeviceError("Choose the SSH key SysAdmin.IA signs in with (Server → SSH keys).")
    try:
        ssh_keys.private_path(key)
    except ssh_keys.SshKeyError as e:
        raise DeviceError(str(e))
    desc = " ".join(str(data.get("description", old["description"] if old else "") or "").split())[:300]
    return {"name": name, "host": host, "port": port, "user": user, "key_name": key, "description": desc}


def create(data: dict) -> dict:
    _ensure_schema()
    c = _clean(data)
    if db.query_one("SELECT id FROM devices WHERE name=? COLLATE NOCASE", (c["name"],)):
        raise DeviceError(f"There's already a device called {c['name']}.")
    dev_id = db.execute("INSERT INTO devices (name, host, port, user, key_name, description, created) VALUES (?,?,?,?,?,?,?)",
                        (c["name"], c["host"], c["port"], c["user"], c["key_name"], c["description"], time.time()))
    return get(dev_id)


def update(dev_id, data: dict) -> dict:
    old = get(dev_id)
    c = _clean(data, old)
    if db.query_one("SELECT id FROM devices WHERE name=? COLLATE NOCASE AND id<>?", (c["name"], old["id"])):
        raise DeviceError(f"There's already a device called {c['name']}.")
    moved = any(c[k] != old[k] for k in ("host", "port", "user", "key_name"))
    db.execute("UPDATE devices SET name=?, host=?, port=?, user=?, key_name=?, description=? WHERE id=?",
               (c["name"], c["host"], c["port"], c["user"], c["key_name"], c["description"], old["id"]))
    if moved:   # another computer or account: forget what we knew and the open connections
        reset_connections(old)
        db.execute("UPDATE devices SET info=NULL, info_at=NULL, stats=NULL, last_state=NULL, last_error=NULL WHERE id=?", (old["id"],))
    return get(old["id"])


def used_by(dev_id: int) -> list[str]:
    """DAGs with a task that runs on this device."""
    try:
        from . import dags
        return [d["dag_id"] for d in dags.list_all() if any(t.get("device") == dev_id for t in d["tasks"])]
    except Exception:  # noqa: BLE001
        return []


def delete(dev_id) -> None:
    d = get(dev_id)
    used = used_by(d["id"])
    if used:
        raise DeviceError(f"{d['name']} runs tasks of these jobs: {', '.join(used)}. "
                          "Change those tasks to run somewhere else first.")
    reset_connections(d)
    db.execute("DELETE FROM devices WHERE id=?", (d["id"],))
    with contextlib.suppress(OSError):
        shutil.rmtree(_device_dir(d["id"]), ignore_errors=True)


def key_renamed(old: str, new: str) -> None:
    _ensure_schema()
    db.execute("UPDATE devices SET key_name=? WHERE key_name=?", (new, old))


def key_users(name: str) -> list[str]:
    _ensure_schema()
    return [r["name"] for r in db.query("SELECT name FROM devices WHERE key_name=? ORDER BY name", (name,))]


def _device_dir(dev_id: int) -> Path:
    return Path(settings.data_dir) / "devices" / str(int(dev_id))


def stacks_dir(dev_id: int) -> Path:
    p = _device_dir(dev_id) / "stacks"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- SSH

def ssh_binary() -> str:
    exe = shutil.which("ssh")
    if not exe:
        raise DeviceError("The ssh command isn't installed on this server (package openssh-client).", "ssh_missing")
    return exe


def _run_dir(name: str) -> Path:
    p = Path(settings.data_dir) / "run" / name
    p.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(p, 0o700)
    return p


def _key_path(d: dict) -> str:
    from . import ssh_keys
    if not d.get("key_name"):
        raise DeviceError(f"{d['name']} has no SSH key: edit the device and choose one.", "no_key")
    try:
        return ssh_keys.private_path(d["key_name"])
    except ssh_keys.SshKeyError:
        raise DeviceError(f"The SSH key “{d['key_name']}” of {d['name']} was deleted: edit the device and choose another one.", "no_key")


def ssh_argv(d: dict, *, batch: bool = True, tty: bool = False, shared: bool = True) -> list[str]:
    """ssh … -- host (the remote command is appended by the caller)."""
    argv = [ssh_binary(), "-p", str(int(d["port"])), "-l", d["user"], "-i", _key_path(d),
            "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
    if batch:
        argv += ["-o", "BatchMode=yes"]
    if shared:
        argv += ["-o", "ControlMaster=auto", "-o", f"ControlPath={_run_dir('ssh')}/%C", "-o", "ControlPersist=120"]
    else:
        argv += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
    if tty:
        argv.append("-t")
    return argv + ["--", d["host"]]


def _classify(stderr: str, d: dict) -> DeviceError:
    s = stderr or ""
    low = s.lower()
    where = f"{d['user']}@{d['host']}" + (f":{d['port']}" if int(d["port"]) != 22 else "")
    if "remote host identification has changed" in low or "host key verification failed" in low:
        return DeviceError(f"The identity (host key) of {where} has changed, so SSH refuses to connect. If the device was "
                           f"reinstalled, remove {d['host']} from Known devices in Server → SSH keys and try again.", "hostkey")
    if "permission denied" in low or "too many authentication failures" in low:
        return DeviceError(f"{where} didn't accept the key “{d['key_name']}”. Install the key on the device "
                           "(the device's password is asked once).", "auth")
    if "could not resolve" in low or "name or service not known" in low:
        return DeviceError(f"The address {d['host']} can't be found. Check it (an IP address always works).", "unreachable")
    if any(x in low for x in ("connection refused", "timed out", "no route to host", "network is unreachable",
                              "connection closed", "connection reset", "operation timed out")):
        return DeviceError(f"Can't reach {where}: the device is off, not on the network, or SSH isn't running on port "
                           f"{d['port']}.", "unreachable")
    last = [line for line in s.strip().splitlines() if line.strip()]
    return DeviceError(f"SSH to {where} failed: {last[-1][:300] if last else 'unknown error'}", "error")


def run(d: dict, command: str, *, stdin: bytes | None = None, timeout: float = 60) -> tuple[int, str, str]:
    """Run a command on the device (non-interactive). Returns (exit code, stdout, stderr); raises DeviceError when
    SSH itself fails (exit 255 with an ssh message)."""
    argv = ssh_argv(d) + [command]
    try:
        if stdin is not None:
            p = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout)
        else:
            p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DeviceError(f"{d['name']} didn't answer within {int(timeout)} seconds.", "unreachable")
    except FileNotFoundError:
        raise DeviceError("The ssh command isn't installed on this server (package openssh-client).", "ssh_missing")
    out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    if p.returncode == 255 and (not out.strip() or "ssh:" in err or "Permission denied" in err):
        raise _classify(err, d)
    return p.returncode, out, err


_PY_CHECK = "command -v python3 >/dev/null 2>&1 || { echo SAI_NO_PYTHON >&2; exit 127; }; exec python3 - "


AGENT_COMMANDS = ("info", "stats", "ports", "ls", "sizes", "blockdevs", "procs", "pyenvs")


def agent(d: dict, cmd: str, *args: str, timeout: float = 40) -> dict:
    """Run the agent on the device (piped into its python3). args are quoted for the device's shell."""
    if cmd not in AGENT_COMMANDS:
        raise DeviceError("Unknown agent command.")
    line = " ".join([cmd, *(shlex.quote(str(a)) for a in args)])
    code, out, err = run(d, "sh -c " + shlex.quote(_PY_CHECK + line), stdin=AGENT.read_bytes(), timeout=timeout)
    if code == 127 and "SAI_NO_PYTHON" in err:
        raise DeviceError(f"{d['name']} has no Python 3, which SysAdmin.IA needs to read its resources. "
                          "Install the Python module on the device.", "no_python")
    if code != 0:
        raise DeviceError(f"Reading {d['name']} failed: {(err or out).strip().splitlines()[-1][:300] if (err or out).strip() else f'exit code {code}'}")
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise DeviceError(f"{d['name']} sent an answer that can't be read.")


def _mark(d: dict, state: str, error: str | None = None, **cols) -> None:
    sets = ["last_state=?", "last_error=?"] + [f"{k}=?" for k in cols]
    vals = [state, error] + [json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in cols.values()]
    if state == "online":
        sets.append("last_seen=?")
        vals.append(time.time())
    db.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id=?", (*vals, d["id"]))


def probe(dev_id, refresh_info: bool = True) -> dict:
    """Connect, read info (what's installed) and stats. Never raises for device problems: the state says it."""
    d = get(dev_id)
    try:
        info = d.get("info")
        if refresh_info or not info or time.time() - (d.get("info_at") or 0) > INFO_MAX_AGE:
            info = agent(d, "info")
            _mark(d, "online", None, info=info, info_at=time.time())
        st = agent(d, "stats")
        _stats_cache[d["id"]] = (time.time(), st)
        _mark(d, "online", None, stats=st)
    except DeviceError as e:
        _mark(d, e.kind, str(e))
    return public(get(d["id"]))


_stats_cache: dict[int, tuple[float, dict]] = {}


def stats(dev_id) -> dict:
    """Live stats of one device (cached a few seconds), with the device's public data."""
    d = get(dev_id)
    hit = _stats_cache.get(d["id"])
    if hit and time.time() - hit[0] < STATS_CACHE and d.get("last_state") == "online":
        return public(d)
    return probe(d["id"], refresh_info=False)


def overview() -> list[dict]:
    """All devices with fresh stats (in parallel)."""
    devs = list_devices()
    if not devs:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(devs))) as ex:
        return list(ex.map(lambda d: stats(d["id"]), devs))


def test(data: dict) -> dict:
    """Try a device's settings before saving: sign in and read its info."""
    c = _clean(data)
    d = {**c, "id": 0}
    try:
        info = agent(d, "info", timeout=30)
        return {"ok": True, "state": "online", "info": info, "modules": modules_state(info)}
    except DeviceError as e:
        return {"ok": False, "state": e.kind, "error": str(e)}


def ports(dev_id) -> list[int]:
    try:
        return agent(get(dev_id), "ports", timeout=20).get("ports", [])
    except DeviceError:
        return []


def reset_connections(d: dict) -> None:
    """Close the shared SSH connection and the tunnels (after installing Docker the new group needs a new login)."""
    close_tunnel(d["id"])
    with contextlib.suppress(Exception):
        subprocess.run(ssh_argv(d)[:-2] + ["-O", "exit", "--", d["host"]], capture_output=True, timeout=10)


# ---------------------------------------------------------------- modules (installed from the app)

MODULES = {
    "python": {"label": "Python 3", "icon": "python",
               "about": "Needed to read the device's resources and drives, and to run Run Python tasks on it."},
    "docker": {"label": "Docker", "icon": "box",
               "about": "Run containers and stacks on the device from the Containers page."},
    "docker_access": {"label": "Docker access", "icon": "key",
                      "about": "Adds the device's user to the docker group, so SysAdmin.IA can manage Docker there "
                               "(root-equivalent on the device, like on this server)."},
    "ollama": {"label": "Ollama (local AI)", "icon": "chip",
               "about": "Run AI models on the device from the Local AI page and use them in the chat."},
}

_SUDO = 'S=""; if [ "$(id -u)" != 0 ]; then S=sudo; fi; '
_FETCH = ('fetch() { if command -v curl >/dev/null 2>&1; then curl -fsSL "$1"; elif command -v wget >/dev/null 2>&1; '
          'then wget -qO- "$1"; else echo "curl or wget is needed." >&2; exit 1; fi; }; ')
_SCRIPTS = {
    "python": _SUDO + (
        'echo "==> Installing Python 3"; '
        'if command -v apt-get >/dev/null 2>&1; then $S apt-get update && $S apt-get install -y python3; '
        'elif command -v dnf >/dev/null 2>&1; then $S dnf install -y python3; '
        'elif command -v yum >/dev/null 2>&1; then $S yum install -y python3; '
        'elif command -v zypper >/dev/null 2>&1; then $S zypper --non-interactive install python3; '
        'elif command -v apk >/dev/null 2>&1; then $S apk add python3; '
        'elif command -v pacman >/dev/null 2>&1; then $S pacman -S --noconfirm python; '
        'else echo "No known package manager: install python3 on the device by hand."; exit 1; fi && '
        'python3 --version && echo "==> Done"'),
    "docker": _SUDO + _FETCH + (
        'echo "==> Downloading the Docker install script (get.docker.com)"; '
        'fetch https://get.docker.com > /tmp/sai-get-docker.sh && $S sh /tmp/sai-get-docker.sh; R=$?; rm -f /tmp/sai-get-docker.sh; '
        '[ $R = 0 ] || exit $R; '
        'if command -v systemctl >/dev/null 2>&1; then $S systemctl enable --now docker; fi; '
        'if [ "$(id -u)" != 0 ]; then $S usermod -aG docker "$(id -un)"; fi; '
        'echo "==> Done. SysAdmin.IA reconnects to the device so the new docker group applies."'),
    "docker_access": _SUDO + (
        'echo "==> Adding $(id -un) to the docker group"; '
        '$S usermod -aG docker "$(id -un)" && echo "==> Done. SysAdmin.IA reconnects to the device so it applies."'),
    "ollama": _FETCH + (
        'echo "==> Installing Ollama (ollama.com/install.sh)"; '
        'fetch https://ollama.com/install.sh > /tmp/sai-ollama.sh && sh /tmp/sai-ollama.sh; R=$?; rm -f /tmp/sai-ollama.sh; '
        '[ $R = 0 ] && echo "==> Done"; exit $R'),
}


def modules_state(info: dict | None) -> dict:
    """{module: {installed, version, ...}} from the agent's info (None while unknown)."""
    if not info:
        return {k: {"installed": None} for k in MODULES}
    dk, ol = info.get("docker") or {}, info.get("ollama") or {}
    return {
        "python": {"installed": bool(info.get("python")), "version": info.get("python")},
        "docker": {"installed": bool(dk.get("installed")), "version": dk.get("version"), "service": dk.get("service"),
                   "compose": dk.get("compose")},
        "docker_access": {"installed": bool(dk.get("access") or dk.get("group")) if dk.get("installed") else None,
                          "needs_login": bool(dk.get("group") and not dk.get("access"))},
        "ollama": {"installed": bool(ol.get("installed")), "version": ol.get("version"), "running": bool(ol.get("running")),
                   "service": ol.get("service")},
    }


def modules_catalog() -> list[dict]:
    return [{"id": k, **v} for k, v in MODULES.items()]


# Passwordless sudo for the device's user, set up once in the key step (sudo asks for the password that one
# time). Afterwards module installs and anything else that needs sudo run without a password. The rule goes in
# /etc/sudoers.d/sysadmin-ia-<user>, checked with visudo before it's put in place (a file name with a dot is
# ignored by sudo, so the .tmp copy never applies).
_SUDO_SETUP = (
    'if [ "$(id -u)" = 0 ]; then echo "==> Signed in as root: sudo isn\'t needed."; exit 0; fi; '
    'command -v sudo >/dev/null 2>&1 || { echo "sudo isn\'t installed on the device: install it as root first (apt install sudo)."; exit 1; }; '
    'U="$(id -un)"; F="/etc/sudoers.d/sysadmin-ia-$(printf %s "$U" | tr -c "A-Za-z0-9_-" _)"; '
    'if sudo -n true 2>/dev/null; then echo "==> sudo already works without a password for $U."; exit 0; fi; '
    'echo "==> Letting $U use sudo without a password. sudo asks for the password of $U once:"; '
    'printf "%s ALL=(ALL) NOPASSWD: ALL\\n" "$U" | sudo tee "$F.tmp" >/dev/null && sudo chmod 0440 "$F.tmp" '
    '&& sudo visudo -cf "$F.tmp" >/dev/null && sudo mv "$F.tmp" "$F" && sudo -k && sudo -n true '
    '&& echo "==> Done: SysAdmin.IA can now install and manage things on the device without a password." '
    '|| { sudo rm -f "$F.tmp" 2>/dev/null; echo "==> Setting up sudo failed (see above)."; exit 1; }')
SETUP_STEPS = ("key", "setup", "sudo")   # key only · key + sudo · sudo only


def module_command(module: str, d: dict | None = None) -> str:
    """The command a Console tab runs on the device: a module install (fixed scripts), or a setup step
    (key = add the device's key to authorized_keys, sudo = passwordless sudo, setup = both)."""
    if module in SETUP_STEPS:
        from . import ssh_keys
        parts = []
        if module in ("key", "setup"):
            try:
                parts.append(ssh_keys.install_command(d["key_name"]))
            except ssh_keys.SshKeyError as e:
                raise DeviceError(str(e))
        if module in ("sudo", "setup"):
            parts.append(_SUDO_SETUP)
        return "sh -c " + shlex.quote("; ".join(parts))
    if module not in _SCRIPTS:
        raise DeviceError("Unknown module.")
    return "sh -c " + shlex.quote(_SCRIPTS[module])


def console_argv(dev_id, module: str = "", container: str = "") -> list[str]:
    """argv for an interactive Console tab on the device: a shell, a module install or setup step, or a shell in a
    container. Not BatchMode: for the key step ssh asks for the device's password (the key isn't accepted yet)."""
    d = get(dev_id)
    argv = ssh_argv(d, batch=False, tty=True, shared=False)
    if module:
        argv.append(module_command(module, d))
    elif container:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
            raise DeviceError("Invalid container.")
        argv.append(f"docker exec -it {container} /bin/sh -c "
                    + shlex.quote("if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"))
    return argv


# ---------------------------------------------------------------- target (which device a request is for)

def current_id() -> int | None:
    return TARGET.get()


def current() -> dict | None:
    i = TARGET.get()
    return get(i) if i else None


@contextlib.contextmanager
def use(dev_id):
    """Run the block for a device (None/0/"" = this server): containers.py and local_ai.py follow it."""
    token = TARGET.set(int(dev_id) if dev_id not in (None, "", 0, "0") else None)
    try:
        yield
    finally:
        TARGET.reset(token)


def where() -> str:
    d = current()
    return f"the device {d['name']}" if d else "this server"


# ---------------------------------------------------------------- tunnels (Docker socket + Ollama API)

class _Tunnel:
    def __init__(self, proc: subprocess.Popen, sock: str, port: int, errfile):
        self.proc, self.sock, self.port, self.errfile = proc, sock, port, errfile
        self.used = time.time()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.proc.terminate()
            self.proc.wait(3)
        with contextlib.suppress(Exception):
            self.proc.kill()
        with contextlib.suppress(Exception):
            self.errfile.close()
        with contextlib.suppress(OSError):
            os.unlink(self.sock)


_tunnels: dict[int, _Tunnel] = {}
_tlocks: dict[int, threading.Lock] = {}
_tmeta = threading.Lock()
_reaper: threading.Thread | None = None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _reap() -> None:
    while True:
        time.sleep(30)
        for i, t in list(_tunnels.items()):
            if not t.alive() or time.time() - t.used > TUNNEL_IDLE:
                with _tmeta:
                    if _tunnels.get(i) is t:
                        _tunnels.pop(i)
                t.close()


def tunnel(d: dict) -> _Tunnel:
    global _reaper
    with _tmeta:
        lock = _tlocks.setdefault(d["id"], threading.Lock())
        if _reaper is None:
            _reaper = threading.Thread(target=_reap, daemon=True, name="device-tunnels")
            _reaper.start()
    with lock:
        t = _tunnels.get(d["id"])
        if t and t.alive():
            t.used = time.time()
            return t
        if t:
            t.close()
        sock = str(_run_dir("tunnels") / f"d{d['id']}-{os.getpid()}.sock")
        with contextlib.suppress(OSError):
            os.unlink(sock)
        port = _free_port()
        argv = ssh_argv(d, shared=False)[:-2] + [
            "-N", "-o", "ExitOnForwardFailure=yes", "-o", "StreamLocalBindUnlink=yes",
            "-L", f"{sock}:{REMOTE_DOCKER_SOCK}", "-L", f"127.0.0.1:{port}:{REMOTE_OLLAMA}", "--", d["host"]]
        err = tempfile.TemporaryFile()
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                start_new_session=os.name != "nt")
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                err.seek(0)
                msg = err.read().decode("utf-8", "replace")
                err.close()
                raise _classify(msg, d)
            if os.path.exists(sock):
                with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            time.sleep(0.15)
        else:
            proc.kill()
            err.close()
            raise DeviceError(f"{d['name']} didn't answer within 15 seconds.", "unreachable")
        t = _Tunnel(proc, sock, port, err)
        _tunnels[d["id"]] = t
        return t


def close_tunnel(dev_id: int) -> None:
    with _tmeta:
        t = _tunnels.pop(int(dev_id), None)
    if t:
        t.close()


def docker_host() -> str | None:
    """unix:// socket of the current device's Docker (None = this server's)."""
    d = current()
    return f"unix://{tunnel(d).sock}" if d else None


def ollama_url(dev_id=None) -> str | None:
    """http:// address of the current (or given) device's Ollama (None = this server's)."""
    d = get(dev_id) if dev_id else current()
    return f"http://127.0.0.1:{tunnel(d).port}" if d else None


def fresh_info(max_age: float = 20) -> dict:
    """The current device's info, read again if older than max_age seconds."""
    d = current()
    if d.get("info") and time.time() - (d.get("info_at") or 0) < max_age:
        return d["info"]
    info = agent(d, "info")
    _mark(d, "online", None, info=info, info_at=time.time())
    return info


# ---------------------------------------------------------------- running job tasks on a device

async def exec_async(d: dict, command: str, *, stdin: bytes | None = None, timeout: float = 300) -> tuple[int, str]:
    """Run a command on the device for a DAG task: (exit code, stdout+stderr). Raises DeviceError if SSH fails and
    asyncio.TimeoutError after timeout (the ssh process is killed; the command may keep running on the device)."""
    argv = ssh_argv(d) + [command]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        start_new_session=os.name != "nt")
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    o, e = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    if proc.returncode == 255 and not o.strip() and ("ssh:" in e or "Permission denied" in e or "Host key" in e
                                                     or "Connection" in e or "connect to host" in e):
        raise _classify(e, d)
    return proc.returncode, (o + (("\n" if o and not o.endswith("\n") else "") + e if e.strip() else "")).strip()


def remote_env(env: dict) -> str:
    """`env K=V …` prefix for a remote command (values quoted)."""
    return "env " + " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items()) + " " if env else ""


def sudo_free(d: dict) -> bool:
    """True when sudo works without a password on the device (set up in the key step), or the user is root."""
    return ((d.get("info") or {}).get("sudo") in ("nopasswd", "root"))


def _need_sudo(d: dict, what: str) -> None:
    if not sudo_free(d):
        raise DeviceError(f"{what} on {d['name']} needs sudo without a password. Set it up on the device's card in "
                          "Server → Devices (the password is typed once).")


# ---------------------------------------------------------------- files (Drives → browse, folder pickers)

def _agent_checked(d: dict, cmd: str, *args: str, timeout: float = 40) -> dict:
    r = agent(d, cmd, *args, timeout=timeout)
    if isinstance(r, dict) and r.get("error") and cmd in ("ls", "sizes"):
        raise DeviceError(r["error"])
    return r


def list_dir(dev_id, path: str = "/", hidden: bool = False) -> dict:
    d = get(dev_id)
    return {**_agent_checked(d, "ls", path or "/", "1" if hidden else "0"), "device": {"id": d["id"], "name": d["name"]}}


def folder_sizes(dev_id, path: str) -> dict:
    return _agent_checked(get(dev_id), "sizes", path or "/", timeout=60)


def file_stream(dev_id, path: str):
    """(file name, chunks) of a file on the device, read with `cat` over SSH."""
    import posixpath
    d = get(dev_id)
    path = posixpath.normpath("/" + (path or "").lstrip("/"))
    q = shlex.quote(path)
    code, _o, _e = run(d, f"test -f {q} && test -r {q}", timeout=20)
    if code != 0:
        raise DeviceError(f"That's not a file on {d['name']}, or its user can't read it.")
    proc = subprocess.Popen(ssh_argv(d) + [f"cat -- {q}"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=os.name != "nt")

    def chunks():
        try:
            while True:
                b = proc.stdout.read(256 * 1024)
                if not b:
                    break
                yield b
        finally:
            with contextlib.suppress(Exception):
                proc.kill()
                proc.wait(5)
    return posixpath.basename(path) or "file", chunks()


# ---------------------------------------------------------------- drives (the Drives page on a device)

_io_prev: dict[int, tuple[float, dict]] = {}


def drives(dev_id) -> dict:
    """The device's disks and volumes in the Drives page's shape (drives.build_drives on the device's lsblk)."""
    from . import drives as dr
    d = get(dev_id)
    raw = agent(d, "blockdevs", timeout=30)
    dev = {"id": d["id"], "name": d["name"], "host": d["host"]}
    if raw.get("blockdevices") is None:
        return {"available": False, "disks": [], "summary": None, "can_manage": False, "setup_command": None,
                "message": raw.get("error") or "The device's drives can't be read.", "device": dev}
    now, io = raw.get("time") or time.time(), raw.get("io") or {}
    prev = _io_prev.get(d["id"])
    rates = {}
    for name, (r, w) in io.items():
        pr = prev[1].get(name) if prev else None
        dt = now - prev[0] if prev else 0
        rates[name] = {"read_rate": max(0, r - pr[0]) / dt if pr and dt > 0 else 0.0,
                       "write_rate": max(0, w - pr[1]) / dt if pr and dt > 0 else 0.0, "read_total": r, "write_total": w}
    _io_prev[d["id"]] = (now, io)
    usage = raw.get("usage") or {}
    out = dr.build_drives(raw["blockdevices"], rates, lambda mp: usage.get(mp))
    out.update(can_manage=sudo_free(d), setup_command=None, device=dev, mount_base=dr.MOUNT_BASE,
               message=None if sudo_free(d) else
               f"Mounting drives on {d['name']} needs sudo without a password: set it up on its card in Server → Devices.")
    return out


def mount(dev_id, device: str, name: str, persist: bool = False, readonly: bool = False) -> dict:
    """Mount a volume of the device under /mnt/NAME (nosuid,nodev), like the root helper does on this server."""
    from . import drives as dr
    d = get(dev_id)
    _need_sudo(d, "Mounting drives")
    name = (name or "").strip()
    if not dr.NAME_RE.fullmatch(name):
        raise DeviceError("Names use 1–64 letters, digits, dots, dashes or underscores (no spaces).")
    vol = next((v for k in drives(d["id"])["disks"] for v in k["volumes"] if v["path"] == device), None)
    if not vol:
        raise DeviceError("That drive is no longer connected. Refresh the list.")
    if not vol["can_mount"]:
        raise DeviceError(vol["note"] or "This volume can't be mounted (it may already be mounted).")
    target = f"{dr.MOUNT_BASE}/{name}"
    opts = "nosuid,nodev" + (",ro" if readonly else "")
    q, t = shlex.quote(vol["path"]), shlex.quote(target)
    script = (f'T={t}; if mountpoint -q "$T" 2>/dev/null; then echo "$T is already in use. Choose another name." >&2; exit 2; fi; '
              f'if [ -d "$T" ] && [ -n "$(ls -A "$T" 2>/dev/null)" ]; then echo "$T already exists and isn\'t empty. Choose another name." >&2; exit 2; fi; '
              f'sudo -n mkdir -p "$T" && sudo -n mount -o {opts} {q} "$T"')
    if persist:
        if not re.fullmatch(r"[A-Za-z0-9-]{4,64}", vol.get("uuid") or "") or vol["fstype"] not in dr.MOUNTABLE_FS:
            raise DeviceError("This volume has no UUID, so it can't be mounted at startup. Mount it without that option.")
        line = f"UUID={vol['uuid']} {target} {vol['fstype']} {opts},nofail 0 2"
        script += (f' && printf "%s\\n%s\\n" {shlex.quote("# added by SysAdmin.IA: " + target)} {shlex.quote(line)} '
                   '| sudo -n tee -a /etc/fstab >/dev/null')
    code, out, err = run(d, "sh -c " + shlex.quote(script), timeout=90)
    if code != 0:
        raise DeviceError(f"Mounting failed on {d['name']}: {(err or out).strip()[-300:] or f'exit code {code}'}")
    return {"mountpoint": target, "output": out}


def unmount(dev_id, mountpoint: str, forget: bool = True) -> dict:
    from . import drives as dr
    import posixpath
    d = get(dev_id)
    _need_sudo(d, "Unmounting drives")
    mp = posixpath.normpath(mountpoint or "")
    if not mp.startswith(dr.MANAGED_PREFIXES) or not re.fullmatch(r"[A-Za-z0-9/._ -]{2,200}", mp):
        raise DeviceError("Only drives mounted under /mnt, /media or /run/media can be unmounted here.")
    t = shlex.quote(mp)
    script = f"sudo -n umount {t}"
    if forget and re.fullmatch(r"[A-Za-z0-9/._-]{2,200}", mp):   # drop the lines this app added to fstab
        esc = mp.replace(".", "\\.")
        script += f" && sudo -n sed -i '\\|^# added by SysAdmin.IA: {esc}$|{{N;d}}' /etc/fstab"
    code, out, err = run(d, "sh -c " + shlex.quote(script), timeout=90)
    if code != 0:
        raise DeviceError(f"Unmounting failed on {d['name']}: {(err or out).strip()[-300:] or f'exit code {code}'}")
    return {"mountpoint": mp, "output": out}


# ---------------------------------------------------------------- processes (the Processes page on a device)

SIGNALS = ("TERM", "KILL", "HUP", "INT", "STOP", "CONT")


def processes(dev_id, sort: str = "cpu", limit: int = 60) -> dict:
    d = get(dev_id)
    sort = sort if sort in ("cpu", "mem", "name", "pid") else "cpu"
    r = agent(d, "procs", sort, str(max(1, min(500, int(limit or 60)))), timeout=30)
    r["device"] = {"id": d["id"], "name": d["name"], "host": d["host"], "user": d["user"]}
    r["sudo"] = sudo_free(d)
    return r


def signal(dev_id, pid, sig: str) -> dict:
    """Send a signal to a process on the device: as the device's user, or with sudo -n for other users' processes."""
    d = get(dev_id)
    sig = (sig or "").upper()
    if sig not in SIGNALS:
        raise DeviceError("Unknown signal.")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        raise DeviceError("Invalid process.")
    if pid <= 1:
        raise DeviceError("That process can't be signalled from here.")
    cmd = f"kill -s {sig} {pid} 2>/dev/null || sudo -n kill -s {sig} {pid}"
    code, out, err = run(d, "sh -c " + shlex.quote(cmd), timeout=20)
    if code != 0:
        msg = (err or out).strip().splitlines()[-1][:200] if (err or out).strip() else ""
        raise DeviceError(f"Couldn't signal process {pid} on {d['name']}"
                          + (f": {msg}" if msg else "") + (". Processes of other users need sudo without a password "
                                                           "(set it up in Server → Devices)." if not sudo_free(d) else "."))
    return {"ok": True, "pid": pid, "signal": sig}
