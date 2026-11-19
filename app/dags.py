"""Jobs as DAGs, the Apache Airflow way.

A DAG (directed acyclic graph) has
  * triggers  – when it runs: a schedule or an event (the trigger catalog of app/jobs.py: full disk, new device,
                new mail, …). No trigger = it only runs when started by hand.
  * tasks     – what it does: each task is an operator (send an email, run a command, ask the AI, … = the action
                catalog of app/jobs.py, plus the flow operators below) with its settings, the tasks it depends on
                (upstream), retries, a retry delay, a trigger rule and an optional time limit.

A DAG run creates a task instance per task. A task runs when its upstream tasks are finished and its trigger rule
allows it (default "all_success"); tasks that are ready at the same time run in parallel. Text fields can use
{{tasks.<task_id>.output}} (output of any earlier task, like Airflow's XCom), {{last.output}} (output of the
upstream task that finished last), {{trigger.*}}, {{var.*}}, {{sys.*}}, {{mon.*}}.

Code view: a DAG can be shown and edited as Airflow-like Python:

    from sysadmin_ia import DAG, triggers, tasks

    with DAG(dag_id="disk_alert", description="…", triggers=[triggers.disk_usage(threshold=90)]) as dag:
        explain = tasks.ask_ai(task_id="explain", prompt="…")
        mail = tasks.send_mail(task_id="mail", account=1, to="me@example.com", subject="…", body="…")
        explain >> mail

The code is read with Python's parser (ast) and never executed: only literal values, tasks.<operator>(…) calls
and >> / << dependencies are accepted.

The scheduler (DagScheduler) runs in the jobs process (app/worker.py); the web process only writes requests
("queued" runs, stop requests) to the database.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Any

from . import db
from . import jobs as J
from . import variables as V

log = logging.getLogger("sysadmin-ia.dags")

DAG_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TASK_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
TERMINAL = {"success", "failed", "skipped", "upstream_failed"}
FAILED = {"failed", "upstream_failed"}
MAX_PARALLEL = 4
KEEP_RUNS = 100

TRIGGER_RULES = {
    "all_success": "All upstream tasks succeeded (default)",
    "all_done": "All upstream tasks finished, whatever the result",
    "none_failed": "No upstream task failed (skipped ones are fine)",
    "one_success": "At least one upstream task succeeded",
    "one_failed": "At least one upstream task failed (e.g. to send a warning)",
    "all_failed": "All upstream tasks failed",
}

# Flow operators (in addition to the actions of app/jobs.py)
FLOW_OPERATORS: dict[str, dict] = {
    "short_circuit": {
        "label": "Condition", "icon": "bolt", "group": "Flow",
        "description": "Continue only if a condition is true; otherwise every task after it is skipped "
                       "(like Airflow's ShortCircuitOperator). E.g. only send the warning if the command printed something.",
        "fields": [
            {"key": "value", "label": "Value", "type": "text", "required": True, "default": "{{last.output}}"},
            {"key": "op", "label": "Is", "type": "select", "default": "not_empty",
             "options": [["not_empty", "not empty"], ["empty", "empty"], ["equals", "equal to"], ["not_equals", "not equal to"],
                         ["contains", "containing"], ["not_contains", "not containing"], ["gt", "a number greater than"],
                         ["lt", "a number less than"]]},
            {"key": "compare", "label": "Compare with", "type": "text", "default": "",
             "show_if": {"op": ["equals", "not_equals", "contains", "not_contains", "gt", "lt"]}},
        ],
        "provides": ["last.output"],
    },
    "wait": {
        "label": "Wait", "icon": "clock", "group": "Flow",
        "description": "Pause before the next tasks, e.g. give a restarted service time to come up.",
        "fields": [{"key": "seconds", "label": "Seconds", "type": "number", "min": 1, "max": 86400, "default": 30}],
        "provides": [],
    },
    "empty": {
        "label": "Join", "icon": "check", "group": "Flow",
        "description": "Does nothing: a meeting point to join or split branches (like Airflow's EmptyOperator).",
        "fields": [],
        "provides": [],
    },
    "start": {
        "label": "Start", "icon": "play", "group": "Flow",
        "description": "Does nothing: marks where the DAG begins (like an EmptyOperator called “start” in Airflow). "
                       "Connect the first tasks after it.",
        "fields": [],
        "provides": [],
    },
    "end": {
        "label": "End", "icon": "flag", "group": "Flow",
        "description": "Does nothing: marks where the DAG ends. Connect the last tasks to it; with the trigger rule "
                       "“all_done” it runs even when something before it failed.",
        "fields": [],
        "provides": [],
    },
}

_CODE_HELP = ("SAI_CONTEXT (an environment variable) holds the run as JSON: trigger values, dag, run, "
              "and every earlier task's output (tasks.<task_id>.output).")
CODE_OPERATORS: dict[str, dict] = {
    "run_python": {
        "label": "Run Python", "icon": "code", "group": "Code",
        "description": "Run Python code with the server's Python or one of your Python environments (Automation → Python "
                       "environments). What it prints is its output for later tasks; an error or a non-zero exit fails the task.",
        "fields": [
            {"key": "venv", "label": "Python", "type": "pyenv", "default": "",
             "help": "Base Python is the server's python3. Environments can have other Python versions and packages."},
            {"key": "code", "label": "Code", "type": "textarea", "mono": True, "required": True,
             "default": "import json, os\n\nctx = json.loads(os.environ[\"SAI_CONTEXT\"])   # trigger values, earlier tasks' outputs\n"
                        "print(f\"Hello from {ctx['dag']['id']}\")\n",
             "help": _CODE_HELP + " Placeholders like {{var.x}} are filled in as text before it runs."},
            {"key": "args", "label": "Arguments", "type": "text", "default": "", "placeholder": "optional, e.g. --dry-run {{trigger.mount}}",
             "help": "Given to the code as sys.argv[1:]."},
            {"key": "workdir", "label": "Working folder", "type": "text", "default": "", "placeholder": "empty = the app's data folder"},
            {"key": "timeout", "label": "Time limit (seconds)", "type": "number", "min": 1, "max": 86400, "default": 300},
        ],
        "provides": ["last.output", "last.exit_code"],
    },
    "run_shell": {
        "label": "Run console", "icon": "console", "group": "Code",
        "description": "Run a script of console (shell) commands, several lines like in the Console module, as the app's user. "
                       "What it prints is its output for later tasks.",
        "fields": [
            {"key": "script", "label": "Script", "type": "textarea", "mono": True, "required": True,
             "default": "echo \"Disk usage:\"\ndf -h /\n", "help": _CODE_HELP},
            {"key": "shell", "label": "Shell", "type": "select", "default": "bash", "options": [["bash", "bash"], ["sh", "sh"]]},
            {"key": "stop_on_error", "label": "Stop at the first command that fails", "type": "bool", "default": True,
             "help": "set -e (and pipefail in bash): the task fails as soon as a command fails."},
            {"key": "workdir", "label": "Working folder", "type": "text", "default": "", "placeholder": "empty = the app's data folder"},
            {"key": "env", "label": "Environment variables", "type": "textarea", "mono": True, "default": "",
             "placeholder": "NAME=value, one per line (placeholders allowed)"},
            {"key": "timeout", "label": "Time limit (seconds)", "type": "number", "min": 1, "max": 86400, "default": 300},
        ],
        "provides": ["last.output", "last.exit_code"],
    },
}


def operators() -> dict[str, dict]:
    return {**J.ACTIONS, **CODE_OPERATORS, **FLOW_OPERATORS}


class DagError(J.JobError):
    pass


# ---------------------------------------------------------------- validation
def _clean_task(t: dict, i: int, ops: dict) -> dict:
    tid = str((t or {}).get("task_id") or "").strip()
    if not TASK_ID_RE.fullmatch(tid):
        raise DagError(f"Task {i}: the task id must start with a letter and use letters, digits and _ (max 64), e.g. send_warning.")
    op = (t or {}).get("operator")
    if op not in ops:
        raise DagError(f"Task “{tid}”: unknown operator “{op}”.")
    params = dict(t.get("params") or {})
    try:
        clean = J._clean_items([{"type": op, **params}], ops, "task")[0]
    except J.JobError as e:
        raise DagError(re.sub(r"^Task 1 \(", f"Task “{tid}” (", str(e)))
    clean.pop("type", None)
    up = t.get("upstream") or []
    if not isinstance(up, list) or not all(isinstance(u, str) for u in up):
        raise DagError(f"Task “{tid}”: invalid upstream list.")
    try:
        retries = int(t.get("retries") or 0)
        delay = int(t.get("retry_delay") if t.get("retry_delay") is not None else 60)
        timeout = int(t["execution_timeout"]) if t.get("execution_timeout") not in (None, "", 0) else None
    except (TypeError, ValueError):
        raise DagError(f"Task “{tid}”: retries, retry delay and time limit are numbers.")
    if not 0 <= retries <= 10:
        raise DagError(f"Task “{tid}”: retries must be between 0 and 10.")
    if not 0 <= delay <= 3600:
        raise DagError(f"Task “{tid}”: the retry delay must be between 0 and 3600 seconds.")
    if timeout is not None and not 1 <= timeout <= 86400:
        raise DagError(f"Task “{tid}”: the time limit must be between 1 and 86400 seconds.")
    rule = t.get("trigger_rule") or "all_success"
    if rule not in TRIGGER_RULES:
        raise DagError(f"Task “{tid}”: unknown trigger rule “{rule}”.")
    return {"task_id": tid, "operator": op, "params": clean, "upstream": sorted(set(up)), "retries": retries,
            "retry_delay": delay, "trigger_rule": rule, "execution_timeout": timeout}


def _toposort(tasks: list[dict]) -> list[str]:
    ids = {t["task_id"] for t in tasks}
    indeg = {t["task_id"]: len(t["upstream"]) for t in tasks}
    down: dict[str, list[str]] = {i: [] for i in ids}
    for t in tasks:
        for u in t["upstream"]:
            down[u].append(t["task_id"])
    order, ready = [], sorted(i for i, d in indeg.items() if d == 0)
    while ready:
        n = ready.pop(0)
        order.append(n)
        for m in sorted(down[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
    if len(order) != len(ids):
        cyc = sorted(i for i in ids if i not in order)
        raise DagError(f"The tasks depend on each other in a circle ({', '.join(cyc)}): a DAG can't have cycles.")
    return order


def clean_dag(data: dict) -> dict:
    dag_id = str(data.get("dag_id") or "").strip()
    if not DAG_ID_RE.fullmatch(dag_id):
        raise DagError("The DAG id must start with a lowercase letter and use lowercase letters, digits and _ (max 64), "
                       "e.g. disk_alert.")
    try:
        triggers = J._clean_items(data.get("triggers") or [], J.TRIGGERS, "trigger")
    except J.JobError as e:
        raise DagError(str(e))
    raw = data.get("tasks") or []
    if not isinstance(raw, list) or not raw:
        raise DagError("Add at least one task (drag one from the list onto the canvas).")
    if len(raw) > 100:
        raise DagError("A DAG can have at most 100 tasks.")
    ops = operators()
    tasks = [_clean_task(t, i, ops) for i, t in enumerate(raw, 1)]
    ids = [t["task_id"] for t in tasks]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise DagError(f"Task ids must be unique: {', '.join(dup)}.")
    for t in tasks:
        missing = [u for u in t["upstream"] if u not in ids]
        if missing:
            raise DagError(f"Task “{t['task_id']}” depends on a task that doesn't exist: {', '.join(missing)}.")
        if t["task_id"] in t["upstream"]:
            raise DagError(f"Task “{t['task_id']}” can't depend on itself.")
    order = _toposort(tasks)
    tasks.sort(key=lambda t: order.index(t["task_id"]))
    layout = {k: [round(float(v[0])), round(float(v[1]))] for k, v in (data.get("layout") or {}).items()
              if k in ids and isinstance(v, (list, tuple)) and len(v) == 2}
    layout = {**auto_layout(tasks, layout), **layout}
    try:
        mar = int((data.get("options") or {}).get("max_active_runs") or data.get("max_active_runs") or 1)
    except (TypeError, ValueError):
        mar = 1
    return {"dag_id": dag_id, "description": " ".join(str(data.get("description") or "").split())[:500],
            "paused": bool(data.get("paused", False)), "triggers": triggers, "tasks": tasks, "layout": layout,
            "options": {"max_active_runs": max(1, min(mar, 5))}}


def normalize(data: dict) -> dict:
    """A DAG that may not be valid yet (editor: switching between Graph and Code while a task is half set up):
    same shape as clean_dag's result, with defaults, but no checks beyond the shape."""
    ops = operators()
    tasks = []
    for t in data.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        tasks.append({"task_id": str(t.get("task_id") or ""), "operator": t.get("operator") if t.get("operator") in ops else "empty",
                      "params": dict(t.get("params") or {}), "upstream": [u for u in (t.get("upstream") or []) if isinstance(u, str)],
                      "retries": t.get("retries") or 0, "retry_delay": t.get("retry_delay", 60),
                      "trigger_rule": t.get("trigger_rule") or "all_success", "execution_timeout": t.get("execution_timeout")})
    ids = {t["task_id"] for t in tasks}
    for t in tasks:
        t["upstream"] = [u for u in t["upstream"] if u in ids and u != t["task_id"]]
    layout = {k: v for k, v in (data.get("layout") or {}).items() if k in ids}
    try:
        layout = {**auto_layout(tasks, layout), **layout}
    except RecursionError:
        pass
    return {"dag_id": str(data.get("dag_id") or ""), "description": str(data.get("description") or ""),
            "paused": bool(data.get("paused", False)),
            "triggers": [t for t in (data.get("triggers") or []) if isinstance(t, dict) and t.get("type") in J.TRIGGERS],
            "tasks": tasks, "layout": layout, "options": {"max_active_runs": (data.get("options") or {}).get("max_active_runs", 1)}}


def auto_layout(tasks: list[dict], keep: dict | None = None) -> dict:
    """Positions for tasks that have none: columns by depth (longest path from a start task)."""
    keep = keep or {}
    depth: dict[str, int] = {}
    by_id = {t["task_id"]: t for t in tasks}

    def d(tid: str, seen=()) -> int:
        if tid in depth:
            return depth[tid]
        ups = [u for u in by_id[tid]["upstream"] if u in by_id and u not in seen]
        depth[tid] = 0 if not ups else 1 + max(d(u, seen + (tid,)) for u in ups)
        return depth[tid]

    rows: dict[int, int] = {}
    out = {}
    for t in tasks:
        col = d(t["task_id"])
        row = rows.get(col, 0)
        rows[col] = row + 1
        if t["task_id"] not in keep:
            out[t["task_id"]] = [40 + col * 260, 40 + row * 120]
    return out


# ---------------------------------------------------------------- code view (Airflow-like, parsed, never executed)
def _py_str(s: str) -> str:
    if "\n" in s:
        body = s.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        if body.endswith('"'):
            body = body[:-1] + '\\"'
        return f'"""{body}"""'
    return json.dumps(s, ensure_ascii=False)


def _py(v: Any) -> str:
    if isinstance(v, bool) or v is None:
        return repr(v)
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _py_str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_py(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_py(k)}: {_py(x)}" for k, x in v.items()) + "}"
    return _py(str(v))


def _call(fn: str, kwargs: list[tuple[str, Any]], indent: str) -> str:
    one = f"{fn}(" + ", ".join(f"{k}={_py(v)}" for k, v in kwargs) + ")"
    if len(indent) + len(one) <= 110 and "\n" not in one:
        return one
    inner = indent + "    "
    return f"{fn}(\n" + "".join(f"{inner}{k}={_py(v)},\n" for k, v in kwargs) + f"{indent})"


def _var(tid: str, used: set) -> str:
    v = re.sub(r"\W", "_", tid)
    if not v.isidentifier() or v in ("dag", "DAG", "tasks", "triggers") or __import__("keyword").iskeyword(v):
        v = f"t_{v}"
    base, n = v, 2
    while v in used:
        v, n = f"{base}_{n}", n + 1
    used.add(v)
    return v


def to_code(d: dict) -> str:
    """The DAG as Airflow-like Python (what the Code view shows and edits)."""
    ops = operators()
    lines = ["from sysadmin_ia import DAG, triggers, tasks", ""]
    dag_kw: list[tuple[str, Any]] = [("dag_id", d["dag_id"])]
    if d.get("description"):
        dag_kw.append(("description", d["description"]))
    if d.get("paused"):
        dag_kw.append(("paused", True))
    if (d.get("options") or {}).get("max_active_runs", 1) != 1:
        dag_kw.append(("max_active_runs", d["options"]["max_active_runs"]))
    head = "with DAG(\n" + "".join(f"    {k}={_py(v)},\n" for k, v in dag_kw)
    trig = []
    for t in d.get("triggers") or []:
        fields = [f["key"] for f in J.TRIGGERS[t["type"]]["fields"]]
        trig.append("        " + _call(f"triggers.{t['type']}", [(k, t[k]) for k in fields if k in t], "        ") + ",")
    head += "    triggers=[\n" + "\n".join(trig) + "\n    ],\n" if trig else "    triggers=[],\n"
    lines.append(head + ") as dag:")
    used: set = set()
    var = {t["task_id"]: _var(t["task_id"], used) for t in d["tasks"]}
    for t in d["tasks"]:
        spec = ops.get(t["operator"], {"fields": []})
        kw: list[tuple[str, Any]] = [("task_id", t["task_id"])]
        kw += [(f["key"], t["params"][f["key"]]) for f in spec["fields"] if f["key"] in t["params"]]
        if t.get("retries"):
            kw.append(("retries", t["retries"]))
            kw.append(("retry_delay", t.get("retry_delay", 60)))
        if t.get("trigger_rule", "all_success") != "all_success":
            kw.append(("trigger_rule", t["trigger_rule"]))
        if t.get("execution_timeout"):
            kw.append(("execution_timeout", t["execution_timeout"]))
        lines.append(f"    {var[t['task_id']]} = " + _call(f"tasks.{t['operator']}", kw, "    "))
    edges = []
    for t in d["tasks"]:
        downs = [x["task_id"] for x in d["tasks"] if t["task_id"] in x["upstream"]]
        if downs:
            right = var[downs[0]] if len(downs) == 1 else "[" + ", ".join(var[x] for x in downs) + "]"
            edges.append(f"    {var[t['task_id']]} >> {right}")
    if edges:
        lines += [""] + edges
    if len(lines) == 3:
        lines.append("    pass")
    return "\n".join(lines) + "\n"


def _err(node: ast.AST | None, msg: str) -> DagError:
    return DagError(f"Line {getattr(node, 'lineno', '?')}: {msg}")


def _literal(node: ast.AST, what: str):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        raise _err(node, f"{what} must be a plain value (text in quotes, a number, True/False, a list…), "
                         "not an expression or a variable.")


def _ns_call(node: ast.AST, ns: str, what: str) -> tuple[str, dict]:
    """triggers.disk_usage(threshold=90) → ("disk_usage", {...})."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
            and node.func.value.id == ns):
        raise _err(node, f"expected {ns}.<name>(…) for a {what}.")
    if node.args:
        raise _err(node, f"use names for the settings of a {what}, e.g. {ns}.{node.func.attr}(key=value).")
    kw = {}
    for k in node.keywords:
        if k.arg is None:
            raise _err(node, "**settings aren't supported; write each setting out.")
        kw[k.arg] = _literal(k.value, f"“{k.arg}”")
    return node.func.attr, kw


def from_code(code: str) -> dict:
    """Read Airflow-like DAG code into a DAG (not yet validated: pass the result to clean_dag)."""
    if len(code or "") > 200_000:
        raise DagError("The code is too long.")
    try:
        tree = ast.parse(code or "")
    except SyntaxError as e:
        raise DagError(f"Line {e.lineno}: {e.msg}")
    withs = []
    for st in tree.body:
        if isinstance(st, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
            continue          # docstring / comment string
        if isinstance(st, ast.With):
            withs.append(st)
            continue
        raise _err(st, "only imports and one `with DAG(…) as dag:` block are allowed at the top level.")
    if len(withs) != 1:
        raise DagError("The code must contain exactly one `with DAG(…) as dag:` block.")
    w = withs[0]
    call = w.items[0].context_expr
    if not (isinstance(call, ast.Call) and ((isinstance(call.func, ast.Name) and call.func.id == "DAG")
                                             or (isinstance(call.func, ast.Attribute) and call.func.attr == "DAG"))):
        raise _err(w, "the block must be `with DAG(…) as dag:`.")
    if call.args:
        raise _err(call, "give DAG settings by name, e.g. DAG(dag_id=\"my_dag\").")
    out: dict[str, Any] = {"triggers": [], "tasks": [], "options": {}}
    for k in call.keywords:
        if k.arg == "triggers":
            if not isinstance(k.value, (ast.List, ast.Tuple)):
                raise _err(k.value, "triggers must be a list, e.g. [triggers.schedule(mode=\"daily\", time=\"08:00\")].")
            for el in k.value.elts:
                ty, kw = _ns_call(el, "triggers", "trigger")
                if ty not in J.TRIGGERS:
                    raise _err(el, f"unknown trigger “{ty}”. Known: {', '.join(J.TRIGGERS)}.")
                out["triggers"].append({"type": ty, **kw})
        elif k.arg in ("dag_id", "description", "paused"):
            out[k.arg] = _literal(k.value, k.arg)
        elif k.arg == "max_active_runs":
            out["options"]["max_active_runs"] = _literal(k.value, k.arg)
        elif k.arg in ("schedule", "schedule_interval", "start_date", "catchup", "tags", "default_args"):
            raise _err(k, f"“{k.arg}” isn't supported here: use triggers=[triggers.schedule(…)] for timing.")
        else:
            raise _err(k, f"unknown DAG setting “{k.arg}”.")
    ops = operators()
    var_task: dict[str, dict] = {}
    edges: list[tuple[str, str]] = []

    def side(node: ast.AST) -> list[str]:
        if isinstance(node, ast.Name):
            if node.id not in var_task:
                raise _err(node, f"“{node.id}” isn't a task defined above.")
            return [node.id]
        if isinstance(node, (ast.List, ast.Tuple)):
            names = []
            for e in node.elts:
                names += side(e)
            return names
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
            left, right = side(node.left), side(node.right)
            pairs = [(a, b) for a in left for b in right] if isinstance(node.op, ast.RShift) else \
                    [(b, a) for a in left for b in right]
            edges.extend(pairs)
            return right
        raise _err(node, "dependencies are written like a >> b, a >> [b, c] or [a, b] >> c.")

    for st in w.body:
        if isinstance(st, ast.Pass) or (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)):
            continue
        if isinstance(st, ast.Assign):
            if len(st.targets) != 1 or not isinstance(st.targets[0], ast.Name):
                raise _err(st, "assign each task to one name, e.g. mail = tasks.send_mail(…).")
            op, kw = _ns_call(st.value, "tasks", "task")
            if op not in ops:
                raise _err(st.value, f"unknown operator “{op}”. Known: {', '.join(ops)}.")
            name = st.targets[0].id
            if name in var_task:
                raise _err(st, f"“{name}” is defined twice.")
            tid = kw.pop("task_id", None) or name
            meta = {k: kw.pop(k) for k in ("retries", "retry_delay", "trigger_rule", "execution_timeout") if k in kw}
            known = {f["key"] for f in ops[op]["fields"]}
            extra = sorted(set(kw) - known)
            if extra:
                raise _err(st.value, f"tasks.{op} has no setting {', '.join(extra)}. It has: "
                                     f"{', '.join(sorted(known)) or 'none'} (and retries, retry_delay, trigger_rule, execution_timeout).")
            var_task[name] = {"task_id": str(tid), "operator": op, "params": kw, "upstream": [], **meta, "_line": st.lineno}
            continue
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.BinOp):
            side(st.value)
            continue
        if (isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) and isinstance(st.value.func, ast.Attribute)
                and st.value.func.attr in ("set_downstream", "set_upstream")):
            src = side(st.value.func.value)
            tgt = []
            for a in st.value.args:
                tgt += side(a)
            edges.extend([(a, b) for a in src for b in tgt] if st.value.func.attr == "set_downstream"
                         else [(b, a) for a in src for b in tgt])
            continue
        raise _err(st, "inside the DAG, only tasks (name = tasks.<operator>(…)) and dependencies (a >> b) are allowed.")
    for a, b in edges:
        up = var_task[b]["upstream"]
        if var_task[a]["task_id"] not in up:
            up.append(var_task[a]["task_id"])
    for t in var_task.values():
        t.pop("_line", None)
    out["tasks"] = list(var_task.values())
    return out


# ---------------------------------------------------------------- storage
def _row(r) -> dict:
    d = dict(r)
    for k, empty in (("triggers", "[]"), ("tasks", "[]"), ("layout", "{}"), ("options", "{}"), ("state", "{}")):
        d[k] = json.loads(d[k] or empty)
    d["paused"] = bool(d["paused"])
    return d


def list_all() -> list[dict]:
    return [_row(r) for r in db.query("SELECT * FROM dags ORDER BY dag_id")]


def get(pk: int) -> dict | None:
    r = db.query_one("SELECT * FROM dags WHERE id=?", (pk,))
    return _row(r) if r else None


def _check_unique(dag_id: str, pk: int | None) -> None:
    r = db.query_one("SELECT id FROM dags WHERE dag_id=?", (dag_id,))
    if r and r["id"] != pk:
        raise DagError(f"There's already a DAG called {dag_id}.")


def create(data: dict) -> dict:
    d = clean_dag(data)
    _check_unique(d["dag_id"], None)
    now = time.time()
    pk = db.execute("INSERT INTO dags (dag_id, description, paused, triggers, tasks, layout, options, state, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?, '{}', ?, ?)",
                    (d["dag_id"], d["description"], int(d["paused"]), json.dumps(d["triggers"]), json.dumps(d["tasks"]),
                     json.dumps(d["layout"]), json.dumps(d["options"]), now, now))
    return get(pk)


def update(pk: int, data: dict) -> dict:
    old = get(pk)
    if not old:
        raise DagError("That DAG no longer exists.")
    d = clean_dag(data)
    _check_unique(d["dag_id"], pk)
    # keep the state of triggers that didn't change (their schedule, alert status…)
    sigs = {J._sig(t) for t in d["triggers"]}
    state = {k: v for k, v in old["state"].items() if isinstance(v, dict) and v.get("sig") in sigs}
    db.execute("UPDATE dags SET dag_id=?, description=?, paused=?, triggers=?, tasks=?, layout=?, options=?, state=?, updated_at=? "
               "WHERE id=?", (d["dag_id"], d["description"], int(d["paused"]), json.dumps(d["triggers"]), json.dumps(d["tasks"]),
                              json.dumps(d["layout"]), json.dumps(d["options"]), json.dumps(_rekey_state(old, d, state)),
                              time.time(), pk))
    return get(pk)


def _rekey_state(old: dict, new: dict, kept: dict) -> dict:
    """Trigger state is keyed by position: move kept states to the new positions of their triggers."""
    by_sig = {v.get("sig"): v for v in kept.values()}
    return {str(i): by_sig[J._sig(t)] for i, t in enumerate(new["triggers"]) if J._sig(t) in by_sig}


def set_paused(pk: int, paused: bool) -> None:
    db.execute("UPDATE dags SET paused=?, updated_at=? WHERE id=?", (int(paused), time.time(), pk))


def delete(pk: int) -> None:
    runs = [r["id"] for r in db.query("SELECT id FROM dag_runs WHERE dag_pk=?", (pk,))]
    for rid in runs:
        db.execute("DELETE FROM task_instances WHERE run_pk=?", (rid,))
    db.execute("DELETE FROM dag_runs WHERE dag_pk=?", (pk,))
    db.execute("DELETE FROM dags WHERE id=?", (pk,))


def public(d: dict, with_runs: int = 12) -> dict:
    out = {k: d[k] for k in ("id", "dag_id", "description", "paused", "triggers", "tasks", "layout", "options",
                             "created_at", "updated_at")}
    out["trigger_summaries"] = [J.describe_trigger(t) for t in d["triggers"]]
    nexts = [s.get("next") for s in d["state"].values() if isinstance(s, dict) and s.get("next")]
    out["next_run"] = min(nexts) if nexts and not d["paused"] else None
    out["alerting"] = any(isinstance(s, dict) and s.get("alert") for s in d["state"].values())
    rows = db.query("SELECT id, run_id, run_type, state, started_at, finished_at, queued_at, trigger FROM dag_runs "
                    "WHERE dag_pk=? ORDER BY id DESC LIMIT ?", (d["id"], with_runs))
    out["recent_runs"] = [dict(r) for r in rows]
    out["active"] = sum(1 for r in rows if r["state"] in ("queued", "running"))
    return out


def runs(pk: int, limit: int = 25) -> list[dict]:
    rows = db.query("SELECT * FROM dag_runs WHERE dag_pk=? ORDER BY id DESC LIMIT ?", (pk, limit))
    out = []
    for r in rows:
        d = dict(r)
        d["conf"] = json.loads(d["conf"] or "{}")
        d["tasks"] = [dict(t) for t in db.query("SELECT * FROM task_instances WHERE run_pk=? ORDER BY id", (r["id"],))]
        out.append(d)
    return out


def trigger(pk: int, user: str, conf: dict | None = None, why: str = "", run_type: str = "manual") -> dict:
    """Queue a run; the jobs process starts it within seconds."""
    d = get(pk)
    if not d:
        raise DagError("That DAG no longer exists.")
    now = time.time()
    stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S")
    conf = conf or {"summary": f"Manual run of {d['dag_id']}", "state": "test", "value": "", "threshold": "",
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    rid = db.execute("INSERT INTO dag_runs (dag_pk, run_id, run_type, state, trigger, conf, requested_by, queued_at) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (pk, f"{run_type}__{stamp}", run_type, "queued", V.mask(why or f"Started by {user}")[:300],
                      json.dumps(conf), user, now))
    return {"id": rid, "run_id": f"{run_type}__{stamp}", "state": "queued"}


def stop(run_pk: int) -> None:
    r = db.query_one("SELECT state FROM dag_runs WHERE id=?", (run_pk,))
    if not r:
        raise DagError("That run no longer exists.")
    if r["state"] == "queued":
        db.execute("UPDATE dag_runs SET state='failed', finished_at=?, note='Cancelled before it started.' WHERE id=?",
                   (time.time(), run_pk))
    elif r["state"] == "running":
        db.execute("UPDATE dag_runs SET stop_requested=1 WHERE id=?", (run_pk,))


# ---------------------------------------------------------------- migration of the old "when → then" jobs
def _slug(name: str, taken: set) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "job"
    if not s[0].isalpha():
        s = "job_" + s
    s = s[:60]
    base, n = s, 2
    while s in taken:
        s, n = f"{base}_{n}", n + 1
    taken.add(s)
    return s


def migrate_jobs() -> int:
    """Turn each old job into a DAG once: its triggers stay, its actions become a chain of tasks."""
    if db.get_setting("jobs_migrated_to_dags"):
        return 0
    # several processes start at once (web, jobs): only the one that claims the migration does it
    with db._lock:
        cur = db.conn().execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('jobs_migrated_to_dags', 'running')")
        if cur.rowcount != 1:
            return 0
    taken = {d["dag_id"] for d in list_all()}
    n = 0
    for j in J.list_all():
        ids = []
        tasks = []
        for i, a in enumerate(j["actions"], 1):
            tid = f"{re.sub(r'[^a-z0-9]+', '_', a['type'])}_{i}"
            ids.append(tid)
            params = {k: v for k, v in a.items() if k != "type"}
            tasks.append({"task_id": tid, "operator": a["type"], "params": params,
                          "upstream": [ids[i - 2]] if i > 1 else [],
                          "trigger_rule": "all_success" if j["options"].get("stop_on_error", True) else "all_done"})
        for t in tasks:   # {{steps.N.output}} → {{tasks.<id>.output}}
            for k, v in list(t["params"].items()):
                if isinstance(v, str):
                    t["params"][k] = re.sub(r"\{\{\s*steps\.(\d+)\.output\s*\}\}",
                                            lambda m: f"{{{{tasks.{ids[int(m[1]) - 1]}.output}}}}" if 0 < int(m[1]) <= len(ids)
                                            else m[0], v)
        data = {"dag_id": _slug(j["name"], taken), "description": j["name"] + (f" — {j['description']}" if j["description"] else ""),
                "paused": not j["enabled"], "triggers": j["triggers"], "tasks": tasks}
        try:
            create(data)
            n += 1
        except J.JobError as e:
            log.warning("job %r could not be turned into a DAG: %s", j["name"], e)
    db.set_setting("jobs_migrated_to_dags", str(time.time()))
    return n


# ---------------------------------------------------------------- flow operators
async def _op_short_circuit(p: dict, ctx: dict) -> tuple[str, str]:
    value = V.render(p.get("value") or "", ctx)
    cmp_ = V.render(p.get("compare") or "", ctx)
    op = p.get("op") or "not_empty"

    def num(x: str) -> float:
        try:
            return float(x.strip().replace(",", "."))
        except ValueError:
            raise J.JobError(f"“{x.strip()[:60]}” isn't a number, so it can't be compared.")

    ok = {"not_empty": lambda: bool(value.strip()), "empty": lambda: not value.strip(),
          "equals": lambda: value.strip() == cmp_.strip(), "not_equals": lambda: value.strip() != cmp_.strip(),
          "contains": lambda: cmp_ in value, "not_contains": lambda: cmp_ not in value,
          "gt": lambda: num(value) > num(cmp_), "lt": lambda: num(value) < num(cmp_)}[op]()
    ctx["_short_circuit"] = not ok
    ctx["last"] = {"output": "true" if ok else "false"}
    words = dict(FLOW_OPERATORS["short_circuit"]["fields"][1]["options"])[op]
    shown = value.strip().replace("\n", " ")[:120]
    return (f"True: continuing (“{shown}” is {words}{' ' + cmp_ if op not in ('empty', 'not_empty') else ''})." if ok else
            f"False: the tasks after this one are skipped (“{shown}” isn't {words}{' ' + cmp_ if op not in ('empty', 'not_empty') else ''})."), ""


async def _op_wait(p: dict, ctx: dict) -> tuple[str, str]:
    s = int(p.get("seconds") or 30)
    await asyncio.sleep(s)
    return f"Waited {s} seconds.", ""


async def _op_empty(p: dict, ctx: dict) -> tuple[str, str]:
    return "Done.", ""


# ---------------------------------------------------------------- code operators (Run Python, Run console)
def _context_json(ctx: dict) -> str:
    keep = {k: ctx.get(k) for k in ("trigger", "dag", "run", "tasks", "last", "mail", "update", "ai") if k in ctx}
    return json.dumps(keep, default=str)


def _workdir(p: dict, ctx: dict) -> str:
    from .config import settings
    w = V.render(p.get("workdir") or "", ctx).strip()
    if not w:
        return str(settings.data_dir)
    from pathlib import Path
    if not Path(w).is_absolute():
        raise J.JobError(f"The working folder must be a full path (starting with /), not “{w[:80]}”.")
    if not Path(w).is_dir():
        raise J.JobError(f"The working folder {w} doesn't exist.")
    return w


async def _run_process(argv: list[str], ctx: dict, p: dict, shown: str, env: dict | None = None) -> tuple[str, str]:
    """Run, collect output (≤ 8000 chars kept), fail on a non-zero exit. Output becomes {{last.output}}."""
    import os
    timeout = int(p.get("timeout") or 300)
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
        cwd=_workdir(p, ctx), env={**os.environ, "SAI_CONTEXT": _context_json(ctx), "PYTHONUNBUFFERED": "1", **(env or {})},
        start_new_session=os.name != "nt")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise J.JobError(f"It took longer than {timeout} seconds and was stopped.", details=shown)
    except asyncio.CancelledError:
        proc.kill()
        raise
    text = out.decode("utf-8", "replace").strip()
    if len(text) > 8000:
        text = text[:4000] + "\n…\n" + text[-3900:]
    ctx["last"] = {"output": text, "exit_code": proc.returncode}
    details = f"{shown}\n\n— Output —\n{text or '(no output)'}\n\n[exit code {proc.returncode}]"
    if proc.returncode != 0:
        last_line = (text.splitlines() or [""])[-1][:200]
        raise J.JobError(f"It failed (exit code {proc.returncode}){': ' + last_line if last_line else ''}", details=details)
    n = len(text.splitlines())
    first = (text.splitlines() or [""])[0][:160]
    return (f"Exit code 0 · {n} line{'s' if n != 1 else ''} of output" + (f": {first}" if first else "")
            if text else "Exit code 0 (no output)."), details


async def _op_run_python(p: dict, ctx: dict) -> tuple[str, str]:
    import os
    import shlex
    import tempfile
    from . import pyenvs
    from .config import settings
    try:
        py = pyenvs.env_python(p["venv"]) if p.get("venv") else pyenvs.base_python()
    except pyenvs.PyEnvError as e:
        raise J.JobError(str(e))
    code = V.render(p.get("code") or "", ctx)
    try:
        args = shlex.split(V.render(p.get("args") or "", ctx))
    except ValueError as e:
        raise J.JobError(f"The arguments can't be read: {e}")
    tmp = settings.data_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="dag-", suffix=".py", dir=str(tmp))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(code)
    try:
        where = f"environment “{p['venv']}”" if p.get("venv") else "base Python"
        return await _run_process([py, "-u", path, *args], ctx, p, f"# {where}: {py}\n{code}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def _op_run_shell(p: dict, ctx: dict) -> tuple[str, str]:
    import shutil
    shell = p.get("shell") if p.get("shell") in ("bash", "sh") else "bash"
    exe = shutil.which(shell)
    if not exe:
        raise J.JobError(f"{shell} isn't installed on this server.")
    script = V.render(p.get("script") or "", ctx)
    if p.get("stop_on_error", True):
        script = ("set -eo pipefail\n" if shell == "bash" else "set -e\n") + script
    env = {}
    for i, line in enumerate((p.get("env") or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, sep, v = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k.strip()):
            raise J.JobError(f"Environment variables, line {i}: write NAME=value.")
        env[k.strip()] = V.render(v, ctx)
    shown = f"# {shell}" + (f" (+ {', '.join(env)})" if env else "") + f"\n{script}"
    return await _run_process([exe, "-c", script], ctx, p, shown, env)


OP_FUNCS = {**J.ACTION_FUNCS, "short_circuit": _op_short_circuit, "wait": _op_wait, "empty": _op_empty,
            "start": _op_empty, "end": _op_empty, "run_python": _op_run_python, "run_shell": _op_run_shell}


# ---------------------------------------------------------------- scheduler / executor (jobs process)
def _rule(rule: str, ups: list[str]) -> str:
    """What to do with a task whose upstream tasks ended in these states: run | skipped | upstream_failed."""
    if not ups:
        return "run"
    failed = [s for s in ups if s in FAILED]
    ok = [s for s in ups if s == "success"]
    if rule == "all_done":
        return "run"
    if rule == "all_success":
        return "run" if len(ok) == len(ups) else "upstream_failed" if failed else "skipped"
    if rule == "none_failed":
        return "upstream_failed" if failed else "run"
    if rule == "one_success":
        return "run" if ok else "skipped"
    if rule == "one_failed":
        return "run" if failed else "skipped"
    if rule == "all_failed":
        return "run" if len(failed) == len(ups) else "skipped"
    return "run"


class DagScheduler(J.JobEngine):
    """Checks DAG triggers, queues runs and executes them (task instances in parallel, retries, trigger rules)."""
    TICK = 20          # trigger checks
    POLL = 2           # queued runs / stop requests

    def __init__(self) -> None:
        super().__init__()
        self.active: dict[int, asyncio.Task] = {}      # run pk → task
        self.last_tick = 0.0

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self.run_forever())

    def info(self) -> dict:
        try:
            queued = db.query_one("SELECT COUNT(*) AS n FROM dag_runs WHERE state='queued'")["n"]
        except Exception:  # noqa: BLE001
            queued = None
        return {"running_runs": len(self.active), "queued_runs": queued, "last_check": self.last_tick}

    async def run_forever(self) -> None:
        migrate_jobs()
        # runs left "running" by a previous jobs process can't continue: close them
        now = time.time()
        for r in db.query("SELECT id FROM dag_runs WHERE state='running'"):
            db.execute("UPDATE task_instances SET state='failed', finished_at=?, message='Interrupted: the jobs process stopped.' "
                       "WHERE run_pk=? AND state IN ('running','up_for_retry')", (now, r["id"]))
            db.execute("UPDATE task_instances SET state='skipped' WHERE run_pk=? AND state='none'", (r["id"],))
            db.execute("UPDATE dag_runs SET state='failed', finished_at=?, note='Interrupted: the jobs process stopped.' "
                       "WHERE id=?", (now, r["id"]))
        await asyncio.sleep(1)
        asyncio.get_running_loop().create_task(self.finish_update_followup())
        for d in list_all():   # "App started" triggers
            if not d["paused"] and any(t["type"] == "app_started" for t in d["triggers"]):
                self.queue_run(d, {"summary": "SysAdmin.IA started", "time": time.strftime("%Y-%m-%d %H:%M:%S")},
                               "App started", "triggered")
        while True:
            try:
                if time.time() - self.last_tick >= self.TICK:
                    self.last_tick = time.time()
                    await self.tick()
                await self.start_queued()
            except Exception:  # never let the scheduler die
                log.exception("DAG scheduler loop failed")
            await asyncio.sleep(self.POLL)

    # -- triggers
    async def tick(self) -> None:
        live = [d for d in list_all() if not d["paused"] and d["triggers"]]
        if not live:
            return
        snap = await self.snapshot({t["type"] for d in live for t in d["triggers"]})
        now = time.time()
        for d in live:
            state, changed = d["state"], False
            for i, t in enumerate(d["triggers"]):
                key = str(i)
                st = state.get(key) if isinstance(state.get(key), dict) else None
                if not st or st.get("sig") != J._sig(t):
                    st = {"sig": J._sig(t)}
                    if t["type"] == "schedule":
                        st["next"] = J.next_run(t, now)
                    state[key] = st
                    changed = True
                fired = await self._check(d, t, st, snap, now)
                if fired is not None or t["type"] in ("schedule", "mail_received", "new_device", "public_ip_changed"):
                    changed = True
                for ctx, why in fired or []:
                    self.queue_run(d, ctx, why, "scheduled" if t["type"] == "schedule" else "triggered")
            if changed:
                db.execute("UPDATE dags SET state=? WHERE id=?", (json.dumps(state), d["id"]))

    def queue_run(self, d: dict, ctx: dict, why: str, run_type: str) -> None:
        busy = db.query_one("SELECT COUNT(*) AS n FROM dag_runs WHERE dag_pk=? AND state IN ('queued','running')", (d["id"],))["n"]
        if busy >= d["options"].get("max_active_runs", 1) + 2:
            log.info("DAG %s has too many runs waiting; skipped trigger %s", d["dag_id"], why)
            return
        trigger(d["id"], "jobs", ctx, why, run_type)

    # -- runs
    async def start_queued(self) -> None:
        for r in db.query("SELECT * FROM dag_runs WHERE state='queued' ORDER BY id"):
            if r["id"] in self.active:
                continue
            d = get(r["dag_pk"])
            if not d:
                db.execute("UPDATE dag_runs SET state='failed', finished_at=?, note='The DAG was deleted.' WHERE id=?",
                           (time.time(), r["id"]))
                continue
            running = db.query_one("SELECT COUNT(*) AS n FROM dag_runs WHERE dag_pk=? AND state='running'", (d["id"],))["n"]
            if running >= d["options"].get("max_active_runs", 1):
                continue
            db.execute("UPDATE dag_runs SET state='running', started_at=? WHERE id=?", (time.time(), r["id"]))
            self.active[r["id"]] = asyncio.get_running_loop().create_task(self.execute(r["id"]))

    async def execute(self, run_pk: int) -> str:
        try:
            return await self._execute(run_pk)
        finally:
            self.active.pop(run_pk, None)

    async def _execute(self, run_pk: int) -> str:
        r = db.query_one("SELECT * FROM dag_runs WHERE id=?", (run_pk,))
        d = get(r["dag_pk"])
        conf = json.loads(r["conf"] or "{}")
        tasks = {t["task_id"]: t for t in d["tasks"]}
        ti_id: dict[str, int] = {}
        for t in d["tasks"]:
            ti_id[t["task_id"]] = db.execute("INSERT INTO task_instances (run_pk, task_id, operator, state) VALUES (?,?,?, 'none')",
                                             (run_pk, t["task_id"], t["operator"]))
        state = {tid: "none" for tid in tasks}
        finished_at: dict[str, float] = {}
        short: set[str] = set()
        ctx: dict[str, Any] = {"trigger": {k: v for k, v in conf.items() if not k.startswith("_")},
                               "dag": {"id": d["dag_id"]}, "run": {"id": r["run_id"]}, "job": {"name": d["dag_id"]},
                               "tasks": {}, "last": {"output": ""}}
        if "_mail" in conf:
            ctx["mail"] = conf["_mail"]
        if "_update" in conf:
            ctx["update"] = conf["_update"]
        running: dict[str, asyncio.Task] = {}
        stopped = False

        def set_state(tid: str, s: str, **cols) -> None:
            state[tid] = s
            sets = ", ".join(["state=?"] + [f"{k}=?" for k in cols])
            db.execute(f"UPDATE task_instances SET {sets} WHERE id=?", (s, *cols.values(), ti_id[tid]))

        while True:
            if not stopped and db.query_one("SELECT stop_requested FROM dag_runs WHERE id=?", (run_pk,))["stop_requested"]:
                stopped = True
                for tid, task in running.items():
                    task.cancel()
                for tid, s in state.items():
                    if s == "none":
                        set_state(tid, "skipped", message="Not run: the run was stopped.")
            for tid, t in tasks.items():
                if state[tid] != "none" or stopped:
                    continue
                ups = [state[u] for u in t["upstream"]]
                if any(s not in TERMINAL for s in ups):
                    continue
                if any(u in short for u in t["upstream"]):
                    set_state(tid, "skipped", message="Skipped: a condition before it was false.", finished_at=time.time())
                    short.add(tid)     # skipping continues down the branch
                    continue
                verdict = _rule(t["trigger_rule"], ups)
                if verdict == "run" and len(running) < MAX_PARALLEL:
                    state[tid] = "running"
                    last = max(t["upstream"], key=lambda u: finished_at.get(u, 0)) if t["upstream"] else None
                    tctx = {**ctx, "last": {"output": (ctx["tasks"].get(last) or {}).get("output", "")} if last else ctx["last"]}
                    running[tid] = asyncio.get_running_loop().create_task(self._run_task(t, tctx, ti_id[tid], run_pk))
                elif verdict != "run":
                    why = ("Skipped: its trigger rule wasn't met." if verdict == "skipped"
                           else "Not run: an upstream task failed.")
                    set_state(tid, verdict, message=why, finished_at=time.time())
            if not running:
                break
            done, _ = await asyncio.wait(list(running.values()), timeout=1, return_when=asyncio.FIRST_COMPLETED)
            for tid in [k for k, v in running.items() if v in done]:
                task = running.pop(tid)
                try:
                    s, tctx = task.result()
                except asyncio.CancelledError:
                    s, tctx = "failed", {}
                    set_state(tid, "failed", message="Stopped by the user.", finished_at=time.time())
                state[tid] = s
                finished_at[tid] = time.time()
                if tctx:
                    out = (tctx.get("last") or {})
                    ctx["tasks"][tid] = {"output": out.get("output", ""), "exit_code": out.get("exit_code", "")}
                    for k in ("ai", "update", "container", "_update_followup"):
                        if k in tctx:
                            ctx[k] = tctx[k]
                    if tctx.get("_short_circuit"):
                        short.add(tid)
        final = "failed" if stopped or any(s in FAILED for s in state.values()) else "success"
        note = "Stopped by the user." if stopped else ""
        db.execute("UPDATE dag_runs SET state=?, finished_at=?, note=? WHERE id=?", (final, time.time(), note, run_pk))
        db.execute("DELETE FROM task_instances WHERE run_pk IN (SELECT id FROM dag_runs WHERE dag_pk=? AND id NOT IN "
                   "(SELECT id FROM dag_runs WHERE dag_pk=? ORDER BY id DESC LIMIT ?))", (d["id"], d["id"], KEEP_RUNS))
        db.execute("DELETE FROM dag_runs WHERE dag_pk=? AND id NOT IN (SELECT id FROM dag_runs WHERE dag_pk=? ORDER BY id DESC "
                   "LIMIT ?)", (d["id"], d["id"], KEEP_RUNS))
        db.audit("dag_run", r["requested_by"] or "jobs", "local", {"dag": d["dag_id"], "run": r["run_id"], "state": final})
        if ctx.get("_update_followup"):
            db.set_setting(J.FOLLOWUP_KEY, json.dumps({**ctx["_update_followup"], "dag_run": run_pk, "started": time.time()}))
        return final

    async def _run_task(self, t: dict, ctx: dict, ti: int, run_pk: int) -> tuple[str, dict]:
        func = OP_FUNCS[t["operator"]]
        attempts = int(t.get("retries") or 0) + 1
        msg, details = "", ""
        for attempt in range(1, attempts + 1):
            t0 = time.time()
            db.execute("UPDATE task_instances SET state='running', try_number=?, started_at=? WHERE id=?", (attempt, t0, ti))
            before = dict(ctx.get("last") or {})
            tctx = {**ctx, "last": before}
            try:
                coro = func(t["params"], tctx)
                res = await (asyncio.wait_for(coro, t["execution_timeout"]) if t.get("execution_timeout") else coro)
                msg, details = res if isinstance(res, tuple) else (str(res), "")
                produced = tctx.get("last") is not before       # operators set ctx["last"] = {...} when they output something
                if not produced:
                    tctx["last"] = {"output": ""}               # a task that produces nothing passes nothing on
                out = str((tctx["last"] or {}).get("output", ""))
                db.execute("UPDATE task_instances SET state='success', finished_at=?, message=?, details=?, output=? WHERE id=?",
                           (time.time(), V.mask(str(msg))[:2000], V.mask(details or "")[:12000], V.mask(out)[:8000], ti))
                return "success", tctx
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                msg, details = f"Took longer than its time limit ({t['execution_timeout']} s) and was stopped.", ""
            except Exception as e:
                msg, details = str(e), getattr(e, "details", "") or ""
            if attempt < attempts:
                db.execute("UPDATE task_instances SET state='up_for_retry', message=?, details=? WHERE id=?",
                           (V.mask(f"Try {attempt} failed: {msg}")[:2000], V.mask(details)[:12000], ti))
                for _ in range(int(t.get("retry_delay") or 0)):
                    await asyncio.sleep(1)
                    if db.query_one("SELECT stop_requested FROM dag_runs WHERE id=?", (run_pk,))["stop_requested"]:
                        raise asyncio.CancelledError()
        db.execute("UPDATE task_instances SET state='failed', finished_at=?, message=?, details=? WHERE id=?",
                   (time.time(), V.mask(msg)[:2000], V.mask(details)[:12000], ti))
        return "failed", {}

    # -- outcome of an update installed by a task (checked after the processes restart)
    async def finish_update_followup(self) -> None:
        raw = db.get_setting(J.FOLLOWUP_KEY)
        if not raw:
            return
        f = json.loads(raw)
        if "dag_run" not in f:
            return await super().finish_update_followup()
        from . import updater
        deadline = f.get("started", time.time()) + 45 * 60
        ok, msg = False, f"Couldn't confirm the update to {f['to']} within 45 minutes; check Admin → Updates."
        while time.time() < deadline:
            cur = updater.current_version()
            prog = updater.progress(log_lines=0)
            if cur == f["to"] and prog.get("state") != "running":
                ok, msg = True, f"Update installed: the server now runs SysAdmin.IA {cur} (was {f['from']})."
                break
            if prog.get("state") == "failed":
                msg = f"The update to {f['to']} failed and was rolled back: {prog.get('message') or 'see Admin → Updates'}"
                break
            await asyncio.sleep(5)
        db.set_setting(J.FOLLOWUP_KEY, None)
        r = db.query_one("SELECT note, state FROM dag_runs WHERE id=?", (f["dag_run"],))
        if r:
            db.execute("UPDATE dag_runs SET note=?, state=? WHERE id=?",
                       ((r["note"] + " " + msg).strip(), r["state"] if ok else "failed", f["dag_run"]))


SCHEDULER = DagScheduler()


def catalog() -> dict:
    """Everything the DAG editor needs: operators (grouped by module), triggers, trigger rules, choices."""
    base = J.catalog()
    from . import pyenvs
    try:
        envs = [{"value": e["name"], "label": f"{e['name']} (Python {e['version'] or '?'})"} for e in pyenvs.list_envs(with_size=False) if e["ok"]]
        bp = pyenvs.base_python()
        base_label = f"Base Python ({pyenvs._version_of(bp) or 'python3'})"
    except Exception:  # noqa: BLE001
        envs, base_label = [], "Base Python"
    return {**base, "operators": operators(), "trigger_rules": TRIGGER_RULES, "pyenvs": envs, "base_python": base_label}
