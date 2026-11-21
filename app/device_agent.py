"""SysAdmin.IA device agent: sent over SSH to a device and run there as `python3 - COMMAND`.

Nothing is installed on the device: SysAdmin.IA pipes this file into the device's python3 on
every call, so it always matches the app's version. It only reads system information and
prints one JSON object. Standard library only, and compatible with Python 3.5+ (no f-strings),
because devices may run old distributions.

Commands:
  info   – host name, OS, kernel, CPU, memory, uptime, GPUs, and what's installed
           (Python, Docker, Ollama, systemd, sudo without a password)
  stats  – CPU % (sampled over ~0.5 s), load, memory, swap, network and disk rates, temperatures,
           and the mounted drives with their usage
  ports  – TCP ports listening on the device (from /proc/net/tcp*)
  ls PATH HIDDEN / sizes PATH – a folder's entries / the size of each item (Drives → browse)
  blockdevs – lsblk + usage + disk I/O counters (the Drives page on a device)
  procs SORT LIMIT – processes with CPU %, memory, user and command line (the Processes page)
  pyenvs WITH_SIZE – Python environments in ~/.sysadmin-ia/pyenvs, Python versions, uv
"""
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time

SKIP_FS = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "pstore", "bpf", "securityfs",
           "debugfs", "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs", "autofs", "binfmt_misc", "overlay",
           "squashfs", "nsfs", "ramfs", "efivarfs", "rpc_pipefs", "fuse.lxcfs", "fuse.gvfsd-fuse", "fuse.portal"}


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except (OSError, IOError):
        return default


def _run(argv, timeout=8):
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                           universal_newlines=True)
        return p.returncode, p.stdout.strip()
    except Exception:  # noqa: BLE001 - missing program, timeout…
        return None, ""


def _which(name):
    return shutil.which(name) or next((p for p in ("/usr/local/bin/" + name, "/usr/bin/" + name, "/snap/bin/" + name)
                                       if os.path.exists(p)), None)


def _meminfo():
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts and parts[0].isdigit():
            out[k.strip()] = int(parts[0]) * 1024
    return out


def _os_name():
    rel = {}
    for line in _read("/etc/os-release").splitlines():
        k, _, v = line.partition("=")
        rel[k] = v.strip().strip('"')
    return rel.get("PRETTY_NAME") or (platform.system() + " " + platform.release())


def _cpu_model():
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith(("model name", "hardware", "cpu model")):
            return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine()


def _uptime():
    try:
        return float(_read("/proc/uptime").split()[0])
    except (IndexError, ValueError):
        return None


def _service(name):
    if not _which("systemctl"):
        return "unknown"
    code, out = _run(["systemctl", "is-active", name], 5)
    return out.splitlines()[-1] if out else "unknown"


def _gpus():
    out = []
    if _which("nvidia-smi"):
        code, text = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
        if code == 0:
            out += ["{} ({})".format(*[x.strip() for x in line.split(",", 1)]) for line in text.splitlines() if "," in line]
    if not out and os.path.exists("/dev/kfd"):
        out.append("AMD GPU (ROCm)")
    return out


def _docker():
    exe = _which("docker")
    d = {"installed": bool(exe), "version": None, "service": _service("docker"), "access": False, "compose": None}
    sock = "/var/run/docker.sock"
    if os.path.exists(sock):
        d["installed"] = True
        d["access"] = os.access(sock, os.R_OK | os.W_OK)
    if exe:
        code, out = _run([exe, "version", "--format", "{{.Server.Version}}"], 8)
        if code == 0 and out and "\n" not in out:
            d["version"] = out
        else:
            code, out = _run([exe, "--version"], 5)
            m = re.search(r"(\d+\.\d+\.\d+)", out or "")
            d["version"] = m.group(1) if m else None
        code, out = _run([exe, "compose", "version", "--short"], 8)
        d["compose"] = out if code == 0 and out else None
    try:
        import grp
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
        g = grp.getgrnam("docker")
        d["group"] = user in g.gr_mem or os.getuid() == 0
    except (KeyError, ImportError):
        d["group"] = False
    return d


def _ollama():
    exe = _which("ollama")
    o = {"installed": bool(exe), "version": None, "running": False, "service": _service("ollama"),
         "models_dir": None, "context_length": None}
    try:
        from urllib.request import urlopen
        with urlopen("http://127.0.0.1:11434/api/version", timeout=2) as r:
            o["version"] = json.loads(r.read().decode()).get("version")
            o["running"] = True
            o["installed"] = True
    except Exception:  # noqa: BLE001 - not running
        pass
    for d in (os.environ.get("OLLAMA_MODELS"), "/usr/share/ollama/.ollama/models", "/var/lib/ollama/models",
              os.path.expanduser("~/.ollama/models")):
        if d and os.path.isdir(d):
            o["models_dir"] = d
            break
    m = re.search(r"OLLAMA_CONTEXT_LENGTH=(\d+)", _read("/etc/systemd/system/ollama.service.d/override.conf"))
    if m:
        o["context_length"] = int(m.group(1))
    return o


def info():
    mem = _meminfo()
    code, _ = _run(["sudo", "-n", "true"], 5) if _which("sudo") else (None, "")
    return {
        "hostname": socket.gethostname(),
        "os": _os_name(), "kernel": platform.release(), "arch": platform.machine(),
        "cpu_model": _cpu_model(), "cpus": os.cpu_count(),
        "mem_total": mem.get("MemTotal"), "uptime": _uptime(),
        "python": platform.python_version(), "python_path": sys.executable,
        "user": os.environ.get("USER") or os.environ.get("LOGNAME") or "", "uid": os.getuid(),
        "home": os.path.expanduser("~"),
        "systemd": bool(_which("systemctl")),
        "sudo": "root" if os.getuid() == 0 else ("nopasswd" if code == 0 else ("password" if _which("sudo") else "none")),
        "bash": bool(_which("bash")), "curl": bool(_which("curl")), "wget": bool(_which("wget")),
        "gpus": _gpus(),
        "docker": _docker(),
        "ollama": _ollama(),
        "agent_time": time.time(),
    }


def _cpu_times():
    line = _read("/proc/stat").splitlines()[0].split()
    vals = [int(x) for x in line[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def _net():
    rx = tx = 0
    for line in _read("/proc/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if name == "lo" or name.startswith(("veth", "docker", "br-", "virbr")):
            continue
        f = rest.split()
        if len(f) >= 9:
            rx += int(f[0])
            tx += int(f[8])
    return rx, tx


def _disk_io():
    r = w = 0
    for line in _read("/proc/diskstats").splitlines():
        f = line.split()
        if len(f) < 10 or re.match(r"^(loop|ram|dm-|sr|zram)", f[2]) or re.match(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+)\d+$", f[2]) \
                or re.match(r"^(nvme\d+n\d+|mmcblk\d+)p\d+$", f[2]):
            continue          # whole disks only (partitions would count twice)
        r += int(f[5]) * 512
        w += int(f[9]) * 512
    return r, w


def _temps():
    out = []
    base = "/sys/class/hwmon"
    try:
        mons = sorted(os.listdir(base))
    except OSError:
        mons = []
    for m in mons:
        name = _read(os.path.join(base, m, "name")).strip() or m
        try:
            files = sorted(os.listdir(os.path.join(base, m)))
        except OSError:
            continue
        for fn in files:
            if fn.startswith("temp") and fn.endswith("_input"):
                try:
                    v = int(_read(os.path.join(base, m, fn)).strip()) / 1000.0
                except ValueError:
                    continue
                if -30 < v < 150:
                    label = _read(os.path.join(base, m, fn.replace("_input", "_label"))).strip()
                    out.append({"sensor": name, "label": label or fn.replace("_input", ""), "c": round(v, 1)})
    if not out:
        for z in sorted(os.listdir("/sys/class/thermal")) if os.path.isdir("/sys/class/thermal") else []:
            if z.startswith("thermal_zone"):
                try:
                    v = int(_read("/sys/class/thermal/{}/temp".format(z)).strip()) / 1000.0
                except ValueError:
                    continue
                out.append({"sensor": _read("/sys/class/thermal/{}/type".format(z)).strip() or z, "label": z, "c": round(v, 1)})
    return out[:16]


def _unescape(path):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)


def drives():
    seen, out = set(), []
    for line in _read("/proc/mounts").splitlines():
        f = line.split()
        if len(f) < 4:
            continue
        dev, mp, fs = f[0], _unescape(f[1]), f[2]
        if fs in SKIP_FS or mp.startswith(("/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker", "/boot/efi")):
            continue
        if not dev.startswith("/") and fs not in ("nfs", "nfs4", "cifs", "smb3", "zfs", "btrfs", "fuse.sshfs"):
            continue
        if dev in seen and fs not in ("nfs", "nfs4", "cifs"):
            continue          # bind mounts of the same device
        seen.add(dev)
        try:
            st = os.statvfs(mp)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if not total:
            continue
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        out.append({"device": dev, "mount": mp, "fstype": fs, "total": total, "used": used, "free": free,
                    "percent": round(used * 100.0 / max(1, used + free), 1), "readonly": "ro" in f[3].split(",")})
    out.sort(key=lambda d: (d["mount"] != "/", d["mount"]))
    return out


def stats():
    c1, i1 = _cpu_times()
    n1 = _net()
    d1 = _disk_io()
    t1 = time.time()
    time.sleep(0.5)
    c2, i2 = _cpu_times()
    n2 = _net()
    d2 = _disk_io()
    dt = max(0.01, time.time() - t1)
    mem = _meminfo()
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", mem.get("MemFree", 0))
    swap_t, swap_f = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
    try:
        load = [float(x) for x in _read("/proc/loadavg").split()[:3]]
    except ValueError:
        load = []
    return {
        "time": time.time(),
        "cpu": round(100.0 * (1 - (i2 - i1) / float(max(1, c2 - c1))), 1),
        "cpus": os.cpu_count(), "load": load,
        "mem": {"total": total, "available": avail, "used": total - avail,
                "percent": round((total - avail) * 100.0 / max(1, total), 1)},
        "swap": {"total": swap_t, "used": swap_t - swap_f, "percent": round((swap_t - swap_f) * 100.0 / max(1, swap_t), 1) if swap_t else 0},
        "net": {"rx_rate": max(0, (n2[0] - n1[0]) / dt), "tx_rate": max(0, (n2[1] - n1[1]) / dt), "rx": n2[0], "tx": n2[1]},
        "disk_io": {"read_rate": max(0, (d2[0] - d1[0]) / dt), "write_rate": max(0, (d2[1] - d1[1]) / dt)},
        "uptime": _uptime(),
        "temps": _temps(),
        "drives": drives(),
        "procs": len([p for p in os.listdir("/proc") if p.isdigit()]),
    }


def ports():
    found = {}
    for fn in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in _read(fn).splitlines()[1:]:
            f = line.split()
            if len(f) > 3 and f[3] == "0A":             # LISTEN
                port = int(f[1].rsplit(":", 1)[1], 16)
                found[port] = True
    return {"ports": sorted(found)}


# ---------------------------------------------------------------- files (Drives → browse on a device)

MAX_ENTRIES = 3000


def _clean_path(path):
    return os.path.normpath("/" + (path or "/").lstrip("/"))


def _statvfs_usage(path):
    try:
        st = os.statvfs(path)
    except (OSError, AttributeError):
        return None
    total = st.f_blocks * st.f_frsize
    if not total:
        return None
    free = st.f_bavail * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    return {"total": total, "used": used, "free": free, "percent": round(used * 100.0 / max(1, used + free), 1)}


def ls(path="/", hidden="0"):
    import stat as st_
    path = _clean_path(path)
    if not os.path.exists(path):
        return {"error": "That folder doesn't exist."}
    if not os.path.isdir(path):
        return {"error": "That's not a folder."}
    entries, truncated = [], False
    try:
        names = os.listdir(path)
    except OSError:
        return {"error": "Permission denied: the device's user can't read this folder."}
    for n in names:
        if hidden != "1" and n.startswith("."):
            continue
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
        p = os.path.join(path, n)
        try:
            s = os.stat(p)
            is_dir = st_.S_ISDIR(s.st_mode)
            size, mtime = (None if is_dir else s.st_size), s.st_mtime
        except OSError:
            is_dir, size, mtime = os.path.isdir(p), None, None
        entries.append({"name": n, "dir": is_dir, "link": os.path.islink(p), "size": size, "mtime": mtime})
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"path": path, "parent": None if path == "/" else os.path.dirname(path), "entries": entries,
            "truncated": truncated, "usage": _statvfs_usage(path)}


def sizes(path="/"):
    import stat as st_
    path = _clean_path(path)
    if not os.path.isdir(path):
        return {"error": "That's not a folder."}
    dev = os.stat(path).st_dev
    deadline = time.time() + 15
    out, complete = {}, True

    def du(top):
        total, stack = 0, [top]
        while stack:
            if time.time() > deadline:
                return total, False
            cur = stack.pop()
            try:
                names = os.listdir(cur)
            except OSError:
                continue
            for n in names:
                p = os.path.join(cur, n)
                try:
                    s = os.lstat(p)
                except OSError:
                    continue
                if st_.S_ISDIR(s.st_mode):
                    if s.st_dev == dev:
                        stack.append(p)
                else:
                    total += getattr(s, "st_blocks", 0) * 512 or s.st_size
        return total, True
    try:
        names = os.listdir(path)
    except OSError:
        return {"error": "Permission denied: the device's user can't read this folder."}
    for n in names:
        if time.time() > deadline:
            complete = False
            break
        p = os.path.join(path, n)
        try:
            s = os.lstat(p)
        except OSError:
            continue
        if st_.S_ISDIR(s.st_mode):
            if s.st_dev == dev:
                out[n], ok = du(p)
                complete = complete and ok
            else:
                out[n] = 0
        else:
            out[n] = s.st_size
    return {"path": path, "sizes": out, "complete": complete}


# ---------------------------------------------------------------- drives (Drives page on a device)

def blockdevs():
    """lsblk's JSON (same columns as the app's Drives page, with fallbacks for old util-linux), the usage of every
    mount point, and disk I/O counters."""
    exe = _which("lsblk") or ("/bin/lsblk" if os.path.exists("/bin/lsblk") else None)
    full = ("NAME,KNAME,PATH,SIZE,TYPE,FSTYPE,FSVER,LABEL,PARTLABEL,UUID,MOUNTPOINTS,MODEL,VENDOR,SERIAL,TRAN,RM,"
            "HOTPLUG,RO,ROTA,PKNAME")
    devs, err = None, "lsblk isn't installed on the device (package util-linux)."
    if exe:
        for cols in (full, full.replace("MOUNTPOINTS", "MOUNTPOINT"),
                     "NAME,KNAME,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT,MODEL,VENDOR,SERIAL,TRAN,RM,RO,ROTA"):
            try:
                p = subprocess.run([exe, "-J", "-b", "-o", cols], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   universal_newlines=True, timeout=15)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                break
            if p.returncode == 0:
                try:
                    devs = json.loads(p.stdout).get("blockdevices", [])
                    break
                except ValueError:
                    err = "lsblk's answer can't be read."
            else:
                err = (p.stderr or "").strip()[-300:] or "lsblk failed."
    usage = {}

    def walk(items):
        for d in items or []:
            mps = d.get("mountpoints")
            if mps is None:
                mps = [d.get("mountpoint")]
            for m in mps:
                if m and m != "[SWAP]" and m not in usage:
                    usage[m] = _statvfs_usage(m)
            walk(d.get("children"))
    walk(devs)
    io = {}
    for line in _read("/proc/diskstats").splitlines():
        f = line.split()
        if len(f) >= 10:
            io[f[2]] = [int(f[5]) * 512, int(f[9]) * 512]
    return {"blockdevices": devs, "error": None if devs is not None else err, "usage": usage, "io": io,
            "time": time.time()}


# ---------------------------------------------------------------- processes (Processes page on a device)

def _proc_user(uid, cache={}):  # noqa: B006 - small per-run cache
    if uid not in cache:
        try:
            import pwd
            cache[uid] = pwd.getpwuid(uid).pw_name
        except (KeyError, ImportError):
            cache[uid] = str(uid)
    return cache[uid]


def _proc_sample():
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        raw = _read("/proc/" + pid + "/stat")
        r = raw.rfind(")")
        if r < 0:
            continue
        f = raw[r + 2:].split()
        try:
            out[int(pid)] = (int(f[11]) + int(f[12]), raw[raw.find("(") + 1:r], f[0], int(f[19]), int(f[21]))
        except (IndexError, ValueError):
            continue
    return out


def procs(sort="cpu", limit="60"):
    """The device's processes: CPU % over ~0.5 s, memory, user, command line (like the app's Monitor)."""
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    c1, _i = _cpu_times()
    a = _proc_sample()
    time.sleep(0.5)
    c2, _i = _cpu_times()
    b = _proc_sample()
    total_ticks = max(1, c2 - c1)
    ncpu = os.cpu_count() or 1
    mem_total = _meminfo().get("MemTotal", 1)
    boot = time.time() - (_uptime() or 0)
    me = os.getpid()
    rows = []
    for pid, (ticks, name, state, threads, start) in b.items():
        if pid == me or pid == os.getppid():
            continue
        prev = a.get(pid)
        cpu = (ticks - prev[0]) * 100.0 * ncpu / total_ticks if prev else 0.0
        rss = 0
        try:
            rss = int(_read("/proc/%d/statm" % pid).split()[1]) * page
        except (IndexError, ValueError):
            pass
        try:
            uid = os.stat("/proc/%d" % pid).st_uid
        except OSError:
            continue
        cmd = _read("/proc/%d/cmdline" % pid).replace("\x00", " ").strip()
        rows.append({"pid": pid, "name": name, "user": _proc_user(uid), "uid": uid, "cpu": round(cpu, 1), "mem": rss,
                     "mem_percent": round(rss * 100.0 / mem_total, 1), "state": state, "threads": threads,
                     "started": boot + start / float(hz), "cmd": cmd[:400] or "[" + name + "]"})
    key = {"cpu": lambda r: -r["cpu"], "mem": lambda r: -r["mem"], "name": lambda r: r["name"].lower(),
           "pid": lambda r: r["pid"]}.get(sort, lambda r: -r["cpu"])
    rows.sort(key=key)
    try:
        n = max(1, min(500, int(limit)))
    except ValueError:
        n = 60
    return {"procs": rows[:n], "total": len(rows), "uid": os.getuid(), "time": time.time()}


# ---------------------------------------------------------------- Python environments (on a device)

SAI_HOME = "~/.sysadmin-ia"
ENV_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")


def _py_version(exe):
    code, out = _run([exe, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"], 10)
    return out if code == 0 and re.match(r"^\d+\.\d+\.\d+$", out or "") else None


def _cfg(d):
    out = {}
    for line in _read(os.path.join(d, "pyvenv.cfg")).splitlines():
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _du(d):
    total = 0
    for dirpath, _dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def pyenvs(with_size="1"):
    base = os.path.expanduser(SAI_HOME)
    root, pyd = os.path.join(base, "pyenvs"), os.path.join(base, "pythons")
    own_uv = os.path.join(base, "tools", "uv", "bin", "uv")
    envs = []
    for n in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        d = os.path.join(root, n)
        if not ENV_NAME_RE.match(n) or not os.path.isdir(d):
            continue
        try:
            with open(os.path.join(d, "meta.json")) as f:
                meta = json.load(f)
        except (OSError, IOError, ValueError):
            meta = {}
        cfg = _cfg(d)
        py = os.path.join(d, "bin", "python")
        envs.append({"name": n, "version": cfg.get("version") or cfg.get("version_info"), "path": d, "python": py,
                     "ok": os.path.exists(py), "description": meta.get("description", ""), "created": meta.get("created"),
                     "requested": meta.get("python", ""), "tool": meta.get("tool", ""), "home": cfg.get("home", ""),
                     "size": _du(d) if with_size == "1" else None})
    downloaded = []
    for n in sorted(os.listdir(pyd)) if os.path.isdir(pyd) else []:
        d = os.path.join(pyd, n)
        m = re.match(r"^[a-z]+-(3\.\d+\.\d+)([a-z0-9+]*)-", n)
        exe = next((x for x in (os.path.join(d, "bin", "python3"), os.path.join(d, "bin", "python")) if os.path.exists(x)), None)
        if os.path.isdir(d) and m and exe and n.startswith(("cpython-", "pypy-", "graalpy-")):
            downloaded.append({"path": exe, "version": m.group(1), "key": n, "dir": d, "downloaded": True,
                               "variant": "freethreaded" if m.group(2) in ("t", "+freethreaded") else "",
                               "implementation": n.split("-", 1)[0], "size": _du(d) if with_size == "1" else None,
                               "used_by": [e["name"] for e in envs if e["home"] and os.path.realpath(e["home"]).startswith(os.path.realpath(d))]})
    seen, interps = set(), []
    dirs = [x for x in os.environ.get("PATH", "").split(os.pathsep) if x] + ["/usr/bin", "/usr/local/bin"]
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not re.match(r"^python3(\.\d{1,2})?$", n):
                continue
            p = os.path.join(d, n)
            real = os.path.realpath(p)
            if real in seen or not os.access(p, os.X_OK):
                continue
            seen.add(real)
            v = _py_version(p)
            if v:
                interps.append({"path": p, "version": v, "downloaded": False})
    interps.sort(key=lambda x: [int(i) for i in x["version"].split(".")], reverse=True)
    uv = own_uv if os.path.exists(own_uv) else _which("uv")
    uv_version = None
    if uv:
        code, out = _run([uv, "--version"], 10)
        uv_version = out.split()[-1] if code == 0 and out else None
    venv_ok = _run([sys.executable, "-c", "import ensurepip, venv"], 10)[0] == 0
    return {"root": root, "pythons_dir": pyd, "envs": envs, "downloaded": downloaded,
            "interpreters": downloaded + interps, "uv": uv_version, "uv_path": uv, "venv": venv_ok,
            "base": {"path": sys.executable, "version": platform.python_version()}}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    fn = {"info": info, "stats": stats, "ports": ports, "ls": ls, "sizes": sizes, "blockdevs": blockdevs,
          "procs": procs, "pyenvs": pyenvs}.get(cmd)
    if not fn:
        print(json.dumps({"error": "unknown command " + cmd}))
        sys.exit(2)
    print(json.dumps(fn(*sys.argv[2:4]), default=str))


if __name__ == "__main__":
    main()
