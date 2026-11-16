"""Names on the local network (Network → Names): this server's own name, names for devices, and
websites served by name on shared ports.

Names
  name.local     announced by this server over mDNS with `avahi-publish -a -R` (one small process per name,
                 started by the app and restarted if it stops; no root needed). Phones, Macs, Windows 10+ and
                 Linux resolve .local names on the same network without any router support.
  other names    (e.g. nas.home.lan) are static DNS entries in the router (OpenWrt dnsmasq, MikroTik
                 /ip/dns/static); devices must use the router as their DNS server (the usual setup).

Websites
  hostname → address of a web app (a container, another device, or SysAdmin.IA itself). Served by the
  Caddy proxy (container sai-caddy, shared with Network → Publish), so any number of sites share port 80
  (and 443 for HTTPS) on this server and are told apart by the name in each request. HTTPS uses Caddy's
  own local certificate authority; browsers warn until its root certificate is trusted.
"""
from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
import threading
import time

from . import db, publish, routers
from .config import settings

MDNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.local$")
_lock = threading.RLock()
_procs: dict[str, dict] = {}     # name → {"proc": Popen, "ip": str, "error": str|None, "started": float}


class NamesError(RuntimeError):
    pass


# ---------------------------------------------------------------- storage
def _load(key: str) -> list[dict]:
    try:
        v = json.loads(db.get_setting(key) or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def _save(key: str, value: list[dict]) -> None:
    db.set_setting(key, json.dumps(value))


def _check_ip(ip: str) -> str:
    ip = (ip or "").strip()
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        raise NamesError("Enter an IPv4 address on your network, e.g. 192.168.1.20.")
    if not a.is_private or a.is_loopback:
        raise NamesError("The address must be a device on your network (a private address such as 192.168.1.20).")
    return ip


def _check_name(name: str) -> str:
    name = (name or "").strip().lower().rstrip(".")
    if name.endswith(".local"):
        if not MDNS_LABEL.fullmatch(name):
            raise NamesError("A .local name is one word plus .local, e.g. nas.local (letters, digits and hyphens).")
    elif not publish.HOST_RE.fullmatch(name):
        raise NamesError("Enter a name such as nas.local, or nas.home.lan for a name kept in the router.")
    from . import server_name
    if name == f"{server_name.current()}.local":
        raise NamesError("That's this server's own name. Change it under “Server name” above.")
    return name


def server_ip() -> str | None:
    return publish.lan_ip()


# ---------------------------------------------------------------- mDNS publishing (avahi-publish)
def avahi_available() -> bool:
    return bool(shutil.which("avahi-publish"))


def _start(name: str, ip: str) -> None:
    try:
        p = subprocess.Popen(["avahi-publish", "-a", "-R", name, ip], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        _procs[name] = {"proc": p, "ip": ip, "error": None, "started": time.time()}
    except OSError as e:
        _procs[name] = {"proc": None, "ip": ip, "error": str(e), "started": time.time()}


def _stop(name: str) -> None:
    e = _procs.pop(name, None)
    if e and e["proc"] and e["proc"].poll() is None:
        e["proc"].terminate()
        try:
            e["proc"].wait(timeout=3)
        except subprocess.TimeoutExpired:
            e["proc"].kill()


def sync_mdns() -> None:
    """Make the running avahi-publish processes match the saved .local names (restarting any that stopped)."""
    if not avahi_available():
        return
    with _lock:
        _clean_strays()
        want ={n["name"]: n["ip"] for n in _load("lan_names")}
        for name in list(_procs):
            if name not in want or _procs[name]["ip"] != want[name]:
                _stop(name)
        for name, ip in want.items():
            e = _procs.get(name)
            if e and e["proc"] and e["proc"].poll() is not None:
                err = (e["proc"].stderr.read() if e["proc"].stderr else "").strip()
                e["error"] = _avahi_error(err)
                if time.time() - e["started"] > 60:       # retry a failed name once a minute
                    _start(name, ip)
                    _procs[name]["error"] = e["error"]
            elif not e:
                _start(name, ip)


def _avahi_error(text: str) -> str:
    if "collision" in text.lower():
        return "Another device already uses this name."
    if "daemon not running" in text.lower() or "Daemon not running" in text:
        return "The avahi-daemon service isn't running."
    return text.splitlines()[-1] if text else "avahi-publish stopped."


def _mdns_state(name: str) -> tuple[str, str | None]:
    if not avahi_available():
        return "error", "avahi-publish isn't installed (package avahi-utils)."
    e = _procs.get(name)
    if not e:
        return "pending", None
    if e["proc"] and e["proc"].poll() is None:
        return "ok", None
    return "error", e["error"] or "avahi-publish stopped."


_cleaned = False


def _clean_strays() -> None:
    """avahi-publish processes left by a previous run of the app (they'd make the new ones collide)."""
    global _cleaned
    if _cleaned:
        return
    _cleaned = True
    try:
        import os
        import psutil
        me = os.getuid() if hasattr(os, "getuid") else None
        for p in psutil.process_iter(["pid", "cmdline", "uids", "ppid"]):
            cmd = p.info.get("cmdline") or []
            if cmd[:3] == ["avahi-publish", "-a", "-R"] and (me is None or (p.info.get("uids") and p.info["uids"].real == me)):
                p.terminate()
    except Exception:  # noqa: BLE001 - best effort
        pass


def stop_all() -> None:
    with _lock:
        for name in list(_procs):
            _stop(name)


# ---------------------------------------------------------------- names
def _devices_by_ip() -> dict[str, str]:
    try:
        from . import network
        return {d["ip"]: d["name"] or d.get("vendor") or d["mac"] for d in network.devices() if d.get("ip")}
    except Exception:  # noqa: BLE001
        return {}


def list_names() -> dict:
    from . import server_name
    sn = server_name.status()
    me = server_ip()
    devs = _devices_by_ip()
    sync_mdns()
    names = []
    for n in _load("lan_names"):
        st, err = _mdns_state(n["name"])
        names.append({"id": f"mdns:{n['name']}", "name": n["name"], "ip": n["ip"], "where": "mdns", "state": st, "error": err,
                      "device": "This server" if n["ip"] == me else devs.get(n["ip"], ""), "auto": bool(n.get("auto"))})
    router_dns, router_error = routers.has_cap("dns"), None
    if router_dns:
        try:
            for e in routers.dns_list():
                names.append({"id": f"router:{e['id']}", "name": e["name"], "ip": e["ip"], "where": "router", "state": "ok",
                              "error": None, "device": "This server" if e["ip"] == me else devs.get(e["ip"], ""), "auto": False})
        except routers.RouterError as ex:
            router_error = str(ex)
    names.sort(key=lambda n: n["name"])
    return {"server": {"name": sn["name"], "mdns_name": sn["mdns_name"], "mdns": sn["mdns"], "ip": me},
            "names": names, "router_dns": router_dns, "router_error": router_error,
            "avahi": avahi_available(), "server_ip": me}


def add_name(name: str, ip: str, auto: bool = False) -> dict:
    name, ip = _check_name(name), _check_ip(ip)
    with _lock:
        if name.endswith(".local"):
            if not avahi_available():
                raise NamesError("Publishing .local names needs avahi-publish on the server (package avahi-utils; "
                                 "installing the next update sets it up).")
            names = [n for n in _load("lan_names") if n["name"] != name]
            names.append({"name": name, "ip": ip, "auto": auto, "created": time.time()})
            _save("lan_names", names)
            sync_mdns()
        else:
            if not routers.has_cap("dns"):
                raise NamesError("Names other than .local are kept in the router's DNS, and this router type can't hold them "
                                 "(OpenWrt and MikroTik can). Use a .local name instead.")
            try:
                routers.dns_add(name, ip)
            except routers.RouterError as e:
                raise NamesError(str(e))
    return list_names()


def remove_name(nid: str) -> dict:
    kind, _, key = (nid or "").partition(":")
    with _lock:
        if kind == "mdns":
            names = _load("lan_names")
            if not any(n["name"] == key for n in names):
                raise NamesError("That name doesn't exist any more.")
            _save("lan_names", [n for n in names if n["name"] != key])
            sync_mdns()
        elif kind == "router":
            try:
                routers.dns_remove(key)
            except routers.RouterError as e:
                raise NamesError(str(e))
        else:
            raise NamesError("Unknown name.")
    return list_names()


def _name_exists(name: str) -> bool:
    if name.endswith(".local"):
        return any(n["name"] == name for n in _load("lan_names"))
    try:
        return any(e["name"] == name for e in routers.dns_list())
    except routers.RouterError:
        return False


# ---------------------------------------------------------------- websites on shared ports
APP_TARGET = "app"


def app_target() -> str:
    return f"{'https' if settings.tls_enabled else 'http'}://127.0.0.1:{settings.port}"


def list_sites() -> list[dict]:
    return _load("lan_sites")


def site_targets() -> list[dict]:
    return [{"label": "SysAdmin.IA (this app)", "target": APP_TARGET}] + publish.targets()


def add_site(hostname: str, target: str, https: bool = False, make_name: bool = True) -> dict:
    hostname = (hostname or "").strip().lower().rstrip(".")
    if hostname.endswith(".local"):
        if not MDNS_LABEL.fullmatch(hostname):
            raise NamesError("A .local name is one word plus .local, e.g. photos.local.")
    elif not publish.HOST_RE.fullmatch(hostname):
        raise NamesError("Enter the site's name, e.g. photos.local or photos.home.lan.")
    if (target or "").strip() == APP_TARGET:
        target = app_target()
        https = https or settings.tls_enabled      # the app's secure login cookie needs HTTPS in front of it
    else:
        try:
            target = publish.check_target(target, allow_admin=True)
        except publish.PublishError as e:
            raise NamesError(str(e))
    with _lock:
        sites = list_sites()
        if any(s["hostname"] == hostname for s in sites):
            raise NamesError(f"{hostname} is already a website here.")
        if any(s["hostname"] == hostname for s in publish.list_services()):
            raise NamesError(f"{hostname} is already published on the internet (Network → Publish).")
        try:
            publish.ports_free((80, 443) if https or any(s.get("https") for s in sites) else (80,))
        except publish.PublishError as e:
            raise NamesError(str(e))
        site = {"id": max([s["id"] for s in sites] + [0]) + 1, "hostname": hostname, "target": target,
                "https": bool(https), "created": time.time(), "name_added": False}
        _save("lan_sites", sites + [site])
        try:
            publish._write_caddy()
        except Exception as e:  # noqa: BLE001 - undo, then report
            _save("lan_sites", sites)
            try:
                publish._write_caddy()
            except Exception:  # noqa: BLE001
                pass
            raise NamesError(str(e))
        note = ""
        ip = server_ip()
        if make_name and ip and not _name_exists(hostname):
            try:
                add_name(hostname, ip, auto=True)
                site["name_added"] = True
                _save("lan_sites", [s if s["id"] != site["id"] else site for s in list_sites()])
            except NamesError as e:
                note = f"The site works, but its name wasn't created: {e} Point {hostname} to {ip} yourself."
        elif not _name_exists(hostname) and ip:
            note = f"Make sure {hostname} points to this server ({ip})."
    return {"site": site, "note": note}


def remove_site(sid: int) -> None:
    with _lock:
        sites = list_sites()
        site = next((s for s in sites if s["id"] == sid), None)
        if not site:
            raise NamesError("That website doesn't exist any more.")
        _save("lan_sites", [s for s in sites if s["id"] != sid])
        try:
            publish._write_caddy()
        except Exception as e:  # noqa: BLE001
            raise NamesError(str(e))
        if site.get("name_added"):
            ip = server_ip()
            try:
                if site["hostname"].endswith(".local"):
                    if any(n["name"] == site["hostname"] and n.get("auto") for n in _load("lan_names")):
                        remove_name(f"mdns:{site['hostname']}")
                else:
                    for e in routers.dns_list():
                        if e["name"] == site["hostname"] and e["ip"] == ip:
                            routers.dns_remove(e["id"])
            except (NamesError, routers.RouterError):
                pass


def sites_status() -> dict:
    c = publish._container(publish.CADDY_CONTAINER) if publish._docker_ok() else None
    return {"sites": list_sites(), "docker": publish._docker_ok(), "proxy": c["state"] if c else None,
            "ca": publish.local_ca_cert() is not None, "targets": site_targets(), "app_target": app_target(),
            "public_proxy": bool((publish.get_config()["caddy"] or {}).get("enabled"))}
