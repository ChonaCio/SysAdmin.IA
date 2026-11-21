// ------------------------------------------------------------ Processes (Server → Processes)
// SysAdmin.IA runs as separate processes (web interface, jobs scheduler, network watcher, names & websites) under a supervisor.
// This page shows each one and starts / stops / restarts it; modules whose process is down show a notice.

const PROC_STATES = {
  running: ["running", "ok"], starting: ["starting…", ""], stopped: ["stopped", ""],
  crashed: ["crashed: restarting…", "warn"], failed: ["failed", "crit"], "not responding": ["not responding", "warn"],
};
// what stops working in each module when its process is down
const PROC_IMPACT = {
  jobs: "No job (DAG) runs until it's running again; runs you start wait in the queue.",
  network: "Periodic network scans and dynamic DNS updates are paused. The pages still work.",
  names: ".local names aren't announced and the websites' proxy isn't watched. You can still edit names and websites.",
};
const VIEW_MODULE = { jobs: "jobs", dag: "jobs", "dag-edit": "jobs", network: "network" };
// the Network module's Names & websites tab depends on its own process
function viewModule(view) {
  if (view === "network" && state.net?.tab === "names") return "names";
  return VIEW_MODULE[view];
}
const PROXY_STATE = { none: "not needed (no websites)", running: "running", restarted: "was stopped: started again",
  "no docker": "Docker isn't available", unknown: "not checked yet" };

function fmtUptime(sec) {
  if (sec == null) return "–";
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} s`;
  if (sec < 3600) return `${Math.floor(sec / 60)} min`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} h ${Math.floor((sec % 3600) / 60)} min`;
  return `${Math.floor(sec / 86400)} d ${Math.floor((sec % 86400) / 3600)} h`;
}

function procStateOf(w) {
  return w.responding === false ? "not responding" : w.state;
}

async function loadProcesses() {
  clearTimeout(state.procTimer);
  const list = $("#proc-list");
  // "Runs on" a device (Server → Devices): that device's processes instead of SysAdmin.IA's own
  const dev = window.targetDevice ? targetDevice("processes") : null;
  $("#proc-restart-all").hidden = !!dev;
  $("#proc-mode").hidden = !!dev;
  list.hidden = !!dev;
  if (dev) {
    await loadSysProcs(dev);
    if (state.view === "processes") state.procTimer = setTimeout(loadProcesses, 10000);
    return;
  }
  let st;
  try { st = await api("/api/processes"); }
  catch (ex) {
    list.innerHTML = "";
    list.append(el("div", { class: "card" }, el("p", { class: "error" }, ex.message),
      el("p", { class: "muted small" }, "The supervisor manages the processes. If it isn't answering, restart the service on the server: sudo systemctl restart sysadmin-ia")));
    state.procTimer = setTimeout(() => state.view === "processes" && loadProcesses(), 5000);
    return;
  }
  if (state.view !== "processes") return;
  $("#proc-mode").textContent = st.supervised
    ? `Supervisor running for ${fmtUptime(Date.now() / 1000 - st.started_at)} (pid ${st.pid}).`
    : "Running as a single process (development mode): everything shares one process, so parts can't be started or stopped separately.";
  const open = new Set($$("#proc-list details.proc-logs[open]").map((d) => d.dataset.name));
  list.innerHTML = "";
  for (const w of st.workers) list.append(procCard(w, st.supervised, open.has(w.name)));
  state.procSummary = null;
  refreshProcIndicators();
  await loadSysProcs(null);
  state.procTimer = setTimeout(() => state.view === "processes" && loadProcesses(), 5000);
}

// ---- every process of this server or of a device: sort, filter, stop (TERM) or kill (KILL)
state.sysprocs = { sort: "cpu", filter: "", limit: 60 };
async function loadSysProcs(dev) {
  const box = $("#proc-device");
  const s = state.sysprocs;
  let r;
  try { r = await api(`/api/sysprocs?sort=${s.sort}&limit=${s.limit}`); }
  catch (ex) {
    box.hidden = false;
    box.innerHTML = "";
    box.append(el("div", { class: "card" }, el("p", { class: "error small" }, ex.message),
      dev && window.deviceReconnect ? el("button", { type: "button", class: "small", onclick: () => deviceReconnect(dev.id, loadProcesses) }, withIcon("refresh", "Reconnect")) : null));
    return;
  }
  if (state.view !== "processes" || String((targetDevice("processes") || {}).id || "") !== String((dev || {}).id || "")) return;
  const keep = $("#sp-filter");
  const focused = document.activeElement === keep;
  box.hidden = false;
  box.innerHTML = "";
  const filter = el("input", { type: "search", id: "sp-filter", placeholder: "Filter by name, user or command…", value: s.filter,
    oninput: (e) => { s.filter = e.target.value; draw(); } });
  const sort = el("select", { "aria-label": "Sort by", onchange: (e) => { s.sort = e.target.value; loadProcesses(); } },
    [["cpu", "Most CPU"], ["mem", "Most memory"], ["name", "Name"], ["pid", "Process id"]].map(([v, l]) => el("option", { value: v, selected: s.sort === v }, l)));
  const limit = el("select", { "aria-label": "How many", onchange: (e) => { s.limit = Number(e.target.value); loadProcesses(); } },
    [30, 60, 150, 500].map((n) => el("option", { value: String(n), selected: s.limit === n }, `Top ${n}`)));
  const rows = el("tbody");
  const app = new Set(r.app_pids || []);
  const draw = () => {
    rows.innerHTML = "";
    const q = s.filter.toLowerCase();
    const list = r.procs.filter((p) => !q || `${p.name} ${p.user} ${p.cmd} ${p.pid}`.toLowerCase().includes(q));
    if (!list.length) rows.append(el("tr", {}, el("td", { colspan: "7", class: "muted small" }, "No processes match.")));
    for (const p of list) {
      const sig = (signal, label, cls) => el("button", { type: "button", class: `small ghost ${cls || ""}`, title: label, onclick: async (e) => {
        const what = signal === "KILL" ? `Kill ${p.name} (process ${p.pid}) at once?\n\nIt can't save anything or clean up.`
          : `Stop ${p.name} (process ${p.pid})?\n\nIt's asked to end (SIGTERM).`;
        if (!confirm(what)) return;
        e.currentTarget.disabled = true;
        try { await api(`/api/sysprocs/${p.pid}/signal`, { method: "POST", body: JSON.stringify({ signal }) }); toast(`Signal sent to ${p.name}.`); }
        catch (ex) { toast(ex.message, 7000); }
        setTimeout(loadProcesses, 800);
      } }, label);
      const own = app.has(p.pid);
      rows.append(el("tr", {},
        el("td", { class: "num mono" }, String(p.pid)),
        el("td", { class: "sp-name" }, el("strong", { "data-no-i18n": "" }, p.name), own ? el("span", { class: "tag" }, "SysAdmin.IA") : null,
          el("div", { class: "muted small mono dk-ellipsis", title: p.cmd, "data-no-i18n": "" }, p.cmd)),
        el("td", { class: "small mono", "data-no-i18n": "" }, p.user),
        el("td", { class: "num" }, `${p.cpu.toFixed(1)}%`),
        el("td", { class: "num nowrap" }, fmtBytes(p.mem)),
        el("td", { class: "small muted nowrap hide-sm" }, p.started ? fmtWhen(p.started) : ""),
        el("td", { class: "num nowrap" }, own ? el("span", { class: "muted small" }, "use the cards above") : [sig("TERM", "Stop"), sig("KILL", "Kill", "danger")])));
    }
  };
  draw();
  const who = dev ? (r.device?.name || dev.name) : "this server";
  box.append(el("div", { class: "card sp-card" },
    el("div", { class: "ai-card-head" },
      el("span", { class: "icon-tile", html: icon("monitor") }),
      el("div", { class: "ai-card-title" }, el("b", {}, dev ? `Processes on ${who}` : "All processes on this server"),
        el("div", { class: "muted small" }, el("span", {}, `${r.total} processes.`), " ", el("span", {}, dev
          ? (r.sudo ? "Any process can be stopped (sudo works without a password)." : `Only the processes of ${r.device?.user || "the device's user"} can be stopped; others need sudo without a password (Server → Devices).`)
          : "SysAdmin.IA's user can only stop its own processes; its own parts are managed with the cards above.")))),
    el("div", { class: "dk-toolbar" }, filter, sort, limit),
    el("div", { class: "table-wrap" }, el("table", { class: "pyenv-ver-table small sp-table" },
      el("thead", {}, el("tr", {}, el("th", {}, "PID"), el("th", {}, "Name"), el("th", {}, "User"), el("th", {}, "CPU"), el("th", {}, "Memory"),
        el("th", { class: "hide-sm" }, "Started"), el("th", {}, ""))), rows))));
  if (focused) { filter.focus(); filter.setSelectionRange(filter.value.length, filter.value.length); }
}

function procCard(w, supervised, logsOpen) {
  const s = procStateOf(w);
  const [label, cls] = PROC_STATES[s] || [s, ""];
  const now = Date.now() / 1000;
  const facts = [
    ["Process id", w.pid ?? "–"],
    ["Running for", w.state === "running" && w.started_at ? fmtUptime(now - w.started_at) : "–"],
    ["CPU", w.cpu != null ? `${w.cpu.toFixed(1)}%` : "–"],
    ["Memory", w.rss != null ? fmtBytes(w.rss) : "–"],
    ["Restarts", String(w.restarts ?? 0)],
  ];
  if (w.last_exit) {
    facts.push(["Last exit", `${w.last_exit.by === "crash" ? "crashed" : "stopped"} with code ${w.last_exit.code} · ${fmtWhen(w.last_exit.at)}`]);
  }
  if (w.heartbeat_at && w.state === "running") facts.push(["Last sign of life", `${fmtUptime(now - w.heartbeat_at)} ago`]);
  const info = w.info || {};
  if (w.name === "jobs" && info.running_runs != null) facts.push(["DAG runs", `${info.running_runs} running · ${info.queued_runs ?? 0} queued`]);
  if (w.name === "network" && "watching" in info) {
    facts.push(["Watching", info.watching ? `every ${info.interval_min} min` : "off (Network → Settings)"]);
    if (info.last_scan) facts.push(["Last scan", fmtWhen(info.last_scan)]);
  }
  if (w.name === "names" && "names" in info) {
    facts.push([".local names", info.avahi === false ? "avahi-publish isn't installed" : `${info.announced} of ${info.names} announced`]);
    facts.push(["Websites", String(info.websites)]);
    facts.push(["Proxy", PROXY_STATE[info.proxy] || info.proxy || "–"]);
  }
  const act = async (action, btn) => {
    if (action === "stop" && !confirm(`Stop the ${w.label.toLowerCase()}?\n\n${PROC_IMPACT[w.name] || ""}\n\nIt stays stopped until you start it again.`)) return;
    if (w.name === "web" && action === "restart" && !confirm("Restart the web interface? This page reconnects in a few seconds.")) return;
    btn.disabled = true;
    try {
      await api(`/api/processes/${w.name}/${action}`, { method: "POST" });
      toast({ start: `${w.label} started.`, stop: `${w.label} stopped.`, restart: `${w.label} restarted.` }[action]);
    } catch (ex) {
      if (w.name === "web" && action === "restart") { waitForWeb(); return; }
      toast(ex.message, 6000);
    }
    if (w.name === "web" && action === "restart") { waitForWeb(); return; }
    loadProcesses();
  };
  const logs = el("details", { class: "proc-logs", "data-name": w.name, open: logsOpen || null },
    el("summary", { class: "small" }, "Recent output"), el("pre", { class: "update-notes mono small" }, "Loading…"));
  const fillLogs = async () => {
    const pre = logs.querySelector("pre");
    try {
      const lines = await api(`/api/processes/${w.name}/logs?lines=300`);
      pre.textContent = lines.length ? lines.map((l) => `${new Date(l.t * 1000).toLocaleTimeString()}  ${l.line}`).join("\n")
        : (supervised ? "No output yet." : "Output goes to the terminal or the service log in development mode.");
      pre.scrollTop = pre.scrollHeight;
    } catch (ex) { pre.textContent = ex.message; }
  };
  logs.addEventListener("toggle", () => { if (logs.open) fillLogs(); });
  if (logsOpen) fillLogs();
  const running = w.state === "running";
  return el("div", { class: `card proc-card ${cls}` },
    el("div", { class: "ai-card-head" },
      el("span", { class: `icon-tile ${running && w.responding !== false ? "accent" : ""}`, html: icon(w.icon || "chip") }),
      el("div", { class: "ai-card-title" },
        el("div", {}, el("strong", {}, w.label), " ", el("span", { class: `status-pill ${cls}` }, label),
          w.desired === "stopped" && w.state === "stopped" ? el("span", { class: "muted small" }, " · stopped on purpose") : null),
        el("div", { class: "muted small" }, w.description)),
      el("div", { class: "ai-actions" },
        !running ? el("button", { type: "button", class: "small primary", disabled: !supervised, onclick: (e) => act("start", e.currentTarget) }, withIcon("play", "Start")) : null,
        running && w.name !== "web" ? el("button", { type: "button", class: "small ghost danger", disabled: !supervised,
          onclick: (e) => act("stop", e.currentTarget) }, withIcon("stop", "Stop")) : null,
        running && w.name === "web" ? el("button", { type: "button", class: "small ghost", disabled: true,
          title: "Stopping the web interface would leave no page to start it again. Use: sudo sysadmin-ia process stop web" }, withIcon("stop", "Stop")) : null,
        el("button", { type: "button", class: "small", disabled: !supervised, onclick: (e) => act("restart", e.currentTarget) }, withIcon("refresh", "Restart")))),
    el("dl", { class: "proc-facts small" }, ...facts.flatMap(([k, v]) => [el("dt", { class: "muted" }, k), el("dd", { "data-no-i18n": k === "Process id" ? "" : null }, String(v))])),
    w.state === "failed" ? el("p", { class: "small error" }, "It crashed several times in a row, so it isn't restarted automatically. Look at its output below, then start it.") : null,
    logs);
}

// After restarting the web interface (or everything): wait until it answers again, then reload.
function waitForWeb(tries = 0) {
  toast("Reconnecting…", 4000);
  setTimeout(async () => {
    try {
      const r = await fetch("/api/version", { cache: "no-store" });
      if (r.ok) { location.reload(); return; }
    } catch { /* still restarting */ }
    if (tries < 60) waitForWeb(tries + 1);
  }, tries ? 2000 : 3000);
}

$("#proc-restart-all").addEventListener("click", async () => {
  if (!confirm("Restart every process of SysAdmin.IA?\n\nThe page reconnects when it's back (a few seconds); running DAG runs are interrupted.")) return;
  try { await api("/api/processes/restart-all", { method: "POST" }); } catch { /* it may go down before answering */ }
  waitForWeb();
});

// ---- "this module isn't working" notice + the red dot on Processes
async function procSummary(force = false) {
  if (!force && state.procSummary && Date.now() - state.procSummary.at < 5000) return state.procSummary.data;
  try {
    const data = await api("/api/processes/summary");
    state.procSummary = { at: Date.now(), data };
    return data;
  } catch { return null; }
}

async function checkProcBanner(view) {
  const b = $("#proc-banner");
  const mod = viewModule(view);
  if (!mod) { b.hidden = true; return; }
  const s = await procSummary();
  if (state.view !== view) return;
  const proc = s && (s.modules || {})[mod];
  const w = proc && s.workers[proc];
  if (!w || w.ok) { b.hidden = true; return; }
  b.innerHTML = "";
  b.append(el("span", { class: "proc-banner-icon", html: icon("bolt") }),
    el("div", {}, el("strong", {}, `${w.label} isn't working (${w.state}).`), " ", PROC_IMPACT[proc] || "",
      el("div", { class: "small" }, s.is_admin
        ? el("button", { type: "button", class: "small", onclick: () => switchView("processes") }, "Open Processes")
        : "Please contact an administrator.")));
  b.hidden = false;
}

async function refreshProcIndicators() {
  if (!state.me) return;
  const s = await procSummary(true);
  const bad = s && Object.values(s.workers || {}).some((w) => !w.ok);
  const dot = $("#proc-nav-alert");
  if (dot) dot.hidden = !bad;
  if (state.view) checkProcBanner(state.view);
}
setInterval(() => { if (state.me && !document.hidden) refreshProcIndicators(); }, 20000);
setTimeout(() => refreshProcIndicators(), 3000);
window.loadProcesses = loadProcesses;
window.checkProcBanner = checkProcBanner;
