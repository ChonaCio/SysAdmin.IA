"""Self-update from GitHub.

Checks the GitHub repository (default ChonaCio/SysAdmin.IA) for a newer version and, on
request, downloads it and installs it over the running app, the same way install.sh
upgrades an installation: the code is copied over the install directory (.venv, data,
.git and .env are left alone), Python dependencies are refreshed, and the change is
committed to the install directory's local git repository.

Where the "latest version" comes from:
  * the latest GitHub release (tag such as v1.2.0), if the repository has releases;
  * otherwise the VERSION file on the update branch (default: main).

The installed version is the VERSION file next to app/. Private repositories need a
GitHub token (fine-grained, read-only "Contents" permission is enough), saved in
Admin → Updates, with `sysadmin-ia set-github-token`, or as SAI_GITHUB_TOKEN.

Before anything is overwritten, the current code is saved to <data>/backups/app-*.tar.gz
and restored automatically if the new version fails to install or import.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

import httpx

from . import db
from .config import settings

APP_DIR: Path = settings.app_dir
VERSION_FILE = APP_DIR / "VERSION"
API = "https://api.github.com"
MAX_DOWNLOAD = 50 * 1024 * 1024
CACHE_SECONDS = 3600
# Same exclusions as install.sh: never touch these in the install directory.
EXCLUDE = {".venv", "data", ".git", "__pycache__", ".env"}

_TOKEN_KEY = "github_token"
_lock = asyncio.Lock()
_last_check: dict | None = None


class UpdateError(Exception):
    pass


# ------------------------------------------------------------------ configuration

def repo() -> str:
    return (os.environ.get("SAI_UPDATE_REPO") or "ChonaCio/SysAdmin.IA").strip().strip("/")


def branch() -> str:
    return (os.environ.get("SAI_UPDATE_BRANCH") or "main").strip()


def token() -> tuple[str | None, str | None]:
    """Return (token, source) where source is 'web', 'config' or None."""
    stored = db.get_setting(_TOKEN_KEY)
    if stored:
        return stored, "web"
    env = (os.environ.get("SAI_GITHUB_TOKEN") or "").strip()
    return (env, "config") if env else (None, None)


def set_token(value: str | None) -> None:
    value = (value or "").strip() or None
    if value is not None and (len(value) < 20 or any(c.isspace() for c in value)):
        raise ValueError("That doesn't look like a valid GitHub token.")
    db.set_setting(_TOKEN_KEY, value)
    global _last_check
    _last_check = None


def current_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def parse_version(v: str) -> tuple[int, ...]:
    """'v1.2.10' -> (1, 2, 10). Non-numeric suffixes are ignored."""
    parts = re.findall(r"\d+", (v or "").split("-")[0].split("+")[0])
    return tuple(int(p) for p in parts[:4]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def dev_checkout() -> bool:
    """True when the app runs from a clone with a git remote (a developer checkout).

    Installed copies (install.sh) have a local-only git repo without remotes. In a clone,
    overwriting files would fight with git, so updating is left to `git pull`.
    """
    if not (APP_DIR / ".git").exists() or not shutil.which("git"):
        return False
    r = subprocess.run(["git", "-C", str(APP_DIR), "remote"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def under_supervisor() -> bool:
    """True if something restarts the app after it exits (systemd sets INVOCATION_ID)."""
    return bool(os.environ.get("INVOCATION_ID") or os.environ.get("SAI_SUPERVISED"))


# ------------------------------------------------------------------ GitHub

def _headers(raw: bool = False) -> dict:
    h = {
        "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SysAdmin.IA-updater",
    }
    tok, _ = token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _raise_for(r: httpx.Response, what: str) -> None:
    if r.status_code == 404:
        hint = "" if token()[0] else " If the repository is private, add a GitHub token."
        raise UpdateError(f"{what}: not found (github.com/{repo()}).{hint}")
    if r.status_code in (401, 403):
        if r.headers.get("x-ratelimit-remaining") == "0":
            raise UpdateError("GitHub API rate limit reached; try again later or add a GitHub token.")
        raise UpdateError(f"{what}: GitHub refused access (HTTP {r.status_code}). Check the GitHub token.")
    if r.status_code >= 400:
        raise UpdateError(f"{what}: GitHub returned HTTP {r.status_code}.")


async def _latest() -> dict:
    """Find the newest available version on GitHub."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        try:
            r = await c.get(f"{API}/repos/{repo()}/releases/latest", headers=_headers())
            if r.status_code == 200:
                rel = r.json()
                return {
                    "version": rel["tag_name"].lstrip("vV"),
                    "source": "release",
                    "ref": rel["tag_name"],
                    "name": rel.get("name") or rel["tag_name"],
                    "notes": (rel.get("body") or "")[:5000],
                    "published_at": rel.get("published_at"),
                    "url": rel.get("html_url"),
                }
            if r.status_code != 404:
                _raise_for(r, "Latest release")
            # No releases: use the VERSION file on the branch.
            r = await c.get(f"{API}/repos/{repo()}/contents/VERSION",
                            params={"ref": branch()}, headers=_headers(raw=True))
            _raise_for(r, f"VERSION file on branch '{branch()}'")
            version = r.text.strip()
            if not re.fullmatch(r"v?\d+(\.\d+){0,3}([-+][\w.]+)?", version):
                raise UpdateError(f"Unexpected VERSION file contents on GitHub: {version[:40]!r}")
            info = {"version": version.lstrip("vV"), "source": "branch", "ref": branch(),
                    "name": f"{branch()} ({version})", "notes": "", "published_at": None,
                    "url": f"https://github.com/{repo()}/tree/{branch()}"}
            r = await c.get(f"{API}/repos/{repo()}/commits/{branch()}", headers=_headers())
            if r.status_code == 200:
                cm = r.json()
                info["ref"] = cm["sha"]  # pin the download to the commit we looked at
                info["notes"] = (cm.get("commit", {}).get("message") or "")[:5000]
                info["published_at"] = cm.get("commit", {}).get("committer", {}).get("date")
            return info
        except httpx.HTTPError as e:
            raise UpdateError(f"Could not reach GitHub: {type(e).__name__}: {e}") from e


async def check(force: bool = False) -> dict:
    """Compare the installed version with GitHub. Cached for an hour unless force=True."""
    global _last_check
    if not force and _last_check and time.time() - _last_check["checked_at"] < CACHE_SECONDS:
        return {**_last_check, "current": current_version()}
    tok, tok_source = token()
    result = {
        "current": current_version(),
        "repo": repo(),
        "branch": branch(),
        "token_set": bool(tok),
        "token_source": tok_source,
        "can_apply": not dev_checkout(),
        "auto_restart": under_supervisor(),
        "checked_at": time.time(),
        "latest": None,
        "update_available": False,
        "error": None,
    }
    try:
        latest = await _latest()
        result["latest"] = latest
        result["update_available"] = is_newer(latest["version"], result["current"])
    except UpdateError as e:
        result["error"] = str(e)
    _last_check = result
    return result


def status() -> dict:
    """Configuration and the last check result, without contacting GitHub."""
    tok, tok_source = token()
    base = _last_check or {"latest": None, "update_available": False, "error": None, "checked_at": None}
    return {**base, "current": current_version(), "repo": repo(), "branch": branch(),
            "token_set": bool(tok), "token_source": tok_source,
            "can_apply": not dev_checkout(), "auto_restart": under_supervisor()}


# ------------------------------------------------------------------ install

async def _download(ref: str, dest: Path) -> None:
    url = f"{API}/repos/{repo()}/tarball/{ref}"
    try:
        # httpx drops the Authorization header when GitHub redirects to codeload.github.com.
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            async with c.stream("GET", url, headers=_headers()) as r:
                _raise_for(r, "Download")
                size = 0
                with dest.open("wb") as f:
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_DOWNLOAD:
                            raise UpdateError("Download is unexpectedly large; aborted.")
                        f.write(chunk)
    except httpx.HTTPError as e:
        raise UpdateError(f"Download failed: {type(e).__name__}: {e}") from e


def _safe_extract(archive: Path, dest: Path) -> Path:
    """Extract a GitHub tarball, rejecting links and paths outside dest. Returns the source root."""
    with tarfile.open(archive, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            p = PurePosixPath(m.name)
            if p.is_absolute() or ".." in p.parts:
                raise UpdateError(f"Unsafe path in archive: {m.name}")
            if m.issym() or m.islnk():
                continue  # the project has no links; never follow any
            if not (m.isfile() or m.isdir()):
                continue
            members.append(m)
        kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tf.extractall(dest, members=members, **kwargs)
    roots = [p for p in dest.iterdir() if p.is_dir()]
    if len(roots) != 1 or not (roots[0] / "app" / "main.py").is_file():
        raise UpdateError("The downloaded archive does not look like SysAdmin.IA.")
    return roots[0]


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        top = rel == Path(".")
        # data/ and .env only matter at the top level; the rest are skipped everywhere.
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE or (not top and d in ("data", ".env"))]
        for name in filenames:
            if name == ".env" and top:
                continue
            if name.endswith(".pyc"):
                continue
            yield rel / name


def _backup(version: str) -> Path:
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    path = settings.backup_dir / f"app-{version}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        for rel in _iter_files(APP_DIR):
            tf.add(APP_DIR / rel, arcname=rel.as_posix(), recursive=False)
    return path


def _restore(backup: Path) -> None:
    with tarfile.open(backup, "r:gz") as tf:
        kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tf.extractall(APP_DIR, **kwargs)


def _copy_tree(src: Path) -> list[str]:
    changed = []
    for rel in _iter_files(src):
        s, d = src / rel, APP_DIR / rel
        if d.is_file() and d.read_bytes() == s.read_bytes():
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        tmp = d.with_name(d.name + ".sai-new")
        shutil.copyfile(s, tmp)
        os.replace(tmp, d)  # atomic per file
        changed.append(rel.as_posix())
    return changed


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=APP_DIR, capture_output=True, text=True, timeout=timeout)


def _git_commit(message: str) -> None:
    if not (APP_DIR / ".git").exists() or not shutil.which("git"):
        return
    _run(["git", "-C", str(APP_DIR), "add", "-A"], 60)
    _run(["git", "-C", str(APP_DIR), "commit", "-qm", message], 60)


def _install(src: Path, new_version: str, log: list[str]) -> list[str]:
    """Blocking part of apply(): back up, copy, refresh deps, verify; roll back on failure."""
    old_version = current_version()
    old_reqs = (APP_DIR / "requirements.txt").read_bytes() if (APP_DIR / "requirements.txt").exists() else b""

    _git_commit(f"Snapshot before update to {new_version} ({time.strftime('%Y-%m-%dT%H:%M:%S')})")
    backup = _backup(old_version)
    log.append(f"Backed up current code to {backup}")

    try:
        changed = _copy_tree(src)
        log.append(f"Updated {len(changed)} file(s).")
        new_reqs = (APP_DIR / "requirements.txt").read_bytes() if (APP_DIR / "requirements.txt").exists() else b""
        if new_reqs != old_reqs:
            log.append("Installing Python dependencies…")
            r = _run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"], 900)
            if r.returncode != 0:
                raise UpdateError(f"pip install failed:\n{(r.stderr or r.stdout)[-2000:]}")
        r = _run([sys.executable, "-c", "import app.main"], 120)
        if r.returncode != 0:
            raise UpdateError(f"The new version failed to load:\n{(r.stderr or r.stdout)[-2000:]}")
    except Exception as e:
        _restore(backup)
        log.append("Rolled back to the previous version.")
        if isinstance(e, UpdateError):
            raise
        raise UpdateError(f"{type(e).__name__}: {e}") from e

    _git_commit(f"Update {old_version} -> {new_version} from github.com/{repo()}")
    return changed


async def apply() -> dict:
    """Download and install the latest version. The caller restarts the app afterwards."""
    if _lock.locked():
        raise UpdateError("An update is already running.")
    async with _lock:
        if dev_checkout():
            raise UpdateError("This app runs from a git clone; update it with `git pull` instead.")
        info = await check(force=True)
        if info["error"]:
            raise UpdateError(info["error"])
        if not info["update_available"]:
            raise UpdateError(f"Already up to date (version {info['current']}).")
        latest = info["latest"]
        log: list[str] = [f"Updating {info['current']} -> {latest['version']} ({latest['source']} {latest['ref']})"]
        work = Path(tempfile.mkdtemp(prefix="update-", dir=settings.data_dir))
        try:
            archive = work / "src.tar.gz"
            await _download(latest["ref"], archive)
            log.append("Downloaded.")
            src = await asyncio.to_thread(_safe_extract, archive, work / "src")
            # Releases may not bump VERSION themselves; record the version we installed.
            (src / "VERSION").write_text(latest["version"] + "\n", encoding="utf-8")
            changed = await asyncio.to_thread(_install, src, latest["version"], log)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        global _last_check
        _last_check = None
        return {"ok": True, "from": info["current"], "to": latest["version"],
                "changed": changed, "log": log, "restart": under_supervisor()}
