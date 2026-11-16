"""Global variables: named values that jobs (and other modules) can use.

In any job text field, write {{var.NAME}} to insert a variable. Built-in values are also
available: {{sys.hostname}}, {{sys.date}}, {{sys.time}}, {{sys.datetime}}, {{sys.ip}},
{{sys.os}}, {{sys.version}}, {{sys.url}}; live monitor values such as {{mon.cpu_percent}},
{{mon.temp_avg}}, {{mon.temp.<sensor>}}, {{mon.disk.root.percent}} (see monitor_values()); plus
whatever the job's trigger and earlier actions provide ({{trigger.summary}}, {{trigger.value}}, …).

Secret variables (passwords, tokens) can be used the same way but are never shown again in
the UI, and their values are masked in job logs.
"""
from __future__ import annotations

import re
import socket
import time

from . import db

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*\}\}")


class VariableError(ValueError):
    pass


def list_all(include_secret_values: bool = False) -> list[dict]:
    rows = db.query("SELECT * FROM variables ORDER BY name COLLATE NOCASE")
    out = []
    for r in rows:
        d = dict(r)
        d["secret"] = bool(d["secret"])
        if d["secret"] and not include_secret_values:
            d["value"] = ""
            d["value_set"] = bool(r["value"])
        out.append(d)
    return out


def get(name: str) -> str | None:
    r = db.query_one("SELECT value FROM variables WHERE name=?", (name,))
    return r["value"] if r else None


def save(name: str, value: str | None, description: str = "", secret: bool = False,
         original_name: str | None = None) -> dict:
    """Create or update. For a secret, an empty value keeps the saved one."""
    name = (name or "").strip()
    if not NAME_RE.fullmatch(name):
        raise VariableError("Names start with a letter and use only letters, digits and _ (e.g. alert_email).")
    if len(value or "") > 20000:
        raise VariableError("The value is too long (max 20,000 characters).")
    original_name = original_name or name
    existing = db.query_one("SELECT * FROM variables WHERE name=?", (original_name,))
    if name != original_name and db.query_one("SELECT 1 FROM variables WHERE name=?", (name,)):
        raise VariableError(f"A variable called {name} already exists.")
    if not existing and db.query_one("SELECT 1 FROM variables WHERE name=?", (name,)):
        raise VariableError(f"A variable called {name} already exists.")
    if secret and not value and existing:
        value = existing["value"]
    with db.tx() as c:
        if existing:
            c.execute("UPDATE variables SET name=?, value=?, description=?, secret=?, updated_at=? WHERE name=?",
                      (name, value or "", (description or "").strip()[:300], int(secret), time.time(), original_name))
        else:
            c.execute("INSERT INTO variables (name, value, description, secret, updated_at) VALUES (?,?,?,?,?)",
                      (name, value or "", (description or "").strip()[:300], int(secret), time.time()))
    return next(v for v in list_all() if v["name"] == name)


def set_value(name: str, value: str) -> None:
    """Used by the "Set a variable" job action (creates the variable if needed)."""
    if not NAME_RE.fullmatch(name or ""):
        raise VariableError(f"Invalid variable name: {name}")
    db.execute("INSERT INTO variables (name, value, description, secret, updated_at) VALUES (?,?,'',0,?) "
               "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
               (name, str(value)[:20000], time.time()))


def delete(name: str) -> None:
    db.execute("DELETE FROM variables WHERE name=?", (name,))


def secret_values() -> list[str]:
    return [r["value"] for r in db.query("SELECT value FROM variables WHERE secret=1 AND value<>''")]


# ---------------------------------------------------------------- templates

def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def system_values() -> dict:
    from . import monitor, updater
    from .config import settings
    host = socket.gethostname().split(".")[0]
    scheme = "https" if settings.tls_enabled else "http"
    return {
        "hostname": host, "ip": _lan_ip(), "os": monitor.host_info().get("os", ""),
        "date": time.strftime("%Y-%m-%d"), "time": time.strftime("%H:%M"), "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": time.strftime("%A"), "version": updater.current_version(),
        "url": f"{scheme}://{host}.local:{settings.port}/",
    }


# ---------------------------------------------------------------- monitor values ({{mon.*}})

_mon_cache: tuple[float, list[dict]] = (0.0, [])


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def _size(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(b) < 1024 or unit == "PB":
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def _duration(sec: float) -> str:
    d, rem = divmod(int(sec), 86400)
    h, m = divmod(rem // 60, 60)
    return f"{d}d {h}h {m}m" if d else (f"{h}h {m}m" if h else f"{m}m")


def monitor_values(max_age: float = 5.0) -> list[dict]:
    """Live server measurements as {{mon.KEY}} values: [{key, value, label, group}].

    Numbers are plain (e.g. 37.5 for a percentage, 54 for °C) so they read well in text
    ("CPU {{mon.cpu_percent}}%"); sizes and speeds are already formatted ("12.3 GB")."""
    global _mon_cache
    if time.time() - _mon_cache[0] < max_age:
        return _mon_cache[1]
    import os

    from . import monitor
    s = monitor.stats()
    out: list[dict] = []

    def add(group: str, key: str, value, label: str) -> None:
        out.append({"group": group, "key": key, "value": value, "label": label})

    r1 = lambda x: round(float(x), 1)  # noqa: E731
    cores = os.cpu_count() or 1
    load = list(s.get("load") or (0, 0, 0))
    add("CPU & load", "cpu_percent", r1(s.get("cpu_percent") or 0), "CPU usage (%)")
    add("CPU & load", "cpu_cores", cores, "CPU cores")
    add("CPU & load", "load_1", round(load[0], 2), "Load average, 1 minute")
    add("CPU & load", "load_5", round(load[1], 2), "Load average, 5 minutes")
    add("CPU & load", "load_15", round(load[2], 2), "Load average, 15 minutes")
    add("CPU & load", "load_per_core", round(load[1] / cores, 2), "5-minute load per core (1.0 = fully busy)")
    add("CPU & load", "processes", s.get("process_count", 0), "Running processes")

    mem, swap = s.get("mem") or {}, s.get("swap") or {}
    add("Memory", "mem_percent", r1(mem.get("percent", 0)), "Memory used (%)")
    add("Memory", "mem_used", _size(mem.get("used", 0)), "Memory used")
    add("Memory", "mem_available", _size(mem.get("available", 0)), "Memory available")
    add("Memory", "mem_total", _size(mem.get("total", 0)), "Total memory")
    add("Memory", "swap_percent", r1(swap.get("percent", 0)), "Swap used (%)")

    disks = s.get("disks") or []
    if disks:
        fullest = max(disks, key=lambda d: d["percent"])
        add("Disks", "disk_max_percent", r1(fullest["percent"]), "Fullest drive (% used)")
        add("Disks", "disk_max_mount", fullest["mount"], "Fullest drive (mount point)")
        add("Disks", "disk_count", len(disks), "Mounted drives")
        used_keys: set[str] = set()
        for d in disks:
            slug = _slug(d["mount"]) or "root"
            while slug in used_keys:
                slug += "_"
            used_keys.add(slug)
            add("Disks", f"disk.{slug}.percent", r1(d["percent"]), f"{d['mount']}: used (%)")
            add("Disks", f"disk.{slug}.free", _size(d["free"]), f"{d['mount']}: free space")
            add("Disks", f"disk.{slug}.used", _size(d["used"]), f"{d['mount']}: used space")
            add("Disks", f"disk.{slug}.total", _size(d["total"]), f"{d['mount']}: size")

    temps = s.get("temps") or []
    if temps:
        vals = [t["current"] for t in temps]
        hottest = max(temps, key=lambda t: t["current"])
        add("Temperatures", "temp_max", r1(max(vals)), "Hottest sensor (°C)")
        add("Temperatures", "temp_max_sensor", hottest["label"], "Hottest sensor (name)")
        add("Temperatures", "temp_avg", r1(sum(vals) / len(vals)), "Average of all sensors (°C)")
        add("Temperatures", "temp_min", r1(min(vals)), "Coolest sensor (°C)")
        add("Temperatures", "temp_count", len(vals), "Number of sensors")
        used_keys = set()
        for t in temps:
            slug = _slug(t["label"]) or _slug(t["chip"]) or "sensor"
            if slug in used_keys:
                slug = _slug(f"{t['chip']}_{t['label']}")
            while slug in used_keys:
                slug += "_"
            used_keys.add(slug)
            add("Temperatures", f"temp.{slug}", r1(t["current"]), f"{t['label']} ({t['chip']}) (°C)")

    add("Network & disk activity", "net_rx", _size(s.get("net_rx_bps", 0)) + "/s", "Network download speed")
    add("Network & disk activity", "net_tx", _size(s.get("net_tx_bps", 0)) + "/s", "Network upload speed")
    add("Network & disk activity", "disk_read", _size(s.get("disk_read_bps", 0)) + "/s", "Disk read speed")
    add("Network & disk activity", "disk_write", _size(s.get("disk_write_bps", 0)) + "/s", "Disk write speed")
    add("System", "uptime", _duration(s.get("uptime", 0)), "Time since the server started")
    add("System", "uptime_days", round((s.get("uptime", 0)) / 86400, 1), "Uptime in days")

    _mon_cache = (time.time(), out)
    return out


def render(text: str, context: dict) -> str:
    """Replace {{var.x}}, {{sys.x}}, {{trigger.x}}, … in text. Unknown placeholders are left as they are."""
    if not text or "{{" not in text:
        return text or ""
    variables = {r["name"]: r["value"] for r in db.query("SELECT name, value FROM variables")}
    sysvals: dict | None = None
    monvals: dict | None = None

    def lookup(path: str):
        nonlocal sysvals, monvals
        parts = path.split(".")
        if parts[0] == "var" and len(parts) == 2:
            return variables.get(parts[1])
        if parts[0] == "sys" and len(parts) == 2:
            if sysvals is None:
                sysvals = system_values()
            return sysvals.get(parts[1])
        if parts[0] == "mon" and len(parts) >= 2:
            if monvals is None:
                monvals = {m["key"]: m["value"] for m in monitor_values()}
            return monvals.get(".".join(parts[1:]))
        cur = context
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return None
        return cur

    def sub(m: re.Match) -> str:
        v = lookup(m.group(1))
        return m.group(0) if v is None else str(v)

    return PLACEHOLDER_RE.sub(sub, text)


def mask(text: str) -> str:
    """Hide secret variable values (for logs)."""
    for s in secret_values():
        if len(s) >= 3:
            text = text.replace(s, "••••••")
    return text
