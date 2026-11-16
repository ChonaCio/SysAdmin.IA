"""Mail accounts: send mail (SMTP) for job actions and read mail (IMAP) for job triggers.

Python's standard library only (smtplib, imaplib, email). Passwords are stored in the app
database like other secrets and never sent back to the browser. For Gmail / Microsoft 365 /
Yahoo with two-step verification, use an "app password".
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import json
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

from . import db

SECURITY = ("ssl", "starttls", "none")

# Presets fill in the server settings for common providers.
PRESETS = {
    "gmail": {"label": "Gmail / Google Workspace", "smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_security": "ssl",
              "imap_host": "imap.gmail.com", "imap_port": 993, "imap_security": "ssl",
              "help": "Use an app password (Google Account → Security → 2-Step Verification → App passwords)."},
    "outlook": {"label": "Outlook.com / Microsoft 365", "smtp_host": "smtp.office365.com", "smtp_port": 587,
                "smtp_security": "starttls", "imap_host": "outlook.office365.com", "imap_port": 993, "imap_security": "ssl",
                "help": "Your organisation may need to allow SMTP AUTH / IMAP for the mailbox."},
    "yahoo": {"label": "Yahoo Mail", "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 465, "smtp_security": "ssl",
              "imap_host": "imap.mail.yahoo.com", "imap_port": 993, "imap_security": "ssl",
              "help": "Use an app password (Account security → Generate app password)."},
    "icloud": {"label": "iCloud Mail", "smtp_host": "smtp.mail.me.com", "smtp_port": 587, "smtp_security": "starttls",
               "imap_host": "imap.mail.me.com", "imap_port": 993, "imap_security": "ssl",
               "help": "Use an app-specific password (appleid.apple.com)."},
    "local": {"label": "This server (local mail relay)", "smtp_host": "localhost", "smtp_port": 25, "smtp_security": "none",
              "imap_host": "", "imap_port": 993, "imap_security": "ssl",
              "help": "Sends through a mail server running on this machine (e.g. Postfix). Usually no password."},
    "custom": {"label": "Other mail server", "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls",
               "imap_host": "", "imap_port": 993, "imap_security": "ssl", "help": ""},
}

CONFIG_KEYS = ("preset", "from_name", "from_address", "username", "smtp_host", "smtp_port", "smtp_security",
               "imap_host", "imap_port", "imap_security", "imap_folder")


class MailError(ValueError):
    pass


def _row(r) -> dict:
    d = dict(r)
    d["config"] = json.loads(d["config"] or "{}")
    d["secrets"] = json.loads(d["secrets"] or "{}")
    return d


def public(a: dict) -> dict:
    c = a["config"]
    return {"id": a["id"], "name": a["name"], "config": c, "password_set": bool(a["secrets"].get("password")),
            "can_send": bool(c.get("smtp_host")), "can_read": bool(c.get("imap_host")),
            "preset_label": PRESETS.get(c.get("preset") or "custom", PRESETS["custom"])["label"]}


def list_all() -> list[dict]:
    return [_row(r) for r in db.query("SELECT * FROM mail_accounts ORDER BY name COLLATE NOCASE")]


def get(acc_id: int | None) -> dict | None:
    if not acc_id:
        return None
    r = db.query_one("SELECT * FROM mail_accounts WHERE id=?", (int(acc_id),))
    return _row(r) if r else None


def _clean(data: dict, existing: dict | None = None) -> tuple[str, dict, dict]:
    old = (existing or {}).get("config", {})
    cfg = {k: data.get("config", {}).get(k, old.get(k, "")) for k in CONFIG_KEYS}
    for k in ("smtp_port", "imap_port"):
        try:
            cfg[k] = int(cfg[k] or (587 if k == "smtp_port" else 993))
        except (TypeError, ValueError):
            raise MailError("Ports must be numbers.")
        if not 1 <= cfg[k] <= 65535:
            raise MailError("Ports must be between 1 and 65535.")
    for k in ("smtp_security", "imap_security"):
        if cfg[k] not in SECURITY:
            cfg[k] = "ssl" if k == "imap_security" else "starttls"
    for k in ("from_name", "from_address", "username", "smtp_host", "imap_host", "imap_folder", "preset"):
        cfg[k] = str(cfg.get(k) or "").strip()
    cfg["imap_folder"] = cfg["imap_folder"] or "INBOX"
    if not cfg["smtp_host"] and not cfg["imap_host"]:
        raise MailError("Enter an outgoing (SMTP) or incoming (IMAP) server.")
    if cfg["smtp_host"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", cfg["from_address"]):
        raise MailError("Enter the address mail is sent from.")
    name = " ".join(str(data.get("name") or "").split()) or (existing or {}).get("name") or cfg["from_address"] or "Mail"
    secrets = dict((existing or {}).get("secrets") or {})
    if data.get("password"):
        secrets["password"] = str(data["password"])
    if data.get("clear_password"):
        secrets.pop("password", None)
    return name[:60], cfg, secrets


def create(data: dict) -> dict:
    name, cfg, secrets = _clean(data)
    now = time.time()
    aid = db.execute("INSERT INTO mail_accounts (name, config, secrets, created_at, updated_at) VALUES (?,?,?,?,?)",
                     (name, json.dumps(cfg), json.dumps(secrets), now, now))
    return get(aid)


def update(acc_id: int, data: dict) -> dict:
    existing = get(acc_id)
    if not existing:
        raise MailError("That mail account no longer exists.")
    name, cfg, secrets = _clean(data, existing)
    db.execute("UPDATE mail_accounts SET name=?, config=?, secrets=?, updated_at=? WHERE id=?",
               (name, json.dumps(cfg), json.dumps(secrets), time.time(), acc_id))
    return get(acc_id)


def draft(data: dict, acc_id: int | None = None) -> dict:
    existing = get(acc_id) if acc_id else None
    name, cfg, secrets = _clean(data, existing)
    return {"id": acc_id or 0, "name": name, "config": cfg, "secrets": secrets}


def delete(acc_id: int) -> None:
    db.execute("DELETE FROM mail_accounts WHERE id=?", (acc_id,))


# ---------------------------------------------------------------- sending

def _ctx() -> ssl.SSLContext:
    return ssl.create_default_context()


def _smtp(a: dict) -> smtplib.SMTP:
    c = a["config"]
    if not c.get("smtp_host"):
        raise MailError(f"“{a['name']}” has no outgoing (SMTP) server.")
    try:
        if c["smtp_security"] == "ssl":
            s = smtplib.SMTP_SSL(c["smtp_host"], c["smtp_port"], timeout=20, context=_ctx())
        else:
            s = smtplib.SMTP(c["smtp_host"], c["smtp_port"], timeout=20)
            if c["smtp_security"] == "starttls":
                s.starttls(context=_ctx())
        if c.get("username") and a["secrets"].get("password"):
            s.login(c["username"], a["secrets"]["password"])
        return s
    except smtplib.SMTPAuthenticationError:
        raise MailError("The mail server rejected the username or password (an app password may be required).")
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        raise MailError(f"Could not connect to {c['smtp_host']}:{c['smtp_port']}: {e}")


def _recipients(to: str) -> list[str]:
    out = []
    for part in re.split(r"[,;\n]+", to or ""):
        addr = parseaddr(part.strip())[1]
        if addr:
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", addr):
                raise MailError(f"Invalid email address: {addr}")
            out.append(addr)
    if not out:
        raise MailError("Add at least one recipient.")
    if len(out) > 50:
        raise MailError("Too many recipients (max 50).")
    return out


def send(a: dict, to: str, subject: str, body: str) -> str:
    c = a["config"]
    rcpt = _recipients(to)
    msg = EmailMessage()
    msg["From"] = formataddr((c.get("from_name") or "SysAdmin.IA", c["from_address"]))
    msg["To"] = ", ".join(rcpt)
    msg["Subject"] = (subject or "(no subject)").replace("\n", " ")[:250]
    msg["Message-ID"] = make_msgid(domain=c["from_address"].split("@")[-1])
    msg.set_content(body or "")
    s = _smtp(a)
    try:
        s.send_message(msg, to_addrs=rcpt)
    except smtplib.SMTPException as e:
        raise MailError(f"The mail server refused the message: {e}")
    finally:
        try:
            s.quit()
        except Exception:
            pass
    return f"Sent to {', '.join(rcpt)}"


# ---------------------------------------------------------------- reading

def _imap(a: dict) -> imaplib.IMAP4:
    c = a["config"]
    if not c.get("imap_host"):
        raise MailError(f"“{a['name']}” has no incoming (IMAP) server.")
    try:
        if c["imap_security"] == "ssl":
            m = imaplib.IMAP4_SSL(c["imap_host"], c["imap_port"], ssl_context=_ctx(), timeout=20)
        else:
            m = imaplib.IMAP4(c["imap_host"], c["imap_port"], timeout=20)
            if c["imap_security"] == "starttls":
                m.starttls(ssl_context=_ctx())
        m.login(c.get("username") or c.get("from_address"), a["secrets"].get("password", ""))
        return m
    except imaplib.IMAP4.error as e:
        raise MailError(f"The mail server rejected the login: {e}")
    except (OSError, ssl.SSLError) as e:
        raise MailError(f"Could not connect to {c['imap_host']}:{c['imap_port']}: {e}")


def _imap_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _text_of(msg: email.message.Message) -> str:
    part = msg.get_body(preferencelist=("plain", "html")) if hasattr(msg, "get_body") else None
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        return ""
    if part.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()[:4000]


def fetch_new(a: dict, since_uid: int, from_filter: str = "", subject_filter: str = "",
              mark_read: bool = False, limit: int = 10) -> tuple[list[dict], int]:
    """Unread messages newer than since_uid (matching the filters). Returns (messages, highest uid seen)."""
    m = _imap(a)
    try:
        folder = a["config"].get("imap_folder") or "INBOX"
        typ, _ = m.select(_imap_quote(folder), readonly=not mark_read)
        if typ != "OK":
            raise MailError(f"Folder “{folder}” not found.")
        crit = ["UNSEEN", "UID", f"{since_uid + 1}:*"]
        if from_filter:
            crit += ["FROM", _imap_quote(from_filter)]
        if subject_filter:
            crit += ["SUBJECT", _imap_quote(subject_filter)]
        typ, data = m.uid("SEARCH", None, *crit)
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > since_uid]
        top = max(uids, default=since_uid)
        out = []
        for uid in sorted(uids)[-limit:]:
            typ, parts = m.uid("FETCH", str(uid), "(BODY.PEEK[])" if not mark_read else "(RFC822)")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if not raw:
                continue
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            out.append({"uid": uid, "from": str(msg.get("From", "")), "to": str(msg.get("To", "")),
                        "subject": str(msg.get("Subject", "")), "date": str(msg.get("Date", "")),
                        "body": _text_of(msg)})
        return out, top
    finally:
        try:
            m.logout()
        except Exception:
            pass


def latest_uid(a: dict) -> int:
    """The newest message's uid (so a new mail trigger only reacts to mail that arrives later)."""
    m = _imap(a)
    try:
        m.select(_imap_quote(a["config"].get("imap_folder") or "INBOX"), readonly=True)
        typ, data = m.uid("SEARCH", None, "ALL")
        return max((int(u) for u in (data[0] or b"").split()), default=0)
    finally:
        try:
            m.logout()
        except Exception:
            pass


def test(a: dict, send_to: str = "") -> str:
    msgs = []
    if a["config"].get("smtp_host"):
        if send_to:
            send(a, send_to, "SysAdmin.IA test message", "This is a test message from SysAdmin.IA. Sending works.")
            msgs.append(f"Sending works (test message sent to {send_to}).")
        else:
            s = _smtp(a)
            s.quit()
            msgs.append("Sending: signed in to the outgoing server.")
    if a["config"].get("imap_host"):
        m = _imap(a)
        try:
            typ, data = m.select(_imap_quote(a["config"].get("imap_folder") or "INBOX"), readonly=True)
            count = int(data[0]) if typ == "OK" and data and data[0] else 0
        finally:
            m.logout()
        msgs.append(f"Reading: signed in, {count} message(s) in {a['config'].get('imap_folder') or 'INBOX'}.")
    return " ".join(msgs)
