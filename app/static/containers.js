// Containers module (Docker). Uses helpers from app.js: $, $$, el, api, toast, icon, withIcon,
// fmtBytes, usageBar, state, openContainerConsole.
"use strict";

const DK_RESTART = [
  ["unless-stopped", "Restart automatically, unless I stop it (recommended)"],
  ["always", "Always restart (also after I stop it, at the next reboot)"],
  ["on-failure", "Restart only if it crashes"],
  ["no", "Never restart"],
];
state.dk = { tab: "containers", filter: "" };
// icon() returns SVG markup; this returns it as an element (for el() children).
function iconEl(name) { const t = document.createElement("template"); t.innerHTML = icon(name); return t.content.firstChild; }

async function loadDocker() {
  clearTimeout(state.dk.timer);
  let s;
  try { s = await api("/api/docker/status"); } catch (ex) { toast(ex.message); return; }
  state.dk.status = s;
  renderDockerStatus(s);
  await loadDockerTasks();
  const ready = s.running && s.access;
  $("#dk-main").hidden = !ready;
  if (ready && !state.dk.loadedOnce) { state.dk.loadedOnce = true; showDockerTab(state.dk.tab); }
  else if (ready && state.dk.tab === "containers" && !document.querySelector("#dk-dialog[open], #dk-logs-dialog[open]")) refreshDockerTab(true);
  const busy = (s.install && s.install.state === "running") || (state.dk.tasks || []).some((t) => t.state === "running");
  if (state.view === "containers") state.dk.timer = setTimeout(loadDocker, busy ? 2000 : 10000);
}

function renderDockerStatus(s) {
  $("#dk-tile").innerHTML = icon("box");
  $("#dk-tile").className = `icon-tile ${s.running && s.access ? "accent" : ""}`;
  const setup = $("#dk-setup");
  const dev = s.device;     // a device from Server → Devices: installed from there; no service controls here
  setup.hidden = !s.available || s.can_manage || !!dev;
  if (!setup.hidden) { setup.innerHTML = ""; setup.append("Installing and controlling Docker needs a one-time setup on the server. Run this as root, then reload: ", el("code", {}, s.setup_command || "")); }
  const inst = s.install || {};
  const installing = inst.state === "running";
  const pill = $("#dk-pill");
  const setPill = (t, c) => { pill.textContent = t; pill.className = `status-pill ${c}`; };
  if (!s.available) setPill("Linux only", "warn");
  else if (installing) setPill("installing…", "warn");
  else if (s.running && s.access) setPill("running", "ok");
  else if (s.installed && s.service === "active") setPill("no access", "warn");
  else if (s.installed) setPill("stopped", "warn");
  else setPill("not installed", "");
  $("#dk-version").textContent = !s.available ? "Containers are only available when the app runs on Linux."
    : dev && s.error && !s.installed ? s.error
    : s.running && s.access ? `Docker ${s.version}${s.compose ? ` · Compose ${s.compose}` : " · Docker Compose not installed (stacks unavailable)"}`
    : s.installed ? (s.error || "Installed, but the service isn't running.")
    : "Not installed yet. Installing takes a few minutes and about 500 MB of disk.";

  const acts = $("#dk-actions");
  acts.innerHTML = "";
  const post = (url, body, done) => async (e) => {
    e.currentTarget.disabled = true;
    try { const r = await api(url, { method: "POST", body: JSON.stringify(body || {}) }); toast(r.message || done); }
    catch (ex) { toast(ex.message); }
    setTimeout(loadDocker, 1500);
  };
  const svc = (action, label, ic) => el("button", { type: "button", class: "small", disabled: !s.can_manage,
    onclick: post("/api/docker/service", { action }, `Docker: ${label.toLowerCase()} done.`) }, withIcon(ic, label));
  if (dev) {
    if (!s.error && !s.installed) acts.append(el("button", { type: "button", class: "primary",
      onclick: () => window.deviceInstallModule && deviceInstallModule(dev.id, "docker") }, withIcon("download", "Install Docker on the device")));
    else if (s.installed && !s.access && !(s.group && s.group.member)) acts.append(el("button", { type: "button", class: "primary",
      onclick: () => window.deviceInstallModule && deviceInstallModule(dev.id, "docker_access") }, withIcon("key", "Give access")));
    if (s.error) acts.append(el("button", { type: "button", class: "small",
      onclick: () => window.deviceReconnect && deviceReconnect(dev.id, loadDocker) }, withIcon("refresh", "Reconnect")));
  } else if (s.available && !s.installed && !installing) {
    acts.append(el("button", { type: "button", class: "primary", disabled: !s.can_manage, onclick: async (e) => {
      if (!confirm("Install Docker on this server?\n\nThis downloads and runs Docker's official install script (get.docker.com) as root. "
        + "It installs Docker Engine and Docker Compose.\n\nSysAdmin.IA is then added to the “docker” group so it can manage containers. "
        + "Note: control over Docker is equivalent to root access on the server (and the chat assistant can use the docker command too).")) return;
      e.currentTarget.disabled = true;
      try { await api("/api/docker/install", { method: "POST" }); } catch (ex) { toast(ex.message); }
      loadDocker();
    } }, withIcon("download", "Install Docker")));
  } else if (s.installed && !installing) {
    if (s.needs_restart) acts.append(el("button", { type: "button", class: "primary", onclick: async (e) => {
      e.currentTarget.disabled = true;
      try { await api("/api/docker/restart-app", { method: "POST" }); toast("Restarting SysAdmin.IA…"); } catch (ex) { toast(ex.message); }
      setTimeout(() => location.reload(), 6000);
    } }, withIcon("refresh", "Restart the app to finish")));
    else if (!s.access && s.group && !s.group.member) acts.append(el("button", { type: "button", class: "primary", disabled: !s.can_manage, onclick: async (e) => {
      if (!confirm("Let SysAdmin.IA manage Docker?\n\nThis adds the app's user to the “docker” group, which is equivalent to root access on the server. The app restarts afterwards.")) return;
      e.currentTarget.disabled = true;
      try { await api("/api/docker/access", { method: "POST", body: JSON.stringify({ action: "grant" }) });
            await api("/api/docker/restart-app", { method: "POST" }); toast("Restarting SysAdmin.IA…"); }
      catch (ex) { toast(ex.message); }
      setTimeout(() => location.reload(), 6000);
    } }, withIcon("key", "Give access")));
    if (s.service === "active" || s.running) acts.append(svc("restart", "Restart", "refresh"), svc("stop", "Stop", "stop"));
    else acts.append(svc("start", "Start", "play"));
  }

  const stats = $("#dk-stats");
  stats.innerHTML = "";
  if (s.info) {
    const i = s.info;
    const stat = (label, value, sub) => el("div", { class: "u-stat" }, el("div", { class: "label" }, label), el("div", { class: "u-value" }, value), el("div", { class: "muted small" }, sub));
    stats.append(el("div", { class: "u-stats" },
      stat("Containers", `${i.running} running`, `${i.stopped} stopped${i.paused ? `, ${i.paused} paused` : ""}`),
      stat("Images", String(i.images), i.driver ? `storage: ${i.driver}` : ""),
      stat("Disk", i.disk_free != null ? fmtBytes(i.disk_free) : "–", `free in ${i.root_dir || "/var/lib/docker"}`)));
  }
  const acc = $("#dk-access");
  acc.hidden = !(s.installed && !s.access && s.error && !installing) && !(dev && s.error && s.installed);
  acc.textContent = s.error || "";

  $("#dk-install").hidden = !(installing || inst.state === "failed");
  if (!$("#dk-install").hidden) {
    const msg = $("#dk-install-msg");
    msg.innerHTML = installing ? '<span class="spinner" aria-hidden="true"></span> ' : "";
    msg.append(installing ? `${inst.stage ? inst.stage.replace(/\.+$/, "") : inst.message || "Installing"}…` : inst.message || "");
    msg.className = `small ${installing ? "busy" : "error"}`;
    const log = $("#dk-install-log");
    log.textContent = inst.log || "";
    log.scrollTop = log.scrollHeight;
    if (inst.state === "failed") $("#dk-install-details").open = true;
  }
  $("#dk-danger").hidden = !s.installed || !s.can_manage || installing || !!dev;
}

// ---- background tasks (downloads, creating containers, stacks)
async function loadDockerTasks() {
  let t = [];
  try { t = await api("/api/docker/tasks?all=1"); } catch { /* ignore */ }   // tasks on every computer
  const before = new Set((state.dk.tasks || []).filter((x) => x.state === "running").map((x) => x.id));
  state.dk.tasks = t;
  const box = $("#dk-tasks");
  box.innerHTML = "";
  for (const x of t.slice(0, 6)) {
    const running = x.state === "running";
    box.append(el("div", { class: `card dk-task ${x.state}` },
      el("div", { class: "small" },
        running ? el("span", { class: "spinner", "aria-hidden": "true" }) : el("span", { class: x.state === "done" ? "ok-text" : "error" }, x.state === "done" ? "✓" : "✕"),
        " ", el("strong", {}, x.title), x.device ? el("span", { class: "tag dk-dev-tag", "data-no-i18n": "" }, x.device_name || `#${x.device}`) : null, " · ",
        el("span", { class: x.state === "failed" ? "error" : "muted" }, x.state === "failed" ? x.error : x.status),
        x.result?.warnings?.length ? el("span", { class: "muted" }, ` (${x.result.warnings.join("; ")})`) : null),
      running && x.progress != null ? usageBar(x.progress) : null,
      x.log.length ? el("details", {}, el("summary", { class: "small" }, "Details"), el("pre", { class: "mono small update-notes" }, x.log.join("\n"))) : null));
    // a task finished since the last poll: refresh the lists
    if (!running && before.has(x.id)) {
      refreshDockerTab();
      if (state.dk.pendingPrepare === x.id && $("#dk-dialog").open) state.dk.form?.clone ? dkImageCheck() : dkPrepare();
    }
  }
}

// ---- tabs
function showDockerTab(tab) {
  state.dk.tab = tab;
  $$("#dk-tabs .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("#dk-pane").innerHTML = "";
  refreshDockerTab();
}
$$("#dk-tabs .seg-btn").forEach((b) => b.addEventListener("click", () => showDockerTab(b.dataset.tab)));

async function refreshDockerTab(quiet = false) {
  const tab = state.dk.tab;
  const fn = { containers: dkContainers, apps: dkApps, images: dkImages, stacks: dkStacks, volumes: dkVolumes, networks: dkNetworks, cleanup: dkCleanup }[tab];
  try { await fn(quiet); }
  catch (ex) { if (!quiet) { $("#dk-pane").innerHTML = ""; $("#dk-pane").append(el("p", { class: "error small" }, ex.message)); } }
}

const dkPost = (url, body) => api(url, { method: "POST", body: JSON.stringify(body || {}) });
const hostUrl = (port, proto = "http", host = null) => `${proto}://${host && host.includes(":") ? `[${host}]` : host || location.hostname}:${port}/`;

function dkToolbar(...kids) { return el("div", { class: "dk-toolbar" }, ...kids); }

// ---- containers: every computer's (this server + devices with Docker, Server → Devices)
const dkTarget = () => String((state.target || {}).docker || "");
async function dkContainers(quiet) {
  const all = await api("/api/docker/everywhere?stats=true");
  if (state.dk.tab !== "containers") return;
  state.dk.hostsSeen = all.hosts;
  state.dk.containers = all.hosts.flatMap((h) => h.containers);
  const pane = $("#dk-pane");
  const keepFilter = $("#dk-filter")?.value ?? state.dk.filter;
  const focused = document.activeElement?.id === "dk-filter";
  pane.innerHTML = "";
  const filter = el("input", { type: "search", id: "dk-filter", placeholder: "Filter by name or image…", value: keepFilter,
    oninput: (e) => { state.dk.filter = e.target.value; dkRenderContainerRows(); } });
  pane.append(dkToolbar(
    el("button", { type: "button", class: "primary small", onclick: () => openDockerDialog() }, withIcon("plus", "New container")),
    filter,
    el("button", { type: "button", class: "small ghost", onclick: () => refreshDockerTab() }, withIcon("refresh", "Refresh"))));
  pane.append(el("div", { id: "dk-rows", class: "dk-rows" }));
  dkRenderContainerRows();
  if (focused) { filter.focus(); filter.setSelectionRange(filter.value.length, filter.value.length); }
}

function dkRenderContainerRows() {
  const box = $("#dk-rows");
  if (!box) return;
  box.innerHTML = "";
  const q = (state.dk.filter || "").toLowerCase();
  const hosts = state.dk.hostsSeen || [];
  const many = hosts.length > 1;
  const list = (state.dk.containers || []).filter((c) => !q || `${c.name} ${c.image} ${c.stack || ""} ${c.device_name || ""}`.toLowerCase().includes(q));
  if (!list.length && !many) {
    box.append(el("div", { class: "dk-empty" },
      el("p", { class: "muted" }, state.dk.containers?.length ? "No containers match." : "No containers yet."),
      state.dk.containers?.length ? null : el("p", { class: "small muted" }, "Click New container and type an app's image name, e.g. nginx, jellyfin/jellyfin or nextcloud. "
        + "Ports and folders are suggested for you.")));
    return;
  }
  if (!many) { for (const c of list) box.append(dkContainerRow(c)); return; }
  // one group per computer, with its problem if it can't be read
  for (const h of hosts) {
    const mine = list.filter((c) => String(c.device) === String(h.device));
    box.append(el("div", { class: "dk-host-head" },
      el("span", { class: "dk-host-icon", html: icon("server") }),
      el("strong", { "data-no-i18n": h.device ? "" : null }, h.name),
      el("span", { class: "muted small" }, h.error ? "" : `${h.containers.filter((c) => c.state === "running").length} running · ${h.containers.length} in all`),
      h.error ? el("span", { class: `small ${h.offline ? "muted" : "error"}` }, h.error) : null));
    if (!h.error && !mine.length) box.append(el("p", { class: "muted small dk-host-empty" }, q ? "No containers match." : "No containers here yet."));
    for (const c of mine) box.append(dkContainerRow(c));
  }
}

// Copy a container's settings to a new container on another computer (or the same one, with another name).
async function dkCloneContainer(c) {
  let hosts = [];
  try { hosts = await api("/api/docker/hosts"); } catch (ex) { toast(ex.message); return; }
  openDockerDialog(null, "", "", "", { clone: c, device: String((hosts.find((h) => String(h.id) !== String(c.device || 0)) || hosts[0] || { id: 0 }).id) });
}

function dkContainerRow(c) {
  const running = c.state === "running";
  const dev = c.device || 0;        // the computer it runs on
  const act = (action, label, ic, cls = "ghost") => el("button", { type: "button", class: `small ${cls}`, title: label, onclick: async (e) => {
    e.currentTarget.disabled = true;
    try { await dkPost(onDevice(`/api/docker/containers/${c.id}/action`, dev), { action }); } catch (ex) { toast(ex.message); }
    refreshDockerTab();
  } }, withIcon(ic, label));
  const ports = c.ports.filter((p) => p.host).map((p) => {
    const label = `${p.host}${p.host !== p.container ? ` → ${p.container}` : ""}${p.proto === "udp" ? "/udp" : ""}`;
    return p.proto === "tcp" && running && p.ip !== "127.0.0.1"
      ? el("a", { class: "tag tag-accent dk-port", href: hostUrl(p.host, [443, 8443, 9443].includes(p.container) ? "https" : "http", c.device_host),
        target: "_blank", rel: "noopener noreferrer", title: c.device ? `Open port ${p.host} on ${c.device_name}` : `Open port ${p.host} on this server` }, iconEl("link"), label)
      : el("span", { class: "tag", title: p.ip === "127.0.0.1" ? "Only reachable from the server itself" : "" }, label);
  });
  const folders = c.mounts.map((m) => el("span", { class: "tag", title: `${m.type === "volume" ? `volume ${m.name}` : m.source} → ${m.target}${m.readonly ? " (read-only)" : ""}` },
    iconEl(m.type === "volume" ? "box" : "folder"), m.type === "volume" ? m.name.slice(0, 24) : m.source.split("/").filter(Boolean).slice(-2).join("/") || "/"));
  const st = c.stats;
  return el("div", { class: `dk-row ${running ? "running" : ""}` },
    el("div", { class: "dk-row-main" },
      el("span", { class: `dot ${running ? "on" : c.state === "paused" ? "warn" : "off"}`, title: c.state }),
      el("div", { class: "dk-row-title" },
        el("strong", {}, c.name), c.stack ? el("span", { class: "tag", title: "Part of a stack" }, c.stack) : null,
        el("div", { class: "muted small mono dk-ellipsis" }, c.image)),
      el("div", { class: "small muted dk-row-status" }, c.status,
        st ? el("div", {}, `CPU ${st.cpu ?? "–"}% · RAM ${fmtBytes(st.mem)}`) : null)),
    ports.length || folders.length ? el("div", { class: "dk-row-links small" }, ports, folders) : null,
    el("div", { class: "dk-row-actions" },
      running ? act("stop", "Stop", "stop") : act("start", "Start", "play", "primary"),
      running ? act("restart", "Restart", "refresh") : null,
      el("button", { type: "button", class: "small ghost", onclick: () => openDockerLogs(c) }, withIcon("file", "Logs")),
      running ? el("button", { type: "button", class: "small ghost", title: "Open a shell inside the container",
        onclick: () => openContainerConsole(c.id, c.name, String(dev || "")) }, withIcon("console", "Terminal")) : null,
      c.stack ? null : el("button", { type: "button", class: "small ghost", title: "Change ports, folders, settings or the image version",
        onclick: () => openDockerDialog(c.id, "", "", "", { device: String(dev || "") }) }, withIcon("edit", "Edit")),
      c.stack ? null : el("button", { type: "button", class: "small ghost", title: "Make a new container with the same settings, on another device or here",
        onclick: () => dkCloneContainer(c) }, withIcon("copy", "Copy to…")),
      el("button", { type: "button", class: "small ghost danger", onclick: async () => {
        if (!confirm(`Remove the container “${c.name}”${c.device ? ` on ${c.device_name}` : ""}?${running ? "\n\nIt is running and will be stopped." : ""}\n\nFiles in shared server folders are kept.`)) return;
        try { await dkPost(onDevice(`/api/docker/containers/${c.id}/remove`, dev), { volumes: false }); toast(`Removed ${c.name}.`); } catch (ex) { toast(ex.message); }
        refreshDockerTab();
      } }, withIcon("trash", "Remove"))));
}

// ---- app catalog: common images with ready-made settings
async function dkApps() {
  const [cat, list] = await Promise.all([api("/api/docker/apps"), api("/api/docker/containers").catch(() => [])]);
  if (state.dk.tab !== "apps") return;
  state.dk.apps = cat;
  state.dk.containers = list;
  const pane = $("#dk-pane");
  pane.innerHTML = "";
  const q = el("input", { type: "search", placeholder: "Search apps (e.g. database, photos, vpn)…", value: state.dk.appQ || "",
    oninput: (e) => { state.dk.appQ = e.target.value; draw(); } });
  const seg = el("div", { class: "seg lai-filter", role: "tablist" });
  const grid = el("div", { class: "lai-grid dk-apps" });
  pane.append(el("p", { class: "small muted" }, "Popular apps with recommended settings. Click Install: the image is downloaded and the form is filled in "
    + "(ports, folders on your drives, passwords). Check it and click Create."), dkToolbar(q), seg, grid);
  const draw = () => {
    const c = state.dk.appCat || "all";
    seg.innerHTML = "";
    for (const x of [{ id: "all", title: "All" }, ...cat.categories]) {
      const n = x.id === "all" ? cat.apps.length : cat.apps.filter((a) => a.cat === x.id).length;
      if (n) seg.append(el("button", { type: "button", class: `seg-btn ${c === x.id ? "active" : ""}`,
        onclick: () => { state.dk.appCat = x.id; draw(); } }, `${x.title} (${n})`));
    }
    const words = (state.dk.appQ || "").toLowerCase().split(/\s+/).filter(Boolean);
    const catTitle = Object.fromEntries(cat.categories.map((x) => [x.id, x.title]));
    const apps = cat.apps.filter((a) => (c === "all" || a.cat === c) &&
      words.every((w) => `${a.title} ${a.image} ${a.about} ${catTitle[a.cat]}`.toLowerCase().includes(w)));
    grid.innerHTML = "";
    if (!apps.length) grid.append(el("p", { class: "muted small" }, "No apps match. You can still install any image with New container."));
    for (const a of apps) grid.append(dkAppCard(a, catTitle[a.cat]));
  };
  draw();
}

// URL to open an installed app: "/path" on its first port, ":PORT/path" on that container port, "https://:PORT/path".
function appUrl(a, c) {
  if (!a.open || !c) return null;
  let proto = "http", spec = a.open;
  if (spec.startsWith("https://")) { proto = "https"; spec = spec.slice(8); }
  let cport = a.ports.find((p) => p.proto === "tcp")?.container, path = spec;
  const m = spec.match(/^:(\d+)(\/.*)?$/);
  if (m) { cport = Number(m[1]); path = m[2] || "/"; }
  if (!cport) return null;
  const mapped = c.ports.find((p) => p.container === cport && p.host);
  const hostPort = mapped ? mapped.host : a.network === "host" ? cport : null;
  return hostPort ? `${proto}://${location.hostname}:${hostPort}${path}` : null;
}

function dkAppCard(a, catTitle) {
  const mine = (state.dk.containers || []).filter((c) => a.containers.includes(c.name));
  const running = mine.find((c) => c.state === "running");
  const url = running ? appUrl(a, running) : null;
  return el("div", { class: `lai-pick dk-app ${mine.length ? "installed" : ""}` },
    el("div", { class: "dk-app-head" }, el("span", { class: "dk-app-logo", "aria-hidden": "true" }, a.logo || "📦"),
      el("div", {}, el("strong", {}, a.title), el("div", { class: "muted small mono lai-name" }, a.image))),
    el("div", { class: "small lai-badges" }, el("span", { class: "tag" }, catTitle),
      a.network === "host" ? el("span", { class: "tag", title: "Uses the server's network directly" }, "host network") : null,
      a.ports.slice(0, 4).map((p) => el("span", { class: "tag", title: p.label || "" }, `${p.container}${p.proto === "udp" ? "/udp" : ""}`))),
    el("p", { class: "small" }, a.about),
    a.needs.length ? el("p", { class: "small muted" }, `You'll need: ${a.needs.join("; ")}.`) : null,
    el("div", { class: "lai-pick-foot small" },
      mine.length ? el("span", { class: "ok-text" }, `✓ ${running ? "Running" : "Installed"} as ${mine.map((c) => c.name).join(", ")}`) : el("span"),
      el("span", { class: "row" },
        url ? el("a", { class: "small button-link", href: url, target: "_blank", rel: "noopener noreferrer" }, withIcon("link", "Open")) : null,
        el("button", { type: "button", class: `small ${mine.length ? "ghost" : "primary"}`,
          title: mine.length ? "Install another copy (with another name and ports)" : "",
          onclick: () => openDockerDialog(null, a.image, a.id, a.title) }, withIcon("download", mine.length ? "Install again" : "Install")))));
}

// ---- logs
function openDockerLogs(c, stack = null) {
  const d = $("#dk-logs-dialog");
  $("#dk-logs-title").textContent = stack ? `Logs: stack ${stack}` : `Logs: ${c.name}`;
  const pre = $("#dk-logs-pre");
  pre.textContent = "Loading…";
  const load = async () => {
    try {
      const r = await api(stack ? `/api/docker/stacks/${stack}/logs?tail=300`
        : c.device != null ? onDevice(`/api/docker/containers/${c.id}/logs?tail=500`, c.device) : `/api/docker/containers/${c.id}/logs?tail=500`);
      const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
      pre.textContent = r.logs || "(no output yet)";
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    } catch (ex) { pre.textContent = ex.message; }
  };
  clearInterval(state.dk.logTimer);
  load().then(() => { pre.scrollTop = pre.scrollHeight; });
  state.dk.logTimer = setInterval(() => { if (d.open && $("#dk-logs-follow").checked) load(); }, 3000);
  d.showModal();
}
$("#dk-logs-close").addEventListener("click", () => { clearInterval(state.dk.logTimer); $("#dk-logs-dialog").close(); });
$("#dk-logs-dialog").addEventListener("close", () => clearInterval(state.dk.logTimer));

// ---- create / edit container
// opts.device: the computer ("" = this server, else a device id; default: the page's "Runs on");
// opts.clone: a container (from the list) whose settings fill a new container, on opts.device.
const dkF = (path) => onDevice(path, state.dk.form?.device);   // a request for the dialog's computer
async function openDockerDialog(editId = null, image = "", appId = "", appTitle = "", opts = {}) {
  const d = $("#dk-dialog");
  const clone = opts.clone || null;
  state.dk.edit = editId;
  state.dk.form = { image, name: "", restart: "unless-stopped", network: "bridge", ports: [], mounts: [], env: [], command: "",
                    privileged: false, allow_system_paths: false, start: true, pull: false, prepared: false, sysctls: {},
                    app_id: appId, app_image: image, app: null, device: opts.device != null ? String(opts.device || "") : dkTarget(),
                    clone: clone ? { device: String(clone.device || 0), name: clone.device_name || "this server" } : null };
  $("#dk-dialog-title").textContent = editId ? "Edit container" : clone ? `Copy ${clone.name}` : appTitle ? `Install ${appTitle}` : "New container";
  $("#dk-save").textContent = editId ? "Save and recreate" : "Create and start";
  $("#dk-form-error").hidden = true;
  $("#dk-form-body").innerHTML = "";
  $("#dk-form-body").append(el("p", { class: "muted small" }, "Loading…"));
  d.showModal();
  try {
    state.dk.hosts = await api("/api/docker/hosts").catch(() => [{ id: 0, name: "This server" }]);
    if (clone) {   // the source container's settings, read from its own computer
      const c = await api(onDevice(`/api/docker/containers/${clone.id}`, clone.device || 0));
      const sameHost = String(clone.device || 0) === String(state.dk.form.device || 0);
      Object.assign(state.dk.form, { image: c.image, name: sameHost ? `${c.name}-copy` : c.name, restart: c.restart, network: c.network || "bridge",
        ports: c.ports, mounts: c.mounts, env: c.env, command: c.command, privileged: c.privileged, prepared: true, sysctls: c.sysctls || {},
        allow_system_paths: c.mounts.some((m) => m.type === "bind" && isSystemPath(m.source)) });
    }
    await dkLoadHost();
    if (editId) {
      const c = await api(dkF(`/api/docker/containers/${editId}`));
      Object.assign(state.dk.form, { image: c.image, name: c.name, restart: c.restart, network: c.network || "bridge",
        ports: c.ports, mounts: c.mounts, env: c.env, command: c.command, privileged: c.privileged, prepared: true, sysctls: c.sysctls || {},
        allow_system_paths: c.mounts.some((m) => m.type === "bind" && isSystemPath(m.source)) });
    }
  } catch (ex) { $("#dk-form-body").innerHTML = ""; $("#dk-form-body").append(el("p", { class: "error small" }, ex.message)); return; }
  renderDockerForm();
  if (clone) dkImageCheck();
  else if (image && !editId) dkPrepare();
}

// Drives, used ports, networks and volumes of the dialog's computer.
async function dkLoadHost() {
  const [host, nets, vols] = await Promise.all([api(dkF("/api/docker/host")), api(dkF("/api/docker/networks")), api(dkF("/api/docker/volumes"))]);
  state.dk.host = host; state.dk.nets = nets; state.dk.vols = vols;
}

// "Create on": another computer. Its ports and folders differ, so a new container's suggestions are made again.
async function dkSetDialogDevice(dev) {
  const f = state.dk.form;
  f.image = ($("#dk-image")?.value || f.image).trim();
  f.device = dev;
  const msg = $("#dk-image-msg");
  if (msg) { msg.className = "small busy"; msg.innerHTML = '<span class="spinner" aria-hidden="true"></span> Connecting…'; }
  try { await dkLoadHost(); } catch (ex) { if (msg) { msg.className = "small error"; msg.textContent = ex.message; } return; }
  if (f.clone) { renderDockerForm(); dkImageCheck(); return; }
  if (f.prepared && f.image) { f.prepared = false; renderDockerForm(); dkPrepare(); } else renderDockerForm();
}

const dkHostName = (dev) => ((state.dk.hosts || []).find((h) => String(h.id) === String(dev || 0)) || { name: dev ? `#${dev}` : "This server" }).name;

// Is the image on the dialog's computer? If not: download it, or copy it from a computer that has it.
async function dkImageCheck(autoPull = false) {
  const f = state.dk.form;
  const msg = $("#dk-image-msg");
  if (!f.image || !msg) return false;
  msg.className = "small busy"; msg.innerHTML = '<span class="spinner" aria-hidden="true"></span> Looking for the image on your computers…';
  let where = [];
  try { where = await api(`/api/docker/images/where?name=${encodeURIComponent(f.image)}`); }
  catch (ex) { msg.className = "small error"; msg.textContent = ex.message; return false; }
  const here = where.find((w) => String(w.device) === String(f.device || 0));
  if (here && here.present) {
    msg.className = "small ok-text";
    msg.textContent = `✓ ${f.image} is on ${dkHostName(f.device)}${here.size ? ` (${fmtBytes(here.size)})` : ""}.`
      + (f.clone ? ` Settings copied from ${f.clone.name}: check the ports and folders (the folders' contents aren't copied).` : "");
    return true;
  }
  const others = where.filter((w) => w.present && String(w.device) !== String(f.device || 0));
  if (!others.length && autoPull) return false;
  msg.className = "small"; msg.innerHTML = "";
  msg.append(`${f.image} isn't on ${dkHostName(f.device)} yet. `);
  for (const o of others) msg.append(el("button", { type: "button", class: "small", onclick: () => dkCopyImage(f.image, o.device, f.device) },
    withIcon("copy", `Copy it from ${o.name}${o.size ? ` (${fmtBytes(o.size)})` : ""}`)), " ");
  msg.append(el("button", { type: "button", class: "small ghost", onclick: () => dkPullForForm() }, withIcon("download", "Download it")),
    el("span", { class: "muted" }, " (or create: it's downloaded then)"));
  return false;
}

async function dkCopyImage(image, from, to, after = null) {
  try {
    const t = await dkPost("/api/docker/images/copy", { name: image, source: Number(from || 0), target: Number(to || 0) });
    state.dk.pendingPrepare = t.id;
    const msg = $("#dk-image-msg");
    if (msg && $("#dk-dialog").open) { msg.className = "small busy"; msg.innerHTML = '<span class="spinner" aria-hidden="true"></span> Copying the image (progress is shown on the page behind this window)…'; }
    else toast(`Copying ${image}…`);
    if (after) after();
    loadDocker();
  } catch (ex) { toast(ex.message, 7000); }
}

async function dkPullForForm() {
  const f = state.dk.form;
  try {
    const t = await dkPost(dkF("/api/docker/images/pull"), { name: f.image });
    state.dk.pendingPrepare = t.id;
    const msg = $("#dk-image-msg");
    msg.className = "small busy";
    msg.innerHTML = '<span class="spinner" aria-hidden="true"></span> Downloading the image (progress is shown on the page behind this window)…';
    loadDocker();
  } catch (ex) { toast(ex.message); }
}

const SYSTEM_PATHS = ["/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run", "/sbin", "/sys", "/usr", "/var/lib/docker", "/var/run/docker.sock"];
function isSystemPath(p) { return SYSTEM_PATHS.some((s) => p === s || (s !== "/" && p.startsWith(`${s}/`))); }

// Step 1: the image. When it's on the server, its ports and data folders are suggested.
async function dkPrepare() {
  const f = state.dk.form;
  f.image = ($("#dk-image")?.value || f.image).trim();
  f.name = ($("#dk-name")?.value || f.name).trim();
  if (!f.image) return;
  if (f.app_id && f.image !== f.app_image) f.app_id = "";   // another image was typed: no preset
  const msg = $("#dk-image-msg");
  msg.className = "small busy"; msg.innerHTML = '<span class="spinner" aria-hidden="true"></span> Checking the image…';
  try {
    const r = await api(dkF(`/api/docker/prepare?image=${encodeURIComponent(f.image)}&name=${encodeURIComponent(f.name)}&app_id=${encodeURIComponent(f.app_id || "")}`));
    if (!r.present) {
      // on another of your computers? offer to copy it from there; otherwise download it
      if ((state.dk.hosts || []).length > 1 && !(await dkImageCheck(true)) && $("#dk-image-msg button")) return;
      const t = await dkPost(dkF("/api/docker/images/pull"), { name: f.image }).catch((ex) => { throw ex; });
      state.dk.pendingPrepare = t.id;
      const m1 = $("#dk-image-msg");
      m1.className = "small busy";
      m1.innerHTML = '<span class="spinner" aria-hidden="true"></span> Downloading the image (progress is shown on the page behind this window)… '
        + "The form fills in when it's done.";
      loadDocker();
      return;
    }
    state.dk.pendingPrepare = null;
    Object.assign(f, { image: r.image, name: f.name || r.name, ports: r.ports, mounts: r.mounts, env: r.env, prepared: true });
    if (r.app) Object.assign(f, { app: r.app, app_id: r.app.id, app_image: r.image, network: r.network, command: r.command,
                                  privileged: r.privileged, sysctls: r.sysctls, allow_system_paths: r.allow_system_paths });
    else f.app = null;
    f.details = r.details;
    renderDockerForm();
    const m2 = $("#dk-image-msg");
    m2.className = "small ok-text";
    m2.textContent = `✓ ${r.image} is on ${f.device ? dkHostName(f.device) : "the server"} (${fmtBytes(r.details.size)}).` +
      (r.app ? ` Recommended settings for ${r.app.title} are filled in below: check them, especially the folders.`
        : r.ports.length || r.mounts.length ? " Suggested ports and folders are filled in below: check them." : " It doesn't declare ports or data folders: add them below if the app needs them.");
  } catch (ex) { msg.className = "small error"; msg.textContent = ex.message; }
}

function renderDockerForm() {
  const f = state.dk.form;
  const body = $("#dk-form-body");
  body.innerHTML = "";
  // where: this server or a device with Docker (Server → Devices); fixed when editing
  const hosts = state.dk.hosts || [];
  if (hosts.length > 1 || f.device) {
    const sel = el("select", { id: "dk-device", disabled: !!state.dk.edit, onchange: (e) => dkSetDialogDevice(e.target.value) },
      hosts.map((h) => el("option", { value: h.id ? String(h.id) : "", selected: String(h.id || "") === String(f.device || "") },
        h.id ? `${h.name} (${h.host})` : "This server")));
    body.append(el("label", { class: "dk-create-on" }, state.dk.edit ? "Runs on" : "Create on", sel,
      f.clone ? el("span", { class: "small muted" }, `Copying the settings of a container on ${f.clone.name}. Ports and folders are those of the original: check them for this computer.`) : null));
  }
  // image
  const hub = el("div", { class: "dk-hub", id: "dk-hub" });
  body.append(el("label", {}, "Image",
    el("div", { class: "row dk-image-row" },
      el("input", { id: "dk-image", value: f.image, required: true, spellcheck: "false", class: "mono", placeholder: "e.g. nginx, jellyfin/jellyfin:latest, ghcr.io/owner/app",
        onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); dkPrepare(); } } }),
      el("button", { type: "button", class: "small", onclick: dkPrepare }, withIcon("download", f.prepared ? "Check again" : "Get image")),
      el("button", { type: "button", class: "small ghost", onclick: () => dkHubSearch($("#dk-image").value) }, "Search Docker Hub")),
    el("span", { class: "small muted", id: "dk-image-msg" }, f.prepared ? "" : "Type the image name and click Get image: it's downloaded and its ports and folders are suggested.")),
    hub);
  if (state.dk.edit) body.append(el("label", { class: "check small" },
    el("input", { type: "checkbox", checked: f.pull, onchange: (e) => (f.pull = e.target.checked) }), " Download the newest version of the image (update the app)"));
  if (!f.prepared) return;
  if (f.app?.notes) body.append(el("p", { class: "note small dk-app-note" }, el("strong", {}, `${f.app.title}: `), f.app.notes));
  const needs = [...f.env.filter((x) => x.required && !x.value).map((x) => x.label || x.key),
                 ...f.mounts.filter((m) => m.required && !m.source).map((m) => m.label)];
  if (needs.length && !state.dk.edit) body.append(el("p", { class: "note warn small dk-app-note" },
    el("strong", {}, "You must choose: "), needs.join("; "), ". They're marked * below."));
  const generated = f.env.filter((x) => x.generated && x.value);
  if (generated.length && !state.dk.edit) body.append(el("p", { class: "note small dk-app-note" },
    el("strong", {}, "Random passwords were filled in: "), generated.map((x) => x.label || x.key).join("; "),
    ". Copy them somewhere safe before creating the container, or type your own."));

  // name, restart, network
  body.append(el("div", { class: "two-col" },
    el("label", {}, "Name", el("input", { id: "dk-name", value: f.name, required: true, maxlength: "63", spellcheck: "false", oninput: (e) => (f.name = e.target.value) })),
    el("label", {}, "When it stops", el("select", { onchange: (e) => (f.restart = e.target.value) },
      DK_RESTART.map(([v, l]) => el("option", { value: v, selected: f.restart === v }, l))))));

  // ports
  const host = state.dk.host || { used_ports: {} };
  const portsBox = el("div", { class: "dk-list" });
  const drawPorts = () => {
    portsBox.innerHTML = "";
    if (f.network === "host") { portsBox.append(el("p", { class: "small muted" }, "The container uses the server's network directly: its ports are the server's ports, no mapping needed.")); return; }
    if (!f.ports.length) portsBox.append(el("p", { class: "small muted" }, "No ports shared. Add one to reach the app from your network (e.g. a web page)."));
    f.ports.forEach((p, i) => {
      const conflict = host.used_ports[String(p.host)];
      portsBox.append(el("div", { class: "dk-map" },
        el("label", { class: "small" }, "Server port", el("input", { type: "number", min: "1", max: "65535", value: p.host, class: conflict ? "invalid" : "",
          oninput: (e) => { p.host = Number(e.target.value); drawPortHint(); } })),
        el("span", { class: "dk-arrow", "aria-hidden": "true" }, "→"),
        el("label", { class: "small" }, "Container port", el("input", { type: "number", min: "1", max: "65535", value: p.container, oninput: (e) => (p.container = Number(e.target.value)) })),
        el("label", { class: "small" }, "Protocol", el("select", { onchange: (e) => (p.proto = e.target.value) },
          ["tcp", "udp"].map((v) => el("option", { value: v, selected: p.proto === v }, v.toUpperCase())))),
        el("label", { class: "small" }, "Reachable from", el("select", { onchange: (e) => (p.ip = e.target.value) },
          el("option", { value: "", selected: !p.ip }, "Whole network"), el("option", { value: "127.0.0.1", selected: p.ip === "127.0.0.1" }, "This server only"))),
        el("button", { type: "button", class: "small ghost", title: "Remove", onclick: () => { f.ports.splice(i, 1); drawPorts(); } }, iconEl("x")),
        el("div", { class: "small dk-map-hint", "data-port": String(i) })));
    });
    drawPortHint();
  };
  const drawPortHint = () => f.ports.forEach((p, i) => {
    const h = portsBox.querySelector(`[data-port="${i}"]`);
    if (!h) return;
    const used = host.used_ports[String(p.host)];
    h.className = `small dk-map-hint ${used ? "error" : "muted"}`;
    const open = p.host && p.proto === "tcp" ? `Open as ${hostUrl(p.host)}` : "";
    h.textContent = used ? `Port ${p.host} is already used by ${used}.`
      : p.label ? [p.note && p.note !== p.label && p.host !== p.container ? p.note : p.label, open].filter(Boolean).join(" · ")
      : p.note || open;
  });
  const freePort = (from) => { let x = Math.max(1024, from || 8080); const taken = new Set(f.ports.map((p) => p.host)); while (host.used_ports[String(x)] || taken.has(x)) x++; return x; };
  body.append(el("fieldset", { class: "dk-section" },
    el("legend", {}, "Ports ", el("span", { class: "muted small" }, "(server port → port inside the container)")),
    el("label", { class: "small dk-inline" }, "Network ",
      el("select", { onchange: (e) => { f.network = e.target.value; drawPorts(); } },
        (state.dk.nets || []).filter((n) => n.name !== "none").map((n) => el("option", { value: n.name, selected: f.network === n.name },
          n.name === "bridge" ? "Default (bridge, ports mapped below)" : n.name === "host" ? "Server's network directly (host)" : `${n.name}${n.stack ? ` (stack ${n.stack})` : ""}`)))),
    portsBox,
    el("button", { type: "button", class: "small ghost", onclick: () => { const c = Number(prompt("Port inside the container (from the app's documentation, e.g. 80 for a web server):", "80")); if (!c) return; f.ports.push({ host: freePort(c >= 1024 ? c : 8080), container: c, proto: "tcp", ip: "" }); drawPorts(); } },
      withIcon("plus", "Add a port"))));
  drawPorts();

  // folders (drives from the Drives module)
  const mountsBox = el("div", { class: "dk-list" });
  const appName = () => (f.name || "app").toLowerCase().replace(/[^a-z0-9_.-]/g, "-");
  const drawMounts = () => {
    mountsBox.innerHTML = "";
    if (!f.mounts.length) mountsBox.append(el("p", { class: "small muted" }, "No folders shared. Without them, the app's data is lost when the container is recreated."));
    f.mounts.forEach((m, i) => {
      const src = m.type === "volume"
        ? el("label", { class: "small" }, "Docker volume", el("select", { onchange: (e) => (m.source = e.target.value) },
            [...new Set([m.source, ...(state.dk.vols || []).map((v) => v.name)])].filter(Boolean).map((v) => el("option", { value: v, selected: m.source === v }, v))))
        : el("label", { class: "small dk-grow" }, "Folder on the server",
            el("div", { class: "row" },
              el("input", { value: m.source, class: `mono ${(isSystemPath(m.source) && !f.allow_system_paths) || (m.required && !m.source) ? "invalid" : ""}`, spellcheck: "false",
                required: !!m.required, placeholder: m.required ? "required: click Browse to choose it" : "",
                oninput: (e) => { m.source = e.target.value.trim(); e.target.classList.toggle("invalid", !!m.required && !m.source); } }),
              el("button", { type: "button", class: "small ghost", onclick: () => pickHostFolder(m.source || (m.suggest ? `${m.suggest}/` : ""), (p) => { m.source = p; drawMounts(); }, f.device) }, withIcon("folder", "Browse"))));
      mountsBox.append(el("div", { class: "dk-map" },
        m.label ? el("div", { class: "small dk-map-title" }, el("strong", {}, m.label),
          m.required ? el("span", { class: "error" }, " * required") : null,
          m.media ? el("span", { class: "muted" }, " — choose the folder that holds your files") : null) : null,
        src,
        el("span", { class: "dk-arrow", "aria-hidden": "true" }, "→"),
        el("label", { class: "small" }, "Folder in the container", el("input", { value: m.target, class: "mono", spellcheck: "false", placeholder: "/data", oninput: (e) => (m.target = e.target.value.trim()) })),
        el("label", { class: "check small" }, el("input", { type: "checkbox", checked: m.readonly, onchange: (e) => (m.readonly = e.target.checked) }), " Read-only"),
        el("button", { type: "button", class: "small ghost", title: "Remove", onclick: () => { f.mounts.splice(i, 1); drawMounts(); } }, iconEl("x"))));
    });
  };
  const drives = host.drives || [];
  body.append(el("fieldset", { class: "dk-section" },
    el("legend", {}, "Folders ", el("span", { class: "muted small" }, "(folders on your drives that the app can use)")),
    drives.length ? el("div", { class: "dk-drives small" }, el("span", { class: "muted" }, "Store on: "),
      drives.map((dr) => el("button", { type: "button", class: "small ghost dk-drive", title: `${dr.disk}${dr.free != null ? ` · ${fmtBytes(dr.free)} free` : ""}`,
        onclick: () => {
          // move every shared folder (or a new one) under this drive
          const root = `${dr.path === "/" ? "/srv" : dr.path}/appdata/${appName()}`;
          const binds = f.mounts.filter((m) => m.type === "bind");
          if (!binds.length) f.mounts.push({ type: "bind", source: `${root}/data`, target: "/data", readonly: false });
          for (const m of binds) m.source = `${root}${m.target === "/" ? "" : `/${m.target.split("/").filter(Boolean).join("-")}`}`;
          drawMounts();
        } }, iconEl(dr.external ? "external" : "internal"), ` ${dr.label || dr.path} `, el("span", { class: "muted" }, dr.free != null ? fmtBytes(dr.free) : "")))) : null,
    mountsBox,
    el("div", { class: "row" },
      el("button", { type: "button", class: "small ghost", onclick: () => pickHostFolder(state.dk.host?.suggested_root || "/", (p) => {
        const t = prompt("Where should it appear inside the container?", "/data");
        if (!t) return;
        f.mounts.push({ type: "bind", source: p, target: t, readonly: false }); drawMounts();
      }, f.device) }, withIcon("folder", "Share a folder")),
      el("button", { type: "button", class: "small ghost", onclick: () => {
        const v = prompt("Docker volume name (created if it doesn't exist):", `${appName()}-data`);
        if (!v) return;
        const t = prompt("Where should it appear inside the container?", "/data");
        if (!t) return;
        f.mounts.push({ type: "volume", source: v, target: t, readonly: false }); drawMounts();
      } }, withIcon("box", "Use a Docker volume")))));
  drawMounts();

  // environment
  const envBox = el("div", { class: "dk-list" });
  const drawEnv = () => {
    envBox.innerHTML = "";
    // required settings first
    const order = [...f.env.keys()].sort((a, b) => (f.env[b].required ? 1 : 0) - (f.env[a].required ? 1 : 0));
    for (const i of order) {
      const e = f.env[i];
      if (e.label) envBox.append(el("div", { class: "small dk-env-label" }, el("strong", {}, e.label), e.required ? el("span", { class: "error" }, " * required") : null));
      // generated passwords start visible so they can be copied; typed ones stay hidden
      const val = el("input", { value: e.value, class: `mono ${e.required && !e.value ? "invalid" : ""}`,
        type: e.secret && !(e.generated && !state.dk.edit) ? "password" : "text",
        placeholder: e.placeholder || (e.required ? "required" : "value"), required: !!e.required, spellcheck: "false", autocomplete: "new-password",
        "aria-label": e.label || "Value", oninput: (ev) => { e.value = ev.target.value; e.generated = false; ev.target.classList.toggle("invalid", !!e.required && !e.value); } });
      envBox.append(el("div", { class: "dk-env" },
        el("input", { value: e.key, class: "mono", placeholder: "NAME", spellcheck: "false", "aria-label": "Variable name", readonly: !!e.preset,
          title: e.preset ? "Name expected by the app" : "", oninput: (ev) => (e.key = ev.target.value) }),
        el("span", { "aria-hidden": "true" }, "="),
        val,
        e.secret ? el("button", { type: "button", class: "small ghost", title: "Show or hide", onclick: () => { val.type = val.type === "password" ? "text" : "password"; } }, "Show") : null,
        e.generate ? el("button", { type: "button", class: "small ghost", title: "Fill in a random password", onclick: () => {
          const b = new Uint8Array(15); crypto.getRandomValues(b);
          e.value = btoa(String.fromCharCode(...b)).replace(/[+/=]/g, "").slice(0, 20);
          val.value = e.value; val.type = "text"; val.classList.remove("invalid");
          toast("Password generated: copy it somewhere safe before creating the container.");
        } }, "Generate") : null,
        e.required && e.preset ? null : el("button", { type: "button", class: "small ghost", title: "Remove", onclick: () => { f.env.splice(i, 1); drawEnv(); } }, iconEl("x"))));
    }
  };
  body.append(el("fieldset", { class: "dk-section" },
    el("legend", {}, "Settings ", el("span", { class: "muted small" }, "(environment variables from the app's documentation, e.g. TZ=Europe/Madrid)")),
    envBox,
    el("button", { type: "button", class: "small ghost", onclick: () => { f.env.push({ key: "", value: "" }); drawEnv(); } }, withIcon("plus", "Add a setting"))));
  drawEnv();

  // advanced
  body.append(el("details", { class: "setup-steps" }, el("summary", {}, "Advanced"),
    el("label", {}, "Command ", el("span", { class: "muted small" }, "(optional: replaces the image's default command)"),
      el("input", { value: f.command, class: "mono", spellcheck: "false", oninput: (e) => (f.command = e.target.value) })),
    el("label", { class: "check small" }, el("input", { type: "checkbox", checked: f.privileged, onchange: (e) => (f.privileged = e.target.checked) }),
      " Privileged (full access to the server's devices; only if the app's documentation requires it)"),
    el("label", { class: "check small" }, el("input", { type: "checkbox", checked: f.allow_system_paths, onchange: (e) => { f.allow_system_paths = e.target.checked; drawMounts(); } }),
      " Allow system folders (e.g. /var/run/docker.sock or /etc) to be shared"),
    state.dk.edit ? null : el("label", { class: "check small" }, el("input", { type: "checkbox", checked: f.start, onchange: (e) => (f.start = e.target.checked) }), " Start it right away")));
}

async function dkHubSearch(term) {
  const box = $("#dk-hub");
  term = (term || "").trim().split(":")[0];
  if (!term) { toast("Type a name to search for."); return; }
  box.innerHTML = "";
  box.append(el("p", { class: "small busy" }, el("span", { class: "spinner", "aria-hidden": "true" }), " Searching Docker Hub…"));
  try {
    const r = await api(`/api/docker/search?q=${encodeURIComponent(term)}`);
    box.innerHTML = "";
    if (!r.length) { box.append(el("p", { class: "small muted" }, "Nothing found.")); return; }
    for (const x of r) box.append(el("button", { type: "button", class: "dk-hub-item", onclick: () => { $("#dk-image").value = x.name; box.innerHTML = ""; dkPrepare(); } },
      el("div", {}, el("strong", { class: "mono" }, x.name), x.official ? el("span", { class: "tag tag-accent" }, "Official") : null,
        el("span", { class: "muted small" }, ` ★ ${x.stars}`)),
      el("div", { class: "small muted" }, x.description)));
  } catch (ex) { box.innerHTML = ""; box.append(el("p", { class: "small error" }, ex.message)); }
}

$("#dk-cancel").addEventListener("click", () => $("#dk-dialog").close());
$("#dk-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = state.dk.form;
  const err = $("#dk-form-error");
  err.hidden = true;
  if (!f.prepared) { dkPrepare(); return; }
  // essential settings the app can't run without (passwords, user names, your media folders)
  const missing = [...f.env.filter((x) => x.required && !String(x.value).trim()).map((x) => x.label || x.key),
                   ...f.mounts.filter((m) => m.required && !m.source).map((m) => m.label || m.target)];
  if (missing.length) {
    err.textContent = `Fill in before creating: ${missing.join("; ")}.`;
    err.hidden = false;
    $("#dk-form-body .invalid")?.scrollIntoView({ block: "center" });
    $("#dk-form-body .invalid")?.focus();
    return;
  }
  f.image = $("#dk-image").value.trim();
  const spec = { ...f, ports: f.network === "host" ? [] : f.ports, mounts: f.mounts.filter((m) => m.source && m.target),
                 env: f.env.filter((x) => x.key && (x.value !== "" || x.required)) };   // empty optional settings are left out
  delete spec.details;
  delete spec.app;
  delete spec.device;
  delete spec.clone;
  try {
    if (state.dk.edit) {
      if (!confirm("Save and recreate the container?\n\nIt is stopped and replaced by a new one with these settings (a few seconds of downtime). "
        + "Files in shared folders and volumes are kept; files written elsewhere inside the old container are lost.")) return;
      await api(dkF(`/api/docker/containers/${state.dk.edit}`), { method: "PUT", body: JSON.stringify({ spec }) });
    } else {
      await dkPost(dkF("/api/docker/containers"), { spec });
    }
    $("#dk-dialog").close();
    toast(state.dk.edit ? "Updating the container…" : "Creating the container…");
    loadDocker();
  } catch (ex) { err.textContent = ex.message; err.hidden = false; }
});

// ---- folder picker (drives from the Drives module + the server's folders)
function pickHostFolder(start, done, dev = dkTarget()) {   // dev: the computer whose folders are shown
  const d = $("#dk-folder-dialog");
  const drives = $("#dk-picker-drives");
  drives.innerHTML = "";
  for (const dr of state.dk.host?.drives || [])
    drives.append(el("button", { type: "button", class: "small ghost", onclick: () => go(dr.path) },
      iconEl(dr.external ? "external" : "internal"), ` ${dr.label || dr.path}`, el("span", { class: "muted" }, ` ${dr.path}`)));
  const input = $("#dk-picker-input");
  const go = async (path) => {
    const list = $("#dk-picker-list");
    list.innerHTML = "";
    input.value = path;
    $("#dk-picker-path").textContent = path;
    try {
      const r = await api(onDevice(`/api/files?path=${encodeURIComponent(path)}&hidden=false`, dev));
      input.value = r.path;
      $("#dk-picker-path").textContent = r.path;
      if (r.parent) list.append(el("button", { type: "button", class: "dk-pick-row", onclick: () => go(r.parent) }, iconEl("arrow-up"), " .."));
      const dirs = r.entries.filter((x) => x.dir);
      if (!dirs.length) list.append(el("p", { class: "small muted" }, "No folders here."));
      for (const x of dirs) list.append(el("button", { type: "button", class: "dk-pick-row",
        onclick: () => go(`${r.path === "/" ? "" : r.path}/${x.name}`) }, iconEl("folder"), ` ${x.name}`));
    } catch (ex) {
      // a folder that doesn't exist yet is fine: Docker creates it
      list.append(el("p", { class: "small muted" }, `${ex.message} It will be created when the container starts.`));
    }
  };
  $("#dk-picker-ok").onclick = () => {
    const p = input.value.trim();
    if (!p.startsWith("/")) { toast("Use an absolute path, starting with /."); return; }
    d.close(); done(p.replace(/\/+$/, "") || "/");
  };
  $("#dk-picker-cancel").onclick = () => d.close();
  d.showModal();
  go(start && start.startsWith("/") ? start.replace(/\/[^/]*$/, "") || "/" : "/");
  if (start) setTimeout(() => (input.value = start), 300);
}

// ---- images
async function dkImages() {
  const [list, hosts] = await Promise.all([api("/api/docker/images"), api("/api/docker/hosts").catch(() => [])]);
  if (state.dk.tab !== "images") return;
  const here = dkTarget() || "0";
  const others = hosts.filter((h) => String(h.id) !== here);
  // copy an image to another computer (docker save → docker load through the app, no registry needed)
  const copyTo = (tag) => others.length ? el("select", { class: "small", "aria-label": "Copy to another computer", onchange: (e) => {
    const to = e.target.value;
    e.target.value = "";
    if (!to) return;
    const h = others.find((x) => String(x.id) === to);
    if (confirm(`Copy ${tag} to ${h ? h.name : to}?\n\nThe image is sent through SysAdmin.IA, so it works without a registry and for images you built yourself.`))
      dkCopyImage(tag, here, to);
  } }, el("option", { value: "" }, "Copy to…"), others.map((h) => el("option", { value: String(h.id) }, h.id ? h.name : "This server"))) : null;
  const pane = $("#dk-pane");
  pane.innerHTML = "";
  const name = el("input", { placeholder: "Image to download, e.g. nginx:latest", class: "mono", spellcheck: "false" });
  const hub = el("div", { class: "dk-hub" });
  pane.append(el("form", { class: "dk-toolbar", onsubmit: async (e) => {
      e.preventDefault();
      if (!name.value.trim()) return;
      try { await dkPost("/api/docker/images/pull", { name: name.value.trim() }); name.value = ""; loadDocker(); } catch (ex) { toast(ex.message); }
    } }, name, el("button", { class: "small primary" }, withIcon("download", "Download")),
    el("button", { type: "button", class: "small ghost", onclick: async () => {
      hub.innerHTML = "";
      if (!name.value.trim()) { toast("Type a name to search for."); return; }
      try {
        const r = await api(`/api/docker/search?q=${encodeURIComponent(name.value.trim())}`);
        for (const x of r) hub.append(el("div", { class: "dk-hub-item" },
          el("div", {}, el("strong", { class: "mono" }, x.name), x.official ? el("span", { class: "tag tag-accent" }, "Official") : null, el("span", { class: "muted small" }, ` ★ ${x.stars}`)),
          el("div", { class: "small muted" }, x.description),
          el("div", { class: "row" },
            el("button", { type: "button", class: "small", onclick: () => openDockerDialog(null, x.name) }, withIcon("plus", "Create container")),
            el("a", { href: x.url, target: "_blank", rel: "noopener noreferrer", class: "small muted" }, "Docker Hub page"))));
        if (!r.length) hub.append(el("p", { class: "small muted" }, "Nothing found."));
      } catch (ex) { toast(ex.message); }
    } }, "Search Docker Hub")), hub);
  const rows = el("div", { class: "dk-rows" });
  if (!list.length) rows.append(el("p", { class: "muted small" }, "No images yet."));
  for (const i of list) rows.append(el("div", { class: "dk-row" },
    el("div", { class: "dk-row-main" },
      el("span", { class: "icon-tile small-tile", html: icon("box") }),
      el("div", { class: "dk-row-title" }, el("strong", { class: "mono" }, i.tags[0] || `(untagged) ${i.short_id}`),
        i.tags.length > 1 ? el("div", { class: "small muted" }, `also ${i.tags.slice(1).join(", ")}`) : null),
      el("div", { class: "small muted dk-row-status" }, `${fmtBytes(i.size)} · ${new Date(i.created * 1000).toLocaleDateString()}`,
        el("div", {}, i.containers.length ? `used by ${i.containers.join(", ")}` : "not used"))),
    el("div", { class: "dk-row-actions" },
      i.tags[0] ? el("button", { type: "button", class: "small", onclick: () => openDockerDialog(null, i.tags[0]) }, withIcon("plus", "Create container")) : null,
      i.tags[0] ? copyTo(i.tags[0]) : null,
      i.tags[0] ? el("button", { type: "button", class: "small ghost", title: "Download the newest version of this tag",
        onclick: async () => { try { await dkPost("/api/docker/images/pull", { name: i.tags[0] }); loadDocker(); } catch (ex) { toast(ex.message); } } }, withIcon("refresh", "Update")) : null,
      el("button", { type: "button", class: "small ghost danger", onclick: async () => {
        if (!confirm(`Delete the image ${i.tags[0] || i.short_id} (${fmtBytes(i.size)})?`)) return;
        try { await api(`/api/docker/images/remove?force=${i.containers.length ? "false" : "true"}`, { method: "POST", body: JSON.stringify({ name: i.id }) }); }
        catch (ex) { toast(ex.message); }
        refreshDockerTab();
      } }, withIcon("trash", "Delete")))));
  pane.append(rows);
}

// ---- stacks (Docker Compose)
async function dkStacks() {
  const list = await api("/api/docker/stacks");
  if (state.dk.tab !== "stacks") return;
  const pane = $("#dk-pane");
  pane.innerHTML = "";
  const compose = state.dk.status?.compose;
  pane.append(dkToolbar(
    el("button", { type: "button", class: "primary small", disabled: !compose, onclick: () => openStackDialog() }, withIcon("plus", "New stack")),
    el("span", { class: "small muted" }, compose ? "A stack is several containers described in one docker-compose file (e.g. an app + its database)."
      : "Docker Compose isn't installed, so stacks aren't available.")));
  const rows = el("div", { class: "dk-rows" });
  if (!list.length) rows.append(el("p", { class: "muted small" }, "No stacks yet. Paste a docker-compose file from an app's documentation to start one."));
  for (const s of list) {
    const act = (action, label, ic, cls = "ghost") => el("button", { type: "button", class: `small ${cls}`, disabled: !s.managed, onclick: async () => {
      if (action === "down" && !confirm(`Stop and remove the containers of “${s.name}”? Volumes and shared folders are kept.`)) return;
      try { await dkPost(`/api/docker/stacks/${s.name}/action`, { action }); loadDocker(); } catch (ex) { toast(ex.message); }
    } }, withIcon(ic, label));
    const ports = s.containers.flatMap((c) => c.ports.filter((p) => p.host && p.proto === "tcp").map((p) =>
      el("a", { class: "tag tag-accent dk-port", href: hostUrl(p.host), target: "_blank", rel: "noopener noreferrer" }, iconEl("link"), `${p.host}`)));
    rows.append(el("div", { class: `dk-row ${s.running ? "running" : ""}` },
      el("div", { class: "dk-row-main" },
        el("span", { class: `dot ${s.running === s.total && s.total ? "on" : s.running ? "warn" : "off"}` }),
        el("div", { class: "dk-row-title" }, el("strong", {}, s.name), s.managed ? null : el("span", { class: "tag", title: "Started outside SysAdmin.IA: its compose file isn't here" }, "external"),
          el("div", { class: "small muted dk-ellipsis" }, s.containers.map((c) => c.name).join(", ") || "not running")),
        el("div", { class: "small muted dk-row-status" }, `${s.running} of ${s.total} running`)),
      ports.length ? el("div", { class: "dk-row-links small" }, ports) : null,
      el("div", { class: "dk-row-actions" },
        s.running ? null : act("up", "Start", "play", "primary"),
        s.running ? act("restart", "Restart", "refresh") : null,
        act("up", "Update", "download"),
        s.total ? act("down", "Stop", "stop") : null,
        el("button", { type: "button", class: "small ghost", disabled: !s.managed, onclick: () => openDockerLogs(null, s.name) }, withIcon("file", "Logs")),
        el("button", { type: "button", class: "small ghost", disabled: !s.managed, onclick: () => openStackDialog(s.name) }, withIcon("edit", "Edit")),
        el("button", { type: "button", class: "small ghost danger", disabled: !s.managed, onclick: async () => {
          if (!confirm(`Delete the stack “${s.name}”? Its containers are stopped and removed. Volumes and shared folders are kept.`)) return;
          try { await api(`/api/docker/stacks/${s.name}`, { method: "DELETE" }); } catch (ex) { toast(ex.message); }
          refreshDockerTab();
        } }, withIcon("trash", "Delete")))));
  }
  pane.append(rows);
}

async function openStackDialog(name = null) {
  const d = $("#dk-stack-dialog");
  const f = $("#dk-stack-form");
  f.reset();
  $("#dk-stack-error").hidden = true;
  $("#dk-stack-title").textContent = name ? `Edit stack ${name}` : "New stack";
  f.name.readOnly = !!name;
  if (name) {
    try { const s = await api(`/api/docker/stacks/${name}`); f.name.value = s.name; f.compose.value = s.compose; f.env.value = s.env; }
    catch (ex) { toast(ex.message); return; }
  }
  // quick inserts: a drive folder path at the cursor
  const ins = $("#dk-stack-insert");
  ins.innerHTML = "";
  try { state.dk.host = await api("/api/docker/host"); } catch { /* ignore */ }   // the page's computer (not a dialog's)
  const drives = state.dk.host?.drives || [];
  if (drives.length) ins.append(el("span", { class: "muted" }, "Insert a folder path: "),
    drives.map((dr) => el("button", { type: "button", class: "small ghost", onclick: () => pickHostFolder(dr.path + "/", (p) => {
      const ta = f.compose; const at = ta.selectionStart ?? ta.value.length;
      ta.value = ta.value.slice(0, at) + p + ta.value.slice(ta.selectionEnd ?? at); ta.focus();
    }, dkTarget()) }, iconEl(dr.external ? "external" : "internal"), ` ${dr.label || dr.path}`)));
  d.showModal();
}
async function saveStack(start) {
  const f = $("#dk-stack-form");
  const err = $("#dk-stack-error");
  err.hidden = true;
  const name = f.name.value.trim();
  try {
    await api(`/api/docker/stacks/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify({ compose: f.compose.value, env: f.env.value }) });
    if (start) await dkPost(`/api/docker/stacks/${encodeURIComponent(name)}/action`, { action: "up" });
    $("#dk-stack-dialog").close();
    toast(start ? `Starting stack ${name}…` : `Saved ${name}.`);
    showDockerTab("stacks");
    loadDocker();
  } catch (ex) { err.textContent = ex.message; err.hidden = false; }
}
$("#dk-stack-cancel").addEventListener("click", () => $("#dk-stack-dialog").close());
$("#dk-stack-save").addEventListener("click", () => saveStack(false));
$("#dk-stack-form").addEventListener("submit", (e) => { e.preventDefault(); saveStack(true); });

// ---- volumes
async function dkVolumes() {
  const list = await api("/api/docker/volumes");
  if (state.dk.tab !== "volumes") return;
  const pane = $("#dk-pane");
  pane.innerHTML = "";
  const name = el("input", { placeholder: "New volume name", spellcheck: "false", maxlength: "63" });
  pane.append(el("form", { class: "dk-toolbar", onsubmit: async (e) => {
    e.preventDefault();
    try { await dkPost("/api/docker/volumes", { name: name.value.trim() }); name.value = ""; refreshDockerTab(); } catch (ex) { toast(ex.message); }
  } }, name, el("button", { class: "small" }, withIcon("plus", "Create volume")),
    el("span", { class: "small muted" }, "Volumes are storage managed by Docker. To keep data on a specific drive, share a folder instead.")));
  const rows = el("div", { class: "dk-rows" });
  if (!list.length) rows.append(el("p", { class: "muted small" }, "No volumes."));
  for (const v of list) rows.append(el("div", { class: "dk-row" },
    el("div", { class: "dk-row-main" },
      el("span", { class: "icon-tile small-tile", html: icon("internal") }),
      el("div", { class: "dk-row-title" }, el("strong", { class: "mono dk-ellipsis" }, v.name), v.stack ? el("span", { class: "tag" }, v.stack) : null,
        el("div", { class: "small muted mono dk-ellipsis" }, v.mountpoint)),
      el("div", { class: "small muted dk-row-status" }, v.containers.length ? `used by ${v.containers.join(", ")}` : "not used")),
    el("div", { class: "dk-row-actions" },
      el("button", { type: "button", class: "small ghost danger", disabled: v.containers.length > 0, title: v.containers.length ? "In use" : "", onclick: async () => {
        if (!confirm(`Delete the volume ${v.name} and all the data in it? This can't be undone.`)) return;
        try { await api(`/api/docker/volumes/${encodeURIComponent(v.name)}`, { method: "DELETE" }); } catch (ex) { toast(ex.message); }
        refreshDockerTab();
      } }, withIcon("trash", "Delete")))));
  pane.append(rows);
}

// ---- networks
async function dkNetworks() {
  const list = await api("/api/docker/networks");
  if (state.dk.tab !== "networks") return;
  const containers = await api("/api/docker/containers").catch(() => []);
  const pane = $("#dk-pane");
  pane.innerHTML = "";
  const name = el("input", { placeholder: "New network name", spellcheck: "false", maxlength: "63" });
  const internal = el("input", { type: "checkbox" });
  pane.append(el("form", { class: "dk-toolbar", onsubmit: async (e) => {
    e.preventDefault();
    try { await dkPost("/api/docker/networks", { name: name.value.trim(), internal: internal.checked }); name.value = ""; refreshDockerTab(); } catch (ex) { toast(ex.message); }
  } }, name, el("label", { class: "check small" }, internal, " No internet (internal)"), el("button", { class: "small" }, withIcon("plus", "Create network")),
    el("span", { class: "small muted" }, "Containers on the same network reach each other by name (e.g. http://db:5432).")));
  const rows = el("div", { class: "dk-rows" });
  for (const n of list) {
    const others = containers.filter((c) => !n.containers.includes(c.name));
    const add = !n.builtin || n.name === "bridge" ? el("select", { class: "small", onchange: async (e) => {
      if (!e.target.value) return;
      try { await dkPost(`/api/docker/networks/${encodeURIComponent(n.name)}/connect`, { container: e.target.value, connect: true }); } catch (ex) { toast(ex.message); }
      refreshDockerTab();
    } }, el("option", { value: "" }, "Connect a container…"), others.map((c) => el("option", { value: c.id }, c.name))) : null;
    rows.append(el("div", { class: "dk-row" },
      el("div", { class: "dk-row-main" },
        el("span", { class: "icon-tile small-tile", html: icon("link") }),
        el("div", { class: "dk-row-title" }, el("strong", {}, n.name), n.builtin ? el("span", { class: "tag" }, "built-in") : null,
          n.internal ? el("span", { class: "tag" }, "no internet") : null, n.stack ? el("span", { class: "tag" }, n.stack) : null,
          el("div", { class: "small muted" }, [n.driver, n.subnet].filter(Boolean).join(" · "))),
        el("div", { class: "small muted dk-row-status" }, n.containers.length ? n.containers.join(", ") : "no containers")),
      el("div", { class: "dk-row-actions" }, add,
        n.builtin ? null : el("button", { type: "button", class: "small ghost danger", disabled: n.containers.length > 0, onclick: async () => {
          if (!confirm(`Delete the network ${n.name}?`)) return;
          try { await api(`/api/docker/networks/${encodeURIComponent(n.name)}`, { method: "DELETE" }); } catch (ex) { toast(ex.message); }
          refreshDockerTab();
        } }, withIcon("trash", "Delete")))));
  }
  pane.append(rows);
}

// ---- clean up
async function dkCleanup() {
  const pane = $("#dk-pane");
  pane.innerHTML = "";
  const usage = el("div", { class: "u-stats" }, el("p", { class: "small busy" }, el("span", { class: "spinner", "aria-hidden": "true" }), " Measuring disk use…"));
  const opts = [["containers", "Stopped containers", true], ["images", "Images not used by any container", true],
                ["networks", "Unused networks", true], ["build_cache", "Build cache", true],
                ["volumes", "Unused volumes (deletes their data!)", false]];
  const checks = opts.map(([id, label, on]) => el("label", { class: "check small" }, el("input", { type: "checkbox", value: id, checked: on }), ` ${label}`));
  const result = el("p", { class: "small", hidden: true });
  pane.append(usage, el("fieldset", { class: "dk-section" }, el("legend", {}, "Free up disk space"), checks,
    el("div", {}, el("button", { type: "button", class: "small primary", onclick: async (e) => {
      const what = checks.map((c) => c.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
      if (!what.length) return;
      if (!confirm(`Delete: ${what.join(", ").replace("_", " ")}?${what.includes("volumes") ? "\n\nUnused volumes and their data are deleted for good." : ""}`)) return;
      e.currentTarget.disabled = true;
      try {
        const r = await dkPost("/api/docker/prune", { what });
        result.textContent = `Freed ${fmtBytes(r.freed)}.` + Object.entries(r.removed).map(([k, v]) => ` ${v} ${k} removed.`).join("");
        result.className = "small ok-text"; result.hidden = false;
      } catch (ex) { result.textContent = ex.message; result.className = "small error"; result.hidden = false; }
      e.currentTarget.disabled = false;
      dkCleanup();
    } }, withIcon("trash", "Clean up"))), result));
  try {
    const d = await api("/api/docker/disk");
    const stat = (label, value, sub) => el("div", { class: "u-stat" }, el("div", { class: "label" }, label), el("div", { class: "u-value" }, value), el("div", { class: "muted small" }, sub));
    usage.innerHTML = "";
    usage.append(stat("Images", fmtBytes(d.images), `${d.images_count} images`), stat("Containers", fmtBytes(d.containers), `${d.containers_count} containers (their own files)`),
      stat("Volumes", fmtBytes(d.volumes), `${d.volumes_count} volumes`), stat("Build cache", fmtBytes(d.build_cache), ""));
  } catch (ex) { usage.innerHTML = ""; usage.append(el("p", { class: "small error" }, ex.message)); }
}

// ---- remove Docker
$("#dk-uninstall").addEventListener("click", async () => {
  const del = $("#dk-delete-data").checked;
  if (!confirm(`Remove Docker from this server?${del ? "\n\nAll containers, images and volumes are deleted for good." : ""}`)) return;
  try { const r = await dkPost("/api/docker/uninstall", { delete_data: del }); toast(r.message); } catch (ex) { toast(ex.message); }
  state.dk.loadedOnce = false;
  loadDocker();
});

if (state.view === "containers") loadDocker();
