"""SSH keys (Server → SSH keys): key pairs the app uses to connect to other devices (Console → SSH tabs), and
the devices it already knows (known_hosts).

Keys live in <data>/ssh/keys/<name> (private key, mode 0600, readable only by the app's user) + <name>.pub,
with <name>.json for the description and creation time. They're made with ssh-keygen (openssh-client).
Keys made here have no passphrase (the file permissions protect them, and the Console can use them without
asking); an imported key may have one, and then ssh asks for it in the Console.

"Install on a device" opens an SSH Console tab that appends the public key to the device's
~/.ssh/authorized_keys (you sign in once with the device's password, like ssh-copy-id).

Known hosts are the app user's ~/.ssh/known_hosts (what `ssh` itself uses); removing an entry lets a device
whose key changed (e.g. reinstalled) be accepted again.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import db
from .config import settings

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")
COMMENT_RE = re.compile(r"^[A-Za-z0-9 _.@:+=-]{0,100}$")
PUB_RE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|"
                    r"sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/=]{40,16000}( [^\r\n'\"\\`$]{0,200})?$")
TYPES = {"ed25519": {"label": "Ed25519 (recommended)", "bits": None},
         "rsa": {"label": "RSA 4096", "bits": 4096},
         "ecdsa": {"label": "ECDSA P-521", "bits": 521}}
DEFAULT_KEY_SETTING = "ssh_default_key"


class SshKeyError(RuntimeError):
    pass


def keygen() -> str | None:
    return shutil.which("ssh-keygen")


def keys_dir() -> Path:
    d = settings.data_dir / "ssh" / "keys"
    d.mkdir(parents=True, exist_ok=True)
    for p in (d.parent, d):
        try:
            p.chmod(0o700)
        except OSError:
            pass
    return d


def _need_keygen() -> str:
    k = keygen()
    if not k:
        raise SshKeyError("ssh-keygen isn't installed on this server (package openssh-client).")
    return k


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.fullmatch(name) or name.endswith((".pub", ".json")):
        raise SshKeyError("Key names use letters, digits, . _ and - (max 48), e.g. backup-server.")
    return name


def _paths(name: str) -> tuple[Path, Path, Path]:
    d = keys_dir()
    return d / name, d / f"{name}.pub", d / f"{name}.json"


def private_path(name: str) -> str:
    """For the Console: the private key file of a saved key."""
    priv, _pub, _m = _paths(_check_name(name))
    if not priv.is_file():
        raise SshKeyError(f"There's no SSH key called {name} (any more).")
    return str(priv)


def _run(argv: list[str], timeout: float = 30, input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input_text,
                              stdin=None if input_text is not None else subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SshKeyError(f"{Path(argv[0]).name} failed: {e}")


def _fingerprint(pub: Path) -> dict:
    """ssh-keygen -l: '256 SHA256:abc… comment (ED25519)'."""
    k = keygen()
    if not k:
        return {}
    r = _run([k, "-l", "-f", str(pub)], 10)
    m = re.match(r"^(\d+)\s+(\S+)\s+(.*)\s+\((\w[\w-]*)\)\s*$", r.stdout.strip())
    if r.returncode != 0 or not m:
        return {}
    return {"bits": int(m.group(1)), "fingerprint": m.group(2), "type": m.group(4).lower()}


def _meta(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(path: Path, meta: dict) -> None:
    path.write_text(json.dumps(meta, indent=1), encoding="utf-8")


def _info(name: str) -> dict | None:
    priv, pub, mpath = _paths(name)
    if not priv.is_file() or not pub.is_file():
        return None
    meta = _meta(mpath)
    try:
        public = pub.read_text(encoding="utf-8").strip()
    except OSError:
        public = ""
    parts = public.split(None, 2)
    return {"name": name, "public_key": public, "comment": parts[2] if len(parts) > 2 else "",
            "description": meta.get("description", ""), "created": meta.get("created"),
            "imported": bool(meta.get("imported")), "encrypted": bool(meta.get("encrypted")),
            "path": str(priv), **_fingerprint(pub)}


def list_keys() -> list[dict]:
    out = []
    for p in sorted(keys_dir().iterdir(), key=lambda x: x.name.lower()):
        if p.is_file() and NAME_RE.fullmatch(p.name) and not p.name.endswith((".pub", ".json")):
            i = _info(p.name)
            if i:
                out.append(i)
    return out


def default_key() -> str:
    name = db.get_setting(DEFAULT_KEY_SETTING) or ""
    return name if name and _paths(name)[0].is_file() else ""


def set_default(name: str) -> None:
    if name:
        private_path(name)
    db.set_setting(DEFAULT_KEY_SETTING, name or "")


def status() -> dict:
    keys = list_keys()
    return {"available": bool(keygen()), "ssh": bool(shutil.which("ssh")), "keys": keys, "default": default_key(),
            "types": [[k, v["label"]] for k, v in TYPES.items()], "dir": str(keys_dir()),
            "known_hosts": known_hosts(), "known_hosts_file": str(known_hosts_file())}


def _secure(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _clean_comment(comment: str) -> str:
    comment = " ".join((comment or "").split())
    if not COMMENT_RE.fullmatch(comment):
        raise SshKeyError("The comment can use letters, digits, spaces and . _ @ : + = - (max 100).")
    return comment


def generate(name: str, key_type: str = "ed25519", comment: str = "", description: str = "") -> dict:
    k = _need_keygen()
    name = _check_name(name)
    if key_type not in TYPES:
        raise SshKeyError("Pick a key type: Ed25519, RSA or ECDSA.")
    comment = _clean_comment(comment) or f"sysadmin-ia@{_host()}"
    priv, pub, mpath = _paths(name)
    if priv.exists() or pub.exists():
        raise SshKeyError(f"There's already a key called {name}.")
    argv = [k, "-q", "-t", key_type, "-N", "", "-C", comment, "-f", str(priv)]
    if TYPES[key_type]["bits"]:
        argv[4:4] = ["-b", str(TYPES[key_type]["bits"])]
    old = os.umask(0o077)
    try:
        r = _run(argv, 120)
    finally:
        os.umask(old)
    if r.returncode != 0 or not priv.is_file():
        for p in (priv, pub):
            p.unlink(missing_ok=True)
        raise SshKeyError(f"ssh-keygen failed: {(r.stderr or r.stdout).strip()[-300:]}")
    _secure(priv)
    _write_meta(mpath, {"description": _clean_desc(description), "created": time.time(), "type": key_type})
    return _info(name)


def _clean_desc(text: str) -> str:
    return " ".join((text or "").split())[:300]


def _host() -> str:
    try:
        from . import server_name
        return server_name.current() or "server"
    except Exception:  # noqa: BLE001
        return "server"


def import_key(name: str, private_key: str, public_key: str = "", description: str = "") -> dict:
    """Save an existing private key (OpenSSH or PEM). Its public key is worked out with ssh-keygen -y; for a
    key with a passphrase, paste the public key too (the passphrase is never asked for here)."""
    k = _need_keygen()
    name = _check_name(name)
    priv, pub, mpath = _paths(name)
    if priv.exists() or pub.exists():
        raise SshKeyError(f"There's already a key called {name}.")
    text = (private_key or "").replace("\r\n", "\n").strip() + "\n"
    if not re.match(r"^-----BEGIN (OPENSSH|RSA|EC|DSA)? ?PRIVATE KEY-----\n", text) or "-----END" not in text or len(text) > 20000:
        raise SshKeyError("Paste the whole private key, from -----BEGIN … PRIVATE KEY----- to -----END … PRIVATE KEY-----.")
    public_key = " ".join((public_key or "").split())
    if public_key and not PUB_RE.fullmatch(public_key):
        raise SshKeyError("That public key doesn't look right: it starts with ssh-ed25519, ssh-rsa or ecdsa-sha2-… .")
    old = os.umask(0o077)
    try:
        priv.write_text(text, encoding="utf-8")
    finally:
        os.umask(old)
    _secure(priv)
    try:
        r = _run([k, "-y", "-P", "", "-f", str(priv)], 20)      # empty passphrase: fails if the key has one
        encrypted = False
        if r.returncode == 0 and r.stdout.strip():
            derived = r.stdout.strip().split(None, 2)
            pasted = public_key.split(None, 2)
            if pasted and pasted[:2] != derived[:2]:
                raise SshKeyError("The public key you pasted doesn't belong to this private key.")
            comment = (pasted[2] if len(pasted) > 2 else derived[2] if len(derived) > 2 else "") or f"{name}@{_host()}"
            public_key = f"{derived[0]} {derived[1]} {comment}"
        else:
            err = (r.stderr or "").lower()
            if "incorrect passphrase" in err or "passphrase" in err:
                encrypted = True
                if not public_key:
                    raise SshKeyError("This key has a passphrase, so its public key can't be read from it. Paste the public "
                                      "key too (the .pub file).")
            else:
                raise SshKeyError(f"That private key can't be read: {(r.stderr or r.stdout).strip()[-200:] or 'invalid key'}")
        pub.write_text(public_key + "\n", encoding="utf-8")
        _write_meta(mpath, {"description": _clean_desc(description), "created": time.time(), "imported": True,
                            "encrypted": encrypted})
    except Exception:
        for p in (priv, pub, mpath):
            p.unlink(missing_ok=True)
        raise
    return _info(name)


def update(name: str, new_name: str = "", description: str | None = None) -> dict:
    name = _check_name(name)
    priv, pub, mpath = _paths(name)
    if not priv.is_file():
        raise SshKeyError(f"There's no SSH key called {name} (any more).")
    meta = _meta(mpath)
    if description is not None:
        meta["description"] = _clean_desc(description)
    _write_meta(mpath, meta)
    new_name = (new_name or "").strip()
    if new_name and new_name != name:
        new_name = _check_name(new_name)
        npriv, npub, nmeta = _paths(new_name)
        if npriv.exists() or npub.exists():
            raise SshKeyError(f"There's already a key called {new_name}.")
        priv.rename(npriv)
        pub.rename(npub)
        mpath.rename(nmeta)
        if db.get_setting(DEFAULT_KEY_SETTING) == name:
            db.set_setting(DEFAULT_KEY_SETTING, new_name)
        from . import devices
        devices.key_renamed(name, new_name)        # devices that sign in with it follow the new name
        name = new_name
    return _info(name)


def delete(name: str) -> None:
    name = _check_name(name)
    priv, pub, mpath = _paths(name)
    if not priv.exists() and not pub.exists():
        raise SshKeyError(f"There's no SSH key called {name} (any more).")
    from . import devices
    users = devices.key_users(name)
    if users:
        raise SshKeyError(f"{name} is used to sign in to: {', '.join(users)}. Choose another key for "
                          "those devices in Server → Devices first.")
    for p in (priv, pub, mpath):
        p.unlink(missing_ok=True)
    if db.get_setting(DEFAULT_KEY_SETTING) == name:
        db.set_setting(DEFAULT_KEY_SETTING, "")


def public_key(name: str) -> str:
    _priv, pub, _m = _paths(_check_name(name))
    try:
        return pub.read_text(encoding="utf-8").strip()
    except OSError:
        raise SshKeyError(f"There's no SSH key called {name} (any more).")


def private_key(name: str) -> str:
    return Path(private_path(name)).read_text(encoding="utf-8")


def install_command(name: str) -> str:
    """The remote command that adds the public key to ~/.ssh/authorized_keys on a device (like ssh-copy-id),
    run in an SSH Console tab. The key is checked against PUB_RE, so it has no quotes or shell characters."""
    key = public_key(name)
    if not PUB_RE.fullmatch(key):
        raise SshKeyError("This key's public key has characters that can't be sent safely.")
    return ("umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
            f"if grep -qF '{key.split()[1]}' ~/.ssh/authorized_keys; then echo 'This key was already installed.'; "
            f"else printf '%s\\n' '{key}' >> ~/.ssh/authorized_keys && echo 'Key installed: you can now sign in with it.'; fi")


# ---------------------------------------------------------------- known hosts (~/.ssh/known_hosts of the app's user)
def known_hosts_file() -> Path:
    try:
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        home = os.path.expanduser("~")
    if not home or not os.path.isdir(home):
        home = str(settings.data_dir)
    return Path(home) / ".ssh" / "known_hosts"


def known_hosts() -> list[dict]:
    f = known_hosts_file()
    k = keygen()
    if not f.is_file() or not k:
        return []
    r = _run([k, "-l", "-f", str(f)], 20)
    out, seen = [], set()
    for line in r.stdout.splitlines():
        m = re.match(r"^(\d+)\s+(\S+)\s+(\S+)\s+\((\w[\w-]*)\)\s*$", line.strip())
        if not m:
            continue
        hosts = m.group(3)
        key = (hosts, m.group(2))
        if key in seen:
            continue
        seen.add(key)
        hashed = hosts.startswith("|1|")
        out.append({"hosts": "" if hashed else hosts, "hashed": hashed, "fingerprint": m.group(2),
                    "type": m.group(4).lower(), "bits": int(m.group(1))})
    return out


HOST_RE = re.compile(r"^(\[[A-Za-z0-9.:-]{1,253}\]:\d{1,5}|[A-Za-z0-9.:-]{1,253})$")


def forget_host(host: str) -> int:
    """ssh-keygen -R: remove every key of that host (a name, an address, or [host]:port)."""
    k = _need_keygen()
    host = (host or "").strip()
    if not HOST_RE.fullmatch(host) or host.startswith("-"):
        raise SshKeyError("Enter a host name or address, e.g. 192.168.1.20 or [nas.local]:2222.")
    f = known_hosts_file()
    if not f.is_file():
        raise SshKeyError("No device is known yet.")
    before = len(known_hosts())
    r = _run([k, "-R", host, "-f", str(f)], 20)
    if r.returncode != 0:
        raise SshKeyError(f"ssh-keygen couldn't remove it: {(r.stderr or r.stdout).strip()[-200:]}")
    Path(str(f) + ".old").unlink(missing_ok=True)     # ssh-keygen keeps a backup copy
    return before - len(known_hosts())
