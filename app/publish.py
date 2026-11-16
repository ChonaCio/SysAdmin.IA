"""Publish self-hosted services (websites, apps in containers…) on the internet.

Two ways, both managed from Network → Publish:

cloudflare – Cloudflare Tunnel (recommended). Needs a domain on Cloudflare and an API token with
             "Account · Cloudflare Tunnel · Edit" and "Zone · DNS · Edit". SysAdmin.IA creates a tunnel,
             runs its connector (container "sai-cloudflared", host network, image cloudflare/cloudflared)
             and, per published hostname, adds the tunnel route and a proxied DNS record. Nothing is
             opened on the router and it works behind carrier-grade NAT; Cloudflare provides HTTPS.
caddy      – Port forwarding + Caddy (container "sai-caddy", host network, image caddy:2). Caddy gets free
             Let's Encrypt certificates and forwards each hostname to its local address. Ports 80 and 443
             must reach this server (forwarded on the router — automatic when the Router tab can do it)
             and each hostname's DNS must point to the public address (dynamic DNS below keeps it current).

Dynamic DNS: Cloudflare (A records in the zone, same token) or DuckDNS (token + subdomains), updated
by network.NetworkEngine when the public address changes.

Secrets (Cloudflare token, tunnel token, DuckDNS token) live in app_settings and are never sent to browsers.
"""
from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import containers, db, network, routers
from .config import settings

CF_API = "https://api.cloudflare.com/client/v4"
IP_SERVICES = ["https://api.ipify.org", "https://ipv4.icanhazip.com", "https://ifconfig.me/ip"]
CF_CONTAINER, CF_IMAGE = "sai-cloudflared", "cloudflare/cloudflared:latest"
CADDY_CONTAINER, CADDY_IMAGE = "sai-caddy", "caddy:2"
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_lock = threading.RLock()


class PublishError(RuntimeError):
    pass


def _dir() -> Path:
    d = settings.data_dir / "publish"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_config() -> dict:
    try:
        c = json.loads(db.get_setting("publish") or "{}")
    except ValueError:
        c = {}
    c.setdefault("cloudflare", {})
    c.setdefault("caddy", {})
    c.setdefault("ddns", {})
    return c


def _save_config(c: dict) -> None:
    db.set_setting("publish", json.dumps(c))


def _secret(key: str) -> str:
    return db.get_setting(f"publish_{key}") or ""


def _set_secret(key: str, value: str | None) -> None:
    db.set_setting(f"publish_{key}", value or None)


# ---------------------------------------------------------------- public address

_ip_cache: dict = {"ip": None, "at": 0.0}


def public_ip(max_age: float = 300) -> str | None:
    if _ip_cache["ip"] and time.time() - _ip_cache["at"] < max_age:
        return _ip_cache["ip"]
    for url in IP_SERVICES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SysAdmin.IA"})
            with urllib.request.urlopen(req, timeout=6) as r:
                ip = r.read(64).decode().strip()
            ipaddress.IPv4Address(ip)
            _ip_cache.update(ip=ip, at=time.time())
            return ip
        except Exception:
            continue
    return None


def cgnat_state(router_ip: str, pub: str | None) -> str:
    """'ok', 'cgnat' (the router's own WAN address isn't public: port forwarding can't work), or 'unknown'."""
    if not router_ip or not pub:
        return "unknown"
    try:
        a = ipaddress.ip_address(router_ip)
    except ValueError:
        return "unknown"
    if a.is_private or a in ipaddress.ip_network("100.64.0.0/10") or router_ip != pub:
        return "cgnat"
    return "ok"


def lan_ip() -> str | None:
    """This server's address in the router's network (what port forwards point to)."""
    gw = network.gateway()
    ifs = network.interfaces()
    for i in ifs:
        if gw and ipaddress.ip_address(gw) in ipaddress.ip_network(i["network"]):
            return i["ip"]
    return ifs[0]["ip"] if ifs else None


# ---------------------------------------------------------------- containers (connector / proxy)

def _container(name: str) -> dict | None:
    try:
        return next((c for c in containers.list_containers() if c["name"] == name), None)
    except containers.DockerError:
        return None


def _ensure_container(spec: dict, recreate: bool = False) -> None:
    """Create (pulling the image if needed) and start a helper container; replace it when recreate=True."""
    try:
        containers._req("GET", "/_ping")
    except containers.DockerError:
        raise PublishError("Docker is needed for this: install it in the Containers module first.")
    old = _container(spec["name"])
    if old and not recreate:
        if old["state"] != "running":
            containers.container_action(old["id"], "start")
        return
    if old:
        containers.remove_container(old["id"])
    s = containers.validate_spec(spec)
    if containers.image_details(s["image"]) is None:
        containers._pull(s["image"], containers._sync_upd([]))
    r = containers._req("POST", "/containers/create", {"name": s["name"]}, containers._create_body(s))
    containers._req("POST", f"/containers/{r['Id']}/start")


def _remove_container(name: str) -> None:
    c = _container(name)
    if c:
        containers.remove_container(c["id"])


# ---------------------------------------------------------------- Cloudflare API

def _cf(method: str, path: str, body=None, token: str | None = None, query: dict | None = None):
    token = token or _secret("cf_token")
    if not token:
        raise PublishError("Connect Cloudflare first.")
    url = CF_API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read() or b"{}")
        except ValueError:
            raise PublishError(f"Cloudflare returned HTTP {e.code}.")
    except (urllib.error.URLError, OSError) as e:
        raise PublishError(f"Can't reach Cloudflare: {getattr(e, 'reason', e)}")
    if not data.get("success", False):
        errs = "; ".join(f"{x.get('message')}" for x in data.get("errors") or []) or "request failed"
        if "Authentication" in errs or "Invalid API Token" in errs or "authorization" in errs.lower():
            errs += " — check the token and its permissions (Cloudflare Tunnel: Edit, DNS: Edit)"
        raise PublishError(f"Cloudflare: {errs}")
    return data.get("result")


def cf_check(token: str) -> dict:
    """Validate a token and list what it can reach: {accounts:[…], zones:[{id,name}]}."""
    token = (token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{30,80}", token):
        raise PublishError("That doesn't look like a Cloudflare API token.")
    _cf("GET", "/user/tokens/verify", token=token)
    accounts = _cf("GET", "/accounts", token=token, query={"per_page": 50}) or []
    zones = _cf("GET", "/zones", token=token, query={"per_page": 50, "status": "active"}) or []
    if not accounts:
        raise PublishError("The token can't see any Cloudflare account (add “Account · Cloudflare Tunnel · Edit”).")
    if not zones:
        raise PublishError("The token can't see any domain (add “Zone · DNS · Edit” for your domain).")
    return {"accounts": [{"id": a["id"], "name": a.get("name", "")} for a in accounts],
            "zones": [{"id": z["id"], "name": z["name"], "account": (z.get("account") or {}).get("id")} for z in zones]}


def cf_connect(token: str, zone_id: str) -> dict:
    """Create (or reuse) this server's tunnel in the zone's account and start its connector."""
    with _lock:
        info = cf_check(token)
        zone = next((z for z in info["zones"] if z["id"] == zone_id), None)
        if not zone:
            raise PublishError("Choose one of the domains the token can manage.")
        account = zone["account"] or info["accounts"][0]["id"]
        name = f"sysadmin-ia-{network.socket.gethostname()}"[:60]
        found = _cf("GET", f"/accounts/{account}/cfd_tunnel", token=token, query={"name": name, "is_deleted": "false"}) or []
        tunnel = found[0] if found else _cf("POST", f"/accounts/{account}/cfd_tunnel", {"name": name, "config_src": "cloudflare"},
                                            token=token)
        tunnel_token = _cf("GET", f"/accounts/{account}/cfd_tunnel/{tunnel['id']}/token", token=token)
        _set_secret("cf_token", token.strip())
        _set_secret("cf_tunnel_token", tunnel_token)
        c = get_config()
        c["cloudflare"] = {"account_id": account, "zone_id": zone["id"], "zone_name": zone["name"],
                           "tunnel_id": tunnel["id"], "tunnel_name": tunnel.get("name", name)}
        _save_config(c)
        _cf_ingress()
        _ensure_container({"name": CF_CONTAINER, "image": CF_IMAGE, "network": "host", "restart": "unless-stopped",
                           "command": "tunnel --no-autoupdate run", "env": [{"key": "TUNNEL_TOKEN", "value": tunnel_token}]},
                          recreate=True)
        return status()


def _cf_ingress() -> None:
    c = get_config()["cloudflare"]
    rules = []
    for s in list_services("cloudflare"):
        rule = {"hostname": s["hostname"], "service": s["target"]}
        if s["target"].startswith("https://"):
            rule["originRequest"] = {"noTLSVerify": True}
        rules.append(rule)
    rules.append({"service": "http_status:404"})
    _cf("PUT", f"/accounts/{c['account_id']}/cfd_tunnel/{c['tunnel_id']}/configurations", {"config": {"ingress": rules}})


def cf_disconnect(delete_tunnel: bool = True) -> None:
    with _lock:
        c = get_config()
        cf = c.get("cloudflare") or {}
        for s in list_services("cloudflare"):
            _remove_service_dns(s, cf)
            db.execute("DELETE FROM published_services WHERE id=?", (s["id"],))
        _remove_container(CF_CONTAINER)
        if delete_tunnel and cf.get("tunnel_id") and _secret("cf_token"):
            try:
                _cf("DELETE", f"/accounts/{cf['account_id']}/cfd_tunnel/{cf['tunnel_id']}/connections")
                _cf("DELETE", f"/accounts/{cf['account_id']}/cfd_tunnel/{cf['tunnel_id']}")
            except PublishError:
                pass   # already gone, or still has connections: harmless leftover in the dashboard
        c["cloudflare"] = {}
        _save_config(c)
        _set_secret("cf_tunnel_token", None)
        if (c.get("ddns") or {}).get("provider") != "cloudflare":
            _set_secret("cf_token", None)


def _remove_service_dns(s: dict, cf: dict) -> None:
    rid = (s.get("state") or {}).get("dns_record")
    if rid and cf.get("zone_id"):
        try:
            _cf("DELETE", f"/zones/{cf['zone_id']}/dns_records/{rid}")
        except PublishError:
            pass


# ---------------------------------------------------------------- Caddy (port forwarding)

def caddyfile(services: list[dict], email: str) -> str:
    out = ["# Written by SysAdmin.IA (Network → Publish). Changes here are overwritten.", "{"]
    if email:
        out.append(f"\temail {email}")
    out += ["}", ""]
    for s in services:
        tgt = s["target"]
        if tgt.startswith("https://"):
            out += [f"{s['hostname']} {{", f"\treverse_proxy {tgt} {{", "\t\ttransport http {", "\t\t\ttls_insecure_skip_verify",
                    "\t\t}", "\t}", "}", ""]
        else:
            out += [f"{s['hostname']} {{", f"\treverse_proxy {tgt.removeprefix('http://')}", "}", ""]
    return "\n".join(out)


def caddy_setup(email: str, forward_ports: bool = True) -> dict:
    email = (email or "").strip()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise PublishError("That doesn't look like an email address.")
    with _lock:
        used = containers.used_host_ports(CADDY_CONTAINER) if _docker_ok() else {}
        for p in (80, 443):
            if p in used:
                raise PublishError(f"Port {p} on this server is already used by {used[p]}. Caddy needs ports 80 and 443.")
        c = get_config()
        c["caddy"] = {"email": email, "enabled": True, "forwarded": False}
        _save_config(c)
        _write_caddy(restart=False)
        d = _dir() / "caddy"
        _ensure_container({"name": CADDY_CONTAINER, "image": CADDY_IMAGE, "network": "host", "restart": "unless-stopped",
                           "mounts": [{"type": "bind", "source": str(d / "Caddyfile"), "target": "/etc/caddy/Caddyfile", "readonly": True},
                                      {"type": "bind", "source": str(d / "data"), "target": "/data"},
                                      {"type": "bind", "source": str(d / "config"), "target": "/config"}]}, recreate=True)
        note = ""
        if forward_ports:
            ip = lan_ip()
            try:
                existing = {(f["proto"], f["ext_port"]) for f in routers.forwards()}
                for port in (80, 443):
                    if ("tcp", port) not in existing and ("both", port) not in existing:
                        routers.add_forward(f"SysAdmin.IA HTTPS proxy ({port})", "tcp", port, ip, port)
                c = get_config()
                c["caddy"]["forwarded"] = True
                _save_config(c)
            except routers.RouterError as e:
                note = f"Ports 80 and 443 weren't forwarded automatically ({e}). Forward them to {ip} in the router."
        return {**status(), "note": note}


def _docker_ok() -> bool:
    try:
        containers._req("GET", "/_ping")
        return True
    except containers.DockerError:
        return False


def _write_caddy(restart: bool = True) -> None:
    d = _dir() / "caddy"
    for sub in ("data", "config"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "Caddyfile").write_text(caddyfile(list_services("caddy"), get_config()["caddy"].get("email", "")), encoding="utf-8")
    if restart:
        c = _container(CADDY_CONTAINER)
        if c:
            containers.container_action(c["id"], "restart")


def caddy_remove() -> None:
    with _lock:
        for s in list_services("caddy"):
            db.execute("DELETE FROM published_services WHERE id=?", (s["id"],))
        _remove_container(CADDY_CONTAINER)
        c = get_config()
        if (c.get("caddy") or {}).get("forwarded"):
            try:
                for f in routers.forwards():
                    if f["name"].startswith("SysAdmin.IA HTTPS proxy"):
                        routers.remove_forward(f["id"])
            except routers.RouterError:
                pass
        c["caddy"] = {}
        _save_config(c)


# ---------------------------------------------------------------- published services

def list_services(method: str | None = None) -> list[dict]:
    rows = db.query("SELECT * FROM published_services" + (" WHERE method=?" if method else "") + " ORDER BY hostname",
                    (method,) if method else ())
    return [{**dict(r), "state": json.loads(r["state"] or "{}")} for r in rows]


def check_target(target: str, allow_admin: bool = False) -> str:
    t = (target or "").strip()
    if not re.match(r"^https?://", t):
        t = "http://" + t
    u = urllib.parse.urlsplit(t)
    host, port = u.hostname or "", u.port
    if not port or not 1 <= port <= 65535 or u.path not in ("", "/") or u.query:
        raise PublishError("The target is an address and port, e.g. 127.0.0.1:8080 or 192.168.1.20:80.")
    if host != "localhost":
        try:
            a = ipaddress.ip_address(host)
        except ValueError:
            raise PublishError("The target must be this server (127.0.0.1) or a device on the LAN (its IP address).")
        if not (a.is_private or a.is_loopback) or a.version != 4:
            raise PublishError("The target must be this server (127.0.0.1) or a device on the LAN (its IP address).")
    if port == settings.port and host in ("127.0.0.1", "localhost", lan_ip() or "-") and not allow_admin:
        raise PublishError("That's SysAdmin.IA itself. Putting the server's admin page on the internet is risky; "
                           "confirm it explicitly if you really want to.")
    return f"{u.scheme}://{host}:{port}"


def add_service(hostname: str, target: str, method: str, allow_admin: bool = False) -> dict:
    hostname = (hostname or "").strip().lower().rstrip(".")
    if not HOST_RE.fullmatch(hostname):
        raise PublishError("Enter a full hostname, e.g. photos.example.com.")
    target = check_target(target, allow_admin)
    if method not in ("cloudflare", "caddy"):
        raise PublishError("Choose how to publish it.")
    with _lock:
        if db.query_one("SELECT id FROM published_services WHERE hostname=?", (hostname,)):
            raise PublishError(f"{hostname} is already published.")
        state: dict = {}
        if method == "cloudflare":
            cf = get_config()["cloudflare"]
            if not cf.get("tunnel_id"):
                raise PublishError("Connect Cloudflare first.")
            if hostname != cf["zone_name"] and not hostname.endswith("." + cf["zone_name"]):
                raise PublishError(f"The hostname must be in your Cloudflare domain {cf['zone_name']}.")
            existing = _cf("GET", f"/zones/{cf['zone_id']}/dns_records", query={"name": hostname}) or []
            target_cname = f"{cf['tunnel_id']}.cfargotunnel.com"
            mine = [r for r in existing if r.get("type") == "CNAME" and r.get("content") == target_cname]
            if existing and not mine:
                raise PublishError(f"{hostname} already has a DNS record in Cloudflare. Delete it there first, or pick another name.")
            rec = mine[0] if mine else _cf("POST", f"/zones/{cf['zone_id']}/dns_records",
                                           {"type": "CNAME", "name": hostname, "content": target_cname, "proxied": True,
                                            "comment": "SysAdmin.IA (Network → Publish)"})
            state["dns_record"] = rec["id"]
        elif not (get_config()["caddy"] or {}).get("enabled"):
            raise PublishError("Set up port forwarding + HTTPS first.")
        sid = db.execute("INSERT INTO published_services (hostname, target, method, state, created_at) VALUES (?,?,?,?,?)",
                         (hostname, target, method, json.dumps(state), time.time()))
        try:
            _cf_ingress() if method == "cloudflare" else _write_caddy()
        except Exception:
            db.execute("DELETE FROM published_services WHERE id=?", (sid,))
            raise
        return next(s for s in list_services() if s["id"] == sid)


def remove_service(sid: int) -> None:
    with _lock:
        s = next((x for x in list_services() if x["id"] == sid), None)
        if not s:
            raise PublishError("That published service no longer exists.")
        db.execute("DELETE FROM published_services WHERE id=?", (sid,))
        if s["method"] == "cloudflare":
            cf = get_config()["cloudflare"]
            _remove_service_dns(s, cf)
            if cf.get("tunnel_id"):
                _cf_ingress()
        else:
            _write_caddy()


def targets() -> list[dict]:
    """Suggestions for "what to publish": container ports on this server and web pages found on the LAN."""
    out = []
    try:
        for c in containers.list_containers():
            if c["name"] in (CF_CONTAINER, CADDY_CONTAINER):
                continue
            for p in c.get("ports") or []:
                if p.get("host") and p.get("proto", "tcp") == "tcp":
                    out.append({"label": f"{c['name']} (container, port {p['host']})", "target": f"http://127.0.0.1:{p['host']}"})
    except containers.DockerError:
        pass
    for d in network.devices():
        for port, info in (d.get("http") or {}).items():
            tls = int(port) in (443, 8443, 5001, 8006)
            if d.get("kind") == "this_server":
                continue
            out.append({"label": f"{d['name'] or d['ip']}: {info.get('title') or 'web page'} (port {port})",
                        "target": f"{'https' if tls else 'http'}://{d['ip']}:{port}"})
    return out


# ---------------------------------------------------------------- dynamic DNS

def ddns_save(provider: str, hostnames: str, token: str | None = None) -> dict:
    names = [h.strip().lower() for h in re.split(r"[\s,]+", hostnames or "") if h.strip()]
    if provider not in ("", "cloudflare", "duckdns"):
        raise PublishError("Unknown dynamic DNS provider.")
    if provider == "duckdns":
        names = [h.removesuffix(".duckdns.org") for h in names]
        if not names or not all(re.fullmatch(r"[a-z0-9-]{1,63}", h) for h in names):
            raise PublishError("DuckDNS: enter your subdomain(s), e.g. myhome (for myhome.duckdns.org).")
        if token is not None:
            if not re.fullmatch(r"[0-9a-f-]{36}", token.strip()):
                raise PublishError("That doesn't look like a DuckDNS token.")
            _set_secret("duckdns_token", token.strip())
        if not _secret("duckdns_token"):
            raise PublishError("Enter your DuckDNS token.")
    if provider == "cloudflare":
        if token:
            cf_check(token)
            _set_secret("cf_token", token.strip())
        if not _secret("cf_token"):
            raise PublishError("Enter a Cloudflare API token (Zone · DNS · Edit).")
        if not names or not all(HOST_RE.fullmatch(h) for h in names):
            raise PublishError("Enter the full hostname(s) to keep updated, e.g. home.example.com.")
    c = get_config()
    c["ddns"] = {"provider": provider, "hostnames": names, "last_ip": None, "last_update": None, "last_error": None} if provider else {}
    _save_config(c)
    if provider:
        ddns_tick(force=True)
    return get_config()["ddns"]


def _ddns_cloudflare(names: list[str], ip: str) -> None:
    zones = _cf("GET", "/zones", query={"per_page": 50, "status": "active"}) or []
    for h in names:
        zone = max((z for z in zones if h == z["name"] or h.endswith("." + z["name"])), key=lambda z: len(z["name"]), default=None)
        if not zone:
            raise PublishError(f"{h} isn't in a domain this Cloudflare token can manage.")
        recs = _cf("GET", f"/zones/{zone['id']}/dns_records", query={"name": h, "type": "A"}) or []
        body = {"type": "A", "name": h, "content": ip, "proxied": False, "ttl": 300, "comment": "SysAdmin.IA dynamic DNS"}
        if recs:
            if recs[0].get("content") != ip:
                _cf("PATCH", f"/zones/{zone['id']}/dns_records/{recs[0]['id']}", body)
        else:
            _cf("POST", f"/zones/{zone['id']}/dns_records", body)


def _ddns_duckdns(names: list[str], ip: str) -> None:
    q = urllib.parse.urlencode({"domains": ",".join(names), "token": _secret("duckdns_token"), "ip": ip})
    try:
        with urllib.request.urlopen(f"https://www.duckdns.org/update?{q}", timeout=15) as r:
            ok = r.read(16).decode().strip()
    except (urllib.error.URLError, OSError) as e:
        raise PublishError(f"Can't reach DuckDNS: {getattr(e, 'reason', e)}")
    if ok != "OK":
        raise PublishError("DuckDNS refused the update (check the token and subdomains).")


def ddns_tick(force: bool = False) -> None:
    c = get_config()
    d = c.get("ddns") or {}
    if not d.get("provider"):
        return
    ip = public_ip(max_age=0 if force else 300)
    if not ip or (ip == d.get("last_ip") and not force and time.time() - (d.get("last_update") or 0) < 86400):
        return
    try:
        (_ddns_cloudflare if d["provider"] == "cloudflare" else _ddns_duckdns)(d["hostnames"], ip)
        d.update(last_ip=ip, last_update=time.time(), last_error=None)
    except PublishError as e:
        d.update(last_error=str(e), last_update=time.time())
    c["ddns"] = d
    _save_config(c)


# ---------------------------------------------------------------- overview

def status(check_public: bool = False) -> dict:
    c = get_config()
    pub = public_ip() if check_public else _ip_cache["ip"]
    rip = routers.external_ip() if check_public else ""
    cf_c, caddy_c = (_container(CF_CONTAINER), _container(CADDY_CONTAINER)) if _docker_ok() else (None, None)
    return {
        "public_ip": pub, "router_ip": rip, "nat": cgnat_state(rip, pub) if check_public else "unknown",
        "lan_ip": lan_ip(), "docker": _docker_ok(), "app_port": settings.port,
        "cloudflare": {**{k: v for k, v in (c.get("cloudflare") or {}).items() if k != "account_id"},
                       "connected": bool((c.get("cloudflare") or {}).get("tunnel_id")),
                       "container": cf_c["state"] if cf_c else None, "token_saved": bool(_secret("cf_token"))},
        "caddy": {**(c.get("caddy") or {}), "container": caddy_c["state"] if caddy_c else None},
        "ddns": {**(c.get("ddns") or {}), "duckdns_token_saved": bool(_secret("duckdns_token"))},
        "services": list_services(),
    }
