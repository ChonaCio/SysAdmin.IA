"""Talk to the LAN's router: DHCP (leases, reserved addresses, pool, lease time) and port forwarding.

Routers have no common configuration standard, so there is one adapter per family:
  upnp      – UPnP Internet Gateway Device: almost every home router. Port forwarding and the public
              (WAN) address only; it can't change DHCP. Must be enabled in the router (often is).
  openwrt   – OpenWrt / LuCI over its ubus JSON-RPC API (/ubus): DHCP leases, reserved addresses
              (dhcp "host" sections), pool & lease time, port forwards (firewall "redirect" sections).
  mikrotik  – MikroTik RouterOS 7 REST API (/rest, needs the www or www-ssl service): the same.
  manual    – no API: the UI links to the router's admin page.
Other systems (OPNsense/pfSense, UniFi, FRITZ!Box TR-064…) can be added as further adapters.

The connection (type, address, user) is stored in app_settings["router"], the password in
app_settings["router_secret"]; the password is never sent back to browsers.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from . import db, network

TYPES = {
    "upnp": {"label": "Automatic (UPnP)", "auth": False,
             "about": "Works with most home routers. Can open ports (port forwarding) and read the public address, "
                      "but can't change DHCP. UPnP must be enabled in the router."},
    "openwrt": {"label": "OpenWrt", "auth": True,
                "about": "Full control through OpenWrt's own API (needs LuCI, which is installed by default): "
                         "DHCP leases, reserved addresses, address range, lease time and port forwarding."},
    "mikrotik": {"label": "MikroTik (RouterOS 7)", "auth": True,
                 "about": "Full control through the RouterOS REST API (IP → Services: enable www or www-ssl): "
                          "DHCP leases, reserved addresses, address range, lease time and port forwarding."},
    "manual": {"label": "Other router (no API)", "auth": False,
               "about": "This router has no API SysAdmin.IA can use. Change DHCP and port forwarding in its own admin page."},
}
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
NAME_RE = re.compile(r"^[A-Za-z0-9 ._()-]{0,60}$")


class RouterError(RuntimeError):
    pass


# ---------------------------------------------------------------- configuration

def get_config() -> dict:
    try:
        c = json.loads(db.get_setting("router") or "{}")
    except ValueError:
        c = {}
    return {"type": c.get("type", ""), "address": c.get("address", ""), "username": c.get("username", ""),
            "verify_tls": bool(c.get("verify_tls", False)), "password_set": bool(db.get_setting("router_secret"))}


def save_config(type_: str, address: str, username: str = "", password: str | None = None, verify_tls: bool = False) -> dict:
    if type_ not in TYPES:
        raise RouterError("Choose the kind of router.")
    address = (address or "").strip().rstrip("/")
    if type_ in ("openwrt", "mikrotik"):
        if not re.fullmatch(r"(https?://)?[A-Za-z0-9.\-\[\]:]+(:\d+)?", address):
            raise RouterError("Enter the router's address, e.g. 192.168.1.1 or https://192.168.1.1.")
        if not address.startswith(("http://", "https://")):
            address = ("https://" if type_ == "mikrotik" else "http://") + address
    if len(username) > 64 or (username and not re.fullmatch(r"[A-Za-z0-9._@-]+", username)):
        raise RouterError("Invalid user name.")
    db.set_setting("router", json.dumps({"type": type_, "address": address, "username": username.strip(),
                                         "verify_tls": bool(verify_tls)}))
    if password is not None:
        db.set_setting("router_secret", password or None)
    return get_config()


def forget_config() -> None:
    db.set_setting("router", None)
    db.set_setting("router_secret", None)


def adapter(cfg: dict | None = None):
    cfg = cfg or get_config()
    pw = db.get_setting("router_secret") or ""
    t = cfg.get("type")
    if t == "upnp":
        return UpnpRouter(cfg.get("address") or "")
    if t == "openwrt":
        return OpenWrtRouter(cfg["address"], cfg.get("username") or "root", pw, cfg.get("verify_tls"))
    if t == "mikrotik":
        return MikroTikRouter(cfg["address"], cfg.get("username") or "admin", pw, cfg.get("verify_tls"))
    if t == "manual":
        return ManualRouter()
    raise RouterError("No router is set up yet.")


def detect() -> dict:
    """Suggest how to talk to this network's router: gateway address, UPnP gateway found, admin page hints."""
    gw = network.gateway()
    out = {"gateway": gw, "upnp": None, "suggested": "manual", "admin_url": f"http://{gw}/" if gw else None, "hint": ""}
    try:
        igd = discover_igd(timeout=3)
        out["upnp"] = {k: igd.get(k) for k in ("friendlyName", "manufacturer", "modelName", "location")}
        out["suggested"] = "upnp"
    except RouterError:
        pass
    if gw:
        for url in (f"http://{gw}/", f"https://{gw}/"):
            info = network.http_info(gw, 443 if url.startswith("https") else 80, url.startswith("https"))
            text = " ".join(info.values()).lower()
            if "luci" in text or "openwrt" in text:
                out.update(suggested="openwrt", hint="Looks like OpenWrt (LuCI).")
                break
            if "routeros" in text or "mikrotik" in text or "webfig" in text:
                out.update(suggested="mikrotik", hint="Looks like MikroTik RouterOS.")
                break
    return out


def _ctx(verify: bool):
    if verify:
        return None
    c = ssl.create_default_context()
    c.check_hostname, c.verify_mode = False, ssl.CERT_NONE
    return c


def _http(method: str, url: str, body=None, headers: dict | None = None, verify: bool = False, timeout: float = 12):
    data = None
    h = dict(headers or {})
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx(verify) if url.startswith("https") else None) as r:
            return r.status, r.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        return e.code, e.read(65536)
    except (urllib.error.URLError, OSError) as e:
        raise RouterError(f"Can't reach the router at {urllib.parse.urlsplit(url).netloc}: {getattr(e, 'reason', e)}")


def _check_ip(ip: str) -> str:
    try:
        a = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        raise RouterError(f"“{ip}” isn't an IPv4 address.")
    if a.version != 4 or not a.is_private:
        raise RouterError("Use a private (LAN) address such as 192.168.1.50.")
    return str(a)


def _check_port(p) -> int:
    try:
        p = int(p)
    except (TypeError, ValueError):
        raise RouterError("Ports must be numbers.")
    if not 1 <= p <= 65535:
        raise RouterError("Ports must be between 1 and 65535.")
    return p


def _check_proto(p: str) -> str:
    p = (p or "tcp").lower()
    if p not in ("tcp", "udp", "both"):
        raise RouterError("Protocol: TCP, UDP or both.")
    return p


def _check_mac(mac: str) -> str:
    mac = (mac or "").strip().lower().replace("-", ":")
    if not MAC_RE.fullmatch(mac):
        raise RouterError("Invalid MAC address.")
    return mac


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.fullmatch(name):
        raise RouterError("Names: letters, digits, spaces and . _ - ( ) (at most 60).")
    return name


BLOCK_TAG = "SysAdmin.IA block"   # name/comment prefix of the router rules this app adds to block a device


class Router:
    type = ""
    caps: set[str] = set()   # leases, static, dhcp_settings, forwards, external_ip, block, kick

    def info(self) -> dict:
        raise NotImplementedError

    def status(self) -> dict:
        i = self.info()
        return {"type": self.type, "label": TYPES[self.type]["label"], "caps": sorted(self.caps), **i}


class ManualRouter(Router):
    type = "manual"

    def info(self) -> dict:
        gw = network.gateway()
        return {"model": "", "admin_url": f"http://{gw}/" if gw else None}


# ---------------------------------------------------------------- UPnP IGD

def _soap_body(action: str, service: str, args: dict) -> bytes:
    a = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
    return (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{service}">{a}</u:{action}></s:Body></s:Envelope>').encode()


def _xml_children(root, tag: str) -> dict:
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == tag:
            return {c.tag.rsplit("}", 1)[-1]: (c.text or "") for c in el}
    return {}


def parse_igd(xml: bytes, location: str) -> dict:
    """Control URL and service type of the WAN connection service in an IGD description."""
    root = ET.fromstring(xml)
    base = network._xml_text(root, "URLBase") or location
    for svc in root.iter():
        if svc.tag.rsplit("}", 1)[-1] != "service":
            continue
        d = {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in svc}
        st = d.get("serviceType", "")
        if "WANIPConnection" in st or "WANPPPConnection" in st:
            return {"service": st, "control": urllib.parse.urljoin(base, d.get("controlURL", "")),
                    **{t: network._xml_text(root, t) for t in ("friendlyName", "manufacturer", "modelName")}, "location": location}
    raise RouterError("The router answers UPnP but has no port-forwarding service.")


def discover_igd(timeout: float = 3.0, location: str = "") -> dict:
    locs = [location] if location else []
    if not locs:
        for st in ("urn:schemas-upnp-org:device:InternetGatewayDevice:2", "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
                   "urn:schemas-upnp-org:service:WANIPConnection:1"):
            locs += [r["location"] for r in network.ssdp_search(timeout, st) if r.get("location")]
            if locs:
                break
    for loc in dict.fromkeys(locs):
        try:
            with urllib.request.urlopen(loc, timeout=5) as r:
                return parse_igd(r.read(262144), loc)
        except (OSError, ET.ParseError, urllib.error.URLError):
            continue
    raise RouterError("No UPnP router found. Enable UPnP (sometimes called “UPnP IGD” or “NAT-PMP/UPnP”) in the router, "
                      "or choose its type if it has an API.")


class UpnpRouter(Router):
    type = "upnp"
    caps = {"forwards", "external_ip"}

    def __init__(self, location: str = "") -> None:
        self.igd = discover_igd(location=location)

    def _call(self, action: str, args: dict | None = None) -> dict:
        st, data = _http("POST", self.igd["control"], _soap_body(action, self.igd["service"], args or {}),
                         {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{self.igd["service"]}#{action}"'})
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            raise RouterError(f"Unexpected answer from the router (HTTP {st}).")
        if st >= 400:
            err = _xml_children(root, "UPnPError")
            code = err.get("errorCode", "")
            raise RouterError({"713": "NO_MORE", "718": "That port is already forwarded on the router.",
                               "714": "No such port forward.", "606": "The router refused (UPnP actions not authorized).",
                               "725": "The router only accepts permanent forwards."}.get(code, f"The router refused: {err.get('errorDescription') or code}"))
        return _xml_children(root, f"{action}Response")

    def info(self) -> dict:
        return {"model": " ".join(filter(None, [self.igd.get("manufacturer"), self.igd.get("modelName")])) or self.igd.get("friendlyName", ""),
                "external_ip": self.external_ip()}

    def external_ip(self) -> str:
        try:
            return self._call("GetExternalIPAddress").get("NewExternalIPAddress", "")
        except RouterError:
            return ""

    def forwards(self) -> list[dict]:
        out = []
        for i in range(256):
            try:
                r = self._call("GetGenericPortMappingEntry", {"NewPortMappingIndex": i})
            except RouterError as e:
                if str(e) == "NO_MORE" or i:
                    break
                raise
            out.append({"id": f"{r.get('NewProtocol', 'TCP').lower()}:{r.get('NewExternalPort')}",
                        "name": r.get("NewPortMappingDescription", ""), "proto": r.get("NewProtocol", "TCP").lower(),
                        "ext_port": int(r.get("NewExternalPort") or 0), "int_ip": r.get("NewInternalClient", ""),
                        "int_port": int(r.get("NewInternalPort") or 0), "enabled": r.get("NewEnabled", "1") in ("1", "true")})
        return out

    def add_forward(self, name: str, proto: str, ext_port: int, int_ip: str, int_port: int) -> None:
        for p in (["tcp", "udp"] if proto == "both" else [proto]):
            self._call("AddPortMapping", {"NewRemoteHost": "", "NewExternalPort": ext_port, "NewProtocol": p.upper(),
                                          "NewInternalPort": int_port, "NewInternalClient": int_ip, "NewEnabled": 1,
                                          "NewPortMappingDescription": name or "SysAdmin.IA", "NewLeaseDuration": 0})

    def remove_forward(self, fid: str) -> None:
        proto, _, port = fid.partition(":")
        self._call("DeletePortMapping", {"NewRemoteHost": "", "NewExternalPort": int(port), "NewProtocol": proto.upper()})


# ---------------------------------------------------------------- OpenWrt (ubus JSON-RPC)

def _lease_seconds(v: str) -> int | None:
    m = re.fullmatch(r"(\d+)\s*([smhdw]?)", (v or "").strip().lower())
    if not m:
        return None
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]


class OpenWrtRouter(Router):
    type = "openwrt"
    caps = {"leases", "static", "dhcp_settings", "forwards", "external_ip", "block", "kick"}
    NULL = "0" * 32

    def __init__(self, address: str, username: str, password: str, verify: bool = False) -> None:
        self.url, self.verify = address.rstrip("/") + "/ubus", verify
        r = self._rpc(self.NULL, "session", "login", {"username": username, "password": password}, login=True)
        self.sid = (r or {}).get("ubus_rpc_session")
        if not self.sid:
            raise RouterError("OpenWrt refused the user name or password.")

    def _rpc(self, sid: str, obj: str, method: str, args: dict | None = None, login: bool = False):
        st, data = _http("POST", self.url, {"jsonrpc": "2.0", "id": 1, "method": "call", "params": [sid, obj, method, args or {}]},
                         verify=self.verify)
        if st == 404:
            raise RouterError("This router has no OpenWrt API at /ubus (is LuCI installed?).")
        try:
            r = json.loads(data)
        except ValueError:
            raise RouterError(f"Unexpected answer from the router (HTTP {st}).")
        if "error" in r:
            raise RouterError(f"OpenWrt: {r['error'].get('message', 'error')}")
        code, *rest = r.get("result") or [None]
        if code == 6:
            raise RouterError("OpenWrt refused the user name or password." if login else "OpenWrt: permission denied.")
        if code not in (0, None):
            raise RouterError(f"OpenWrt: {obj}.{method} failed (code {code}).")
        return rest[0] if rest else {}

    def call(self, obj: str, method: str, args: dict | None = None):
        return self._rpc(self.sid, obj, method, args)

    def _uci(self, config: str) -> dict:
        return (self.call("uci", "get", {"config": config}) or {}).get("values", {})

    def _commit(self, config: str, service: str) -> None:
        self.call("uci", "commit", {"config": config})
        try:
            self.call("rc", "init", {"name": service, "action": "reload"})
        except RouterError:
            self.call("file", "exec", {"command": f"/etc/init.d/{service}", "params": ["reload"]})

    def info(self) -> dict:
        b = self.call("system", "board") or {}
        rel = b.get("release") or {}
        return {"model": b.get("model", ""), "version": rel.get("description") or rel.get("version", ""),
                "external_ip": self.external_ip()}

    def external_ip(self) -> str:
        try:
            st = self.call("network.interface.wan", "status") or {}
            return ((st.get("ipv4-address") or [{}])[0]).get("address", "")
        except RouterError:
            return ""

    def leases(self) -> list[dict]:
        static = {v.get("mac", "").lower(): k for k, v in self._uci("dhcp").items() if v.get(".type") == "host" and v.get("mac")}
        try:
            dyn = (self.call("luci-rpc", "getDHCPLeases") or {}).get("dhcp_leases") or []
        except RouterError:
            dyn = []
        out, seen = [], set()
        for l in dyn:
            mac = (l.get("macaddr") or "").lower()
            seen.add(mac)
            out.append({"ip": l.get("ipaddr", ""), "mac": mac, "hostname": l.get("hostname", ""),
                        "expires": l.get("expires"), "static": mac in static, "id": static.get(mac, "")})
        for k, v in self._uci("dhcp").items():
            if v.get(".type") == "host" and v.get("mac", "").lower() not in seen:
                out.append({"ip": v.get("ip", ""), "mac": v.get("mac", "").lower(), "hostname": v.get("name", ""),
                            "expires": None, "static": True, "id": k})
        return out

    def add_static(self, mac: str, ip: str, name: str) -> None:
        for k, v in self._uci("dhcp").items():
            if v.get(".type") == "host" and (v.get("mac", "").lower() == mac or v.get("ip") == ip):
                self.call("uci", "delete", {"config": "dhcp", "section": k})
        vals = {"mac": mac, "ip": ip, "dns": "1"}
        if name:
            vals["name"] = re.sub(r"[^A-Za-z0-9-]", "-", name)[:60].strip("-")
        self.call("uci", "add", {"config": "dhcp", "type": "host", "values": vals})
        self._commit("dhcp", "dnsmasq")

    def remove_static(self, sid: str) -> None:
        self.call("uci", "delete", {"config": "dhcp", "section": sid})
        self._commit("dhcp", "dnsmasq")

    def _lan(self) -> tuple[str, dict, ipaddress.IPv4Network]:
        dhcp = self._uci("dhcp")
        sec = next((k for k, v in dhcp.items() if v.get(".type") == "dhcp" and v.get("interface") == "lan"), None)
        if not sec:
            raise RouterError("No DHCP server configured for the LAN on this router.")
        lan = self._uci("network").get("lan") or {}
        addr = lan.get("ipaddr", "192.168.1.1")
        addr = addr[0] if isinstance(addr, list) else addr
        net = ipaddress.ip_network(addr if "/" in addr else f"{addr}/{lan.get('netmask', '255.255.255.0')}", strict=False)
        return sec, dhcp[sec], net

    def dhcp_settings(self) -> dict:
        sec, d, net = self._lan()
        start = int(d.get("start", 100))
        limit = int(d.get("limit", 150))
        opts = d.get("dhcp_option") or []
        dns = next((o.split(",", 1)[1] for o in opts if o.startswith("6,")), "")
        return {"network": str(net), "start": str(net.network_address + start),
                "end": str(net.network_address + start + limit - 1), "lease_time": d.get("leasetime", "12h"),
                "dns": dns, "enabled": d.get("ignore", "0") != "1"}

    def set_dhcp_settings(self, start: str, end: str, lease_time: str, dns: str) -> None:
        sec, d, net = self._lan()
        s, e = ipaddress.ip_address(start), ipaddress.ip_address(end)
        if s not in net or e not in net or e < s:
            raise RouterError(f"The range must be inside {net} and the end after the start.")
        vals = {"start": str(int(s) - int(net.network_address)), "limit": str(int(e) - int(s) + 1),
                "leasetime": lease_time}
        opts = [o for o in (d.get("dhcp_option") or []) if not o.startswith("6,")]
        if dns:
            opts.append("6," + dns.replace(" ", ""))
        vals["dhcp_option"] = opts
        self.call("uci", "set", {"config": "dhcp", "section": sec, "values": vals})
        self._commit("dhcp", "dnsmasq")

    def forwards(self) -> list[dict]:
        out = []
        for k, v in self._uci("firewall").items():
            if v.get(".type") == "redirect" and v.get("target", "DNAT") == "DNAT" and v.get("src") == "wan":
                proto = v.get("proto", "tcp udp")
                proto = " ".join(proto) if isinstance(proto, list) else proto
                out.append({"id": k, "name": v.get("name", ""), "proto": "both" if "tcp" in proto and "udp" in proto else proto,
                            "ext_port": int(str(v.get("src_dport", "0")).split("-")[0] or 0), "int_ip": v.get("dest_ip", ""),
                            "int_port": int(str(v.get("dest_port") or v.get("src_dport") or "0").split("-")[0] or 0),
                            "enabled": v.get("enabled", "1") != "0"})
        return out

    def add_forward(self, name: str, proto: str, ext_port: int, int_ip: str, int_port: int) -> None:
        self.call("uci", "add", {"config": "firewall", "type": "redirect", "values": {
            "name": name or "SysAdmin.IA", "src": "wan", "src_dport": str(ext_port), "dest": "lan", "dest_ip": int_ip,
            "dest_port": str(int_port), "proto": "tcp udp" if proto == "both" else proto, "target": "DNAT"}})
        self._commit("firewall", "firewall")

    def remove_forward(self, fid: str) -> None:
        self.call("uci", "delete", {"config": "firewall", "section": fid})
        self._commit("firewall", "firewall")

    # -- blocking: two firewall rules per device (through the router, and to the router itself: no
    #    internet, no DNS, no DHCP), named "SysAdmin.IA block <mac>"
    def blocked(self) -> list[dict]:
        out: dict[str, dict] = {}
        for k, v in self._uci("firewall").items():
            if v.get(".type") == "rule" and str(v.get("name", "")).startswith(BLOCK_TAG) and v.get("src_mac"):
                mac = str(v["src_mac"]).lower()
                d = out.setdefault(mac, {"mac": mac, "name": v["name"][len(BLOCK_TAG):].strip(" -").removeprefix(mac).strip(" ()"), "ids": []})
                d["ids"].append(k)
        return list(out.values())

    def block(self, mac: str, name: str) -> None:
        if any(b["mac"] == mac for b in self.blocked()):
            return
        label = f"{BLOCK_TAG} {mac}" + (f" ({name})" if name else "")
        base = {"name": label, "src": "lan", "src_mac": mac, "target": "REJECT", "proto": "all"}
        self.call("uci", "add", {"config": "firewall", "type": "rule", "values": {**base, "dest": "*"}})
        self.call("uci", "add", {"config": "firewall", "type": "rule", "values": base})
        self._commit("firewall", "firewall")
        try:   # also drop its Wi-Fi association so open connections end now
            self.kick(mac, ban_ms=0)
        except RouterError:
            pass

    def unblock(self, mac: str) -> None:
        b = next((x for x in self.blocked() if x["mac"] == mac), None)
        if not b:
            raise RouterError("That device isn't blocked.")
        for sid in b["ids"]:
            self.call("uci", "delete", {"config": "firewall", "section": sid})
        self._commit("firewall", "firewall")

    def _wifi_ifaces(self) -> list[str]:
        try:
            return (self.call("iwinfo", "devices") or {}).get("devices") or []
        except RouterError:
            return []

    def kick(self, mac: str, ban_ms: int = 60000) -> None:
        found = False
        for dev in self._wifi_ifaces():
            try:
                clients = (self.call(f"hostapd.{dev}", "get_clients") or {}).get("clients") or {}
            except RouterError:
                continue
            if mac in {c.lower() for c in clients}:
                self.call(f"hostapd.{dev}", "del_client", {"addr": mac, "reason": 5, "deauth": True, "ban_time": ban_ms})
                found = True
        if not found:
            raise RouterError("That device isn't connected to the router's Wi-Fi. Wired devices can't be kicked; block them instead.")


# ---------------------------------------------------------------- MikroTik RouterOS 7 (REST)

class MikroTikRouter(Router):
    type = "mikrotik"
    caps = {"leases", "static", "dhcp_settings", "forwards", "block", "kick"}
    WIFI_PATHS = ("/interface/wifi", "/interface/wireless")   # RouterOS 7 "wifi" package / legacy "wireless"

    def __init__(self, address: str, username: str, password: str, verify: bool = False) -> None:
        self.base, self.verify = address.rstrip("/") + "/rest", verify
        self.auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.get("/system/resource")

    def _req(self, method: str, path: str, body=None):
        st, data = _http(method, self.base + path, body, {"Authorization": self.auth}, self.verify)
        if st == 401:
            raise RouterError("RouterOS refused the user name or password.")
        if st == 404 and path == "/system/resource":
            raise RouterError("No RouterOS REST API here (RouterOS 7 with the www or www-ssl service is needed).")
        try:
            r = json.loads(data) if data.strip() else {}
        except ValueError:
            raise RouterError(f"Unexpected answer from the router (HTTP {st}).")
        if st >= 400:
            raise RouterError(f"RouterOS: {r.get('detail') or r.get('message') or st}")
        return r

    def get(self, path):
        return self._req("GET", path)

    def info(self) -> dict:
        r = self.get("/system/resource")
        return {"model": r.get("board-name", ""), "version": r.get("version", ""), "external_ip": ""}

    def leases(self) -> list[dict]:
        return [{"ip": l.get("address", ""), "mac": (l.get("mac-address") or "").lower(), "hostname": l.get("host-name", "") or l.get("comment", ""),
                 "expires": l.get("expires-after"), "static": l.get("dynamic") in ("false", False), "id": l.get(".id")}
                for l in self.get("/ip/dhcp-server/lease")]

    def add_static(self, mac: str, ip: str, name: str) -> None:
        for l in self.get("/ip/dhcp-server/lease"):
            if (l.get("mac-address") or "").lower() == mac or l.get("address") == ip:
                self._req("DELETE", f"/ip/dhcp-server/lease/{l['.id']}")
        self._req("PUT", "/ip/dhcp-server/lease", {"address": ip, "mac-address": mac.upper(), "comment": name or "SysAdmin.IA"})

    def remove_static(self, lid: str) -> None:
        self._req("DELETE", f"/ip/dhcp-server/lease/{urllib.parse.quote(lid, safe='*')}")

    def _server(self) -> tuple[dict, dict, dict]:
        servers = self.get("/ip/dhcp-server")
        if not servers:
            raise RouterError("No DHCP server is configured on this router.")
        srv = servers[0]
        pool = next((p for p in self.get("/ip/pool") if p.get("name") == srv.get("address-pool")), {})
        nets = self.get("/ip/dhcp-server/network")
        return srv, pool, (nets[0] if nets else {})

    def dhcp_settings(self) -> dict:
        srv, pool, net = self._server()
        start, _, end = (pool.get("ranges") or "").split(",")[0].partition("-")
        return {"network": net.get("address", ""), "start": start, "end": end or start, "lease_time": srv.get("lease-time", ""),
                "dns": net.get("dns-server", ""), "enabled": srv.get("disabled") not in ("true", True)}

    def set_dhcp_settings(self, start: str, end: str, lease_time: str, dns: str) -> None:
        srv, pool, net = self._server()
        if pool:
            self._req("PATCH", f"/ip/pool/{pool['.id']}", {"ranges": f"{start}-{end}"})
        self._req("PATCH", f"/ip/dhcp-server/{srv['.id']}", {"lease-time": lease_time})
        if net:
            self._req("PATCH", f"/ip/dhcp-server/network/{net['.id']}", {"dns-server": dns.replace(" ", "")})

    def forwards(self) -> list[dict]:
        out = []
        for r in self.get("/ip/firewall/nat"):
            if r.get("chain") == "dstnat" and r.get("action") == "dst-nat":
                ext = str(r.get("dst-port") or "0").split("-")[0].split(",")[0]
                out.append({"id": r.get(".id"), "name": r.get("comment", ""), "proto": r.get("protocol", "tcp"),
                            "ext_port": int(ext or 0), "int_ip": r.get("to-addresses", ""),
                            "int_port": int(str(r.get("to-ports") or ext or 0).split("-")[0]),
                            "enabled": r.get("disabled") not in ("true", True)})
        return out

    def add_forward(self, name: str, proto: str, ext_port: int, int_ip: str, int_port: int) -> None:
        for p in (["tcp", "udp"] if proto == "both" else [proto]):
            self._req("PUT", "/ip/firewall/nat", {"chain": "dstnat", "action": "dst-nat", "protocol": p, "dst-port": str(ext_port),
                                                  "in-interface-list": "WAN", "to-addresses": int_ip, "to-ports": str(int_port),
                                                  "comment": name or "SysAdmin.IA"})

    def remove_forward(self, fid: str) -> None:
        self._req("DELETE", f"/ip/firewall/nat/{urllib.parse.quote(fid, safe='*')}")

    # -- blocking: firewall drop rules (forward + input, placed first) and a Wi-Fi access-list reject
    def blocked(self) -> list[dict]:
        out: dict[str, dict] = {}
        for r in self.get("/ip/firewall/filter"):
            if str(r.get("comment", "")).startswith(BLOCK_TAG) and r.get("src-mac-address"):
                mac = str(r["src-mac-address"]).lower()
                d = out.setdefault(mac, {"mac": mac, "name": str(r["comment"])[len(BLOCK_TAG):].strip().removeprefix(mac).strip(" ()"), "ids": []})
                d["ids"].append(r[".id"])
        return list(out.values())

    def block(self, mac: str, name: str) -> None:
        if any(b["mac"] == mac for b in self.blocked()):
            return
        comment = f"{BLOCK_TAG} {mac}" + (f" ({name})" if name else "")
        rules = self.get("/ip/firewall/filter")
        for chain in ("forward", "input"):
            first = next((r[".id"] for r in rules if r.get("chain") == chain), None)
            body = {"chain": chain, "src-mac-address": mac.upper(), "action": "drop", "comment": comment}
            if first:
                body["place-before"] = first
            self._req("PUT", "/ip/firewall/filter", body)
        for base in self.WIFI_PATHS:   # keep it off the Wi-Fi too (whichever wireless package is installed)
            try:
                body = {"mac-address": mac.upper(), "comment": comment}
                body.update({"action": "reject"} if base.endswith("wifi") else {"authentication": "no", "forwarding": "no"})
                self._req("PUT", f"{base}/access-list", body)
            except RouterError:
                continue
        try:
            self.kick(mac)
        except RouterError:
            pass

    def unblock(self, mac: str) -> None:
        b = next((x for x in self.blocked() if x["mac"] == mac), None)
        if not b:
            raise RouterError("That device isn't blocked.")
        for rid in b["ids"]:
            self._req("DELETE", f"/ip/firewall/filter/{urllib.parse.quote(rid, safe='*')}")
        for base in self.WIFI_PATHS:
            try:
                for a in self.get(f"{base}/access-list"):
                    if str(a.get("comment", "")).startswith(BLOCK_TAG) and str(a.get("mac-address", "")).lower() == mac:
                        self._req("DELETE", f"{base}/access-list/{urllib.parse.quote(a['.id'], safe='*')}")
            except RouterError:
                continue

    def kick(self, mac: str, ban_ms: int = 60000) -> None:
        found = False
        for base in self.WIFI_PATHS:
            try:
                table = self.get(f"{base}/registration-table")
            except RouterError:
                continue
            for r in table:
                if str(r.get("mac-address", "")).lower() == mac:
                    self._req("DELETE", f"{base}/registration-table/{urllib.parse.quote(r['.id'], safe='*')}")
                    found = True
        if not found:
            raise RouterError("That device isn't connected to the router's Wi-Fi. Wired devices can't be kicked; block them instead.")


# ---------------------------------------------------------------- validated operations (used by the API)

def status() -> dict:
    cfg = get_config()
    out = {"config": cfg, "types": TYPES, "connected": False, "error": None}
    if not cfg["type"]:
        return out
    try:
        out.update(adapter(cfg).status(), connected=True)
    except RouterError as e:
        out["error"] = str(e)
    return out


def leases() -> list[dict]:
    r = adapter()
    if "leases" not in r.caps:
        raise RouterError("This router type can't list DHCP leases.")
    return r.leases()


def add_static(mac: str, ip: str, name: str = "") -> None:
    r = adapter()
    if "static" not in r.caps:
        raise RouterError("This router type can't reserve addresses.")
    r.add_static(_check_mac(mac), _check_ip(ip), _check_name(name))


def remove_static(lid: str) -> None:
    r = adapter()
    if "static" not in r.caps:
        raise RouterError("This router type can't reserve addresses.")
    r.remove_static(str(lid))


def dhcp_settings() -> dict:
    r = adapter()
    if "dhcp_settings" not in r.caps:
        raise RouterError("This router type can't read the DHCP settings.")
    return r.dhcp_settings()


def set_dhcp_settings(start: str, end: str, lease_time: str, dns: str) -> dict:
    r = adapter()
    if "dhcp_settings" not in r.caps:
        raise RouterError("This router type can't change the DHCP settings.")
    start, end = _check_ip(start), _check_ip(end)
    secs = _lease_seconds(lease_time)
    if secs is None or not 120 <= secs <= 365 * 86400:
        raise RouterError("Lease time: a number with m, h or d (e.g. 12h), between 2 minutes and a year.")
    dns_list = [x for x in re.split(r"[\s,]+", dns or "") if x]
    for x in dns_list:
        try:
            ipaddress.ip_address(x)
        except ValueError:
            raise RouterError(f"DNS server “{x}” isn't an IP address.")
    r.set_dhcp_settings(start, end, lease_time.strip().lower(), ",".join(dns_list))
    return r.dhcp_settings()


def forwards() -> list[dict]:
    r = adapter()
    if "forwards" not in r.caps:
        raise RouterError("This router type can't forward ports.")
    return r.forwards()


def add_forward(name: str, proto: str, ext_port, int_ip: str, int_port) -> None:
    r = adapter()
    if "forwards" not in r.caps:
        raise RouterError("This router type can't forward ports.")
    r.add_forward(_check_name(name), _check_proto(proto), _check_port(ext_port), _check_ip(int_ip), _check_port(int_port))


def remove_forward(fid: str) -> None:
    r = adapter()
    if "forwards" not in r.caps:
        raise RouterError("This router type can't forward ports.")
    r.remove_forward(str(fid))


def blocked() -> list[dict]:
    r = adapter()
    if "block" not in r.caps:
        raise RouterError("This router type can't block devices.")
    return [{"mac": b["mac"], "name": b["name"]} for b in r.blocked()]


def _protect(mac: str, protected: dict[str, str]) -> None:
    if mac in protected:
        raise RouterError(protected[mac])


def block(mac: str, name: str = "", protected: dict[str, str] | None = None) -> None:
    """Keep a device off the network (no internet, no router, no Wi-Fi where supported).
    protected: mac → reason it must not be blocked (this server, the router, the admin's own device)."""
    r = adapter()
    if "block" not in r.caps:
        raise RouterError("This router type can't block devices. OpenWrt and MikroTik can.")
    mac = _check_mac(mac)
    _protect(mac, protected or {})
    # the name is only a label on the router rule: keep the safe characters instead of refusing
    label = re.sub(r"\s+", " ", re.sub(r"[^\w .()-]", "", name or "", flags=re.ASCII)).strip()[:60]
    r.block(mac, _check_name(label))


def unblock(mac: str) -> None:
    r = adapter()
    if "block" not in r.caps:
        raise RouterError("This router type can't block devices.")
    r.unblock(_check_mac(mac))


def kick(mac: str, protected: dict[str, str] | None = None) -> None:
    """Disconnect a Wi-Fi device now (it may reconnect after about a minute)."""
    r = adapter()
    if "kick" not in r.caps:
        raise RouterError("This router type can't disconnect devices. OpenWrt and MikroTik can.")
    mac = _check_mac(mac)
    _protect(mac, protected or {})
    r.kick(mac)


def external_ip() -> str:
    try:
        r = adapter()
        return r.external_ip() if "external_ip" in r.caps else ""
    except RouterError:
        return ""
