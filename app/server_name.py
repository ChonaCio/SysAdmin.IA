"""Server name: the hostname other computers on the LAN use to reach this app (NAME.local).

Changing it needs root, so it goes through the root-owned helper that install.sh puts in
/usr/local/sbin (scripts/sysadmin-ia-hostname), which the service user may run via sudo.
The helper sets the hostname, re-announces it via mDNS (Avahi) and refreshes the
self-signed TLS certificate so it is valid for NAME.local.
"""
from __future__ import annotations

import re
import shutil
import socket
import subprocess
from pathlib import Path

import psutil

from .config import settings

HELPER = Path(f"/usr/local/sbin/{settings.service_name}-hostname")
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def validate(name: str) -> str:
    name = (name or "").strip().lower()
    if name.endswith(".local"):
        name = name[: -len(".local")]
    if not NAME_RE.fullmatch(name) or name.isdigit():
        raise ValueError("Use 1–63 letters, digits and hyphens (no spaces or dots, "
                         "not starting or ending with a hyphen), e.g. 'office-server'.")
    return name


def current() -> str:
    return socket.gethostname().split(".")[0].lower()


def _lan_ips() -> list[str]:
    ips = []
    for nic, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                ips.append(a.address)
    return ips


def _mdns_state() -> str:
    """'active', 'installed' or 'missing' (Avahi)."""
    if not shutil.which("avahi-daemon") and not Path("/usr/sbin/avahi-daemon").exists():
        return "missing"
    if shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "is-active", "--quiet", "avahi-daemon"], capture_output=True)
        return "active" if r.returncode == 0 else "installed"
    return "installed"


def status() -> dict:
    name = current()
    scheme = "https" if settings.tls_enabled else "http"
    mdns = _mdns_state()
    urls = []
    if mdns == "active":
        urls.append(f"{scheme}://{name}.local:{settings.port}/")
    urls += [f"{scheme}://{ip}:{settings.port}/" for ip in _lan_ips()]
    return {
        "name": name,
        "mdns_name": f"{name}.local",
        "mdns": mdns,
        "urls": urls,
        "can_change": HELPER.is_file() and bool(shutil.which("sudo")),
    }


def set_name(name: str) -> dict:
    """Rename the server. Returns {name, output, cert_changed}."""
    name = validate(name)
    if not HELPER.is_file():
        raise RuntimeError("The server-name helper is not installed. Re-run install.sh "
                           "(with --server-name NAME) to set it up.")
    r = subprocess.run(["sudo", "-n", str(HELPER), "set", name],
                       capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        raise RuntimeError(out or f"The helper failed (exit code {r.returncode}).")
    return {"name": current(), "output": out.replace("CERT_CHANGED", "").strip(),
            "cert_changed": "CERT_CHANGED" in out}
