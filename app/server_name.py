"""Server name: the hostname other computers on the LAN use to reach this app (NAME.local).

Changing it needs root, so it goes through the root-owned helper in /usr/local/sbin
(source: scripts/sysadmin-ia-hostname), which the service user may run via sudo. The helper
sets the hostname, re-announces it via mDNS (Avahi), refreshes the self-signed TLS
certificate for NAME.local, and can reboot the server.

install.sh sets the helper up. If the app was updated in place (Admin → Updates) the helper
may be missing or outdated; when the service user has full access (passwordless sudo) the
app installs or refreshes it by itself. Otherwise an admin runs the command that
status()["setup_command"] shows, once.
"""
from __future__ import annotations

import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import psutil

from .config import settings

HELPER = Path(f"/usr/local/sbin/{settings.service_name}-hostname")
BUNDLED = settings.app_dir / "scripts" / "sysadmin-ia-hostname"
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class HelperError(RuntimeError):
    pass


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
    for addrs in psutil.net_if_addrs().values():
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


def _run(cmd: list[str], timeout: int = 120, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          input=input if input is not None else "")


def _full_access() -> bool:
    """True if the service user has passwordless sudo for everything."""
    if not shutil.which("sudo"):
        return False
    try:
        return _run(["sudo", "-n", "true"], 10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _helper_current() -> bool:
    """Installed helper exists and matches the copy shipped with this version of the app."""
    if not HELPER.is_file():
        return False
    try:
        return not BUNDLED.is_file() or HELPER.read_bytes() == BUNDLED.read_bytes()
    except OSError:
        return True  # can't compare; use what is installed


def _helper_usable() -> bool:
    if not HELPER.is_file() or not shutil.which("sudo"):
        return False
    try:  # "sudo -n -l CMD" succeeds only if the sudoers rule allows it without a password
        return _run(["sudo", "-n", "-l", str(HELPER)], 10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_helper() -> None:
    """Install or refresh the helper when possible (needs full access)."""
    if _helper_current() and _helper_usable():
        return
    if not BUNDLED.is_file():
        if _helper_usable():
            return
        raise HelperError("The root helper is missing. Re-run install.sh to set it up.")
    if not _full_access():
        if _helper_usable():
            return  # outdated but working; new subcommands may be unavailable
        raise HelperError("This needs a one-time setup as root. On the server, run:\n"
                         f"  {setup_command()}")
    r = _run(["sudo", "-n", "bash", str(BUNDLED), "install"])
    if r.returncode != 0:
        raise HelperError(f"Could not install the helper: {(r.stderr or r.stdout).strip()}")


def setup_command() -> str:
    return f"sudo bash {BUNDLED} install"


def status() -> dict:
    name = current()
    scheme = "https" if settings.tls_enabled else "http"
    mdns = _mdns_state()
    urls = []
    if mdns == "active":
        urls.append(f"{scheme}://{name}.local:{settings.port}/")
    urls += [f"{scheme}://{ip}:{settings.port}/" for ip in _lan_ips()]
    linux = sys.platform.startswith("linux")
    usable = linux and _helper_usable()
    can_setup = linux and BUNDLED.is_file() and _full_access()
    return {
        "name": name,
        "mdns_name": f"{name}.local",
        "mdns": mdns,
        "urls": urls,
        "can_change": usable or can_setup,
        "setup_command": setup_command() if linux and BUNDLED.is_file() and not (usable or can_setup) else None,
        "linux": linux,
    }


def helper_supports(subcommand: str) -> bool:
    """True if the root helper can be used (installing/refreshing it if possible) and knows
    `subcommand`. Never raises."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        ensure_helper()
        r = _run(["sudo", "-n", str(HELPER)], 10)  # no arguments: prints the usage line
    except (HelperError, OSError, subprocess.TimeoutExpired):
        return False
    return f"|{subcommand}" in r.stderr or f"{{{subcommand}" in r.stderr


def run_helper(*args: str, timeout: int = 120, input: str | None = None) -> str:
    return _helper(*args, timeout=timeout, input=input)


def _helper(*args: str, timeout: int = 120, input: str | None = None) -> str:
    ensure_helper()
    r = _run(["sudo", "-n", str(HELPER), *args], timeout, input)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        if "usage:" in out:
            out = ("The installed helper is outdated. On the server, run:\n  " + setup_command())
        raise HelperError(out or f"The helper failed (exit code {r.returncode}).")
    return out


def set_name(name: str) -> dict:
    """Rename the server. Returns {name, output, cert_changed}. No reboot is needed."""
    name = validate(name)
    out = _helper("set", name)
    return {"name": current(), "output": out.replace("CERT_CHANGED", "").strip(),
            "cert_changed": "CERT_CHANGED" in out}


def reboot() -> str:
    return _helper("reboot", timeout=30)
