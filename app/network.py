"""Network module: find and identify every device on the LAN.

No root needed. How a scan works:
  1. The server's own IPv4 networks (psutil) and the gateway (/proc/net/route).
  2. Sweep: a quick TCP connection attempt to every address of each network (at most a /22).
     Whether or not anything answers, the kernel resolves the MAC address first, so afterwards
     the ARP table (/proc/net/arp) lists every device that is switched on.
  3. Identification, in parallel:
       vendor     – first half of the MAC address (IEEE list, downloaded once to the data folder);
                    "randomized" MACs are phones/laptops with private Wi-Fi addresses
       mDNS       – names and services announced on the LAN (avahi-browse; printers, Apple, Chromecast…)
       SSDP/UPnP  – friendly name, maker and model from the device's own description (TVs, routers, NAS…)
       NetBIOS    – Windows computer names
       DNS        – reverse lookup (names the router's DHCP server knows)
       ports      – ~30 common ports (web, SSH, file sharing, printer, camera, Plex…) and web page titles
  4. A kind is guessed from all that (router, computer, phone, TV, printer, NAS, camera, IoT…). Users can
     rename devices, change the kind, and mark them as known.

Devices are kept in the net_devices table (key = MAC address). A background loop re-scans every few
minutes (Network → Settings), so new devices are noticed (job trigger "New device on the network").
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import csv
import ipaddress
import json
import logging
import re
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import psutil

from . import db
from .config import settings

log = logging.getLogger("sysadmin-ia.network")

ARP_FILE = Path("/proc/net/arp")
ROUTE_FILE = Path("/proc/net/route")
OUI_FILE = settings.data_dir / "oui.csv"
OUI_URLS = ["https://standards-oui.ieee.org/oui/oui.csv",
            "https://www.wireshark.org/download/automated/data/manuf"]
MAX_HOSTS = 1024                      # networks larger than a /22: only the /24 around this server
SKIP_IFACES = ("lo", "docker", "br-", "veth", "virbr", "cni", "flannel", "tailscale", "wg", "tun", "zt")

# port → (label, kind hint)
PORTS = {
    21: ("FTP", None), 22: ("SSH", None), 23: ("Telnet", None), 53: ("DNS", None),
    80: ("Web", None), 443: ("Web (HTTPS)", None), 139: ("Windows sharing", "computer"), 445: ("File sharing (SMB)", None),
    515: ("Printer (LPD)", "printer"), 548: ("Apple file sharing", None), 554: ("Video stream (RTSP)", "camera"),
    631: ("Printer (IPP)", "printer"), 1400: ("Sonos", "speaker"), 1883: ("MQTT", "iot"), 2049: ("NFS", "nas"),
    3306: ("MySQL", None), 3389: ("Remote Desktop", "computer"), 5000: ("Web (5000)", None), 5001: ("Web (5001)", None),
    5432: ("PostgreSQL", None), 5900: ("VNC", None), 7000: ("AirPlay", None), 8006: ("Proxmox", "server"),
    8008: ("Chromecast", "media"), 8009: ("Chromecast", "media"), 8080: ("Web (8080)", None),
    8123: ("Home Assistant", "home"), 8443: ("Web (8443)", None), 9100: ("Printer (raw)", "printer"),
    32400: ("Plex", "media"), 62078: ("iPhone/iPad sync", "phone"),
    # mail, files, remote access
    25: ("Mail (SMTP)", None), 110: ("Mail (POP3)", None), 143: ("Mail (IMAP)", None), 465: ("Mail (SMTPS)", None),
    587: ("Mail (submission)", None), 993: ("Mail (IMAPS)", None), 995: ("Mail (POP3S)", None), 873: ("rsync", "nas"),
    5901: ("VNC", None), 8291: ("MikroTik Winbox", "network"),
    # databases & message brokers
    1433: ("SQL Server", None), 1521: ("Oracle database", None), 5672: ("RabbitMQ (AMQP)", None), 5984: ("CouchDB", None),
    6379: ("Redis", None), 8086: ("InfluxDB", None), 9200: ("Elasticsearch", None), 11211: ("Memcached", None),
    27017: ("MongoDB", None), 15672: ("RabbitMQ web", None),
    # containers & infrastructure
    2375: ("Docker API (unencrypted!)", "server"), 2376: ("Docker API (TLS)", "server"), 6443: ("Kubernetes API", "server"),
    10000: ("Webmin", "server"), 9090: ("Web (9090)", None), 9443: ("Web (9443)", None), 9000: ("Web (9000)", None),
    # web apps on common non-standard ports
    81: ("Web (81)", None), 591: ("Web (591)", None), 2283: ("Immich", None), 3000: ("Web (3000)", None), 3001: ("Web (3001)", None),
    4000: ("Web (4000)", None), 4443: ("Web (4443)", None), 5050: ("Web (5050)", None), 5055: ("Overseerr", None),
    5601: ("Kibana", None), 6767: ("Bazarr", None), 7080: ("Web (7080)", None), 7443: ("Web (7443)", None), 7878: ("Radarr", None),
    8000: ("Web (8000)", None), 8001: ("Web (8001)", None), 8081: ("Web (8081)", None), 8082: ("Web (8082)", None),
    8088: ("Web (8088)", None), 8090: ("Web (8090)", None), 8096: ("Jellyfin", "media"), 8112: ("Deluge", None),
    8181: ("Web (8181)", None), 8200: ("Web (8200)", None), 8384: ("Syncthing", None), 8444: ("Web (8444)", None),
    8686: ("Lidarr", None), 8800: ("Web (8800)", None), 8880: ("Web (8880)", None), 8888: ("Web (8888)", None),
    8920: ("Jellyfin (HTTPS)", "media"), 8989: ("Sonarr", None), 9001: ("Web (9001)", None), 9091: ("Transmission", None),
    9696: ("Prowlarr", None), 3030: ("Web (3030)", None), 4040: ("Web (4040)", None), 1880: ("Node-RED", "home"),
    51413: ("BitTorrent", None), 25565: ("Minecraft", None),
}
WEB_PORTS = {80: False, 8080: False, 5000: False, 8123: False, 443: True, 8443: True, 5001: True, 8006: True}
TLS_FIRST = {443, 465, 993, 995, 2376, 4443, 5001, 6443, 7443, 8006, 8443, 8920, 9443, 10000}   # usually TLS from the first byte

# what a service is, by what it answers: key → (label, kind of button in the UI)
SERVICES = {"http": ("Web", "web"), "https": ("Web (HTTPS)", "web"), "ssh": ("SSH", "ssh"), "ftp": ("FTP", None),
            "smtp": ("Mail (SMTP)", None), "pop3": ("Mail (POP3)", None), "imap": ("Mail (IMAP)", None),
            "telnet": ("Telnet", None), "vnc": ("VNC", None), "mysql": ("MySQL", None), "mariadb": ("MariaDB", None),
            "postgresql": ("PostgreSQL", None), "redis": ("Redis", None), "mongodb": ("MongoDB", None), "tls": ("TLS service", None)}

KINDS = {   # kind → label (the UI has an icon for each)
    "router": "Router", "this_server": "This server", "computer": "Computer", "phone": "Phone or tablet",
    "tv": "TV", "media": "Media player", "speaker": "Speaker", "printer": "Printer", "nas": "Storage (NAS)",
    "camera": "Camera", "iot": "Smart device", "home": "Home automation", "console": "Game console",
    "server": "Server", "network": "Network device", "unknown": "Unknown",
}


class NetworkError(RuntimeError):
    pass


# ---------------------------------------------------------------- this server's networks

def _private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and (a.is_private or ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")) and not a.is_loopback


def interfaces() -> list[dict]:
    """IPv4 LAN networks of this server: [{iface, ip, network, prefix, mac, scan}] (scan = what a sweep covers)."""
    out = []
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        if name.startswith(SKIP_IFACES) or (name in stats and not stats[name].isup):
            continue
        mac = next((a.address.lower() for a in addrs if getattr(a, "family", None) == getattr(psutil, "AF_LINK", -1)), None)
        for a in addrs:
            if a.family != socket.AF_INET or not a.netmask or not _private(a.address):
                continue
            net = ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False)
            scan = net if net.num_addresses <= MAX_HOSTS else ipaddress.ip_network(f"{a.address}/24", strict=False)
            out.append({"iface": name, "ip": a.address, "network": str(net), "prefix": net.prefixlen, "mac": mac,
                        "scan": str(scan)})
    return out


def parse_route(text: str) -> str | None:
    """Default gateway from /proc/net/route (hex, little-endian)."""
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) >= 3 and f[1] == "00000000" and f[2] != "00000000":
            return socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
    return None


def gateway() -> str | None:
    try:
        return parse_route(ROUTE_FILE.read_text())
    except OSError:
        return None


def parse_arp(text: str) -> dict[str, str]:
    """ip → mac from /proc/net/arp (complete entries only)."""
    out = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) >= 4 and f[2] != "0x0" and re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", f[3].lower()) \
                and f[3] != "00:00:00:00:00:00":
            out[f[0]] = f[3].lower()
    return out


def arp_table() -> dict[str, str]:
    try:
        return parse_arp(ARP_FILE.read_text())
    except OSError:
        return {}


# ---------------------------------------------------------------- MAC vendors

_oui: dict[str, str] | None = None
_oui_lock = threading.Lock()


def parse_oui(text: str) -> dict[str, str]:
    """IEEE oui.csv ("MA-L,286FB9,Nokia…") or Wireshark manuf ("28:6F:B9<TAB>Nokia<TAB>Nokia Shanghai…")."""
    out: dict[str, str] = {}
    if text.startswith("Registry"):
        for row in csv.reader(text.splitlines()[1:]):
            if len(row) >= 3 and len(row[1]) == 6:
                out[row[1].upper()] = row[2].strip()
        return out
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        f = line.split("\t")
        p = f[0].replace(":", "").replace("-", "").upper()
        if len(p) == 6 and len(f) >= 2:
            out[p] = (f[2] if len(f) > 2 and f[2].strip() else f[1]).strip()
    return out


def _load_oui(download: bool = True) -> dict[str, str]:
    global _oui
    with _oui_lock:
        if _oui is not None:
            return _oui
        stale = not OUI_FILE.exists() or time.time() - OUI_FILE.stat().st_mtime > 90 * 86400
        if download and stale:
            for url in OUI_URLS:
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "SysAdmin.IA (network module)"})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        data = r.read(20 * 1024 * 1024)
                    if len(parse_oui(data.decode("utf-8", "replace"))) > 10000:
                        OUI_FILE.parent.mkdir(parents=True, exist_ok=True)
                        OUI_FILE.write_bytes(data)
                        break
                except Exception as e:  # offline: vendors stay unknown
                    log.info("MAC vendor list download failed from %s: %s", url, e)
        try:
            _oui = parse_oui(OUI_FILE.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            _oui = {}
        return _oui


def randomized_mac(mac: str) -> bool:
    """Locally administered address: phones and laptops use these as private Wi-Fi addresses."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def vendor(mac: str, download: bool = True) -> str:
    if randomized_mac(mac):
        return "Private (randomized) address"
    return _load_oui(download).get(mac.replace(":", "").upper()[:6], "")


# ---------------------------------------------------------------- discovery helpers

def _knock(ip: str, port: int = 80, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def sweep(network: str, workers: int = 128) -> None:
    """Touch every address so the kernel learns its MAC address (results come from the ARP table)."""
    hosts = [str(h) for h in ipaddress.ip_network(network, strict=False).hosts()]
    with cf.ThreadPoolExecutor(workers) as ex:
        list(ex.map(lambda h: _knock(h, 80, 0.4), hosts))


def open_ports(ip: str, ports=None, timeout: float = 0.6, workers: int = 32) -> list[int]:
    ports = list(ports or PORTS)
    with cf.ThreadPoolExecutor(min(workers, len(ports))) as ex:
        res = ex.map(lambda p: (p, _knock(ip, p, timeout)), ports)
        return sorted(p for p, ok in res if ok)


# ---------------------------------------------------------------- what runs on an open port

def _recv(s: socket.socket, timeout: float, limit: int = 8192) -> bytes:
    s.settimeout(timeout)
    data = b""
    try:
        while len(data) < limit:
            chunk = s.recv(limit - len(data))
            if not chunk:
                break
            data += chunk
            if data.startswith(b"HTTP/") and b"\r\n\r\n" in data and len(data) > 512:
                break
    except (socket.timeout, OSError):
        pass
    return data


def _printable(b: bytes, n: int = 80) -> str:
    return re.sub(r"[^\x20-\x7e]+", " ", b.decode("latin-1"))[:n].strip()


def classify_banner(b: bytes, port: int) -> tuple[str, str] | None:
    """(service, detail) from what a server sends first (SSH, FTP, mail, VNC, MySQL, Telnet)."""
    if b.startswith(b"SSH-"):
        soft = b.split(b"\r\n")[0].split(b"\n")[0].decode("latin-1")[4:]
        soft = soft.split("-", 1)[1] if "-" in soft else soft          # "2.0-OpenSSH_9.2p1 Debian" → "OpenSSH_9.2p1 Debian"
        return "ssh", soft.replace("_", " ", 1)[:60]
    if b.startswith(b"220"):
        text = _printable(b[3:])
        return ("ftp" if "ftp" in text.lower() or port == 21 else "smtp"), text[:60]
    if b.startswith(b"+OK"):
        return "pop3", _printable(b[3:], 60)
    if b.startswith(b"* OK"):
        return "imap", _printable(b[4:], 60)
    if b.startswith(b"RFB "):
        return "vnc", b[4:11].decode("latin-1")
    if len(b) > 6 and b[4] == 10 and b"\x00" in b[5:60]:        # MySQL/MariaDB greeting: header, protocol 10, version
        ver = b[5:b.index(b"\x00", 5)].decode("latin-1")
        return ("mariadb" if "mariadb" in ver.lower() else "mysql"), ver.replace("-MariaDB", "")[:40]
    if b[:1] == b"\xff" and len(b) > 2 and b[1] in (0xfb, 0xfc, 0xfd, 0xfe):
        return "telnet", ""
    return None


def classify_reply(r: bytes) -> tuple[str, str] | None:
    """(service, detail) from the answer to an HTTP request sent to an unknown port."""
    if r.startswith(b"HTTP/"):
        low = r[:4096].lower()
        if b"mongodb" in low and b"native driver port" in low:
            return "mongodb", ""
        if b"sent to https port" in low or b"plain http request" in low:
            return "https", ""
        return "http", ""
    if r.startswith((b"-ERR", b"-NOAUTH", b"-DENIED")):
        return "redis", ""
    if r[:1] == b"E" and (b"SFATAL" in r or b"startup packet" in r or b"protocol" in r.lower()):
        return "postgresql", ""
    if r[:1] in (b"\x15", b"\x16") and r[1:2] == b"\x03":       # TLS alert/handshake: it speaks TLS
        return "tls", ""
    return None


def _http_request(ip: str, port: int) -> bytes:
    return (f"GET / HTTP/1.1\r\nHost: {ip}:{port}\r\nUser-Agent: Mozilla/5.0 (SysAdmin.IA network scan)\r\n"
            "Accept: */*\r\nConnection: close\r\n\r\n").encode()


def _tls_probe(ip: str, port: int, timeout: float = 3.0) -> bytes | None:
    """HTTP response over TLS (b"" if TLS works but the service doesn't speak HTTP); None if no TLS."""
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    try:
        with socket.create_connection((ip, port), timeout=timeout) as raw, ctx.wrap_socket(raw, server_hostname=None) as s:
            s.sendall(_http_request(ip, port))
            return _recv(s, timeout, 4096)
    except (ssl.SSLError, OSError):
        return None


def probe_service(ip: str, port: int, timeout: float = 2.0) -> dict:
    """What answers on an open port: {port, service, label, detail, url?, ssh?}."""
    known = PORTS.get(port, (f"Port {port}", None))[0]
    service, detail = None, ""
    if port not in TLS_FIRST:
        try:
            with socket.create_connection((ip, port), timeout=timeout) as s:
                hit = classify_banner(_recv(s, 1.2, 1024), port)       # servers that talk first
                if hit:
                    service, detail = hit
                else:
                    s.sendall(_http_request(ip, port))
                    hit = classify_reply(_recv(s, timeout))
                    if hit:
                        service, detail = hit
        except OSError:
            pass
    if service in (None, "tls", "https"):
        r = _tls_probe(ip, port, timeout + 1)
        if r is not None:
            service = "https" if r.startswith(b"HTTP/") else "tls"
    out = {"port": port, "service": service or "", "label": known, "detail": detail}
    if service in ("http", "https"):
        page = http_info(ip, port, service == "https")
        out["url"] = f"{service}://{ip}:{port}/"
        out["title"] = page.get("title", "")
        out["server"] = page.get("server", "")
        generic = known.startswith(("Web", "Port ")) or port in (80, 443)
        out["label"] = (page.get("title") or known) if generic else known
        if out["label"].startswith("Port "):
            out["label"] = "Web"
    elif service:
        label = SERVICES[service][0]
        out["label"] = known if service == "tls" and not known.startswith("Port ") else label
        if service == "ssh":
            out["ssh"] = True
    return out


def identify_services(ip: str, ports: list[int], workers: int = 16) -> list[dict]:
    if not ports:
        return []
    with cf.ThreadPoolExecutor(min(workers, len(ports))) as ex:
        found = list(ex.map(lambda p: probe_service(ip, p), ports))
    order = {"http": 0, "https": 0, "ssh": 1}
    return sorted(found, key=lambda s: (order.get(s["service"], 2), s["port"]))


def http_info(ip: str, port: int, tls: bool, timeout: float = 2.5) -> dict:
    """Title and Server header of a web page (certificate not checked: LAN devices use self-signed ones)."""
    url = f"{'https' if tls else 'http'}://{ip}:{port}/"
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (SysAdmin.IA network scan)"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx if tls else None) as r:
            body = r.read(65536).decode("utf-8", "replace")
            server = r.headers.get("Server", "")
    except urllib.error.HTTPError as e:
        body, server = "", e.headers.get("Server", "") if e.headers else ""
    except Exception:
        return {}
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    title = re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""
    return {k: v for k, v in {"title": title, "server": server[:80]}.items() if v}


def reverse_dns(ip: str) -> str:
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return ""
    return "" if name == ip else name


# -- NetBIOS (Windows names)
def _nbstat_packet() -> bytes:
    name = b"*" + b"\x00" * 15
    enc = bytes(b for c in name for b in (0x41 + (c >> 4), 0x41 + (c & 0x0F)))
    return struct.pack(">HHHHHH", 0x5AA5, 0, 1, 0, 0, 0) + b"\x20" + enc + b"\x00" + struct.pack(">HH", 0x21, 1)


def parse_nbstat(data: bytes) -> str:
    """Computer name from a NetBIOS node status answer ('' if none)."""
    try:
        count = data[56]
        for i in range(count):
            off = 57 + i * 18
            raw, suffix, flags = data[off:off + 15], data[off + 15], struct.unpack(">H", data[off + 16:off + 18])[0]
            if suffix == 0x00 and not flags & 0x8000:   # unique workstation name
                return raw.decode("ascii", "replace").strip()
    except (IndexError, struct.error):
        pass
    return ""


def netbios_name(ip: str, timeout: float = 1.0) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(_nbstat_packet(), (ip, 137))
            return parse_nbstat(s.recv(1024))
    except OSError:
        return ""


# -- SSDP / UPnP
def ssdp_search(timeout: float = 3.0, st: str = "ssdp:all") -> list[dict]:
    """[{ip, location, server, st, usn}] from devices answering an M-SEARCH."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           f"MX: 2\r\nST: {st}\r\n\r\n").encode()
    found, seen = [], set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.settimeout(0.5)
            s.sendto(msg, ("239.255.255.250", 1900))
            s.sendto(msg, ("239.255.255.250", 1900))
            end = time.time() + timeout
            while time.time() < end:
                try:
                    data, (ip, _) = s.recvfrom(4096)
                except socket.timeout:
                    continue
                h = parse_ssdp(data.decode("utf-8", "replace"))
                key = (ip, h.get("location"), h.get("st"))
                if key not in seen:
                    seen.add(key)
                    found.append({"ip": ip, **h})
    except OSError as e:
        log.info("SSDP search failed: %s", e)
    return found


def parse_ssdp(text: str) -> dict:
    h = {}
    for line in text.split("\r\n")[1:]:
        k, _, v = line.partition(":")
        if k.strip().lower() in ("location", "server", "st", "usn", "nt"):
            h[k.strip().lower()] = v.strip()
    return h


def _xml_text(root, tag: str) -> str:
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == tag and el.text:
            return el.text.strip()
    return ""


def upnp_description(location: str, timeout: float = 3.0) -> dict:
    try:
        with urllib.request.urlopen(location, timeout=timeout) as r:
            root = ET.fromstring(r.read(262144))
    except Exception:
        return {}
    return {k: v for k, v in {t: _xml_text(root, t) for t in
            ("friendlyName", "manufacturer", "modelName", "modelDescription", "deviceType")}.items() if v}


# -- mDNS (Avahi)
def parse_avahi(text: str) -> dict[str, dict]:
    """ip → {hostname, services:[…], names:[…]} from `avahi-browse -aprt` output."""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        f = line.split(";")
        if len(f) < 9 or f[0] != "=" or f[2] != "IPv4":
            continue
        name, stype, host, ip = f[3].replace("\\032", " "), f[4], f[6], f[7]
        name = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), name)
        d = out.setdefault(ip, {"hostname": "", "services": [], "names": [], "txt": {}})
        d["hostname"] = d["hostname"] or host.removesuffix(".local")
        if stype not in d["services"]:
            d["services"].append(stype)
        if name not in d["names"]:
            d["names"].append(name)
        for kv in re.findall(r'"([^"=]+)=([^"]*)"', f[9] if len(f) > 9 else ""):
            if kv[0] in ("md", "model", "ty", "fn", "manufacturer", "usb_MFG", "usb_MDL", "am"):
                d["txt"][kv[0]] = kv[1][:80]
    return out


def mdns_browse(timeout: int = 8) -> dict[str, dict]:
    if not shutil.which("avahi-browse"):
        return {}
    try:
        r = subprocess.run(["avahi-browse", "-aprt", "--no-db-lookup"], capture_output=True, text=True, timeout=timeout)
        return parse_avahi(r.stdout)
    except (OSError, subprocess.TimeoutExpired) as e:
        out = getattr(e, "stdout", None)
        return parse_avahi(out.decode() if isinstance(out, bytes) else out or "")


# ---------------------------------------------------------------- identification

def guess_kind(d: dict, gw: str | None, own_ips: set[str]) -> tuple[str, str]:
    """(kind, why) from everything known about a device."""
    ip, ports = d.get("ip"), set(d.get("ports") or [])
    up = d.get("upnp") or {}
    mdns = d.get("mdns") or {}
    svc = " ".join(mdns.get("services") or []).lower()
    vend = (d.get("vendor") or "").lower()
    text = " ".join([vend, (up.get("manufacturer") or ""), (up.get("modelName") or ""), (up.get("friendlyName") or ""),
                     (up.get("deviceType") or ""), " ".join(mdns.get("names") or []), json.dumps(mdns.get("txt") or {}),
                     " ".join((d.get("http") or {}).get(str(p), {}).get("title", "") for p in ports),
                     d.get("hostname") or "", d.get("netbios") or ""]).lower()
    has = lambda *w: any(x in text for x in w)  # noqa: E731
    if ip in own_ips:
        return "this_server", "This server"
    if ip and ip == gw:
        return "router", "Default gateway of this network"
    if "internetgatewaydevice" in text:
        return "router", "Announces itself as an internet gateway (UPnP)"
    if "_ipp" in svc or "_printer" in svc or "_pdl-datastream" in svc or ports & {631, 9100, 515} or \
            has("printer", "laserjet", "deskjet", "officejet", "epson", "brother", "canon", "kyocera", "lexmark"):
        return "printer", "Printer services"
    if "_googlecast" in svc or 8009 in ports or has("chromecast", "roku", "fire tv", "apple tv", "shield"):
        return "media", "Streams media (Chromecast, AirPlay…)"
    if "mediarenderer" in text and has("tv", "television", "bravia", "webos", "tizen", "vizio", "hisense", "tcl") or \
            has("smart tv", "bravia", "[tv]", " tv ", "webos", "tizen"):
        return "tv", "TV"
    if "_sonos" in svc or 1400 in ports or has("sonos", "bose", "homepod", "echo", "_raop", "google home", "nest audio"):
        return "speaker", "Speaker / voice assistant"
    if has("synology", "qnap", "truenas", "unraid", "asustor", "terramaster", "netgear readynas", "wd my cloud") or \
            ports & {2049} or ("_smb" in svc and ports & {5000, 5001}):
        return "nas", "Network storage"
    if 554 in ports or has("camera", "ipcam", "hikvision", "dahua", "reolink", "axis", "amcrest", "wyze", "ring"):
        return "camera", "Camera"
    if 8123 in ports or has("home assistant", "homey", "hubitat", "smartthings", "philips hue", "hue bridge", "deconz", "zigbee"):
        return "home", "Home automation hub"
    if has("playstation", "xbox", "nintendo", "sony interactive"):
        return "console", "Game console"
    if 62078 in ports or has("iphone", "ipad", "android", "pixel", "galaxy", "xiaomi", "oneplus", "huawei device") or \
            (vend.startswith("private") and not ports & {22, 445, 3389}):
        return "phone", "Phone or tablet" if not vend.startswith("private") else "Private Wi-Fi address (usually a phone or laptop)"
    if has("espressif", "tuya", "shelly", "sonoff", "tasmota", "esphome", "wiz", "lifx", "nanoleaf", "tp-link smart", "meross", "_hap"):
        return "iot", "Smart-home device"
    if has("ubiquiti", "unifi", "mikrotik", "aruba", "ruckus", "cisco", "netgear", "tp-link", "d-link", "zyxel", "eero",
           "orbi", "deco", "openwrt", "access point", "switch") and not ports & {445, 3389, 22}:
        return "network", "Network equipment (switch, access point, mesh…)"
    if 8006 in ports or has("proxmox", "esxi", "raspberry pi"):
        return "server", "Server"
    if ports & {139, 445, 3389} or d.get("netbios") or has("windows", "microsoft", "macbook", "imac", "_device-info", "dell",
                                                            "lenovo", "hewlett", "asus", "intel corporate"):
        return "computer", "Computer"
    if 22 in ports:
        return "server", "Runs an SSH server"
    return "unknown", ""


def best_name(d: dict) -> str:
    up, mdns = d.get("upnp") or {}, d.get("mdns") or {}
    for v in (up.get("friendlyName"), (mdns.get("names") or [None])[0], d.get("netbios"), mdns.get("hostname"),
              (d.get("hostname") or "").split(".")[0], (mdns.get("txt") or {}).get("md")):
        if v and not re.fullmatch(r"[0-9a-f:-]{12,}", v.lower()):
            return v[:80]
    return ""


# ---------------------------------------------------------------- scanning

_scan_lock = threading.Lock()
_progress: dict = {"state": "idle", "stage": "", "pct": None, "started": None, "finished": None, "found": 0, "new": 0,
                   "error": None}


def progress() -> dict:
    return dict(_progress)


def _set(**kw) -> None:
    _progress.update(kw)


def _row_to_dict(r) -> dict:
    d = dict(r)
    d["info"] = json.loads(d.get("info") or "{}")
    return d


def devices() -> list[dict]:
    """All devices, newest information merged, with online status and display fields."""
    now = time.time()
    s = get_settings()
    online_window = max(15, int(s["interval"])) * 60 * 2 + 120
    out = []
    for r in db.query("SELECT * FROM net_devices"):
        d = _row_to_dict(r)
        info = d.pop("info")
        d.update({"ports": info.get("ports", []), "services": info.get("services") or [],
                  "mdns_services": (info.get("mdns") or {}).get("services", []), "full_port_scan": info.get("full_port_scan"),
                  "upnp": info.get("upnp") or {}, "http": info.get("http") or {}, "netbios": info.get("netbios") or "",
                  "why": info.get("why") or "", "auto_name": info.get("name") or "",
                  "online": now - (d["last_seen"] or 0) < online_window,
                  "name": d.get("label") or info.get("name") or d.get("hostname") or "",
                  "kind_label": KINDS.get(d.get("kind") or "unknown", "Unknown"),
                  "port_labels": [[p, PORTS.get(p, (str(p), None))[0]] for p in info.get("ports", [])]})
        out.append(d)
    out.sort(key=lambda d: tuple(int(x) for x in (d["ip"] or "0.0.0.0").split(".")) if d.get("ip") else (999,))
    return out


def get_settings() -> dict:
    try:
        s = json.loads(db.get_setting("network_settings") or "{}")
    except ValueError:
        s = {}
    return {"watch": s.get("watch", True), "interval": int(s.get("interval", 15)), "extra": s.get("extra", "")}


def save_settings(watch: bool, interval: int, extra: str = "") -> dict:
    interval = max(5, min(1440, int(interval)))
    extra = (extra or "").strip()
    for part in filter(None, re.split(r"[\s,]+", extra)):
        try:
            n = ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise NetworkError(f"“{part}” isn't a network like 192.168.2.0/24.")
        if n.version != 4 or not n.is_private or n.num_addresses > MAX_HOSTS:
            raise NetworkError(f"{part}: use a private IPv4 network of at most {MAX_HOSTS} addresses (/22).")
    db.set_setting("network_settings", json.dumps({"watch": bool(watch), "interval": interval, "extra": extra}))
    return get_settings()


def scan(deep: bool = True, wait: bool = False) -> dict:
    """Start a scan in the background (or run it here with wait=True). deep=False skips identification of
    devices already known (used by the periodic check)."""
    if not _scan_lock.acquire(blocking=False):
        if wait:
            raise NetworkError("A scan is already running.")
        return progress()
    _set(state="running", stage="Starting", pct=0, started=time.time(), finished=None, error=None, found=0, new=0)

    def run():
        try:
            _scan(deep)
            _set(state="done", stage="Done", pct=100, finished=time.time())
        except Exception as e:  # noqa: BLE001 - shown to the user
            log.exception("network scan failed")
            _set(state="failed", error=str(e), finished=time.time())
        finally:
            _scan_lock.release()
    if wait:
        run()
        if _progress["state"] == "failed":
            raise NetworkError(_progress["error"])
    else:
        threading.Thread(target=run, daemon=True, name="network-scan").start()
    return progress()


def _scan(deep: bool) -> None:
    ifs = interfaces()
    nets = {i["scan"] for i in ifs} | set(filter(None, re.split(r"[\s,]+", get_settings()["extra"])))
    if not nets:
        raise NetworkError("This server has no private IPv4 network to scan.")
    own = {i["ip"]: i for i in ifs}
    gw = gateway()
    _set(stage="Looking for devices", pct=5)
    for n in sorted(nets):
        sweep(n)
    time.sleep(0.5)
    arp = arp_table()
    for ip, i in own.items():   # this server itself is never in its own ARP table
        if i.get("mac"):
            arp[ip] = i["mac"]
    inside = [ipaddress.ip_network(n, strict=False) for n in nets]
    arp = {ip: mac for ip, mac in arp.items() if any(ipaddress.ip_address(ip) in n for n in inside)}
    _set(stage=f"Found {len(arp)} devices; identifying them", pct=30, found=len(arp))

    existing = {r["mac"]: _row_to_dict(r) for r in db.query("SELECT * FROM net_devices")}
    todo = [ip for ip, mac in arp.items() if deep or mac not in existing]
    found: dict[str, dict] = {ip: {"ip": ip, "mac": mac} for ip, mac in arp.items()}
    if todo:
        _identify({ip: found[ip] for ip in todo}, own, gw)
    _set(stage="Saving", pct=95)
    now = time.time()
    new = 0
    for ip, d in found.items():
        old = existing.get(d["mac"])
        if old and ip not in todo:   # quick check: only presence and address
            db.execute("UPDATE net_devices SET ip=?, last_seen=? WHERE mac=?", (ip, now, d["mac"]))
            continue
        info = {"ports": d.get("ports", []), "services": d.get("services", []), "http": d.get("http", {}), "upnp": d.get("upnp", {}),
                "mdns": d.get("mdns", {}), "netbios": d.get("netbios", ""), "why": d.get("why", ""), "name": d.get("name", "")}
        if old:
            kind = old["kind"] if old.get("kind_set") else d["kind"]
            db.execute("UPDATE net_devices SET ip=?, hostname=?, vendor=?, kind=?, info=?, last_seen=? WHERE mac=?",
                       (ip, d.get("hostname", ""), d.get("vendor", ""), kind, json.dumps(info), now, d["mac"]))
        else:
            new += 1
            db.execute("INSERT INTO net_devices (mac, ip, hostname, vendor, kind, label, notes, info, first_seen, last_seen, known, kind_set)"
                       " VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                       (d["mac"], ip, d.get("hostname", ""), d.get("vendor", ""), d["kind"], "", "", json.dumps(info), now, now))
    db.set_setting("network_last_scan", str(now))
    _set(new=new)


def _identify(found: dict[str, dict], own: dict[str, dict], gw: str | None) -> None:
    ips = list(found)
    with cf.ThreadPoolExecutor(8) as ex:
        f_ssdp = ex.submit(ssdp_search, 3.0)
        f_mdns = ex.submit(mdns_browse, 8)
        f_oui = ex.submit(_load_oui, True)
        with cf.ThreadPoolExecutor(64) as pool:
            names = dict(zip(ips, pool.map(reverse_dns, ips)))
            nb = dict(zip(ips, pool.map(netbios_name, ips)))
            _set(stage="Checking open ports", pct=50)
            ports = dict(zip(ips, pool.map(open_ports, ips)))
        _set(stage="Identifying services", pct=70)
        with cf.ThreadPoolExecutor(16) as pool:
            svcs = dict(zip(ips, pool.map(lambda ip: identify_services(ip, ports[ip]), ips)))
        ssdp, mdns = f_ssdp.result(), f_mdns.result()
        f_oui.result()
    _set(stage="Reading device descriptions", pct=85)
    loc_by_ip: dict[str, str] = {}
    for s in ssdp:
        if s["ip"] in found and s.get("location"):
            # prefer the root device description
            if "rootdevice" in (s.get("st") or "") or s["ip"] not in loc_by_ip:
                loc_by_ip[s["ip"]] = s["location"]
    with cf.ThreadPoolExecutor(16) as pool:
        desc = dict(zip(loc_by_ip, pool.map(upnp_description, loc_by_ip.values())))
    gw_types = {s["ip"] for s in ssdp if "InternetGatewayDevice" in (s.get("st") or "") + (s.get("usn") or "")}
    for ip, d in found.items():
        d["vendor"] = vendor(d["mac"], download=False)
        d["hostname"] = names.get(ip, "") or (mdns.get(ip) or {}).get("hostname", "")
        d["netbios"] = nb.get(ip, "")
        d["ports"] = ports.get(ip, [])
        d["services"] = svcs.get(ip, [])
        d["http"] = {str(s["port"]): {k: s[k] for k in ("title", "server") if s.get(k)} for s in d["services"] if s.get("url")}
        d["upnp"] = desc.get(ip, {})
        if ip in gw_types:
            d["upnp"]["deviceType"] = d["upnp"].get("deviceType") or "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
        d["mdns"] = mdns.get(ip, {})
        d["kind"], d["why"] = guess_kind(d, gw, set(own))
        d["name"] = best_name(d) or (socket.gethostname() if ip in own else "")


_port_scans: dict[str, dict] = {}


def port_scan_status(mac: str) -> dict:
    return dict(_port_scans.get(mac) or {"state": "idle"})


def scan_all_ports(mac: str, wait: bool = False) -> dict:
    """Check all 65535 TCP ports of one device (about a minute), then identify what answers."""
    row = db.query_one("SELECT * FROM net_devices WHERE mac=?", (mac,))
    if not row or not row["ip"]:
        raise NetworkError("That device isn't in the list any more.")
    if (_port_scans.get(mac) or {}).get("state") == "running":
        return port_scan_status(mac)
    ip = row["ip"]
    st = _port_scans[mac] = {"state": "running", "pct": 0, "found": 0, "ip": ip, "error": None}

    def run():
        try:
            found: list[int] = []
            step = 4096
            for start in range(1, 65536, step):
                found += open_ports(ip, range(start, min(start + step, 65536)), timeout=0.4, workers=512)
                st.update(pct=round(100 * min(start + step, 65536) / 65536 * 0.85), found=len(found))
            services = identify_services(ip, found)
            r = db.query_one("SELECT info FROM net_devices WHERE mac=?", (mac,))
            info = json.loads(r["info"] or "{}") if r else {}
            info.update(ports=found, services=services, full_port_scan=time.time(),
                        http={str(s["port"]): {k: s[k] for k in ("title", "server") if s.get(k)} for s in services if s.get("url")})
            db.execute("UPDATE net_devices SET info=?, last_seen=? WHERE mac=?", (json.dumps(info), time.time(), mac))
            st.update(state="done", pct=100, found=len(found))
        except Exception as e:  # noqa: BLE001 - shown to the user
            st.update(state="failed", error=str(e))
    if wait:
        run()
    else:
        threading.Thread(target=run, daemon=True, name=f"portscan-{ip}").start()
    return port_scan_status(mac)


def update_device(mac: str, label: str | None = None, kind: str | None = None, notes: str | None = None,
                  known: bool | None = None) -> dict:
    row = db.query_one("SELECT * FROM net_devices WHERE mac=?", (mac,))
    if not row:
        raise NetworkError("That device isn't in the list any more.")
    if label is not None:
        db.execute("UPDATE net_devices SET label=? WHERE mac=?", (label.strip()[:80], mac))
    if kind is not None:
        if kind not in KINDS:
            raise NetworkError("Unknown kind of device.")
        db.execute("UPDATE net_devices SET kind=?, kind_set=1 WHERE mac=?", (kind, mac))
    if notes is not None:
        db.execute("UPDATE net_devices SET notes=? WHERE mac=?", (notes.strip()[:1000], mac))
    if known is not None:
        db.execute("UPDATE net_devices SET known=? WHERE mac=?", (1 if known else 0, mac))
    return next(d for d in devices() if d["mac"] == mac)


def forget(mac: str) -> None:
    db.execute("DELETE FROM net_devices WHERE mac=?", (mac,))


def mark_all_known() -> int:
    return db.execute("UPDATE net_devices SET known=1 WHERE known=0")


def new_devices_since(ts: float) -> list[dict]:
    return [d for d in devices() if (d["first_seen"] or 0) > ts]


def overview() -> dict:
    ifs = interfaces()
    last = db.get_setting("network_last_scan")
    return {"interfaces": ifs, "gateway": gateway(), "last_scan": float(last) if last else None,
            "progress": progress(), "settings": get_settings(), "kinds": KINDS,
            "linux": ARP_FILE.exists(), "avahi": bool(shutil.which("avahi-browse"))}


# ---------------------------------------------------------------- background loop

class NetworkEngine:
    """Re-scans every few minutes (Network → Settings) and keeps dynamic DNS up to date."""
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        if self.task is None and ARP_FILE.exists():
            self.task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                s = get_settings()
                last = float(db.get_setting("network_last_scan") or 0)
                if s["watch"] and time.time() - last >= s["interval"] * 60:
                    await asyncio.to_thread(scan, False, True)
            except NetworkError:
                pass
            except Exception:
                log.exception("network watch failed")
            try:
                from . import publish
                await asyncio.to_thread(publish.ddns_tick)
            except Exception:
                log.exception("dynamic DNS update failed")
            await asyncio.sleep(60)


ENGINE = NetworkEngine()
