// ------------------------------------------------------------ Devices (Server → Devices)
// Other Linux computers controlled over SSH with a key from Server → SSH keys: resources and drives,
// modules (Python, Docker, Ollama) installed from here in a Console tab, and the "Runs on" picker of the
// Containers and Local AI pages (state.target, used by api() in app.js). Dialogs reuse #net-dialog (network.js).
"use strict";

const DEV_STATE = {
  online: ["online", "ok"], unreachable: ["offline", "warn"], auth: ["key not accepted", "warn"],
  hostkey: ["identity changed", "error"], no_python: ["needs Python", "warn"], no_key: ["no key", "warn"],
  ssh_missing: ["no ssh", "error"], error: ["error", "error"],
};
state.devices = { open: {} };

async function loadDevices(live = true) {
  clearTimeout(state.devices.timer);
  const list = $("#dev-list");
  if (!state.devices.data) {
    try { state.devices.data = await api("/api/devices"); renderDevices(); } catch (ex) {
      list.innerHTML = "";
      list.append(el("div", { class: "card" }, el("p", { class: "error" }, ex.message)));
      return;
    }
  }
  if (live && state.devices.data.devices.length) {
    $("#dev-refresh").disabled = true;
    try { state.devices.data = await api("/api/devices?live=1"); } catch (ex) { toast(ex.message); }
    $("#dev-refresh").disabled = false;
  }
  if (state.view !== "devices") return;
  renderDevices();
  state.devices.timer = setTimeout(() => loadDevices(true), 15000);
}

function renderDevices() {
  const data = state.devices.data;
  const list = $("#dev-list");
  const notice = $("#dev-notice");
  notice.innerHTML = "";
  if (!data.ssh) notice.append(el("div", { class: "card warn" }, el("p", { class: "small" },
    "The ssh command isn't installed on this server, so devices can't be reached. Install the openssh-client package.")));
  else if (!data.keys.length) notice.append(el("div", { class: "card" }, el("p", { class: "small" },
    "SysAdmin.IA signs in to devices with an SSH key. Make one first in Server → SSH keys."),
    el("button", { type: "button", class: "small", onclick: () => switchView("sshkeys") }, withIcon("key", "Open SSH keys"))));
  list.innerHTML = "";
  if (!data.devices.length) {
    list.append(el("div", { class: "card empty-state" },
      el("p", {}, "No devices yet."),
      el("p", { class: "muted small" }, "Add another Linux computer (a Raspberry Pi, a NAS, a second server…) to watch its resources and drives, install Docker or Ollama on it, run its containers and AI models from here, and run job tasks on it.")));
    return;
  }
  for (const d of data.devices) list.append(deviceCard(d, data));
}

function devPill(d) {
  const [t, c] = DEV_STATE[d.last_state] || ["not checked", ""];
  return el("span", { class: `status-pill ${c}` }, t);
}

function devMeter(label, pct, sub) {
  return el("div", { class: "dev-meter" },
    el("div", { class: "dev-meter-top small" }, el("span", { class: "muted" }, label), el("strong", {}, pct == null ? "–" : `${Math.round(pct)}%`)),
    usageBar(pct || 0), sub ? el("div", { class: "muted small" }, sub) : null);
}

function deviceCard(d, data) {
  const st = d.stats || {}, info = d.info || {};
  const online = d.last_state === "online";
  const root = (st.drives || []).find((x) => x.mount === "/") || (st.drives || [])[0];
  const hot = (st.temps || []).reduce((m, t) => Math.max(m, t.c), -1);
  const where = `${d.user}@${d.host}${d.port !== 22 ? `:${d.port}` : ""}`;
  const consoleOk = !!state.me?.console;
  const card = el("div", { class: `card dev-card ${online ? "" : "dev-off"}` });
  const head = el("div", { class: "ai-card-head" },
    el("span", { class: `icon-tile ${online ? "accent" : ""}`, html: icon("server") }),
    el("div", { class: "ai-card-title" },
      el("div", {}, el("strong", { "data-no-i18n": "" }, d.name), " ", devPill(d)),
      el("div", { class: "muted small" }, el("span", { class: "mono", "data-no-i18n": "" }, where), " · ",
        info.os ? el("span", { "data-no-i18n": "" }, `${info.os} · ${info.arch}`) : el("span", {}, "system not read yet"))),
    el("div", { class: "ai-actions" },
      el("button", { type: "button", class: "small", onclick: () => toggleDeviceDetails(d.id) },
        withIcon("monitor", state.devices.open[d.id] ? "Hide details" : "Details")),
      el("button", { type: "button", class: "small ghost", disabled: !consoleOk, title: consoleOk ? "" : "The Console is turned off on this server",
        onclick: () => openDeviceConsole(d) }, withIcon("console", "Console")),
      el("button", { type: "button", class: "small ghost", onclick: () => deviceDialog(d) }, withIcon("edit", "Edit")),
      el("button", { type: "button", class: "small ghost danger", onclick: () => deviceDelete(d) }, withIcon("trash", "Delete"))));
  card.append(head);
  if (d.description) card.append(el("p", { class: "muted small dev-desc" }, d.description));
  if (!online && d.last_error) card.append(deviceProblem(d));
  if (st.mem) {
    card.append(el("div", { class: "dev-meters" },
      devMeter("CPU", st.cpu, st.load && st.load.length ? `load ${st.load.map((x) => x.toFixed(2)).join(" ")}` : `${st.cpus || "?"} cores`),
      devMeter("Memory", st.mem.percent, `${fmtBytes(st.mem.used)} of ${fmtBytes(st.mem.total)}`),
      root ? devMeter(`Disk ${root.mount}`, root.percent, `${fmtBytes(root.free)} free`) : null,
      el("div", { class: "dev-meter" }, el("div", { class: "dev-meter-top small" }, el("span", { class: "muted" }, "Up"),
        el("strong", {}, st.uptime ? fmtDuration(st.uptime) : "–")),
        el("div", { class: "muted small" }, hot >= 0 ? `hottest ${hot.toFixed(0)} °C` : `↓ ${fmtBytes(st.net?.rx_rate, true)} ↑ ${fmtBytes(st.net?.tx_rate, true)}`))));
    if (!online && d.last_seen) card.append(el("p", { class: "muted small" }, `Last seen ${fmtWhen(d.last_seen)}: these are the last values read.`));
  }
  card.append(deviceModules(d, data));
  if (state.devices.open[d.id]) card.append(deviceDetails(d));
  return card;
}

function deviceProblem(d) {
  const box = el("div", { class: "note warn dev-problem" }, el("p", { class: "small" }, d.last_error));
  const acts = el("div", { class: "row" });
  if (d.last_state === "auth" && state.me?.console) acts.append(el("button", { type: "button", class: "small primary",
    onclick: () => deviceInstallKey(d, "setup") }, withIcon("upload", "Install the key on the device")));
  if (d.last_state === "hostkey") acts.append(el("button", { type: "button", class: "small", onclick: async () => {
    if (!confirm(`Forget the old identity of ${d.host}?\n\nDo this only if you know the device was reinstalled or replaced: the next connection accepts the key it shows then.`)) return;
    try { await api("/api/ssh/known-hosts/remove", { method: "POST", body: JSON.stringify({ host: d.port === 22 ? d.host : `[${d.host}]:${d.port}` }) }); }
    catch (ex) { toast(ex.message, 6000); }
    deviceReconnect(d.id);
  } }, withIcon("shield", "Forget the old identity")));
  if (d.last_state === "no_python" && state.me?.console) acts.append(el("button", { type: "button", class: "small primary",
    onclick: () => deviceInstallModule(d.id, "python") }, withIcon("python", "Install Python")));
  acts.append(el("button", { type: "button", class: "small ghost", onclick: () => deviceReconnect(d.id) }, withIcon("refresh", "Try again")));
  box.append(acts);
  return box;
}

function deviceModules(d, data) {
  const box = el("div", { class: "dev-modules" });
  const known = !!d.info;
  for (const m of data.modules) {
    const s = (d.modules || {})[m.id] || {};
    if (m.id === "docker_access" && !(d.modules?.docker?.installed)) continue;   // only once Docker is there
    const ok = s.installed === true;
    const sub = m.id === "python" ? s.version : m.id === "docker" ? [s.version, s.compose ? `Compose ${s.compose}` : ""].filter(Boolean).join(" · ")
      : m.id === "ollama" ? (s.version || (s.installed ? (s.running ? "running" : "not running") : "")) : s.needs_login ? "reconnect to apply" : "";
    const chip = el("div", { class: `dev-mod ${ok ? "ok" : ""}`, title: m.about },
      el("span", { class: "dev-mod-icon", html: icon(m.icon) }),
      el("span", { class: "dev-mod-text" }, el("strong", { class: "small" }, m.label),
        el("span", { class: "muted small", "data-no-i18n": sub && m.id !== "docker_access" ? "" : null },
          !known ? "unknown" : ok ? (sub || "installed") : (sub || "not installed"))));
    if (known && !ok && d.last_state === "online" && state.me?.console) chip.append(el("button", { type: "button", class: "small",
      onclick: () => deviceInstallModule(d.id, m.id) }, withIcon("download", m.id === "docker_access" ? "Give access" : "Install")));
    if (ok && m.id === "docker") chip.append(el("button", { type: "button", class: "small ghost", onclick: () => deviceOpenModule(d.id, "containers") }, withIcon("box", "Containers")));
    if (ok && m.id === "ollama") chip.append(el("button", { type: "button", class: "small ghost", onclick: () => deviceOpenModule(d.id, "localai") }, withIcon("chip", "Local AI")));
    box.append(chip);
  }
  // sudo: set up once (password typed in a Console tab), then installs don't ask for it
  const sudo = (d.info || {}).sudo;
  if (known && sudo && sudo !== "none") {
    const free = devSudoFree(d);
    const chip = el("div", { class: `dev-mod ${free ? "ok" : ""}`, title: "Installs on the device use sudo." },
      el("span", { class: "dev-mod-icon", html: icon("shield") }),
      el("span", { class: "dev-mod-text" }, el("strong", { class: "small" }, "sudo"),
        el("span", { class: "muted small" }, sudo === "root" ? "signed in as root" : free ? "no password needed" : "asks for the password")));
    if (!free && d.last_state === "online" && state.me?.console) chip.append(el("button", { type: "button", class: "small",
      onclick: () => {
        if (confirm(`Let SysAdmin.IA use sudo on ${d.name} without a password?\n\nInstalls then never ask for the password. This gives SysAdmin.IA's admins root on the device.\n\nA Console tab opens: type the password of ${d.user} once when sudo asks for it.`)) deviceInstallKey(d, "sudo");
      } }, withIcon("shield", "Set up without a password")));
    box.append(chip);
  }
  return box;
}

function toggleDeviceDetails(id) {
  state.devices.open[id] = !state.devices.open[id];
  renderDevices();
  if (state.devices.open[id]) api(`/api/devices/${id}`).then((d) => {
    const i = state.devices.data.devices.findIndex((x) => x.id === id);
    if (i >= 0) { state.devices.data.devices[i] = d; renderDevices(); }
  }).catch(() => {});
}

function deviceDetails(d) {
  const st = d.stats || {}, info = d.info || {};
  const facts = [
    ["Host name", info.hostname, true], ["System", info.os, true], ["Kernel", info.kernel, true],
    ["Processor", info.cpu_model ? `${info.cpu_model} · ${info.cpus} cores` : null, true],
    ["Graphics", (info.gpus || []).length ? info.gpus.join(", ") : "No GPU found", !!(info.gpus || []).length],
    ["Memory", st.mem ? `${fmtBytes(st.mem.total)} · ${fmtBytes(st.mem.available)} free` : null, false],
    ["Swap", st.swap && st.swap.total ? `${fmtBytes(st.swap.used)} of ${fmtBytes(st.swap.total)}` : "none", false],
    ["Network", st.net ? `↓ ${fmtBytes(st.net.rx_rate, true)} · ↑ ${fmtBytes(st.net.tx_rate, true)}` : null, false],
    ["Disk activity", st.disk_io ? `read ${fmtBytes(st.disk_io.read_rate, true)} · write ${fmtBytes(st.disk_io.write_rate, true)}` : null, false],
    ["Processes", st.procs, false],
    ["Python", info.python, true],
    ["sudo", { root: "signed in as root", nopasswd: "works without a password", password: "asks for the password", none: "not installed" }[info.sudo], false],
    ["SSH key", d.key_name, true],
  ].filter((f) => f[1] != null && f[1] !== "");
  const box = el("div", { class: "dev-details" },
    el("dl", { class: "proc-facts small" }, ...facts.flatMap(([a, b, raw]) => [el("dt", { class: "muted" }, a),
      el("dd", { class: raw ? "mono" : "", "data-no-i18n": raw ? "" : null }, String(b))])));
  if ((st.temps || []).length) box.append(el("div", { class: "flow-label" }, "Temperatures"),
    el("div", { class: "dev-temps" }, ...st.temps.map((t) => el("span", { class: `tag ${t.c >= 80 ? "tag-warn" : ""}`, "data-no-i18n": "" },
      `${t.sensor}${t.label && t.label !== t.sensor ? ` ${t.label}` : ""}: ${t.c} °C`))));
  box.append(el("div", { class: "flow-label" }, "Drives"));
  if ((st.drives || []).length) {
    box.append(el("div", { class: "table-wrap" }, el("table", { class: "pyenv-ver-table small dev-drives" },
      el("thead", {}, el("tr", {}, el("th", {}, "Mounted at"), el("th", {}, "Device"), el("th", {}, "Type"), el("th", {}, "Size"), el("th", {}, "Free"), el("th", {}, "Used"))),
      el("tbody", {}, ...st.drives.map((v) => el("tr", {},
        el("td", { class: "mono", "data-no-i18n": "" }, v.mount + (v.readonly ? " (read-only)" : "")),
        el("td", { class: "mono muted", "data-no-i18n": "" }, v.device),
        el("td", { class: "mono", "data-no-i18n": "" }, v.fstype),
        el("td", {}, fmtBytes(v.total)), el("td", {}, fmtBytes(v.free)),
        el("td", { class: "dev-used" }, usageBar(v.percent), el("span", { class: "muted" }, `${Math.round(v.percent)}%`))))))));
  } else box.append(el("p", { class: "muted small" }, d.last_state === "online" ? "No drives found." : "Not read yet."));
  if ((d.used_by || []).length) box.append(el("div", { class: "flow-label" }, "Jobs with tasks on this device"),
    el("p", { class: "small mono", "data-no-i18n": "" }, d.used_by.join(", ")));
  box.append(el("div", { class: "row" },
    el("button", { type: "button", class: "small ghost", onclick: () => deviceReconnect(d.id) }, withIcon("refresh", "Reconnect and read again")),
    info.agent_time ? el("span", { class: "muted small" }, `System read ${fmtWhen(d.info_at)}`) : null));
  return box;
}

// ---- add / edit / delete
async function deviceDialog(d = null) {
  const data = state.devices.data || await api("/api/devices");
  const host = await sshDevicePicker();
  if (d) host.value = d.host;
  const key = el("select", { name: "key_name", required: true },
    el("option", { value: "" }, data.keys.length ? "Choose a key…" : "No keys yet: make one in Server → SSH keys"),
    ...data.keys.map((k) => el("option", { value: k }, k)));
  key.value = d ? d.key_name : (data.default_key || data.keys[0] || "");
  const result = el("div", { class: "small dev-test", hidden: true });
  const test = el("button", { type: "button", class: "small ghost", onclick: async () => {
    const f = $("#net-form");
    result.hidden = false;
    result.className = "small dev-test busy";
    result.textContent = "Connecting…";
    test.disabled = true;
    try {
      const r = await api("/api/devices/test", { method: "POST", body: JSON.stringify(deviceForm(f)) });
      result.className = `small dev-test ${r.ok ? "ok-text" : "error"}`;
      result.textContent = r.ok ? `Connected: ${r.info.hostname} · ${r.info.os} · ${r.info.cpus} cores · ${fmtBytes(r.info.mem_total)} memory` : r.error;
    } catch (ex) { result.className = "small dev-test error"; result.textContent = ex.message; }
    test.disabled = false;
  } }, withIcon("test", "Test the connection"));
  netDialog(d ? `Edit ${d.name}` : "Add a device", [
    field("Name", el("input", { name: "name", required: true, maxlength: "48", value: d ? d.name : "", placeholder: "Garage Pi" })),
    field("Address", host, "Its IP address or name. The device needs an SSH server (openssh-server)."),
    el("div", { class: "row ssh-target-row" },
      field("User on the device", el("input", { name: "user", required: true, value: d ? d.user : lastSshUser(), spellcheck: "false", class: "mono", autocomplete: "off" })),
      field("SSH port", el("input", { name: "port", type: "number", min: "1", max: "65535", value: d ? String(d.port) : "22" }))),
    field("SSH key", key, "SysAdmin.IA signs in with this key, never with a password. If the key isn't on the device yet, you can install it after saving (the device's password is asked once)."),
    field("Description", el("input", { name: "description", maxlength: "300", value: d ? d.description : "", placeholder: "What it is or where it is" })),
    el("div", { class: "row" }, test), result,
    d ? null : el("label", { class: "check" }, el("input", { type: "checkbox", name: "sudo_setup", checked: true }),
      el("span", {}, "Let SysAdmin.IA use sudo on the device without a password"),
      el("span", { class: "muted small" }, "Set up together with the key (the password is typed once), so installing Docker, Ollama or Python never asks for it. This gives SysAdmin.IA's admins root on the device.")),
    el("p", { class: "muted small" }, "Tasks and installs run as this user."),
  ], async (f) => {
    const body = deviceForm(f);
    const sudo = !d && f.sudo_setup?.checked;
    const saved = await api(d ? `/api/devices/${d.id}` : "/api/devices", { method: d ? "PUT" : "POST", body: JSON.stringify(body) });
    toast(d ? "Saved." : `${saved.name} added. Connecting…`);
    state.devices.data = null;
    devTargetCache = null;
    const fresh = await api(`/api/devices/${saved.id}`).catch(() => null);
    await loadDevices(false);
    if (!fresh || !state.me?.console) return;
    if (fresh.last_state === "auth"
        && confirm(`${fresh.name} didn't accept the key ${fresh.key_name} yet.\n\nInstall it now${sudo ? " and set up sudo without a password" : ""}? A Console tab opens: type the password of ${fresh.user} on the device when asked.`)) {
      deviceInstallKey(fresh, sudo ? "setup" : "key");
    } else if (sudo && fresh.last_state === "online" && !devSudoFree(fresh)
        && confirm(`The key works. Set up sudo without a password on ${fresh.name} now?\n\nA Console tab opens: type the password of ${fresh.user} once when sudo asks for it.`)) {
      deviceInstallKey(fresh, "sudo");
    }
  }, d ? "Save" : "Add");
}

function deviceForm(f) {
  return { name: f.name.value.trim(), host: f.host.value.trim(), user: f.user.value.trim(), port: Number(f.port.value || 22),
           key_name: f.key_name.value, description: f.description.value };
}

async function deviceDelete(d) {
  if (!confirm(`Remove ${d.name} from SysAdmin.IA?\n\nNothing on the device changes: its containers, models and files stay, and the key stays in its authorized_keys.`)) return;
  try { await api(`/api/devices/${d.id}`, { method: "DELETE" }); toast(`${d.name} removed.`); }
  catch (ex) { toast(ex.message, 7000); }
  for (const m of Object.keys(TARGET_BARS)) if (String((state.target || {})[m]) === String(d.id)) setTarget(m, "");
  state.devices.data = null;
  devTargetCache = null;
  loadDevices(false);
}

// ---- console actions: install the key (+ passwordless sudo), install a module, reconnect
// step: "setup" = key + sudo without a password, "key" = key only, "sudo" = sudo only (the key already works)
function deviceInstallKey(d, step = "setup") {
  const label = { setup: "Install the key and set up sudo", key: "Install the key", sudo: "Set up sudo" }[step];
  openDeviceConsole(d, step, label);
  toast(step === "sudo" ? `Type the password of ${d.user} once when sudo asks for it.`
    : `Type the password of ${d.user} on the device when asked${step === "setup" ? " (to sign in, then once more for sudo)" : ""}.`, 8000);
}

const devSudoFree = (d) => ["nopasswd", "root"].includes((d.info || {}).sudo);

function deviceInstallModule(id, module) {
  const d = (state.devices.data?.devices || []).find((x) => x.id === id) || (devTargetCache || []).find((x) => x.id === id);
  const m = (state.devices.data?.modules || []).find((x) => x.id === module) || { label: module, about: "" };
  if (!d) { toast("Open Server → Devices first."); return; }
  const why = { docker: "Docker's official install script (get.docker.com) runs with sudo, then the user is added to the docker group (root-equivalent on the device).",
                docker_access: "The user is added to the docker group (root-equivalent on the device).",
                ollama: "Ollama's official installer (ollama.com/install.sh) runs; it uses sudo.",
                python: "Python 3 is installed with the device's package manager (sudo)." }[module] || "";
  const pw = devSudoFree(d) ? "A Console tab opens and shows the install. No password is needed: sudo is set up for SysAdmin.IA on the device."
    : `A Console tab opens: if sudo asks for the password of ${d.user}, type it there. (Set up sudo without a password on the device card to skip this next time.)`;
  if (!confirm(`Install ${m.label} on ${d.name}?\n\n${why}\n\n${pw}`)) return;
  openDeviceConsole(d, module, m.label);
}

// Called by the console when a module install or setup session ends (app.js).
async function deviceAfterModule(id, module, code) {
  try { await api(`/api/devices/${id}/refresh`, { method: "POST" }); } catch { /* shown on the Devices page */ }
  const setup = ["key", "setup", "sudo"].includes(module);
  toast(code === 0 ? (setup ? "Done. SysAdmin.IA reconnected to the device." : "Installed. SysAdmin.IA reconnected to the device.")
    : setup ? "The setup didn't finish: see the Console tab." : "The install didn't finish: see the Console tab.", 6000);
  state.devices.data = null;
  if (state.view === "devices") loadDevices(false);
}

async function deviceReconnect(id, after) {
  toast("Connecting…");
  try {
    const d = await api(`/api/devices/${id}/refresh`, { method: "POST" });
    toast(d.last_state === "online" ? `${d.name} is online.` : d.last_error || "Not reachable.", 6000);
  } catch (ex) { toast(ex.message, 6000); }
  state.devices.data = null;
  if (after) after();
  else if (state.view === "devices") loadDevices(false);
}

function deviceOpenModule(id, view) {
  setTarget(view === "containers" ? "docker" : "localai", String(id));
  switchView(view);
}

// ---- "Runs on" picker of the Containers and Local AI pages
let devTargetCache = null;

function loadTargets() {
  try { state.target = JSON.parse(localStorage.getItem("sai.target") || "{}") || {}; } catch { state.target = {}; }
}

function setTarget(module, id) {
  state.target = state.target || {};
  if (id) state.target[module] = id; else delete state.target[module];
  try { localStorage.setItem("sai.target", JSON.stringify(state.target)); } catch { /* private mode */ }
}

// module → [bar element, what to do when the device changes]. The view names map to these in app.js switchView.
const TARGET_BARS = {
  docker: ["#dk-target", () => resetDockerView()],
  localai: ["#lai-target", () => resetLaiView()],
  drives: ["#drv-target", () => { state.drives = null; if (state.fb) { state.fb.path = "/"; state.fb.listing = null; } if (state.view === "drives") openDrives(); }],
  pyenvs: ["#pye-target", () => { if (state.view === "pyenvs" && window.loadPyEnvs) loadPyEnvs(); }],
  processes: ["#proc-target", () => { if (state.view === "processes" && window.loadProcesses) loadProcesses(); }],
};
const TARGET_VIEWS = { containers: "docker", localai: "localai", drives: "drives", pyenvs: "pyenvs", processes: "processes" };

async function renderTargetBar(module) {
  const [sel_, reset] = TARGET_BARS[module] || [];
  const box = sel_ && $(sel_);
  if (!box) return;
  if (!devTargetCache) {
    try { devTargetCache = (await api("/api/devices")).devices; } catch { devTargetCache = []; }
  }
  const cur = String((state.target || {})[module] || "");
  if (cur && !devTargetCache.some((d) => String(d.id) === cur)) {   // the device was removed
    setTarget(module, "");
    reset();
  }
  box.innerHTML = "";
  box.hidden = !devTargetCache.length;
  if (!devTargetCache.length) return;
  const sel = el("select", { "aria-label": "Runs on" }, el("option", { value: "" }, "This server"),
    ...devTargetCache.map((d) => el("option", { value: String(d.id) }, `${d.name} (${d.host})`)));
  sel.value = String((state.target || {})[module] || "");
  sel.addEventListener("change", () => {
    setTarget(module, sel.value);
    reset();
  });
  box.append(el("label", { class: "dev-target" }, el("span", { class: "small muted" }, module === "drives" ? "Drives of" : "Runs on"), sel));
}

// The device a page works on (null = this server), with its saved data when known.
function targetDevice(module) {
  const id = String((state.target || {})[module] || "");
  if (!id) return null;
  return (devTargetCache || []).find((d) => String(d.id) === id) || { id: Number(id), name: `#${id}` };
}

function resetDockerView() {
  if (!state.dk) return;
  state.dk.loadedOnce = false;
  state.dk.tasks = [];
  $("#dk-main").hidden = true;
  if (state.view === "containers" && window.loadDocker) loadDocker();
}

function resetLaiView() {
  state.laiModels = null;
  if (state.view === "localai") loadLocalAI();
}

loadTargets();
$("#dev-add").addEventListener("click", () => deviceDialog());
$("#dev-refresh").addEventListener("click", () => loadDevices(true));
Object.assign(window, { loadDevices, renderTargetBar, deviceInstallModule, deviceAfterModule, deviceReconnect, targetDevice, TARGET_VIEWS });
