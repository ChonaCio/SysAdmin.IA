"""Admin CLI: `sysadmin-ia <command>` (installed by install.sh) or `python -m app.cli <command>`."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from . import auth, claude_settings, db, server_name, updater
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

    k = sub.add_parser("set-api-key", help="save the Anthropic API key (same as Admin → Claude API)")
    k.add_argument("--stdin", action="store_true", help="read the key from stdin")
    sub.add_parser("clear-api-key", help="remove the API key saved via the web UI / sysadmin-ia")
    m = sub.add_parser("set-model", help="set the Claude model (empty string resets to default)")
    m.add_argument("model")
    sub.add_parser("claude-status", help="show the Claude API configuration")

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
            claude_settings.set_api_key(key)
            db.audit("claude_settings", "sysadmin-ia", "local", "api_key set")
            print("API key saved. It takes effect immediately; no restart needed.")
        elif args.cmd == "clear-api-key":
            claude_settings.set_api_key(None)
            db.audit("claude_settings", "sysadmin-ia", "local", "api_key cleared")
            print("Saved API key removed (a key in the config file, if any, is still used).")
        elif args.cmd == "set-model":
            claude_settings.set_model(args.model)
            print(f"Model set to {claude_settings.model()}.")
        elif args.cmd == "claude-status":
            s = claude_settings.summary()
            src = {"web": "saved via web UI / sysadmin-ia", "config": "config file"}.get(s["api_key_source"], "")
            print(f"API key: {s['api_key_hint'] + ' (' + src + ')' if s['api_key_set'] else 'not configured'}")
            print(f"Model:   {s['model']}\nEffort:  {s['effort']}")
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
            db.audit("update", "sysadmin-ia", "local", {"from": r["from"], "to": r["to"], "files": len(r["changed"])})
            print("\n".join(r["log"]))
            print(f"Updated to {r['to']}. Restart to load it: sudo systemctl restart {settings.service_name}")
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
