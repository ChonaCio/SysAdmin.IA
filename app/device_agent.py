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


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    fn = {"info": info, "stats": stats, "ports": ports}.get(cmd)
    if not fn:
        print(json.dumps({"error": "unknown command " + cmd}))
        sys.exit(2)
    print(json.dumps(fn(), default=str))


if __name__ == "__main__":
    main()
