"""Containers: install Docker and manage containers, images, volumes, networks and stacks.

Docker is installed with its official script (get.docker.com) through the root helper, which also
adds the service user to the `docker` group. After the app restarts it talks to the Docker Engine
API on /var/run/docker.sock (SAI_DOCKER_HOST=tcp://host:port overrides it, for tests).

Membership of the docker group is root-equivalent (anyone who controls Docker can mount the
host's disks into a container), so the page says so before installing or granting access.

Making the host ↔ container connection easy:
  * ports: suggested from the image (EXPOSE), host port = the same if free, else the next free one;
    conflicts with other containers or programs listening on the host are reported up front;
  * folders: any folder on the drives from the Drives module can be shared into a container
    (bind mount); missing folders are created by Docker; the image's VOLUMEs are suggested as
    /mnt/DRIVE/appdata/NAME/... so data lives on the drive you pick.
Long operations (pulling images, creating/recreating containers, compose stacks) run as background
tasks with progress, like model downloads in Local AI.
"""
from __future__ import annotations

import http.client
import json
import os
import posixpath
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import psutil

from .config import settings

try:  # POSIX only
    import grp
    import pwd
except ImportError:  # pragma: no cover - Windows development machines
    grp = pwd = None  # type: ignore[assignment]

DOCKER_HOST = os.environ.get("SAI_DOCKER_HOST") or "unix:///var/run/docker.sock"
INSTALL_LOG = Path("/var/log/sysadmin-ia/docker.log")
INSTALL_STATUS = Path("/var/log/sysadmin-ia/docker.status")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,254}$", re.I)
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
RESTART_POLICIES = {"no", "always", "unless-stopped", "on-failure"}
# Host folders that are refused as shared folders unless "allow system paths" is ticked.
SYSTEM_PATHS = ("/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run", "/sbin",
                "/sys", "/usr", "/var/lib/docker", "/var/run/docker.sock", "/run/docker.sock")
DEFAULT_NETWORKS = {"bridge", "host", "none"}


class DockerError(RuntimeError):
    pass


def available() -> bool:
    return sys.platform.startswith("linux") or DOCKER_HOST.startswith("tcp://")


# ---------------------------------------------------------------- Docker Engine API client

class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def _device():
    """The device this request is for (Server → Devices), or None for this server."""
    from . import devices
    return devices.current()


def _host() -> str:
    """Docker address: this server's, or the current device's through an SSH tunnel (devices.py)."""
    from . import devices
    if devices.current_id():
        try:
            return devices.docker_host()
        except devices.DeviceError as e:
            raise DockerError(str(e))
    return DOCKER_HOST


def _not_running() -> str:
    d = _device()
    return (f"Docker isn't running on {d['name']}, or SysAdmin.IA isn't allowed to use it there."
            if d else "Docker isn't running on this server.")


def _conn(timeout: float) -> http.client.HTTPConnection:
    host = _host()
    if host.startswith("tcp://"):
        hp = host[6:].rstrip("/")
        h, _, port = hp.rpartition(":")
        return http.client.HTTPConnection(h or hp, int(port or 2375), timeout=timeout)
    return _UnixConnection(host.replace("unix://", "", 1), timeout)


def _open(method: str, path: str, query: dict | None = None, body=None, timeout: float = 30,
          headers: dict | None = None):
    url = path + ("?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None}) if query else "")
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    hdrs = {"Content-Type": "application/json", "Host": "docker"}
    hdrs.update(headers or {})
    c = _conn(timeout)
    try:
        c.request(method, url, body=data, headers=hdrs)
        r = c.getresponse()
    except PermissionError:
        c.close()
        raise DockerError("NO_ACCESS")
    except (FileNotFoundError, ConnectionRefusedError, ConnectionError, socket.timeout, OSError) as e:
        c.close()
        if isinstance(e, PermissionError) or getattr(e, "errno", None) == 13:
            raise DockerError("NO_ACCESS")
        raise DockerError(_not_running())
    except http.client.HTTPException:          # a tunnel whose far end was refused closes the connection at once
        c.close()
        raise DockerError(_not_running())
    if r.status >= 400:
        raw = r.read()
        c.close()
        try:
            msg = json.loads(raw).get("message") or raw.decode(errors="replace")
        except Exception:
            msg = raw.decode(errors="replace") or r.reason
        raise DockerError(_friendly(msg, r.status))
    return c, r


def _req(method: str, path: str, query: dict | None = None, body=None, timeout: float = 30):
    c, r = _open(method, path, query, body, timeout)
    try:
        raw = r.read()
    finally:
        c.close()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _friendly(msg: str, status: int) -> str:
    m = msg.strip()
    if "port is already allocated" in m or "address already in use" in m:
        return "That host port is already in use. Pick another one."
    if "No such image" in m or ("pull access denied" in m) or "manifest unknown" in m:
        return f"Image not found. Check the name (and that it exists on Docker Hub or its registry). Details: {m}"
    if "is already in use by container" in m:
        return "A container with that name already exists. Pick another name."
    if status == 409 and "image is being used" in m:
        return "This image is used by a container. Remove the container first (or use Force)."
    return m[:600]


def _err_text(e: DockerError) -> str:
    return "SysAdmin.IA has no access to Docker yet." if str(e) == "NO_ACCESS" else str(e)


# ---------------------------------------------------------------- status / install

def _binary() -> str | None:
    return shutil.which("docker") or next((p for p in ("/usr/bin/docker", "/usr/local/bin/docker") if os.path.exists(p)), None)


def _service_state() -> str:
    if not shutil.which("systemctl"):
        return "unknown"
    try:
        r = subprocess.run(["systemctl", "is-active", "docker"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _group_state() -> dict:
    """Is the service user in the docker group, and does this process already have it?"""
    if grp is None:
        return {"member": True, "active": True}
    try:
        g = grp.getgrnam("docker")
    except KeyError:
        return {"member": False, "active": False}
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
        primary = pwd.getpwuid(os.getuid()).pw_gid
    except KeyError:
        user, primary = "", -1
    member = user in g.gr_mem or primary == g.gr_gid or os.getuid() == 0
    return {"member": member, "active": os.getuid() == 0 or g.gr_gid in os.getgroups()}


def install_progress() -> dict:
    from .local_ai import parse_install_log
    out = {"state": None, "message": None, "log": "", "stage": ""}
    try:
        for line in INSTALL_STATUS.read_text().splitlines():
            k, _, v = line.partition("=")
            if k in ("state", "message", "updated"):
                out[k] = v
    except OSError:
        return out
    try:
        parsed = parse_install_log(INSTALL_LOG.read_text(errors="replace"))
        out["log"] = "\n".join(parsed["lines"][-80:])
        out["stage"] = parsed["stage"]
    except OSError:
        pass
    return out


def _remote_status(d: dict) -> dict:
    """status() for a device: what's installed comes from the device's agent, the rest from its Docker API."""
    from . import devices
    out = {"available": True, "installed": False, "running": False, "access": False, "version": None, "api_version": None,
           "service": "unknown", "compose": None, "info": None, "needs_restart": False, "error": None, "can_manage": False,
           "setup_command": None, "install": {"state": None, "message": None, "log": "", "stage": ""},
           "group": {"member": True, "active": True},
           "device": {"id": d["id"], "name": d["name"], "host": d["host"]}}
    try:
        info = devices.fresh_info()
    except devices.DeviceError as e:
        out["error"] = str(e)
        out["device"]["state"] = e.kind
        return out
    dk = info.get("docker") or {}
    out.update(installed=bool(dk.get("installed")), service=dk.get("service") or "unknown", compose=dk.get("compose"),
               group={"member": bool(dk.get("group")), "active": bool(dk.get("access"))})
    try:
        v = _req("GET", "/version", timeout=6)
        out.update(running=True, access=True, version=v.get("Version"), api_version=v.get("ApiVersion"), installed=True)
        i = _req("GET", "/info", timeout=8)
        out["info"] = {"containers": i.get("Containers", 0), "running": i.get("ContainersRunning", 0),
                       "paused": i.get("ContainersPaused", 0), "stopped": i.get("ContainersStopped", 0),
                       "images": i.get("Images", 0), "driver": i.get("Driver"), "root_dir": i.get("DockerRootDir"),
                       "os": i.get("OperatingSystem"), "cpus": i.get("NCPU"), "memory": i.get("MemTotal"), "disk_free": None}
        root = i.get("DockerRootDir") or "/"
        for dr in sorted(((d.get("stats") or {}).get("drives") or []), key=lambda x: -len(x["mount"])):
            if root == dr["mount"] or root.startswith(dr["mount"].rstrip("/") + "/"):
                out["info"]["disk_free"] = dr["free"]
                break
    except DockerError as e:
        if not out["installed"]:
            out["error"] = None
        elif not dk.get("access"):
            out["running"] = dk.get("service") == "active"
            out["error"] = (f"The user {d['user']} on {d['name']} isn't allowed to use Docker yet. Install the "
                            "“Docker access” module on the device (Server → Devices)."
                            if not dk.get("group") else
                            f"{d['user']} was added to the docker group on {d['name']}: reconnect to the device to use it.")
        else:
            out["error"] = str(e)
    return out


def status() -> dict:
    from . import server_name
    d = _device()
    if d:
        return _remote_status(d)
    out = {"available": available(), "installed": bool(_binary()), "running": False, "access": False,
           "version": None, "api_version": None, "service": _service_state(), "compose": None,
           "info": None, "needs_restart": False, "error": None,
           "can_manage": server_name.helper_supports("docker-install"), "install": install_progress()}
    out["setup_command"] = None if out["can_manage"] else server_name.setup_command()
    g = _group_state() if available() and sys.platform.startswith("linux") else {"member": True, "active": True}
    try:
        v = _req("GET", "/version", timeout=4)
        out.update(running=True, access=True, version=v.get("Version"), api_version=v.get("ApiVersion"), installed=True)
        info = _req("GET", "/info", timeout=6)
        out["info"] = {
            "containers": info.get("Containers", 0), "running": info.get("ContainersRunning", 0),
            "paused": info.get("ContainersPaused", 0), "stopped": info.get("ContainersStopped", 0),
            "images": info.get("Images", 0), "driver": info.get("Driver"), "root_dir": info.get("DockerRootDir"),
            "os": info.get("OperatingSystem"), "cpus": info.get("NCPU"), "memory": info.get("MemTotal"),
        }
        try:
            out["info"]["disk_free"] = psutil.disk_usage(info.get("DockerRootDir") or "/").free
        except OSError:
            out["info"]["disk_free"] = None
        out["compose"] = compose_version()
    except DockerError as e:
        if str(e) == "NO_ACCESS":
            out["running"] = out["service"] == "active" or out["installed"]
            out["needs_restart"] = g["member"] and not g["active"]
            out["error"] = ("Docker is installed. Restart SysAdmin.IA to finish giving it access."
                            if out["needs_restart"] else
                            "SysAdmin.IA isn't allowed to use Docker yet. Use “Give access” below.")
        else:
            out["error"] = str(e) if out["installed"] else None
    out["group"] = g
    return out


def helper(*args: str, timeout: int = 120) -> str:
    from . import server_name
    d = _device()
    if d:
        raise DockerError(f"On a device, Docker is installed and managed from Server → Devices → {d['name']}.")
    if not server_name.helper_supports("docker-install"):
        raise DockerError("This needs a one-time setup as root. On the server, run:\n  " + server_name.setup_command())
    try:
        return server_name.run_helper(*args, timeout=timeout)
    except RuntimeError as e:
        raise DockerError(str(e))


def _call(fn, *a, **kw):
    """Run a Docker API call and turn "no access" into a readable error."""
    try:
        return fn(*a, **kw)
    except DockerError as e:
        raise DockerError(_err_text(e))


# ---------------------------------------------------------------- containers

def _short_id(i: str) -> str:
    return (i or "").replace("sha256:", "")[:12]


def _ports(raw: list) -> list[dict]:
    seen, out = set(), []
    for p in raw or []:
        key = (p.get("PublicPort"), p.get("PrivatePort"), p.get("Type"))
        if key in seen:      # the same port is listed for IPv4 and IPv6
            continue
        seen.add(key)
        out.append({"host": p.get("PublicPort"), "container": p.get("PrivatePort"), "proto": p.get("Type", "tcp"),
                    "ip": p.get("IP") or ""})
    return sorted(out, key=lambda x: (x["host"] is None, x["host"] or 0, x["container"] or 0))


def _mounts(raw: list) -> list[dict]:
    return [{"type": m.get("Type"), "source": m.get("Source") or "", "name": m.get("Name") or "",
             "target": m.get("Destination"), "readonly": not m.get("RW", True)} for m in raw or []]


def list_containers(stats: bool = False) -> list[dict]:
    raw = _call(_req, "GET", "/containers/json", {"all": "1"})
    out = []
    for c in raw:
        labels = c.get("Labels") or {}
        out.append({
            "id": c["Id"], "short_id": _short_id(c["Id"]), "name": (c.get("Names") or ["/?"])[0].lstrip("/"),
            "image": c.get("Image"), "image_id": c.get("ImageID"), "command": c.get("Command"),
            "created": c.get("Created"), "state": c.get("State"), "status": c.get("Status"),
            "ports": _ports(c.get("Ports")), "mounts": _mounts(c.get("Mounts")),
            "networks": sorted((c.get("NetworkSettings") or {}).get("Networks", {}).keys()),
            "stack": labels.get("com.docker.compose.project"), "service": labels.get("com.docker.compose.service"),
            "managed": labels.get("sysadmin-ia.managed") == "1",
        })
    out.sort(key=lambda c: (c["state"] != "running", c["stack"] or "", c["name"]))
    if stats:
        running = [c for c in out if c["state"] == "running"]
        import contextvars
        threads = [threading.Thread(target=contextvars.copy_context().run, args=(lambda c=c: c.update(stats=_stats(c["id"])),),
                                    daemon=True) for c in running[:40]]
        for t in threads:
            t.start()
        for t in threads:
            t.join(6)
    return out


def _stats(cid: str) -> dict | None:
    try:
        s = _req("GET", f"/containers/{cid}/stats", {"stream": "false", "one-shot": "true"}, timeout=5)
    except DockerError:
        return None
    try:
        cpu = s["cpu_stats"]; pre = s.get("precpu_stats") or {}
        cd = cpu["cpu_usage"]["total_usage"] - (pre.get("cpu_usage") or {}).get("total_usage", 0)
        sd = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or []) or 1
        cpu_pct = round(cd / sd * ncpu * 100, 1) if sd > 0 and cd >= 0 else 0.0
    except (KeyError, TypeError):
        cpu_pct = None
    mem = s.get("memory_stats") or {}
    cache = (mem.get("stats") or {}).get("inactive_file", 0)
    used = max(0, mem.get("usage", 0) - cache)
    net = s.get("networks") or {}
    return {"cpu": cpu_pct, "mem": used, "mem_limit": mem.get("limit"),
            "net_rx": sum(n.get("rx_bytes", 0) for n in net.values()), "net_tx": sum(n.get("tx_bytes", 0) for n in net.values())}


def container_action(cid: str, action: str) -> None:
    if action not in ("start", "stop", "restart", "pause", "unpause", "kill"):
        raise DockerError("Unknown action.")
    _call(_req, "POST", f"/containers/{_cid(cid)}/{action}", {"t": "15"} if action in ("stop", "restart") else None, timeout=60)


def remove_container(cid: str, volumes: bool = False) -> None:
    _call(_req, "DELETE", f"/containers/{_cid(cid)}", {"force": "1", "v": "1" if volumes else "0"}, timeout=60)


def _cid(cid: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", cid or ""):
        raise DockerError("Invalid container.")
    return cid


def _demux(raw: bytes) -> str:
    """Docker log streams without a TTY have 8-byte frame headers: [stream, 0,0,0, size(4)]."""
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\0\0\0":
        out, i = [], 0
        while i + 8 <= len(raw):
            n = int.from_bytes(raw[i + 4:i + 8], "big")
            out.append(raw[i + 8:i + 8 + n])
            i += 8 + n
        raw = b"".join(out)
    return raw.decode("utf-8", "replace")


def logs(cid: str, tail: int = 300, timestamps: bool = False) -> str:
    c, r = _open("GET", f"/containers/{_cid(cid)}/logs",
                 {"stdout": "1", "stderr": "1", "tail": str(max(10, min(int(tail), 5000))),
                  "timestamps": "1" if timestamps else "0"}, timeout=15)
    try:
        return _demux(r.read())
    finally:
        c.close()


def inspect(cid: str) -> dict:
    """A container's settings in the same shape the create form uses (for Edit / Duplicate)."""
    d = _call(_req, "GET", f"/containers/{_cid(cid)}/json")
    cfg, hc = d.get("Config") or {}, d.get("HostConfig") or {}
    ports = []
    for key, binds in (hc.get("PortBindings") or {}).items():
        cport, _, proto = key.partition("/")
        for b in binds or [{}]:
            if b.get("HostPort"):
                ports.append({"host": int(b["HostPort"]), "container": int(cport), "proto": proto or "tcp",
                              "ip": b.get("HostIp") or ""})
    mounts = []
    for m in d.get("Mounts") or []:
        if m.get("Type") == "bind":
            mounts.append({"type": "bind", "source": m.get("Source"), "target": m.get("Destination"), "readonly": not m.get("RW", True)})
        elif m.get("Type") == "volume":
            mounts.append({"type": "volume", "source": m.get("Name"), "target": m.get("Destination"), "readonly": not m.get("RW", True)})
    image_env = set()
    try:
        image_env = set((_req("GET", f"/images/{urllib.parse.quote(cfg.get('Image', ''), safe='')}/json").get("Config") or {}).get("Env") or [])
    except DockerError:
        pass
    env = []
    for e in cfg.get("Env") or []:
        if e in image_env:
            continue   # set by the image itself, not by the user
        k, _, v = e.partition("=")
        env.append({"key": k, "value": v})
    rp = hc.get("RestartPolicy") or {}
    nets = list(((d.get("NetworkSettings") or {}).get("Networks") or {}).keys())
    return {
        "id": d.get("Id"), "name": (d.get("Name") or "").lstrip("/"), "image": cfg.get("Image"),
        "ports": ports, "mounts": mounts, "env": env,
        "restart": rp.get("Name") or "no", "command": " ".join(shlex.quote(a) for a in (cfg.get("Cmd") or []))
        if d.get("Args") is not None and cfg.get("Cmd") and cfg.get("Cmd") != _image_cmd(cfg.get("Image")) else "",
        "network": hc.get("NetworkMode") if hc.get("NetworkMode") not in (None, "default") else (nets[0] if nets else "bridge"),
        "privileged": bool(hc.get("Privileged")), "sysctls": hc.get("Sysctls") or {}, "state": (d.get("State") or {}).get("Status"),
        "labels": cfg.get("Labels") or {}, "created": d.get("Created"),
        "ip": next((n.get("IPAddress") for n in ((d.get("NetworkSettings") or {}).get("Networks") or {}).values() if n.get("IPAddress")), None),
    }


def _image_cmd(image: str | None):
    if not image:
        return None
    try:
        return (_req("GET", f"/images/{urllib.parse.quote(image, safe='')}/json").get("Config") or {}).get("Cmd")
    except DockerError:
        return None


# ---------------------------------------------------------------- host ↔ container helpers

def used_host_ports(exclude_container: str | None = None) -> dict[int, str]:
    """Host ports in use: by containers (published) and by programs listening on the host."""
    used: dict[int, str] = {}
    mine: set[int] = set()   # the container being edited: its ports (and docker-proxy's listeners) are free
    try:
        for c in list_containers():
            for p in c["ports"]:
                if not p["host"]:
                    continue
                if exclude_container and c["id"].startswith(exclude_container):
                    mine.add(p["host"])
                else:
                    used[p["host"]] = f"container “{c['name']}”"
    except DockerError:
        pass
    d = _device()
    if d:   # a device: its listening ports from the agent (no program names without root)
        from . import devices
        for p in devices.ports(d["id"]):
            if p not in used and p not in mine:
                used[p] = f"a program on {d['name']}"
        return used
    try:
        for s in psutil.net_connections(kind="inet"):
            if s.status == psutil.CONN_LISTEN and s.laddr and s.laddr.port not in used and s.laddr.port not in mine:
                name = "a program on the server"
                if s.pid:
                    try:
                        name = f"“{psutil.Process(s.pid).name()}” on the server"
                    except psutil.Error:
                        pass
                used[s.laddr.port] = name
    except (psutil.Error, PermissionError, OSError):
        pass
    return used


def suggest_port(wanted: int, used: dict[int, str], taken: set[int] | None = None) -> int:
    taken = taken or set()
    start = wanted if 1024 <= wanted <= 65535 else 8000 + (wanted % 1000)
    p = start
    while p in used or p in taken:
        p += 1
        if p > 65535:
            p = 8000
        if p == start:
            break
    return p


def host_folders() -> dict:
    """Where container data can live: mounted drives from the Drives module (+ the app's data dir)."""
    from . import drives
    out = []
    dev = _device()
    if dev:   # a device: its mounted drives from the last stats
        from . import devices
        try:
            st = devices.stats(dev["id"]).get("stats") or {}
        except devices.DeviceError:
            st = {}
        for v in st.get("drives") or []:
            system = v["mount"] in ("/", "/boot", "/var", "/usr", "/home")
            out.append({"path": v["mount"], "label": v["mount"], "disk": v["device"], "external": False, "system": system,
                        "free": v["free"], "total": v["total"], "fstype": v["fstype"]})
    try:
        d = drives.list_drives() if not dev else {"disks": []}
    except Exception:
        d = {"disks": []}
    for disk in d.get("disks", []):
        for v in disk["volumes"]:
            for mp in v.get("mountpoints") or []:
                if mp in ("[SWAP]", "/boot", "/boot/efi", "/efi"):
                    continue
                u = v.get("usage") or {}
                out.append({"path": mp, "label": v.get("label") or v.get("name"), "disk": disk["model"],
                            "external": disk["external"], "system": v.get("system", False),
                            "free": u.get("free"), "total": u.get("total"), "fstype": v.get("fstype")})
    # data drives first (internal before removable), then the system disk
    out.sort(key=lambda x: (x["system"], x["external"], x["path"].lower()))
    # suggest the internal data drive with the most free space (a USB stick may be unplugged)
    cands = [x for x in out if not x["system"] and not x["external"]] or [x for x in out if not x["system"]]
    default_root = max(cands, key=lambda x: x["free"] or 0)["path"] if cands else None
    return {"drives": out, "suggested_root": f"{default_root}/appdata" if default_root else "/srv/appdata"}


def image_details(image: str) -> dict | None:
    """What an image (already on the server) suggests: ports, volumes, environment, size."""
    try:
        d = _req("GET", f"/images/{urllib.parse.quote(_check_image(image), safe='')}/json")
    except DockerError as e:
        if "not found" in str(e).lower() or "no such image" in str(e).lower():
            return None
        raise DockerError(_err_text(e))
    cfg = d.get("Config") or {}
    ports = []
    for key in (cfg.get("ExposedPorts") or {}):
        p, _, proto = key.partition("/")
        if p.isdigit():
            ports.append({"container": int(p), "proto": proto or "tcp"})
    env = []
    for e in cfg.get("Env") or []:
        k, _, v = e.partition("=")
        if k not in ("PATH", "HOME", "LANG", "TERM") and not k.endswith("_VERSION") and not k.startswith(("GPG_", "PYTHON_", "NODE_")):
            env.append({"key": k, "value": v})
    return {"image": image, "id": d.get("Id"), "size": d.get("Size"), "created": d.get("Created"),
            "ports": sorted(ports, key=lambda p: p["container"]), "volumes": sorted((cfg.get("Volumes") or {}).keys()),
            "env": env, "cmd": cfg.get("Cmd"), "entrypoint": cfg.get("Entrypoint"),
            "labels": cfg.get("Labels") or {}, "workdir": cfg.get("WorkingDir")}


def apps_catalog() -> dict:
    """Common apps (docker_apps.APPS) with whether each is already running here."""
    from . import docker_apps
    have: dict[str, list[str]] = {}
    try:
        for c in list_containers():
            have.setdefault(docker_apps.repo(c["image"] or ""), []).append(c["name"])
    except DockerError:
        pass
    apps = []
    for a in docker_apps.APPS:
        apps.append({k: a.get(k) for k in ("id", "cat", "title", "image", "logo", "about", "notes", "open", "network")} |
                    {"ports": [{"container": p["container"], "proto": p.get("proto", "tcp"), "label": p.get("label")} for p in a["ports"]],
                     "containers": have.get(docker_apps.repo(a["image"]), []),
                     "needs": [e["label"] for e in a["env"] if e.get("required") and not e.get("value") and not e.get("autogen")] +
                              [f["label"] for f in a["folders"] if f.get("kind") == "media"]})
    return {"categories": [{"id": i, "title": t} for i, t in docker_apps.CATEGORIES], "apps": apps}


def _prepare_app(app: dict, det: dict, name: str, used: dict[int, str], image: str | None = None) -> dict:
    """Form values from a catalog app's preset: every setting the app needs, with its default value
    (user names as documented by the image, passwords freshly generated). Required settings can't be
    emptied, and your media folders must be chosen, before the container can be created."""
    import secrets as _secrets
    base = re.sub(r"[^a-z0-9_.-]", "-", (name or app["id"]).lower()).strip("-.") or app["id"]
    if not name:   # a second copy of the same app gets postgres-2, postgres-3…
        try:
            names = {c["name"] for c in list_containers()}
        except DockerError:
            names = set()
        n, first = 2, base
        while base in names:
            base, n = f"{first}-{n}", n + 1
    taken: set[int] = set()
    ports = []
    host_net = app.get("network") == "host"
    for p in app["ports"]:
        want = p.get("host", p["container"])
        host = want if host_net else suggest_port(want, used, taken)
        taken.add(host)
        note = p.get("label")
        if host != want:
            note = f"{note}. {want} is used by {used.get(want, 'something else')}, so {host} was picked"
        ports.append({"host": host, "container": p["container"], "proto": p.get("proto", "tcp"),
                      "ip": "127.0.0.1" if p.get("local") else "", "note": note, "label": p.get("label")})
    hf = host_folders()
    root = hf["suggested_root"]
    media_root = root.rsplit("/appdata", 1)[0] if root.endswith("/appdata") else "/srv"
    mounts = []
    for f in app["folders"]:
        kind = f.get("kind")
        if kind == "host":
            src = f["source"]
        elif kind == "media":
            src = ""   # the user must choose their own folder (movies, music…)
        else:
            src = f"{root}/{base}/{f.get('sub', 'data')}"
        mounts.append({"type": "bind", "source": src, "target": f["target"], "readonly": bool(f.get("readonly")),
                       "label": f.get("label"), "media": kind == "media", "required": kind == "media",
                       "suggest": media_root if kind == "media" else None})
    from . import server_name
    tz = server_name.get_timezone() if hasattr(server_name, "get_timezone") else None
    env = []
    for e in app["env"]:
        v = e.get("value", "")
        if e["key"] in ("TZ", "GENERIC_TIMEZONE") and v == "Etc/UTC" and tz:
            v = tz   # the server's own time zone
        if e.get("autogen") and not v:
            v = _secrets.token_urlsafe(18)   # 24 characters, letters/digits/-/_ (safe in URLs and env files)
        env.append({"key": e["key"], "value": v, "label": e.get("label"), "required": bool(e.get("required")),
                    "secret": bool(e.get("secret")), "generate": bool(e.get("generate")), "placeholder": e.get("placeholder", ""),
                    "generated": bool(e.get("autogen")), "preset": True})
    return {"present": True, "image": image or app["image"], "details": det, "name": base, "ports": ports, "mounts": mounts, "env": env,
            "network": app.get("network") or "bridge", "command": app.get("command", ""), "privileged": bool(app.get("privileged")),
            "sysctls": app.get("sysctls") or {}, "allow_system_paths": any(f.get("kind") == "host" for f in app["folders"]),
            "app": {"id": app["id"], "title": app["title"], "notes": app.get("notes"), "open": app.get("open"), "logo": app.get("logo")},
            "used_ports": {str(k): v for k, v in sorted(used.items())}}


def prepare(image: str, name: str = "", app_id: str = "") -> dict:
    """Pre-fill the create form for an image (or a catalog app): free host ports, shared folders on a drive."""
    from . import docker_apps
    app = docker_apps.get(app_id) if app_id else None
    if app_id and not app:
        raise DockerError("Unknown app.")
    # A catalog app's image typed by hand (e.g. "postgres" or "postgres:16") gets the same preset,
    # with the tag the user typed.
    typed = _check_image(image) if image else None
    if typed and not app:
        app = docker_apps.for_image(typed)
    if app and not (typed and docker_apps.repo(typed) == docker_apps.repo(app["image"])):
        typed = None   # another image than the app's: use the app's own
    image = typed or _check_image(app["image"])
    det = image_details(image)
    if det is None:
        return {"present": False, "image": image}
    used = used_host_ports()
    if app:
        return _prepare_app(app, det, name, used, image)
    taken: set[int] = set()
    ports = []
    for p in det["ports"]:
        host = suggest_port(p["container"], used, taken)
        taken.add(host)
        ports.append({"host": host, "container": p["container"], "proto": p["proto"], "ip": "",
                      "note": None if host == p["container"] else
                      f"{p['container']} is used by {used.get(p['container'], 'something else')}, so {host} was picked"})
    base = re.sub(r"[^a-z0-9_.-]", "-", (name or image.split("/")[-1].split(":")[0].split("@")[0]).lower()).strip("-.") or "app"
    root = host_folders()["suggested_root"]
    mounts = [{"type": "bind", "source": f"{root}/{base}{'/' + v.strip('/').replace('/', '-') if v.strip('/') else ''}",
               "target": v, "readonly": False} for v in det["volumes"]]
    return {"present": True, "image": image, "details": det, "name": base, "ports": ports, "mounts": mounts,
            "env": det["env"], "used_ports": {str(k): v for k, v in sorted(used.items())}}


# ---------------------------------------------------------------- create / recreate

def _check_image(image: str) -> str:
    image = (image or "").strip()
    if image.startswith("docker pull "):
        image = image[12:].strip()
    if not IMAGE_RE.fullmatch(image):
        raise DockerError("Enter an image like nginx, nginx:1.27 or ghcr.io/owner/app:latest.")
    return image


def _is_system_path(p: str) -> bool:
    p = posixpath.normpath(p)
    for s in SYSTEM_PATHS:
        if p == s or (s not in ("/",) and p.startswith(s + "/")):
            return True
    return False


def validate_spec(spec: dict, exclude_container: str | None = None) -> dict:
    s = {"name": (spec.get("name") or "").strip(), "image": _check_image(spec.get("image", "")),
         "restart": spec.get("restart") or "unless-stopped", "command": (spec.get("command") or "").strip(),
         "network": (spec.get("network") or "bridge").strip(), "privileged": bool(spec.get("privileged")),
         "allow_system_paths": bool(spec.get("allow_system_paths")), "start": spec.get("start", True) is not False,
         "ports": [], "mounts": [], "env": []}
    if not NAME_RE.fullmatch(s["name"]):
        raise DockerError("Name: letters, digits, '-', '_' and '.', starting with a letter or digit.")
    if s["restart"] not in RESTART_POLICIES:
        raise DockerError("Unknown restart policy.")
    if not NAME_RE.fullmatch(s["network"]):
        raise DockerError("Unknown network.")
    used = used_host_ports(exclude_container)
    seen: set[tuple] = set()
    for p in spec.get("ports") or []:
        try:
            host, cont = int(p.get("host")), int(p.get("container"))
        except (TypeError, ValueError):
            raise DockerError("Ports must be numbers.")
        proto = (p.get("proto") or "tcp").lower()
        if proto not in ("tcp", "udp") or not (1 <= host <= 65535 and 1 <= cont <= 65535):
            raise DockerError("Ports must be between 1 and 65535 (TCP or UDP).")
        ip = (p.get("ip") or "").strip()
        if ip and ip not in ("127.0.0.1", "0.0.0.0", "::1", "::"):
            raise DockerError("Listen address must be empty (all), 127.0.0.1 (this server only) or 0.0.0.0.")
        if (host, proto) in seen:
            raise DockerError(f"Host port {host}/{proto} is listed twice.")
        seen.add((host, proto))
        if host in used and s["network"] != "host":
            raise DockerError(f"Host port {host} is already used by {used[host]}. Pick another one.")
        s["ports"].append({"host": host, "container": cont, "proto": proto, "ip": ip})
    for m in spec.get("mounts") or []:
        kind = m.get("type") or "bind"
        src, target = (m.get("source") or "").strip(), (m.get("target") or "").strip()
        if not target.startswith("/") or ":" in target:
            raise DockerError("The folder inside the container must be an absolute path like /data.")
        if kind == "bind":
            if not src:
                raise DockerError(f"Choose the folder on the server for “{m.get('label') or target}”.")
            if not src.startswith("/") or ":" in src or "\n" in src:
                raise DockerError("The folder on the server must be an absolute path like /mnt/data/app.")
            src = posixpath.normpath(src)
            if _is_system_path(src) and not s["allow_system_paths"]:
                raise DockerError(f"{src} is a system folder. Tick “Allow system folders” if you really want to share it.")
        elif kind == "volume":
            if not NAME_RE.fullmatch(src):
                raise DockerError("Volume names: letters, digits, '-', '_' and '.'.")
        else:
            raise DockerError("Unknown kind of storage.")
        s["mounts"].append({"type": kind, "source": src, "target": target, "readonly": bool(m.get("readonly"))})
    for e in spec.get("env") or []:
        k = (e.get("key") or "").strip()
        if not k:
            continue
        if not ENV_KEY_RE.fullmatch(k):
            raise DockerError(f"Invalid environment variable name: {k}")
        s["env"].append({"key": k, "value": str(e.get("value") or "")})
    if s["command"]:
        try:
            shlex.split(s["command"])
        except ValueError as e:
            raise DockerError(f"Command: {e}")
    # kernel settings for the container's own network namespace (e.g. a VPN needs ip_forward)
    s["sysctls"] = {}
    for k, v in (spec.get("sysctls") or {}).items():
        if not re.fullmatch(r"net\.[a-z0-9_.]{1,100}", str(k)) or not re.fullmatch(r"[A-Za-z0-9 ._-]{1,50}", str(v)):
            raise DockerError(f"Unsupported kernel setting: {k}")
        s["sysctls"][str(k)] = str(v)
    for e in s["env"]:
        req = next((x for x in spec.get("env") or [] if (x.get("key") or "").strip() == e["key"]), {})
        if req.get("required") and not e["value"].strip():
            raise DockerError(f"Fill in {req.get('label') or e['key']}.")
    return s


def _create_body(s: dict) -> dict:
    exposed, bindings = {}, {}
    for p in s["ports"]:
        key = f"{p['container']}/{p['proto']}"
        exposed[key] = {}
        bindings.setdefault(key, []).append({"HostIp": p["ip"], "HostPort": str(p["host"])})
    binds = [f"{m['source']}:{m['target']}{':ro' if m['readonly'] else ''}" for m in s["mounts"]]
    body = {
        "Image": s["image"], "Env": [f"{e['key']}={e['value']}" for e in s["env"]],
        "ExposedPorts": exposed, "Labels": {"sysadmin-ia.managed": "1"},
        "HostConfig": {"PortBindings": bindings if s["network"] != "host" else {}, "Binds": binds,
                       "RestartPolicy": {"Name": s["restart"]} if s["restart"] != "on-failure" else {"Name": "on-failure", "MaximumRetryCount": 5},
                       "NetworkMode": s["network"], "Privileged": s["privileged"]},
    }
    if s["sysctls"]:
        body["HostConfig"]["Sysctls"] = s["sysctls"]
    if s["command"]:
        body["Cmd"] = shlex.split(s["command"])
    return body


# ---------------------------------------------------------------- background tasks (pull, create, stacks)

_tasks: dict[str, dict] = {}
_tlock = threading.RLock()


def tasks() -> list[dict]:
    from . import devices
    dev = devices.current_id()
    with _tlock:
        now = time.time()
        for k in [k for k, v in _tasks.items() if v.get("finished") and now - v["finished"] > 900]:
            _tasks.pop(k)
        return sorted((dict(v) for v in _tasks.values() if v.get("device") == dev), key=lambda t: -t["started"])


def _task(kind: str, title: str, target) -> dict:
    import contextvars
    from . import devices
    tid = f"{kind}-{int(time.time() * 1000)}"
    dev = devices.current_id()
    with _tlock:
        if any(t["title"] == title and t["state"] == "running" and t.get("device") == dev for t in _tasks.values()):
            raise DockerError(f"“{title}” is already running.")
        _tasks[tid] = {"id": tid, "kind": kind, "title": title, "state": "running", "status": "Starting…", "device": dev,
                       "progress": None, "log": [], "started": time.time(), "finished": None, "error": None, "result": None}

    def upd(**kw):
        with _tlock:
            t = _tasks[tid]
            if "line" in kw:
                t["log"] = (t["log"] + [kw.pop("line")])[-200:]
            t.update(kw)

    def run():
        try:
            res = target(upd)
            upd(state="done", finished=time.time(), status="Done", progress=100 if _tasks[tid]["progress"] is not None else None, result=res)
        except Exception as e:  # noqa: BLE001 - reported to the user
            msg = _err_text(e) if isinstance(e, DockerError) else str(e)
            upd(state="failed", finished=time.time(), error=msg, status="Failed")
    ctx = contextvars.copy_context()      # the thread keeps working on the same device
    threading.Thread(target=ctx.run, args=(run,), daemon=True, name=f"docker-{tid}").start()
    return dict(_tasks[tid])


def _pull(image: str, upd) -> None:
    """Pull with combined progress over all layers."""
    name, tag = image, "latest"
    if "@" not in image:
        last = image.rsplit("/", 1)[-1]
        if ":" in last:
            name, tag = image.rsplit(":", 1)
    else:
        name, tag = image.split("@", 1)[0], None
    q = {"fromImage": name, "tag": tag} if tag else {"fromImage": image}
    c, r = _open("POST", "/images/create", q, timeout=3600)
    layers: dict[str, tuple[int, int]] = {}
    try:
        buf = b""
        while True:
            chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("error"):
                    raise DockerError(_friendly(ev["error"], 500))
                lid, st = ev.get("id"), ev.get("status", "")
                det = ev.get("progressDetail") or {}
                if lid and det.get("total"):
                    layers[lid] = (det.get("current", 0), det["total"])
                elif lid and st in ("Download complete", "Pull complete", "Already exists") and lid in layers:
                    layers[lid] = (layers[lid][1], layers[lid][1])
                done = sum(a for a, _ in layers.values()); total = sum(b for _, b in layers.values())
                pct = round(100 * done / total, 1) if total else None
                upd(status=f"Downloading {image}" + (f" — {done / 1e6:.0f} of {total / 1e6:.0f} MB" if total else ""),
                    progress=pct)
                if not lid and st:
                    upd(line=st)
    finally:
        c.close()


def pull_image(image: str) -> dict:
    image = _check_image(image)
    _call(_req, "GET", "/_ping")
    return _task("pull", f"Download {image}", lambda upd: _pull(image, upd))


def create_container(spec: dict) -> dict:
    s = validate_spec(spec)

    def run(upd):
        if image_details(s["image"]) is None:
            _pull(s["image"], upd)
        upd(status="Creating the container", progress=None)
        r = _req("POST", "/containers/create", {"name": s["name"]}, _create_body(s))
        if s["start"]:
            upd(status="Starting")
            _req("POST", f"/containers/{r['Id']}/start")
        return {"id": r["Id"], "warnings": r.get("Warnings") or []}
    return _task("create", f"Create {s['name']}", run)


def _sync_upd(lines: list[str]):
    """Progress callback for jobs that wait for the result: keep the log lines, ignore the rest."""
    def upd(**kw):
        if "line" in kw:
            lines.append(str(kw["line"]))
    return upd


def update_container(name: str) -> dict:
    """Download the newest version of a container's image; recreate the container if it changed (blocking)."""
    old = inspect(name)
    before = _req("GET", f"/containers/{old['id']}/json").get("Image")
    lines: list[str] = []
    _pull(old["image"], _sync_upd(lines))
    after = (_req("GET", f"/images/{urllib.parse.quote(old['image'], safe='')}/json") or {}).get("Id")
    if not after or after == before:
        return {"updated": False, "image": old["image"], "log": lines}
    spec = {**old, "start": old["state"] == "running" or old["state"] == "restarting",
            "allow_system_paths": any(m["type"] == "bind" and _is_system_path(m["source"]) for m in old["mounts"])}
    recreate_container(old["id"], spec, wait=True)
    return {"updated": True, "image": old["image"], "old": _short_id(before), "new": _short_id(after), "log": lines}


def recreate_container(cid: str, spec: dict, wait: bool = False) -> dict:
    """Edit = make a new container with the new settings, keep the old one until the new one runs.
    wait=True runs it here and returns the result (for jobs) instead of starting a background task."""
    old = inspect(cid)
    s = validate_spec(spec, exclude_container=old["id"])

    def run(upd):
        if image_details(s["image"]) is None or spec.get("pull"):
            _pull(s["image"], upd)
        was_running = old["state"] == "running"
        backup = f"{old['name']}-old-{int(time.time())}"
        upd(status="Stopping the old container")
        if was_running:
            _req("POST", f"/containers/{old['id']}/stop", {"t": "15"}, timeout=60)
        _req("POST", f"/containers/{old['id']}/rename", {"name": backup})
        try:
            upd(status="Creating the new container")
            r = _req("POST", "/containers/create", {"name": s["name"]}, _create_body(s))
            if s["start"]:
                upd(status="Starting")
                try:
                    _req("POST", f"/containers/{r['Id']}/start")
                except DockerError:
                    _req("DELETE", f"/containers/{r['Id']}", {"force": "1"})
                    raise
        except DockerError as e:
            upd(status="Putting the old container back", line=f"Failed: {e}")
            try:
                _req("POST", f"/containers/{old['id']}/rename", {"name": old["name"]})
                if was_running:
                    _req("POST", f"/containers/{old['id']}/start")
            except DockerError as e2:
                raise DockerError(f"{e}. Putting the old container back also failed ({e2}); "
                                  f"it is still there as “{backup}” or “{old['name']}”.")
            raise DockerError(f"{e} (the old container was kept{' and restarted' if was_running else ''})")
        upd(status="Removing the old container")
        _req("DELETE", f"/containers/{old['id']}", {"force": "1", "v": "0"})
        return {"id": r["Id"]}
    if wait:
        return run(_sync_upd([]))
    return _task("recreate", f"Update {old['name']}", run)


# ---------------------------------------------------------------- images, volumes, networks

def list_images() -> list[dict]:
    imgs = _call(_req, "GET", "/images/json")
    used: dict[str, list[str]] = {}
    for c in _call(_req, "GET", "/containers/json", {"all": "1"}):
        used.setdefault(c.get("ImageID"), []).append((c.get("Names") or ["/?"])[0].lstrip("/"))
    out = []
    for i in imgs:
        tags = [t for t in (i.get("RepoTags") or []) if t != "<none>:<none>"]
        out.append({"id": i["Id"], "short_id": _short_id(i["Id"]), "tags": tags,
                    "digest": (i.get("RepoDigests") or [None])[0], "size": i.get("Size", 0), "created": i.get("Created"),
                    "containers": used.get(i["Id"], []), "dangling": not tags})
    return sorted(out, key=lambda x: (x["dangling"], (x["tags"] or [""])[0]))


def remove_image(iid: str, force: bool = False) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,254}", iid or ""):
        raise DockerError("Invalid image.")
    _call(_req, "DELETE", f"/images/{urllib.parse.quote(iid, safe='')}", {"force": "1" if force else "0"}, timeout=120)


def search_hub(term: str) -> list[dict]:
    term = (term or "").strip()[:100]
    if not term:
        return []
    r = _call(_req, "GET", "/images/search", {"term": term, "limit": "25"}, timeout=20)
    return [{"name": x.get("name"), "description": x.get("description") or "", "stars": x.get("star_count", 0),
             "official": bool(x.get("is_official")),
             "url": f"https://hub.docker.com/{'_/' if x.get('is_official') else 'r/'}{x.get('name')}"} for x in r]


def list_volumes() -> list[dict]:
    r = _call(_req, "GET", "/volumes")
    users: dict[str, list[str]] = {}
    for c in list_containers():
        for m in c["mounts"]:
            if m["type"] == "volume" and m["name"]:
                users.setdefault(m["name"], []).append(c["name"])
    out = [{"name": v["Name"], "driver": v.get("Driver"), "mountpoint": v.get("Mountpoint"), "created": v.get("CreatedAt"),
            "labels": v.get("Labels") or {}, "containers": users.get(v["Name"], []),
            "stack": (v.get("Labels") or {}).get("com.docker.compose.project")} for v in r.get("Volumes") or []]
    return sorted(out, key=lambda v: (not v["containers"], v["name"]))


def create_volume(name: str) -> dict:
    if not NAME_RE.fullmatch(name or ""):
        raise DockerError("Volume names: letters, digits, '-', '_' and '.'.")
    return _call(_req, "POST", "/volumes/create", body={"Name": name, "Labels": {"sysadmin-ia.managed": "1"}})


def remove_volume(name: str) -> None:
    if not NAME_RE.fullmatch(name or ""):
        raise DockerError("Invalid volume.")
    _call(_req, "DELETE", f"/volumes/{name}")


def list_networks() -> list[dict]:
    out = []
    for n in _call(_req, "GET", "/networks"):
        cfg = ((n.get("IPAM") or {}).get("Config") or [{}])
        out.append({"id": n["Id"], "name": n["Name"], "driver": n.get("Driver"), "scope": n.get("Scope"),
                    "subnet": ", ".join(c.get("Subnet", "") for c in cfg if c.get("Subnet")),
                    "internal": n.get("Internal", False), "builtin": n["Name"] in DEFAULT_NETWORKS,
                    "stack": (n.get("Labels") or {}).get("com.docker.compose.project")})
    # container counts need a per-network inspect; use the containers list instead
    counts: dict[str, list[str]] = {}
    try:
        for c in list_containers():
            for net in c["networks"]:
                counts.setdefault(net, []).append(c["name"])
    except DockerError:
        pass
    for n in out:
        n["containers"] = counts.get(n["name"], [])
    return sorted(out, key=lambda n: (not n["builtin"], n["name"]))


def create_network(name: str, internal: bool = False) -> dict:
    if not NAME_RE.fullmatch(name or ""):
        raise DockerError("Network names: letters, digits, '-', '_' and '.'.")
    return _call(_req, "POST", "/networks/create", body={"Name": name, "Driver": "bridge", "Internal": bool(internal),
                                                         "Labels": {"sysadmin-ia.managed": "1"}})


def remove_network(name: str) -> None:
    if not NAME_RE.fullmatch(name or "") or name in DEFAULT_NETWORKS:
        raise DockerError("This network can't be removed.")
    _call(_req, "DELETE", f"/networks/{name}")


def connect_network(network: str, cid: str, connect: bool = True) -> None:
    if not NAME_RE.fullmatch(network or ""):
        raise DockerError("Invalid network.")
    _call(_req, "POST", f"/networks/{network}/{'connect' if connect else 'disconnect'}", body={"Container": _cid(cid)})


def disk_usage() -> dict:
    d = _call(_req, "GET", "/system/df", timeout=120)
    imgs = d.get("Images") or []; ctrs = d.get("Containers") or []; vols = d.get("Volumes") or []
    return {"images": sum(i.get("Size", 0) for i in imgs), "images_count": len(imgs),
            "containers": sum(c.get("SizeRw", 0) or 0 for c in ctrs), "containers_count": len(ctrs),
            "volumes": sum(((v.get("UsageData") or {}).get("Size", 0) or 0) for v in vols if ((v.get("UsageData") or {}).get("Size", -1) or 0) > 0),
            "volumes_count": len(vols), "build_cache": sum(b.get("Size", 0) for b in d.get("BuildCache") or [])}


def prune(what: list[str]) -> dict:
    """Clean up: stopped containers, unused images, unused networks, unused volumes, build cache."""
    freed, removed = 0, {}
    if "containers" in what:
        r = _call(_req, "POST", "/containers/prune", timeout=300)
        freed += r.get("SpaceReclaimed", 0); removed["containers"] = len(r.get("ContainersDeleted") or [])
    if "images" in what:
        r = _call(_req, "POST", "/images/prune", {"filters": json.dumps({"dangling": ["false"]})}, timeout=600)
        freed += r.get("SpaceReclaimed", 0); removed["images"] = len(r.get("ImagesDeleted") or [])
    if "networks" in what:
        r = _call(_req, "POST", "/networks/prune", timeout=120)
        removed["networks"] = len(r.get("NetworksDeleted") or [])
    if "volumes" in what:
        r = _call(_req, "POST", "/volumes/prune", {"filters": json.dumps({"all": ["true"]})}, timeout=600)
        freed += r.get("SpaceReclaimed", 0); removed["volumes"] = len(r.get("VolumesDeleted") or [])
    if "build_cache" in what:
        r = _call(_req, "POST", "/build/prune", timeout=600)
        freed += r.get("SpaceReclaimed", 0)
    return {"freed": freed, "removed": removed}


# ---------------------------------------------------------------- stacks (Docker Compose)

def _stacks_dir() -> Path:
    d = _device()
    if d:      # a device's stacks are kept apart (and copied to the device to run there)
        from . import devices
        return devices.stacks_dir(d["id"])
    p = Path(settings.data_dir) / "stacks"
    p.mkdir(parents=True, exist_ok=True)
    return p


_WRITE_STACK = ("import json,os,sys;d=os.path.expanduser('~/.sysadmin-ia/stacks/'+sys.argv[1]);os.makedirs(d,exist_ok=True);"
                "f=json.load(sys.stdin)\nfor k,v in f.items():\n  p=os.path.join(d,k);open(p,'w').write(v);os.chmod(p,0o600)")


def _compose_remote(d: dict, name: str, *args: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    """docker compose on a device: the stack's files are copied to ~/.sysadmin-ia/stacks/NAME there first, so
    relative paths in the compose file are folders of the device."""
    from . import devices
    local = _stacks_dir() / name
    files = {n: (local / n).read_text() for n in ("compose.yaml", ".env") if (local / n).is_file()}
    rdir = f"$HOME/.sysadmin-ia/stacks/{name}"
    cmd = (f"python3 -c {shlex.quote(_WRITE_STACK)} {name} && cd \"{rdir}\" && "
           f"docker compose -p {name} -f compose.yaml {' '.join(shlex.quote(a) for a in args)}")
    if args[:1] == ("down",) and "--rm-files" in args:
        cmd = f"cd \"{rdir}\" 2>/dev/null && docker compose -p {name} -f compose.yaml down; cd && rm -rf \"{rdir}\""
    try:
        code, out, err = devices.run(d, cmd, stdin=json.dumps(files).encode(), timeout=timeout)
    except devices.DeviceError as e:
        raise DockerError(str(e))
    return subprocess.CompletedProcess(args, code, out, err)


def compose_version() -> str | None:
    docker = _binary()
    if not docker:
        return None
    try:
        r = subprocess.run([docker, "compose", "version", "--short"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


def _compose(name: str, *args: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    d = _device()
    if d:
        return _compose_remote(d, name, *args, timeout=timeout)
    docker = _binary()
    if not docker:
        raise DockerError("The docker command isn't installed.")
    f = _stacks_dir() / name / "compose.yaml"
    return subprocess.run([docker, "compose", "-p", name, "-f", str(f), *args], capture_output=True, text=True,
                          timeout=timeout, cwd=str(f.parent))


def list_stacks() -> list[dict]:
    containers = []
    try:
        containers = list_containers()
    except DockerError:
        pass
    by: dict[str, list[dict]] = {}
    for c in containers:
        if c["stack"]:
            by.setdefault(c["stack"], []).append(c)
    out = []
    names = {p.name for p in _stacks_dir().iterdir() if (p / "compose.yaml").is_file()} | set(by)
    for n in sorted(names):
        cs = by.get(n, [])
        f = _stacks_dir() / n / "compose.yaml"
        out.append({"name": n, "managed": f.is_file(), "containers": [{"name": c["name"], "state": c["state"], "ports": c["ports"]} for c in cs],
                    "running": sum(1 for c in cs if c["state"] == "running"), "total": len(cs),
                    "updated": f.stat().st_mtime if f.is_file() else None})
    return out


def get_stack(name: str) -> dict:
    _check_stack(name)
    f = _stacks_dir() / name / "compose.yaml"
    env = _stacks_dir() / name / ".env"
    return {"name": name, "compose": f.read_text() if f.is_file() else "", "env": env.read_text() if env.is_file() else ""}


def _check_stack(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", name or ""):
        raise DockerError("Stack names: lowercase letters, digits, '-' and '_'.")
    return name


def save_stack(name: str, compose: str, env: str = "") -> dict:
    _check_stack(name)
    if not compose.strip():
        raise DockerError("The compose file is empty.")
    if len(compose) > 200_000 or len(env) > 50_000:
        raise DockerError("The compose file is too large.")
    d = _stacks_dir() / name
    d.mkdir(exist_ok=True)
    tmp = d / "compose.yaml.new"
    tmp.write_text(compose)
    (d / ".env").write_text(env)
    os.chmod(d / ".env", 0o600)
    docker = _binary()
    if docker:   # let Compose validate it before replacing the old file
        r = subprocess.run([docker, "compose", "-p", name, "-f", str(tmp), "config", "-q"], capture_output=True,
                           text=True, timeout=30, cwd=str(d))
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise DockerError("The compose file has an error:\n" + (r.stderr or r.stdout).strip()[-1500:])
    tmp.replace(d / "compose.yaml")
    return get_stack(name)


def stack_action(name: str, action: str, wait: bool = False) -> dict:
    _check_stack(name)
    if not (_stacks_dir() / name / "compose.yaml").is_file():
        raise DockerError("This stack wasn't created here, so its compose file isn't available.")
    args = {"up": ["up", "-d", "--remove-orphans"], "pull": ["pull"], "down": ["down"], "restart": ["restart"],
            "stop": ["stop"], "start": ["start"], "down_volumes": ["down", "-v"]}.get(action)
    if not args:
        raise DockerError("Unknown action.")

    def run(upd):
        if action == "up":
            upd(status="Downloading images")
            p = _compose(name, "pull", "--ignore-pull-failures")
            for line in (p.stdout + p.stderr).splitlines()[-40:]:
                upd(line=line)
        upd(status={"up": "Starting", "down": "Stopping and removing", "down_volumes": "Removing everything"}.get(action, action.capitalize()))
        p = _compose(name, *args)
        for line in (p.stdout + p.stderr).splitlines()[-100:]:
            upd(line=line)
        if p.returncode != 0:
            raise DockerError((p.stderr or p.stdout).strip()[-1500:] or f"docker compose {action} failed")
        return {"ok": True}
    if wait:
        lines: list[str] = []
        res = run(_sync_upd(lines))
        return {**res, "log": lines}
    return _task("stack", f"Stack {name}: {action.replace('_', ' ')}", run)


def delete_stack(name: str) -> None:
    _check_stack(name)
    d = _stacks_dir() / name
    if (d / "compose.yaml").is_file():
        # on a device the copy of the files there is removed too
        p = _compose(name, "down", *(["--rm-files"] if _device() else []), timeout=600)
        if p.returncode != 0 and "no configuration file" not in (p.stderr or ""):
            raise DockerError((p.stderr or p.stdout).strip()[-800:])
    shutil.rmtree(d, ignore_errors=True)


def stack_logs(name: str, tail: int = 200) -> str:
    _check_stack(name)
    p = _compose(name, "logs", "--no-color", "--tail", str(max(10, min(int(tail), 2000))), timeout=60)
    return (p.stdout + p.stderr)[-200_000:]
