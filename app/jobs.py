"""Jobs: "when <trigger>, do <actions>" automation.

A job has one or more triggers (it runs when any of them fires) and a list of actions that
run in order. Text fields in actions can use placeholders: {{var.NAME}} (global variables),
{{sys.hostname}} and other built-ins (see variables.py), {{trigger.summary}} /
{{trigger.value}} / … from the trigger, and {{last.output}} / {{steps.N.output}} from
earlier actions.

Condition triggers (disk, CPU, memory, temperature, …) fire when the condition starts being
true, then again every "repeat" minutes while it stays true (0 = only once), and optionally
once more when things are back to normal.

The engine (JobEngine) runs inside the app and checks triggers every 20 seconds.
"""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from . import db
from . import variables as V

log = logging.getLogger("sysadmin-ia.jobs")

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FOLLOWUP_KEY = "job_update_followup"   # app_settings key: an update a job is installing
REPEAT = {"key": "repeat_minutes", "label": "Run again every … minutes while it lasts", "type": "number",
          "min": 0, "max": 100000, "default": 60, "help": "0 = only once, until it's back to normal."}
RECOVER = {"key": "notify_recovery", "label": "Also run when it's back to normal", "type": "bool", "default": False,
           "help": "In actions, {{trigger.state}} is “alert” or “recovered”."}
COMMON_TRIGGER_PLACEHOLDERS = ["trigger.summary", "trigger.state", "trigger.value", "trigger.threshold", "trigger.time"]

# ---------------------------------------------------------------- catalog

TRIGGERS: dict[str, dict] = {
    "schedule": {
        "label": "Schedule", "icon": "clock", "group": "Time",
        "description": "At set times: every few minutes, daily, weekly, monthly, or a cron expression.",
        "fields": [
            {"key": "mode", "label": "Repeat", "type": "select", "default": "daily",
             "options": [["interval", "Every few minutes"], ["daily", "Every day"], ["weekly", "Every week"],
                         ["monthly", "Every month"], ["cron", "Cron expression (advanced)"]]},
            {"key": "every_minutes", "label": "Every … minutes", "type": "number", "min": 1, "max": 525600, "default": 15,
             "show_if": {"mode": ["interval"]}},
            {"key": "time", "label": "At", "type": "time", "default": "08:00", "show_if": {"mode": ["daily", "weekly", "monthly"]}},
            {"key": "weekdays", "label": "On", "type": "weekdays", "default": ["mon"], "show_if": {"mode": ["weekly"]}},
            {"key": "day", "label": "Day of the month", "type": "number", "min": 1, "max": 31, "default": 1,
             "show_if": {"mode": ["monthly"]}, "help": "In shorter months, the last day is used."},
            {"key": "cron", "label": "Cron expression", "type": "cron", "default": "0 8 * * 1-5",
             "show_if": {"mode": ["cron"]}, "help": "minute hour day-of-month month day-of-week, e.g. 0 8 * * 1-5 (weekdays at 08:00)."},
        ],
        "placeholders": ["trigger.summary", "trigger.time"],
    },
    "disk_usage": {
        "label": "Disk space", "icon": "internal", "group": "Server",
        "description": "When a drive is fuller than a percentage.",
        "fields": [
            {"key": "mount", "label": "Drive", "type": "mount", "default": "*", "help": "“Any drive” checks every mounted drive."},
            {"key": "threshold", "label": "Above (% used)", "type": "number", "min": 1, "max": 100, "default": 90},
            REPEAT, RECOVER,
        ],
        "placeholders": COMMON_TRIGGER_PLACEHOLDERS + ["trigger.mount", "trigger.free", "trigger.total"],
    },
    "cpu_usage": {
        "label": "CPU usage", "icon": "monitor", "group": "Server",
        "description": "When the processor is busier than a percentage for a while.",
        "fields": [
            {"key": "threshold", "label": "Above (%)", "type": "number", "min": 1, "max": 100, "default": 90},
            {"key": "for_minutes", "label": "For at least … minutes", "type": "number", "min": 0, "max": 1440, "default": 5},
            REPEAT, RECOVER,
        ],
        "placeholders": COMMON_TRIGGER_PLACEHOLDERS,
    },
    "memory_usage": {
        "label": "Memory usage", "icon": "monitor", "group": "Server",
        "description": "When RAM use is above a percentage.",
        "fields": [
            {"key": "threshold", "label": "Above (%)", "type": "number", "min": 1, "max": 100, "default": 90},
            {"key": "for_minutes", "label": "For at least … minutes", "type": "number", "min": 0, "max": 1440, "default": 2},
            REPEAT, RECOVER,
        ],
        "placeholders": COMMON_TRIGGER_PLACEHOLDERS,
    },
    "load_average": {
        "label": "System load", "icon": "monitor", "group": "Server",
        "description": "When the 5-minute load average per CPU core is above a value (1.0 = fully busy).",
        "fields": [
            {"key": "threshold", "label": "Above (per core)", "type": "number", "min": 0.1, "max": 100, "step": 0.1, "default": 1.5},
            REPEAT, RECOVER,
        ],
        "placeholders": COMMON_TRIGGER_PLACEHOLDERS,
    },
    "temperature": {
        "label": "Temperature", "icon": "monitor", "group": "Server",
        "description": "When a hardware sensor (CPU, disk, …) is hotter than a temperature.",
        "fields": [
            {"key": "sensor", "label": "Sensor", "type": "sensor", "default": "*", "help": "“Any sensor” checks all of them."},
            {"key": "threshold", "label": "Above (°C)", "type": "number", "min": 1, "max": 150, "default": 80},
            REPEAT, RECOVER,
        ],
        "placeholders": COMMON_TRIGGER_PLACEHOLDERS + ["trigger.sensor"],
    },
    "service_failed": {
        "label": "Service failed", "icon": "settings", "group": "Server",
        "description": "When a system service (systemd unit) has failed.",
        "fields": [
            {"key": "service", "label": "Service", "type": "text", "default": "", "placeholder": "e.g. nginx (empty = any service)",
             "pattern": r"^[A-Za-z0-9@._:-]*$"},
            REPEAT, RECOVER,
        ],
        "placeholders": COMMON_TRIGGER_PLACEHOLDERS + ["trigger.services"],
    },
    "update_available": {
        "label": "Update available", "icon": "refresh", "group": "App",
        "description": "When a newer version of SysAdmin.IA is published (checked hourly).",
        "fields": [],
        "placeholders": ["trigger.summary", "update.current", "update.latest", "update.notes"],
    },
    "app_started": {
        "label": "App started", "icon": "login", "group": "App",
        "description": "When SysAdmin.IA starts, e.g. after a reboot or an update.",
        "fields": [],
        "placeholders": ["trigger.summary", "trigger.time"],
    },
    "mail_received": {
        "label": "Mail received", "icon": "mail", "group": "Mail",
        "description": "When a new unread message arrives in a mail account (optionally from someone or with a subject).",
        "fields": [
            {"key": "account", "label": "Mail account", "type": "mail_account", "required": True, "need": "read"},
            {"key": "from_contains", "label": "From contains", "type": "text", "default": "", "placeholder": "optional, e.g. alerts@example.com"},
            {"key": "subject_contains", "label": "Subject contains", "type": "text", "default": "", "placeholder": "optional"},
            {"key": "check_minutes", "label": "Check every … minutes", "type": "number", "min": 1, "max": 1440, "default": 5},
            {"key": "mark_read", "label": "Mark matching messages as read", "type": "bool", "default": False},
        ],
        "placeholders": ["trigger.summary", "mail.from", "mail.subject", "mail.date", "mail.body"],
    },
}

ACTIONS: dict[str, dict] = {
    "send_mail": {
        "label": "Send an email", "icon": "mail",
        "description": "Send a message, e.g. a warning with the details of what happened.",
        "fields": [
            {"key": "account", "label": "Send with", "type": "mail_account", "required": True, "need": "send"},
            {"key": "to", "label": "To", "type": "text", "required": True, "placeholder": "you@example.com, {{var.alert_email}}"},
            {"key": "subject", "label": "Subject", "type": "text", "required": True,
             "default": "[{{sys.hostname}}] {{trigger.summary}}"},
            {"key": "body", "label": "Message", "type": "textarea", "required": True,
             "default": "Hello,\n\n{{trigger.summary}}\n\nServer: {{sys.hostname}} ({{sys.ip}})\nTime: {{sys.datetime}}\n\n— SysAdmin.IA {{sys.url}}"},
        ],
        "provides": [],
    },
    "webhook": {
        "label": "Call a webhook", "icon": "login",
        "description": "Send a message to Slack, Teams, Discord, ntfy or any web address.",
        "fields": [
            {"key": "url", "label": "Address (URL)", "type": "text", "required": True, "placeholder": "https://hooks.slack.com/services/…"},
            {"key": "method", "label": "Method", "type": "select", "default": "POST", "options": [["POST", "POST"], ["GET", "GET"], ["PUT", "PUT"]]},
            {"key": "content_type", "label": "Body format", "type": "select", "default": "application/json",
             "options": [["application/json", "JSON"], ["text/plain", "Plain text"]]},
            {"key": "body", "label": "Body", "type": "textarea", "default": "{\"text\": \"{{trigger.summary}} on {{sys.hostname}}\"}"},
        ],
        "provides": ["last.output"],
    },
    "run_command": {
        "label": "Run a command", "icon": "console",
        "description": "Run a shell command on the server (as the app's user; sudo only with full access). Its output can be used by later actions.",
        "fields": [
            {"key": "command", "label": "Command", "type": "textarea", "required": True, "mono": True, "placeholder": "df -h"},
            {"key": "timeout", "label": "Time limit (seconds)", "type": "number", "min": 1, "max": 3600, "default": 120},
        ],
        "provides": ["last.output", "last.exit_code"],
    },
    "ask_ai": {
        "label": "Ask the AI", "icon": "ai",
        "description": "Ask an AI connection a question (e.g. “explain this and suggest a fix”); its answer can be used in later actions.",
        "fields": [
            {"key": "connection", "label": "AI connection", "type": "ai_connection", "default": ""},
            {"key": "prompt", "label": "Question", "type": "textarea", "required": True,
             "default": "Explain briefly what this means and what an administrator should do:\n{{trigger.summary}}\n\n{{last.output}}"},
        ],
        "provides": ["ai.answer", "last.output"],
    },
    "check_updates": {
        "label": "Check for updates", "icon": "refresh",
        "description": "Check GitHub for a newer version of SysAdmin.IA, and optionally install it.",
        "fields": [{"key": "install", "label": "Install it if there is one", "type": "bool", "default": False,
                    "help": "The app restarts during the installation."}],
        "provides": ["update.available", "update.current", "update.latest", "last.output"],
    },
    "set_variable": {
        "label": "Set a variable", "icon": "edit",
        "description": "Store a value in a global variable (created if needed), e.g. the time of the last alert.",
        "fields": [
            {"key": "name", "label": "Variable", "type": "variable", "required": True},
            {"key": "value", "label": "New value", "type": "text", "default": "{{sys.datetime}}"},
        ],
        "provides": [],
    },
}


class JobError(ValueError):
    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.details = details


def catalog() -> dict:
    from . import ai_connections as ai
    from . import mail, monitor
    try:
        st = monitor.stats()
    except Exception:
        st = {"disks": [], "temps": []}
    return {
        "triggers": TRIGGERS, "actions": ACTIONS, "weekdays": WEEKDAYS,
        "mounts": [{"value": d["mount"], "label": f"{d['mount']} ({d['percent']:.0f}% used)"} for d in st.get("disks", [])],
        "sensors": [{"value": t["label"], "label": f"{t['label']} (now {t['current']:.0f} °C)"} for t in st.get("temps", [])],
        "mail_accounts": [{"value": a["id"], "label": a["name"], "send": bool(a["config"].get("smtp_host")),
                           "read": bool(a["config"].get("imap_host"))} for a in mail.list_all()],
        "ai_connections": [{"value": c["id"], "label": c["name"]} for c in ai.list_all() if c["enabled"]],
        "variables": [{"name": v["name"], "description": v["description"], "secret": v["secret"]} for v in V.list_all()],
        "system_placeholders": ["sys.hostname", "sys.ip", "sys.date", "sys.time", "sys.datetime", "sys.weekday",
                                "sys.os", "sys.version", "sys.url"],
        "monitor_placeholders": [{"key": f"mon.{m['key']}", "label": m["label"], "group": m["group"]}
                                 for m in V.monitor_values()],
    }


# ---------------------------------------------------------------- schedules

def _parse_time(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (s or "").strip())
    if not m:
        raise JobError("Times use the 24-hour format HH:MM, e.g. 08:30.")
    return int(m.group(1)), int(m.group(2))


_CRON_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def _cron_field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
            if step < 1:
                raise ValueError
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        if a < lo or b > hi or a > b:
            raise ValueError
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr: str) -> list[set[int]]:
    parts = (expr or "").split()
    if len(parts) != 5:
        raise JobError("A cron expression has 5 parts: minute hour day-of-month month day-of-week.")
    try:
        sets = [_cron_field(p, lo, hi) for p, (lo, hi) in zip(parts, _CRON_RANGES)]
    except ValueError:
        raise JobError(f"Invalid cron expression: {expr}")
    if 7 in sets[4]:
        sets[4].add(0)
    sets.append({0: "*" == parts[2], 1: "*" == parts[4]})  # type: ignore[arg-type]  # which day fields are unrestricted
    return sets


def _cron_match(sets, dt: datetime) -> bool:
    mins, hours, dom, months, dow, star = sets
    if dt.minute not in mins or dt.hour not in hours or dt.month not in months:
        return False
    dom_ok, dow_ok = dt.day in dom, (dt.isoweekday() % 7) in dow
    if star[0] or star[1]:  # standard cron: if either day field is "*", both must match
        return dom_ok and dow_ok
    return dom_ok or dow_ok  # both restricted: either matches


def next_run(t: dict, after: float) -> float:
    """The next time a schedule trigger fires, after the given timestamp."""
    mode = t.get("mode", "daily")
    if mode == "interval":
        return after + max(1, int(t.get("every_minutes") or 15)) * 60
    base = datetime.fromtimestamp(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
    if mode == "cron":
        sets = parse_cron(t.get("cron", ""))
        dt = base
        for _ in range(366 * 24 * 60):
            if _cron_match(sets, dt):
                return dt.timestamp()
            dt += timedelta(minutes=1)
        raise JobError("That cron expression never matches.")
    h, m = _parse_time(t.get("time") or "08:00")
    day = base.replace(hour=h, minute=m)
    if day < base:
        day += timedelta(days=1)
    for _ in range(400):
        if mode == "daily":
            return day.timestamp()
        if mode == "weekly" and WEEKDAYS[day.weekday()] in (t.get("weekdays") or ["mon"]):
            return day.timestamp()
        if mode == "monthly":
            want = min(int(t.get("day") or 1), calendar.monthrange(day.year, day.month)[1])
            if day.day == want:
                return day.timestamp()
        day += timedelta(days=1)
    raise JobError("Couldn't work out when this schedule runs next.")


# ---------------------------------------------------------------- descriptions (plain English)

def _again(t: dict) -> str:
    r = int(t.get("repeat_minutes") or 0)
    s = f", again every {r} min while it lasts" if r else ""
    return s + (", and when it's back to normal" if t.get("notify_recovery") else "")


def describe_trigger(t: dict) -> str:
    ty = t.get("type")
    if ty == "schedule":
        mode = t.get("mode")
        if mode == "interval":
            n = int(t.get("every_minutes") or 15)
            return f"Every {n} minutes" if n % 60 else f"Every {n // 60} hour{'s' if n // 60 > 1 else ''}"
        if mode == "daily":
            return f"Every day at {t.get('time')}"
        if mode == "weekly":
            days = [d.capitalize() for d in WEEKDAYS if d in (t.get("weekdays") or [])]
            return f"Every {', '.join(days) or 'week'} at {t.get('time')}"
        if mode == "monthly":
            return f"Every month on day {t.get('day')} at {t.get('time')}"
        return f"Cron schedule “{t.get('cron')}”"
    if ty == "disk_usage":
        where = "any drive" if t.get("mount") in ("", "*", None) else f"drive {t.get('mount')}"
        return f"When {where} is over {t.get('threshold')}% full{_again(t)}"
    if ty == "cpu_usage":
        dur = int(t.get("for_minutes") or 0)
        return f"When CPU usage is over {t.get('threshold')}%{f' for {dur} min' if dur else ''}{_again(t)}"
    if ty == "memory_usage":
        dur = int(t.get("for_minutes") or 0)
        return f"When memory usage is over {t.get('threshold')}%{f' for {dur} min' if dur else ''}{_again(t)}"
    if ty == "load_average":
        return f"When the load per CPU core is over {t.get('threshold')}{_again(t)}"
    if ty == "temperature":
        where = "any sensor" if t.get("sensor") in ("", "*", None) else f"sensor {t.get('sensor')}"
        return f"When {where} is hotter than {t.get('threshold')} °C{_again(t)}"
    if ty == "service_failed":
        return f"When {'service ' + t['service'] if t.get('service') else 'any service'} has failed{_again(t)}"
    if ty == "update_available":
        return "When a new version of SysAdmin.IA is available"
    if ty == "app_started":
        return "When SysAdmin.IA starts"
    if ty == "mail_received":
        from . import mail
        a = mail.get(t.get("account"))
        bits = [f"from “{t['from_contains']}”" if t.get("from_contains") else "",
                f"with “{t['subject_contains']}” in the subject" if t.get("subject_contains") else ""]
        return f"When new mail arrives in {a['name'] if a else '(deleted account)'} {' '.join(b for b in bits if b)}".strip()
    return ty or "?"


def describe_action(a: dict) -> str:
    ty = a.get("type")
    if ty == "send_mail":
        from . import mail
        acc = mail.get(a.get("account"))
        return f"Email {a.get('to')} via {acc['name'] if acc else '(deleted account)'}"
    if ty == "webhook":
        host = re.sub(r"^https?://([^/]+).*$", r"\1", a.get("url") or "")
        return f"Call webhook {host}"
    if ty == "run_command":
        cmd = (a.get("command") or "").strip().splitlines()
        return f"Run “{cmd[0][:60] if cmd else ''}”"
    if ty == "ask_ai":
        from . import ai_connections as ai
        c = ai.get(int(a["connection"])) if a.get("connection") else ai.default()
        return f"Ask {c['name'] if c else 'the AI'}"
    if ty == "check_updates":
        return "Check for updates" + (" and install them" if a.get("install") else "")
    if ty == "set_variable":
        return f"Set variable {a.get('name')}"
    return ty or "?"


# ---------------------------------------------------------------- validation & storage

def _clean_items(items: list, catalog_: dict, what: str) -> list[dict]:
    from . import ai_connections as ai
    from . import mail
    if not isinstance(items, list):
        raise JobError(f"Invalid {what}s.")
    out = []
    for i, item in enumerate(items, 1):
        ty = (item or {}).get("type")
        spec = catalog_.get(ty)
        if not spec:
            raise JobError(f"{what.capitalize()} {i}: choose a type.")
        clean: dict[str, Any] = {"type": ty}
        label = f"{what.capitalize()} {i} ({spec['label']})"
        for f in spec["fields"]:
            k = f["key"]
            v = item.get(k, f.get("default"))
            visible = all(clean.get(dk) in vals for dk, vals in (f.get("show_if") or {}).items())
            ft = f["type"]
            if ft == "bool":
                clean[k] = bool(v)
                continue
            if ft == "number":
                try:
                    num = float(v)
                except (TypeError, ValueError):
                    if not visible:
                        clean[k] = f.get("default")
                        continue
                    raise JobError(f"{label}: “{f['label']}” must be a number.")
                if visible and not (f.get("min", -1e18) <= num <= f.get("max", 1e18)):
                    raise JobError(f"{label}: “{f['label']}” must be between {f.get('min')} and {f.get('max')}.")
                clean[k] = int(num) if num == int(num) and not f.get("step") else num
                continue
            if ft == "weekdays":
                days = [d for d in (v or []) if d in WEEKDAYS]
                if visible and not days:
                    raise JobError(f"{label}: pick at least one day.")
                clean[k] = days
                continue
            v = "" if v is None else str(v).strip() if ft not in ("textarea",) else str(v)
            if visible and f.get("required") and not str(v).strip():
                raise JobError(f"{label}: “{f['label']}” is required.")
            if not visible:
                clean[k] = v
                continue
            if ft == "select" and v not in [o[0] for o in f["options"]]:
                raise JobError(f"{label}: invalid choice for “{f['label']}”.")
            if ft == "time":
                _parse_time(v)
            if ft == "cron":
                parse_cron(v)
            if f.get("pattern") and not re.fullmatch(f["pattern"], v):
                raise JobError(f"{label}: invalid “{f['label']}”.")
            if ft == "mail_account":
                acc = mail.get(v)
                if not acc:
                    raise JobError(f"{label}: choose a mail account (add one in Mail accounts first).")
                if f.get("need") == "send" and not acc["config"].get("smtp_host"):
                    raise JobError(f"{label}: “{acc['name']}” can't send mail (no outgoing server).")
                if f.get("need") == "read" and not acc["config"].get("imap_host"):
                    raise JobError(f"{label}: “{acc['name']}” can't read mail (no incoming server).")
                v = acc["id"]
            if ft == "ai_connection" and v and not ai.get(int(v)):
                raise JobError(f"{label}: that AI connection no longer exists.")
            if ft == "variable" and not V.NAME_RE.fullmatch(v):
                raise JobError(f"{label}: variable names start with a letter and use letters, digits and _.")
            if k == "url" and not re.match(r"^https?://", v) and "{{" not in v:
                raise JobError(f"{label}: the address must start with http:// or https://")
            if isinstance(v, str) and len(v) > 20000:
                raise JobError(f"{label}: “{f['label']}” is too long.")
            clean[k] = v
        if ty == "schedule":
            next_run(clean, time.time())  # validates the schedule as a whole
        out.append(clean)
    return out


def _row(r) -> dict:
    d = dict(r)
    for k in ("triggers", "actions", "options", "state"):
        d[k] = json.loads(d[k] or ("[]" if k in ("triggers", "actions") else "{}"))
    d["enabled"] = bool(d["enabled"])
    return d


def public(j: dict) -> dict:
    out = {k: j[k] for k in ("id", "name", "description", "enabled", "triggers", "actions", "options",
                             "last_run", "last_status", "created_at", "updated_at")}
    out["trigger_summaries"] = [describe_trigger(t) for t in j["triggers"]]
    out["action_summaries"] = [describe_action(a) for a in j["actions"]]
    nexts = [s.get("next") for s in j["state"].values() if isinstance(s, dict) and s.get("next")]
    out["next_run"] = min(nexts) if nexts and j["enabled"] else None
    out["alerting"] = any(isinstance(s, dict) and s.get("alert") for s in j["state"].values())
    return out


def list_all() -> list[dict]:
    return [_row(r) for r in db.query("SELECT * FROM jobs ORDER BY name COLLATE NOCASE")]


def get(job_id: int) -> dict | None:
    r = db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
    return _row(r) if r else None


def _clean_job(data: dict) -> dict:
    name = " ".join(str(data.get("name") or "").split())
    if not name:
        raise JobError("Give the job a name.")
    triggers = _clean_items(data.get("triggers") or [], TRIGGERS, "trigger")
    actions = _clean_items(data.get("actions") or [], ACTIONS, "action")
    if not triggers:
        raise JobError("Add at least one trigger (when the job runs).")
    if not actions:
        raise JobError("Add at least one action (what the job does).")
    opts = data.get("options") or {}
    return {"name": name[:80], "description": str(data.get("description") or "").strip()[:500],
            "enabled": bool(data.get("enabled", True)), "triggers": triggers, "actions": actions,
            "options": {"stop_on_error": bool(opts.get("stop_on_error", True))}}


def create(data: dict) -> dict:
    j = _clean_job(data)
    now = time.time()
    jid = db.execute(
        "INSERT INTO jobs (name, description, enabled, triggers, actions, options, state, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?, '{}', ?, ?)",
        (j["name"], j["description"], int(j["enabled"]), json.dumps(j["triggers"]), json.dumps(j["actions"]),
         json.dumps(j["options"]), now, now))
    ENGINE.prime(jid)
    return get(jid)


def update(job_id: int, data: dict) -> dict:
    if not get(job_id):
        raise JobError("That job no longer exists.")
    j = _clean_job(data)
    db.execute("UPDATE jobs SET name=?, description=?, enabled=?, triggers=?, actions=?, options=?, updated_at=? WHERE id=?",
               (j["name"], j["description"], int(j["enabled"]), json.dumps(j["triggers"]), json.dumps(j["actions"]),
                json.dumps(j["options"]), time.time(), job_id))
    ENGINE.prime(job_id)
    return get(job_id)


def set_enabled(job_id: int, enabled: bool) -> None:
    db.execute("UPDATE jobs SET enabled=?, updated_at=? WHERE id=?", (int(enabled), time.time(), job_id))
    ENGINE.prime(job_id)


def delete(job_id: int) -> None:
    db.execute("DELETE FROM job_runs WHERE job_id=?", (job_id,))
    db.execute("DELETE FROM jobs WHERE id=?", (job_id,))


def runs(job_id: int, limit: int = 30) -> list[dict]:
    rows = db.query("SELECT * FROM job_runs WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit))
    return [{**dict(r), "log": json.loads(r["log"] or "[]")} for r in rows]


def _save_state(job_id: int, state: dict) -> None:
    db.execute("UPDATE jobs SET state=? WHERE id=?", (json.dumps(state), job_id))


def _sig(t: dict) -> str:
    return hashlib.sha1(json.dumps(t, sort_keys=True).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- actions

# Each action returns (one-line result, details shown when the history entry is expanded).
# Errors are raised as JobError(message, details=…).

async def _act_send_mail(a: dict, ctx: dict) -> tuple[str, str]:
    from . import mail
    acc = mail.get(a["account"])
    if not acc:
        raise JobError("The mail account was deleted.")
    to, subject, body = (V.render(a.get(k, ""), ctx) for k in ("to", "subject", "body"))
    details = f"From: {acc['config'].get('from_address')} (account “{acc['name']}”)\nTo: {to}\nSubject: {subject}\n\n{body}"
    try:
        sent = await asyncio.to_thread(mail.send, acc, to, subject, body)
    except Exception as e:
        raise JobError(str(e), details=details)
    return f"{sent} — “{subject[:120]}”", details


async def _act_webhook(a: dict, ctx: dict) -> str:
    url = V.render(a["url"], ctx).strip()
    if not re.match(r"^https?://", url):
        raise JobError(f"Invalid webhook address: {V.mask(url)}")
    body = V.render(a.get("body") or "", ctx)
    ctype = a.get("content_type") or "application/json"
    if ctype == "application/json" and body:
        try:
            json.loads(body)
        except json.JSONDecodeError as e:
            raise JobError(f"The body isn't valid JSON after filling in the placeholders ({e.msg}). "
                           "Tip: values with quotes or line breaks can break JSON.")
    method = a.get("method") or "POST"
    data = body.encode() if method != "GET" and body else None

    def call():
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": ctype, "User-Agent": "SysAdmin.IA-jobs"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read(4000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise JobError(f"The webhook answered HTTP {e.code}: {e.read(300).decode('utf-8', 'replace')}")
        except (urllib.error.URLError, OSError) as e:
            raise JobError(f"Could not reach the webhook: {getattr(e, 'reason', e)}")

    host = re.sub(r"^https?://([^/?#]+).*$", r"\1", url)
    req_details = f"{method} {url}\nContent-Type: {ctype}\n\n{body}" if data else f"{method} {url}"
    try:
        status, text = await asyncio.to_thread(call)
    except JobError as e:
        raise JobError(str(e), details=req_details)
    ctx["last"] = {"output": text}
    return f"{method} {host} → HTTP {status}", f"{req_details}\n\n— Response (HTTP {status}) —\n{text or '(empty)'}"


async def _act_run_command(a: dict, ctx: dict) -> tuple[str, str]:
    from .config import settings
    cmd = V.render(a["command"], ctx)
    timeout = int(a.get("timeout") or 120)
    proc = await asyncio.create_subprocess_exec(
        "bash", "-lc", cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL, cwd=str(settings.data_dir), start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise JobError(f"The command took longer than {timeout} seconds and was stopped.", details=f"$ {cmd}")
    text = out.decode("utf-8", "replace").strip()
    if len(text) > 8000:
        text = text[:4000] + "\n…\n" + text[-3900:]
    ctx["last"] = {"output": text, "exit_code": proc.returncode}
    details = f"$ {cmd}\n{text or '(no output)'}\n\n[exit code {proc.returncode}]"
    if proc.returncode != 0:
        last_line = (text.strip().splitlines() or [""])[-1][:200]
        raise JobError(f"The command failed (exit code {proc.returncode}){': ' + last_line if last_line else ''}", details=details)
    n = len(text.splitlines())
    first = (text.splitlines() or [""])[0][:160]
    return (f"Exit code 0 · {n} line{'s' if n != 1 else ''} of output" + (f": {first}" if first else "")
            if text else "Exit code 0 (no output)."), details


async def _act_ask_ai(a: dict, ctx: dict) -> tuple[str, str]:
    from . import ai_connections as ai
    from . import providers
    conn = ai.get(int(a["connection"])) if a.get("connection") else ai.default()
    if not conn or not conn["enabled"]:
        raise JobError("No enabled AI connection to ask.")
    blocked = ai.limit_reached(conn)
    if blocked:
        raise JobError(blocked)
    prompt = V.render(a["prompt"], ctx)
    try:
        answer, usage = await providers.for_connection(conn).complete(
            prompt, "You help a Linux server administrator. Answer concisely in plain text (no Markdown).")
    except Exception as e:
        raise JobError(str(e), details=f"Connection: {conn['name']} ({conn['config'].get('model')})\n\nQuestion:\n{prompt}")
    tokens = ""
    if usage:
        ai.record_usage(conn["id"], usage)
        total = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        tokens = f" · {total:,} tokens"
    ctx["ai"] = {"answer": answer}
    ctx["last"] = {"output": answer}
    first = " ".join(answer.split())[:200]
    return (f"{conn['name']} answered{tokens}: {first}{'…' if len(answer) > 200 else ''}",
            f"Connection: {conn['name']} ({conn['config'].get('model')})\n\nQuestion:\n{prompt}\n\nAnswer:\n{answer}")


async def _act_check_updates(a: dict, ctx: dict) -> tuple[str, str]:
    from . import updater
    s = await updater.check(force=True)
    if s.get("error"):
        raise JobError(f"Couldn't check for updates: {s['error']}")
    latest = (s.get("latest") or {}).get("version")
    pending = s.get("pending") or []
    ctx["update"] = {"available": "yes" if s["update_available"] else "no", "current": s["current"], "latest": latest or ""}
    notes = "\n\n".join(f"— {p['version']}{' · ' + p['published_at'][:10] if p.get('published_at') else ''} —\n"
                        f"{(p.get('notes') or '').strip() or '(no release notes)'}" for p in reversed(pending))
    source = f"Checked github.com/{s['repo']} at {time.strftime('%H:%M:%S')}."
    if not s["update_available"]:
        msg = f"No update found: {s['current']} is the latest version."
        details = source
    else:
        incl = f" (includes {len(pending)} versions: {', '.join(p['version'] for p in pending)})" if len(pending) > 1 else ""
        found = f"Update found: {s['current']} → {latest}{incl}."
        details = f"{source}\n\nWhat's new:\n\n{notes}" if notes else source
        if not a.get("install"):
            msg = f"{found} Not installed (this action only checks)."
        else:
            try:
                r = await updater.apply()
            except Exception as e:
                raise JobError(f"{found} Installing it failed: {e}", details=details)
            if r.get("mode") == "full":
                msg = (f"{found} Installing it now: the new version's installer is running in the background and "
                       "the app restarts when it finishes. The result is added to this run.")
            else:
                msg = f"{found} Installed (app code); the app restarts in a few seconds. The result is added to this run."
                if r.get("restart"):
                    # Code-only update: restart (systemd starts the new code) a moment after this run is saved.
                    import os
                    import signal
                    asyncio.get_running_loop().call_later(5, lambda: os.kill(os.getpid(), signal.SIGTERM))
            ctx["_update_followup"] = {"from": s["current"], "to": r["to"], "mode": r.get("mode")}
            log_lines = "\n".join(r.get("log") or [])
            if log_lines:
                details += f"\n\nInstaller:\n{log_lines}"
    ctx["last"] = {"output": msg}
    return msg, details


async def _act_set_variable(a: dict, ctx: dict) -> tuple[str, str]:
    old = V.get(a["name"])
    value = V.render(a.get("value") or "", ctx)
    V.set_value(a["name"], value)
    if old is None:
        return f"Created {a['name']} = “{value[:200]}”", ""
    if old == value:
        return f"{a['name']} unchanged (“{value[:200]}”)", ""
    return f"{a['name']}: “{old[:100]}” → “{value[:200]}”", f"Before:\n{old}\n\nAfter:\n{value}"


ACTION_FUNCS = {"send_mail": _act_send_mail, "webhook": _act_webhook, "run_command": _act_run_command,
                "ask_ai": _act_ask_ai, "check_updates": _act_check_updates, "set_variable": _act_set_variable}


# ---------------------------------------------------------------- engine

class JobEngine:
    TICK = 20

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.running: set[int] = set()
        self.started_at = time.time()
        self._last_update_check = 0.0
        self._update_info: dict | None = None

    # -- lifecycle
    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        await asyncio.sleep(3)
        asyncio.get_running_loop().create_task(self.finish_update_followup())
        for j in list_all():  # "App started" triggers
            if j["enabled"] and any(t["type"] == "app_started" for t in j["triggers"]):
                self._spawn(j, {"summary": "SysAdmin.IA started", "time": time.strftime("%Y-%m-%d %H:%M:%S")},
                            "App started")
        while True:
            try:
                await self.tick()
            except Exception:  # never let the scheduler die
                log.exception("job engine tick failed")
            await asyncio.sleep(self.TICK)

    def prime(self, job_id: int) -> None:
        """Reset trigger state after a job is created/edited (keeps state of unchanged triggers)."""
        j = get(job_id)
        if not j:
            return
        old = j["state"]
        new = {}
        now = time.time()
        for i, t in enumerate(j["triggers"]):
            key, sig = str(i), _sig(t)
            st = old.get(key) if isinstance(old.get(key), dict) and old[key].get("sig") == sig else {"sig": sig}
            if t["type"] == "schedule" and not st.get("next"):
                try:
                    st["next"] = next_run(t, now)
                except JobError:
                    st["next"] = None
            new[key] = st
        _save_state(job_id, new)

    # -- checking triggers
    async def tick(self) -> None:
        jobs = [j for j in list_all() if j["enabled"]]
        if not jobs:
            return
        types = {t["type"] for j in jobs for t in j["triggers"]}
        snap: dict[str, Any] = {}
        if types & {"disk_usage", "cpu_usage", "memory_usage", "load_average", "temperature"}:
            from . import monitor
            snap["stats"] = await asyncio.to_thread(monitor.stats)
        if "service_failed" in types:
            from . import monitor
            snap["services"] = await monitor.services()
        if "update_available" in types and time.time() - self._last_update_check > 3600:
            from . import updater
            self._last_update_check = time.time()
            try:
                self._update_info = await updater.check(force=False)
            except Exception:
                self._update_info = None
        now = time.time()
        for j in jobs:
            state = j["state"]
            changed = False
            for i, t in enumerate(j["triggers"]):
                key = str(i)
                st = state.get(key) if isinstance(state.get(key), dict) else None
                if not st or st.get("sig") != _sig(t):
                    st = {"sig": _sig(t)}
                    if t["type"] == "schedule":
                        st["next"] = next_run(t, now)
                    state[key] = st
                    changed = True
                fired = await self._check(j, t, st, snap, now)
                changed = True if fired is not None or t["type"] in ("schedule", "mail_received") else changed
                for ctx, why in fired or []:
                    self._spawn(j, ctx, why)
            if changed:
                _save_state(j["id"], state)

    async def _check(self, j: dict, t: dict, st: dict, snap: dict, now: float) -> list | None:
        ty = t["type"]
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if ty == "schedule":
            if st.get("next") and now >= st["next"]:
                st["next"] = next_run(t, now)
                return [({"summary": describe_trigger(t), "time": stamp}, describe_trigger(t))]
            return None
        if ty == "update_available":
            info = self._update_info
            if info and info.get("update_available") and info.get("latest"):
                ver = info["latest"]["version"]
                if st.get("notified") != ver:
                    st["notified"] = ver
                    ctx = {"summary": f"SysAdmin.IA {ver} is available (installed: {info['current']})", "time": stamp}
                    return [(ctx | {"_update": {"current": info["current"], "latest": ver,
                                                "notes": (info["latest"].get("notes") or "")[:2000]}}, ctx["summary"])]
            return None
        if ty == "mail_received":
            return await self._check_mail(t, st, now, stamp)
        if ty == "app_started":
            return None
        # condition triggers
        alert, ctx = self._condition(t, snap)
        if alert is None:
            return None
        ctx.update({"time": stamp, "threshold": t.get("threshold")})
        out = []
        need = int(t.get("for_minutes") or 0) * 60
        if alert:
            st.setdefault("since", now)
            if not st.get("alert"):
                if now - st["since"] >= need:
                    st.update(alert=True, last_fired=now)
                    out.append(({**ctx, "state": "alert"}, ctx["summary"]))
            else:
                rep = int(t.get("repeat_minutes") or 0) * 60
                if rep and now - st.get("last_fired", 0) >= rep:
                    st["last_fired"] = now
                    out.append(({**ctx, "state": "alert"}, ctx["summary"] + " (still)"))
        else:
            st.pop("since", None)
            if st.get("alert"):
                st["alert"] = False
                if t.get("notify_recovery"):
                    summary = ctx.get("ok_summary") or f"Back to normal: {describe_trigger(t)}"
                    out.append(({**ctx, "summary": summary, "state": "recovered"}, summary))
        return out

    def _condition(self, t: dict, snap: dict) -> tuple[bool | None, dict]:
        ty, thr = t["type"], float(t.get("threshold") or 0)
        stats = snap.get("stats") or {}
        if ty == "disk_usage":
            disks = stats.get("disks") or []
            pick = disks if t.get("mount") in ("", "*", None) else [d for d in disks if d["mount"] == t["mount"]]
            if not pick:
                return None, {}
            worst = max(pick, key=lambda d: d["percent"])
            gb = lambda b: f"{b / 1e9:.1f} GB"  # noqa: E731
            ctx = {"value": f"{worst['percent']:.0f}%", "mount": worst["mount"], "free": gb(worst["free"]), "total": gb(worst["total"]),
                   "summary": f"Disk {worst['mount']} is {worst['percent']:.0f}% full ({gb(worst['free'])} free of {gb(worst['total'])})",
                   "ok_summary": f"Disk {worst['mount']} is back below {thr:g}% ({worst['percent']:.0f}% full)"}
            return worst["percent"] >= thr, ctx
        if ty == "cpu_usage":
            v = float(stats.get("cpu_percent") or 0)
            return v >= thr, {"value": f"{v:.0f}%", "summary": f"CPU usage is {v:.0f}% (limit {thr:g}%)",
                              "ok_summary": f"CPU usage is back to {v:.0f}%"}
        if ty == "memory_usage":
            v = float((stats.get("mem") or {}).get("percent") or 0)
            return v >= thr, {"value": f"{v:.0f}%", "summary": f"Memory usage is {v:.0f}% (limit {thr:g}%)",
                              "ok_summary": f"Memory usage is back to {v:.0f}%"}
        if ty == "load_average":
            import os
            cores = os.cpu_count() or 1
            v = float((stats.get("load") or [0, 0, 0])[1]) / cores
            return v >= thr, {"value": f"{v:.2f}", "summary": f"Load per CPU core is {v:.2f} (limit {thr:g})",
                              "ok_summary": f"Load per CPU core is back to {v:.2f}"}
        if ty == "temperature":
            temps = stats.get("temps") or []
            pick = temps if t.get("sensor") in ("", "*", None) else [x for x in temps if x["label"] == t["sensor"]]
            if not pick:
                return None, {}
            hot = max(pick, key=lambda x: x["current"])
            return hot["current"] >= thr, {"value": f"{hot['current']:.0f} °C", "sensor": hot["label"],
                                           "summary": f"{hot['label']} is at {hot['current']:.0f} °C (limit {thr:g} °C)",
                                           "ok_summary": f"{hot['label']} cooled down to {hot['current']:.0f} °C"}
        if ty == "service_failed":
            svc = snap.get("services") or {}
            if not svc.get("available"):
                return None, {}
            failed = svc.get("failed") or []
            name = (t.get("service") or "").strip()
            if name:
                failed = [f for f in failed if f in (name, f"{name}.service")]
            listing = ", ".join(failed)
            return bool(failed), {"value": listing, "services": listing,
                                  "summary": f"Failed service{'s' if len(failed) > 1 else ''}: {listing}",
                                  "ok_summary": f"{name or 'All services'} running again"}
        return None, {}

    async def _check_mail(self, t: dict, st: dict, now: float, stamp: str) -> list | None:
        from . import mail
        if now - st.get("checked", 0) < int(t.get("check_minutes") or 5) * 60:
            return None
        st["checked"] = now
        acc = mail.get(t.get("account"))
        if not acc:
            return None
        try:
            if "uid" not in st:  # first check: only react to mail that arrives from now on
                st["uid"] = await asyncio.to_thread(mail.latest_uid, acc)
                st.pop("error", None)
                return None
            msgs, top = await asyncio.to_thread(mail.fetch_new, acc, int(st["uid"]), t.get("from_contains", ""),
                                                t.get("subject_contains", ""), bool(t.get("mark_read")), 5)
            st["uid"] = top
            st.pop("error", None)
        except Exception as e:
            st["error"] = str(e)[:300]
            return None
        out = []
        for m in msgs:
            summary = f"New mail from {m['from']}: {m['subject']}"
            out.append(({"summary": summary, "time": stamp, "_mail": m}, summary))
        return out

    # -- running jobs
    def _spawn(self, j: dict, trigger_ctx: dict, why: str) -> None:
        if j["id"] in self.running:
            log.info("job %s still running; skipped trigger %s", j["name"], why)
            return
        asyncio.get_running_loop().create_task(self.run(j["id"], trigger_ctx, why))

    async def run(self, job_id: int, trigger_ctx: dict, why: str, user: str = "jobs") -> dict:
        j = get(job_id)
        if not j:
            raise JobError("That job no longer exists.")
        if job_id in self.running:
            raise JobError("This job is already running.")
        self.running.add(job_id)
        started = time.time()
        run_id = db.execute("INSERT INTO job_runs (job_id, started_at, status, trigger) VALUES (?,?,?,?)",
                            (job_id, started, "running", V.mask(why)[:300]))
        ctx: dict[str, Any] = {"trigger": {k: v for k, v in trigger_ctx.items() if not k.startswith("_")},
                               "job": {"name": j["name"]}, "steps": {}, "last": {"output": ""}}
        if "_mail" in trigger_ctx:
            ctx["mail"] = trigger_ctx["_mail"]
        if "_update" in trigger_ctx:
            ctx["update"] = trigger_ctx["_update"]
        # First history line: what started the run, with the values the trigger saw.
        shown = {k: v for k, v in ctx["trigger"].items() if k not in ("summary",) and v not in (None, "")}
        trig_details = "\n".join(f"{k}: {v}" for k, v in shown.items())
        if "mail" in ctx:
            m = ctx["mail"]
            trig_details += f"\n\nFrom: {m.get('from')}\nSubject: {m.get('subject')}\nDate: {m.get('date')}\n\n{(m.get('body') or '')[:2000]}"
        entries: list[dict] = [{"step": 0, "action": "Trigger", "ok": True, "message": V.mask(why)[:500],
                                "details": V.mask(trig_details.strip())[:6000]}]
        status = "ok"
        try:
            for i, a in enumerate(j["actions"], 1):
                label = describe_action(a)
                t0 = time.time()
                try:
                    res = await ACTION_FUNCS[a["type"]](a, ctx)
                    msg, details = res if isinstance(res, tuple) else (str(res), "")
                    entries.append({"step": i, "action": label, "ok": True, "message": V.mask(str(msg))[:2000],
                                    "details": V.mask(details or "")[:12000], "ms": int((time.time() - t0) * 1000)})
                except Exception as e:
                    status = "failed"
                    entries.append({"step": i, "action": label, "ok": False, "message": V.mask(str(e))[:2000],
                                    "details": V.mask(getattr(e, "details", "") or "")[:12000],
                                    "ms": int((time.time() - t0) * 1000)})
                    if j["options"].get("stop_on_error", True):
                        skipped = len(j["actions"]) - i
                        if skipped:
                            entries.append({"step": None, "action": "", "ok": False,
                                            "message": f"Stopped: the remaining {skipped} action{'s were' if skipped > 1 else ' was'} skipped."})
                        break
                ctx["steps"][str(i)] = {"output": (ctx.get("last") or {}).get("output", "")}
        finally:
            self.running.discard(job_id)
            done = time.time()
            db.execute("UPDATE job_runs SET finished_at=?, status=?, log=? WHERE id=?",
                       (done, status, json.dumps(entries), run_id))
            db.execute("UPDATE jobs SET last_run=?, last_status=? WHERE id=?", (done, status, job_id))
            db.execute("DELETE FROM job_runs WHERE job_id=? AND id NOT IN "
                       "(SELECT id FROM job_runs WHERE job_id=? ORDER BY id DESC LIMIT 100)", (job_id, job_id))
            db.audit("job_run", user, "local", {"job": j["name"], "trigger": V.mask(why)[:200], "status": status})
            if ctx.get("_update_followup"):
                # The install restarts the app: remember to add its outcome to this run afterwards.
                db.set_setting(FOLLOWUP_KEY, json.dumps({**ctx["_update_followup"], "run_id": run_id,
                                                         "job_id": job_id, "started": done}))
        return {"id": run_id, "status": status, "log": entries}

    # -- outcome of an update installed by a job (checked after the app restarts)
    async def finish_update_followup(self) -> None:
        raw = db.get_setting(FOLLOWUP_KEY)
        if not raw:
            return
        from . import updater
        f = json.loads(raw)
        deadline = f.get("started", time.time()) + 45 * 60
        entry = None
        while time.time() < deadline:
            cur = updater.current_version()
            prog = updater.progress(log_lines=200)
            if cur == f["to"] and prog.get("state") != "running":
                entry = {"ok": True, "message": f"Update installed: the server now runs SysAdmin.IA {cur} (was {f['from']})."}
                break
            if prog.get("state") == "failed":
                entry = {"ok": False, "message": f"The update to {f['to']} failed and was rolled back: {prog.get('message') or 'see Admin → Updates'}",
                         "details": (prog.get("log") or "")[-6000:]}
                break
            if f.get("mode") == "code" and cur != f["to"] and time.time() - f.get("started", 0) > 300:
                entry = {"ok": False, "message": f"The update to {f['to']} didn't take effect (still running {cur})."}
                break
            await asyncio.sleep(5)
        if entry is None:
            entry = {"ok": False, "message": f"Couldn't confirm the update to {f['to']} within 45 minutes; check Admin → Updates."}
        db.set_setting(FOLLOWUP_KEY, None)
        row = db.query_one("SELECT log, status FROM job_runs WHERE id=?", (f["run_id"],))
        if not row:
            return
        log_entries = json.loads(row["log"] or "[]")
        log_entries.append({"step": None, "action": "Update result", **entry})
        status = row["status"] if entry["ok"] else "failed"
        db.execute("UPDATE job_runs SET log=?, status=? WHERE id=?", (json.dumps(log_entries), status, f["run_id"]))
        if not entry["ok"]:
            db.execute("UPDATE jobs SET last_status='failed' WHERE id=?", (f["job_id"],))


ENGINE = JobEngine()
