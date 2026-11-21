"""Server resource monitoring via psutil (+ systemd when available)."""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import socket
import time

import psutil

_last_net: tuple[float, int, int] | None = None
_last_disk: tuple[float, int, int] | None = None

# Pseudo / virtual filesystems not worth showing.
_SKIP_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs", "cgroup", "cgroup2",
            "autofs", "devpts", "mqueue", "tracefs", "debugfs", "securityfs", "pstore", "fusectl",
            "configfs", "hugetlbfs", "efivarfs", "binfmt_misc", "nsfs", "ramfs"}


def _os_name() -> str:
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return f"{platform.system()} {platform.release()}"


def host_info() -> dict:
    return {
        "hostname": socket.gethostname(),
        "os": _os_name(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu_count": psutil.cpu_count(logical=True),
        "cpu_physical": psutil.cpu_count(logical=False),
        "python": platform.python_version(),
        "boot_time": psutil.boot_time(),
    }


def _rates() -> dict:
    global _last_net, _last_disk
    now = time.time()
    net = psutil.net_io_counters()
    out = {"net_rx_bps": 0.0, "net_tx_bps": 0.0, "disk_read_bps": 0.0, "disk_write_bps": 0.0}
    if _last_net:
        dt = max(now - _last_net[0], 1e-3)
        out["net_rx_bps"] = max(0, net.bytes_recv - _last_net[1]) / dt
        out["net_tx_bps"] = max(0, net.bytes_sent - _last_net[2]) / dt
    _last_net = (now, net.bytes_recv, net.bytes_sent)
    try:
        dio = psutil.disk_io_counters()
    except Exception:
        dio = None
    if dio:
        if _last_disk:
            dt = max(now - _last_disk[0], 1e-3)
            out["disk_read_bps"] = max(0, dio.read_bytes - _last_disk[1]) / dt
            out["disk_write_bps"] = max(0, dio.write_bytes - _last_disk[2]) / dt
        _last_disk = (now, dio.read_bytes, dio.write_bytes)
    return out


def _disks() -> list[dict]:
    seen = set()
    disks = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype in _SKIP_FS or part.mountpoint in seen or part.mountpoint.startswith("/snap/"):
            continue
        seen.add(part.mountpoint)
        try:
            u = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        disks.append({"mount": part.mountpoint, "device": part.device, "fstype": part.fstype,
                      "total": u.total, "used": u.used, "free": u.free, "percent": u.percent})
    return disks


def _temps() -> list[dict]:
    try:
        temps = psutil.sensors_temperatures()  # type: ignore[attr-defined]
    except Exception:
        return []
    out = []
    for chip, entries in (temps or {}).items():
        for e in entries:
            if e.current:
                out.append({"chip": chip, "label": e.label or chip, "current": e.current,
                            "high": e.high, "critical": e.critical})
    return out[:12]


def stats() -> dict:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    try:
        load = os.getloadavg()
    except (AttributeError, OSError):
        load = (0.0, 0.0, 0.0)
    return {
        "ts": time.time(),
        "uptime": time.time() - psutil.boot_time(),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_per_core": psutil.cpu_percent(interval=None, percpu=True),
        "load": load,
        "mem": {"total": vm.total, "used": vm.total - vm.available, "available": vm.available,
                "percent": vm.percent},
        "swap": {"total": sw.total, "used": sw.used, "percent": sw.percent},
        "disks": _disks(),
        "temps": _temps(),
        "process_count": len(psutil.pids()),
        **_rates(),
    }


def processes(sort: str = "cpu", limit: int = 25) -> list[dict]:
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_percent",
                                  "memory_info", "status", "cmdline"]):
        info = p.info
        procs.append({
            "pid": info["pid"],
            "name": info["name"],
            "user": info["username"],
            "cpu": info["cpu_percent"] or 0.0,
            "mem": round(info["memory_percent"] or 0.0, 2),
            "rss": info["memory_info"].rss if info["memory_info"] else 0,
            "status": info["status"],
            "cmd": " ".join(info["cmdline"] or [])[:200],
        })
    key = {"cpu": "cpu", "mem": "mem"}.get(sort, "cpu")
    procs.sort(key=lambda x: x[key], reverse=True)
    return procs[:limit]


def system_processes(sort: str = "cpu", limit: int = 60) -> dict:
    """Every process with CPU % over ~0.5 s (the Processes page; same shape as a device's, see device_agent.procs)."""
    attrs = ["pid", "name", "username", "memory_info", "status", "cmdline", "num_threads", "create_time"]
    if hasattr(psutil.Process, "uids"):      # POSIX only
        attrs.append("uids")
    procs = list(psutil.process_iter(attrs))
    for p in procs:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(0.5)
    total = psutil.virtual_memory().total or 1
    rows = []
    for p in procs:
        try:
            cpu = p.cpu_percent(None)
        except psutil.Error:
            continue
        i = p.info
        rss = i["memory_info"].rss if i.get("memory_info") else 0
        rows.append({"pid": i["pid"], "name": i["name"] or "?", "user": i.get("username") or "",
                     "uid": i["uids"].real if i.get("uids") else None, "cpu": round(cpu, 1), "mem": rss,
                     "mem_percent": round(rss * 100 / total, 1), "state": i.get("status") or "",
                     "threads": i.get("num_threads"), "started": i.get("create_time"),
                     "cmd": " ".join(i.get("cmdline") or [])[:400] or f"[{i['name']}]"})
    key = {"cpu": lambda r: -r["cpu"], "mem": lambda r: -r["mem"], "name": lambda r: r["name"].lower(),
           "pid": lambda r: r["pid"]}.get(sort, lambda r: -r["cpu"])
    rows.sort(key=key)
    return {"procs": rows[:max(1, min(500, int(limit or 60)))], "total": len(rows),
            "uid": os.getuid() if hasattr(os, "getuid") else None, "time": time.time(), "app_pids": sorted(_app_pids())}


def _app_pids() -> set[int]:
    """SysAdmin.IA's own processes (the supervisor and its workers): managed with the cards, not signalled here."""
    try:
        me = psutil.Process()
        uid = me.uids().real
        top = me
        for parent in me.parents():      # up to the supervisor (the service's main process, same user)
            if parent.pid <= 1 or parent.uids().real != uid:
                break
            top = parent
        return {top.pid} | {c.pid for c in top.children(recursive=True)}
    except (psutil.Error, AttributeError):
        return {os.getpid()}


SIGNALS = {"TERM": 15, "KILL": 9, "HUP": 1, "INT": 2, "STOP": 19, "CONT": 18}


def signal_process(pid: int, sig: str) -> dict:
    """Send a signal to a process of this server (as the app's user: only its own processes, unless it's root)."""
    import signal as _signal
    sig = (sig or "").upper()
    if sig not in SIGNALS:
        raise ValueError("Unknown signal.")
    pid = int(pid)
    if pid <= 1:
        raise ValueError("That process can't be signalled from here.")
    if pid in _app_pids():
        raise ValueError("That's one of SysAdmin.IA's own processes: start, stop or restart it with the cards above.")
    try:
        p = psutil.Process(pid)
        p.send_signal(getattr(_signal, "SIG" + sig, SIGNALS[sig]))
    except psutil.NoSuchProcess:
        raise ValueError("That process has already ended.")
    except psutil.AccessDenied:
        raise ValueError("SysAdmin.IA's user isn't allowed to signal that process (it belongs to another user).")
    return {"ok": True, "pid": pid, "signal": sig}


async def services() -> dict:
    """Return failed systemd units and a running-service count (empty if no systemd)."""
    if not shutil.which("systemctl"):
        return {"available": False, "failed": [], "running": 0}

    async def run(*args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return out.decode(errors="replace")

    try:
        failed_raw = await run("list-units", "--state=failed", "--no-legend", "--plain", "--no-pager")
        running_raw = await run("list-units", "--type=service", "--state=running", "--no-legend",
                                "--plain", "--no-pager")
    except Exception:
        return {"available": False, "failed": [], "running": 0}
    failed = [line.split()[0] for line in failed_raw.splitlines() if line.strip()]
    running = len([line for line in running_raw.splitlines() if line.strip()])
    return {"available": True, "failed": failed, "running": running}


# Prime cpu_percent counters so the first real reading is meaningful.
psutil.cpu_percent(interval=None)
psutil.cpu_percent(interval=None, percpu=True)
