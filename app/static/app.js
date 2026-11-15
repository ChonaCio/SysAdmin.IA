"use strict";
/* SysAdmin.IA – frontend (no build step, no external dependencies). */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const md = window.renderMarkdown;
const esc = window.escapeHtml;

const state = {
  me: null,
  convId: null,
  ws: null,
  busy: false,
  currentAssistant: null,  // element receiving streamed text
  currentThinking: null,
  toolCards: new Map(),    // tool_use id -> card element
  history: { cpu: [], mem: [], rx: [], tx: [] },
  procSort: "cpu",
  view: "chat",
};

// ------------------------------------------------------------ helpers
async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", "X-SAI-CSRF": "1", ...(opts.headers || {}) },
  });
  if (r.status === 401) { location.href = "/login"; throw new Error("unauthenticated"); }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

function fmtBytes(n, perSec = false) {
  if (n == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${u[i]}${perSec ? "/s" : ""}`;
}
function fmtDuration(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h ${m}m` : h ? `${h}h ${m}m` : `${m}m`;
}
function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}
function level(p) { return p >= 90 ? "crit" : p >= 75 ? "warn" : "ok"; }
function setBar(el, pct) { el.style.width = `${Math.min(100, pct)}%`; el.className = level(pct); }

// ------------------------------------------------------------ boot
async function boot() {
  state.me = await api("/api/me");
  $("#whoami").textContent = `${state.me.username} (${state.me.role})`;
  if (!state.me.api_key_configured) $("#api-warning").hidden = false;
  if (!state.me.allow_auto_approve) $("#auto-approve-wrap").hidden = true;
  if (state.me.role !== "admin") {
    $("#tab-chat").hidden = true;
    $$(".admin-only").forEach((e) => (e.hidden = true));
    switchView("monitor");
  } else {
    connectWs();
    loadConversations();
  }
  api("/api/host").then((h) => {
    $("#hostname").textContent = h.hostname;
    $("#host-os").textContent = `${h.os} · ${h.arch}`;
    document.title = `${h.hostname} · SysAdmin.IA`;
  });
  startMonitor();
}

$$(".tab").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
function switchView(v) {
  state.view = v;
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach((s) => s.classList.toggle("active", s.id === `view-${v}`));
  if (v === "admin") loadAdmin();
  if (v === "monitor") { drawCharts(); refreshProcs(); }
}

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  location.href = "/login";
});

// ------------------------------------------------------------ websocket
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/chat`);
  state.ws = ws;
  ws.onopen = () => {
    $("#conn-dot").className = "dot on";
    ws.send(JSON.stringify({ type: "set_auto_approve", value: $("#auto-approve").checked }));
  };
  ws.onclose = (e) => {
    $("#conn-dot").className = "dot off";
    if (e.code === 4401) { location.href = "/login"; return; }
    setBusy(false);
    setTimeout(connectWs, 2000);
  };
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
  clearInterval(connectWs._ping);
  connectWs._ping = setInterval(() => ws.readyState === 1 && ws.send('{"type":"ping"}'), 25000);
}
function wsSend(obj) {
  if (!state.ws || state.ws.readyState !== 1) { toast("Not connected – retrying…"); return false; }
  state.ws.send(JSON.stringify(obj));
  return true;
}

function handleEvent(ev) {
  switch (ev.type) {
    case "conversation":
      state.convId = ev.id;
      loadConversations();
      break;
    case "assistant_start":
      state.currentAssistant = null;
      state.currentThinking = null;
      setStatus("Claude is thinking…");
      break;
    case "thinking_start":
      state.currentThinking = addThinking("");
      break;
    case "thinking_delta":
      if (!state.currentThinking) state.currentThinking = addThinking("");
      state.currentThinking._raw += ev.text;
      $(".thinking-body", state.currentThinking).textContent = state.currentThinking._raw;
      scrollDown();
      break;
    case "text_start":
      state.currentAssistant = addMessage("assistant", "");
      setStatus("Claude is responding…");
      break;
    case "text_delta":
      if (!state.currentAssistant) state.currentAssistant = addMessage("assistant", "");
      state.currentAssistant._raw += ev.text;
      $(".content", state.currentAssistant).innerHTML = md(state.currentAssistant._raw);
      scrollDown();
      break;
    case "tool_preparing":
      setStatus(`Claude is preparing a ${ev.name === "bash" ? "command" : ev.name === "restart_app" ? "restart" : "file operation"}…`);
      break;
    case "tool_request":
      state.currentAssistant = null;
      addToolCard(ev);
      setStatus(ev.needs_approval ? "Waiting for your approval…" : "Running…");
      break;
    case "tool_result":
      setToolResult(ev.id, ev.output, ev.is_error);
      setStatus("Claude is thinking…");
      break;
    case "notice":
      addNotice(ev.message);
      break;
    case "error":
      addNotice(ev.message, true);
      break;
    case "restarting":
      addNotice(`App is restarting: ${ev.reason || "backend updated"}. Reconnecting…`);
      setTimeout(() => location.reload(), 5000);
      break;
    case "auto_approve":
      $("#auto-approve").checked = ev.value;
      break;
    case "done":
      setBusy(false);
      state.currentAssistant = null;
      loadConversations();
      break;
  }
}

// ------------------------------------------------------------ chat rendering
const messagesEl = $("#messages");
const emptyState = $("#empty-state");
function scrollDown() {
  const nearBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 200;
  if (nearBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
}
function hideEmpty() { emptyState.hidden = true; }

function addMessage(role, text) {
  hideEmpty();
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = `<div class="who">${role === "user" ? "You" : "Claude"}</div><div class="content"></div>`;
  el._raw = text;
  $(".content", el).innerHTML = role === "user" ? `<p>${esc(text).replace(/\n/g, "<br>")}</p>` : md(text);
  messagesEl.appendChild(el);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return el;
}
function addThinking(text) {
  hideEmpty();
  const el = document.createElement("details");
  el.className = "thinking";
  el.innerHTML = `<summary>Reasoning</summary><div class="thinking-body"></div>`;
  el._raw = text;
  $(".thinking-body", el).textContent = text;
  messagesEl.appendChild(el);
  return el;
}
function addNotice(text, isError = false) {
  hideEmpty();
  const el = document.createElement("div");
  el.className = `notice ${isError ? "error" : ""}`;
  el.textContent = text;
  messagesEl.appendChild(el);
  scrollDown();
}

function describeTool(name, input) {
  if (name === "bash") return input.restart ? { title: "Restart shell session", body: "" } : { title: "Run command", body: input.command, editable: true };
  if (name === "restart_app") return { title: "Restart this app", body: input.reason || "" };
  const c = input.command;
  const p = input.path;
  if (c === "view") return { title: `View ${p}`, body: input.view_range ? `lines ${input.view_range.join("–")}` : "" };
  if (c === "create") return { title: `Write file ${p}`, body: input.file_text || "" };
  if (c === "str_replace") return { title: `Edit ${p}`, diff: [input.old_str || "", input.new_str || ""] };
  if (c === "insert") return { title: `Insert into ${p} after line ${input.insert_line}`, body: input.insert_text || "" };
  return { title: `${name}`, body: JSON.stringify(input, null, 2) };
}

function addToolCard(ev, historical = false) {
  hideEmpty();
  const d = describeTool(ev.name, ev.input || {});
  const card = document.createElement("div");
  card.className = "tool-card";
  card.dataset.id = ev.id;
  let bodyHtml = "";
  if (d.diff) {
    bodyHtml = `<div class="diff"><pre class="del">${esc(d.diff[0])}</pre><pre class="add">${esc(d.diff[1])}</pre></div>`;
  } else if (d.body) {
    bodyHtml = d.editable && ev.needs_approval && !historical
      ? `<textarea class="cmd-edit" spellcheck="false">${esc(d.body)}</textarea>`
      : `<pre class="code">${esc(d.body)}</pre>`;
  }
  card.innerHTML = `
    <div class="tool-head"><span class="tool-icon">${ev.name === "bash" ? "$" : ev.name === "restart_app" ? "⟳" : "✎"}</span>
      <span class="tool-title">${esc(d.title)}</span><span class="tool-state muted small"></span></div>
    ${bodyHtml}
    <div class="tool-actions" hidden>
      <input class="note" placeholder="Optional note to Claude">
      <button class="danger deny">Deny</button>
      <button class="primary approve">Approve &amp; run</button>
    </div>
    <details class="tool-output" hidden><summary>Output</summary><pre class="code out"></pre></details>`;
  messagesEl.appendChild(card);
  state.toolCards.set(ev.id, card);

  if (ev.needs_approval && !historical) {
    const actions = $(".tool-actions", card);
    actions.hidden = false;
    $(".tool-state", card).textContent = "awaiting approval";
    card.classList.add("pending");
    const decide = (approved) => {
      const edited = $(".cmd-edit", card);
      const ok = wsSend({ type: "approve", id: ev.id, approved, note: $(".note", card).value, command: edited ? edited.value : undefined });
      if (!ok) return;
      actions.hidden = true;
      card.classList.remove("pending");
      $(".tool-state", card).textContent = approved ? "running…" : "denied";
      if (edited) { const pre = document.createElement("pre"); pre.className = "code"; pre.textContent = edited.value; edited.replaceWith(pre); }
    };
    $(".approve", card).addEventListener("click", () => decide(true));
    $(".deny", card).addEventListener("click", () => decide(false));
  } else if (!historical) {
    $(".tool-state", card).textContent = "running…";
  }
  scrollDown();
  return card;
}

function setToolResult(id, output, isError) {
  const card = state.toolCards.get(id);
  if (!card) return;
  $(".tool-actions", card).hidden = true;
  card.classList.remove("pending");
  card.classList.toggle("errored", !!isError);
  $(".tool-state", card).textContent = isError ? "error" : "done";
  const out = $(".tool-output", card);
  out.hidden = false;
  $(".out", out).textContent = output || "(no output)";
  if (isError || (output || "").length < 1500) out.open = true;
  scrollDown();
}

function setStatus(s) { $("#status").textContent = s; }
function setBusy(b) {
  state.busy = b;
  $("#send").hidden = b;
  $("#stop").hidden = !b;
  if (!b) setStatus("");
}

// ------------------------------------------------------------ composer
const input = $("#input");
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); sendMessage(); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(); }
});
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 240) + "px"; });
$("#stop").addEventListener("click", () => { wsSend({ type: "stop" }); setStatus("Stopping…"); });
$("#auto-approve").addEventListener("change", (e) => {
  if (e.target.checked && !confirm("Auto-approve lets Claude run commands and edit files on this server without asking. Enable for this session?")) {
    e.target.checked = false;
    return;
  }
  wsSend({ type: "set_auto_approve", value: e.target.checked });
});
$$(".chip").forEach((c) => c.addEventListener("click", () => { input.value = c.textContent; sendMessage(); }));

function sendMessage() {
  const text = input.value.trim();
  if (!text || state.busy) return;
  if (!wsSend({ type: "chat", conversation_id: state.convId, text })) return;
  addMessage("user", text);
  input.value = "";
  input.style.height = "auto";
  setBusy(true);
  setStatus("Sending…");
}

// ------------------------------------------------------------ conversations
async function loadConversations() {
  const list = await api("/api/conversations").catch(() => []);
  const ul = $("#conv-list");
  ul.innerHTML = "";
  for (const c of list) {
    const li = document.createElement("li");
    li.className = c.id === state.convId ? "active" : "";
    li.innerHTML = `<span class="t"></span><button class="ghost small del" title="Delete">✕</button>`;
    $(".t", li).textContent = c.title;
    li.addEventListener("click", () => openConversation(c.id));
    $(".del", li).addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete "${c.title}"?`)) return;
      await api(`/api/conversations/${c.id}`, { method: "DELETE" });
      if (state.convId === c.id) newChat();
      loadConversations();
    });
    ul.appendChild(li);
  }
}

function clearMessages() {
  messagesEl.innerHTML = "";
  state.toolCards.clear();
}

function newChat() {
  if (state.busy) { toast("Stop the current task first."); return; }
  state.convId = null;
  clearMessages();
  emptyState.hidden = false;
  messagesEl.appendChild(emptyState);
  $$("#conv-list li").forEach((li) => li.classList.remove("active"));
  input.focus();
}
$("#new-chat").addEventListener("click", newChat);

async function openConversation(id) {
  if (state.busy) { toast("Stop the current task first."); return; }
  const conv = await api(`/api/conversations/${id}`);
  state.convId = id;
  clearMessages();
  for (const it of conv.items) {
    if (it.kind === "text" && it.role === "user") addMessage("user", it.text);
    else if (it.kind === "text") addMessage("assistant", it.text);
    else if (it.kind === "thinking") addThinking(it.text);
    else if (it.kind === "tool_use") addToolCard({ id: it.id, name: it.name, input: it.input, needs_approval: false }, true);
    else if (it.kind === "tool_result") setToolResult(it.id, it.output, it.is_error);
  }
  $$("#conv-list li").forEach((li) => li.classList.remove("active"));
  loadConversations();
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ------------------------------------------------------------ monitor
const MAX_POINTS = 150; // 5 min at 2 s
async function pollStats() {
  try {
    const s = await api("/api/stats");
    const h = state.history;
    const push = (arr, v) => { arr.push(v); if (arr.length > MAX_POINTS) arr.shift(); };
    push(h.cpu, s.cpu_percent); push(h.mem, s.mem.percent); push(h.rx, s.net_rx_bps); push(h.tx, s.net_tx_bps);

    $("#m-cpu").textContent = `${s.cpu_percent.toFixed(0)}%`; setBar($("#b-cpu"), s.cpu_percent);
    $("#m-mem").textContent = `${s.mem.percent.toFixed(0)}%`; setBar($("#b-mem"), s.mem.percent);
    $("#m-mem-sub").textContent = `${fmtBytes(s.mem.used)} / ${fmtBytes(s.mem.total)}`;
    $("#m-swap").textContent = s.swap.total ? `${s.swap.percent.toFixed(0)}%` : "off"; setBar($("#b-swap"), s.swap.percent);
    $("#m-swap-sub").textContent = s.swap.total ? `${fmtBytes(s.swap.used)} / ${fmtBytes(s.swap.total)}` : "";
    $("#m-load").textContent = s.load.map((x) => x.toFixed(2)).join(" ");
    $("#m-cores").textContent = `${s.cpu_per_core.length} cores`;
    $("#m-net").innerHTML = `↓ ${fmtBytes(s.net_rx_bps, true)}<br>↑ ${fmtBytes(s.net_tx_bps, true)}`;
    $("#m-io").innerHTML = `R ${fmtBytes(s.disk_read_bps, true)}<br>W ${fmtBytes(s.disk_write_bps, true)}`;
    $("#m-uptime").textContent = fmtDuration(s.uptime);
    $("#m-procs").textContent = `${s.process_count} processes`;

    if (state.view === "monitor") {
      $("#cores").innerHTML = s.cpu_per_core.map((p, i) =>
        `<div class="core"><span class="muted small">${i}</span><div class="bar"><i class="${level(p)}" style="width:${p}%"></i></div><span class="small num">${p.toFixed(0)}%</span></div>`).join("");
      $("#disks").innerHTML = s.disks.map((d) =>
        `<tr><td>${esc(d.mount)}</td><td class="muted">${esc(d.fstype)}</td><td>${fmtBytes(d.used)}</td><td>${fmtBytes(d.total)}</td>
         <td><div class="bar"><i class="${level(d.percent)}" style="width:${d.percent}%"></i></div><span class="small">${d.percent.toFixed(0)}%</span></td></tr>`).join("");
      if (s.temps.length) {
        $("#temps").innerHTML = s.temps.map((t) =>
          `<div class="temp"><span>${esc(t.label)}</span><strong class="${t.high && t.current >= t.high ? "t-crit" : ""}">${t.current.toFixed(0)}°C</strong></div>`).join("");
      }
      drawCharts();
    }
  } catch (e) { /* transient */ }
}

async function pollServices() {
  try {
    const s = await api("/api/services");
    if (!s.available) { $("#m-svc").textContent = "n/a"; return; }
    $("#m-svc").textContent = `${s.running} running`;
    const f = $("#m-svc-failed");
    f.textContent = s.failed.length ? `${s.failed.length} failed: ${s.failed.slice(0, 3).join(", ")}` : "none failed";
    f.className = `sub ${s.failed.length ? "t-crit" : ""}`;
  } catch { /* ignore */ }
}

async function refreshProcs() {
  try {
    const ps = await api(`/api/processes?sort=${state.procSort}&limit=25`);
    $("#procs").innerHTML = ps.map((p) =>
      `<tr><td>${p.pid}</td><td class="muted">${esc(p.user || "")}</td><td>${esc(p.name || "")}</td><td class="num">${p.cpu.toFixed(1)}</td>
       <td class="num">${p.mem.toFixed(1)}</td><td class="num">${fmtBytes(p.rss)}</td><td class="cmd muted small" title="${esc(p.cmd)}">${esc(p.cmd)}</td></tr>`).join("");
  } catch { /* ignore */ }
}
$$(".seg-btn").forEach((b) => b.addEventListener("click", () => {
  state.procSort = b.dataset.sort;
  $$(".seg-btn").forEach((x) => x.classList.toggle("active", x === b));
  refreshProcs();
}));

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function drawLines(canvas, series, maxY, fmt) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w) return;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  const pad = { l: 52, r: 8, t: 8, b: 8 };
  const iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
  ctx.strokeStyle = cssVar("--grid"); ctx.fillStyle = cssVar("--muted"); ctx.lineWidth = 1;
  ctx.font = "11px system-ui, sans-serif"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + ih - (ih * i) / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.fillText(fmt((maxY * i) / 4), pad.l - 6, y);
  }
  series.forEach(({ data, color }) => {
    if (data.length < 2) return;
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = "round";
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = pad.l + iw - ((data.length - 1 - i) / (MAX_POINTS - 1)) * iw;
      const y = pad.t + ih - (Math.min(v, maxY) / maxY) * ih;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
  });
}
function drawCharts() {
  const h = state.history;
  const c1 = cssVar("--series-1"), c2 = cssVar("--series-2");
  drawLines($("#chart-usage"), [{ data: h.cpu, color: c1 }, { data: h.mem, color: c2 }], 100, (v) => `${v.toFixed(0)}%`);
  const peak = Math.max(1024, ...h.rx, ...h.tx) * 1.15;
  drawLines($("#chart-net"), [{ data: h.rx, color: c1 }, { data: h.tx, color: c2 }], peak, (v) => fmtBytes(v, true));
}
window.addEventListener("resize", () => state.view === "monitor" && drawCharts());

function startMonitor() {
  pollStats(); pollServices();
  setInterval(pollStats, 2000);
  setInterval(pollServices, 30000);
  setInterval(() => state.view === "monitor" && refreshProcs(), 5000);
}

// ------------------------------------------------------------ admin
// ------------------------------------------------------------ Claude API settings
function setClaudeMsg(text, ok) {
  const m = $("#claude-msg");
  m.textContent = text;
  m.className = `small ${ok ? "ok-text" : "error"}`;
  m.hidden = !text;
}

function renderClaudeSettings(s) {
  const form = $("#claude-form");
  const pill = $("#key-status");
  pill.textContent = s.api_key_set ? "connected" : "not configured";
  pill.className = `status-pill ${s.api_key_set ? "ok" : "warn"}`;
  form.api_key.value = "";
  form.api_key.placeholder = s.api_key_set
    ? `${s.api_key_hint} (saved${s.api_key_source === "config" ? " in config file" : ""}) — type to replace`
    : "sk-ant-…";
  $("#clear-key").hidden = s.api_key_source !== "web";
  $("#key-help").textContent = s.api_key_source === "config"
    ? "Currently using the key from the server config file. A key saved here takes precedence."
    : "Get a key at console.anthropic.com. It is stored on this server and never shown again.";
  form.model.value = s.model;
  $("#model-options").innerHTML = s.suggested_models.map((m) => `<option value="${esc(m)}">`).join("");
  form.effort.innerHTML = s.effort_levels.map((e) => `<option value="${e}">${e}</option>`).join("");
  form.effort.value = s.effort;
  state.me.api_key_configured = s.api_key_set;
  $("#api-warning").hidden = s.api_key_set;
}

async function loadClaudeSettings() {
  renderClaudeSettings(await api("/api/claude-settings"));
}

$("#claude-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = { model: f.model.value.trim(), effort: f.effort.value };
  if (f.api_key.value.trim()) body.api_key = f.api_key.value.trim();
  try {
    renderClaudeSettings(await api("/api/claude-settings", { method: "PUT", body: JSON.stringify(body) }));
    setClaudeMsg("Saved.", true);
  } catch (ex) { setClaudeMsg(ex.message, false); }
});

$("#test-key").addEventListener("click", async () => {
  const typed = $("#claude-form").api_key.value.trim();
  setClaudeMsg("Testing…", true);
  try {
    const r = await api("/api/claude-settings/test", { method: "POST", body: JSON.stringify({ api_key: typed || null }) });
    setClaudeMsg(r.message + (r.ok && typed ? " Click Save to keep it." : ""), r.ok);
  } catch (ex) { setClaudeMsg(ex.message, false); }
});

$("#clear-key").addEventListener("click", async () => {
  if (!confirm("Remove the API key saved in the web UI?")) return;
  try {
    renderClaudeSettings(await api("/api/claude-settings", { method: "PUT", body: JSON.stringify({ clear_api_key: true }) }));
    setClaudeMsg("Key removed.", true);
  } catch (ex) { setClaudeMsg(ex.message, false); }
});

$("#goto-claude-settings").addEventListener("click", (e) => {
  e.preventDefault();
  switchView("admin");
  $("#claude-card").scrollIntoView({ behavior: "smooth" });
  $("#claude-form").api_key.focus();
});

// ------------------------------------------------------------ server name
function setSnMsg(text, ok) {
  const m = $("#sn-msg");
  m.textContent = text;
  m.className = `small ${ok ? "ok-text" : "error"}`;
  m.hidden = !text;
}

function renderServerName(s) {
  const form = $("#sn-form");
  form.server_name.value = s.name;
  const pill = $("#sn-status");
  const labels = { active: ["reachable as " + s.mdns_name, "ok"], installed: ["mDNS not running", "warn"], missing: ["mDNS not installed", "warn"] };
  const [text, cls] = labels[s.mdns] || ["", ""];
  pill.textContent = text; pill.className = `status-pill ${cls}`;
  const urls = $("#sn-urls");
  urls.innerHTML = "";
  if (s.urls.length) {
    urls.append("Open from another computer on the LAN: ");
    s.urls.forEach((u, i) => {
      const a = document.createElement("a");
      a.href = u; a.textContent = u; a.target = "_blank"; a.rel = "noopener noreferrer";
      if (i) urls.append(" · ");
      urls.append(a);
    });
  }
  if (s.mdns !== "active") {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = s.mdns === "installed"
      ? "The .local name needs the avahi-daemon service, which isn't running right now."
      : "The .local name needs the avahi-daemon service; installing the next update (Admin → Updates) sets it up.";
    urls.append(p);
  }
  form.server_name.disabled = $("#sn-save").disabled = $("#sn-reboot").disabled = !s.can_change;
  const setup = $("#sn-setup");
  setup.hidden = !s.setup_command;
  if (s.setup_command) {
    setup.textContent = s.setup_command;
    setSnMsg("One-time setup needed: run the command above on the server as root, then reload this page.", false);
  } else if (!s.can_change) {
    setSnMsg(s.linux ? "Renaming isn't available on this installation."
                     : "Renaming is only available on an installed Linux server.", false);
  }
}

$("#sn-reboot").addEventListener("click", async () => {
  if (!confirm("Reboot the server now?\n\nThe web UI, and everything else running on this server, " +
      "will be unavailable for a minute or two.")) return;
  try {
    const before = (await api("/api/host")).boot_time;
    await api("/api/server/reboot", { method: "POST" });
    setSnMsg("Rebooting… this page reloads when the server is back.", true);
    const started = Date.now();
    const poll = async () => {
      try {
        const r = await fetch("/api/host", { cache: "no-store" });
        // Reload once the server answers with a new boot time.
        if (r.ok && (await r.json()).boot_time > before + 5) { location.reload(); return; }
      } catch { /* still down */ }
      if (Date.now() - started < 10 * 60000) setTimeout(poll, 3000);
      else setSnMsg("The server hasn't come back after 10 minutes. Check it directly.", false);
    };
    setTimeout(poll, 8000);
  } catch (ex) { setSnMsg(ex.message, false); }
});

async function loadServerName() {
  renderServerName(await api("/api/server-name"));
}

$("#sn-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = e.target.server_name.value.trim().toLowerCase().replace(/\.local$/, "");
  if (!confirm(`Rename this server to "${name}"?\n\nIt will be reachable on the LAN as ${name}.local. ` +
      "Other services that rely on the current hostname may be affected.")) return;
  setSnMsg("Renaming…", true);
  $("#sn-save").disabled = true;
  try {
    const r = await api("/api/server-name", { method: "PUT", body: JSON.stringify({ name }) });
    renderServerName(r.status);
    const out = $("#sn-output");
    out.textContent = r.output; out.hidden = !r.output;
    $("#hostname").textContent = r.name;
    document.title = `${r.name} · SysAdmin.IA`;
    if (r.restarting) {
      setSnMsg("Renamed. Restarting with the new certificate…", true);
      const next = `${location.protocol}//${r.name}.local${location.port ? ":" + location.port : ""}/`;
      setTimeout(() => setSnMsg(`Renamed. If this page stops responding, open ${next}`, true), 4000);
    } else {
      setSnMsg(`Renamed to ${r.name}.`, true);
    }
  } catch (ex) { setSnMsg(ex.message, false); }
  finally { $("#sn-save").disabled = false; }
});

// ------------------------------------------------------------ updates
function setUpdateMsg(text, ok) {
  const m = $("#update-msg");
  m.textContent = text;
  m.className = `small ${ok ? "ok-text" : "error"}`;
  m.hidden = !text;
}

function renderUpdate(s) {
  $("#update-current").textContent = s.current;
  const repo = $("#update-repo");
  repo.textContent = `github.com/${s.repo}`;
  repo.href = `https://github.com/${s.repo}`;
  const pill = $("#update-status");
  const latest = s.latest;
  $("#update-latest").hidden = !latest;
  if (latest) {
    $("#update-latest-version").textContent = latest.version;
    const when = latest.published_at ? new Date(latest.published_at).toLocaleString() : "";
    $("#update-latest-meta").textContent = `(${latest.source === "release" ? "release " + latest.name : "branch " + s.branch}${when ? ", " + when : ""})`;
    const notes = $("#update-notes");
    notes.textContent = latest.notes || "";
    notes.hidden = !latest.notes || !s.update_available;
  }
  if (s.error) { pill.textContent = "check failed"; pill.className = "status-pill warn"; }
  else if (!s.checked_at) { pill.textContent = ""; pill.className = "status-pill"; }
  else if (s.update_available) { pill.textContent = "update available"; pill.className = "status-pill warn"; }
  else { pill.textContent = "up to date"; pill.className = "status-pill ok"; }
  $("#update-apply").hidden = !s.update_available || !s.can_apply;
  if (s.error) setUpdateMsg(s.error, false);
  else if (s.update_available && !s.can_apply) setUpdateMsg("This app runs from a git clone: update it with `git pull`.", false);
  else setUpdateMsg("", true);
  $("#update-clear-token").hidden = s.token_source !== "web";
  $("#update-token-form").github_token.placeholder = s.token_set
    ? `saved${s.token_source === "config" ? " in config file" : ""} — type to replace`
    : "github_pat_…";
  const mode = $("#update-mode");
  mode.hidden = !s.setup_command;
  if (s.setup_command) {
    mode.textContent = "Updates currently replace only the app code. To let them also install system packages, " +
      `the service and helpers (like install.sh does), run this once on the server as root: ${s.setup_command}`;
  }
  state.update = s;
}

async function loadUpdate() {
  const s = await api("/api/update");
  renderUpdate(s);
  if (s.progress && s.progress.state === "running") followUpgrade(s.progress.version);
  // Check GitHub automatically (the server caches the answer for an hour).
  if (!s.checked_at) renderUpdate(await api("/api/update/check?force=false", { method: "POST" }));
}

$("#update-check").addEventListener("click", async () => {
  setUpdateMsg("Checking GitHub…", true);
  try { renderUpdate(await api("/api/update/check", { method: "POST" })); }
  catch (ex) { setUpdateMsg(ex.message, false); }
});

$("#update-apply").addEventListener("click", async () => {
  const s = state.update;
  if (!confirm(`Update SysAdmin.IA from ${s.current} to ${s.latest.version}?\n\n` +
      (s.full_upgrade ? "The new version's installer runs exactly as a fresh install would (system packages, " +
        "service, helpers, app code); your settings, users and data are kept. " : "") +
      "The current code is backed up first and restored automatically if the update fails. " +
      "Changes Claude made to this app's own files may be overwritten (they stay in the app's git history).")) return;
  const btn = $("#update-apply");
  btn.disabled = true; $("#update-check").disabled = true;
  setUpdateMsg("Downloading and installing… this can take a few minutes.", true);
  try {
    const r = await api("/api/update/apply", { method: "POST" });
    if (r.mode === "full") {
      followUpgrade(r.to);
      return;  // buttons stay disabled until the page reloads
    }
    if (r.restart) {
      setUpdateMsg(`Updated to ${r.to}. Restarting…`, true);
      waitForVersion(r.to);
    } else {
      setUpdateMsg(`Updated to ${r.to}. Restart the app to load the new version.`, true);
    }
    btn.disabled = false; $("#update-check").disabled = false;
  } catch (ex) {
    setUpdateMsg(ex.message, false);
    btn.disabled = false; $("#update-check").disabled = false;
  }
});

// Follow a full upgrade (install.sh running as root in the background). The app restarts
// during it, so failed requests are expected for a few seconds.
function followUpgrade(target) {
  const logEl = $("#update-log");
  logEl.hidden = false;
  $("#update-apply").disabled = $("#update-check").disabled = true;
  const started = Date.now();
  const poll = async () => {
    try {
      const r = await fetch("/api/update/progress", { cache: "no-store", headers: { "X-SAI-CSRF": "1" } });
      if (r.status === 401) { location.href = "/login"; return; }
      if (r.ok) {
        const p = await r.json();
        const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20;
        logEl.textContent = p.log || "";
        if (atBottom) logEl.scrollTop = logEl.scrollHeight;
        if (p.state === "done" && p.current === target) {
          setUpdateMsg(`${p.message}. Reloading…`, true);
          setTimeout(() => location.reload(), 1500);
          return;
        }
        if (p.state === "failed") {
          setUpdateMsg(p.message || "The update failed.", false);
          $("#update-apply").disabled = $("#update-check").disabled = false;
          return;
        }
        setUpdateMsg(`Updating to ${target}: ${p.message || "working"}…`, true);
      }
    } catch { /* app restarting */ }
    if (Date.now() - started < 30 * 60000) setTimeout(poll, 2000);
    else setUpdateMsg("The update is taking unusually long; check /var/log/sysadmin-ia/upgrade.log on the server.", false);
  };
  poll();
}

function waitForVersion(target, tries = 0) {
  setTimeout(async () => {
    try {
      const r = await fetch("/api/version", { cache: "no-store" });
      if (r.ok && (await r.json()).version === target) { location.reload(); return; }
    } catch { /* still restarting */ }
    if (tries < 60) waitForVersion(target, tries + 1);
    else setUpdateMsg("The app did not come back after the update; check the service logs.", false);
  }, 2000);
}

$("#update-token-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const value = e.target.github_token.value.trim();
  if (!value) return;
  try {
    renderUpdate(await api("/api/update/settings", { method: "PUT", body: JSON.stringify({ github_token: value }) }));
    e.target.github_token.value = "";
    setUpdateMsg("Token saved. Checking…", true);
    renderUpdate(await api("/api/update/check", { method: "POST" }));
  } catch (ex) { setUpdateMsg(ex.message, false); }
});

$("#update-clear-token").addEventListener("click", async () => {
  if (!confirm("Remove the GitHub token saved in the web UI?")) return;
  try {
    renderUpdate(await api("/api/update/settings", { method: "PUT", body: JSON.stringify({ clear_github_token: true }) }));
    setUpdateMsg("Token removed.", true);
  } catch (ex) { setUpdateMsg(ex.message, false); }
});

async function loadAdmin() {
  if (state.me.role !== "admin") return;
  loadClaudeSettings().catch((e) => setClaudeMsg(e.message, false));
  loadUpdate().catch((e) => setUpdateMsg(e.message, false));
  loadServerName().catch((e) => setSnMsg(e.message, false));
  const users = await api("/api/users");
  $("#users").innerHTML = "";
  for (const u of users) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td></td><td class="muted"></td><td class="num"></td>`;
    tr.children[0].textContent = u.username;
    tr.children[1].textContent = u.role;
    if (u.username !== state.me.username) {
      const b = document.createElement("button");
      b.className = "ghost small"; b.textContent = "Delete";
      b.addEventListener("click", async () => {
        if (!confirm(`Delete user ${u.username}?`)) return;
        await api(`/api/users/${u.id}`, { method: "DELETE" }).catch((e) => toast(e.message));
        loadAdmin();
      });
      tr.children[2].appendChild(b);
    }
    $("#users").appendChild(tr);
  }
  loadAudit();
}
async function loadAudit() {
  const rows = await api("/api/audit?limit=200").catch(() => []);
  $("#audit").innerHTML = rows.map((r) =>
    `<tr><td class="small nowrap">${new Date(r.ts * 1000).toLocaleString()}</td><td>${esc(r.username || "")}</td><td class="muted small">${esc(r.ip || "")}</td>
     <td><span class="tag">${esc(r.action)}</span></td><td class="small mono detail">${esc((r.detail || "").slice(0, 400))}</td></tr>`).join("");
}
$("#refresh-audit").addEventListener("click", loadAudit);

$("#add-user").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#user-error");
  err.hidden = true;
  try {
    await api("/api/users", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(e.target))) });
    e.target.reset();
    loadAdmin();
  } catch (ex) { err.textContent = ex.message; err.hidden = false; }
});
$("#pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#pw-msg");
  try {
    await api("/api/me/password", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(e.target))) });
    msg.textContent = "Password updated. Please sign in again.";
    msg.className = "small ok-text"; msg.hidden = false;
    setTimeout(() => (location.href = "/login"), 1500);
  } catch (ex) { msg.textContent = ex.message; msg.className = "small error"; msg.hidden = false; }
});

boot().catch((e) => console.error(e));
