// ------------------------------------------------------------ Jobs as DAGs (Airflow-like)
// List → DAG page (Graph / Runs / Code) → editor (drag-and-drop canvas, task inspector, Graph ↔ Code).

const NODE_W = 210, NODE_H = 66;
const TI_STATE = {   // task instance / run states, Airflow colours
  success: ["success", "#2e9d5b"], failed: ["failed", "#d64545"], running: ["running", "#3b82f6"], queued: ["queued", "#8a8f98"],
  up_for_retry: ["up for retry", "#e6a23c"], upstream_failed: ["upstream failed", "#e0793c"], skipped: ["skipped", "#c084fc"],
  none: ["not run yet", "#5b616b"],
};
const RUN_TYPE = { manual: "started by hand", scheduled: "on schedule", triggered: "by a trigger" };
state.dag = { list: [], cat: null, current: null, runs: [], edit: null, timer: null };

async function dagCatalog() {
  state.dag.cat = await api("/api/dags/catalog");
  state.jobCat = state.dag.cat;            // the trigger cards (addJobItem / jobField in app.js) read it
  return state.dag.cat;
}

function tiColor(s) { return (TI_STATE[s] || TI_STATE.none)[1]; }
function tiLabel(s) { return (TI_STATE[s] || [s])[0]; }
function fmtDur(a, b) {
  if (!a) return "";
  const s = Math.max(0, Math.round((b || Date.now() / 1000) - a));
  return s < 60 ? `${s} s` : s < 3600 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}
function opSpec(op) { return (state.dag.cat?.operators || {})[op] || { label: op, icon: "bolt", fields: [], group: "Other" }; }

// ------------------------------------------------------------ list
async function loadDags() {
  clearTimeout(state.dag.timer);
  const list = $("#dag-list");
  let dags;
  try { [dags] = await Promise.all([api("/api/dags"), state.dag.cat ? null : dagCatalog()]); }
  catch (ex) { list.textContent = ex.message; return; }
  state.dag.list = dags;
  list.innerHTML = "";
  if (!dags.length) {
    list.append(el("div", { class: "card empty-card" }, el("strong", {}, "No DAGs yet"),
      el("p", { class: "muted small" }, "For example: every night run a backup command and, only if it fails, email me; or when a disk is over 90% full, ask the AI what to delete and send its answer."),
      el("button", { type: "button", class: "primary", onclick: () => openDagEditor() }, withIcon("plus", "New DAG"))));
  }
  for (const d of dags) list.append(dagCard(d));
  if (dags.some((d) => d.active) && state.view === "jobs") state.dag.timer = setTimeout(loadDags, 4000);
}

function runSquares(runs, onClick) {
  return el("span", { class: "run-squares" }, [...runs].reverse().map((r) => el("button", {
    type: "button", class: "run-sq", style: `background:${tiColor(r.state)}`, "aria-label": tiLabel(r.state),
    title: `${tiLabel(r.state)} · ${r.run_id}${r.started_at ? " · " + fmtWhen(r.started_at) : ""}`, onclick: () => onClick && onClick(r),
  })));
}

function dagCard(d) {
  const toggle = el("input", { type: "checkbox", class: "switch", "aria-label": "Active (not paused)", title: d.paused ? "Paused: click to activate" : "Active: click to pause" });
  toggle.checked = !d.paused;
  toggle.addEventListener("change", async () => {
    try { await api(`/api/dags/${d.id}/paused`, { method: "POST", body: JSON.stringify({ paused: !toggle.checked }) }); loadDags(); }
    catch (ex) { toast(ex.message); toggle.checked = !toggle.checked; }
  });
  const running = d.recent_runs.filter((r) => r.state === "running").length, queued = d.recent_runs.filter((r) => r.state === "queued").length;
  return el("div", { class: `card job-card dag-card ${d.paused ? "disabled" : ""}` },
    el("div", { class: "ai-card-head" },
      toggle,
      el("div", { class: "ai-card-title" },
        el("div", {}, el("button", { type: "button", class: "link-btn dag-name mono", "data-no-i18n": "", onclick: () => openDag(d.id) }, d.dag_id), " ",
          d.paused ? el("span", { class: "tag" }, "Paused") : null, " ",
          running ? el("span", { class: "tag tag-accent" }, "Running") : null, " ",
          queued ? el("span", { class: "tag" }, "Queued") : null, " ",
          d.alerting ? el("span", { class: "tag tag-warn" }, "Alert active") : null),
        d.description ? el("div", { class: "muted small", "data-no-i18n": "" }, d.description) : null),
      el("div", { class: "ai-actions" },
        el("button", { type: "button", class: "small", title: "Start a run now", onclick: (e) => triggerDag(d, e.currentTarget) }, withIcon("play", "Run")),
        el("button", { type: "button", class: "small", onclick: () => openDagEditor(d.id) }, withIcon("edit", "Edit")),
        el("button", { type: "button", class: "small ghost", onclick: () => openDag(d.id) }, withIcon("history", "Runs")))),
    el("div", { class: "dag-card-body small" },
      el("div", {}, el("span", { class: "flow-label" }, "Triggers"), " ",
        d.trigger_summaries.length ? d.trigger_summaries.join(" · ") : el("span", { class: "muted" }, "none: it runs only when started by hand")),
      el("div", {}, el("span", { class: "flow-label" }, "Tasks"), " ", el("span", { "data-no-i18n": "" }, d.tasks.map((t) => t.task_id).join(", "))),
      el("div", { class: "dag-card-foot" },
        d.recent_runs.length ? runSquares(d.recent_runs, () => openDag(d.id, "runs")) : el("span", { class: "muted" }, "Never run yet"),
        d.next_run ? el("span", { class: "muted" }, ` · Next: ${fmtWhen(d.next_run)}`) : null)));
}

async function triggerDag(d, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/dags/${d.id}/trigger`, { method: "POST" });
    toast(r.warning || `Run of ${d.dag_id} queued: it starts within seconds.`, r.warning ? 8000 : 3500);
  } catch (ex) { toast(ex.message); }
  if (btn) btn.disabled = false;
  if (state.view === "jobs") loadDags(); else if (state.view === "dag") openDag(d.id, state.dag.tab, true);
}

$("#dag-add").addEventListener("click", () => openDagEditor());
$("#dag-back").addEventListener("click", () => switchView("jobs"));

// ------------------------------------------------------------ DAG page
async function openDag(pk, tab = null, keep = false) {
  clearTimeout(state.dag.timer);
  let d, runs;
  try { [d, runs] = await Promise.all([api(`/api/dags/${pk}`), api(`/api/dags/${pk}/runs?limit=25`), state.dag.cat ? null : dagCatalog()]); }
  catch (ex) { toast(ex.message); return; }
  state.dag.current = d;
  state.dag.runs = runs;
  state.dag.tab = tab || (keep ? state.dag.tab : "graph");
  if (!keep || !runs.some((r) => r.id === state.dag.runSel)) state.dag.runSel = runs[0]?.id ?? null;
  if (state.view !== "dag") switchView("dag");
  $("#dag-title").textContent = d.dag_id;
  $("#dag-sub").textContent = [d.description, d.trigger_summaries.length ? `Triggers: ${d.trigger_summaries.join(" · ")}` : "No triggers: started by hand only",
    d.next_run ? `Next: ${fmtWhen(d.next_run)}` : ""].filter(Boolean).join(" — ");
  const acts = $("#dag-actions");
  acts.innerHTML = "";
  acts.append(
    el("button", { type: "button", class: "primary small", onclick: (e) => triggerDag(d, e.currentTarget) }, withIcon("play", "Run now")),
    el("button", { type: "button", class: "small", onclick: () => openDagEditor(d.id) }, withIcon("edit", "Edit")),
    el("button", { type: "button", class: "small ghost", onclick: async () => {
      try { await api(`/api/dags/${d.id}/paused`, { method: "POST", body: JSON.stringify({ paused: !d.paused }) }); openDag(d.id, state.dag.tab, true); }
      catch (ex) { toast(ex.message); }
    } }, d.paused ? withIcon("play", "Activate") : withIcon("pause", "Pause")),
    el("button", { type: "button", class: "small ghost danger", onclick: async () => {
      if (!confirm(`Delete the DAG ${d.dag_id} and all its runs?`)) return;
      try { await api(`/api/dags/${d.id}`, { method: "DELETE" }); toast("DAG deleted."); switchView("jobs"); } catch (ex) { toast(ex.message); }
    } }, withIcon("trash", "Delete")));
  $$("#dag-tabs .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.dag.tab));
  renderDagTab();
  if (runs.some((r) => ["queued", "running"].includes(r.state))) {
    state.dag.timer = setTimeout(() => state.view === "dag" && state.dag.current?.id === pk && openDag(pk, null, true), 2500);
  }
}
$$("#dag-tabs .seg-btn").forEach((b) => b.addEventListener("click", () => {
  state.dag.tab = b.dataset.tab;
  $$("#dag-tabs .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
  renderDagTab();
}));

function renderDagTab() {
  const pane = $("#dag-pane"), d = state.dag.current, runs = state.dag.runs;
  pane.innerHTML = "";
  if (state.dag.tab === "code") {
    pane.append(el("div", { class: "card" },
      el("div", { class: "net-card-head" }, el("p", { class: "muted small" }, "The DAG as Airflow-style Python. It's read, never run."),
        el("button", { type: "button", class: "small", onclick: () => openDagEditor(d.id, "code") }, withIcon("edit", "Edit as code"))),
      el("pre", { class: "dag-code-view mono", "data-no-i18n": "" }, d.code)));
    return;
  }
  const run = runs.find((r) => r.id === state.dag.runSel) || null;
  const detail = el("div", { class: "ti-detail" });
  if (state.dag.tab === "graph") {
    const sel = el("select", { "aria-label": "Run" },
      ...(runs.length ? runs.map((r) => el("option", { value: String(r.id) }, `${r.run_id} · ${tiLabel(r.state)}`)) : [el("option", { value: "" }, "No runs yet")]));
    sel.value = String(run?.id ?? "");
    sel.addEventListener("change", () => { state.dag.runSel = Number(sel.value); renderDagTab(); });
    const states = run ? Object.fromEntries(run.tasks.map((t) => [t.task_id, t.state])) : {};
    const wrap = el("div", { class: "dag-canvas-wrap view-only" });
    const canvas = el("div", { class: "dag-canvas" });
    wrap.append(canvas);
    pane.append(el("div", { class: "dag-run-pick small" }, el("span", { class: "muted" }, "Showing run"), sel,
      run ? runBadge(run) : null), wrap, detail);
    drawGraph(canvas, d.tasks, d.layout, { states, onSelect: (tid) => showTi(detail, run, tid) });
    if (run) showRunSummary(detail, run);
    return;
  }
  // runs: a grid of task instances (rows: tasks, columns: runs, newest first) + the run list
  if (!runs.length) { pane.append(el("p", { class: "muted" }, "No runs yet. Use “Run now” to start one.")); return; }
  const grid = el("div", { class: "ti-grid", style: `grid-template-columns: minmax(120px, max-content) repeat(${runs.length}, 22px)` });
  grid.append(el("span", { class: "muted small" }, ""), ...runs.map((r) => el("button", {
    type: "button", class: `run-col ${r.id === state.dag.runSel ? "sel" : ""}`, style: `background:${tiColor(r.state)}`,
    title: `${r.run_id} · ${tiLabel(r.state)}`, "aria-label": r.run_id, onclick: () => { state.dag.runSel = r.id; renderDagTab(); },
  })));
  for (const t of d.tasks) {
    grid.append(el("span", { class: "mono small ti-name", "data-no-i18n": "" }, t.task_id));
    for (const r of runs) {
      const ti = r.tasks.find((x) => x.task_id === t.task_id);
      grid.append(el("button", { type: "button", class: "ti-cell", style: `background:${tiColor(ti?.state || "none")}`,
        title: `${t.task_id} · ${tiLabel(ti?.state || "none")} · ${r.run_id}`, "aria-label": `${t.task_id} ${tiLabel(ti?.state || "none")}`,
        onclick: () => { state.dag.runSel = r.id; showTi(detail, r, t.task_id); } }));
    }
  }
  const table = el("div", { class: "table-wrap" }, el("table", { class: "table fw-table run-table" },
    el("thead", {}, el("tr", {}, ...["Run", "State", "Started", "Took", "Why", ""].map((h) => el("th", {}, h)))),
    el("tbody", {}, ...runs.map((r) => el("tr", { class: r.id === state.dag.runSel ? "sel" : "", onclick: () => { state.dag.runSel = r.id; renderDagTab(); } },
      el("td", { class: "mono small", "data-no-i18n": "" }, r.run_id),
      el("td", {}, runBadge(r)),
      el("td", { class: "small" }, r.started_at ? fmtWhen(r.started_at) : fmtWhen(r.queued_at)),
      el("td", { class: "small" }, fmtDur(r.started_at, r.finished_at)),
      el("td", { class: "small" }, `${r.trigger}${RUN_TYPE[r.run_type] ? "" : ""}`, r.note ? el("div", { class: "muted small" }, r.note) : null),
      el("td", { class: "right" }, ["queued", "running"].includes(r.state) ? el("button", { type: "button", class: "small ghost danger",
        onclick: async (e) => {
          e.stopPropagation();
          if (!confirm(r.state === "queued" ? "Cancel this run before it starts?" : "Stop this run? Running tasks are interrupted and the rest are skipped.")) return;
          try { await api(`/api/dags/runs/${r.id}/stop`, { method: "POST" }); toast("Stopping…"); openDag(d.id, "runs", true); } catch (ex) { toast(ex.message); }
        } }, withIcon("stop", r.state === "queued" ? "Cancel" : "Stop")) : null))))));
  pane.append(el("div", { class: "card" }, el("div", { class: "ti-legend small" },
    ...Object.entries(TI_STATE).map(([k, [lbl, c]]) => el("span", {}, el("i", { style: `background:${c}` }), lbl))), grid), table, detail);
  if (run) showRunSummary(detail, run);
}

function runBadge(r) {
  return el("span", { class: "tag ti-tag", style: `--c:${tiColor(r.state)}` }, tiLabel(r.state));
}

function showRunSummary(box, r) {
  box.innerHTML = "";
  const counts = {};
  for (const t of r.tasks) counts[t.state] = (counts[t.state] || 0) + 1;
  box.append(el("div", { class: "card small" },
    el("div", {}, el("strong", { class: "mono", "data-no-i18n": "" }, r.run_id), " ", runBadge(r)),
    el("div", { class: "muted" }, `${r.trigger} · ${RUN_TYPE[r.run_type] || r.run_type}`,
      r.started_at ? ` · started ${fmtWhen(r.started_at)}` : "", r.finished_at ? ` · took ${fmtDur(r.started_at, r.finished_at)}` : ""),
    r.note ? el("div", {}, r.note) : null,
    el("div", { class: "ti-legend" }, ...Object.entries(counts).map(([s, n]) => el("span", {}, el("i", { style: `background:${tiColor(s)}` }), `${n} ${tiLabel(s)}`))),
    el("p", { class: "muted" }, "Click a task to see what it did.")));
}

function showTi(box, run, tid) {
  box.innerHTML = "";
  if (!run) { box.append(el("p", { class: "muted small" }, "No run selected.")); return; }
  const ti = run.tasks.find((x) => x.task_id === tid);
  if (!ti) { box.append(el("p", { class: "muted small" }, "This task didn't exist in that run.")); return; }
  box.append(el("div", { class: "card ti-card" },
    el("div", {}, el("strong", { class: "mono", "data-no-i18n": "" }, ti.task_id), " ", el("span", { class: "muted small" }, opSpec(ti.operator).label), " ",
      el("span", { class: "tag ti-tag", style: `--c:${tiColor(ti.state)}` }, tiLabel(ti.state))),
    el("div", { class: "muted small" }, [ti.try_number > 1 ? `try ${ti.try_number}` : "", ti.started_at ? `started ${fmtWhen(ti.started_at)}` : "",
      ti.finished_at && ti.started_at ? `took ${fmtDur(ti.started_at, ti.finished_at)}` : ""].filter(Boolean).join(" · ")),
    ti.message ? el("p", { class: ti.state === "failed" ? "error" : "" }, ti.message) : null,
    ti.output ? el("details", { open: true }, el("summary", { class: "small muted" }, "Output ({{tasks." + ti.task_id + ".output}})"),
      el("pre", { class: "update-notes mono small", "data-no-i18n": "" }, ti.output)) : null,
    ti.details ? el("details", {}, el("summary", { class: "small muted" }, "Details"), el("pre", { class: "update-notes mono small", "data-no-i18n": "" }, ti.details)) : null));
}

// ------------------------------------------------------------ graph drawing (view + editor)
function nodePos(layout, tid) { return layout[tid] || [40, 40]; }

function drawGraph(canvas, tasks, layout, opts = {}) {
  canvas.innerHTML = "";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "dag-edges");
  svg.innerHTML = `<defs><marker id="dag-arrow-${opts.editable ? "e" : "v"}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="currentColor"/></marker></defs>`;
  canvas.append(svg);
  let maxX = 600, maxY = 260;
  for (const t of tasks) {
    const [x, y] = nodePos(layout, t.task_id);
    maxX = Math.max(maxX, x + NODE_W + 80);
    maxY = Math.max(maxY, y + NODE_H + 60);
    const spec = opSpec(t.operator);
    const st = opts.states ? (opts.states[t.task_id] || "none") : null;
    const node = el("div", { class: `dag-node ${opts.selected === t.task_id ? "sel" : ""} ${st ? "has-state" : ""}`, style: `left:${x}px;top:${y}px;${st ? `--c:${tiColor(st)}` : ""}`,
      "data-tid": t.task_id, tabindex: "0", role: "button", "aria-label": `${t.task_id} (${spec.label})` },
      el("span", { class: "dag-node-icon", html: icon(spec.icon || "bolt") }),
      el("span", { class: "dag-node-text" }, el("span", { class: "dag-node-id mono", "data-no-i18n": "" }, t.task_id || "?"),
        el("span", { class: "dag-node-op small muted" }, spec.label),
        t.device ? el("span", { class: "dag-node-dev small", "data-no-i18n": "", title: "Runs on this device" }, "@ " + dagDeviceName(t.device)) : null,
        st ? el("span", { class: "dag-node-state small" }, tiLabel(st)) : null),
      opts.editable ? el("span", { class: "port in", title: "Input: connect a task's output here" }) : null,
      opts.editable ? el("span", { class: "port out", title: "Drag from here to the task that should run after this one" }) : null);
    node.addEventListener("click", () => opts.onSelect && opts.onSelect(t.task_id));
    node.addEventListener("keydown", (e) => { if (e.key === "Enter") opts.onSelect && opts.onSelect(t.task_id); });
    canvas.append(node);
  }
  canvas.style.width = `${maxX}px`;
  canvas.style.height = `${maxY}px`;
  svg.setAttribute("width", maxX);
  svg.setAttribute("height", maxY);
  drawEdges(svg, tasks, layout, opts);
}

function edgePath(a, b) {
  const x1 = a[0] + NODE_W, y1 = a[1] + NODE_H / 2, x2 = b[0], y2 = b[1] + NODE_H / 2;
  const dx = Math.max(40, Math.abs(x2 - x1) / 2);
  return `M${x1} ${y1} C${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2 - 2} ${y2}`;
}

function drawEdges(svg, tasks, layout, opts = {}) {
  svg.querySelectorAll("path.edge, path.edge-hit, g.edge-del").forEach((p) => p.remove());
  const ns = "http://www.w3.org/2000/svg";
  for (const t of tasks) {
    for (const u of t.upstream) {
      const d = edgePath(nodePos(layout, u), nodePos(layout, t.task_id));
      const key = `${u}>${t.task_id}`;
      const p = document.createElementNS(ns, "path");
      p.setAttribute("d", d);
      p.setAttribute("class", `edge ${opts.selectedEdge === key ? "sel" : ""}`);
      p.setAttribute("marker-end", `url(#dag-arrow-${opts.editable ? "e" : "v"})`);
      svg.append(p);
      if (opts.editable) {
        const hit = document.createElementNS(ns, "path");
        hit.setAttribute("d", d);
        hit.setAttribute("class", "edge-hit");
        hit.addEventListener("click", (e) => { e.stopPropagation(); opts.onEdge && opts.onEdge(key); });
        const title = document.createElementNS(ns, "title");
        title.textContent = `${u} → ${t.task_id} (click to select, then Delete)`;
        hit.append(title);
        svg.append(hit);
        if (opts.selectedEdge === key) {   // a small × at the middle of the selected connection
          const a = nodePos(layout, u), b = nodePos(layout, t.task_id);
          const mx = (a[0] + NODE_W + b[0]) / 2, my = (a[1] + b[1]) / 2 + NODE_H / 2;
          const g = document.createElementNS(ns, "g");
          g.setAttribute("class", "edge-del");
          g.innerHTML = `<circle cx="${mx}" cy="${my}" r="10"/><path d="M${mx - 4} ${my - 4}L${mx + 4} ${my + 4}M${mx + 4} ${my - 4}L${mx - 4} ${my + 4}"/>`;
          g.addEventListener("click", (e) => { e.stopPropagation(); opts.onDeleteEdge && opts.onDeleteEdge(key); });
          svg.append(g);
        }
      }
    }
  }
}

// ------------------------------------------------------------ editor
const ED = () => state.dag.edit;

async function openDagEditor(pk = null, mode = "graph") {
  clearTimeout(state.dag.timer);
  try { await dagCatalog(); } catch (ex) { toast(ex.message); return; }
  let d = null;
  if (pk) {
    try { d = await api(`/api/dags/${pk}`); } catch (ex) { toast(ex.message); return; }
  }
  state.dag.edit = {
    pk, mode: "graph", selected: null, selectedEdge: null,
    tasks: d ? JSON.parse(JSON.stringify(d.tasks)) : [],
    layout: d ? { ...d.layout } : {},
  };
  $("#dag-edit-title").textContent = d ? `Edit ${d.dag_id}` : "New DAG";
  $("#dag-id").value = d ? d.dag_id : "";
  $("#dag-desc").value = d ? d.description : "";
  $("#dag-paused").checked = d ? d.paused : false;
  $("#dag-mar").value = d ? d.options.max_active_runs || 1 : 1;
  $("#job-triggers").innerHTML = "";
  (d ? d.triggers : []).forEach((t) => addJobItem("trigger", t));
  $("#dag-triggers-box").open = true;
  setFormMsg("#dag-msg", "");
  renderPalette();
  setDagMode("graph", true);
  switchView("dag-edit");
  renderEditor();
  if (!d) $("#dag-id").focus();
  if (mode === "code") setDagMode("code");
}

function renderPalette() {
  const pal = $("#dag-palette");
  pal.innerHTML = "";
  pal.append(el("div", { class: "flow-label" }, "Operators"), el("p", { class: "muted small" }, "Drag onto the canvas, or click to add."));
  const groups = {};
  for (const [k, o] of Object.entries(state.dag.cat.operators)) (groups[o.group || "Other"] ||= []).push([k, o]);
  for (const [g, items] of Object.entries(groups)) {
    const box = el("details", { class: "pal-group", open: true }, el("summary", { class: "small" }, g));
    for (const [k, o] of items) {
      const it = el("button", { type: "button", class: "pal-item", draggable: "true", title: o.description, "data-op": k },
        el("span", { class: "pal-icon", html: icon(o.icon || "bolt") }), el("span", {}, o.label));
      it.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", `op:${k}`); e.dataTransfer.effectAllowed = "copy"; });
      it.addEventListener("click", () => addTask(k));
      box.append(it);
    }
    pal.append(box);
  }
}

function uniqueTaskId(base) {
  const ids = new Set(ED().tasks.map((t) => t.task_id));
  let id = base.replace(/[^A-Za-z0-9_]/g, "_").replace(/^[^A-Za-z]+/, "") || "task";
  if (!ids.has(id)) return id;
  let n = 2;
  while (ids.has(`${id}_${n}`)) n++;
  return `${id}_${n}`;
}

function addTask(op, at = null) {
  const e = ED();
  const spec = opSpec(op);
  const params = {};
  for (const f of spec.fields) if (f.default !== undefined) params[f.key] = f.default;
  const tid = uniqueTaskId(op);
  e.tasks.push({ task_id: tid, operator: op, params, upstream: [], retries: 0, retry_delay: 60, trigger_rule: "all_success", execution_timeout: null });
  if (at) e.layout[tid] = at;
  else {   // next free spot: right of the selected task (connected to it), or below the others
    const sel = e.tasks.find((t) => t.task_id === e.selected && t.task_id !== tid);
    if (sel) {
      const [x, y] = nodePos(e.layout, sel.task_id);
      let ny = y;
      while (Object.values(e.layout).some(([px, py]) => Math.abs(px - (x + 260)) < 40 && Math.abs(py - ny) < 60)) ny += 100;
      e.layout[tid] = [x + 260, ny];
      e.tasks.find((t) => t.task_id === tid).upstream.push(sel.task_id);
    } else {
      const ys = Object.values(e.layout).map(([, y]) => y);
      e.layout[tid] = [40, ys.length ? Math.max(...ys) + 110 : 40];
    }
  }
  e.selected = tid;
  e.selectedEdge = null;
  renderEditor();
}

function wouldCycle(from, to) {
  // adding from → to makes a cycle if `from` is reachable from `to`
  const down = {};
  for (const t of ED().tasks) for (const u of t.upstream) (down[u] ||= []).push(t.task_id);
  const seen = new Set(), stack = [to];
  while (stack.length) {
    const n = stack.pop();
    if (n === from) return true;
    if (seen.has(n)) continue;
    seen.add(n);
    stack.push(...(down[n] || []));
  }
  return false;
}

function connect(from, to) {
  const t = ED().tasks.find((x) => x.task_id === to);
  if (!t || from === to) return;
  if (t.upstream.includes(from)) return;
  if (wouldCycle(from, to)) { toast("That connection would make a loop: a DAG can't have cycles."); return; }
  t.upstream.push(from);
  renderEditor();
}

function renderEditor() {
  const e = ED();
  const canvas = $("#dag-canvas");
  drawGraph(canvas, e.tasks, e.layout, {
    editable: true, selected: e.selected, selectedEdge: e.selectedEdge,
    onSelect: (tid) => { e.selected = tid; e.selectedEdge = null; renderEditor(); },
    onEdge: (key) => { e.selectedEdge = key; e.selected = null; renderEditor(); },
    onDeleteEdge: deleteEdge,
  });
  $("#dag-empty").hidden = e.tasks.length > 0;
  bindCanvas(canvas);
  renderInspector();
}

function deleteEdge(key) {
  const [u, d] = key.split(">");
  const t = ED().tasks.find((x) => x.task_id === d);
  if (t) t.upstream = t.upstream.filter((x) => x !== u);
  ED().selectedEdge = null;
  renderEditor();
}

function deleteTask(tid) {
  const e = ED();
  e.tasks = e.tasks.filter((t) => t.task_id !== tid);
  for (const t of e.tasks) t.upstream = t.upstream.filter((u) => u !== tid);
  delete e.layout[tid];
  if (e.selected === tid) e.selected = null;
  renderEditor();
}

// pointer interactions: move nodes, connect ports
function canvasPoint(canvas, ev) {
  const r = canvas.getBoundingClientRect();
  return [ev.clientX - r.left, ev.clientY - r.top];
}

function bindCanvas(canvas) {
  canvas.querySelectorAll(".dag-node").forEach((node) => {
    const tid = node.dataset.tid;
    node.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0) return;
      const e = ED();
      if (ev.target.classList.contains("port")) {
        if (!ev.target.classList.contains("out")) return;
        ev.preventDefault();
        startConnect(canvas, tid, ev);
        return;
      }
      const start = canvasPoint(canvas, ev), [x0, y0] = nodePos(e.layout, tid);
      let moved = false;
      try { node.setPointerCapture(ev.pointerId); } catch { /* not a real pointer (e.g. synthetic): moves still reach the node */ }
      const move = (m) => {
        const [x, y] = canvasPoint(canvas, m);
        if (!moved && Math.hypot(x - start[0], y - start[1]) < 4) return;
        moved = true;
        const nx = Math.max(0, Math.round((x0 + x - start[0]) / 10) * 10), ny = Math.max(0, Math.round((y0 + y - start[1]) / 10) * 10);
        e.layout[tid] = [nx, ny];
        node.style.left = `${nx}px`;
        node.style.top = `${ny}px`;
        drawEdges(canvas.querySelector("svg"), e.tasks, e.layout, { editable: true, selectedEdge: e.selectedEdge, onEdge: (k) => { e.selectedEdge = k; e.selected = null; renderEditor(); }, onDeleteEdge: deleteEdge });
      };
      const up = () => {
        node.removeEventListener("pointermove", move);
        node.removeEventListener("pointerup", up);
        if (moved) { node.addEventListener("click", (c) => c.stopImmediatePropagation(), { capture: true, once: true }); renderEditor(); }
      };
      node.addEventListener("pointermove", move);
      node.addEventListener("pointerup", up);
    });
  });
}

function startConnect(canvas, from, ev) {
  const e = ED();
  const svg = canvas.querySelector("svg");
  const temp = document.createElementNS("http://www.w3.org/2000/svg", "path");
  temp.setAttribute("class", "edge temp");
  svg.append(temp);
  const a = nodePos(e.layout, from);
  const move = (m) => {
    const [x, y] = canvasPoint(canvas, m);
    temp.setAttribute("d", edgePath(a, [x, y - NODE_H / 2]));
    canvas.querySelectorAll(".dag-node").forEach((n) => n.classList.toggle("drop", n.contains(document.elementFromPoint(m.clientX, m.clientY)) && n.dataset.tid !== from));
  };
  const up = (u) => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    temp.remove();
    const target = document.elementFromPoint(u.clientX, u.clientY)?.closest(".dag-node");
    canvas.querySelectorAll(".dag-node.drop").forEach((n) => n.classList.remove("drop"));
    if (target && target.dataset.tid !== from) connect(from, target.dataset.tid);
  };
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
  move(ev);
}

const canvasWrap = $("#dag-canvas-wrap");
canvasWrap.addEventListener("dragover", (ev) => { ev.preventDefault(); ev.dataTransfer.dropEffect = "copy"; });
canvasWrap.addEventListener("drop", (ev) => {
  ev.preventDefault();
  const data = ev.dataTransfer.getData("text/plain") || "";
  if (!data.startsWith("op:")) return;
  const [x, y] = canvasPoint($("#dag-canvas"), ev);
  addTask(data.slice(3), [Math.max(0, Math.round((x - NODE_W / 2) / 10) * 10), Math.max(0, Math.round((y - NODE_H / 2) / 10) * 10)]);
});
canvasWrap.addEventListener("click", (ev) => {
  if (ev.target.closest(".dag-node") || ev.target.closest(".edge-hit") || ev.target.closest(".edge-del") || ev.target.closest("button")) return;
  const e = ED();
  if (e && (e.selected || e.selectedEdge)) { e.selected = null; e.selectedEdge = null; renderEditor(); }
});
document.addEventListener("keydown", (ev) => {
  if (state.view !== "dag-edit" || ED()?.mode !== "graph") return;
  if (!["Delete", "Backspace"].includes(ev.key) || ev.target.closest?.("input, textarea, select, [contenteditable]")) return;
  const e = ED();
  if (e.selectedEdge) { ev.preventDefault(); deleteEdge(e.selectedEdge); }
  else if (e.selected) { ev.preventDefault(); if (confirm(`Remove the task ${e.selected}?`)) deleteTask(e.selected); }
});
$("#dag-autolayout").addEventListener("click", () => {
  const e = ED();
  const depth = {}, by = Object.fromEntries(e.tasks.map((t) => [t.task_id, t]));
  const d = (tid, seen = new Set()) => {
    if (depth[tid] != null) return depth[tid];
    seen.add(tid);
    const ups = by[tid].upstream.filter((u) => by[u] && !seen.has(u));
    return (depth[tid] = ups.length ? 1 + Math.max(...ups.map((u) => d(u, seen))) : 0);
  };
  const rows = {};
  for (const t of e.tasks) {
    const c = d(t.task_id);
    const r = rows[c] = (rows[c] ?? -1) + 1;
    e.layout[t.task_id] = [40 + c * 260, 40 + r * 110];
  }
  renderEditor();
});

// ---- inspector (the selected task's settings)
function collectFields(box) {
  const vals = {};
  box.querySelectorAll("[data-key]").forEach((i) => {
    if (i.tagName === "LABEL") return;
    const ft = i.dataset.ftype;
    if (ft === "bool") vals[i.dataset.key] = i.checked;
    else if (ft === "number") vals[i.dataset.key] = i.value === "" ? null : Number(i.value);
    else if (ft === "weekdays") vals[i.dataset.key] = [...i.querySelectorAll("input:checked")].map((c) => c.value);
    else vals[i.dataset.key] = i.value;
  });
  return vals;
}

function showIf(box, spec) {
  const vals = collectFields(box);
  for (const fd of spec.fields) {
    if (!fd.show_if) continue;
    const lbl = box.querySelector(`label[data-key="${fd.key}"]`);
    if (lbl) lbl.hidden = !Object.entries(fd.show_if).every(([k, allowed]) => allowed.includes(vals[k]));
  }
}

function renderInspector() {
  const e = ED(), box = $("#dag-inspector");
  box.innerHTML = "";
  const t = e.tasks.find((x) => x.task_id === e.selected);
  if (!t) {
    box.append(el("div", { class: "flow-label" }, "Task"),
      el("p", { class: "muted small" }, e.selectedEdge
        ? "A connection is selected: press Delete (or click its ×) to remove it."
        : "Select a task on the canvas to edit it. Drag a task by its body to move it; drag from its right dot to another task's left dot to make that one run after it."),
      el("div", { class: "flow-label" }, "Trigger rules"),
      el("dl", { class: "small rule-list" }, ...Object.entries(state.dag.cat.trigger_rules).flatMap(([k, v]) => [el("dt", { class: "mono" }, k), el("dd", { class: "muted" }, v)])));
    return;
  }
  const spec = opSpec(t.operator);
  const idInput = el("input", { class: "mono", value: t.task_id, maxlength: "64", spellcheck: "false", autocomplete: "off" });
  idInput.addEventListener("change", () => {
    const nid = idInput.value.trim();
    if (!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(nid)) { toast("Task ids start with a letter and use letters, digits and _."); idInput.value = t.task_id; return; }
    if (nid !== t.task_id && e.tasks.some((x) => x.task_id === nid)) { toast(`There's already a task ${nid}.`); idInput.value = t.task_id; return; }
    const old = t.task_id;
    for (const x of e.tasks) x.upstream = x.upstream.map((u) => (u === old ? nid : u));
    e.layout[nid] = e.layout[old];
    delete e.layout[old];
    t.task_id = nid;
    e.selected = nid;
    renderEditor();
  });
  const fields = el("div", { class: "item-fields" });
  for (const fd of spec.fields) fields.append(jobField(fd, t.params[fd.key] ?? fd.default));
  const sync = () => { Object.assign(t.params, collectFields(fields)); showIf(fields, spec); };
  fields.addEventListener("input", sync);
  fields.addEventListener("change", sync);
  showIf(fields, spec);
  const num = (v, min, max, ph = "") => el("input", { type: "number", min, max, value: v ?? "", placeholder: ph });
  const retries = num(t.retries || 0, 0, 10), delay = num(t.retry_delay ?? 60, 0, 3600), limit = num(t.execution_timeout, 1, 86400, "none");
  const rule = el("select", {}, ...Object.entries(state.dag.cat.trigger_rules).map(([k, v]) => el("option", { value: k }, `${k} — ${v}`)));
  rule.value = t.trigger_rule || "all_success";
  const syncAdv = () => {
    t.retries = Number(retries.value || 0); t.retry_delay = Number(delay.value || 0);
    t.execution_timeout = limit.value ? Number(limit.value) : null; t.trigger_rule = rule.value;
  };
  [retries, delay, limit, rule].forEach((i) => i.addEventListener("change", syncAdv));
  // Where the task runs: this server or a device from Server → Devices (only operators that can run remotely).
  let runOn = null;
  const devs = state.dag.cat.devices || [];
  if ((state.dag.cat.remote_operators || []).includes(t.operator) && (devs.length || t.device)) {
    const sel = el("select", {}, el("option", { value: "" }, "This server"),
      ...devs.map((d) => el("option", { value: String(d.value) }, `${d.label} (${d.host})`)));
    if (t.device && !devs.some((d) => String(d.value) === String(t.device))) sel.append(el("option", { value: String(t.device) }, `${t.device} (not found)`));
    sel.value = t.device ? String(t.device) : "";
    sel.addEventListener("change", () => { t.device = sel.value ? (Number(sel.value) || sel.value) : null; renderEditor(); });
    runOn = el("label", { class: "field" }, "Runs on", sel,
      el("span", { class: "muted small" }, t.device
        ? (t.operator === "run_python" ? "Runs over SSH with the device's python3; a Python file must be on the device." : "Runs over SSH on the device, with the device's user.")
        : "Pick a device to run this task there instead (Server → Devices)."));
  }
  box.append(
    el("div", { class: "insp-head" }, el("span", { class: "icon-tile small-tile", html: icon(spec.icon || "bolt") }),
      el("div", {}, el("strong", {}, spec.label), el("div", { class: "muted small" }, spec.description))),
    el("label", { class: "field" }, "Task id", idInput, el("span", { class: "muted small" }, `Other tasks use its output as {{tasks.${t.task_id}.output}}.`)),
    runOn || "",
    t.upstream.length ? el("p", { class: "small" }, "Runs after: ", el("span", { class: "mono", "data-no-i18n": "" }, t.upstream.join(", "))) :
      el("p", { class: "small muted" }, "Runs first (nothing before it)."),
    fields,
    el("details", { class: "insp-adv", open: t.retries || t.execution_timeout || t.trigger_rule !== "all_success" ? true : null },
      el("summary", { class: "small" }, "Retries, time limit and trigger rule"),
      el("div", { class: "two-col" }, el("label", { class: "field" }, "Retries", retries), el("label", { class: "field" }, "Wait between tries (s)", delay)),
      el("label", { class: "field" }, "Time limit (s)", limit, el("span", { class: "muted small" }, "Stopped and marked failed if it takes longer.")),
      el("label", { class: "field" }, "Run when", rule)),
    el("button", { type: "button", class: "small ghost danger", onclick: () => deleteTask(t.task_id) }, withIcon("trash", "Remove this task")),
    el("div", { class: "flow-label" }, "Placeholders"),
    el("p", { class: "muted small" }, "Click one to insert it in the text box you were editing."),
    dagPlaceholders(t));
}

function dagDeviceName(id) {
  const d = ((state.dag && state.dag.cat && state.dag.cat.devices) || []).find((x) => String(x.value) === String(id));
  return d ? d.label : String(id);
}

function dagPlaceholders(t) {
  const cat = state.dag.cat, e = ED();
  const groups = [];
  const trig = new Set();
  $$("#job-triggers .item-type").forEach((s) => (cat.triggers[s.value]?.placeholders || []).forEach((p) => trig.add(p)));
  if (trig.size) groups.push(["From the trigger", [...trig]]);
  // every task before this one (ancestors) can be referenced
  const by = Object.fromEntries(e.tasks.map((x) => [x.task_id, x]));
  const anc = new Set(), stack = [...(t?.upstream || [])];
  while (stack.length) { const n = stack.pop(); if (!anc.has(n) && by[n]) { anc.add(n); stack.push(...by[n].upstream); } }
  const before = ["last.output", ...[...anc].map((a) => `tasks.${a}.output`)];
  for (const a of anc) if (by[a].operator === "run_command") before.push(`tasks.${a}.exit_code`);
  groups.push(["From earlier tasks", before]);
  if (cat.variables.length) groups.push(["Your variables", cat.variables.map((v) => `var.${v.name}`)]);
  const mon = cat.monitor_placeholders || [];
  if (mon.length) groups.push(["Monitor values", mon.filter((m) => !/^mon\.(disk|temp)\./.test(m.key)).map((m) => [m.key, m.label]), true]);
  groups.push(["Built-in", cat.system_placeholders, true]);
  const box = el("div", { class: "insp-ph" });
  for (const [title, items, collapsed] of groups) {
    const chips = el("div", { class: "ph-chips" }, items.map((p) => {
      const [key, label] = Array.isArray(p) ? p : [p, ""];
      return el("button", { type: "button", class: "ph-chip", title: label || null, onclick: () => insertPlaceholder(key) }, ph(key));
    }));
    box.append(collapsed ? el("details", { class: "ph-group" }, el("summary", { class: "flow-label" }, title), chips)
      : el("div", { class: "ph-group" }, el("div", { class: "flow-label" }, title), chips));
  }
  return box;
}

// trigger cards (app.js) call this when they change
function renderPlaceholders() {
  if (state.view === "dag-edit" && ED()) renderInspector();
}

$("#dag-add-trigger").addEventListener("click", () => { addJobItem("trigger", { type: "schedule" }); renderPlaceholders(); });

// ---- the DAG as data (from the Graph editor)
function editorModel() {
  const e = ED();
  return {
    dag_id: $("#dag-id").value.trim(), description: $("#dag-desc").value.trim(), paused: $("#dag-paused").checked,
    options: { max_active_runs: Number($("#dag-mar").value || 1) },
    triggers: $$("#job-triggers .job-item").map(itemValues),
    tasks: e.tasks, layout: e.layout,
  };
}

function applyModel(d) {
  const e = ED();
  $("#dag-id").value = d.dag_id || "";
  $("#dag-desc").value = d.description || "";
  $("#dag-paused").checked = !!d.paused;
  $("#dag-mar").value = (d.options || {}).max_active_runs || 1;
  $("#job-triggers").innerHTML = "";
  (d.triggers || []).forEach((t) => addJobItem("trigger", t));
  e.tasks = d.tasks;
  e.layout = { ...d.layout };
  if (!e.tasks.some((t) => t.task_id === e.selected)) e.selected = null;
  e.selectedEdge = null;
}

async function setDagMode(mode, quiet = false) {
  const e = ED();
  if (!quiet && mode === e.mode) return;
  if (!quiet && mode === "code") {
    try {
      const r = await api("/api/dags/to-code", { method: "POST", body: JSON.stringify(editorModel()) });
      $("#dag-code").value = r.code;
      setFormMsg("#dag-msg", r.warning ? `Not valid yet: ${r.warning}` : "", false);
    } catch (ex) { setFormMsg("#dag-msg", ex.message, false); return; }
  }
  if (!quiet && mode === "graph") {
    try {
      const r = await api("/api/dags/code", { method: "POST", body: JSON.stringify({ code: $("#dag-code").value, layout: e.layout }) });
      applyModel(r.dag);
      setFormMsg("#dag-msg", r.warning ? `Not valid yet: ${r.warning}` : "", false);
    } catch (ex) { setFormMsg("#dag-msg", ex.message, false); return; }   // keep the code view until it parses
  }
  e.mode = mode;
  $$("#dag-mode .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#dag-graph-mode").hidden = mode !== "graph";
  $("#dag-code-mode").hidden = mode !== "code";
  if (mode === "graph") renderEditor();
  else $("#dag-code").focus();
}
$$("#dag-mode .seg-btn").forEach((b) => b.addEventListener("click", () => setDagMode(b.dataset.mode)));
$("#dag-code").addEventListener("keydown", (ev) => {   // Tab indents in the code box
  if (ev.key !== "Tab") return;
  ev.preventDefault();
  const t = ev.target, s = t.selectionStart;
  t.setRangeText("    ", s, t.selectionEnd, "end");
});

$("#dag-save").addEventListener("click", async () => {
  const e = ED();
  const body = e.mode === "code" ? { code: $("#dag-code").value, layout: e.layout } : editorModel();
  $("#dag-save").disabled = true;
  try {
    const d = await api(e.pk ? `/api/dags/${e.pk}` : "/api/dags", { method: e.pk ? "PUT" : "POST", body: JSON.stringify(body) });
    toast(e.pk ? "DAG saved." : `DAG ${d.dag_id} created.`);
    openDag(d.id);
  } catch (ex) { setFormMsg("#dag-msg", ex.message, false); }
  $("#dag-save").disabled = false;
});
const leaveEditor = () => {
  const e = ED();
  if (e?.pk) openDag(e.pk); else switchView("jobs");
};
$("#dag-cancel").addEventListener("click", leaveEditor);
$("#dag-edit-back").addEventListener("click", leaveEditor);

window.loadDags = loadDags;
window.openDag = openDag;
window.openDagEditor = openDagEditor;
