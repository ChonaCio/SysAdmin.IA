"""Admin CLI: `sysadmin-ia <command>` (installed by install.sh) or `python -m app.cli <command>`."""
from __future__ import annotations

import argparse
import getpass
import sys

from . import auth, claude_settings, db


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
