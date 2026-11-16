"""Drive management: detect disks and volumes, usage and I/O metrics, mount/unmount, browse.

Detection uses `lsblk` (util-linux) plus psutil for usage and I/O. Mounting and unmounting
need root, so they go through the root helper (scripts/sysadmin-ia-hostname: `mount`,
`unmount`), which only mounts under /mnt/<name> with nosuid,nodev and only unmounts what is
under /mnt, /media or /run/media. Browsing runs as the service user, so it shows what that
user is allowed to read.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import psutil

from . import server_name

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MOUNT_BASE = "/mnt"
MANAGED_PREFIXES = ("/mnt/", "/media/", "/run/media/")
SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "/opt", "/srv", "/tmp", "[SWAP]"}
MOUNTABLE_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat", "ntfs", "ntfs3", "f2fs",
                "iso9660", "udf", "hfsplus", "jfs", "reiserfs"}
EXTERNAL_TRANSPORTS = {"usb", "ieee1394", "mmc", "sdio"}

_LSBLK_COLS = ("NAME,KNAME,PATH,SIZE,TYPE,FSTYPE,FSVER,LABEL,PARTLABEL,UUID,MOUNTPOINTS,MODEL,VENDOR,"
               "SERIAL,TRAN,RM,HOTPLUG,RO,ROTA,PKNAME")

_io_prev: dict[str, tuple[float, int, int]] = {}


class DriveError(RuntimeError):
    pass


def available() -> bool:
    return sys.platform.startswith("linux") and shutil.which("lsblk") is not None


# ---------------------------------------------------------------- detection

def _lsblk() -> list[dict]:
    cols = _LSBLK_COLS
    for attempt in range(2):
        r = subprocess.run(["lsblk", "-J", "-b", "-o", cols], capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return json.loads(r.stdout).get("blockdevices", [])
        # util-linux < 2.37 has no MOUNTPOINTS (plural)
        cols = cols.replace("MOUNTPOINTS", "MOUNTPOINT")
    raise DriveError(f"lsblk failed: {r.stderr.strip()}")


def _flag(v) -> bool:
    return v in (True, 1, "1", "true")


def _mounts(dev: dict) -> list[str]:
    mps = dev.get("mountpoints")
    if mps is None:
        mps = [dev.get("mountpoint")]
    return [m for m in mps if m]


def _usage(mountpoint: str) -> dict | None:
    if not mountpoint or mountpoint == "[SWAP]":
        return None
    try:
        u = psutil.disk_usage(mountpoint)
    except OSError:
        return None
    return {"total": u.total, "used": u.used, "free": u.free, "percent": u.percent}


def _volume(dev: dict) -> dict:
    mounts = _mounts(dev)
    primary = sorted(mounts, key=len)[0] if mounts else None
    fstype = dev.get("fstype") or ""
    system = any(m in SYSTEM_MOUNTS for m in mounts)
    managed = bool(primary) and primary.startswith(MANAGED_PREFIXES) and not system
    label = dev.get("label") or dev.get("partlabel") or ""
    return {
        "name": dev.get("name"),
        "path": dev.get("path") or f"/dev/{dev.get('kname') or dev.get('name')}",
        "type": dev.get("type"),
        "size": int(dev.get("size") or 0),
        "fstype": fstype,
        "label": label,
        "uuid": dev.get("uuid") or "",
        "readonly": _flag(dev.get("ro")),
        "mountpoints": mounts,
        "mountpoint": primary,
        "usage": _usage(primary) if primary else None,
        "system": system or fstype == "swap",
        "swap": fstype == "swap",
        "can_mount": not mounts and fstype in MOUNTABLE_FS,
        "can_unmount": managed,
        "default_name": default_mount_name(dev, label),
        "note": _volume_note(fstype, mounts),
    }


def _volume_note(fstype: str, mounts: list[str]) -> str:
    if mounts:
        return ""
    if not fstype:
        return "No filesystem (unformatted or unknown)."
    if fstype == "swap":
        return "Swap space."
    if fstype == "crypto_LUKS":
        return "Encrypted (LUKS): unlock it on the console first."
    if fstype == "LVM2_member":
        return "Part of an LVM volume group."
    if fstype == "linux_raid_member":
        return "Part of a RAID array."
    if fstype not in MOUNTABLE_FS:
        return f"Filesystem '{fstype}' can't be mounted from here."
    return ""


def default_mount_name(dev: dict, label: str = "") -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-._") if label else ""
    if not base:
        model = re.sub(r"[^A-Za-z0-9]+", "-", (dev.get("model") or "")).strip("-").lower()
        base = f"{model[:20]}-{dev.get('name')}" if model else f"disk-{dev.get('name')}"
    return base[:64] or "disk"


def _walk_volumes(dev: dict, out: list[dict], parent_model: str = "") -> None:
    for child in dev.get("children") or []:
        child.setdefault("model", parent_model)
        if child.get("fstype") or child.get("type") in ("part", "lvm", "crypt", "raid1", "raid5", "md"):
            out.append(_volume(child))
        _walk_volumes(child, out, parent_model)


def _io_rates() -> dict[str, dict]:
    now = time.time()
    try:
        counters = psutil.disk_io_counters(perdisk=True) or {}
    except Exception:
        counters = {}
    rates = {}
    for name, c in counters.items():
        prev = _io_prev.get(name)
        r = w = 0.0
        if prev and now > prev[0]:
            dt = now - prev[0]
            r = max(0, c.read_bytes - prev[1]) / dt
            w = max(0, c.write_bytes - prev[2]) / dt
        _io_prev[name] = (now, c.read_bytes, c.write_bytes)
        rates[name] = {"read_rate": r, "write_rate": w, "read_total": c.read_bytes, "write_total": c.write_bytes}
    return rates


_manage_cache: tuple[float, bool] = (0.0, False)


def _can_manage() -> bool:
    """Whether the root helper can mount (cached: the page refreshes every few seconds)."""
    global _manage_cache
    checked, ok = _manage_cache
    if time.time() - checked < (300 if ok else 10):
        return ok
    ok = server_name.helper_supports("mount")
    _manage_cache = (time.time(), ok)
    return ok


def list_drives() -> dict:
    if not available():
        return {"available": False, "disks": [], "summary": None, "can_manage": False,
                "setup_command": None, "message": "Drive management is only available when the app runs on Linux."}
    io = _io_rates()
    disks = []
    for dev in _lsblk():
        dtype = dev.get("type")
        name = dev.get("name") or ""
        if dtype in ("loop", "ram") or name.startswith(("zram", "loop", "ram")):
            continue
        if dtype == "rom" and not int(dev.get("size") or 0):
            continue  # empty optical drive
        tran = (dev.get("tran") or "").lower()
        external = tran in EXTERNAL_TRANSPORTS or _flag(dev.get("rm")) or _flag(dev.get("hotplug"))
        volumes: list[dict] = []
        if dev.get("fstype"):  # filesystem directly on the disk (e.g. a USB stick without partitions)
            volumes.append(_volume(dev))
        _walk_volumes(dev, volumes, dev.get("model") or "")
        disks.append({
            "name": name,
            "path": dev.get("path") or f"/dev/{name}",
            "model": " ".join(filter(None, [(dev.get("vendor") or "").strip(), (dev.get("model") or "").strip()])) or name,
            "serial": dev.get("serial") or "",
            "size": int(dev.get("size") or 0),
            "type": dtype,
            "transport": tran or ("optical" if dtype == "rom" else ""),
            "external": external,
            "removable": _flag(dev.get("rm")),
            "media": "optical" if dtype == "rom" else ("hdd" if _flag(dev.get("rota")) else "ssd"),
            "readonly": _flag(dev.get("ro")),
            "system": any(v["system"] for v in volumes),
            "volumes": volumes,
            "io": io.get(name),
        })
    disks.sort(key=lambda d: (d["external"], not d["system"], d["name"]))

    seen, total, used = set(), 0, 0
    for d in disks:
        for v in d["volumes"]:
            u = v["usage"]
            key = v["uuid"] or v["path"]
            if u and key not in seen:
                seen.add(key)
                total += u["total"]; used += u["used"]
    can_manage = _can_manage()
    return {
        "available": True,
        "disks": disks,
        "summary": {
            "disks": len(disks),
            "internal": sum(1 for d in disks if not d["external"]),
            "external": sum(1 for d in disks if d["external"]),
            "capacity": sum(d["size"] for d in disks),
            "mounted_total": total, "mounted_used": used, "mounted_free": max(0, total - used),
        },
        "can_manage": can_manage,
        "setup_command": None if can_manage else server_name.setup_command(),
        "mount_base": MOUNT_BASE,
    }


# ---------------------------------------------------------------- mount / unmount (root helper)

def _find_volume(device: str) -> dict:
    for d in list_drives()["disks"]:
        for v in d["volumes"]:
            if v["path"] == device:
                return v
    raise DriveError("That drive is no longer connected. Refresh the list.")


def mount(device: str, name: str, persist: bool = False, readonly: bool = False) -> dict:
    name = (name or "").strip()
    if not NAME_RE.fullmatch(name):
        raise ValueError("Names use 1–64 letters, digits, dots, dashes or underscores (no spaces).")
    v = _find_volume(device)
    if not v["can_mount"]:
        raise DriveError(v["note"] or "This volume can't be mounted (it may already be mounted).")
    target = f"{MOUNT_BASE}/{name}"
    if os.path.ismount(target):
        raise DriveError(f"{target} is already in use. Choose another name.")
    if os.path.isdir(target) and any(os.scandir(target)):
        raise DriveError(f"{target} already exists and isn't empty. Choose another name.")
    args = ["mount", device, name]
    if persist:
        args.append("--persist")
    if readonly:
        args.append("--readonly")
    out = server_name.run_helper(*args, timeout=90)
    return {"mountpoint": target, "output": out}


def unmount(mountpoint: str, forget: bool = True) -> dict:
    mp = os.path.realpath(mountpoint or "")
    if not mp.startswith(MANAGED_PREFIXES):
        raise DriveError("Only drives mounted under /mnt, /media or /run/media can be unmounted here.")
    args = ["unmount", mp] + (["--forget"] if forget else [])
    out = server_name.run_helper(*args, timeout=90)
    return {"mountpoint": mp, "output": out}


# ---------------------------------------------------------------- browsing (as the service user)

MAX_ENTRIES = 3000


def _clean(path: str) -> str:
    path = os.path.normpath("/" + (path or "/").lstrip("/")) if not sys.platform.startswith("win") else (path or "/")
    return path


def list_dir(path: str, show_hidden: bool = False) -> dict:
    path = _clean(path)
    p = Path(path)
    if not p.exists():
        raise DriveError("That folder doesn't exist.")
    if not p.is_dir():
        raise DriveError("That's not a folder.")
    entries, truncated = [], False
    try:
        with os.scandir(p) as it:
            for e in it:
                if not show_hidden and e.name.startswith("."):
                    continue
                if len(entries) >= MAX_ENTRIES:
                    truncated = True
                    break
                try:
                    st = e.stat(follow_symlinks=True)
                    is_dir = stat.S_ISDIR(st.st_mode)
                    size, mtime = (None if is_dir else st.st_size), st.st_mtime
                except OSError:
                    is_dir, size, mtime = e.is_dir(follow_symlinks=False), None, None
                entries.append({"name": e.name, "dir": is_dir, "link": e.is_symlink(),
                                "size": size, "mtime": mtime})
    except PermissionError:
        raise DriveError("Permission denied: the app's user can't read this folder.")
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    try:
        u = psutil.disk_usage(path)
        usage = {"total": u.total, "used": u.used, "free": u.free, "percent": u.percent}
    except OSError:
        usage = None
    parent = None if path == "/" else str(p.parent)
    return {"path": path, "parent": parent, "entries": entries, "truncated": truncated, "usage": usage}


def folder_sizes(path: str, budget: float = 15.0) -> dict:
    """Total size of each item in a folder (stays on the same filesystem; time-limited)."""
    path = _clean(path)
    root = Path(path)
    if not root.is_dir():
        raise DriveError("That's not a folder.")
    dev = root.stat().st_dev
    deadline = time.monotonic() + budget

    def same_fs(st) -> bool:  # don't count other drives mounted inside this folder
        return not st.st_dev or not dev or st.st_dev == dev
    sizes: dict[str, int] = {}
    complete = True

    def du(p: str) -> int:
        nonlocal complete
        total = 0
        stack = [p]
        while stack:
            if time.monotonic() > deadline:
                complete = False
                return total
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for e in it:
                        try:
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISDIR(st.st_mode):
                            if same_fs(st):
                                stack.append(e.path)
                        else:
                            total += st.st_blocks * 512 if hasattr(st, "st_blocks") else st.st_size
            except OSError:
                continue
        return total

    try:
        children = list(os.scandir(root))
    except PermissionError:
        raise DriveError("Permission denied: the app's user can't read this folder.")
    for e in children:
        if time.monotonic() > deadline:
            complete = False
            break
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            sizes[e.name] = du(e.path) if same_fs(st) else 0
        else:
            sizes[e.name] = st.st_size
    return {"path": path, "sizes": sizes, "complete": complete}


def file_for_download(path: str) -> Path:
    p = Path(_clean(path))
    if not p.is_file():
        raise DriveError("That's not a file.")
    if not os.access(p, os.R_OK):
        raise DriveError("Permission denied: the app's user can't read this file.")
    return p
