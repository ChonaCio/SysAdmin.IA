"""Admin CLI: `sysadmin-ia <command>` (installed by install.sh) or `python -m app.cli <command>`."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from . import ai_connections as ai
from . import auth, db, server_name, updater
from .config import settings


def _prompt_password() -> str:
    while True:
        p1 = getpass.getpass("Password (min 10 chars): ")
        p2 = getpass.getpass("Repeat password: ")
        if p1 != p2:
            print("Passwords do not match.")
            continue
        try:
            auth.validate_password_strength(p1)
            return p1
        except ValueError as e:
            print(e)


def _claude_connection() -> dict | None:
    """The Claude API-key connection to manage from the command line (the default one first)."""
    conns = [c for c in ai.list_all() if c["provider"] == "anthropic" and c["method"] == "api_key"]
    return conns[0] if conns else None   # list_all() puts the default connection first


def _follow_upgrade(timeout: int = 1800) -> int:
    """Print the full upgrade's log as it grows, until it finishes."""
    import time
    printed = 0
    start = time.time()
    while time.time() - start < timeout:
        p = updater.progress(log_lines=100000)
        lines = p["log"].splitlines()
        for line in lines[printed:]:
            print(line, flush=True)
        printed = len(lines)
        if p["state"] in ("done", "failed"):
            print(p["message"] or p["state"])
            return 0 if p["state"] == "done" else 1
        time.sleep(2)
    print(f"Still running; follow it in {updater.UPGRADE_LOG}")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sysadmin-ia", description="SysAdmin.IA administration")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create-user", help="create a user")
    c.add_argument("username")
    c.add_argument("--role", choices=auth.ROLES, default="admin")
    c.add_argument("--password-stdin", action="store_true", help="read the password from stdin")

    r = sub.add_parser("reset-password", help="set a new password for a user")
    r.add_argument("username")
    r.add_argument("--password-stdin", action="store_true")

    d = sub.add_parser("delete-user", help="delete a user")
    d.add_argument("username")

    sub.add_parser("list-users", help="list users")

    a = sub.add_parser("audit", help="show the audit log")
    a.add_argument("-n", type=int, default=50)

    k = sub.add_parser("set-api-key", help="save the API key of the default Claude connection (creates it if needed)")
    k.add_argument("--stdin", action="store_true", help="read the key from stdin")
    sub.add_parser("clear-api-key", help="remove the saved API key of the default Claude connection")
    m = sub.add_parser("set-model", help="set the model of the default AI connection")
    m.add_argument("model")
    sub.add_parser("ai-status", aliases=["claude-status"], help="list the AI connections")

    sub.add_parser("server-name", help="show the server name and LAN addresses")
    sn = sub.add_parser("set-server-name", help="rename the server (reachable on the LAN as NAME.local)")
    sn.add_argument("name")
    sub.add_parser("reboot", help="reboot the server")

    sub.add_parser("version", help="show the installed version")
    sub.add_parser("check-update", help="check GitHub for a newer version")
    u = sub.add_parser("update", help="download and install the newest version from GitHub")
    u.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    g = sub.add_parser("set-github-token", help="save a GitHub token (needed if the repository is private)")
    g.add_argument("--stdin", action="store_true", help="read the token from stdin")
    sub.add_parser("clear-github-token", help="remove the saved GitHub token")

    args = ap.parse_args(argv)

    def read_pw() -> str:
        if getattr(args, "password_stdin", False):
            return sys.stdin.readline().rstrip("\n")
        return _prompt_password()

    try:
        if args.cmd == "create-user":
            auth.create_user(args.username, read_pw(), args.role)
            print(f"Created {args.role} user '{args.username}'.")
        elif args.cmd == "reset-password":
            row = db.query_one("SELECT id FROM users WHERE username=?", (args.username,))
            if not row:
                print("No such user.", file=sys.stderr)
                return 1
            auth.set_password(row["id"], read_pw())
            print("Password updated; existing sessions were logged out.")
        elif args.cmd == "delete-user":
            row = db.query_one("SELECT id FROM users WHERE username=?", (args.username,))
            if not row:
                print("No such user.", file=sys.stderr)
                return 1
            auth.delete_user(row["id"])
            print("Deleted.")
        elif args.cmd == "list-users":
            for u in auth.list_users():
                print(f"{u['id']:>4}  {u['username']:<24} {u['role']}")
        elif args.cmd == "set-api-key":
            key = sys.stdin.readline().strip() if args.stdin else getpass.getpass("Anthropic API key: ").strip()
            if len(key) < 20 or any(c.isspace() for c in key):
                raise ValueError("That doesn't look like a valid API key.")
            ai.migrate_legacy()
            conn = _claude_connection()
            if conn:
                ai.update(conn["id"], {"secrets": {"api_key": key}})
            else:
                conn = ai.create({"name": "Claude", "provider": "anthropic", "method": "api_key",
                                  "secrets": {"api_key": key}, "is_default": not ai.any_enabled()})
            db.audit("ai_connection_updated", "sysadmin-ia", "local", {"id": conn["id"], "secrets_changed": ["api_key"]})
            print(f"API key saved in AI connection “{conn['name']}”. No restart needed.")
        elif args.cmd == "clear-api-key":
            conn = _claude_connection()
            if not conn:
                print("There is no Claude API-key connection.", file=sys.stderr)
                return 1
            ai.update(conn["id"], {"clear_secrets": ["api_key"]})
            db.audit("ai_connection_updated", "sysadmin-ia", "local", {"id": conn["id"], "secrets_changed": ["api_key"]})
            print(f"Saved API key removed from “{conn['name']}” (ANTHROPIC_API_KEY in the config file, if set, is still used).")
        elif args.cmd == "set-model":
            conn = ai.default()
            if not conn:
                print("There is no AI connection yet.", file=sys.stderr)
                return 1
            ai.update(conn["id"], {"config": {"model": args.model}})
            print(f"Model of “{conn['name']}” set to {args.model}.")
        elif args.cmd in ("ai-status", "claude-status"):
            ai.migrate_legacy()
            conns = ai.list_all()
            if not conns:
                print("No AI connections. Add one in the web UI (AI connections) or with: sudo sysadmin-ia set-api-key")
            for c in conns:
                p = ai.public(c)
                flags = ", ".join(f for f in ("default" if c["is_default"] else "", "" if c["enabled"] else "disabled") if f)
                creds = "key from config file" if p["uses_env_key"] else \
                    ("credentials saved" if any(p["secrets_set"].values()) else "no secret saved")
                print(f"{c['id']:>3}  {c['name']:<20} {p['provider_label']} · {p['method_label']} · {p['model']}"
                      f"  ({creds}{'; ' + flags if flags else ''})")
        elif args.cmd == "server-name":
            s = server_name.status()
            print(f"Server name: {s['name']}   (LAN name: {s['mdns_name']}, mDNS {s['mdns']})")
            for url in s["urls"]:
                print(f"  {url}")
        elif args.cmd == "set-server-name":
            old = server_name.current()
            try:
                r = server_name.set_name(args.name)
            except RuntimeError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            db.audit("server_name", "sysadmin-ia", "local", {"from": old, "to": r["name"]})
            print(r["output"])
            if r["cert_changed"]:
                print(f"Restart to use the new certificate: sudo systemctl restart {settings.service_name}")
        elif args.cmd == "reboot":
            if input("Reboot the server now? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
            try:
                print(server_name.reboot())
            except RuntimeError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            db.audit("server_reboot", "sysadmin-ia", "local")
        elif args.cmd == "version":
            print(updater.current_version())
        elif args.cmd == "check-update":
            s = asyncio.run(updater.check(force=True))
            print(f"Installed: {s['current']}   Source: github.com/{s['repo']}")
            if s["error"]:
                print(f"Error: {s['error']}", file=sys.stderr)
                return 1
            latest = s["latest"]
            print(f"Latest:    {latest['version']} ({latest['source']} {latest['name']})")
            print("An update is available: sudo sysadmin-ia update" if s["update_available"] else "Up to date.")
        elif args.cmd == "update":
            s = asyncio.run(updater.check(force=True))
            if s["error"]:
                print(f"Error: {s['error']}", file=sys.stderr)
                return 1
            if not s["update_available"]:
                print(f"Already up to date ({s['current']}).")
                return 0
            if not args.yes and input(f"Update {s['current']} -> {s['latest']['version']}? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
            try:
                r = asyncio.run(updater.apply())
            except updater.UpdateError as e:
                db.audit("update_failed", "sysadmin-ia", "local", {"from": s["current"], "error": str(e)})
                print(f"Update failed: {e}", file=sys.stderr)
                return 1
            if r["mode"] == "full":
                db.audit("update_started", "sysadmin-ia", "local", {"from": r["from"], "to": r["to"]})
                print("\n".join(r["log"]))
                return _follow_upgrade()
            db.audit("update", "sysadmin-ia", "local", {"from": r["from"], "to": r["to"], "files": len(r["changed"])})
            print("\n".join(r["log"]))
            print(f"Updated to {r['to']} (app code only). Restart to load it: sudo systemctl restart {settings.service_name}")
            if r.get("setup_command"):
                print(f"For complete updates (system packages, service, helper), run once: {r['setup_command']}")
        elif args.cmd == "set-github-token":
            tok = sys.stdin.readline().strip() if args.stdin else getpass.getpass("GitHub token: ").strip()
            updater.set_token(tok)
            db.audit("update_settings", "sysadmin-ia", "local", "github_token set")
            print("GitHub token saved.")
        elif args.cmd == "clear-github-token":
            updater.set_token(None)
            db.audit("update_settings", "sysadmin-ia", "local", "github_token cleared")
            print("Saved GitHub token removed.")
        elif args.cmd == "audit":
            import datetime
            for r in reversed(db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (args.n,))):
                ts = datetime.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
                print(f"{ts}  {r['username'] or '-':<12} {r['ip'] or '-':<15} {r['action']:<16} {(r['detail'] or '')[:150]}")
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
