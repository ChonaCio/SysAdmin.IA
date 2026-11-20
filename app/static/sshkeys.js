// ------------------------------------------------------------ SSH keys (Server → SSH keys)
// Key pairs the server uses to sign in to other devices (Console → SSH tabs), and the devices it already knows.
// Dialogs reuse the Network module's #net-dialog (netDialog / field from network.js).

async function loadSshKeys() {
  const list = $("#ssh-list");
  let st;
  try { st = await api("/api/ssh"); }
  catch (ex) {
    list.innerHTML = "";
    list.append(el("div", { class: "card" }, el("p", { class: "error" }, ex.message)));
    return;
  }
  if (state.view !== "sshkeys") return;
  state.ssh = st;
  const notice = $("#ssh-notice");
  notice.innerHTML = "";
  if (!st.available) notice.append(el("div", { class: "card warn" }, el("p", { class: "small" },
    "ssh-keygen isn't installed on this server, so keys can't be made or read. Install the openssh-client package.")));
  else if (!state.me?.console) notice.append(el("div", { class: "card" }, el("p", { class: "small muted" },
    "The Console is turned off on this server, so keys can't be installed on devices or used from here. They can still be made, downloaded and copied.")));
  $("#ssh-add").disabled = $("#ssh-import").disabled = !st.available;
  list.innerHTML = "";
  if (!st.keys.length) {
    list.append(el("div", { class: "card empty-state" },
      el("p", {}, "No SSH keys yet."),
      el("p", { class: "muted small" }, "Make a key, install it on a device (you type the device's password once), and from then on SSH tabs sign in with the key.")));
  }
  for (const k of st.keys) list.append(sshKeyCard(k, st));
  renderKnownHosts(st);
}

function sshKeyCard(k, st) {
  const isDefault = st.default === k.name;
  const typeTxt = k.type ? `${k.type.toUpperCase()}${k.bits && k.type !== "ed25519" ? ` ${k.bits}` : ""}` : "–";
  const consoleOk = !!state.me?.console;
  const facts = [
    ["Type", typeTxt, true],
    ["Fingerprint", k.fingerprint || "–", true],
    ["Comment", k.comment || "–", true],
    ["Created", k.created ? fmtWhen(k.created) : "–", false],
  ];
  if (k.encrypted) facts.push(["Passphrase", "yes: ssh asks for it in the Console", false]);
  const pubBox = el("textarea", { class: "mono small ssh-pub", readonly: true, rows: "3", spellcheck: "false", "aria-label": "Public key" });
  pubBox.value = k.public_key;
  return el("div", { class: "card proc-card" },
    el("div", { class: "ai-card-head" },
      el("span", { class: `icon-tile ${isDefault ? "accent" : ""}`, html: icon("key") }),
      el("div", { class: "ai-card-title" },
        el("div", {}, el("strong", { "data-no-i18n": "" }, k.name), " ",
          isDefault ? el("span", { class: "status-pill ok" }, "default") : null,
          k.imported ? el("span", { class: "tag" }, "imported") : null),
        el("div", { class: "muted small" }, k.description || "No description")),
      el("div", { class: "ai-actions" },
        el("button", { type: "button", class: "small", disabled: !consoleOk, title: consoleOk ? "" : "The Console is turned off on this server",
          onclick: () => sshInstallDialog(k) }, withIcon("upload", "Install on a device")),
        el("button", { type: "button", class: "small ghost", disabled: !consoleOk, onclick: () => sshConnectDialog(k.name) }, withIcon("console", "Connect")),
        el("button", { type: "button", class: "small ghost", onclick: () => sshEditDialog(k) }, withIcon("edit", "Edit")),
        el("button", { type: "button", class: "small ghost danger", onclick: () => sshDelete(k) }, withIcon("trash", "Delete")))),
    el("dl", { class: "proc-facts small" }, ...facts.flatMap(([a, b, raw]) => [el("dt", { class: "muted" }, a),
      el("dd", { class: raw ? "mono" : "", "data-no-i18n": raw ? "" : null }, String(b))])),
    el("div", { class: "ssh-pub-row" }, el("span", { class: "small muted" }, "Public key (safe to share: this is what goes on the devices)"), pubBox),
    el("div", { class: "row ssh-key-actions" },
      el("button", { type: "button", class: "small ghost", onclick: () => sshCopy(k.public_key) }, withIcon("copy", "Copy public key")),
      el("a", { class: "small button-link", href: `/api/ssh/keys/${encodeURIComponent(k.name)}/public`, download: `${k.name}.pub` }, withIcon("download", "Download .pub")),
      isDefault ? el("button", { type: "button", class: "small ghost", onclick: () => sshSetDefault("") }, withIcon("x", "Stop using by default"))
        : el("button", { type: "button", class: "small ghost", onclick: () => sshSetDefault(k.name),
          title: "SSH tabs opened from Network → Devices sign in with this key unless you pick another" }, withIcon("star", "Use by default")),
      el("button", { type: "button", class: "small ghost danger", onclick: () => sshDownloadPrivate(k) }, withIcon("download", "Download private key"))));
}

function sshCopy(text) {
  const done = () => toast("Public key copied.");
  if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(done, () => toast("Select the key and copy it."));
  else toast("Select the key and copy it.");
}

function sshDownloadPrivate(k) {
  if (!confirm(`Download the private key ${k.name}?\n\nAnyone with this file can sign in to every device where the key is installed${k.encrypted ? "" : " (it has no passphrase)"}. Keep it somewhere safe.`)) return;
  const a = el("a", { href: `/api/ssh/keys/${encodeURIComponent(k.name)}/private`, download: k.name });
  document.body.append(a);
  a.click();
  a.remove();
}

async function sshSetDefault(name) {
  try {
    await api("/api/ssh/default", { method: "PUT", body: JSON.stringify({ name }) });
    toast(name ? `${name} is now the default key.` : "No default key.");
  } catch (ex) { toast(ex.message, 6000); }
  state.sshKeysCache = null;
  loadSshKeys();
}

// ---- new key / import
function sshNewDialog() {
  const st = state.ssh || {};
  const name = el("input", { name: "name", required: true, spellcheck: "false", maxlength: "48", placeholder: "home-servers", class: "mono" });
  const type = el("select", { name: "type" }, ...(st.types || [["ed25519", "Ed25519 (recommended)"]]).map(([v, l]) => el("option", { value: v }, l)));
  netDialog("New SSH key", [
    field("Name", name, "Letters, digits, . _ and -. Only for you: devices don't see it."),
    field("Type", type, "Ed25519 works with every current device. Pick RSA for very old ones."),
    field("Comment", el("input", { name: "comment", maxlength: "100", spellcheck: "false", class: "mono", placeholder: "sysadmin-ia@this-server" }),
      "Optional. Written at the end of the public key, so you can recognise it on the devices."),
    field("Description", el("input", { name: "description", maxlength: "300", placeholder: "What it's for" })),
    el("p", { class: "muted small" }, "The key has no passphrase: it's stored readable only by SysAdmin.IA's user, so SSH tabs can sign in without asking."),
  ], async (f) => {
    const k = await api("/api/ssh/keys", { method: "POST", body: JSON.stringify({
      name: f.name.value.trim(), type: f.type.value, comment: f.comment.value.trim(), description: f.description.value }) });
    toast(`Key ${k.name} created.`);
    state.sshKeysCache = null;
    loadSshKeys();
  }, "Create");
  setTimeout(() => name.focus(), 50);
}

function sshImportDialog() {
  const name = el("input", { name: "name", required: true, spellcheck: "false", maxlength: "48", placeholder: "laptop-key", class: "mono" });
  const priv = el("textarea", { name: "private_key", rows: "6", required: true, spellcheck: "false", class: "mono small",
    placeholder: "-----BEGIN OPENSSH PRIVATE KEY-----\n…\n-----END OPENSSH PRIVATE KEY-----" });
  const file = el("input", { type: "file", class: "small", "aria-label": "Private key file" });
  file.addEventListener("change", async () => {
    const f = file.files && file.files[0];
    if (!f) return;
    if (f.size > 20000) { toast("That file is too big to be an SSH key."); return; }
    priv.value = await f.text();
    if (!name.value.trim()) name.value = f.name.replace(/\.[^.]*$/, "").replace(/[^A-Za-z0-9_.-]/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 48);
  });
  netDialog("Import an SSH key", [
    field("Name", name),
    field("Private key", priv, "Paste it, or open the file (e.g. id_ed25519). It's stored readable only by SysAdmin.IA's user."),
    file,
    field("Public key", el("textarea", { name: "public_key", rows: "2", spellcheck: "false", class: "mono small", placeholder: "ssh-ed25519 AAAA… you@laptop" }),
      "Only needed if the key has a passphrase (then ssh asks for the passphrase in the Console)."),
    field("Description", el("input", { name: "description", maxlength: "300", placeholder: "What it's for" })),
  ], async (f) => {
    const k = await api("/api/ssh/keys/import", { method: "POST", body: JSON.stringify({
      name: f.name.value.trim(), private_key: f.private_key.value, public_key: f.public_key.value, description: f.description.value }) });
    toast(`Key ${k.name} imported.`);
    state.sshKeysCache = null;
    loadSshKeys();
  }, "Import");
  setTimeout(() => name.focus(), 50);
}

function sshEditDialog(k) {
  netDialog(`Edit ${k.name}`, [
    field("Name", el("input", { name: "new_name", value: k.name, required: true, spellcheck: "false", maxlength: "48", class: "mono" })),
    field("Description", el("input", { name: "description", maxlength: "300", value: k.description || "", placeholder: "What it's for" })),
  ], async (f) => {
    await api(`/api/ssh/keys/${encodeURIComponent(k.name)}`, { method: "PUT", body: JSON.stringify({
      new_name: f.new_name.value.trim(), description: f.description.value }) });
    toast("Saved.");
    state.sshKeysCache = null;
    loadSshKeys();
  });
}

async function sshDelete(k) {
  if (!confirm(`Delete the SSH key ${k.name}?\n\nThe server can't sign in with it any more. Devices keep it in their authorized_keys until you remove it there; download the private key first if you still need it.`)) return;
  try {
    await api(`/api/ssh/keys/${encodeURIComponent(k.name)}`, { method: "DELETE" });
    toast("Key deleted.");
  } catch (ex) { toast(ex.message, 6000); }
  state.sshKeysCache = null;
  loadSshKeys();
}

// ---- devices: pick one (known network devices) or type an address
async function sshDevicePicker() {
  const host = el("input", { name: "host", required: true, spellcheck: "false", class: "mono", placeholder: "192.168.1.20 or nas.local", list: "ssh-hosts-list" });
  if (!$("#ssh-hosts-list")) document.body.append(el("datalist", { id: "ssh-hosts-list" }));
  try {
    const devs = await api("/api/network/devices");
    const withSsh = devs.filter((d) => d.ip && ((d.services || []).some((s) => s.service === "ssh") || (d.ports || []).includes(22)));
    const rest = devs.filter((d) => d.ip && !withSsh.includes(d));
    $("#ssh-hosts-list").innerHTML = "";
    for (const d of [...withSsh, ...rest]) $("#ssh-hosts-list").append(el("option", { value: d.ip }, `${d.name || d.vendor || d.mac}${withSsh.includes(d) ? " · SSH" : ""}`));
  } catch { /* the network module may be unused: typing works */ }
  return host;
}

function sshTarget(f) {
  const host = f.host.value.trim(), user = f.user.value.trim(), port = Number(f.port.value || 22);
  if (!host) throw new Error("Enter the device's address.");
  if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/.test(user)) throw new Error("Invalid user name.");
  if (!(port >= 1 && port <= 65535)) throw new Error("Invalid port.");
  try { localStorage.setItem("sai.ssh.last", user); } catch { /* private mode */ }
  return { host, user, port };
}

function lastSshUser() {
  try { return localStorage.getItem("sai.ssh.last") || "root"; } catch { return "root"; }
}

async function sshInstallDialog(k) {
  const host = await sshDevicePicker();
  netDialog(`Install ${k.name} on a device`, [
    field("Device", host, "Its address or name. The device needs an SSH server."),
    el("div", { class: "row ssh-target-row" },
      field("User name on the device", el("input", { name: "user", required: true, value: lastSshUser(), spellcheck: "false", class: "mono", autocomplete: "off" })),
      field("Port", el("input", { name: "port", type: "number", min: "1", max: "65535", value: "22" }))),
    el("p", { class: "muted small" }, "A Console tab opens and signs in with the device's password (you type it there, once). The public key is then added to ~/.ssh/authorized_keys of that user on the device."),
  ], async (f) => {
    const t = sshTarget(f);
    openSshConsole(t.host, t.port, t.user, `${t.host} (install ${k.name})`, "", k.name);
  }, "Open the Console");
  setTimeout(() => host.focus(), 50);
}

async function sshConnectDialog(keyName) {
  const host = await sshDevicePicker();
  netDialog("Connect over SSH", [
    field("Device", host),
    el("div", { class: "row ssh-target-row" },
      field("User name on the device", el("input", { name: "user", required: true, value: lastSshUser(), spellcheck: "false", class: "mono", autocomplete: "off" })),
      field("Port", el("input", { name: "port", type: "number", min: "1", max: "65535", value: "22" }))),
    el("p", { class: "muted small" }, `Signs in with the key ${keyName}. If the key isn't installed on the device yet, ssh asks for the password instead.`),
  ], async (f) => {
    const t = sshTarget(f);
    openSshConsole(t.host, t.port, t.user, t.host, keyName);
  }, "Connect");
  setTimeout(() => host.focus(), 50);
}

// ---- known hosts
function renderKnownHosts(st) {
  const box = $("#ssh-hosts");
  box.innerHTML = "";
  const hosts = st.known_hosts || [];
  box.append(
    el("div", { class: "ai-card-head" },
      el("span", { class: "icon-tile", html: icon("shield") }),
      el("div", { class: "ai-card-title" },
        el("b", {}, "Known devices"),
        el("div", { class: "muted small" }, "Devices this server has connected to over SSH, with the key each one showed the first time. If a device is reinstalled its key changes and ssh refuses to connect: remove it here, then connect again."))),
    hosts.length ? el("table", { class: "pyenv-ver-table small" },
      el("thead", {}, el("tr", {}, el("th", {}, "Device"), el("th", {}, "Key"), el("th", {}, ""))),
      el("tbody", {}, ...hosts.map((h) => el("tr", {},
        el("td", { class: "mono", "data-no-i18n": h.hashed ? null : "" }, h.hashed ? "(name hidden: hashed entry)" : h.hosts),
        el("td", { class: "mono muted", "data-no-i18n": "" }, `${h.type.toUpperCase()} ${h.fingerprint}`),
        el("td", { class: "pyenv-ver-actions" }, h.hashed ? null : el("button", { type: "button", class: "small ghost danger",
          onclick: () => sshForget(h.hosts.split(",")[0]) }, withIcon("trash", "Remove")))))))
      : el("p", { class: "muted small" }, "No device yet."),
    el("form", { class: "pyenv-dl", onsubmit: (ev) => { ev.preventDefault(); const v = ev.target.host.value.trim(); if (v) sshForget(v); } },
      el("input", { name: "host", class: "mono", spellcheck: "false", placeholder: "192.168.1.20 or [nas.local]:2222", "aria-label": "Device to forget" }),
      el("button", { type: "submit", class: "small ghost" }, withIcon("x", "Forget this device"))),
    el("p", { class: "muted small" }, el("span", {}, "Stored in "), el("code", { "data-no-i18n": "" }, st.known_hosts_file || "~/.ssh/known_hosts")));
}

async function sshForget(host) {
  if (!confirm(`Forget ${host}?\n\nThe next connection accepts the key the device shows then.`)) return;
  try {
    const r = await api("/api/ssh/known-hosts/remove", { method: "POST", body: JSON.stringify({ host }) });
    toast(r.removed ? `${host} forgotten.` : `${host} wasn't in the list.`);
  } catch (ex) { toast(ex.message, 6000); }
  loadSshKeys();
}

// Keys for the SSH dialog in Network → Devices (cached for a minute)
async function sshKeyChoices() {
  const c = state.sshKeysCache;
  if (c && Date.now() - c.at < 60000) return c.data;
  try {
    const st = await api("/api/ssh");
    state.sshKeysCache = { at: Date.now(), data: { keys: st.keys.map((k) => k.name), default: st.default } };
  } catch { state.sshKeysCache = { at: Date.now(), data: { keys: [], default: "" } }; }
  return state.sshKeysCache.data;
}

$("#ssh-add").addEventListener("click", sshNewDialog);
$("#ssh-import").addEventListener("click", sshImportDialog);
window.loadSshKeys = loadSshKeys;
window.sshKeyChoices = sshKeyChoices;
