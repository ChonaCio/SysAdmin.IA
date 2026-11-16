"""Self-update from GitHub.

Checks the GitHub repository (default ChonaCio/SysAdmin.IA) for a newer version and, on
request, installs it. Normally ("full" mode) the root helper downloads the new version and
runs its own install.sh non-interactively, so an update does everything a fresh install of
that version would (system packages, systemd unit, helpers, sudoers, CLI, virtualenv, code)
while keeping config, data and users. If the helper isn't available ("code" mode), only the
code is copied over the install directory (.venv, data, .git and .env are left alone),
Python dependencies are refreshed, and the change is committed to the local git repo.

The "latest version" is the highest version number among the repository's GitHub releases
and version tags (v1.2.3); only if there are neither, the VERSION file on the update branch
(default: main). Updates are cumulative: they go straight to the latest version.

The installed version is the VERSION file next to app/. install.sh writes it from the git
tag (or release archive name) it is installed from, so it need not be bumped by hand. Private repositories need a
GitHub token (fine-grained, read-only "Contents" permission is enough), saved in
Admin → Updates, with `sysadmin-ia set-github-token`, or as SAI_GITHUB_TOKEN.

Before anything is overwritten, the current code is saved to <data>/backups/app-*.tar.gz
and restored automatically if the new version fails to install or import.

Any published version can be installed on purpose (apply(version=…)): a newer one other than the
latest, or an older one (a downgrade, which needs allow_downgrade=True). A downgrade runs that
version's own installer exactly like an update. Data is kept; the database only ever gains
columns/tables, which older versions ignore.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

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

def _request(url: str, raw: bool = False) -> urllib.request.Request:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SysAdmin.IA-updater",
    })
    tok, _ = token()
    if tok:
        # Unredirected: not forwarded when GitHub redirects a download to codeload.github.com.
        req.add_unredirected_header("Authorization", f"Bearer {tok}")
    return req


def _http_error(e: urllib.error.HTTPError, what: str) -> UpdateError:
    if e.code == 404:
        hint = "" if token()[0] else " If the repository is private, add a GitHub token."
        return UpdateError(f"{what}: not found (github.com/{repo()}).{hint}")
    if e.code in (401, 403):
        if e.headers.get("x-ratelimit-remaining") == "0":
            return UpdateError("GitHub API rate limit reached; try again later or add a GitHub token.")
        return UpdateError(f"{what}: GitHub refused access (HTTP {e.code}). Check the GitHub token.")
    return UpdateError(f"{what}: GitHub returned HTTP {e.code}.")


def _get(url: str, what: str, raw: bool = False, missing_ok: bool = False) -> bytes | None:
    """GET a GitHub API URL (blocking). Returns None on 404 when missing_ok."""
    try:
        with urllib.request.urlopen(_request(url, raw), timeout=20) as r:
            return r.read(MAX_DOWNLOAD)
    except urllib.error.HTTPError as e:
        if missing_ok and e.code == 404:
            return None
        raise _http_error(e, what) from e
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"Could not reach GitHub: {getattr(e, 'reason', e)}") from e


STABLE_RE = re.compile(r"v?\d+(\.\d+){1,3}")  # v1.2.3 / 1.2.3 (pre-releases like -beta are skipped)


def _vkey(v: str) -> tuple[int, ...]:
    p = parse_version(v)
    return p + (0,) * (4 - len(p))


def _versions_blocking() -> list[dict]:
    """Every published version, highest first: GitHub releases plus version tags.

    The highest version number wins, not the most recently published release, so an update
    always goes straight to the newest version (cumulative). Without releases or tags, the
    VERSION file on the update branch is used.
    """
    base = f"{API}/repos/{repo()}"
    found: dict[tuple, dict] = {}
    body = _get(f"{base}/releases?per_page=100", "Releases", missing_ok=True)
    for rel in json.loads(body or b"[]"):
        tag = rel.get("tag_name") or ""
        if rel.get("draft") or rel.get("prerelease") or not STABLE_RE.fullmatch(tag):
            continue
        found[_vkey(tag)] = {
            "version": tag.lstrip("vV"), "source": "release", "ref": tag,
            "name": rel.get("name") or tag, "notes": (rel.get("body") or "")[:5000],
            "published_at": rel.get("published_at"), "url": rel.get("html_url") or release_page(tag),
        }
    body = _get(f"{base}/tags?per_page=100", "Tags", missing_ok=True)
    for t in json.loads(body or b"[]"):
        tag = t.get("name") or ""
        if STABLE_RE.fullmatch(tag) and _vkey(tag) not in found:
            found[_vkey(tag)] = {
                "version": tag.lstrip("vV"), "source": "tag", "ref": tag, "name": tag, "notes": "",
                "published_at": None, "url": release_page(tag),
                "_sha": t.get("commit", {}).get("sha"),
            }
    versions = [found[k] for k in sorted(found, reverse=True)]
    if not versions:
        return [_branch_version(base)]
    # A tag without a release has no notes: use its commit message (newest only, to save requests).
    top = versions[0]
    if top["source"] == "tag" and top.get("_sha"):
        cm = _get(f"{base}/commits/{top['_sha']}", "Commit", missing_ok=True)
        if cm:
            c = json.loads(cm).get("commit", {})
            top["notes"] = (c.get("message") or "")[:5000]
            top["published_at"] = c.get("committer", {}).get("date")
    for v in versions:
        v.pop("_sha", None)
    return versions


def _branch_version(base: str) -> dict:
    """Fallback when the repository has no releases or version tags."""
    ref = urllib.parse.quote(branch(), safe="")
    version = _get(f"{base}/contents/VERSION?ref={ref}", f"VERSION file on branch '{branch()}'",
                   raw=True).decode("utf-8", "replace").strip()
    if not re.fullmatch(r"v?\d+(\.\d+){0,3}([-+][\w.]+)?", version):
        raise UpdateError(f"Unexpected VERSION file contents on GitHub: {version[:40]!r}")
    info = {"version": version.lstrip("vV"), "source": "branch", "ref": branch(),
            "name": f"{branch()} ({version})", "notes": "", "published_at": None,
            "url": f"https://github.com/{repo()}/tree/{branch()}"}
    body = _get(f"{base}/commits/{ref}", "Latest commit", missing_ok=True)
    if body is not None:
        cm = json.loads(body)
        info["ref"] = cm["sha"]  # pin the download to the commit we looked at
        info["notes"] = (cm.get("commit", {}).get("message") or "")[:5000]
        info["published_at"] = cm.get("commit", {}).get("committer", {}).get("date")
    return info


def release_page(tag: str) -> str:
    """GitHub's page for a release (for a plain tag GitHub shows the tag there)."""
    return f"https://github.com/{repo()}/releases/tag/{urllib.parse.quote(tag, safe='')}"


def _with_pending(result: dict) -> dict:
    """Add the versions between the installed one and the newest ("pending", oldest first) and
    the full list of published versions ("releases", newest first, each marked newer/current/older)."""
    current = current_version()
    versions = result.get("versions") or []
    pending = [{k: v[k] for k in ("version", "name", "notes", "published_at", "url")}
               for v in reversed(versions) if is_newer(v["version"], current)]
    releases = []
    for v in versions:
        rel = "newer" if is_newer(v["version"], current) else "older" if is_newer(current, v["version"]) else "current"
        releases.append({k: v[k] for k in ("version", "name", "notes", "published_at", "url", "source")} | {"status": rel})
    latest = result.get("latest")
    out = {k: v for k, v in result.items() if k != "versions"}
    return {**out, "current": current, "pending": pending, "releases": releases,
            "update_available": bool(latest) and is_newer(latest["version"], current)}


async def check(force: bool = False) -> dict:
    """Compare the installed version with GitHub. Cached for an hour unless force=True.

    "latest" is the newest version, which is what an update installs (it is cumulative);
    "pending" lists every version from the installed one up to it, with release notes.
    """
    global _last_check
    if not force and _last_check and time.time() - _last_check["checked_at"] < CACHE_SECONDS:
        return _with_pending(_last_check)
    tok, tok_source = token()
    result = {
        "current": current_version(),
        "repo": repo(),
        "branch": branch(),
        "token_set": bool(tok),
        "token_source": tok_source,
        "can_apply": not dev_checkout(),
        "auto_restart": under_supervisor(),
        **await asyncio.to_thread(_upgrade_mode),
        "checked_at": time.time(),
        "latest": None,
        "update_available": False,
        "error": None,
    }
    try:
        versions = await asyncio.to_thread(_versions_blocking)
        result["versions"] = versions
        result["latest"] = versions[0]
    except UpdateError as e:
        result["error"] = str(e)
    _last_check = result
    return _with_pending(result)


def status() -> dict:
    """Configuration and the last check result, without contacting GitHub."""
    tok, tok_source = token()
    base = _with_pending(_last_check or {"latest": None, "error": None, "checked_at": None})
    return {**base, "current": current_version(), "repo": repo(), "branch": branch(),
            "token_set": bool(tok), "token_source": tok_source,
            "can_apply": not dev_checkout(), "auto_restart": under_supervisor(),
            **_upgrade_mode(), "progress": progress(0)}


# ------------------------------------------------------------------ install

def _download_blocking(ref: str, dest: Path) -> None:
    url = f"{API}/repos/{repo()}/tarball/{urllib.parse.quote(ref, safe='')}"
    try:
        with urllib.request.urlopen(_request(url), timeout=120) as r, dest.open("wb") as f:
            size = 0
            while chunk := r.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise UpdateError("Download is unexpectedly large; aborted.")
                f.write(chunk)
    except urllib.error.HTTPError as e:
        raise _http_error(e, "Download") from e
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"Download failed: {getattr(e, 'reason', e)}") from e


async def _download(ref: str, dest: Path) -> None:
    await asyncio.to_thread(_download_blocking, ref, dest)


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


UPGRADE_STATUS = Path("/var/log/sysadmin-ia/upgrade.status")
UPGRADE_LOG = Path("/var/log/sysadmin-ia/upgrade.log")


def progress(log_lines: int = 40) -> dict:
    """State of the last full upgrade (written by the root helper)."""
    out: dict = {"state": None, "version": None, "message": None, "updated": None, "log": ""}
    try:
        for line in UPGRADE_STATUS.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key in out:
                out[key] = value
    except OSError:
        return out
    if out["updated"]:
        out["updated"] = int(out["updated"]) if out["updated"].isdigit() else None
    try:
        out["log"] = "\n".join(UPGRADE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-log_lines:])
    except OSError:
        pass
    return out


def full_upgrade_available() -> bool:
    """True if updates can run the new version's install.sh as root (via the helper)."""
    from . import server_name  # local import: server_name is optional at import time
    return server_name.helper_supports("upgrade")


def _upgrade_mode() -> dict:
    """{"full_upgrade": bool, "setup_command": one-time root command to enable it, or None}."""
    from . import server_name
    full = full_upgrade_available()
    hint = None
    if not full and sys.platform.startswith("linux") and not dev_checkout() and server_name.BUNDLED.is_file():
        hint = server_name.setup_command()
    return {"full_upgrade": full, "setup_command": hint}


async def _apply_full(info: dict, target: dict) -> dict:
    """Start a full upgrade (the target version's install.sh, as root, in the background)."""
    from . import server_name
    tok, _ = token()
    out = await asyncio.to_thread(server_name.run_helper, "upgrade", target["ref"], target["version"],
                                  timeout=60, input=(tok or "") + "\n")
    return {"ok": True, "mode": "full", "from": info["current"], "to": target["version"],
            "changed": [], "log": [out], "restart": True,
            "downgrade": is_newer(info["current"], target["version"])}


def _pick(versions: list[dict], version: str | None, current: str, allow_downgrade: bool) -> dict:
    """The version to install: `version` if given (any published one), else the newest."""
    if not version:
        target = versions[0]
    else:
        want = _vkey(version.strip().lstrip("vV"))
        target = next((v for v in versions if _vkey(v["version"]) == want), None)
        if target is None:
            raise UpdateError(f"Version {version} isn't published on github.com/{repo()}.")
    if not is_newer(target["version"], current):
        if not is_newer(current, target["version"]):
            raise UpdateError(f"Version {target['version']} is already installed.")
        if not allow_downgrade:
            raise UpdateError(f"Version {target['version']} is older than the installed {current}; "
                              "confirm the downgrade to install it.")
    return target


async def apply(version: str | None = None, allow_downgrade: bool = False) -> dict:
    """Install the latest version, or a chosen published `version` (older ones need
    allow_downgrade: going back reinstalls that version exactly like an update).

    Preferred ("full" mode): the root helper downloads the release and runs its install.sh
    non-interactively, exactly like a manual install/upgrade (system packages, systemd unit,
    helper, sudoers, CLI wrapper, virtualenv, code), then restarts the app. It runs in the
    background; follow it with progress().

    Fallback ("code" mode, when the helper isn't available): replace the app code and Python
    packages only. The caller restarts the app afterwards.
    """
    global _last_check
    if _lock.locked():
        raise UpdateError("An update is already running.")
    async with _lock:
        if dev_checkout():
            raise UpdateError("This app runs from a git clone; update it with `git pull` instead.")
        info = await check(force=True)
        if info["error"]:
            raise UpdateError(info["error"])
        if not version and not info["update_available"]:
            raise UpdateError(f"Already up to date (version {info['current']}).")
        latest = _pick(_last_check.get("versions") or [], version, info["current"], allow_downgrade)
        if re.fullmatch(r"\d+(\.\d+){0,3}", latest["version"]) and await asyncio.to_thread(full_upgrade_available):
            try:
                return await _apply_full(info, latest)
            except RuntimeError as e:
                raise UpdateError(str(e)) from e
        log: list[str] = [f"{'Downgrading' if is_newer(info['current'], latest['version']) else 'Updating'} "
                          f"{info['current']} -> {latest['version']} ({latest['source']} {latest['ref']})"]
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
        _last_check = None
        from . import server_name
        return {"ok": True, "mode": "code", "from": info["current"], "to": latest["version"],
                "changed": changed, "log": log, "restart": under_supervisor(),
                "downgrade": is_newer(info["current"], latest["version"]),
                "setup_command": server_name.setup_command() if sys.platform.startswith("linux") else None}
