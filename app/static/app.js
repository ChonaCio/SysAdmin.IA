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

// ------------------------------------------------------------ icons
// One icon set for the whole app: 24×24 grid, 1.8px rounded strokes, drawn in currentColor.
// Use icon("name") in scripts, or <span data-icon="name"></span> in the HTML.
const ICON = {
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
  console: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M12 15h5"/>',
  monitor: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  internal: '<rect x="3" y="6" width="18" height="12" rx="2"/><circle cx="16.5" cy="12" r="1.2"/><path d="M6 12h6"/>',
  external: '<rect x="7" y="2" width="10" height="13" rx="2"/><path d="M9 15v4a3 3 0 0 0 6 0v-4M10 6h1M13 6h1"/>',
  optical: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/>',
  ai: '<path d="M12 3l1.8 4.7 4.7 1.8-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/><path d="M18 15l.8 2.2L21 18l-2.2.8L18 21l-.8-2.2L15 18l2.2-.8z"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6 6 0 0 1 3.5 6"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  "chevron-left": '<path d="M15 6l-6 6 6 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M14 6l4 4"/>',
  key: '<circle cx="8" cy="15" r="3.5"/><path d="M10.5 12.5L20 3M17 6l2 2M14.5 8.5l2 2"/>',
  logout: '<path d="M10 4H5v16h5M14 8l4 4-4 4M18 12H9"/>',
  login: '<path d="M14 4h5v16h-5M10 8l4 4-4 4M14 12H4"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  "arrow-left": '<path d="M19 12H5M11 6l-6 6 6 6"/>',
  "arrow-up": '<path d="M12 19V5M6 11l6-6 6 6"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  check: '<path d="M5 12l5 5 9-10"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  test: '<path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
  braces: '<path d="M8 4H7a2 2 0 0 0-2 2v4l-2 2 2 2v4a2 2 0 0 0 2 2h1M16 4h1a2 2 0 0 1 2 2v4l2 2-2 2v4a2 2 0 0 1-2 2h-1"/>',
  play: '<path d="M7 4l13 8-13 8z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5M12 7v5l3 2"/>',
  "arrow-down": '<path d="M12 5v14M6 13l6 6 6-6"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6z"/>',
  bolt: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  chip: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  box: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  network: '<rect x="9" y="3" width="6" height="5" rx="1"/><rect x="3" y="16" width="6" height="5" rx="1"/><rect x="15" y="16" width="6" height="5" rx="1"/><path d="M12 8v4M6 16v-4h12v4"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  router: '<rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 16.5h.01M11 16.5h.01M8 13l-2-6M16 13l2-6M9.5 6.5a4 4 0 0 1 5 0"/>',
  computer: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  phone: '<rect x="7" y="2.5" width="10" height="19" rx="2"/><path d="M11 18h2"/>',
  tv: '<rect x="2.5" y="5" width="19" height="12" rx="2"/><path d="M8 21h8M9 2l3 3 3-3"/>',
  media: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 9l5 3-5 3z"/>',
  speaker: '<rect x="6" y="2.5" width="12" height="19" rx="2"/><circle cx="12" cy="14" r="3.5"/><path d="M12 6.5h.01"/>',
  printer: '<path d="M7 8V3h10v5"/><rect x="3" y="8" width="18" height="9" rx="2"/><path d="M7 14h10v7H7z"/>',
  nas: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M4 9h16M4 15h16M8 6h.01M8 12h.01M8 18h.01"/>',
  camera: '<path d="M3 8a2 2 0 0 1 2-2h2l2-2h6l2 2h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><circle cx="12" cy="12.5" r="3.5"/>',
  iot: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9V16h7v-2.1A6 6 0 0 0 12 3z"/>',
  home: '<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-5h4v5"/>',
  console: '<path d="M6 8h12a4 4 0 0 1 3.8 5.2l-1.3 4A2.5 2.5 0 0 1 16 18l-2-2h-4l-2 2a2.5 2.5 0 0 1-4.5-.8l-1.3-4A4 4 0 0 1 6 8z"/><path d="M7 11v3M5.5 12.5h3M15.5 12h.01M17.5 13.5h.01"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  question: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6V14M12 17h.01"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  palette: '<path d="M12 3a9 9 0 1 0 0 18c1.1 0 1.6-.8 1.6-1.6 0-1.2-1-1.6-1-2.6 0-.9.7-1.6 1.6-1.6H17a4 4 0 0 0 4-4C21 6.6 17 3 12 3z"/><circle cx="7.5" cy="11" r="1.2"/><circle cx="10" cy="7" r="1.2"/><circle cx="15" cy="7.5" r="1.2"/>',
};
function icon(name, cls = "") {
  return `<svg class="i ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICON[name] || ""}</svg>`;
}
// Button content: icon + label, laid out the same everywhere.
function withIcon(name, text) {
  const s = document.createElement("span");
  s.className = "btn-in";
  s.innerHTML = icon(name);
  s.append(text);
  return s;
}
function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((e) => {
    const cls = [...e.classList].join(" ");
    e.outerHTML = icon(e.dataset.icon, cls);
  });
}
hydrateIcons();

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
  SAI_PREFS.adopt(state.me.prefs);   // the account's appearance & language win over this browser's
  renderProfileCard();
  if (!state.me.ai_ready && state.me.role === "admin") $("#api-warning").hidden = false;
  if (!state.me.allow_auto_approve) $("#auto-approve-wrap").hidden = true;
  if (state.me.role !== "admin") {
    $("#tab-chat").hidden = true;
    $$(".admin-only").forEach((e) => (e.hidden = true));
    switchView("monitor");
  } else {
    connectWs();
    loadConversations();
    loadChatConnections();
    if (state.me.console) $("#tab-console").hidden = false;
    // Resume showing an update that is still running.
    api("/api/update/progress").then((p) => {
      if (p.state === "running" && !state.updating) followUpgrade(p.version);
    }).catch(() => {});
  }
  api("/api/host").then((h) => {
    $("#hostname").textContent = h.hostname;
    $("#host-os").textContent = `${h.os} · ${h.arch}`;
    document.title = `${h.hostname} · SysAdmin.IA`;
  });
  startMonitor();
}

$$(".tab").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
const SUBPAGES = new Set(["profile", "password", "prefs"]);   // opened from the profile card, not tabs
function switchView(v) {
  if (!SUBPAGES.has(v) && v !== state.view) state.lastTab = v;
  if (SUBPAGES.has(v) && !SUBPAGES.has(state.view)) state.lastTab = state.view;
  state.view = v;
  const navView = v === "job-edit" ? "jobs" : v;   // sub-pages keep their module highlighted
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === navView));
  $$(".view").forEach((s) => s.classList.toggle("active", s.id === `view-${v}`));
  closeProfileMenu();
  closeNav();
  clearInterval(state.drivesTimer);
  if (v === "ai") loadAi();
  if (v === "jobs") loadJobs();
  if (v === "localai") loadLocalAI();
  if (v === "containers" && window.loadDocker) loadDocker();
  if (v === "network" && window.loadNetwork) loadNetwork();
  if (v === "variables") loadVariables();
  if (v === "mail") loadMail();
  if (v === "admin") loadAdmin();
  if (v === "monitor") { drawCharts(); refreshProcs(); }
  if (v === "console") showConsoleSession(state.consoleSession || "shell");
  if (v === "users") loadUsers();
  if (v === "drives") openDrives();
  if (v === "profile") loadProfilePage();
  if (v === "prefs") renderPrefs();
  if (v === "password") { $("#pw-form").reset(); setFormMsg("#pw-msg", ""); $("#pw-form").current_password.focus(); }
}
// ---- left sidebar: off-canvas on phones (menu button), collapsible to icons on wider screens
function closeNav() {
  document.body.classList.remove("nav-open");
  $("#nav-scrim").hidden = true;
  $("#nav-toggle").setAttribute("aria-expanded", "false");
}
$("#nav-toggle").addEventListener("click", () => {
  const open = !document.body.classList.contains("nav-open");
  document.body.classList.toggle("nav-open", open);
  $("#nav-scrim").hidden = !open;
  $("#nav-toggle").setAttribute("aria-expanded", String(open));
});
$("#nav-scrim").addEventListener("click", closeNav);
function setNavCollapsed(collapsed) {
  document.body.classList.toggle("nav-collapsed", collapsed);
  const b = $("#nav-collapse");
  b.title = collapsed ? "Expand the sidebar" : "Collapse the sidebar";
  b.querySelector("span").textContent = collapsed ? "Expand" : "Collapse";
  try { localStorage.setItem("sai.navCollapsed", collapsed ? "1" : ""); } catch { /* private mode */ }
  // terminals and charts depend on the width
  if (state.view === "console" && consoles[state.consoleSession]) setTimeout(() => consoles[state.consoleSession].fit.fit(), 250);
}
$("#nav-collapse").addEventListener("click", () => setNavCollapsed(!document.body.classList.contains("nav-collapsed")));
try { if (localStorage.getItem("sai.navCollapsed")) setNavCollapsed(true); } catch { /* private mode */ }

// ---- chat list: collapsible like the modules sidebar (icon rail / slide-out on phones)
function setChatsCollapsed(collapsed) {
  document.body.classList.toggle("chats-collapsed", collapsed);
  const b = $("#chats-collapse");
  b.title = collapsed ? "Show the chat list" : "Collapse the chat list";
  b.querySelector("span").textContent = collapsed ? "Expand" : "Collapse";
  try { localStorage.setItem("sai.chatsCollapsed", collapsed ? "1" : ""); } catch { /* private mode */ }
}
function closeChats() {
  document.body.classList.remove("chats-open");
  $("#chats-scrim").hidden = true;
  $("#chats-toggle").setAttribute("aria-expanded", "false");
}
$("#chats-collapse").addEventListener("click", () => setChatsCollapsed(!document.body.classList.contains("chats-collapsed")));
$("#chats-toggle").addEventListener("click", () => {
  const open = !document.body.classList.contains("chats-open");
  document.body.classList.toggle("chats-open", open);
  $("#chats-scrim").hidden = !open;
  $("#chats-toggle").setAttribute("aria-expanded", String(open));
});
$("#chats-scrim").addEventListener("click", closeChats);
try { if (localStorage.getItem("sai.chatsCollapsed")) setChatsCollapsed(true); } catch { /* private mode */ }

$$("[data-back]").forEach((b) => b.addEventListener("click", () =>
  switchView(state.lastTab || (state.me.role === "admin" ? "chat" : "monitor"))));

function setFormMsg(sel, text, ok = true) {
  const m = $(sel);
  m.textContent = text;
  m.className = `small ${ok ? "ok-text" : "error"}`;
  m.hidden = !text;
}

// ------------------------------------------------------------ avatars & profile card
const AVATAR_COLORS = ["#c2571a", "#2f7fb8", "#2f9e5b", "#8a5cc2", "#c23f72", "#b88a1f", "#3a8f8f", "#6b7280"];
function initials(p) {
  const src = (p.display_name || p.username || "?").trim();
  const parts = src.split(/[\s._@-]+/).filter(Boolean);
  return ((parts[0] || "?")[0] + (parts.length > 1 ? parts[1][0] : "")).toUpperCase();
}
function fillAvatar(el, p) {
  el.innerHTML = "";
  el.classList.toggle("has-img", !!p.avatar_url);
  if (p.avatar_url) {
    const img = document.createElement("img");
    img.src = p.avatar_url; img.alt = "";
    el.append(img);
  } else {
    let h = 0;
    for (const c of p.username || "") h = (h * 31 + c.charCodeAt(0)) >>> 0;
    el.style.background = AVATAR_COLORS[h % AVATAR_COLORS.length];
    el.textContent = initials(p);
  }
}

function renderProfileCard() {
  const me = state.me;
  fillAvatar($("#profile-btn-avatar"), me);
  $("#profile-btn-name").textContent = me.username;
  fillAvatar($("#profile-menu-avatar"), me);
  $("#profile-menu-name").textContent = me.display_name || me.username;
  $("#profile-menu-sub").textContent = [me.display_name ? `@${me.username}` : "", me.email,
    me.role === "admin" ? "Administrator" : "Viewer"].filter(Boolean).join(" · ");
}

function closeProfileMenu() {
  $("#profile-menu").hidden = true;
  $("#profile-btn").setAttribute("aria-expanded", "false");
}
$("#profile-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  const menu = $("#profile-menu");
  menu.hidden = !menu.hidden;
  $("#profile-btn").setAttribute("aria-expanded", String(!menu.hidden));
  if (!menu.hidden) $("#menu-edit-profile").focus();
});
document.addEventListener("click", (e) => {
  if (!$("#profile-menu").hidden && !e.target.closest("#profile-menu")) closeProfileMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#profile-menu").hidden) { closeProfileMenu(); $("#profile-btn").focus(); }
});
$("#menu-edit-profile").addEventListener("click", () => switchView("profile"));
$("#menu-change-password").addEventListener("click", () => switchView("password"));
$("#menu-prefs").addEventListener("click", () => switchView("prefs"));

// ---- Appearance & language (prefs.js applies them; saved to the account here)
SAI_PREFS.onChange((p) => {
  clearTimeout(SAI_PREFS._saveT);
  SAI_PREFS._saveT = setTimeout(() => {
    api("/api/me/prefs", { method: "PUT", body: JSON.stringify(p) })
      .then(() => { if (state.view === "prefs") setFormMsg("#pref-msg", "Saved."); })
      .catch((ex) => { if (state.view === "prefs") setFormMsg("#pref-msg", ex.message, false); });
  }, 400);
});
function renderPrefs() {
  const p = SAI_PREFS.get();
  const sel = $("#pref-lang");
  if (!sel.options.length) for (const [code, name] of Object.entries(SAI_PREFS.LANGS)) sel.add(new Option(name, code));
  sel.value = p.lang;
  for (const [id, key] of [["#pref-theme", "theme"], ["#pref-accent", "accent"], ["#pref-text", "text"]]) {
    $$("[data-v]", $(id)).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === p[key])));
  }
  $("#pref-contrast").checked = p.contrast;
  $("#pref-motion").checked = !p.motion;
  renderThemeToggle();
}
function setPref(changes) { SAI_PREFS.set(changes); renderPrefs(); setFormMsg("#pref-msg", ""); }
$("#pref-lang").addEventListener("change", (e) => setPref({ lang: e.target.value }));
for (const [id, key] of [["#pref-theme", "theme"], ["#pref-accent", "accent"], ["#pref-text", "text"]]) {
  $(id).addEventListener("click", (e) => { const b = e.target.closest("[data-v]"); if (b) setPref({ [key]: b.dataset.v }); });
}
$("#pref-contrast").addEventListener("change", (e) => setPref({ contrast: e.target.checked }));
$("#pref-motion").addEventListener("change", (e) => setPref({ motion: !e.target.checked }));
$("#pref-reset").addEventListener("click", () => setPref({ ...SAI_PREFS.DEFAULTS, lang: SAI_PREFS.get().lang }));
// Quick light/dark switch in the top bar
function renderThemeToggle() {
  const dark = SAI_PREFS.mode() === "dark";
  const b = $("#theme-toggle");
  b.innerHTML = icon(dark ? "sun" : "moon");
  b.title = dark ? "Switch to light mode" : "Switch to dark mode";   // English in the DOM; prefs.js translates
  b.setAttribute("aria-label", b.title);
}
$("#theme-toggle").addEventListener("click", () => {
  const p = SAI_PREFS.get();
  const dark = SAI_PREFS.mode() === "dark";
  SAI_PREFS.set({ theme: dark ? "light" : (p.theme === "black" ? "black" : "dark") });
  if (state.view === "prefs") renderPrefs(); else renderThemeToggle();
});
window.addEventListener("sai:theme", () => {
  renderThemeToggle();
  if (state.view === "monitor") drawCharts();   // charts read the colours when drawn
});
renderThemeToggle();
$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  location.href = "/login";
});

// ---- Edit profile page
function loadProfilePage() {
  const me = state.me, f = $("#profile-form");
  f.username.value = me.username;
  f.display_name.value = me.display_name || "";
  f.email.value = me.email || "";
  fillAvatar($("#profile-avatar"), me);
  $("#avatar-remove").hidden = !me.avatar_url;
  $("#profile-meta").textContent = `Role: ${me.role === "admin" ? "Administrator" : "Viewer"}`;
  setFormMsg("#profile-msg", "");
}

function applyProfile(p) {
  Object.assign(state.me, { username: p.username, display_name: p.display_name, email: p.email, avatar_url: p.avatar_url });
  renderProfileCard();
  loadProfilePage();
}

$("#profile-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    const p = await api("/api/me/profile", { method: "PUT", body: JSON.stringify({
      username: f.username.value.trim(), display_name: f.display_name.value, email: f.email.value.trim() }) });
    applyProfile(p);
    setFormMsg("#profile-msg", "Saved.");
  } catch (ex) { setFormMsg("#profile-msg", ex.message, false); }
});

$("#avatar-change").addEventListener("click", () => $("#avatar-file").click());
$("#avatar-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  try {
    const data = await squareImage(file, 256);
    const p = await api("/api/me/avatar", { method: "PUT", body: JSON.stringify({ data, type: "image/jpeg" }) });
    applyProfile(p);
    setFormMsg("#profile-msg", "Picture updated.");
  } catch (ex) { setFormMsg("#profile-msg", ex.message || "Couldn't use that picture.", false); }
});
$("#avatar-remove").addEventListener("click", async () => {
  try {
    applyProfile(await api("/api/me/avatar", { method: "PUT", body: JSON.stringify({ data: null }) }));
    setFormMsg("#profile-msg", "Picture removed.");
  } catch (ex) { setFormMsg("#profile-msg", ex.message, false); }
});

// Center-crop an image file to a square and return base64 JPEG (no data: prefix).
function squareImage(file, size) {
  return new Promise((resolve, reject) => {
    if (!file.type.startsWith("image/")) { reject(new Error("Choose an image file.")); return; }
    if (file.size > 20 * 1024 * 1024) { reject(new Error("That image is too large (max 20 MB).")); return; }
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const s = Math.min(img.naturalWidth, img.naturalHeight);
      const c = document.createElement("canvas");
      c.width = c.height = size;
      const ctx = c.getContext("2d");
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, size, size);
      ctx.drawImage(img, (img.naturalWidth - s) / 2, (img.naturalHeight - s) / 2, s, s, 0, 0, size, size);
      URL.revokeObjectURL(url);
      resolve(c.toDataURL("image/jpeg", 0.88).split(",")[1]);
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Couldn't read that image.")); };
    img.src = url;
  });
}

// ---- Change password page
$("#pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  if (f.new_password.value !== f.confirm_password.value) {
    setFormMsg("#pw-msg", "The new passwords don't match.", false);
    f.confirm_password.focus();
    return;
  }
  try {
    await api("/api/me/password", { method: "POST", body: JSON.stringify({
      current_password: f.current_password.value, new_password: f.new_password.value }) });
    setFormMsg("#pw-msg", "Password changed. Signing you out…");
    setTimeout(() => (location.href = "/login"), 1500);
  } catch (ex) { setFormMsg("#pw-msg", ex.message, false); }
});

// ------------------------------------------------------------ users
async function loadUsers() {
  const list = $("#user-list");
  let users;
  try { users = await api("/api/users"); }
  catch (ex) { list.textContent = ex.message; return; }
  list.innerHTML = "";
  for (const u of users) {
    const me = u.id === state.me.id;
    const row = document.createElement("div");
    row.className = "user-row";
    const av = document.createElement("span");
    av.className = "avatar lg";
    fillAvatar(av, u);
    const who = document.createElement("div");
    who.className = "user-who";
    const name = document.createElement("strong");
    name.textContent = u.display_name || u.username;
    const sub = document.createElement("div");
    sub.className = "muted small";
    sub.textContent = [u.display_name ? `@${u.username}` : "", u.email, me ? "you" : ""].filter(Boolean).join(" · ");
    who.append(name, sub);

    const role = document.createElement("select");
    role.innerHTML = '<option value="admin">Admin</option><option value="viewer">Viewer</option>';
    role.value = u.role;
    role.disabled = me;
    role.title = me ? "You can't change your own role" : "Role";
    role.setAttribute("aria-label", `Role of ${u.username}`);
    role.addEventListener("change", async () => {
      try { await api(`/api/users/${u.id}`, { method: "PATCH", body: JSON.stringify({ role: role.value }) }); toast(`${u.username} is now ${role.value === "admin" ? "an admin" : "a viewer"}.`); }
      catch (ex) { toast(ex.message); role.value = u.role; }
    });

    const actions = document.createElement("div");
    actions.className = "user-actions";
    const pw = document.createElement("button");
    pw.type = "button"; pw.className = "ghost small"; pw.append(withIcon("key", "Set password"));
    pw.addEventListener("click", async () => {
      if (me) { switchView("password"); return; }
      const value = prompt(`New password for ${u.username} (at least 10 characters).\nThey'll be signed out everywhere.`);
      if (!value) return;
      try { await api(`/api/users/${u.id}`, { method: "PATCH", body: JSON.stringify({ password: value }) }); toast(`Password for ${u.username} changed.`); }
      catch (ex) { toast(ex.message); }
    });
    actions.append(pw);
    if (!me) {
      const del = document.createElement("button");
      del.type = "button"; del.className = "ghost small danger"; del.append(withIcon("trash", "Delete"));
      del.addEventListener("click", async () => {
        if (!confirm(`Delete user ${u.username}? Their conversations are deleted too.`)) return;
        try { await api(`/api/users/${u.id}`, { method: "DELETE" }); loadUsers(); }
        catch (ex) { toast(ex.message); }
      });
      actions.append(del);
    }
    row.append(av, who, role, actions);
    list.append(row);
  }
}

$("#add-user").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#user-error");
  err.hidden = true;
  try {
    await api("/api/users", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(e.target))) });
    toast(`User ${e.target.username.value} added.`);
    e.target.reset();
    loadUsers();
  } catch (ex) { err.textContent = ex.message; err.hidden = false; }
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
    case "connection":   // which AI connection is answering this chat
      if (ev.conversation_id === state.convId) { state.convConnection = ev.id; selectChatConnection(ev.id); }
      break;
    case "assistant_start":
      state.currentAssistant = null;
      state.currentThinking = null;
      setStatus("Thinking…", true);
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
      endStreaming();
      state.currentAssistant = addMessage("assistant", "");
      state.currentAssistant.classList.add("streaming");   // blinking cursor while text arrives
      setStatus("Responding…", true);
      break;
    case "text_delta":
      if (!state.currentAssistant) { state.currentAssistant = addMessage("assistant", ""); state.currentAssistant.classList.add("streaming"); }
      state.currentAssistant._raw += ev.text;
      $(".content", state.currentAssistant).innerHTML = md(state.currentAssistant._raw);
      scrollDown();
      break;
    case "tool_preparing":
      setStatus(`Preparing a ${ev.name === "bash" ? "command" : ev.name === "restart_app" ? "restart" : "file operation"}…`, true);
      break;
    case "tool_request":
      endStreaming();
      state.currentAssistant = null;
      addToolCard(ev);
      setStatus(ev.needs_approval ? "Waiting for your approval…" : "Running…", !ev.needs_approval);
      break;
    case "tool_result":
      setToolResult(ev.id, ev.output, ev.is_error);
      setStatus("Thinking…", true);
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
    case "usage":   // tokens this reply used (sent just before "done")
      state.lastUsage = turnUsageText(ev);
      break;
    case "done":
      endStreaming();
      setBusy(false);
      if (state.lastUsage) { setStatus(state.lastUsage); state.lastUsage = null; }
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

function endStreaming() {
  document.querySelectorAll(".msg.streaming").forEach((e) => e.classList.remove("streaming"));
}

// Chat status line; spin = show the loading spinner while the AI works.
function setStatus(s, spin = false) {
  const st = $("#status");
  st.innerHTML = spin && s ? '<span class="spinner" aria-hidden="true"></span>' : "";
  st.append(s);
  st.classList.toggle("working", spin && !!s);
}

// 1234 → "1.2k", 5_300_000 → "5.3M"
function fmtTok(n) {
  n = Number(n) || 0;
  if (n < 1000) return String(n);
  if (n < 1e6) return `${(n / 1e3).toFixed(n < 1e4 ? 1 : 0)}k`;
  if (n < 1e9) return `${(n / 1e6).toFixed(n < 1e7 ? 2 : 1)}M`;
  return `${(n / 1e9).toFixed(2)}B`;
}
function turnUsageText(u) {
  const input = u.input_tokens + u.cache_read_tokens + u.cache_write_tokens;
  let s = `${u.connection}: ${fmtTok(input + u.output_tokens)} tokens (${fmtTok(input)} in · ${fmtTok(u.output_tokens)} out)`;
  if (u.monthly_limit) s += ` · ${Math.round(100 * u.month_tokens / u.monthly_limit)}% of monthly limit`;
  return s;
}
function setBusy(b) {
  state.busy = b;
  $("#send").hidden = b;
  $("#stop").hidden = !b;
  if (!b) { setStatus(""); endStreaming(); }
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
  const connection_id = Number($("#chat-connection").value) || null;
  if (!wsSend({ type: "chat", conversation_id: state.convId, text, connection_id })) return;
  addMessage("user", text);
  input.value = "";
  input.style.height = "auto";
  setBusy(true);
  setStatus("Sending…", true);
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
  closeChats();
  state.convId = null;
  state.convConnection = null;
  selectChatConnection(null);   // new chats start on the default connection
  clearMessages();
  emptyState.hidden = false;
  messagesEl.appendChild(emptyState);
  $$("#conv-list li").forEach((li) => li.classList.remove("active"));
  input.focus();
}
$("#new-chat").addEventListener("click", newChat);

async function openConversation(id) {
  if (state.busy) { toast("Stop the current task first."); return; }
  closeChats();
  const conv = await api(`/api/conversations/${id}`);
  state.convId = id;
  state.convConnection = conv.connection_id || null;
  selectChatConnection(state.convConnection);
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

// ------------------------------------------------------------ AI connections module
// Same tile as the drive icons: a monogram of the provider (e.g. "Cl", "Ch", "Gr").
function providerBadge(text, accent) {
  const b = document.createElement("span");
  b.className = `icon-tile monogram ${accent ? "accent" : ""}`;
  b.textContent = (text || "?").slice(0, 2);
  return b;
}

async function loadAiCatalog() {
  if (!state.aiCatalog) state.aiCatalog = await api("/api/ai/providers");
  return state.aiCatalog;
}

// Usage panel of a connection card.
function rateReset(v) {
  if (!v) return "";
  const d = new Date(v);
  if (!isNaN(d) && /\d{4}-\d{2}-\d{2}/.test(v)) return ` · resets ${d.toLocaleTimeString()}`;
  return ` · resets in ${v}`;   // OpenAI-style durations, e.g. "6m0s"
}
function usageBlock(c, u) {
  const box = el("div", { class: "ai-usage" });
  if (!u) return box;
  const stat = (label, value, sub) => el("div", { class: "u-stat" },
    el("div", { class: "label" }, label), el("div", { class: "u-value" }, value), el("div", { class: "muted small" }, sub));
  const tokIn = (t) => t.input_tokens + t.cache_read_tokens + t.cache_write_tokens;
  box.append(el("div", { class: "u-stats" },
    stat("Today", fmtTok(u.today.tokens), `${u.today.requests} request${u.today.requests === 1 ? "" : "s"}`),
    stat("This month", fmtTok(u.month.tokens), `${fmtTok(tokIn(u.month))} in · ${fmtTok(u.month.output_tokens)} out`),
    stat("All time", fmtTok(u.total.tokens), `${u.total.requests} requests`)));
  if (u.limit) {
    const pct = Math.min(100, u.used_pct);
    box.append(el("div", { class: "u-meter" },
      el("div", { class: "small" }, el("strong", {}, `${u.used_pct}%`), ` of the ${fmtTok(u.limit)}-token monthly limit · ${fmtTok(u.remaining)} left`,
         u.block_at_limit ? el("span", { class: "muted" }, " · stops at the limit") : null),
      usageBar(pct)));
  }
  const r = u.rate;
  if (r) {
    const rows = [["tokens", "tokens"], ["input_tokens", "input tokens"], ["output_tokens", "output tokens"], ["requests", "requests"]]
      .filter(([k]) => r[k] && r[k].limit);
    if (rows.length) {
      const age = Math.round((Date.now() / 1000 - r.at) / 60);
      const meter = el("div", { class: "u-meter" }, el("div", { class: "small muted" },
        `Rate limits reported by the service ${age < 1 ? "just now" : `${age} min ago`}`));
      for (const [k, label] of rows) {
        const x = r[k], rem = x.remaining ?? x.limit;
        meter.append(el("div", { class: "u-rate" },
          el("span", { class: "small" }, `${fmtTok(rem)} of ${fmtTok(x.limit)} ${label} left${rateReset(x.reset)}`),
          usageBar(100 - (100 * rem / x.limit))));
      }
      box.append(meter);
    }
  } else if (c.provider !== "ollama") {
    box.append(el("div", { class: "small muted" }, "Rate limits appear here after the next reply."));
  }
  return box;
}

async function loadAi() {
  const list = $("#ai-list");
  let conns, usage = {};
  try { [conns, , usage] = await Promise.all([api("/api/ai/connections"), loadAiCatalog(), api("/api/ai/usage").catch(() => ({}))]); }
  catch (ex) { list.textContent = ex.message; return; }
  state.aiConnections = conns;
  list.innerHTML = "";
  if (!conns.length) {
    list.append(el("div", { class: "card empty-card" },
      el("strong", {}, "No AI connections yet"),
      el("p", { class: "muted small" }, "Add one to start chatting: Claude, ChatGPT, Grok, Gemini, Copilot (Azure / GitHub Models), Groq, Mistral, local models with Ollama, and more."),
      el("button", { type: "button", class: "primary", onclick: () => openAiEditor() }, "+ Add connection")));
  }
  // Grouped by provider (e.g. all "Ollama (Server Managed)" models together); the default first.
  const provOrder = Object.keys(state.aiCatalog.providers);
  conns.sort((a, b) => (provOrder.indexOf(a.provider) - provOrder.indexOf(b.provider)) || (b.is_default - a.is_default) || a.name.localeCompare(b.name));
  const multiProviders = new Set(conns.map((c) => c.provider)).size > 1;
  let lastProvider = null;
  for (const c of conns) {
    if (multiProviders && c.provider !== lastProvider) {
      lastProvider = c.provider;
      const extra = c.provider === "ollama_managed"
        ? el("button", { type: "button", class: "small ghost", onclick: () => switchView("localai") }, withIcon("settings", "Manage in Local AI")) : null;
      list.append(el("div", { class: "provider-head" }, el("span", {}, c.provider_label), extra));
    }
    const status = [];
    if (c.is_default) status.push(el("span", { class: "tag tag-accent" }, "Default"));
    if (!c.enabled) status.push(el("span", { class: "tag" }, "Disabled"));
    const cred = c.config.google_account ? `signed in as ${c.config.google_account}`
      : c.uses_env_key ? "key from the server config file"
      : Object.keys(c.secrets_set).length === 0 ? "no sign-in needed"
      : Object.values(c.secrets_set).some(Boolean) ? "credentials saved" : (c.method === "vertex" || c.method === "bedrock" ? "server's own login" : "no key saved");
    const result = el("p", { class: "small", hidden: true });
    const actions = el("div", { class: "ai-actions" },
      el("button", { type: "button", class: "small", onclick: async (e) => {
        const b = e.currentTarget; b.disabled = true;
        result.hidden = false; result.className = "small muted"; result.textContent = "Testing…";
        try {
          const r = await api("/api/ai/test", { method: "POST", body: JSON.stringify({ id: c.id }) });
          result.className = `small ${r.ok ? "ok-text" : "error"}`; result.textContent = r.message;
        } catch (ex) { result.className = "small error"; result.textContent = ex.message; }
        b.disabled = false;
      } }, withIcon("test", "Test")),
      el("button", { type: "button", class: "small", onclick: () => openAiEditor(c) }, withIcon("edit", "Edit")),
      !c.is_default && c.enabled ? el("button", { type: "button", class: "small ghost", onclick: async () => {
        try { await api(`/api/ai/connections/${c.id}/default`, { method: "POST" }); loadAi(); loadChatConnections(); }
        catch (ex) { toast(ex.message); }
      } }, withIcon("check", "Make default")) : null,
      el("button", { type: "button", class: "small ghost danger", onclick: async () => {
        if (!confirm(`Delete the AI connection “${c.name}”? Its saved key or credentials are deleted too. Chats that used it switch to the default connection.`)) return;
        try { await api(`/api/ai/connections/${c.id}`, { method: "DELETE" }); toast(`Deleted “${c.name}”.`); loadAi(); loadChatConnections(); }
        catch (ex) { toast(ex.message); }
      } }, withIcon("trash", "Delete")));
    list.append(el("div", { class: `card ai-card ${c.enabled ? "" : "disabled"}` },
      el("div", { class: "ai-card-head" },
        providerBadge(c.short, c.is_default),
        el("div", { class: "ai-card-title" },
          el("div", {}, el("strong", {}, c.name), " ", status),
          el("div", { class: "muted small" }, `${c.provider_label} · ${c.method_label}`),
          el("div", { class: "small" }, el("span", { class: "muted" }, "Model "), el("span", { class: "mono" }, c.model),
             el("span", { class: "muted" }, ` · ${cred}`))),
        actions),
      usageBlock(c, usage[String(c.id)]),
      result));
  }
  state.me.ai_ready = conns.some((c) => c.enabled);
  $("#api-warning").hidden = state.me.ai_ready;
}

$("#ai-add").addEventListener("click", () => openAiEditor());
$("#goto-ai").addEventListener("click", (e) => { e.preventDefault(); switchView("ai"); });

// ---- editor dialog
async function openAiEditor(conn = null) {
  state.aiCatalog = null;          // fresh: e.g. models just installed in Local AI
  const cat = await loadAiCatalog();
  const f = $("#ai-form");
  state.aiEditing = conn;
  $("#ai-dialog-title").textContent = conn ? `Edit “${conn.name}”` : "Add AI connection";
  const sel = $("#ai-provider");
  sel.innerHTML = "";
  for (const [id, p] of Object.entries(cat.providers)) sel.append(el("option", { value: id }, p.label));
  sel.value = conn ? conn.provider : "anthropic";
  f.effort.innerHTML = cat.effort_levels.map((e) => `<option value="${e}">${e}</option>`).join("");
  f.name.value = conn ? conn.name : "";
  f.enabled.checked = conn ? conn.enabled : true;
  f.monthly_token_limit.value = conn && conn.config.monthly_token_limit ? conn.config.monthly_token_limit : "";
  f.block_at_limit.checked = !!(conn && conn.config.block_at_limit);
  f.is_default.checked = conn ? conn.is_default : !(state.aiConnections || []).length;
  state.aiMethod = conn ? conn.method : null;
  renderAiProvider(conn);
  setFormMsg("#ai-msg", "");
  $("#ai-save").disabled = false;
  $("#ai-dialog").showModal();
}

function renderAiProvider(conn = null) {
  const cat = state.aiCatalog, f = $("#ai-form");
  const pid = $("#ai-provider").value, p = cat.providers[pid];
  $("#ai-provider-desc").textContent = p.description;
  const methods = Object.entries(p.methods);
  if (!state.aiMethod || !p.methods[state.aiMethod]) state.aiMethod = methods[0][0];
  const box = $("#ai-methods");
  box.innerHTML = "";
  for (const [mid, m] of methods) {
    const input = el("input", { type: "radio", name: "method", value: mid });
    input.checked = mid === state.aiMethod;
    input.addEventListener("change", () => { state.aiMethod = mid; renderAiFields(conn); });
    box.append(el("label", { class: "method-option" }, input, el("span", {}, m.label)));
  }
  $("#ai-methods-wrap").hidden = methods.length < 2;
  if (!f.name.value || (state.aiPrevShort && f.name.value === state.aiPrevShort)) f.name.value = p.short;
  state.aiPrevShort = p.short;
  const sameProvider = conn && conn.provider === pid;
  f.model.value = sameProvider ? conn.model : (p.default_model || "");
  $("#ai-model-options").innerHTML = (p.models || []).map((m) => `<option value="${esc(m)}">`).join("");
  // Server-managed Ollama: choose from the models installed on this server.
  const msel = $("#ai-model-select");
  $("#ai-model-row").hidden = !!p.dynamic_models;
  msel.hidden = !p.dynamic_models;
  if (p.dynamic_models) {
    msel.innerHTML = "";
    if (!p.models.length) msel.append(el("option", { value: "" }, p.installed ? "No models installed yet — download one in Local AI" : "Ollama isn't installed — set it up in Local AI"));
    for (const m of p.models) msel.append(el("option", { value: m }, m));
    if (f.model.value && !p.models.includes(f.model.value)) msel.prepend(el("option", { value: f.model.value }, `${f.model.value} (not installed)`));
    msel.value = f.model.value || (p.models[0] || "");
    f.model.value = msel.value;
  }
  $("#ai-effort-wrap").hidden = !p.supports_effort;
  f.effort.value = (sameProvider && conn.config.effort) || "high";
  renderAiFields(conn);
}

function renderAiFields(conn) {
  const cat = state.aiCatalog, pid = $("#ai-provider").value, p = cat.providers[pid];
  const m = p.methods[state.aiMethod];
  const same = conn && conn.provider === pid && conn.method === state.aiMethod;
  $("#ai-method-help").textContent = m.help || "";
  $("#ai-model-label").textContent = m.model_label || "Model";
  $("#ai-model-help").textContent = m.model_help || (p.models && p.models.length ? "Pick a suggestion, type any model ID, or use Load models." : "");
  const box = $("#ai-fields");
  box.innerHTML = "";
  if (m.setup) {
    box.append(el("details", { class: "setup-steps", open: !same },
      el("summary", {}, "One-time setup in Google Cloud"),
      el("ol", {}, m.setup.map((s) => el("li", {}, s)))));
  }
  for (const fd of m.fields) {
    const id = `ai-f-${fd.key}`;
    if (fd.type === "google_signin") { box.append(googleSignInField(fd, same ? conn : null)); continue; }
    let input;
    if (fd.type === "textarea") input = el("textarea", { id, rows: "4", spellcheck: "false" });
    else input = el("input", { id, type: fd.type === "password" ? "password" : (fd.type === "url" ? "url" : "text"), spellcheck: "false", autocomplete: "off" });
    input.dataset.key = fd.key;
    input.dataset.secret = fd.secret ? "1" : "";
    if (fd.secret) {
      const isSet = same && conn.secrets_set[fd.key];
      input.placeholder = isSet ? "saved — leave empty to keep it, or type a new one" : (fd.placeholder || "");
      if (fd.key === "api_key" && !isSet && p.env_key_available) input.placeholder = `optional — ${p.env_key} from the server config file is used`;
    } else {
      input.value = same ? (conn.config[fd.key] ?? "") : (fd.default || "");
      input.placeholder = fd.placeholder || "";
    }
    const label = el("label", { for: id }, fd.label, fd.required ? "" : el("span", { class: "muted small" }, " (optional)"), input);
    if (fd.help) label.append(el("span", { class: "muted small" }, fd.help));
    if (fd.secret && same && conn.secrets_set[fd.key]) {
      const clear = el("label", { class: "check small" }, el("input", { type: "checkbox", "data-clear": fd.key }), "Remove the saved value");
      label.append(clear);
    }
    box.append(label);
  }
}
$("#ai-provider").addEventListener("change", () => { state.aiMethod = null; renderAiProvider(state.aiEditing); });
$("#ai-model-select").addEventListener("change", (e) => { $("#ai-form").model.value = e.target.value; });

// "Sign in with Google" (Claude on Vertex AI). Google sends the browser to a loopback address
// (http://127.0.0.1:53682/…) that doesn't load; the admin pastes that address back here and the
// server exchanges it for a sign-in it keeps. The browser only ever holds a short-lived "grant" id.
function googleSignInField(fd, conn) {
  const hidden = el("input", { type: "hidden", "data-key": fd.key, "data-secret": "1" });
  const account = conn && conn.config.google_account;
  const status = el("div", { class: "g-status" },
    el("span", { class: `dot ${account ? "on" : ""}` }),
    el("span", {}, account ? `Signed in as ${account}` : (conn && conn.secrets_set[fd.key] ? "Signed in" : "Not signed in yet")));
  const paste = el("input", { type: "url", placeholder: "http://127.0.0.1:53682/?state=…&code=…", spellcheck: "false", "aria-label": "Address from Google" });
  const finishBtn = el("button", { type: "button", class: "primary small" }, "Finish sign-in");
  const msg = el("p", { class: "small", hidden: true });
  const steps = el("div", { class: "g-steps", hidden: true },
    el("ol", {},
      el("li", {}, "In the Google tab that just opened, choose your account and allow access."),
      el("li", {}, "Google then shows a page that doesn't load (“This site can't be reached”). That's expected."),
      el("li", {}, "Copy the whole address from that tab's address bar, paste it here, and click Finish sign-in.")),
    el("div", { class: "row" }, paste, finishBtn), msg);
  const startBtn = el("button", { type: "button", class: "g-btn" }, withIcon("login", account ? "Sign in with a different Google account" : "Sign in with Google"));
  const say = (text, ok) => { msg.textContent = text; msg.className = `small ${ok ? "ok-text" : "error"}`; msg.hidden = !text; };

  startBtn.addEventListener("click", async () => {
    const win = window.open("", "_blank");   // open now (a popup opened after an await may be blocked)
    try {
      const r = await api("/api/ai/google/start", { method: "POST", body: JSON.stringify({
        client_id: ($("#ai-f-google_client_id")?.value || "").trim(),
        client_secret: ($("#ai-f-google_client_secret")?.value || "").trim() || null,
        connection_id: state.aiEditing?.id || null }) });
      if (win) win.location.href = r.auth_url;
      steps.hidden = false;
      say("", true);
      if (!win) {
        msg.hidden = false; msg.className = "small";
        msg.innerHTML = ""; msg.append("Your browser blocked the new tab. ", el("a", { href: r.auth_url, target: "_blank", rel: "noopener" }, "Open the Google sign-in page"), ".");
      }
      paste.focus();
    } catch (ex) {
      if (win) win.close();
      steps.hidden = false; say(ex.message, false);
    }
  });
  finishBtn.addEventListener("click", async () => {
    finishBtn.disabled = true;
    try {
      const r = await api("/api/ai/google/finish", { method: "POST", body: JSON.stringify({ url: paste.value }) });
      hidden.value = r.grant;
      status.innerHTML = "";
      status.append(el("span", { class: "dot on" }), el("span", {}, `Signed in as ${r.email || "your Google account"} — click Save to keep it`));
      steps.hidden = true;
      paste.value = "";
      setFormMsg("#ai-msg", "Signed in with Google. Test the connection, then Save.");
    } catch (ex) { say(ex.message, false); }
    finishBtn.disabled = false;
  });
  return el("div", { class: "g-signin" }, el("div", { class: "g-label" }, fd.label), status, startBtn, steps, hidden);
}

function aiFormBody() {
  const f = $("#ai-form");
  const body = { name: f.name.value.trim(), provider: $("#ai-provider").value, method: state.aiMethod,
                 config: { model: f.model.value.trim(), effort: f.effort.value,
                           monthly_token_limit: f.monthly_token_limit.value.replace(/[\s,._]/g, "") || null,
                           block_at_limit: f.block_at_limit.checked }, secrets: {}, clear_secrets: [],
                 enabled: f.enabled.checked, is_default: f.is_default.checked };
  $$("#ai-fields [data-key]").forEach((i) => {
    if (i.dataset.secret) { if (i.value.trim()) body.secrets[i.dataset.key] = i.value.trim(); }
    else body.config[i.dataset.key] = i.value.trim();
  });
  $$("#ai-fields [data-clear]").forEach((c) => { if (c.checked) body.clear_secrets.push(c.dataset.clear); });
  return body;
}

$("#ai-cancel").addEventListener("click", () => $("#ai-dialog").close());
$("#ai-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = aiFormBody(), editing = state.aiEditing;
  $("#ai-save").disabled = true;
  try {
    await api(editing ? `/api/ai/connections/${editing.id}` : "/api/ai/connections",
              { method: editing ? "PUT" : "POST", body: JSON.stringify(body) });
    $("#ai-dialog").close();
    toast(editing ? "Connection saved." : `Added “${body.name}”.`);
    loadAi(); loadChatConnections();
  } catch (ex) { setFormMsg("#ai-msg", ex.message, false); }
  $("#ai-save").disabled = false;
});
$("#ai-test").addEventListener("click", async () => {
  const b = $("#ai-test");
  b.disabled = true;
  setFormMsg("#ai-msg", "Testing…");
  try {
    const r = await api("/api/ai/test", { method: "POST", body: JSON.stringify({ id: state.aiEditing?.id || null, draft: aiFormBody() }) });
    setFormMsg("#ai-msg", r.message, r.ok);
  } catch (ex) { setFormMsg("#ai-msg", ex.message, false); }
  b.disabled = false;
});
$("#ai-load-models").addEventListener("click", async () => {
  const b = $("#ai-load-models");
  b.disabled = true; b.textContent = "Loading…";
  try {
    const r = await api("/api/ai/models", { method: "POST", body: JSON.stringify({ id: state.aiEditing?.id || null, draft: aiFormBody() }) });
    $("#ai-model-options").innerHTML = r.models.map((m) => `<option value="${esc(m)}">`).join("");
    setFormMsg("#ai-msg", `${r.models.length} models available. Click the Model box to pick one.`);
  } catch (ex) { setFormMsg("#ai-msg", ex.message, false); }
  b.disabled = false; b.textContent = "Load models";
});

// ---- chat: which connection answers
async function loadChatConnections() {
  if (!state.me || state.me.role !== "admin") return;
  let conns = [];
  try { conns = await api("/api/chat/connections"); } catch { /* ignore */ }
  state.chatConnections = conns;
  const sel = $("#chat-connection");
  sel.innerHTML = "";
  for (const c of conns) sel.append(el("option", { value: String(c.id) }, `${c.name}${c.is_default ? " (default)" : ""} · ${c.model}`));
  if (!conns.length) sel.append(el("option", { value: "" }, "No AI connection"));
  sel.disabled = !conns.length;
  selectChatConnection(state.convConnection);
  state.me.ai_ready = conns.length > 0;
  $("#api-warning").hidden = state.me.ai_ready;
}
function selectChatConnection(id) {
  const sel = $("#chat-connection"), conns = state.chatConnections || [];
  const pick = conns.find((c) => c.id === id) || conns.find((c) => c.is_default) || conns[0];
  if (pick) sel.value = String(pick.id);
}
$("#chat-connection").addEventListener("change", (e) => { state.convConnection = Number(e.target.value) || null; });

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

function fmtDate(iso) { return iso ? new Date(iso).toLocaleDateString() : ""; }

function renderUpdate(s) {
  $("#update-current").textContent = s.current;
  const repo = $("#update-repo");
  repo.textContent = `github.com/${s.repo}`;
  repo.href = `https://github.com/${s.repo}`;
  $("#update-checked").textContent = s.checked_at ? `· checked ${new Date(s.checked_at * 1000).toLocaleTimeString()}` : "";
  const pill = $("#update-status");
  const latest = s.latest;
  const pending = s.pending || [];
  $("#update-latest").hidden = !latest;
  if (latest) {
    const line = $("#update-latest-line");
    if (s.update_available) {
      line.innerHTML = "";
      const strong = document.createElement("strong");
      strong.textContent = latest.version;
      line.append("Version ", strong, " is available. Updating installs it directly");
      line.append(pending.length > 1
        ? `, including everything from the ${pending.length} versions since ${s.current}:`
        : ":");
    } else {
      line.textContent = `You have the latest version (${latest.version}).`;
    }
    const list = $("#update-pending");
    list.innerHTML = "";
    if (s.update_available) {
      [...pending].reverse().forEach((v, i) => {   // newest first
        const d = document.createElement("details");
        d.open = i === 0;
        const sum = document.createElement("summary");
        const b = document.createElement("strong"); b.textContent = v.version;
        sum.append(b, v.name && v.name.replace(/^v/i, "") !== v.version ? ` · ${v.name}` : "",
                   v.published_at ? ` · ${fmtDate(v.published_at)}` : "");
        d.append(sum);
        const notes = document.createElement("pre");
        notes.className = "update-notes mono small";
        notes.textContent = (v.notes || "").trim() || "No release notes.";
        d.append(notes);
        list.append(d);
      });
    }
  }
  if (s.error) { pill.textContent = "check failed"; pill.className = "status-pill warn"; }
  else if (!s.checked_at) { pill.textContent = ""; pill.className = "status-pill"; }
  else if (s.update_available) { pill.textContent = "update available"; pill.className = "status-pill warn"; }
  else { pill.textContent = "up to date"; pill.className = "status-pill ok"; }
  // Several newer versions: pick which one to install (default: the newest, which includes the rest).
  const sel = $("#update-target");
  const prev = sel.value;
  sel.innerHTML = "";
  [...pending].reverse().forEach((v, i) => sel.add(new Option(i === 0 ? `${v.version} (latest)` : v.version, v.version)));
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
  $("#update-target-wrap").hidden = !(s.update_available && s.can_apply && pending.length > 1);
  const apply = $("#update-apply");
  apply.hidden = !s.update_available || !s.can_apply;
  if (latest) apply.textContent = `Update to ${sel.value || latest.version}`;
  renderReleases(s);
  $("#update-check").className = s.update_available ? "ghost big" : "primary big";
  renderUpdateChannel(s);
  if (state.updating) { /* keep the progress message */ }
  else if (s.error) setUpdateMsg(s.error, false);
  else if (s.note && !latest) setUpdateMsg(s.note, false);
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

// ---- update channel: Public (published releases) or Developer (every vX.X.X / vX.X.X.X tag)
function renderUpdateChannel(s) {
  const chan = s.channel || "public";
  $$("#update-channel .seg-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.channel === chan);
    b.setAttribute("aria-checked", String(b.dataset.channel === chan));
  });
  $("#update-channel-help").textContent = chan === "developer"
    ? "Every version tag pushed to GitHub (vX.X.X or vX.X.X.X), including pre-releases and tags without a release. For trying versions before they're released."
    : "Published releases only. Recommended.";
  $("#update-releases-title").textContent = chan === "developer" ? "All version tags" : "All releases";
  const push = $("#update-push");
  const p = chan === "developer" ? s.latest_push : null;
  push.hidden = !p;
  if (p) {
    push.innerHTML = "";
    const a = el("a", { href: p.url, target: "_blank", rel: "noopener noreferrer", class: "mono" }, p.sha);
    push.append(`Latest push to ${p.branch}: `, a, p.message ? el("span", { "data-no-i18n": "" }, ` “${p.message}”`) : "",
      p.date ? ` · ${fmtDate(p.date)}` : "", " · ",
      p.tag ? el("span", {}, "tagged ", el("strong", { "data-no-i18n": "" }, p.tag))
        : el("span", {}, "not tagged yet: only tagged versions can be installed"));
  }
}

$$("#update-channel .seg-btn").forEach((b) => b.addEventListener("click", async () => {
  const chan = b.dataset.channel;
  if (chan === (state.update?.channel || "public")) return;
  if (chan === "developer" && !confirm("Switch updates to the Developer channel?\n\nYou'll be offered every version tag pushed to GitHub, " +
      "including versions that aren't released yet and may be unfinished.")) return;
  $$("#update-channel .seg-btn").forEach((x) => { x.disabled = true; });
  setUpdateBusy("Checking GitHub…");
  try {
    renderUpdate(await api("/api/update/settings", { method: "PUT", body: JSON.stringify({ channel: chan }) }));
    renderUpdate(await api("/api/update/check", { method: "POST" }));
  } catch (ex) { setUpdateMsg(ex.message, false); }
  finally { $$("#update-channel .seg-btn").forEach((x) => { x.disabled = false; }); }
}));

async function loadUpdate() {
  const s = await api("/api/update");
  renderUpdate(s);
  if (s.progress && s.progress.state === "running" && !state.updating) followUpgrade(s.progress.version);
  // Check GitHub automatically (the server caches the answer for an hour).
  if (!s.checked_at) renderUpdate(await api("/api/update/check?force=false", { method: "POST" }));
}

$("#update-check").addEventListener("click", async () => {
  const btn = $("#update-check");
  btn.disabled = true;
  setUpdateBusy("Checking GitHub…");
  try { renderUpdate(await api("/api/update/check", { method: "POST" })); if (!state.update.error) setUpdateMsg("", true); }
  catch (ex) { setUpdateMsg(ex.message, false); }
  finally { btn.disabled = false; }
});

// ---- "Updating…" state: a bar under the top bar (all tabs) with a spinner and "See details".
function setUpdateBusy(text) {
  const m = $("#update-msg");
  m.innerHTML = '<span class="spinner" aria-hidden="true"></span> ';
  m.append(text);
  m.className = "small busy";
  m.hidden = false;
}

function showUpdating(text) {
  state.updating = true;
  $("#updating-bar").hidden = false;
  $("#updating-text").textContent = text;
  $("#update-apply").disabled = $("#update-check").disabled = true;
  setUpdateBusy(text);
}

function hideUpdating() {
  state.updating = false;
  $("#updating-bar").hidden = true;
  $("#update-apply").disabled = $("#update-check").disabled = false;
}

$("#updating-details").addEventListener("click", () => openConsole("update"));

$("#update-target").addEventListener("change", (e) => { $("#update-apply").textContent = `Update to ${e.target.value}`; });

// All published versions, each linked to its GitHub release page, with Install / Downgrade.
function renderReleases(s) {
  const rels = s.releases || [];
  const box = $("#update-releases");
  box.hidden = !rels.length || (rels.length === 1 && rels[0].source === "branch");
  $("#update-releases-count").textContent = rels.length ? `(${rels.length})` : "";
  const list = $("#update-release-list");
  list.innerHTML = "";
  for (const v of rels) {
    const link = el("a", { href: v.url, target: "_blank", rel: "noopener noreferrer", class: "release-name", "data-no-i18n": "" },
      v.name && v.name.replace(/^v/i, "") !== v.version ? `${v.version} · ${v.name}` : v.version);
    const tag = el("span", {}, v.status === "current" ? el("span", { class: "tag tag-accent" }, "Installed")
      : v.status === "newer" ? el("span", { class: "tag" }, "Newer") : el("span", { class: "tag muted" }, "Older"),
      v.prerelease ? " " : null, v.prerelease ? el("span", { class: "tag tag-warn", title: "Marked as a pre-release on GitHub" }, "pre-release") : null,
      v.source === "tag" ? " " : null, v.source === "tag" ? el("span", { class: "tag muted", title: "A version tag without a GitHub release" }, "tag only") : null);
    let btn = null;
    if (v.status !== "current" && s.can_apply) {
      btn = el("button", { type: "button", class: `small ${v.status === "older" ? "ghost" : "primary"}`,
        title: v.status === "older" ? "Go back to this version" : "Install this version",
        onclick: () => installVersion(v.version, v.status === "older") },
        v.status === "older" ? "Downgrade" : "Install");
    }
    list.append(el("div", { class: `release-row ${v.status}` }, link, tag,
      el("span", { class: "muted" }, v.published_at ? fmtDate(v.published_at) : ""), el("span", { class: "spacer" }), btn));
  }
}

$("#update-apply").addEventListener("click", () => {
  const s = state.update;
  installVersion($("#update-target").value || s.latest.version, false);
});

async function installVersion(target, downgrade) {
  const s = state.update;
  if (state.updating) return;
  const included = (s.pending || []).filter((v) => !downgrade && v.version !== target && compareVersions(v.version, target) < 0).length;
  const between = included ? ` (includes ${included + 1} versions)` : "";
  const intro = downgrade
    ? `Downgrade SysAdmin.IA from ${s.current} to ${target}?\n\n` +
      "Features added after that version disappear, but your settings, users and data are kept (the older version ignores what it doesn't know). " +
      "You can update again at any time.\n\n"
    : `Update SysAdmin.IA from ${s.current} to ${target}${between}?\n\n`;
  if (!confirm(intro +
      (s.full_upgrade ? "That version's installer runs exactly as a fresh install would (system packages, " +
        "service, helpers, app code); your settings, users and data are kept. " : "") +
      "The current code is backed up first and restored automatically if the update fails. " +
      "Changes Claude made to this app's own files may be overwritten (they stay in the app's git history).")) return;
  state.updateConsole = s.full_upgrade ? "log" : "local";
  resetUpdateConsole();
  showUpdating(`${downgrade ? "Downgrading" : "Updating"} to ${target}…`);
  if (state.updateConsole === "local") {
    updateConsoleWrite(`${downgrade ? "Downgrading" : "Updating"} ${s.current} -> ${target} (app code only)\r\nDownloading and installing…\r\n`);
  }
  try {
    const r = await api("/api/update/apply", { method: "POST", body: JSON.stringify({ version: target, downgrade }) });
    if (r.mode === "full") { followUpgrade(r.to); return; }
    updateConsoleWrite((r.log || []).join("\r\n") + "\r\n");
    if (r.restart) {
      $("#updating-text").textContent = `Updated to ${r.to}. Restarting…`;
      setUpdateBusy(`Updated to ${r.to}. Restarting…`);
      updateConsoleWrite("Restarting the app…\r\n");
      waitForVersion(r.to);
    } else {
      hideUpdating();
      setUpdateMsg(`Updated to ${r.to}. Restart the app to load the new version.`, true);
    }
  } catch (ex) {
    hideUpdating();
    updateConsoleWrite(`\r\n\x1b[31mUpdate failed: ${ex.message}\x1b[0m\r\n`);
    setUpdateMsg(ex.message, false);
  }
}
function compareVersions(a, b) {
  const pa = String(a).split(/[.-]/).map((x) => parseInt(x, 10) || 0), pb = String(b).split(/[.-]/).map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) - (pb[i] || 0);
  return 0;
}

// Follow a full upgrade (install.sh running as root in the background). The app restarts
// during it, so failed requests are expected for a few seconds.
function followUpgrade(target) {
  state.updateConsole = "log";
  showUpdating(`Updating to ${target}…`);
  const started = Date.now();
  const poll = async () => {
    try {
      const r = await fetch("/api/update/progress", { cache: "no-store" });
      if (r.status === 401) { location.href = "/login"; return; }
      if (r.ok) {
        const p = await r.json();
        if (p.state === "done" && p.current === target) {
          $("#updating-text").textContent = `Updated to ${target}. Reloading…`;
          setUpdateBusy(`Updated to ${target}. Reloading…`);
          setTimeout(() => location.reload(), 1500);
          return;
        }
        if (p.state === "failed") {
          hideUpdating();
          setUpdateMsg(p.message || "The update failed.", false);
          return;
        }
        const text = `Updating to ${target}: ${(p.message || "working").replace(/\.$/, "")}…`;
        $("#updating-text").textContent = text;
        setUpdateBusy(text);
      }
    } catch { /* app restarting */ }
    if (Date.now() - started < 30 * 60000) setTimeout(poll, 2000);
    else { hideUpdating(); setUpdateMsg("The update is taking unusually long; open the details to see where it is.", false); }
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
    else { hideUpdating(); setUpdateMsg("The app did not come back after the update; check the service logs.", false); }
  }, 2000);
}

// ------------------------------------------------------------ console
// Terminal sessions over /ws/console: "shell" (interactive) and "update" (follows the update
// log). For an app-code-only update there is no log file, so its output is written locally.
const consoles = {};
state.sshSessions = {};   // "ssh-N" → {host, port, user, label}: tabs opened from Network → Devices
const isInteractive = (name) => name === "shell" || name === "container" || name.startsWith("ssh-");

function makeConsole(name) {
  if (consoles[name]) return consoles[name];
  if (!$(`#console-term-${name}`)) $(".console").append(el("div", { class: "console-term", id: `console-term-${name}`, hidden: true }));
  const interactive = isInteractive(name);
  const term = new Terminal({
    cursorBlink: interactive, disableStdin: !interactive, scrollback: 5000, fontSize: 13,
    fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--mono").trim() || "monospace",
    theme: { background: "#111317", foreground: "#e6e8eb", cursor: "#e0793c", selectionBackground: "#3a4250" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($(`#console-term-${name}`));
  const c = { name, term, fit, ws: null };
  term.onData((d) => { if (c.ws && c.ws.readyState === 1) c.ws.send(JSON.stringify({ type: "input", data: d })); });
  term.onResize(({ cols, rows }) => {
    if (c.ws && c.ws.readyState === 1) c.ws.send(JSON.stringify({ type: "resize", cols, rows }));
  });
  consoles[name] = c;
  return c;
}

function connectConsole(name) {
  const c = makeConsole(name);
  if (c.ws && c.ws.readyState <= 1) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ssh = state.sshSessions[name];
  const extra = name === "container" ? `&id=${encodeURIComponent(state.consoleContainer?.id || "")}`
    : ssh ? `&host=${encodeURIComponent(ssh.host)}&port=${ssh.port}&user_name=${encodeURIComponent(ssh.user)}` : "";
  const ws = new WebSocket(`${proto}://${location.host}/ws/console?mode=${ssh ? "ssh" : name}${extra}`);
  ws.binaryType = "arraybuffer";
  c.ws = ws;
  if (name === "update") c.term.reset();  // the log is replayed from the start
  setConsoleStatus(name, "connecting…");
  ws.onopen = () => {
    setConsoleStatus(name, name === "shell" ? "connected" : name === "container"
      ? `connected to container “${state.consoleContainer?.name}”`
      : ssh ? `SSH to ${ssh.user}@${ssh.host}${ssh.port !== 22 ? `:${ssh.port}` : ""}` : "following the update log");
    ws.send(JSON.stringify({ type: "resize", cols: c.term.cols, rows: c.term.rows }));
  };
  ws.onmessage = (e) => {
    if (typeof e.data !== "string") { c.term.write(new Uint8Array(e.data)); return; }
    const m = JSON.parse(e.data);
    if (m.type === "exit") c.term.write(`\r\n\x1b[90m[session ended, exit code ${m.code}]\x1b[0m\r\n`);
    if (m.type === "error") c.term.write(`\r\n\x1b[31m${m.message}\x1b[0m\r\n`);
  };
  ws.onclose = (e) => {
    if (e.code === 4401) { location.href = "/login"; return; }
    setConsoleStatus(name, "disconnected");
    // During an update the app restarts: keep following the log.
    if (name === "update" && state.updating && state.updateConsole === "log") setTimeout(() => connectConsole("update"), 2000);
  };
}

function setConsoleStatus(name, text) {
  consoles[name] && (consoles[name].status = text);
  if (state.consoleSession === name) {
    $("#console-status").textContent = text;
    $("#console-reconnect").hidden = !(isInteractive(name) && text === "disconnected");
  }
}

function showConsoleSession(name) {
  if (name === "container" && !state.consoleContainer) name = "shell";
  if (name.startsWith("ssh-") && !state.sshSessions[name]) name = "shell";
  state.consoleSession = name;
  if (name === "update") $("#console-update-tab").hidden = false;
  $$("#console-sessions .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.session === name));
  $$(".console-term").forEach((t) => { t.hidden = t.id !== `console-term-${name}`; });
  const c = makeConsole(name);
  requestAnimationFrame(() => { c.fit.fit(); if (name !== "update") c.term.focus(); });
  if (name !== "update" || state.updateConsole !== "local") connectConsole(name);
  setConsoleStatus(name, name === "update" && state.updateConsole === "local" ? "update output" : (c.status || ""));
}

function openConsole(name) {
  switchView("console");
  showConsoleSession(name);
}

// A shell inside a Docker container (opened from the Containers module).
function openContainerConsole(id, name) {
  const c = makeConsole("container");
  if (state.consoleContainer?.id !== id) {
    if (c.ws) { c.ws.onclose = null; c.ws.close(); c.ws = null; }
    c.term.reset();
    c.status = "";
  }
  state.consoleContainer = { id, name };
  const tab = $("#console-container-tab");
  tab.hidden = false;
  tab.textContent = `Container: ${name}`;
  openConsole("container");
}

// An SSH session to a device, in a new tab (opened from Network → Devices). Each click opens a new tab.
function openSshConsole(host, port, user, label) {
  state.sshCount = (state.sshCount || 0) + 1;
  const name = `ssh-${state.sshCount}`;
  state.sshSessions[name] = { host, port: Number(port) || 22, user, label };
  const close = el("span", { class: "console-tab-close", role: "button", title: "Close this session", "aria-label": "Close this session",
    onclick: (e) => { e.stopPropagation(); closeSshConsole(name); } }, "×");
  const tab = el("button", { type: "button", class: "seg-btn", "data-session": name, title: `${user}@${host}:${port}`,
    onclick: () => showConsoleSession(name) }, el("span", { "data-no-i18n": "" }, `SSH: ${label || host}`), close);
  $("#console-sessions").append(tab);
  openConsole(name);
}

function closeSshConsole(name) {
  const c = consoles[name];
  if (c) { if (c.ws) { c.ws.onclose = null; c.ws.close(); } c.term.dispose(); delete consoles[name]; }
  $(`#console-term-${name}`)?.remove();
  $(`#console-sessions [data-session="${name}"]`)?.remove();
  delete state.sshSessions[name];
  if (state.consoleSession === name) showConsoleSession("shell");
}
window.openSshConsole = openSshConsole;

function resetUpdateConsole() {
  const c = makeConsole("update");
  if (c.ws) { c.ws.onclose = null; c.ws.close(); c.ws = null; }
  c.term.reset();
}

function updateConsoleWrite(text) { makeConsole("update").term.write(text); }

$$("#console-sessions .seg-btn").forEach((b) => b.addEventListener("click", () => showConsoleSession(b.dataset.session)));
$("#console-reconnect").addEventListener("click", () => {
  const name = isInteractive(state.consoleSession || "") ? state.consoleSession : "shell";
  const c = makeConsole(name);
  c.term.write("\r\n");
  connectConsole(name);
});
window.addEventListener("resize", () => {
  if (state.view === "console" && consoles[state.consoleSession]) consoles[state.consoleSession].fit.fit();
});

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
  loadUpdate().catch((e) => setUpdateMsg(e.message, false));
  loadTimezone().catch((e) => setFormMsg("#tz-msg", e.message, false));
  loadAudit();
}

// ---- server time zone
async function loadTimezone() {
  renderTimezone(await api("/api/server/timezone"));
}
function renderTimezone(s) {
  const form = $("#tz-form");
  form.timezone.value = s.timezone || "";
  form.timezone.disabled = !s.can_change;
  $("#tz-save").disabled = !s.can_change;
  $("#tz-list").innerHTML = s.zones.map((z) => `<option value="${esc(z)}">`).join("");
  const pill = $("#tz-status");
  pill.textContent = s.timezone ? `${s.timezone} (${s.utc_offset})` : "unknown";
  pill.className = "status-pill ok";
  $("#tz-now").textContent = `Server time now: ${s.now} · ${s.ntp_synced === true ? "synchronized with internet time (NTP)"
    : s.ntp_synced === false ? "not synchronized with internet time" : ""}`;
  // one-click: the browser's own time zone
  const mine = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const quick = $("#tz-quick");
  quick.innerHTML = "";
  if (mine && mine !== s.timezone && s.zones.includes(mine) && s.can_change)
    quick.append(el("button", { type: "button", class: "small ghost", onclick: () => { form.timezone.value = mine; form.requestSubmit(); } },
      withIcon("clock", `Use my time zone (${mine})`)));
  const setup = $("#tz-setup");
  setup.hidden = !s.setup_command;
  setup.textContent = s.setup_command ? `Changing it needs a one-time setup on the server. Run this as root, then reload:\n  ${s.setup_command}` : "";
  if (!s.linux) setFormMsg("#tz-msg", "The time zone can only be changed when the app runs on Linux.", false);
}
$("#tz-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const zone = e.target.timezone.value.trim();
  if (!confirm(`Change the server's time zone to ${zone}?\n\nThe clock, logs and job schedules switch to the new local time.`)) return;
  $("#tz-save").disabled = true;
  try {
    const r = await api("/api/server/timezone", { method: "PUT", body: JSON.stringify({ timezone: zone }) });
    renderTimezone(r.status);
    setFormMsg("#tz-msg", r.output || `Time zone set to ${r.timezone}.`, true);
  } catch (ex) { setFormMsg("#tz-msg", ex.message, false); }
  $("#tz-save").disabled = false;
});
async function loadAudit() {
  const rows = await api("/api/audit?limit=200").catch(() => []);
  $("#audit").innerHTML = rows.map((r) =>
    `<tr><td class="small nowrap">${new Date(r.ts * 1000).toLocaleString()}</td><td>${esc(r.username || "")}</td><td class="muted small">${esc(r.ip || "")}</td>
     <td><span class="tag">${esc(r.action)}</span></td><td class="small mono detail">${esc((r.detail || "").slice(0, 400))}</td></tr>`).join("");
}
$("#refresh-audit").addEventListener("click", loadAudit);

// ------------------------------------------------------------ drives
const TRANSPORT = { nvme: "NVMe", sata: "SATA", ata: "ATA", usb: "USB", mmc: "SD card", sdio: "SD card", scsi: "SCSI",
                    sas: "SAS", virtio: "Virtual", optical: "Optical", ieee1394: "FireWire" };

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k);
  return e;
}
function usageBar(pct) {
  const bar = el("div", { class: "bar" }, el("i"));
  setBar(bar.firstChild, pct);
  return bar;
}

function openDrives() {
  showDrivesSub(state.drivesSub || "drives");
}

function showDrivesSub(sub) {
  state.drivesSub = sub;
  $$("#drives-seg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.sub === sub));
  $("#drives-sub-drives").hidden = sub !== "drives";
  $("#drives-sub-files").hidden = sub !== "files";
  clearInterval(state.drivesTimer);
  if (sub === "drives") {
    loadDrives();
    state.drivesTimer = setInterval(() => { if (state.view === "drives" && !document.hidden) loadDrives(true); }, 5000);
  } else {
    if (!state.drives) loadDrives(true);
    browse(state.fb?.path || "/");
  }
}
$$("#drives-seg .seg-btn").forEach((b) => b.addEventListener("click", () => showDrivesSub(b.dataset.sub)));
$("#drives-refresh").addEventListener("click", () => loadDrives());

async function loadDrives(quiet = false) {
  try {
    const d = await api("/api/drives");
    state.drives = d;
    renderDrives(d);
    renderPlaces();
  } catch (ex) {
    if (!quiet) { $("#drive-list").innerHTML = ""; $("#drive-list").append(el("p", { class: "error" }, ex.message)); }
  }
}

function renderDrives(d) {
  const list = $("#drive-list"), tiles = $("#drive-tiles");
  const setup = $("#drives-setup");
  if (!d.available) {
    tiles.innerHTML = ""; list.innerHTML = "";
    list.append(el("p", { class: "muted" }, d.message));
    setup.hidden = true;
    return;
  }
  setup.hidden = d.can_manage;
  if (!d.can_manage) {
    setup.innerHTML = "";
    setup.append("Mounting and unmounting need a one-time setup on the server. Run this as root, then click Refresh: ",
                 el("code", {}, d.setup_command || ""));
  }
  const s = d.summary;
  const usedPct = s.mounted_total ? (100 * s.mounted_used / s.mounted_total) : 0;
  tiles.innerHTML = "";
  tiles.append(
    el("div", { class: "tile" }, el("div", { class: "label" }, "Drives"),
       el("div", { class: "value" }, String(s.disks)),
       el("div", { class: "sub" }, `${s.internal} internal · ${s.external} external`)),
    el("div", { class: "tile" }, el("div", { class: "label" }, "Total capacity"),
       el("div", { class: "value" }, fmtBytes(s.capacity)), el("div", { class: "sub" }, "all drives")),
    el("div", { class: "tile" }, el("div", { class: "label" }, "Used (mounted)"),
       el("div", { class: "value" }, `${usedPct.toFixed(0)}%`),
       el("div", { class: "sub" }, `${fmtBytes(s.mounted_used)} of ${fmtBytes(s.mounted_total)}`), usageBar(usedPct)),
    el("div", { class: "tile" }, el("div", { class: "label" }, "Free (mounted)"),
       el("div", { class: "value" }, fmtBytes(s.mounted_free)), el("div", { class: "sub" }, "available space")),
  );

  list.innerHTML = "";
  if (!d.disks.length) list.append(el("p", { class: "muted" }, "No drives found."));
  for (const disk of d.disks) list.append(driveCard(disk, d));
  $("#drives-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

function driveCard(disk, d) {
  const kind = disk.media === "optical" ? "optical" : disk.external ? "external" : "internal";
  const badges = [
    el("span", { class: `tag ${disk.external ? "tag-accent" : ""}` }, disk.external ? "External" : "Internal"),
    disk.transport ? el("span", { class: "tag" }, TRANSPORT[disk.transport] || disk.transport.toUpperCase()) : null,
    disk.media !== "optical" ? el("span", { class: "tag" }, disk.media.toUpperCase()) : null,
    disk.system ? el("span", { class: "tag" }, "System drive") : null,
    disk.readonly ? el("span", { class: "tag" }, "Read-only") : null,
  ];
  const io = disk.io;
  const ioLine = io ? el("div", { class: "drive-io small muted" },
    el("span", { title: "Read speed now" }, `↓ ${fmtBytes(io.read_rate, true)}`),
    el("span", { title: "Write speed now" }, `↑ ${fmtBytes(io.write_rate, true)}`),
    el("span", { title: "Since boot" }, `${fmtBytes(io.read_total)} read · ${fmtBytes(io.write_total)} written since boot`)) : null;

  const vols = el("div", { class: "volumes" });
  if (!disk.volumes.length) vols.append(el("div", { class: "volume muted small" }, "No partitions or filesystems on this drive."));
  for (const v of disk.volumes) vols.append(volumeRow(v, d));

  return el("div", { class: "card drive-card" },
    el("div", { class: "drive-head" },
      el("span", { class: `icon-tile ${disk.external ? "accent" : ""}`, html: icon(kind) }),
      el("div", { class: "drive-title" },
        el("div", {}, el("strong", {}, disk.model), " ", el("span", { class: "muted" }, fmtBytes(disk.size))),
        el("div", { class: "drive-badges" }, badges, el("span", { class: "muted small mono" }, disk.path))),
      ioLine),
    vols);
}

function volumeRow(v, d) {
  const title = v.label || (v.mountpoint && v.mountpoint !== "/" ? v.mountpoint.split("/").pop() : "") || v.name;
  const meta = [v.fstype ? v.fstype.toUpperCase() : "no filesystem", fmtBytes(v.size), v.path].join(" · ");
  const left = el("div", { class: "vol-name" }, el("strong", {}, title), el("div", { class: "muted small" }, meta));

  let middle;
  if (v.usage) {
    const free = el("div", { class: "small" }, `${fmtBytes(v.usage.free)} free of ${fmtBytes(v.usage.total)}`);
    middle = el("div", { class: "vol-usage" }, usageBar(v.usage.percent), free);
  } else {
    middle = el("div", { class: "vol-usage muted small" }, v.note || (v.can_mount ? "Mount it to see how full it is." : ""));
  }

  const status = v.mountpoints.length
    ? el("div", { class: "vol-status small" }, el("span", { class: "dot on" }), " ", v.swap ? "In use (swap)" : el("span", { class: "mono" }, v.mountpoint))
    : el("div", { class: "vol-status small muted" }, el("span", { class: "dot" }), " Not mounted");

  const actions = el("div", { class: "vol-actions" });
  const noManage = !d.can_manage ? "Needs the one-time setup shown above" : null;
  if (v.mountpoint && !v.swap) {
    actions.append(el("button", { type: "button", class: "small", onclick: () => { showDrivesSub("files"); browse(v.mountpoint); } }, withIcon("folder", "Open")));
  }
  if (v.can_unmount) {
    actions.append(el("button", { type: "button", class: "small", disabled: !!noManage, title: noManage,
                                  onclick: () => openUnmount(v) }, withIcon("logout", "Unmount")));
  } else if (v.mountpoints.length && v.system) {
    actions.append(el("span", { class: "tag", title: "Used by the operating system; can't be unmounted here" }, "System"));
  }
  if (v.can_mount) {
    actions.append(el("button", { type: "button", class: "small primary", disabled: !!noManage, title: noManage,
                                  onclick: () => openMount(v) }, withIcon("login", "Mount…")));
  }
  return el("div", { class: "volume" }, left, middle, status, actions);
}

// ---- mount / unmount dialogs
function openMount(v) {
  const dlg = $("#mount-dialog"), f = $("#mount-form");
  state.mountVol = v;
  $("#mount-title").textContent = `Mount ${v.label || v.name}`;
  $("#mount-desc").textContent = `${fmtBytes(v.size)} ${v.fstype.toUpperCase()} volume (${v.path}). Pick a name — its files will be at /mnt/<name>.`;
  f.name.value = v.default_name;
  f.persist.checked = false;
  f.readonly.checked = false;
  $("#mount-error").hidden = true;
  $("#mount-ok").disabled = false;
  dlg.showModal();
  f.name.select();
}
$("#mount-cancel").addEventListener("click", () => $("#mount-dialog").close());
$("#mount-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, v = state.mountVol, err = $("#mount-error");
  const name = f.name.value.trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(name)) {
    err.textContent = "Use letters, digits, dots, dashes or underscores (no spaces), starting with a letter or digit.";
    err.hidden = false; return;
  }
  $("#mount-ok").disabled = true;
  err.hidden = true;
  try {
    const r = await api("/api/drives/mount", { method: "POST", body: JSON.stringify({
      device: v.path, name, persist: f.persist.checked, readonly: f.readonly.checked }) });
    $("#mount-dialog").close();
    toast(`Mounted at ${r.mountpoint}`);
    loadDrives();
  } catch (ex) { err.textContent = ex.message; err.hidden = false; $("#mount-ok").disabled = false; }
});

function openUnmount(v) {
  state.unmountVol = v;
  $("#unmount-title").textContent = `Unmount ${v.label || v.mountpoint}`;
  $("#unmount-desc").textContent = `Its files at ${v.mountpoint} will no longer be available until it's mounted again. ` +
    "Close any files or console sessions using it first.";
  $("#unmount-form").forget.checked = true;
  $("#unmount-error").hidden = true;
  $("#unmount-ok").disabled = false;
  $("#unmount-dialog").showModal();
}
$("#unmount-cancel").addEventListener("click", () => $("#unmount-dialog").close());
$("#unmount-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = state.unmountVol, err = $("#unmount-error");
  $("#unmount-ok").disabled = true;
  try {
    const r = await api("/api/drives/unmount", { method: "POST", body: JSON.stringify({
      mountpoint: v.mountpoint, forget: e.target.forget.checked }) });
    $("#unmount-dialog").close();
    toast(r.output.includes("safe to remove") ? "Unmounted. It's now safe to remove the drive." : "Unmounted.");
    if (state.fb && state.fb.path.startsWith(v.mountpoint)) state.fb.path = "/";
    loadDrives();
  } catch (ex) { err.textContent = ex.message; err.hidden = false; $("#unmount-ok").disabled = false; }
});

// ---- file browser
function renderPlaces() {
  const box = $("#fb-places");
  if (!state.drives || !state.drives.available) { box.innerHTML = ""; return; }
  box.innerHTML = "";
  const seen = new Set();
  const add = (label, path, kind, sub) => {
    if (seen.has(path)) return;
    seen.add(path);
    box.append(el("button", { type: "button", class: `place ${state.fb && state.fb.path.startsWith(path) && (path !== "/" || state.fb.path === "/") ? "active" : ""}`,
                              onclick: () => browse(path), title: path },
      el("span", { class: "place-icon", html: icon(kind) }),
      el("span", { class: "place-text" }, el("strong", {}, label), el("span", { class: "muted small" }, sub))));
  };
  add("System", "/", "internal", "/");
  for (const disk of state.drives.disks) {
    for (const v of disk.volumes) {
      if (!v.mountpoint || v.swap || v.mountpoint === "/") continue;
      const kind = disk.external ? "external" : "internal";
      add(v.label || v.mountpoint.split("/").pop() || v.mountpoint, v.mountpoint, kind,
          v.usage ? `${fmtBytes(v.usage.free)} free` : v.mountpoint);
    }
  }
}

async function browse(path, opts = {}) {
  state.fb = state.fb || { hidden: false };
  const msg = $("#fb-msg");
  msg.hidden = true;
  let r;
  try { r = await api(`/api/files?path=${encodeURIComponent(path)}&hidden=${state.fb.hidden ? "true" : "false"}`); }
  catch (ex) { msg.textContent = ex.message; msg.className = "small error"; msg.hidden = false; return; }
  state.fb.path = r.path;
  state.fb.listing = r;
  if (!opts.keepSizes) state.fb.sizes = null;
  renderBrowser();
  renderPlaces();
}

function renderBrowser() {
  const r = state.fb.listing, sizes = state.fb.sizes;
  // breadcrumbs
  const crumbs = $("#fb-crumbs");
  crumbs.innerHTML = "";
  const parts = r.path.split("/").filter(Boolean);
  crumbs.append(el("button", { type: "button", class: "crumb", onclick: () => browse("/") }, "/"));
  parts.forEach((p, i) => {
    const to = "/" + parts.slice(0, i + 1).join("/");
    if (i) crumbs.append(el("span", { class: "crumb-sep" }, "/"));
    crumbs.append(el("button", { type: "button", class: "crumb", onclick: () => browse(to) }, p));
  });
  $("#fb-up").disabled = !r.parent;
  // usage of the filesystem this folder is on
  const u = r.usage, ub = $("#fb-usage");
  ub.innerHTML = "";
  if (u) ub.append(el("div", { class: "small" }, `This drive: ${fmtBytes(u.free)} free of ${fmtBytes(u.total)} (${u.percent.toFixed(0)}% used)`), usageBar(u.percent));
  // rows
  const tbody = $("#fb-rows");
  tbody.innerHTML = "";
  let entries = r.entries.slice();
  const sizeOf = (e) => (sizes && e.name in sizes.sizes ? sizes.sizes[e.name] : e.size);
  if (sizes) entries.sort((a, b) => (sizeOf(b) || 0) - (sizeOf(a) || 0));
  const max = sizes ? Math.max(1, ...entries.map((e) => sizeOf(e) || 0)) : 1;
  if (!entries.length) tbody.append(el("tr", {}, el("td", { colspan: "4", class: "muted small" }, "This folder is empty.")));
  for (const e of entries) {
    const full = (r.path === "/" ? "" : r.path) + "/" + e.name;
    const name = e.dir
      ? el("button", { type: "button", class: "fb-name link", onclick: () => browse(full) }, el("span", { class: "fb-icon dir", html: icon("folder") }), e.name)
      : el("span", { class: "fb-name" }, el("span", { class: "fb-icon", html: icon("file") }), e.name);
    if (e.link) name.append(el("span", { class: "muted small" }, " ↪"));
    const sz = sizeOf(e);
    const sizeCell = el("td", { class: "num nowrap" }, sz == null ? (e.dir ? "—" : "") : fmtBytes(sz));
    if (sizes && sz) sizeCell.prepend(el("span", { class: "size-bar", style: `width:${Math.max(2, 60 * sz / max)}px` }));
    const actions = el("td", { class: "num" });
    if (!e.dir) actions.append(el("a", { class: "small", href: `/api/files/download?path=${encodeURIComponent(full)}`, download: e.name }, withIcon("download", "Download")));
    tbody.append(el("tr", {}, el("td", {}, name), sizeCell,
      el("td", { class: "small muted nowrap hide-sm" }, e.mtime ? new Date(e.mtime * 1000).toLocaleString() : ""), actions));
  }
  const msg = $("#fb-msg");
  const notes = [];
  if (r.truncated) notes.push(`Showing the first ${r.entries.length} items.`);
  if (sizes && !sizes.complete) notes.push("Size analysis stopped early (too many files); sizes are partial.");
  msg.textContent = notes.join(" ");
  msg.className = "small muted";
  msg.hidden = !notes.length;
}

$("#fb-up").addEventListener("click", () => { if (state.fb?.listing?.parent) browse(state.fb.listing.parent); });
$("#fb-hidden").addEventListener("change", (e) => { state.fb.hidden = e.target.checked; browse(state.fb.path); });
$("#fb-sizes").addEventListener("click", async () => {
  const btn = $("#fb-sizes");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "Analyzing…";
  try {
    state.fb.sizes = await api(`/api/files/sizes?path=${encodeURIComponent(state.fb.path)}`, { method: "POST" });
    renderBrowser();
  } catch (ex) { toast(ex.message); }
  finally { btn.disabled = false; btn.textContent = old; }
});

// ------------------------------------------------------------ local AI (Ollama) module
const CAP_LABELS = { tools: "Tools", vision: "Vision", thinking: "Thinking", embedding: "Embeddings", insert: "Code fill" };

async function loadLocalAI() {
  clearTimeout(state.laiTimer);
  let s;
  try { s = await api("/api/localai/status"); } catch (ex) { toast(ex.message); return; }
  state.lai = s;
  renderLaiStatus(s);
  if (s.running) await loadLaiModels();
  const installing = s.install && s.install.state === "running";
  const pulling = (state.laiModels?.pulls || []).some((p) => p.state === "downloading");
  if (state.view === "localai") state.laiTimer = setTimeout(loadLocalAI, installing || pulling ? 2000 : 15000);
}

function renderLaiStatus(s) {
  $("#lai-tile").innerHTML = icon("chip");
  $("#lai-tile").className = `icon-tile ${s.running ? "accent" : ""}`;
  const setup = $("#lai-setup");
  setup.hidden = !s.available || s.can_manage;
  if (!setup.hidden) { setup.innerHTML = ""; setup.append("Installing and controlling Ollama needs a one-time setup on the server. Run this as root, then reload: ", el("code", {}, s.setup_command || "")); }
  const pill = $("#lai-pill");
  const inst = s.install || {};
  const installing = inst.state === "running";
  if (!s.available) { pill.textContent = "Linux only"; pill.className = "status-pill warn"; }
  else if (installing) { pill.textContent = "installing…"; pill.className = "status-pill warn"; }
  else if (s.running) { pill.textContent = "running"; pill.className = "status-pill ok"; }
  else if (s.installed) { pill.textContent = "stopped"; pill.className = "status-pill warn"; }
  else { pill.textContent = "not installed"; pill.className = "status-pill"; }
  $("#lai-version").textContent = !s.available ? "Local AI is only available when the app runs on Linux."
    : s.running ? `Version ${s.version} · listening on ${s.url} (this server only)`
    : s.installed ? "Installed, but the service isn't running." : "Not installed yet. Installing takes a few minutes and about 1–2 GB of disk.";

  const acts = $("#lai-actions");
  acts.innerHTML = "";
  const svc = (action, label, iconName) => el("button", { type: "button", class: "small", disabled: !s.can_manage, onclick: async (e) => {
    e.currentTarget.disabled = true;
    try { await api("/api/localai/service", { method: "POST", body: JSON.stringify({ action }) }); toast(`Ollama: ${label.toLowerCase()} done.`); }
    catch (ex) { toast(ex.message); }
    setTimeout(loadLocalAI, 1500);
  } }, withIcon(iconName, label));
  if (s.available && !s.installed && !installing) {
    acts.append(el("button", { type: "button", class: "primary", disabled: !s.can_manage, onclick: async (e) => {
      if (!confirm("Install Ollama on this server?\n\nThis downloads and runs Ollama's official installer (ollama.com/install.sh) as root. "
        + "It adds an “ollama” service and user, and GPU support if a GPU is found.")) return;
      e.currentTarget.disabled = true;
      try { await api("/api/localai/install", { method: "POST" }); } catch (ex) { toast(ex.message); }
      loadLocalAI();
    } }, withIcon("download", "Install Ollama")));
  } else if (s.installed && !installing) {
    if (s.running) acts.append(svc("restart", "Restart", "refresh"), svc("stop", "Stop", "stop"));
    else acts.append(svc("start", "Start", "play"));
  }

  const hw = $("#lai-hw");
  hw.innerHTML = "";
  if (s.available) {
    const stat = (label, value, sub) => el("div", { class: "u-stat" }, el("div", { class: "label" }, label), el("div", { class: "u-value" }, value), el("div", { class: "muted small" }, sub));
    hw.append(el("div", { class: "u-stats" },
      stat("Memory", fmtBytes(s.ram_total), `${fmtBytes(s.ram_available)} free now`),
      stat("Graphics", s.gpus.length ? `${s.gpus.length} GPU${s.gpus.length > 1 ? "s" : ""}` : "CPU only",
           s.gpus.length ? s.gpus.join(", ") : "No GPU found: models run on the processor (slower)"),
      stat("Disk for models", s.disk_free != null ? fmtBytes(s.disk_free) : "–", s.models_dir ? `free in ${s.models_dir}` : "free space")));
  }
  $("#lai-install").hidden = !(installing || inst.state === "failed" || (inst.state === "done" && !s.running));
  if (!$("#lai-install").hidden) {
    const msg = $("#lai-install-msg");
    msg.innerHTML = installing ? '<span class="spinner" aria-hidden="true"></span> ' : "";
    msg.append(inst.message || "");
    msg.className = `small ${installing ? "busy" : inst.state === "failed" ? "error" : "ok-text"}`;
    // One progress bar instead of the installer's terminal progress output.
    const prog = $("#lai-install-progress");
    const pct = inst.progress;
    prog.hidden = pct == null || inst.state === "failed";
    if (!prog.hidden) {
      $("#lai-install-stage").textContent = inst.stage ? `${inst.stage.replace(/\.+$/, "")}…` : "Downloading…";
      $("#lai-install-pct").textContent = `${Math.round(pct)}%`;
      $("#lai-install-bar").style.width = `${pct}%`;
    }
    const log = $("#lai-install-log");
    log.textContent = inst.log || "";
    log.scrollTop = log.scrollHeight;
    if (inst.state === "failed") $("#lai-install-details").open = true;
  }
  $("#lai-context").hidden = !s.installed;
  const csel = $("#lai-context-select");
  csel.innerHTML = s.context_choices.map((n) => `<option value="${n}">${n >= 1024 ? n / 1024 + "k" : n} tokens</option>`).join("");
  csel.value = String(s.context_length || 4096);
  $("#lai-context-apply").disabled = !s.can_manage;
  $("#lai-models-card").hidden = !s.running;
  $("#lai-get-card").hidden = !s.running;
  $("#lai-danger").hidden = !s.installed || !s.can_manage || installing;
}

async function loadLaiModels() {
  let r;
  try { r = await api("/api/localai/models"); }
  catch (ex) { $("#lai-models").textContent = ex.message; return; }
  state.laiModels = r;
  // downloads in progress
  const pulls = $("#lai-pulls");
  pulls.innerHTML = "";
  for (const p of r.pulls) {
    const pct = p.total ? Math.round(100 * p.completed / p.total) : 0;
    const done = p.state === "done", failed = p.state === "failed";
    pulls.append(el("div", { class: "lai-pull" },
      el("div", { class: "small" }, el("strong", {}, p.name), " · ",
        el("span", { class: failed ? "error" : done ? "ok-text" : "muted" },
          failed ? `Failed: ${p.error}` : done ? "Downloaded" : `${p.status}${p.total ? ` — ${fmtBytes(p.completed)} of ${fmtBytes(p.total)} (${pct}%)` : ""}`),
        p.state === "downloading" ? el("button", { type: "button", class: "small ghost", onclick: async () => {
          await api("/api/localai/pull/cancel", { method: "POST", body: JSON.stringify({ name: p.name }) }).catch(() => {});
        } }, "Cancel") : null),
      p.state === "downloading" ? usageBar(pct) : null));
  }
  // installed models
  const box = $("#lai-models");
  box.innerHTML = "";
  if (!r.models.length) box.append(el("p", { class: "muted small" }, "No models yet. Download one below — Qwen 3 8B or Llama 3.1 8B are good starting points."));
  else box.append(el("div", { class: "lai-models-head", "aria-hidden": "true" },
    el("span", {}, "Model"), el("span", {}, "Size & abilities"), el("span", {}, "Memory"), el("span", {}, "")));
  for (const m of r.models) {
    const caps = m.capabilities.filter((c) => CAP_LABELS[c]).map((c) => el("span", { class: `tag ${c === "tools" ? "tag-accent" : ""}` }, CAP_LABELS[c]));
    const actions = el("div", { class: "var-actions" },
      m.connection
        ? el("span", { class: "small muted", title: "Already in AI connections" }, `In chat as “${m.connection}”`)
        : el("button", { type: "button", class: "small", title: m.capabilities.includes("tools") ? "" : "This model can't use tools: it can answer questions but not run commands",
            onclick: async () => {
              try { const c = await api("/api/localai/use", { method: "POST", body: JSON.stringify({ name: m.name }) });
                    toast(`Added to AI connections as “${c.name}”.`); loadChatConnections(); loadLaiModels(); }
              catch (ex) { toast(ex.message); }
            } }, withIcon("chat", "Use in chat")),
      m.loaded ? el("button", { type: "button", class: "small ghost", title: "Free the memory it uses", onclick: async () => {
        try { await api("/api/localai/unload", { method: "POST", body: JSON.stringify({ name: m.name }) }); loadLaiModels(); } catch (ex) { toast(ex.message); }
      } }, withIcon("pause", "Unload")) : null,
      el("button", { type: "button", class: "small ghost danger", onclick: async () => {
        if (!confirm(`Delete ${m.name} (${fmtBytes(m.size)})?${m.connection ? `\n\nThe AI connection “${m.connection}” will stop working.` : ""}`)) return;
        try { await api("/api/localai/delete", { method: "POST", body: JSON.stringify({ name: m.name }) }); loadLaiModels(); } catch (ex) { toast(ex.message); }
      } }, withIcon("trash", "Delete")));
    box.append(el("div", { class: "var-row lai-model" },
      el("div", { class: "var-name" }, el("strong", { class: "mono" }, m.name)),
      el("div", { class: "small" }, [m.parameters, m.quantization, fmtBytes(m.size)].filter(Boolean).join(" · "), " ", caps),
      el("div", { class: "small" }, m.loaded
        ? el("span", { class: "ok-text" }, `● Loaded · ${fmtBytes(m.memory)}${m.vram ? ` (${Math.round(100 * m.vram / m.memory)}% on GPU)` : " on CPU"}`)
        : el("span", { class: "muted" }, "Not loaded (starts on first use)")),
      actions));
  }
  // Re-run the model search when a download finishes (so it shows as installed), else just
  // redraw the cards with the current downloads.
  const finished = r.pulls.filter((p) => p.state === "done").length;
  if (!state.laiResults || finished !== state.laiFinished) { state.laiFinished = finished; laiSearch(); }
  else renderLaiResults();
}

// ---- Find models: Recommended / whole Ollama library / Hugging Face, with filters.
state.laiQuery = { source: "recommended", q: "", use: "", params: "", sort: "", fits: false };

async function laiSearch(more = false) {
  const qy = state.laiQuery;
  const offset = more && state.laiResults ? state.laiResults.models.length : 0;
  const params = new URLSearchParams({ ...qy, fits: qy.fits ? "true" : "false", offset: String(offset) });
  const seq = (state.laiSeq = (state.laiSeq || 0) + 1);
  $("#lai-results-info").textContent = qy.source === "recommended" ? "" :
    `Searching ${qy.source === "hf" ? "Hugging Face" : "the Ollama library"}…`;
  let r;
  try { r = await api(`/api/localai/search?${params}`); }
  catch (ex) { if (seq === state.laiSeq) $("#lai-results-info").textContent = ex.message; return; }
  if (seq !== state.laiSeq) return;   // a newer search was started meanwhile
  if (more) r.models = [...state.laiResults.models, ...r.models];
  state.laiResults = r;
  laiFillFilters(r.options);
  renderLaiResults();
}

function laiFillFilters(o) {
  const qy = state.laiQuery;
  const src = $("#lai-src");
  src.innerHTML = "";
  for (const [id, label] of o.sources)
    src.append(el("button", { type: "button", role: "tab", class: `seg-btn ${qy.source === id ? "active" : ""}`,
      onclick: () => { if (qy.source !== id) { qy.source = id; qy.sort = ""; laiSearch(); } } }, label));
  const fill = (sel, first, items, value) => {
    if (sel.options.length <= 1 || sel.dataset.filled !== String(items.length)) {
      sel.innerHTML = "";
      if (first) sel.append(el("option", { value: "" }, first));
      for (const [id, label] of items) sel.append(el("option", { value: id }, label));
      sel.dataset.filled = String(items.length);
    }
    sel.value = value;
  };
  fill($("#lai-use"), "Any use", o.uses, qy.use);
  fill($("#lai-params"), "Any size", o.params, qy.params);
  fill($("#lai-sort"), "Default order", o.sorts, qy.sort);
  $("#lai-fits").checked = qy.fits;
}

function renderLaiResults() {
  const r = state.laiResults;
  if (!r) return;
  const box = $("#lai-catalog");
  box.innerHTML = "";
  const downloading = new Set((state.laiModels?.pulls || []).filter((p) => p.state === "downloading").map((p) => p.name));
  const where = { recommended: "recommended models", ollama: "models in the Ollama library", hf: "GGUF models on Hugging Face" }[r.source];
  $("#lai-results-info").textContent = r.error && !r.models.length ? r.error
    : `${r.total} ${where}${r.total > r.models.length ? ` · showing ${r.models.length}` : ""}` +
      (r.fetched && r.source === "ollama" ? ` · list from ollama.com, ${timeAgoShort(r.fetched)}` : "") +
      (r.error ? ` · ${r.error}` : "");
  if (!r.models.length && !r.error) box.append(el("p", { class: "muted small" }, "No models match these filters."));
  for (const m of r.models) box.append(laiPick(m, downloading));
  $("#lai-more").hidden = !r.more;
  const notes = $("#lai-notes");
  notes.innerHTML = "";
  for (const n of r.notes || []) notes.append(el("p", {}, n));
}

function timeAgoShort(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
}

const LAI_BADGES = [["thinking", "Thinking"], ["vision", "Vision"], ["code", "Coding"], ["embedding", "Embeddings"],
                    ["audio", "Audio"], ["image", "Image generation"]];

function laiPick(m, downloading) {
  const badges = [
    m.tools === true ? el("span", { class: "tag tag-accent", title: "Can run commands from the chat" }, "Tools")
      : m.tools === null ? el("span", { class: "tag", title: "Shown under Installed models after downloading" }, "Tools: check after download")
      : (!m.embedding && !m.image ? el("span", { class: "tag" }, "No tools") : null),
    ...LAI_BADGES.filter(([k]) => m[k]).map(([, label]) => el("span", { class: "tag" }, label)),
    m.experimental ? el("span", { class: "tag tag-warn" }, "Experimental") : null,
    m.cloud ? el("span", { class: "tag", title: "Also offered as a cloud model on ollama.com; the sizes listed here run on this server" }, "Also in cloud") : null,
  ];
  // sizes: one line for a single variant, a picker for several (8B, 70B, 405B…)
  const vText = (v) => {
    const parts = [v.label && v.label !== "latest" ? v.label : null];
    if (v.size != null) parts.push(`${v.estimated ? "≈" : ""}${v.size < 1 ? `${Math.round(v.size * 1000)} MB` : `${v.size} GB`} download`);
    if (v.ram != null) parts.push(`${v.estimated ? "≈" : ""}${v.ram} GB RAM`);
    if (v.size == null) parts.push("size: see the model page");
    return parts.filter(Boolean).join(" · ") + (v.fits === false ? " — too big for this server" : "") + (v.installed ? " ✓" : "");
  };
  const vs = m.variants;
  let pick = vs.find((v) => !v.installed && v.fits !== false) || vs[0];
  let sizeEl, foot;
  const busy = () => downloading.has(pick.tag);
  const btn = el("button", { type: "button", class: "small" });
  const refresh = () => {
    btn.hidden = pick.installed;
    btn.disabled = busy();
    btn.innerHTML = "";
    btn.append(withIcon("download", busy() ? "Downloading…" : "Download"));
    btn.title = `ollama pull ${pick.tag}`;
    btn.onclick = () => pullModel(pick.tag);
    inst.hidden = !pick.installed;
    if (sizeEl.tagName === "DIV") sizeEl.className = `small ${pick.fits === false ? "error" : "muted"}`;
  };
  const inst = el("span", { class: "ok-text" }, "✓ Installed");
  if (vs.length > 1) {
    sizeEl = el("select", { class: "small lai-variant", "aria-label": `Size of ${m.title}`,
                            onchange: (e) => { pick = vs[Number(e.target.value)]; refresh(); } },
      vs.map((v, i) => el("option", { value: String(i) }, vText(v))));
    sizeEl.value = String(vs.indexOf(pick));
  } else {
    sizeEl = el("div", {}, vText(pick));
  }
  const stats = [m.popularity_label, m.likes ? `${m.likes} likes` : null, m.updated ? `updated ${new Date(m.updated).toLocaleDateString()}` : null]
    .filter(Boolean).join(" · ");
  foot = el("div", { class: "lai-pick-foot small" },
    el("a", { href: m.url, target: "_blank", rel: "noopener noreferrer", class: "muted" }, m.source), inst, btn);
  refresh();
  return el("div", { class: `lai-pick ${m.installed ? "installed" : ""}` },
    el("div", { class: "lai-pick-head" }, el("strong", {}, m.title)),
    el("div", { class: "muted small mono lai-name" }, vs.length > 1 ? m.name : pick.tag),
    el("div", { class: "small lai-badges" }, badges),
    el("p", { class: "small lai-use" }, m.use),
    m.about ? el("p", { class: "small muted lai-about" }, m.about) : null,
    stats ? el("div", { class: "small muted" }, stats) : null,
    sizeEl, foot);
}

$("#lai-q").addEventListener("input", (e) => {
  clearTimeout(state.laiQTimer);
  state.laiQTimer = setTimeout(() => { state.laiQuery.q = e.target.value.trim(); laiSearch(); }, 450);
});
for (const [id, key] of [["#lai-use", "use"], ["#lai-params", "params"], ["#lai-sort", "sort"]])
  $(id).addEventListener("change", (e) => { state.laiQuery[key] = e.target.value; laiSearch(); });
$("#lai-fits").addEventListener("change", (e) => { state.laiQuery.fits = e.target.checked; laiSearch(); });
$("#lai-more").addEventListener("click", () => laiSearch(true));

async function pullModel(name) {
  try { await api("/api/localai/pull", { method: "POST", body: JSON.stringify({ name }) }); toast(`Downloading ${name}…`); }
  catch (ex) { toast(ex.message); }
  loadLocalAI();
}
$("#lai-pull-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const name = $("#lai-pull-name").value.trim();
  if (name) { pullModel(name); $("#lai-pull-name").value = ""; }
});
$("#lai-context-apply").addEventListener("click", async () => {
  const n = Number($("#lai-context-select").value);
  try { const r = await api("/api/localai/context", { method: "POST", body: JSON.stringify({ context_length: n }) }); toast(r.message); }
  catch (ex) { toast(ex.message); }
  loadLocalAI();
});
$("#lai-uninstall").addEventListener("click", async () => {
  const del = $("#lai-delete-models").checked;
  if (!confirm(`Remove Ollama from this server?${del ? "\n\nAll downloaded models will be deleted too." : ""}`)) return;
  try { const r = await api("/api/localai/uninstall", { method: "POST", body: JSON.stringify({ delete_models: del }) }); toast(r.message); }
  catch (ex) { toast(ex.message); }
  loadLocalAI();
});

// ------------------------------------------------------------ variables module
const ph = (name) => `{{${name}}}`;
async function loadVariables() {
  let r;
  try { r = await api("/api/variables"); } catch (ex) { $("#var-list").textContent = ex.message; return; }
  state.variables = r.variables;
  const list = $("#var-list");
  list.innerHTML = "";
  if (!r.variables.length) list.append(el("p", { class: "muted small" }, "No variables yet. Create one, e.g. alert_email = you@example.com."));
  for (const v of r.variables) {
    list.append(el("div", { class: "var-row" },
      el("div", { class: "var-name" }, el("code", { class: "copyable", title: "Click to copy", onclick: () => copyText(ph(`var.${v.name}`)) }, ph(`var.${v.name}`)),
        v.secret ? el("span", { class: "tag" }, "Secret") : null),
      el("div", { class: "var-value mono small" }, v.secret ? (v.value_set ? "••••••••" : el("span", { class: "muted" }, "(empty)")) : (v.value || el("span", { class: "muted" }, "(empty)"))),
      el("div", { class: "muted small var-desc" }, v.description),
      el("div", { class: "var-actions" },
        el("button", { type: "button", class: "small", onclick: () => openVariable(v) }, withIcon("edit", "Edit")),
        el("button", { type: "button", class: "small ghost danger", onclick: async () => {
          if (!confirm(`Delete the variable ${v.name}? Jobs that use {{var.${v.name}}} will show it unreplaced.`)) return;
          try {
            const res = await api(`/api/variables/${encodeURIComponent(v.name)}`, { method: "DELETE" });
            toast(res.used_by.length ? `Deleted. Check these jobs: ${res.used_by.join(", ")}` : "Variable deleted.");
            loadVariables();
          } catch (ex) { toast(ex.message); }
        } }, withIcon("trash", "Delete")))));
  }
  renderMonitorValues(r.monitor);
  clearInterval(state.monTimer);
  state.monTimer = setInterval(async () => {
    if (state.view !== "variables" || document.hidden) return;
    try { renderMonitorValues(await api("/api/variables/monitor")); } catch { /* keep the last values */ }
  }, 5000);
  const b = $("#var-builtins");
  b.innerHTML = "";
  const labels = { hostname: "Server name", ip: "Server IP address", os: "Operating system", date: "Today's date",
                   time: "Current time", datetime: "Date and time", weekday: "Day of the week", version: "SysAdmin.IA version", url: "Address of this app" };
  for (const [k, val] of Object.entries(r.builtins)) {
    b.append(el("div", { class: "var-row" },
      el("div", { class: "var-name" }, el("code", { class: "copyable", title: "Click to copy", onclick: () => copyText(ph(`sys.${k}`)) }, ph(`sys.${k}`))),
      el("div", { class: "var-value mono small" }, String(val)),
      el("div", { class: "muted small var-desc" }, labels[k] || ""), el("div")));
  }
}
// Monitor values, grouped (CPU & load, Memory, Disks, Temperatures, …). Rows are reused on refresh
// so an open group and the scroll position stay put.
function renderMonitorValues(list) {
  const box = $("#var-monitor");
  $("#mon-updated").textContent = `· updated ${new Date().toLocaleTimeString()}`;
  const groups = {};
  for (const m of list || []) (groups[m.group] ||= []).push(m);
  const known = new Set(Object.keys(groups));
  box.querySelectorAll(":scope > details").forEach((d) => { if (!known.has(d.dataset.group)) d.remove(); });
  for (const [g, items] of Object.entries(groups)) {
    let det = box.querySelector(`:scope > details[data-group="${CSS.escape(g)}"]`);
    if (!det) {
      det = el("details", { class: "mon-group", open: ["CPU & load", "Memory", "Disks", "Temperatures"].includes(g) },
        el("summary", {}, g), el("div", { class: "var-list" }));
      det.dataset.group = g;
      box.append(det);
    }
    const rows = det.querySelector(".var-list");
    rows.innerHTML = "";
    for (const m of items) {
      const name = `mon.${m.key}`;
      rows.append(el("div", { class: "var-row mon-row" },
        el("div", { class: "var-name" }, el("code", { class: "copyable", title: "Click to copy", onclick: () => copyText(ph(name)) }, ph(name))),
        el("div", { class: "var-value mono small" }, String(m.value)),
        el("div", { class: "muted small var-desc" }, m.label), el("div")));
    }
  }
  if (!list || !list.length) box.textContent = "No measurements available.";
}

function copyText(t) {
  navigator.clipboard?.writeText(t).then(() => toast(`Copied ${t}`), () => toast(t));
}
function openVariable(v = null) {
  const f = $("#var-form");
  state.varEditing = v;
  $("#var-title").textContent = v ? `Edit ${v.name}` : "New variable";
  f.name.value = v ? v.name : "";
  f.value.value = v && !v.secret ? v.value : "";
  f.value.placeholder = v && v.secret && v.value_set ? "saved — leave empty to keep it" : "";
  f.description.value = v ? v.description : "";
  f.secret.checked = !!(v && v.secret);
  $("#var-preview").textContent = ph(`var.${f.name.value || "name"}`);
  $("#var-error").hidden = true;
  $("#var-dialog").showModal();
  f.name.focus();
}
$("#var-add").addEventListener("click", () => openVariable());
$("#var-cancel").addEventListener("click", () => $("#var-dialog").close());
$("#var-form").name.addEventListener("input", (e) => { $("#var-preview").textContent = ph(`var.${e.target.value || "name"}`); });
$("#var-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, v = state.varEditing;
  const body = { name: f.name.value.trim(), value: f.value.value, description: f.description.value, secret: f.secret.checked };
  try {
    await api(v ? `/api/variables/${encodeURIComponent(v.name)}` : "/api/variables", { method: v ? "PUT" : "POST", body: JSON.stringify(body) });
    $("#var-dialog").close();
    loadVariables();
  } catch (ex) { $("#var-error").textContent = ex.message; $("#var-error").hidden = false; }
});

// ------------------------------------------------------------ mail accounts module
async function loadMail() {
  let r;
  try { r = await api("/api/mail/accounts"); } catch (ex) { $("#mail-list").textContent = ex.message; return; }
  state.mailPresets = r.presets;
  const list = $("#mail-list");
  list.innerHTML = "";
  if (!r.accounts.length) {
    list.append(el("div", { class: "card empty-card" }, el("strong", {}, "No mail accounts yet"),
      el("p", { class: "muted small" }, "Add one so jobs can email you warnings, or react to messages that arrive."),
      el("button", { type: "button", class: "primary", onclick: () => openMail() }, withIcon("plus", "Add account"))));
  }
  for (const a of r.accounts) {
    const result = el("p", { class: "small", hidden: true });
    list.append(el("div", { class: "card ai-card" },
      el("div", { class: "ai-card-head" },
        el("span", { class: "icon-tile", html: icon("mail") }),
        el("div", { class: "ai-card-title" },
          el("div", {}, el("strong", {}, a.name), " ",
             a.can_send ? el("span", { class: "tag" }, "Sends") : null, " ", a.can_read ? el("span", { class: "tag" }, "Reads") : null),
          el("div", { class: "muted small" }, `${a.config.from_address || a.config.username} · ${a.preset_label}`),
          el("div", { class: "muted small" }, a.used_by.length ? `Used by: ${a.used_by.join(", ")}` : "Not used by any job yet")),
        el("div", { class: "ai-actions" },
          el("button", { type: "button", class: "small", onclick: async (e) => {
            const b = e.currentTarget; b.disabled = true;
            result.hidden = false; result.className = "small muted"; result.textContent = "Testing…";
            try { const t = await api("/api/mail/test", { method: "POST", body: JSON.stringify({ id: a.id }) });
                  result.className = `small ${t.ok ? "ok-text" : "error"}`; result.textContent = t.message; }
            catch (ex) { result.className = "small error"; result.textContent = ex.message; }
            b.disabled = false;
          } }, withIcon("test", "Test")),
          el("button", { type: "button", class: "small", onclick: () => openMail(a) }, withIcon("edit", "Edit")),
          el("button", { type: "button", class: "small ghost danger", onclick: async () => {
            if (!confirm(`Delete the mail account “${a.name}”? Its saved password is deleted too.`)) return;
            try { await api(`/api/mail/accounts/${a.id}`, { method: "DELETE" }); loadMail(); } catch (ex) { toast(ex.message); }
          } }, withIcon("trash", "Delete")))),
      result));
  }
}
function fillMailPreset(key, keepServers = false) {
  const p = state.mailPresets[key] || state.mailPresets.custom, f = $("#mail-form");
  $("#mail-preset-help").textContent = p.help || "";
  if (keepServers) return;
  for (const k of ["smtp_host", "smtp_port", "smtp_security", "imap_host", "imap_port", "imap_security"]) f[k].value = p[k] ?? "";
  $("#mail-advanced").open = key === "custom";
}
async function openMail(a = null) {
  if (!state.mailPresets) await loadMail();
  const f = $("#mail-form");
  state.mailEditing = a;
  $("#mail-title").textContent = a ? `Edit “${a.name}”` : "Add mail account";
  const sel = $("#mail-preset");
  sel.innerHTML = "";
  for (const [k, p] of Object.entries(state.mailPresets)) sel.append(el("option", { value: k }, p.label));
  const c = a ? a.config : {};
  sel.value = c.preset || "gmail";
  f.name.value = a ? a.name : "";
  for (const k of ["from_address", "from_name", "username", "imap_folder"]) f[k].value = c[k] || "";
  f.password.value = "";
  f.password.placeholder = a && a.password_set ? "saved — leave empty to keep it" : "app password recommended";
  f.test_to.value = "";
  if (a) {
    for (const k of ["smtp_host", "smtp_port", "smtp_security", "imap_host", "imap_port", "imap_security"]) f[k].value = c[k] ?? "";
    fillMailPreset(sel.value, true);
    $("#mail-advanced").open = false;
  } else fillMailPreset(sel.value);
  setFormMsg("#mail-msg", "");
  $("#mail-dialog").showModal();
}
$("#mail-preset").addEventListener("change", (e) => fillMailPreset(e.target.value));
$("#mail-add").addEventListener("click", () => openMail());
$("#mail-cancel").addEventListener("click", () => $("#mail-dialog").close());
function mailBody() {
  const f = $("#mail-form");
  const config = { preset: f.preset.value };
  for (const k of ["from_address", "from_name", "username", "smtp_host", "smtp_port", "smtp_security", "imap_host", "imap_port", "imap_security", "imap_folder"])
    config[k] = f[k].value.trim();
  if (!config.username) config.username = config.from_address;
  return { name: f.name.value.trim(), config, password: f.password.value || null };
}
$("#mail-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const a = state.mailEditing;
  try {
    await api(a ? `/api/mail/accounts/${a.id}` : "/api/mail/accounts", { method: a ? "PUT" : "POST", body: JSON.stringify(mailBody()) });
    $("#mail-dialog").close();
    loadMail();
  } catch (ex) { setFormMsg("#mail-msg", ex.message, false); }
});
$("#mail-test").addEventListener("click", async () => {
  const b = $("#mail-test");
  b.disabled = true;
  setFormMsg("#mail-msg", "Testing…");
  try {
    const r = await api("/api/mail/test", { method: "POST", body: JSON.stringify({
      id: state.mailEditing?.id || null, draft: mailBody(), send_to: $("#mail-form").test_to.value.trim() }) });
    setFormMsg("#mail-msg", r.message, r.ok);
  } catch (ex) { setFormMsg("#mail-msg", ex.message, false); }
  b.disabled = false;
});

// ------------------------------------------------------------ jobs module
function fmtWhen(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay ? `today ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

async function loadJobs() {
  const list = $("#job-list");
  let jobsList;
  try { jobsList = await api("/api/jobs"); } catch (ex) { list.textContent = ex.message; return; }
  list.innerHTML = "";
  if (!jobsList.length) {
    list.append(el("div", { class: "card empty-card" }, el("strong", {}, "No jobs yet"),
      el("p", { class: "muted small" }, "For example: email me when a disk is over 90% full, check for updates every Monday, or ask the AI to explain a failed service."),
      el("button", { type: "button", class: "primary", onclick: () => openJobEditor() }, withIcon("plus", "New job"))));
  }
  for (const j of jobsList) list.append(jobCard(j));
}

function jobCard(j) {
  const tags = [
    j.enabled ? null : el("span", { class: "tag" }, "Paused"),
    j.running ? el("span", { class: "tag tag-accent" }, "Running") : null,
    j.alerting ? el("span", { class: "tag tag-warn" }, "Alert active") : null,
  ];
  const last = j.last_run
    ? el("span", { class: j.last_status === "ok" ? "ok-text" : "error" }, `${j.last_status === "ok" ? "✓" : "✗"} Last run ${fmtWhen(j.last_run)}`)
    : el("span", { class: "muted" }, "Never run yet");
  const history = el("div", { class: "job-history", hidden: true });
  const card = el("div", { class: `card job-card ${j.enabled ? "" : "disabled"}` },
    el("div", { class: "ai-card-head" },
      el("span", { class: `icon-tile ${j.alerting ? "accent" : ""}`, html: icon("clock") }),
      el("div", { class: "ai-card-title" },
        el("div", {}, el("strong", {}, j.name), " ", tags),
        j.description ? el("div", { class: "muted small" }, j.description) : null),
      el("div", { class: "ai-actions" },
        el("button", { type: "button", class: "small", title: "Run the actions now, as a test", onclick: async (e) => {
          const b = e.currentTarget; b.disabled = true;
          try { const r = await api(`/api/jobs/${j.id}/run`, { method: "POST" });
                toast(r.status === "ok" ? "Job ran successfully." : "The job ran with errors — see History.");
                loadJobs(); }
          catch (ex) { toast(ex.message); b.disabled = false; }
        } }, withIcon("play", "Run now")),
        el("button", { type: "button", class: "small", onclick: () => openJobEditor(j) }, withIcon("edit", "Edit")),
        el("button", { type: "button", class: "small ghost", onclick: async () => {
          history.hidden = !history.hidden;
          if (!history.hidden) renderJobHistory(j.id, history);
        } }, withIcon("history", "History")),
        el("button", { type: "button", class: "small ghost", onclick: async () => {
          try { await api(`/api/jobs/${j.id}/enabled`, { method: "POST", body: JSON.stringify({ enabled: !j.enabled }) }); loadJobs(); }
          catch (ex) { toast(ex.message); }
        } }, j.enabled ? withIcon("pause", "Pause") : withIcon("play", "Resume")),
        el("button", { type: "button", class: "small ghost danger", onclick: async () => {
          if (!confirm(`Delete the job “${j.name}” and its history?`)) return;
          try { await api(`/api/jobs/${j.id}`, { method: "DELETE" }); loadJobs(); } catch (ex) { toast(ex.message); }
        } }, withIcon("trash", "Delete")))),
    el("div", { class: "job-flow" },
      el("div", { class: "flow-col" }, el("div", { class: "flow-label" }, "When"),
        el("ul", {}, j.trigger_summaries.map((s) => el("li", {}, s)))),
      el("div", { class: "flow-col" }, el("div", { class: "flow-label" }, "Then"),
        el("ol", {}, j.action_summaries.map((s) => el("li", {}, s))))),
    el("div", { class: "job-foot small" }, last,
      j.next_run ? el("span", { class: "muted" }, ` · Next: ${fmtWhen(j.next_run)}`) : null),
    history);
  return card;
}

async function renderJobHistory(id, box) {
  box.innerHTML = "";
  box.append(el("p", { class: "muted small" }, "Loading…"));
  let runs;
  try { runs = await api(`/api/jobs/${id}/runs`); } catch (ex) { box.textContent = ex.message; return; }
  box.innerHTML = "";
  if (!runs.length) { box.append(el("p", { class: "muted small" }, "No runs yet.")); return; }
  const dur = (ms) => ms == null ? "" : ms < 1000 ? `${ms} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms / 60000)} min`;
  runs.forEach((r, idx) => {
    const total = r.finished_at ? Math.round((r.finished_at - r.started_at) * 1000) : null;
    const steps = r.log.filter((x) => typeof x.step === "number" && x.step > 0);
    const okSteps = steps.filter((x) => x.ok).length;
    const updateLine = r.log.find((x) => x.action === "Update result");
    const rows = r.log.map((x) => {
      const head = el("div", { class: "run-step-head" },
        el("span", { class: `run-mark ${x.ok ? "ok-text" : "error"}` }, x.step === 0 ? "▶" : x.ok ? "✓" : "✗"),
        el("div", { class: "run-step-text" },
          x.action ? el("strong", {}, x.step > 0 ? `${x.step}. ${x.action}` : x.action) : null,
          x.action ? el("br") : null,
          el("span", { class: x.ok ? "" : "error" }, x.message)),
        x.ms != null ? el("span", { class: "muted small run-ms" }, dur(x.ms)) : null);
      return el("li", { class: `run-step ${x.action === "Update result" ? "run-result" : ""}` }, head,
        x.details ? el("details", { class: "run-details" }, el("summary", { class: "small muted" }, "Details"),
          el("pre", { class: "update-notes mono small" }, x.details)) : null);
    });
    box.append(el("details", { class: "run", open: idx === 0 && r.status !== "ok" },
      el("summary", {},
        el("span", { class: r.status === "ok" ? "ok-text" : r.status === "running" ? "muted" : "error" },
          r.status === "ok" ? "✓ OK" : r.status === "running" ? "… Running" : "✗ Failed"),
        ` · ${fmtWhen(r.started_at)}`, total != null ? el("span", { class: "muted" }, ` · took ${dur(total)}`) : null,
        steps.length ? el("span", { class: "muted" }, ` · ${okSteps}/${steps.length} actions OK`) : null,
        updateLine ? el("span", { class: updateLine.ok ? "ok-text" : "error" }, updateLine.ok ? " · updated" : " · update failed") : null,
        el("div", { class: "muted small run-why" }, r.trigger)),
      el("ol", { class: "run-log" }, rows)));
  });
}

// ---- job editor
async function openJobEditor(job = null) {
  state.jobCat = await api("/api/jobs/catalog");
  state.jobEditing = job;
  const f = $("#job-form");
  $("#job-edit-title").textContent = job ? `Edit “${job.name}”` : "New job";
  f.name.value = job ? job.name : "";
  f.description.value = job ? job.description : "";
  f.enabled.checked = job ? job.enabled : true;
  f.stop_on_error.checked = job ? job.options.stop_on_error !== false : true;
  $("#job-triggers").innerHTML = "";
  $("#job-actions").innerHTML = "";
  if (job) {
    job.triggers.forEach((t) => addJobItem("trigger", t));
    job.actions.forEach((a) => addJobItem("action", a));
  } else {
    addJobItem("trigger", { type: "disk_usage" });
    addJobItem("action", { type: "send_mail" });
  }
  setFormMsg("#job-msg", "");
  switchView("job-edit");
  renderPlaceholders();
  f.name.focus();
}
$("#job-add").addEventListener("click", () => openJobEditor());
$("#job-back").addEventListener("click", () => switchView("jobs"));
$("#job-cancel").addEventListener("click", () => switchView("jobs"));
$("#job-add-trigger").addEventListener("click", () => { addJobItem("trigger", { type: "schedule" }); renderPlaceholders(); });
$("#job-add-action").addEventListener("click", () => { addJobItem("action", { type: "send_mail" }); renderPlaceholders(); });

function addJobItem(kind, item) {
  const cat = kind === "trigger" ? state.jobCat.triggers : state.jobCat.actions;
  const box = $(kind === "trigger" ? "#job-triggers" : "#job-actions");
  const card = el("div", { class: "card job-item" });
  card.dataset.kind = kind;
  const typeSel = el("select", { class: "item-type", "aria-label": kind === "trigger" ? "Trigger type" : "Action type" });
  // grouped by the module they come from (Server, Containers, AI, Mail, …)
  const groups = {};
  for (const [k, t] of Object.entries(cat)) (groups[t.group || "Other"] ||= []).push([k, t]);
  for (const [g, items] of Object.entries(groups)) {
    const og = el("optgroup", { label: g });
    for (const [k, t] of items) og.append(el("option", { value: k }, t.label));
    typeSel.append(og);
  }
  typeSel.value = item.type;
  const head = el("div", { class: "item-head" },
    el("span", { class: "icon-tile small-tile item-icon" }),
    kind === "action" ? el("span", { class: "step-no" }) : null,
    typeSel, el("span", { class: "spacer" }),
    kind === "action" ? el("button", { type: "button", class: "small ghost", title: "Move up", "aria-label": "Move up", onclick: () => { card.previousElementSibling?.before(card); renumber(); } }, el("span", { html: icon("arrow-up") })) : null,
    kind === "action" ? el("button", { type: "button", class: "small ghost", title: "Move down", "aria-label": "Move down", onclick: () => { card.nextElementSibling?.after(card); renumber(); } }, el("span", { html: icon("arrow-down") })) : null,
    el("button", { type: "button", class: "small ghost danger", title: "Remove", "aria-label": "Remove", onclick: () => { card.remove(); renumber(); renderPlaceholders(); } }, el("span", { html: icon("x") })));
  const desc = el("p", { class: "muted small item-desc" });
  const fields = el("div", { class: "item-fields" });
  card.append(head, desc, fields);
  const render = (values) => {
    const spec = cat[typeSel.value];
    head.querySelector(".item-icon").innerHTML = icon(spec.icon || "bolt");
    desc.textContent = spec.description;
    fields.innerHTML = "";
    for (const fd of spec.fields) fields.append(jobField(fd, values[fd.key] ?? fd.default));
    applyShowIf(card, spec);
  };
  typeSel.addEventListener("change", () => { render({}); renderPlaceholders(); });
  fields.addEventListener("change", () => applyShowIf(card, cat[typeSel.value]));
  render(item);
  box.append(card);
  renumber();
}

function renumber() {
  $$("#job-actions .job-item .step-no").forEach((s, i) => { s.textContent = String(i + 1); });
}

function jobField(fd, value) {
  const cat = state.jobCat;
  let input;
  const opts = (list, extra = []) => [...extra, ...list].map(([v, l]) => el("option", { value: String(v) }, l));
  switch (fd.type) {
    case "textarea":
      input = el("textarea", { rows: fd.key === "body" || fd.key === "prompt" ? "6" : "3", spellcheck: "false", class: fd.mono ? "mono" : "" });
      input.value = value ?? "";
      break;
    case "number":
      input = el("input", { type: "number", min: fd.min, max: fd.max, step: fd.step || 1 });
      input.value = value ?? "";
      break;
    case "time":
      input = el("input", { type: "time" });
      input.value = value || "08:00";
      break;
    case "bool":
      input = el("input", { type: "checkbox" });
      input.checked = !!value;
      break;
    case "select":
      input = el("select", {}, opts(fd.options));
      input.value = value ?? fd.default;
      break;
    case "mount":
      input = el("select", {}, opts(cat.mounts.map((m) => [m.value, m.label]), [["*", "Any drive"]]));
      input.value = value || "*";
      if (value && value !== "*" && input.value !== value) { input.append(el("option", { value }, value)); input.value = value; }
      break;
    case "sensor":
      input = el("select", {}, opts(cat.sensors.map((m) => [m.value, m.label]), [["*", cat.sensors.length ? "Any sensor" : "Any sensor (none detected on this server)"]]));
      input.value = value || "*";
      break;
    case "mail_account": {
      const accs = cat.mail_accounts.filter((a) => !fd.need || a[fd.need]);
      input = el("select", {}, opts(accs.map((a) => [a.value, a.label]), [["", accs.length ? "Choose…" : "No suitable mail account — add one in Mail accounts"]]));
      input.value = value ? String(value) : (accs.length === 1 ? String(accs[0].value) : "");
      break;
    }
    case "ai_connection":
      input = el("select", {}, opts(cat.ai_connections.map((c) => [c.value, c.label]), [["", "Default connection"]]));
      input.value = value ? String(value) : "";
      break;
    case "choice": {   // containers, stacks, local models (lists from the server)
      const list = (cat.choices || {})[fd.source] || [];
      const empty = { containers: "No containers yet", stacks: "No stacks yet", local_models: "No models installed" }[fd.source] || "None";
      if (fd.free) {   // a list of suggestions, but any value can be typed
        const id = `choices-${fd.source}`;
        if (!$(`#${id}`)) document.body.append(el("datalist", { id }));
        $(`#${id}`).innerHTML = list.map(([v]) => `<option value="${esc(v)}">`).join("");
        input = el("input", { list: id, spellcheck: "false", placeholder: fd.placeholder || "", class: "mono" });
        input.value = value ?? "";
        break;
      }
      const extra = [...(fd.any ? [["*", fd.any]] : []), ...(fd.extra || [])];
      input = el("select", {}, opts(list, [...(fd.any ? [] : [["", list.length ? "Choose…" : empty]]), ...extra]));
      const v = value ?? (fd.any ? "*" : "");
      if (v && ![...input.options].some((o) => o.value === v)) input.append(el("option", { value: v }, `${v} (not found now)`));
      input.value = v;
      break;
    }
    case "weekdays": {
      input = el("div", { class: "weekdays" });
      for (const d of cat.weekdays) {
        const cb = el("input", { type: "checkbox", value: d });
        cb.checked = (value || []).includes(d);
        input.append(el("label", { class: "day-chip" }, cb, d.charAt(0).toUpperCase() + d.slice(1, 3)));
      }
      break;
    }
    case "variable":
      input = el("input", { list: "var-names", spellcheck: "false", placeholder: "last_alert" });
      input.value = value || "";
      if (!$("#var-names")) document.body.append(el("datalist", { id: "var-names" }));
      $("#var-names").innerHTML = cat.variables.map((v) => `<option value="${esc(v.name)}">`).join("");
      break;
    default:
      input = el("input", { type: "text", spellcheck: "false", placeholder: fd.placeholder || "", class: fd.type === "cron" ? "mono" : "" });
      input.value = value ?? "";
  }
  input.dataset.key = fd.key;
  input.dataset.ftype = fd.type;
  if (["text", "textarea"].includes(fd.type)) input.dataset.ph = "1";
  const label = fd.type === "bool"
    ? el("label", { class: "check field" }, input, fd.label)
    : el("label", { class: "field" }, fd.label, fd.required ? null : (["bool", "select", "mount", "sensor", "ai_connection", "weekdays", "number", "time", "choice"].includes(fd.type) ? null : el("span", { class: "muted small" }, " (optional)")), input);
  label.dataset.key = fd.key;
  if (fd.help) label.append(el("span", { class: "muted small" }, fd.help));
  return label;
}

function itemValues(card) {
  const vals = { type: card.querySelector(".item-type").value };
  card.querySelectorAll(".item-fields [data-key]").forEach((i) => {
    if (i.tagName === "LABEL") return;
    const ft = i.dataset.ftype;
    if (ft === "bool") vals[i.dataset.key] = i.checked;
    else if (ft === "number") vals[i.dataset.key] = i.value === "" ? null : Number(i.value);
    else if (ft === "weekdays") vals[i.dataset.key] = [...i.querySelectorAll("input:checked")].map((c) => c.value);
    else vals[i.dataset.key] = i.value;
  });
  return vals;
}

function applyShowIf(card, spec) {
  const vals = itemValues(card);
  for (const fd of spec.fields) {
    if (!fd.show_if) continue;
    const show = Object.entries(fd.show_if).every(([k, allowed]) => allowed.includes(vals[k]));
    const lbl = card.querySelector(`.item-fields label[data-key="${fd.key}"]`);
    if (lbl) lbl.hidden = !show;
  }
}

// Placeholder helper: what can be used in the job's text fields right now.
function renderPlaceholders() {
  const cat = state.jobCat, box = $("#ph-groups");
  if (!cat) return;
  const groups = [];
  const trig = new Set();
  $$("#job-triggers .item-type").forEach((s) => (cat.triggers[s.value]?.placeholders || []).forEach((p) => trig.add(p)));
  if (trig.size) groups.push(["From the trigger", [...trig]]);
  const prov = new Set(["steps.1.output"]);
  $$("#job-actions .item-type").forEach((s) => (cat.actions[s.value]?.provides || []).forEach((p) => prov.add(p)));
  groups.push(["From earlier actions", [...prov]]);
  if (cat.variables.length) groups.push(["Your variables", cat.variables.map((v) => `var.${v.name}`)]);
  const mon = cat.monitor_placeholders || [];
  const mainMon = mon.filter((m) => !/^mon\.(disk|temp)\./.test(m.key));       // summary values
  const perItem = mon.filter((m) => /^mon\.(disk|temp)\./.test(m.key));        // per drive / per sensor
  if (mainMon.length) groups.push(["Monitor values", mainMon.map((m) => [m.key, m.label])]);
  if (perItem.length) groups.push(["Per drive / sensor", perItem.map((m) => [m.key, m.label]), true]);
  groups.push(["Built-in", cat.system_placeholders]);
  box.innerHTML = "";
  for (const [title, items, collapsed] of groups) {
    const chips = el("div", { class: "ph-chips" }, items.map((p) => {
      const [key, label] = Array.isArray(p) ? p : [p, ""];
      return el("button", { type: "button", class: "ph-chip", title: label || null, onclick: () => insertPlaceholder(key) }, ph(key));
    }));
    box.append(collapsed
      ? el("details", { class: "ph-group" }, el("summary", { class: "flow-label" }, title), chips)
      : el("div", { class: "ph-group" }, el("div", { class: "flow-label" }, title), chips));
  }
}
document.addEventListener("focusin", (e) => {
  if (e.target.dataset && e.target.dataset.ph) state.phTarget = e.target;
});
function insertPlaceholder(p) {
  const t = state.phTarget;
  if (!t || !document.body.contains(t)) { copyText(ph(p)); return; }
  const s = t.selectionStart ?? t.value.length, e2 = t.selectionEnd ?? t.value.length;
  t.value = t.value.slice(0, s) + ph(p) + t.value.slice(e2);
  t.focus();
  t.selectionStart = t.selectionEnd = s + ph(p).length;
}

$("#job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, j = state.jobEditing;
  const body = {
    name: f.name.value.trim(), description: f.description.value.trim(), enabled: f.enabled.checked,
    triggers: $$("#job-triggers .job-item").map(itemValues),
    actions: $$("#job-actions .job-item").map(itemValues),
    options: { stop_on_error: f.stop_on_error.checked },
  };
  $("#job-save").disabled = true;
  try {
    await api(j ? `/api/jobs/${j.id}` : "/api/jobs", { method: j ? "PUT" : "POST", body: JSON.stringify(body) });
    toast(j ? "Job saved." : `Job “${body.name}” created.`);
    switchView("jobs");
  } catch (ex) { setFormMsg("#job-msg", ex.message, false); }
  $("#job-save").disabled = false;
});

boot().catch((e) => console.error(e));
