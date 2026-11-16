"""Server firewall (Network → Ports): which ports of THIS server can be reached from the network.

Uses the firewall the system already has: ufw (Debian/Ubuntu), firewalld (Fedora/RHEL/openSUSE), or,
when neither is installed, a small nftables table managed by SysAdmin.IA (`inet sysadmin_ia`, loaded at
boot by sysadmin-ia-firewall.service). Every change goes through the root helper (`firewall …`); this
module parses what the helper prints, shows which ports are actually in use and by what, and refuses
changes that would lock the admin out (the app's own ports always stay open; SSH and mDNS are opened
before ports are closed by default).

Not covered: ports that Docker publishes for containers. Docker routes them around the host's input
rules (ufw, firewalld and nftables alike), so they stay reachable whatever this page says; the UI flags
them.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

from . import db
from .config import settings


class FirewallError(Exception):
    pass


NEEDS_SETUP = "NEEDS_SETUP"
BACKENDS = {"ufw": "ufw", "firewalld": "firewalld", "nftables": "nftables (managed by SysAdmin.IA)", "none": "none"}
PORT_RE = re.compile(r"^(\d{1,5})(?:-(\d{1,5}))?$")
MDNS = (5353, "udp")

# process name (as ss shows it, at most 15 characters) → what it is
PROCS = {
    "sshd": "SSH server", "sshd-session": "SSH server", "nginx": "nginx web server", "apache2": "Apache web server",
    "httpd": "Apache web server", "caddy": "Caddy web server", "lighttpd": "lighttpd web server",
    "cupsd": "Printing (CUPS)", "avahi-daemon": "Local names (mDNS)", "systemd-resolve": "DNS resolver",
    "dnsmasq": "DNS/DHCP (dnsmasq)", "named": "DNS server (BIND)", "unbound": "DNS server (Unbound)",
    "pihole-FTL": "Pi-hole", "smbd": "File sharing (Samba)", "nmbd": "Windows names (Samba)",
    "postgres": "PostgreSQL", "mysqld": "MySQL", "mariadbd": "MariaDB", "redis-server": "Redis", "mongod": "MongoDB",
    "ollama": "Ollama (local AI)", "dockerd": "Docker", "docker-proxy": "Docker container", "containerd": "containerd",
    "chronyd": "Time sync (chrony)", "ntpd": "Time sync (NTP)", "systemd-timesyn": "Time sync", "rpcbind": "NFS (rpcbind)",
    "rpc.statd": "NFS", "rpc.mountd": "NFS", "dhclient": "DHCP client", "NetworkManager": "Network manager",
    "systemd-network": "Network (DHCP client)", "cloudflared": "Cloudflare Tunnel", "minidlnad": "Media server (DLNA)",
    "Plex Media Serv": "Plex", "jellyfin": "Jellyfin", "xrdp": "Remote desktop (xrdp)", "xrdp-sesman": "Remote desktop (xrdp)",
    "vsftpd": "FTP server", "proftpd": "FTP server", "exim4": "Mail server (Exim)", "master": "Mail server (Postfix)",
    "dovecot": "Mail server (Dovecot)", "tailscaled": "Tailscale", "cockpit-ws": "Cockpit", "webmin": "Webmin",
    "snmpd": "SNMP", "rsync": "rsync", "transmission-da": "Transmission", "syncthing": "Syncthing",
    "node": "Node.js app", "java": "Java app", "mosquitto": "MQTT broker", "zabbix_agentd": "Zabbix agent",
    "wsdd": "Windows discovery (wsdd)", "wsdd2": "Windows discovery (wsdd)", "kdeconnectd": "KDE Connect",
}


# ---------------------------------------------------------------- parsing what the helper prints
def ports_from(spec: str) -> list[list]:
    """'22/tcp', '80,443/tcp|53/udp', '8000:8010/tcp', '8000-8010/tcp 5353/udp', '80' → [[lo, hi, proto]]."""
    out = []
    for grp in re.split(r"[|\s]+", (spec or "").strip()):
        if not grp:
            continue
        nums, _, proto = grp.partition("/")
        proto = proto.lower() or "any"
        for n in nums.split(","):
            m = re.fullmatch(r"(\d+)(?:[:-](\d+))?", n)
            if m:
                out.append([int(m[1]), int(m[2] or m[1]), proto])
    return out


def ports_text(ports: list[list]) -> str:
    return ", ".join((f"{lo}" if lo == hi else f"{lo}–{hi}") + ("" if proto == "any" else f"/{proto}") for lo, hi, proto in ports)


def _anywhere(src: str) -> bool:
    return src in ("", "any", "Anywhere", "0.0.0.0/0", "::/0")


def parse_ufw_rule(line: str, apps: dict[str, str]) -> dict | None:
    """One line of `ufw show added` → rule (None for outgoing/route/source-port rules)."""
    try:
        t = shlex.split(line)
    except ValueError:
        return None
    if not t or t[0] != "ufw":
        return None
    t = t[1:]
    comment = ""
    if "comment" in t:
        i = t.index("comment")
        comment = " ".join(t[i + 1:])
        t = t[:i]
    if not t or t[0] == "route" or t[0] not in ("allow", "deny", "reject", "limit"):
        return None
    action, t = t[0], t[1:]
    if t and t[0] in ("in", "out"):
        if t[0] == "out":
            return None
        t = t[1:]
        if t and t[0] == "on":
            t = t[2:]
    src, app, ports = "any", None, []
    if t and t[0] in ("from", "to", "proto"):
        side, sides, proto = None, {"from": {}, "to": {}}, "any"
        i = 0
        while i < len(t):
            k, v = t[i], (t[i + 1] if i + 1 < len(t) else "")
            if k in ("from", "to"):
                side = k
                sides[k]["addr"] = v
                i += 2
            elif k in ("port", "app") and side:
                sides[side][k] = v
                i += 2
            elif k == "proto":
                proto = v
                i += 2
            else:
                i += 1
        if sides["from"].get("port") or sides["from"].get("app"):
            return None
        src = sides["from"].get("addr", "any")
        to = sides["to"]
        if to.get("app"):
            app = to["app"]
            ports = ports_from(apps.get(app, ""))
        elif to.get("port"):
            ports = ports_from(to["port"] + ("" if proto == "any" else f"/{proto}"))
        else:
            ports = [[1, 65535, proto]]
    elif t:
        if re.fullmatch(r"[\d,:]+(/\w+)?", t[0]):
            ports = ports_from(t[0])
        else:
            app = t[0]
            ports = ports_from(apps.get(app, ""))
    else:
        return None
    return {"id": line, "action": "allow" if action in ("allow", "limit") else "deny", "limit": action == "limit",
            "app": app, "label": app or ports_text(ports), "ports": ports,
            "source": "Anywhere" if _anywhere(src) else src, "comment": comment, "removable": True}


def parse_firewalld(text: str, apps: dict[str, str]) -> tuple[str, list[dict]]:
    """`firewall-cmd --list-all` → (target, rules)."""
    target, services, ports, rich, in_rich = "default", [], [], [], False
    for raw in text.splitlines():
        line = raw.strip()
        if in_rich and line and ":" not in line.split(" ", 1)[0]:
            rich.append(line)
            continue
        in_rich = False
        key, _, val = line.partition(":")
        val = val.strip()
        if key == "target":
            target = val
        elif key == "services":
            services = val.split()
        elif key == "ports":
            ports = val.split()
        elif key == "rich rules":
            in_rich = True
            if val:
                rich.append(val)
    rules = []
    for s in services:
        p = ports_from(apps.get(s, ""))
        rules.append({"id": f"service:{s}", "action": "allow", "limit": False, "app": s, "label": s, "ports": p,
                      "source": "Anywhere", "comment": "", "removable": True})
    for p in ports:
        pp = ports_from(p)
        rules.append({"id": f"port:{p}", "action": "allow", "limit": False, "app": None, "label": ports_text(pp), "ports": pp,
                      "source": "Anywhere", "comment": "", "removable": True})
    for r in rich:
        m_port = re.search(r'port port="?([\d-]+)"? protocol="?(\w+)"?', r)
        m_src = re.search(r'source address="?([^" ]+)"?', r)
        m_svc = re.search(r'service name="?([\w.+-]+)"?', r)
        act = "allow" if re.search(r"\baccept\b", r) else "deny"
        pp = ports_from(f"{m_port[1]}/{m_port[2]}") if m_port else ports_from(apps.get(m_svc[1], "")) if m_svc else []
        if not pp and not m_svc:
            continue
        rules.append({"id": None, "action": act, "limit": False, "app": m_svc[1] if m_svc else None,
                      "label": (m_svc[1] if m_svc else ports_text(pp)), "ports": pp,
                      "source": m_src[1] if m_src else "Anywhere", "comment": "Rich rule (edit it with firewall-cmd)", "removable": False})
    return target, rules


def parse_nft_conf(lines: list[str]) -> list[dict]:
    rules = []
    for line in lines:
        m = re.fullmatch(r"open (\d+(?:-\d+)?) (tcp|udp)", line.strip())
        if m:
            pp = ports_from(f"{m[1]}/{m[2]}")
            rules.append({"id": f"{m[1]}/{m[2]}", "action": "allow", "limit": False, "app": None, "label": ports_text(pp),
                          "ports": pp, "source": "Anywhere", "comment": "", "removable": True})
    return rules


def _scope(addr: str) -> str:
    if addr in ("*", "0.0.0.0", "::", ""):
        return "all"
    if addr.startswith("127.") or addr == "::1" or addr.startswith("::ffff:127."):
        return "local"
    if addr.lower().startswith("fe80"):
        return "link"
    return "address"


def parse_ss(text: str) -> list[dict]:
    """`ss -H -tulpn` → one entry per (port, proto) with every address and process listening on it."""
    found: dict[tuple, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] not in ("tcp", "udp"):
            continue
        proto, state, local = parts[0], parts[1], parts[4]
        if proto == "tcp" and state != "LISTEN":
            continue
        if proto == "udp" and state not in ("UNCONN", "LISTEN"):
            continue
        addr, _, port = local.rpartition(":")
        if not port.isdigit():
            continue
        addr = addr.strip("[]").split("%")[0]
        procs = re.findall(r'\("([^"]+)",pid=(\d+)', " ".join(parts[5:]))
        e = found.setdefault((int(port), proto), {"port": int(port), "proto": proto, "addresses": [], "scopes": [], "processes": [], "pids": []})
        if addr not in e["addresses"]:
            e["addresses"].append(addr)
        sc = _scope(addr)
        if sc not in e["scopes"]:
            e["scopes"].append(sc)
        for name, pid in procs:
            if name not in e["processes"]:
                e["processes"].append(name)
            if int(pid) not in e["pids"]:
                e["pids"].append(int(pid))
    return sorted(found.values(), key=lambda e: (e["port"], e["proto"]))


def parse_status(raw: str) -> dict:
    """Split the helper's `firewall status` output: key=value header, then @@rules / @@apps / @@listening."""
    out = {"head": {}, "rules": [], "apps": {}, "listening": ""}
    section = None
    listening = []
    for line in raw.splitlines():
        if line.startswith("@@"):
            section = line[2:].strip()
            continue
        if section is None:
            k, _, v = line.partition("=")
            if k.strip():
                out["head"][k.strip()] = v.strip()
        elif section == "rules":
            out["rules"].append(line)
        elif section == "apps":
            name, _, ports = line.partition("\t")
            if name:
                out["apps"][name] = ports.strip()
        elif section == "listening":
            listening.append(line)
    out["listening"] = "\n".join(listening)
    return out


# ---------------------------------------------------------------- the picture shown in the UI
def _covers(rule: dict, port: int, proto: str) -> bool:
    return any(lo <= port <= hi and (p in ("any", proto)) for lo, hi, p in rule["ports"])


def app_ports() -> list[int]:
    return sorted({p for p, _tls in settings.listeners()})


def _describe(e: dict, docker_ports: dict, own_pid: int) -> dict:
    names = []
    for name in e["processes"]:
        if own_pid in e["pids"] and name.startswith(("python", "uvicorn")):
            names.append("SysAdmin.IA")
        elif name == "docker-proxy" and (e["port"], e["proto"]) in docker_ports:
            continue
        else:
            names.append(PROCS.get(name, name))
    container = docker_ports.get((e["port"], e["proto"]))
    if container:
        names.insert(0, f"Container {container}")
    if e["port"] in app_ports() and e["proto"] == "tcp" and "SysAdmin.IA" not in names:
        names.insert(0, "SysAdmin.IA")
    # DHCP clients only receive replies to this server's own requests (every firewall lets those in)
    dhcp_client = e["proto"] == "udp" and e["port"] in (68, 546)
    return {**e, "used_by": list(dict.fromkeys(names)), "docker": bool(container) or "docker-proxy" in e["processes"],
            "reachable": not dhcp_client and any(s != "local" for s in e["scopes"])}


def build(parsed: dict, docker_ports: dict | None = None, notes: dict | None = None, own_pid: int | None = None) -> dict:
    """Everything the Ports tab shows, from the helper's parsed output."""
    head = parsed["head"]
    backend = head.get("backend", "none")
    active = head.get("active") == "yes"
    apps = parsed["apps"]
    notes = notes or {}
    if backend == "ufw":
        rules = [r for r in (parse_ufw_rule(line, apps) for line in parsed["rules"]) if r]
        default_closed = head.get("default", "DROP").upper() != "ACCEPT"
    elif backend == "firewalld":
        target, rules = parse_firewalld("\n".join(parsed["rules"]), apps)
        default_closed = target.upper() not in ("ACCEPT",)
    elif backend == "nftables":
        rules = parse_nft_conf(parsed["rules"])
        default_closed = head.get("default", "drop") != "accept"
    else:
        rules, default_closed = [], False
    docker_ports = docker_ports or {}
    own_pid = own_pid or os.getpid()
    listening = [_describe(e, docker_ports, own_pid) for e in parse_ss(parsed["listening"])]
    # Docker-published ports that ss doesn't show (userland proxy turned off)
    seen = {(e["port"], e["proto"]) for e in listening}
    for (port, proto), name in sorted(docker_ports.items()):
        if (port, proto) not in seen:
            listening.append({"port": port, "proto": proto, "addresses": ["*"], "scopes": ["all"], "processes": [], "pids": [],
                              "used_by": [f"Container {name}"], "docker": True, "reachable": True})
    listening.sort(key=lambda e: (e["port"], e["proto"]))
    ssh = sorted({e["port"] for e in listening if e["proto"] == "tcp" and any(p.startswith("sshd") for p in e["processes"])})
    mine = app_ports()
    for r in rules:
        r["in_use"] = [{"port": e["port"], "proto": e["proto"], "used_by": e["used_by"], "docker": e["docker"]}
                       for e in listening if e["reachable"] and _covers(r, e["port"], e["proto"])]
        r["note"] = notes.get(r["id"] or "", "") or (r["comment"] if r["comment"] and r["comment"] != "SysAdmin.IA" else "")
        r["protected"] = None
        if r["action"] == "allow" and _anywhere_src(r) and any(_covers(r, p, "tcp") for p in mine):
            r["protected"] = "SysAdmin.IA itself uses this port: closing it would lock you out of this page."
        r["ssh"] = any(_covers(r, p, "tcp") for p in ssh) and r["action"] == "allow"
    for e in listening:
        allow = [r for r in rules if r["action"] == "allow" and _covers(r, e["port"], e["proto"])]
        deny = [r for r in rules if r["action"] == "deny" and _covers(r, e["port"], e["proto"])]
        if not e["reachable"]:
            e["state"] = "local"
        elif e["docker"]:
            e["state"] = "docker"
        elif not active:
            e["state"] = "open"
        elif any(_anywhere_src(r) for r in allow):
            e["state"] = "open"
        elif allow:
            e["state"] = "limited"
        elif deny or default_closed:
            e["state"] = "closed"
        else:
            e["state"] = "open"
    return {"backend": backend, "backend_label": BACKENDS.get(backend, backend), "active": active,
            "default_closed": default_closed, "zone": head.get("zone"), "rules": rules, "listening": listening,
            "app_ports": mine, "ssh_ports": ssh}


def _anywhere_src(r: dict) -> bool:
    return _anywhere(r["source"])


# ---------------------------------------------------------------- helper calls
def _helper(*args: str, timeout: int = 60) -> str:
    from . import server_name
    if not server_name.helper_supports("firewall"):
        raise FirewallError(NEEDS_SETUP)
    try:
        return server_name.run_helper("firewall", *args, timeout=timeout)
    except RuntimeError as e:
        raise FirewallError(str(e))


def _notes() -> dict:
    try:
        return json.loads(db.get_setting("firewall_notes") or "{}")
    except ValueError:
        return {}


def _docker_ports() -> dict:
    """(host port, proto) → container name, for ports Docker publishes on all addresses."""
    try:
        from . import containers
        if not containers.available():
            return {}
        out = {}
        for c in containers.list_containers():
            if c.get("state") != "running":
                continue
            for p in c.get("ports") or []:
                if p.get("host") and (p.get("ip") or "0.0.0.0") not in ("127.0.0.1", "::1"):
                    out[(int(p["host"]), p.get("proto") or "tcp")] = c["name"]
        return out
    except Exception:  # noqa: BLE001 - Docker is optional here
        return {}


def status() -> dict:
    linux = sys.platform.startswith("linux")
    base = {"linux": linux, "available": False, "setup_command": None, "error": None, "backend": "none",
            "backend_label": "none", "active": False, "default_closed": False, "rules": [], "listening": [],
            "app_ports": app_ports(), "ssh_ports": []}
    if not linux:
        return base
    try:
        raw = _helper("status")
    except FirewallError as e:
        if str(e) == NEEDS_SETUP:
            from . import server_name
            base["setup_command"] = server_name.setup_command()
        else:
            base["error"] = str(e)
        return base
    return {**base, **build(parse_status(raw), _docker_ports(), _notes()), "available": True}


def _check_port(port: str) -> str:
    port = str(port or "").strip().replace(" ", "").replace("–", "-").replace(":", "-")
    m = PORT_RE.fullmatch(port)
    if not m:
        raise FirewallError("Enter a port (1–65535) or a range such as 8000-8010.")
    lo, hi = int(m[1]), int(m[2] or m[1])
    if not (1 <= lo <= hi <= 65535):
        raise FirewallError("Ports must be between 1 and 65535, the first one lower.")
    return f"{lo}" if lo == hi else f"{lo}-{hi}"


def _protos(proto: str) -> list[str]:
    proto = (proto or "tcp").lower()
    if proto == "both":
        return ["tcp", "udp"]
    if proto not in ("tcp", "udp"):
        raise FirewallError("The protocol must be TCP, UDP or both.")
    return [proto]


def _needed(st: dict) -> list[str]:
    """Ports opened before closing ports by default: the app, SSH, and mDNS (server-name.local)."""
    want = [(p, "tcp") for p in st["app_ports"]] + [(p, "tcp") for p in (st["ssh_ports"] or [22])] + [MDNS]
    have = st["rules"]
    out = []
    for port, proto in want:
        if not any(r["action"] == "allow" and _anywhere_src(r) and _covers(r, port, proto) for r in have):
            spec = f"{port}/{proto}"
            if spec not in out:
                out.append(spec)
    return out


def _require(st: dict) -> None:
    if not st["linux"]:
        raise FirewallError("The firewall can only be managed when SysAdmin.IA runs on Linux.")
    if st["setup_command"]:
        raise FirewallError("This needs a one-time setup as root. On the server, run:\n  " + st["setup_command"])
    if st["error"]:
        raise FirewallError(st["error"])
    if st["backend"] == "none":
        raise FirewallError("No firewall tool is installed. Install ufw (sudo apt install ufw) or nftables, then reload this page.")


def open_port(port: str, proto: str = "tcp", note: str = "") -> dict:
    st = status()
    _require(st)
    spec = _check_port(port)
    note = (note or "").strip()[:80]
    notes = _notes()
    lo, _, hi = spec.partition("-")
    lo, hi = int(lo), int(hi or lo)
    protos = _protos(proto)
    # a block rule for the same port would win (ufw checks rules in order): drop it first
    for r in st["rules"]:
        if r["action"] != "deny" or not any(a <= hi and lo <= b and p in ("any", *protos) for a, b, p in r["ports"]):
            continue
        same = all(a == lo and b == hi for a, b, _p in r["ports"]) and _anywhere_src(r)
        if not (same and r["removable"]):
            raise FirewallError(f"A rule blocks {r['label']}. Delete it from the table first, then open the port.")
        _helper("remove", r["id"])
    for p in protos:
        _helper("open", spec, p)
        if note:
            notes[_rule_key(st["backend"], spec, p)] = note
    if note:
        db.set_setting("firewall_notes", json.dumps(notes))
    return status()


def _rule_key(backend: str, spec: str, proto: str) -> str:
    """The id the rule will have once added (for its note)."""
    if backend == "ufw":
        return f"ufw allow {spec.replace('-', ':')}/{proto} comment 'SysAdmin.IA'"
    if backend == "firewalld":
        return f"port:{spec}/{proto}"
    return f"{spec}/{proto}"


def remove_rule(rule_id: str) -> dict:
    st = status()
    _require(st)
    rule = next((r for r in st["rules"] if r["id"] == rule_id), None)
    if not rule:
        raise FirewallError("That rule doesn't exist any more. Reload the page.")
    if not rule["removable"]:
        raise FirewallError("This rule was made outside SysAdmin.IA in a form it can't edit. Change it with the firewall's own tools.")
    if rule["protected"] and st["active"]:
        others = [r for r in st["rules"] if r is not rule and r["action"] == "allow" and _anywhere_src(r)]
        if not all(any(_covers(o, p, "tcp") for o in others) for p in st["app_ports"] if _covers(rule, p, "tcp")):
            raise FirewallError(rule["protected"])
    _helper("remove", rule_id)
    notes = _notes()
    if notes.pop(rule_id, None) is not None:
        db.set_setting("firewall_notes", json.dumps(notes))
    return status()


def set_default(closed: bool) -> dict:
    st = status()
    _require(st)
    if closed:
        _helper("default", "deny", *_needed(st))
    else:
        _helper("default", "allow")
    return status()


def set_enabled(on: bool) -> dict:
    st = status()
    _require(st)
    if on:
        _helper("enable", *_needed(st))
    else:
        _helper("disable")
    return status()
