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
    """Make the running avahi-publish processes match the saved .local names (restarting any that stopped).
    Only the network watcher process does this (procs.owns("network")); the web interface just saves the names
    and shows the state the watcher records in the database."""
    from . import procs
    if not procs.owns("network") or not avahi_available():
        return
    try:
        _sync_mdns()
    finally:
        try:
            state = {n: dict(zip(("state", "error"), _mdns_state(n))) for n in _procs}
            db.set_setting("lan_mdns_state", json.dumps({"at": time.time(), "names": state}))
        except Exception:  # noqa: BLE001
            pass


def _sync_mdns() -> None:
    with _lock:
        _clean_strays()
        want = {n["name"]: n["ip"] for n in _load("lan_names")}
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
    from . import procs
    if not procs.owns("network"):
        # another process (the network watcher) announces the names: read what it last recorded
        try:
            rec = json.loads(db.get_setting("lan_mdns_state") or "{}")
        except ValueError:
            rec = {}
        if not rec or time.time() - float(rec.get("at") or 0) > 60:
            return "error", "The network watcher isn't running, so .local names aren't announced. See Server → Processes."
        n = (rec.get("names") or {}).get(name)
        return (n["state"], n.get("error")) if n else ("pending", None)
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
    sites = list_sites()
    for n in names:
        mine = [s for s in sites if s["hostname"] == n["name"]] if n["ip"] == me else []
        n["ports"] = [{"port": s["port"], "target": s["target"], "https": bool(s.get("https")), "site_id": s["id"],
                       "app": s["target"] == app_target()} for s in mine]
        n["device_ip"] = _mapped_device(mine) or n["ip"]     # what the edit dialog shows as the address
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
        # its port mappings (websites by this name) go too: without the name they can't be reached
        name = key if kind == "mdns" else next((e["name"] for e in _router_names() if e["id"] == key), None)
        sites = _load("lan_sites")
        if name and any(s["hostname"] == name for s in sites):
            _save("lan_sites", [s for s in sites if s["hostname"] != name])
            try:
                publish._write_caddy()
            except Exception as e:  # noqa: BLE001
                raise NamesError(str(e))
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
    """How the proxy reaches SysAdmin.IA: its main port, over HTTPS when the app uses TLS."""
    host = settings.host if settings.host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    return f"{'https' if settings.tls_enabled else 'http'}://{host}:{settings.port}"


def _is_app(target: str) -> bool:
    """A target typed by hand that is in fact SysAdmin.IA itself (this server, one of the app's ports)."""
    import urllib.parse
    u = urllib.parse.urlsplit(target if "://" in target else "http://" + target)
    mine = {"127.0.0.1", "localhost", "0.0.0.0", server_ip() or "-", settings.host}
    try:
        return (u.hostname or "") in mine and u.port in {p for p, _tls in settings.listeners()}
    except ValueError:
        return False


def site_port(s: dict) -> int:
    """The port a website listens on (older entries have none: the default for its scheme)."""
    return int(s.get("port") or (443 if s.get("https") else 80))


def list_sites() -> list[dict]:
    return [{**s, "port": site_port(s)} for s in _load("lan_sites")]


def site_targets() -> list[dict]:
    return [{"label": "SysAdmin.IA (this app)", "target": APP_TARGET, "group": "app"}] + publish.targets()


def _proxy_ports(sites: list[dict]) -> set[int]:
    """Ports the proxy already listens on (so they're ours, not "used by another program")."""
    ports = {site_port(s) for s in sites}
    if any(s.get("https") and site_port(s) == 443 for s in sites):
        ports.add(80)                                    # Caddy redirects http:// to https:// on port 80
    if (publish.get_config()["caddy"] or {}).get("enabled"):
        ports |= {80, 443}
    return ports


def _check_listen_port(port, https: bool) -> int:
    if port in (None, ""):
        return 443 if https else 80
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise NamesError("Ports are numbers between 1 and 65535.")
    if not 1 <= port <= 65535:
        raise NamesError("Ports are numbers between 1 and 65535.")
    if port in {p for p, _tls in settings.listeners()}:
        raise NamesError(f"Port {port} is SysAdmin.IA's own port. Pick another one, e.g. 80.")
    return port


def _target_host(target: str) -> str:
    import urllib.parse
    h = urllib.parse.urlsplit(target).hostname or ""
    return (server_ip() or h) if h in ("127.0.0.1", "localhost") else h


def _mapped_device(mapped: list[dict]) -> str | None:
    """The device a name's port mappings lead to (the first one that isn't SysAdmin.IA itself)."""
    return next((_target_host(s["target"]) for s in mapped if s["target"] != app_target()), None)


def _retarget(target: str, ip: str) -> str:
    """Same scheme and port, another device (this server itself becomes 127.0.0.1)."""
    import urllib.parse
    u = urllib.parse.urlsplit(target)
    host = "127.0.0.1" if ip == server_ip() else ip
    return f"{u.scheme}://{host}:{u.port}"


def update_name(nid: str, name: str, ip: str) -> dict:
    """Rename a name and/or point it elsewhere. A name with port mappings keeps pointing to this server (the
    proxy); a new address then moves its mappings to that device."""
    kind, _, key = (nid or "").partition(":")
    ip = _check_ip(ip)
    with _lock:
        if kind == "mdns":
            entry = next((n for n in _load("lan_names") if n["name"] == key), None)
            old_name, old_ip, auto = (entry["name"], entry["ip"], bool(entry.get("auto"))) if entry else (None, None, False)
        elif kind == "router":
            e = next((x for x in _router_names() if x["id"] == key), None)
            old_name, old_ip, auto = (e["name"], e["ip"], False) if e else (None, None, False)
        else:
            raise NamesError("Unknown name.")
        if not old_name:
            raise NamesError("That name doesn't exist any more.")
        new_name = _check_name(name) if (name or "").strip().lower().rstrip(".") != old_name else old_name
        me = server_ip()
        sites = _load("lan_sites")
        mapped = [s for s in sites if s["hostname"] == old_name] if old_ip == me else []
        point_to = old_ip if mapped else ip
        if new_name != old_name:
            if _name_exists(new_name):
                raise NamesError(f"{new_name} already exists. Remove it first, or pick another name.")
            if any(s["hostname"] == new_name for s in sites) or any(s["hostname"] == new_name for s in publish.list_services()):
                raise NamesError(f"{new_name} is already used by a website.")
            add_name(new_name, point_to, auto=auto)
            try:
                if kind == "mdns":
                    _remove_mdns(old_name)
                else:
                    routers.dns_remove(key)
            except routers.RouterError as ex:
                raise NamesError(f"{new_name} was added, but {old_name} couldn't be removed: {ex}")
        elif not mapped and ip != old_ip:
            add_name(old_name, ip, auto=auto)
        device_now = _mapped_device(mapped)
        move = mapped and ip not in (old_ip, device_now)
        if mapped and (new_name != old_name or move):
            for s in sites:
                if s["hostname"] == old_name:
                    s["hostname"] = new_name
                    if move and s["target"] != app_target() and _target_host(s["target"]) == device_now:
                        s["target"] = _retarget(s["target"], ip)
            _save("lan_sites", sites)
            try:
                publish._write_caddy()
            except Exception as ex:  # noqa: BLE001
                raise NamesError(str(ex))
    return list_names()


def _router_names() -> list[dict]:
    if not routers.has_cap("dns"):
        return []
    try:
        return routers.dns_list()
    except routers.RouterError:
        return []


def _name_ip(name: str) -> str | None:
    """Where a name points now (None if it doesn't exist)."""
    if name.endswith(".local"):
        return next((n["ip"] for n in _load("lan_names") if n["name"] == name), None)
    if not routers.has_cap("dns"):
        return None
    try:
        return next((e["ip"] for e in routers.dns_list() if e["name"] == name), None)
    except routers.RouterError:
        return None


def _name_exists(name: str) -> bool:
    return _name_ip(name) is not None


def _firewall_note(port: int) -> str:
    try:
        from . import firewall
        st = firewall.status()
    except Exception:  # noqa: BLE001 - firewall not manageable here: nothing to say
        return ""
    if not st.get("available") or not st.get("active") or not st.get("default_closed"):
        return ""
    if any(r["action"] == "allow" and any(lo <= port <= hi and p in ("any", "tcp") for lo, hi, p in r["ports"]) for r in st["rules"]):
        return ""
    return f"The firewall keeps port {port} closed: open it in Network → Ports."


def _check_hostname(hostname: str) -> str:
    hostname = (hostname or "").strip().lower().rstrip(".")
    if hostname.endswith(".local"):
        if not MDNS_LABEL.fullmatch(hostname):
            raise NamesError("A .local name is one word plus .local, e.g. photos.local.")
    elif not publish.HOST_RE.fullmatch(hostname):
        raise NamesError("Enter the site's name, e.g. photos.local or photos.home.lan.")
    return hostname


def _check_site_target(target: str) -> str:
    t = (target or "").strip()
    if t == APP_TARGET or t == app_target() or _is_app(t):
        return app_target()          # SysAdmin.IA itself, however it was typed (its port may be HTTPS)
    try:
        return publish.check_target(t, allow_admin=True)
    except publish.PublishError as e:
        raise NamesError(str(e))


def add_site(hostname: str, target: str, https: bool = False, make_name: bool = True, port=None,
             repoint: bool = False, _id: int | None = None, _inherit: dict | None = None,
             _extra_ports: frozenset = frozenset()) -> dict:
    """A website on this server: hostname:port (default 80, or 443 for HTTPS) → target, through the proxy.
    repoint: if the name already points to another device, point it to this server (the proxy) instead.
    _id/_inherit/_extra_ports: used by update_site (keep the id and what was done with the name)."""
    hostname = _check_hostname(hostname)
    target = _check_site_target(target)
    port = _check_listen_port(port, https)
    if https and port == 80:
        raise NamesError("HTTPS can't use port 80 (browsers ask port 80 for plain HTTP). Use 443, or turn HTTPS off.")
    if not https and port == 443:
        raise NamesError("Port 443 is for HTTPS. Turn HTTPS on, or use port 80.")
    with _lock:
        sites = list_sites()
        if any(s["hostname"] == hostname and s["port"] == port for s in sites):
            raise NamesError(f"{hostname}:{port} is already a website here.")
        if any(s["hostname"] == hostname for s in publish.list_services()):
            raise NamesError(f"{hostname} is already published on the internet (Network → Publish).")
        need = {port} | ({80} if https and port == 443 else set())
        try:
            publish.ports_free(tuple(sorted(need - _proxy_ports(sites) - set(_extra_ports))))
        except publish.PublishError as e:
            raise NamesError(str(e))
        site = {"id": _id or max([s["id"] for s in _load("lan_sites")] + [0]) + 1, "hostname": hostname, "target": target,
                "https": bool(https), "port": port, "created": (_inherit or {}).get("created", time.time()),
                "name_added": bool((_inherit or {}).get("name_added"))}
        if (_inherit or {}).get("repointed_from"):
            site["repointed_from"] = _inherit["repointed_from"]
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
        notes = []
        ip = server_ip()
        now = _name_ip(hostname)
        if target == app_target() and not https and settings.tls_enabled:
            notes.append("SysAdmin.IA is reached over plain HTTP at this address: passwords cross the network "
                         "unencrypted. Turn HTTPS on for it if you can.")
        if ip and make_name and now is None:
            try:
                add_name(hostname, ip, auto=True)
                site["name_added"] = True
            except NamesError as e:
                notes.append(f"The site works, but its name wasn't created: {e} Point {hostname} to {ip} yourself.")
        elif ip and now and now != ip:
            if repoint:
                try:
                    add_name(hostname, ip, auto=hostname.endswith(".local") and _is_auto(hostname))
                    site["repointed_from"] = now
                except NamesError as e:
                    notes.append(f"{hostname} still points to {now}: {e}")
            else:
                notes.append(f"{hostname} points to {now}, not to this server ({ip}): visits by that name won't reach the website.")
        elif ip and now is None:
            notes.append(f"Make sure {hostname} points to this server ({ip}).")
        _save("lan_sites", [s if s["id"] != site["id"] else site for s in _load("lan_sites")])
        fw = _firewall_note(port)
        if fw:
            notes.append(fw)
    return {"site": site, "note": " ".join(notes)}


def _is_auto(name: str) -> bool:
    return any(n["name"] == name and n.get("auto") for n in _load("lan_names"))


def _release_name(site: dict, remaining: list[dict]) -> None:
    """After the last website of a name is gone: remove the name it created, or point it back where it was."""
    host = site["hostname"]
    if any(s["hostname"] == host for s in remaining):
        return
    ip = server_ip()
    try:
        if site.get("repointed_from"):
            add_name(host, site["repointed_from"])
        elif site.get("name_added"):
            if host.endswith(".local"):
                if _is_auto(host):
                    _remove_mdns(host)
            else:
                for e in routers.dns_list():
                    if e["name"] == host and e["ip"] == ip:
                        routers.dns_remove(e["id"])
    except (NamesError, routers.RouterError):
        pass


def _remove_mdns(name: str) -> None:
    _save("lan_names", [n for n in _load("lan_names") if n["name"] != name])
    sync_mdns()


def update_site(sid: int, hostname: str, target: str, https: bool = False, port=None) -> dict:
    """Change a website (name, port, target, HTTPS), keeping its id; the old name is released if no other
    website uses it any more."""
    with _lock:
        sites = _load("lan_sites")
        old = next((s for s in sites if s["id"] == sid), None)
        if not old:
            raise NamesError("That website doesn't exist any more.")
        others = [s for s in sites if s["id"] != sid]
        new_host = _check_hostname(hostname)
        _save("lan_sites", others)
        try:
            r = add_site(new_host, target, https, make_name=True, port=port, repoint=False, _id=sid,
                         _inherit=old if new_host == old["hostname"] else None,
                         _extra_ports=frozenset(_proxy_ports(sites)))
        except Exception:
            _save("lan_sites", sites)
            try:
                publish._write_caddy()
            except Exception:  # noqa: BLE001
                pass
            raise
        if new_host != old["hostname"]:
            _release_name(old, _load("lan_sites"))
        return r


def remove_site(sid: int) -> None:
    with _lock:
        sites = _load("lan_sites")
        site = next((s for s in sites if s["id"] == sid), None)
        if not site:
            raise NamesError("That website doesn't exist any more.")
        remaining = [s for s in sites if s["id"] != sid]
        _save("lan_sites", remaining)
        try:
            publish._write_caddy()
        except Exception as e:  # noqa: BLE001
            raise NamesError(str(e))
        _release_name(site, remaining)


def add_name_with_ports(name: str, ip: str, port=None, target_port=None, https: bool = False) -> dict:
    """A name, optionally with a port mapping: name:port → ip:target_port through this server's proxy
    (e.g. website1.local:80 → 192.168.1.10:8080). Without target_port it's a plain name (all ports go to ip)."""
    if target_port in (None, ""):
        if port not in (None, ""):
            raise NamesError("Enter the port on the device too (or leave both ports empty for a plain name).")
        return add_name(name, ip)
    name, ip = _check_name(name), _check_ip(ip)
    try:
        tp = int(target_port)
        if not 1 <= tp <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        raise NamesError("Ports are numbers between 1 and 65535.")
    if ip == server_ip():
        ip = "127.0.0.1"                                  # a port of this server itself
    # the device's port is reached over plain HTTP (for an HTTPS device page use a website with an https:// target)
    r = add_site(name, f"http://{ip}:{tp}", https=https, make_name=True, port=port, repoint=True)
    return {**list_names(), "note": r["note"]}


def sites_status() -> dict:
    c = publish._container(publish.CADDY_CONTAINER) if publish._docker_ok() else None
    return {"sites": list_sites(), "docker": publish._docker_ok(), "proxy": c["state"] if c else None,
            "ca": publish.local_ca_cert() is not None, "targets": site_targets(), "app_target": app_target(),
            "public_proxy": bool((publish.get_config()["caddy"] or {}).get("enabled"))}
