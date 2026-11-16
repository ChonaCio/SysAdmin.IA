// Network module: devices on the LAN, router (DHCP, port forwarding), publishing services on the internet.
// Uses helpers from app.js / containers.js: $, $$, el, api, toast, icon, withIcon, iconEl, state, fmtDate.
"use strict";

state.net = { tab: "devices", filter: "", kind: "", onlyNew: false };
const NET_ICON = { router: "router", this_server: "server", computer: "computer", phone: "phone", tv: "tv", media: "media",
  speaker: "speaker", printer: "printer", nas: "nas", camera: "camera", iot: "iot", home: "home", console: "console",
  server: "server", network: "network", unknown: "question" };

function netAgo(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

async function loadNetwork() {
  clearTimeout(state.net.timer);
  let o;
  try { o = await api("/api/network"); } catch (ex) { toast(ex.message); return; }
  state.net.overview = o;
  renderNetHead(o);
  if (!state.net.loadedOnce) { state.net.loadedOnce = true; showNetTab(state.net.tab); }
  if (o.progress.state === "running") pollNetScan();
}

function renderNetHead(o) {
  $("#net-tile").innerHTML = icon("network");
  const i = o.interfaces[0];
  const devs = state.net.devices || [];
  $("#net-title").textContent = i ? i.network : "This network";
  const pill = $("#net-pill");
  const running = o.progress.state === "running";
  pill.textContent = running ? "scanning…" : o.last_scan ? `scanned ${netAgo(o.last_scan)}` : "not scanned yet";
  pill.className = `status-pill ${running ? "warn" : o.last_scan ? "ok" : ""}`;
  const bits = [];
  if (i) bits.push(`${i.iface} · this server ${i.ip}`);
  if (o.gateway) bits.push(`router ${o.gateway}`);
  if (devs.length) bits.push(`${devs.filter((d) => d.online).length} of ${devs.length} devices online`);
  $("#net-sub").textContent = !o.linux ? "Network discovery works when the app runs on Linux." : bits.join(" · ");
  const acts = $("#net-actions");
  acts.innerHTML = "";
  acts.append(el("button", { type: "button", class: "primary", disabled: running || !o.linux, onclick: () => startNetScan(true) },
    withIcon("refresh", "Scan now")));
}

async function startNetScan(deep) {
  try { await api("/api/network/scan", { method: "POST", body: JSON.stringify({ deep }) }); } catch (ex) { toast(ex.message); return; }
  pollNetScan();
}

async function pollNetScan() {
  clearTimeout(state.net.timer);
  let p;
  try { p = await api("/api/network/scan"); } catch { return; }
  const box = $("#net-progress");
  box.hidden = p.state !== "running" && p.state !== "failed";
  box.innerHTML = p.state === "running" ? '<span class="spinner" aria-hidden="true"></span> ' : "";
  box.append(p.state === "failed" ? (p.error || "The scan failed.") : `${p.stage || "Scanning"}…`);
  box.className = `small ${p.state === "failed" ? "error" : "busy"}`;
  if (p.state === "running") {
    if (state.net.overview) renderNetHead({ ...state.net.overview, progress: p });
    if (state.view === "network") state.net.timer = setTimeout(pollNetScan, 1500);
    return;
  }
  if (p.state === "done") {
    toast(p.new ? `Scan finished: ${p.found} devices, ${p.new} new.` : `Scan finished: ${p.found} devices.`);
    await loadNetDevices();
    loadNetwork();
  }
}

$$("#net-tabs .seg-btn").forEach((b) => b.addEventListener("click", () => showNetTab(b.dataset.tab)));
function showNetTab(tab) {
  state.net.tab = tab;
  $$("#net-tabs .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  const pane = $("#net-pane");
  const sn = $("#server-name-card");
  if (sn && pane.contains(sn)) $("#net-names-holder").append(sn);   // keep the Server name card (and its handlers)
  pane.innerHTML = '<p class="muted small"><span class="spinner" aria-hidden="true"></span> Loading…</p>';
  ({ devices: netDevicesTab, names: netNamesTab, ports: netPortsTab, router: netRouterTab, publish: netPublishTab, settings: netSettingsTab })[tab](pane);
}

// ------------------------------------------------------------ Names & websites
const NAME_STATE = { ok: ["announced", "tag-ok"], pending: ["starting…", ""], error: ["not announced", "tag-warn"] };

async function netNamesTab(pane) {
  let names, sites;
  if (!state.net.devices) state.net.devices = await api("/api/network/devices").catch(() => []);
  try { [names, sites] = await Promise.all([api("/api/network/names"), api("/api/network/sites")]); }
  catch (ex) { pane.innerHTML = ""; pane.append(el("p", { class: "small error" }, ex.message)); return; }
  if (state.net.tab !== "names") return;
  state.net.names = names;
  state.net.sites = sites;
  pane.innerHTML = "";
  pane.append($("#server-name-card"));
  loadServerName().catch((e) => setSnMsg(e.message, false));
  pane.append(netNamesCard(names), netSitesCard(sites, names));
}

function netNamesCard(n) {
  const box = el("div", { class: "net-card" },
    el("div", { class: "net-card-head" }, el("h3", {}, "Names on the network"),
      el("button", { type: "button", class: "small", onclick: () => netNameDialog() }, withIcon("plus", "Add a name"))),
    el("p", { class: "muted small" }, "Give devices (or this server) a name instead of an address. Names ending in .local are announced by this server and work on any network. Other names, such as nas.home.lan, are kept in the router's DNS" +
      (n.router_dns ? "." : " (needs an OpenWrt or MikroTik router, set up in Router & DHCP).")));
  if (n.router_error) box.append(el("p", { class: "small error" }, n.router_error));
  if (!n.avahi) box.append(el("p", { class: "note warn small" }, "Publishing .local names needs avahi-publish on the server (package avahi-utils; installing the next update sets it up)."));
  const rows = [el("tr", { class: "lan-self" },
    el("td", {}, el("strong", { class: "mono", "data-no-i18n": "" }, n.server.mdns_name)),
    el("td", { class: "small" }, el("span", { class: "mono" }, n.server.ip || "–"), el("div", { class: "muted small" }, "This server")),
    el("td", {}, ".local"),
    el("td", {}, el("span", { class: `tag ${n.server.mdns === "active" ? "tag-ok" : "tag-warn"}` }, n.server.mdns === "active" ? "announced" : "not announced")),
    el("td", { class: "right muted small" }, "Server name"))];
  for (const x of n.names) {
    const [label, cls] = x.where === "router" ? ["in the router", "tag-ok"] : (NAME_STATE[x.state] || NAME_STATE.pending);
    rows.push(el("tr", {},
      el("td", {}, el("span", { class: "mono", "data-no-i18n": "" }, x.name),
        x.auto ? el("div", { class: "muted small" }, "added with a website") : null),
      el("td", { class: "small" }, el("span", { class: "mono" }, x.ip), x.device ? el("div", { class: "muted small", "data-no-i18n": x.device === "This server" ? null : "" }, x.device) : null),
      el("td", {}, x.where === "router" ? "Router DNS" : ".local"),
      el("td", {}, el("span", { class: `tag ${cls}`, title: x.error || "" }, label), x.error ? el("div", { class: "small error" }, x.error) : null),
      el("td", { class: "right" }, el("button", { type: "button", class: "small ghost danger icon-only", "aria-label": "Remove this name", title: "Remove this name",
        onclick: async () => {
          if (!confirm(`Remove the name ${x.name}?`)) return;
          try { await api("/api/network/names/remove", { method: "POST", body: JSON.stringify({ id: x.id }) }); toast("Name removed."); }
          catch (ex) { toast(ex.message, 6000); }
          showNetTab("names");
        } }, iconEl("trash")))));
  }
  box.append(el("div", { class: "table-wrap" }, el("table", { class: "table fw-table" },
    el("thead", {}, el("tr", {}, ...["Name", "Address", "Kind", "Status", ""].map((h) => el("th", {}, h)))),
    el("tbody", {}, ...rows))));
  return box;
}

function netNameDialog() {
  const n = state.net.names || {};
  const devs = (state.net.devices || []).filter((d) => d.ip);
  const list = el("datalist", { id: "lan-name-ips" }, n.server_ip ? el("option", { value: n.server_ip }, "This server") : null,
    ...devs.filter((d) => d.ip !== n.server_ip).map((d) => el("option", { value: d.ip }, d.name || d.vendor || d.mac)));
  const f = {
    name: el("input", { required: true, class: "mono", placeholder: n.router_dns ? "e.g. nas.local or nas.home.lan" : "e.g. nas.local", spellcheck: "false", autocomplete: "off" }),
    ip: el("input", { required: true, class: "mono", list: "lan-name-ips", placeholder: "192.168.1.20", spellcheck: "false", autocomplete: "off" }),
  };
  netDialog("Add a name", [list, field("Name", f.name, n.router_dns ? "Ends in .local (announced by this server) or any other domain (kept in the router)." : "One word plus .local."),
    field("Device (address)", f.ip, "Pick a device or this server. Give the device a reserved address (Router & DHCP) so the name keeps working.")],
  async () => {
    await api("/api/network/names", { method: "POST", body: JSON.stringify({ name: f.name.value, ip: f.ip.value }) });
    toast("Name added.");
    showNetTab("names");
  }, "Add");
}

function netSitesCard(s, n) {
  const box = el("div", { class: "net-card" },
    el("div", { class: "net-card-head" }, el("h3", {}, "Websites on this server"),
      el("button", { type: "button", class: "small", disabled: !s.docker, onclick: () => netSiteDialog() }, withIcon("plus", "Add a website"))),
    el("p", { class: "muted small" }, "Serve several websites and apps on the same port (80, and 443 for HTTPS) of this server: each one gets its own name, and a small proxy (Caddy, in a container) sends every visit to the right app. Many apps can then share port 80 instead of each needing its own port."));
  if (!s.docker) { box.append(el("p", { class: "note warn small" }, "This needs Docker: install it in the Containers module first.")); return box; }
  if (s.sites.length) {
    box.append(el("p", { class: "small" }, "Proxy: ", el("span", { class: `tag ${s.proxy === "running" ? "tag-ok" : "tag-warn"}` }, s.proxy === "running" ? "running" : s.proxy || "not running"),
      s.public_proxy ? el("span", { class: "muted" }, " · also serves the sites published on the internet") : null));
  } else box.append(el("p", { class: "muted small" }, "No websites yet."));
  const tgtLabel = (t) => (t === s.app_target ? "SysAdmin.IA (this app)" : t);
  const rows = s.sites.map((x) => {
    const url = `${x.https ? "https" : "http"}://${x.hostname}/`;
    return el("tr", {},
      el("td", {}, el("a", { href: url, target: "_blank", rel: "noopener noreferrer", class: "mono", "data-no-i18n": "" }, x.hostname)),
      el("td", { class: "mono small" }, tgtLabel(x.target)),
      el("td", {}, x.https ? el("span", { class: "tag tag-ok" }, "HTTPS") : el("span", { class: "tag" }, "HTTP")),
      el("td", { class: "right" }, el("button", { type: "button", class: "small ghost danger icon-only", "aria-label": "Remove this website", title: "Remove this website",
        onclick: async () => {
          if (!confirm(`Remove the website ${x.hostname}? The app behind it keeps running; it's just no longer reachable by this name.`)) return;
          try { await api(`/api/network/sites/${x.id}`, { method: "DELETE" }); toast("Website removed."); }
          catch (ex) { toast(ex.message, 6000); }
          showNetTab("names");
        } }, iconEl("trash"))));
  });
  if (rows.length) {
    box.append(el("div", { class: "table-wrap" }, el("table", { class: "table fw-table" },
      el("thead", {}, el("tr", {}, ...["Website", "Goes to", "", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...rows))));
  }
  if (s.sites.some((x) => x.https)) {
    box.append(el("p", { class: "muted small" }, "HTTPS websites use certificates from this server's own certificate authority. Browsers warn until you trust it: ",
      s.ca ? el("a", { href: "/api/network/sites/ca.crt", download: "sysadmin-ia-local-ca.crt" }, "download the certificate") : el("span", {}, "it's created when the proxy starts"),
      " and add it to each device's trusted certificates."));
  }
  if (!n.server.ip) box.append(el("p", { class: "small error" }, "This server's LAN address isn't known."));
  return box;
}

function netSiteDialog() {
  const s = state.net.sites || { targets: [] };
  const n = state.net.names || {};
  const list = el("datalist", { id: "lan-site-targets" }, ...s.targets.map((t) => el("option", { value: t.target }, t.label)));
  const f = {
    host: el("input", { required: true, class: "mono", placeholder: n.router_dns ? "e.g. photos.local or photos.home.lan" : "e.g. photos.local", spellcheck: "false", autocomplete: "off" }),
    target: el("input", { required: true, class: "mono", list: "lan-site-targets", placeholder: "127.0.0.1:2283", spellcheck: "false", autocomplete: "off" }),
    https: el("input", { type: "checkbox" }),
    name: el("input", { type: "checkbox", checked: true }),
  };
  const appNote = el("p", { class: "muted small", hidden: true }, "SysAdmin.IA itself: it's served over HTTPS when the app uses HTTPS, so signing in keeps working.");
  f.target.addEventListener("input", () => {
    appNote.hidden = f.target.value.trim() !== "app";
    if (f.target.value.trim() === "app" && s.app_target.startsWith("https")) f.https.checked = true;
  });
  netDialog("Add a website", [list,
    field("Name", f.host, "The address people type, e.g. photos.local."),
    field("Goes to", f.target, "Pick an app (a container, a device's web page, or “app” for SysAdmin.IA itself) or type an address and port."),
    appNote,
    el("label", { class: "check" }, f.https, " HTTPS (with this server's own certificate; browsers warn until it's trusted)"),
    el("label", { class: "check" }, f.name, ` Also create the name, pointing to this server (${n.server_ip || "?"})`)],
  async () => {
    const r = await api("/api/network/sites", { method: "POST", body: JSON.stringify({ hostname: f.host.value, target: f.target.value,
      https: f.https.checked, make_name: f.name.checked }) });
    toast(r.note || "Website added.", r.note ? 8000 : 3500);
    showNetTab("names");
  }, "Add");
}

// ------------------------------------------------------------ Ports (this server's firewall)
const FW_STATE = {
  open: ["Open", "tag-ok", "Reachable from the network."],
  limited: ["Some addresses", "tag-warn", "Reachable only from the addresses a rule allows."],
  closed: ["Closed", "", "The firewall blocks it: other devices can't reach it."],
  docker: ["Docker", "tag-warn", "Docker publishes this port itself: it's reachable from the network even when closed here."],
  local: ["This server only", "", "Listens only on this server's own address (localhost); the network can't reach it."],
};

function fwProtoText(ports) {
  const set = [...new Set(ports.map((p) => p[2]))];
  return set.includes("any") || (set.includes("tcp") && set.includes("udp")) ? "TCP/UDP" : set.map((p) => p.toUpperCase()).join("/");
}
function fwPortText(ports) {
  return [...new Set(ports.map(([lo, hi]) => (lo === hi ? `${lo}` : `${lo}–${hi}`)))].join(", ") || "–";
}

async function netFwCall(path, body, done) {
  let ok = true;
  try {
    await api(`/api/network/firewall/${path}`, { method: "POST", body: JSON.stringify(body) });
    if (done) toast(done);
  } catch (ex) { toast(ex.message, 6000); ok = false; }
  if (state.net.tab === "ports") showNetTab("ports");
  return ok;
}

async function netPortsTab(pane) {
  let st;
  try { st = await api("/api/network/firewall"); } catch (ex) { pane.innerHTML = ""; pane.append(el("p", { class: "small error" }, ex.message)); return; }
  state.net.fw = st;
  pane.innerHTML = "";
  if (!st.linux) { pane.append(el("p", { class: "muted" }, "The firewall can only be managed when SysAdmin.IA runs on Linux.")); return; }
  if (st.setup_command) {
    pane.append(el("div", { class: "net-card" }, el("h3", {}, "One-time setup needed"),
      el("p", { class: "muted small" }, "Managing the firewall needs a newer version of the app's root helper. On the server, run:"),
      el("pre", { class: "mono small", "data-no-i18n": "" }, st.setup_command)));
    return;
  }
  if (st.error) { pane.append(el("p", { class: "small error" }, st.error)); return; }
  if (st.backend === "none") {
    pane.append(el("div", { class: "net-card" }, el("h3", {}, "No firewall tool installed"),
      el("p", { class: "muted small" }, "Install ufw (sudo apt install ufw) or nftables on the server, then reload this page.")));
    return;
  }
  pane.append(netFwSummary(st), netFwRules(st), netFwListening(st));
}

function netFwSummary(st) {
  const keep = [`SysAdmin.IA (${st.app_ports.join(", ")})`, `SSH (${(st.ssh_ports.length ? st.ssh_ports : [22]).join(", ")})`, "local names (mDNS 5353/udp)"];
  const box = el("div", { class: "net-card" },
    el("div", { class: "net-card-head" },
      el("h3", {}, iconEl("shield"), "Firewall of this server", " ",
        el("span", { class: `status-pill ${st.active ? "ok" : "warn"}` }, st.active ? "on" : "off")),
      st.active ? el("button", { type: "button", class: "small ghost danger", onclick: () => {
        if (!confirm("Turn the firewall off? Every port a program listens on becomes reachable from the network.")) return;
        netFwCall("enabled", { value: false }, "The firewall is off.");
      } }, withIcon("stop", "Turn off")) : null),
    el("p", { class: "muted small" }, `Controls which ports of this server other devices can connect to. Uses ${st.backend_label}${st.zone ? ` (zone ${st.zone})` : ""}.`));
  if (!st.active) {
    box.append(el("p", { class: "note warn small" }, "The firewall is off: every port a program listens on can be reached from the network."),
      el("div", {}, el("button", { type: "button", onclick: () => {
        if (!confirm(`Turn the firewall on with ports closed by default? These stay open: ${keep.join(", ")}, plus the ports in the table below.`)) return;
        (async () => {
          if (!(await netFwCall("enabled", { value: true }))) return;
          if (!st.default_closed && !(await netFwCall("default", { value: true }))) return;
          toast("The firewall is on.");
        })();
      } }, withIcon("shield", "Turn on (ports closed by default)"))));
    return box;
  }
  if (st.default_closed) {
    box.append(el("div", { class: "net-card-head" },
      el("p", { class: "small" }, el("span", { class: "tag tag-ok" }, "Closed by default"), " ", "Only the ports in the table below can be reached."),
      el("button", { type: "button", class: "small ghost", onclick: () => {
        if (!confirm("Open all ports by default? Every program listening on the network becomes reachable, unless a rule blocks its port.")) return;
        netFwCall("default", { value: false }, "All ports are open by default.");
      } }, "Open all ports by default")));
  } else {
    box.append(el("p", { class: "note warn small" }, "All ports are open by default: anything listening on the network can be reached."),
      el("div", {}, el("button", { type: "button", onclick: () => {
        if (!confirm(`Close ports by default? These stay open: ${keep.join(", ")}, plus the ports in the table below.`)) return;
        netFwCall("default", { value: true }, "Ports are now closed by default.");
      } }, withIcon("shield", "Close ports by default (recommended)"))));
  }
  return box;
}

function netFwRules(st) {
  const box = el("div", { class: "net-card" }, el("div", { class: "net-card-head" }, el("h3", {}, "Open ports"),
    el("button", { type: "button", class: "small", onclick: () => netFwOpenDialog() }, withIcon("plus", "Open port"))));
  if (!st.active) box.append(el("p", { class: "muted small" }, "These rules take effect when the firewall is on."));
  if (!st.rules.length) { box.append(el("p", { class: "muted small" }, "No ports are open.")); return box; }
  const rows = st.rules.map((r) => {
    const users = [...new Set(r.in_use.flatMap((u) => u.used_by))];
    const status = r.action === "deny" ? el("span", { class: "tag tag-warn" }, "Blocked")
      : r.in_use.length ? el("span", { class: "tag tag-ok", title: r.in_use.map((u) => `${u.port}/${u.proto}`).join(", ") }, "In use")
      : el("span", { class: "muted" }, "Not in use");
    const del = el("button", { type: "button", class: "small ghost danger icon-only", "aria-label": "Close this port",
      title: r.protected || (!r.removable ? "Made outside SysAdmin.IA; change it with the firewall's own tools." : "Close this port (delete the rule)"),
      disabled: !!r.protected || !r.removable, onclick: () => {
        let msg = r.app ? `Close ${r.app} (${fwPortText(r.ports)})? Programs on it can no longer be reached from the network.`
          : `Close the port ${fwPortText(r.ports)}? Programs on it can no longer be reached from the network.`;
        if (r.ssh) msg += "\n\nThis is the SSH port: you could lose SSH access to this server (the Console here keeps working).";
        if (st.active && !st.default_closed) msg += "\n\nPorts are open by default, so it stays reachable until you close ports by default.";
        if (!confirm(msg)) return;
        netFwCall("remove", { id: r.id }, "Port closed.");
      } }, iconEl("trash"));
    return el("tr", {},
      el("td", {}, r.app ? el("span", {}, el("strong", { "data-no-i18n": "" }, r.app), " ", el("span", { class: "muted mono small" }, fwPortText(r.ports))) : el("span", { class: "mono" }, fwPortText(r.ports)),
        r.limit ? el("span", { class: "tag", title: "Repeated connection attempts from one address are refused for a while (ufw limit)." }, "rate-limited") : null,
        r.source !== "Anywhere" ? el("div", { class: "muted small" }, "only from ", el("span", { class: "mono" }, r.source)) : null,
        r.note ? el("div", { class: "muted small fw-note", "data-no-i18n": "" }, r.note) : null),
      el("td", {}, fwProtoText(r.ports)),
      el("td", {}, status),
      el("td", { class: "small" }, users.length ? users.join(", ") : "–"),
      el("td", { class: "right" }, del));
  });
  box.append(el("div", { class: "table-wrap" }, el("table", { class: "table fw-table" },
    el("thead", {}, el("tr", {}, ...["Port", "Protocol", "Status", "Used by", ""].map((h) => el("th", {}, h)))),
    el("tbody", {}, ...rows))));
  return box;
}

function netFwListening(st) {
  const reach = st.listening.filter((e) => e.state !== "local");
  const local = st.listening.filter((e) => e.state === "local");
  const box = el("div", { class: "net-card" }, el("h3", {}, "Ports in use on this server"),
    el("p", { class: "muted small" }, "Programs waiting for connections, and whether other devices can reach them."));
  if (!reach.length) box.append(el("p", { class: "muted small" }, "No program listens on the network."));
  else {
    box.append(el("div", { class: "table-wrap" }, el("table", { class: "table fw-table" },
      el("thead", {}, el("tr", {}, ...["Port", "Protocol", "Used by", "Reachable", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...reach.map((e) => {
        const [label, cls, why] = FW_STATE[e.state] || FW_STATE.open;
        return el("tr", {},
          el("td", { class: "mono" }, String(e.port)),
          el("td", {}, e.proto.toUpperCase()),
          el("td", { class: "small" }, e.used_by.length ? e.used_by.join(", ") : "–",
            e.addresses.some((a) => !["*", "0.0.0.0", "::"].includes(a)) ? el("span", { class: "muted mono small" }, ` (${e.addresses.join(", ")})`) : null),
          el("td", {}, el("span", { class: `tag ${cls}`, title: why }, label)),
          el("td", { class: "right" }, e.state === "closed" || e.state === "limited"
            ? el("button", { type: "button", class: "small ghost", onclick: () => netFwOpenDialog(e) }, "Open port") : null));
      })))));
  }
  if (local.length) {
    box.append(el("details", { class: "small" }, el("summary", { class: "muted" }, `Not reachable from other devices (${local.length})`),
      el("p", { class: "muted small" }, "Programs listening only on this server's own address, and DHCP clients (they only receive replies to this server's requests)."),
      el("p", { class: "muted small" }, local.map((e) => `${e.port}/${e.proto} ${e.used_by.length ? `(${e.used_by.join(", ")})` : ""}`.trim()).join(" · "))));
  }
  return box;
}

function netFwOpenDialog(e = null) {
  const st = state.net.fw || { listening: [] };
  const closed = st.listening.filter((x) => x.state === "closed" || x.state === "limited");
  const list = el("datalist", { id: "fw-closed-ports" }, ...closed.map((x) => el("option", { value: String(x.port) }, x.used_by.join(", ") || x.proto)));
  const f = {
    port: el("input", { required: true, class: "mono", list: "fw-closed-ports", placeholder: "e.g. 8080 or 8000-8010", value: e ? String(e.port) : "",
      autocomplete: "off", spellcheck: "false" }),
    proto: el("select", {}, el("option", { value: "tcp" }, "TCP"), el("option", { value: "udp" }, "UDP"), el("option", { value: "both" }, "TCP and UDP")),
    note: el("input", { maxlength: "80", placeholder: "e.g. Minecraft server", value: e && e.used_by.length ? e.used_by.join(", ") : "" }),
  };
  if (e) f.proto.value = e.proto;
  f.port.addEventListener("change", () => {
    const hit = closed.find((x) => String(x.port) === f.port.value.trim());
    if (hit) { f.proto.value = hit.proto; if (!f.note.value) f.note.value = hit.used_by.join(", "); }
  });
  netDialog("Open a port", [list, field("Port", f.port, "A single port or a range."), field("Protocol", f.proto), field("Note", f.note, "(optional)"),
    el("p", { class: "note warn small" }, "Every device that can reach this server can then connect to that port (and the internet too, if the router forwards it).")],
  async () => {
    await api("/api/network/firewall/open", { method: "POST", body: JSON.stringify({ port: f.port.value, proto: f.proto.value, note: f.note.value }) });
    toast("Port opened.");
    showNetTab("ports");
  }, "Open port");
}

// ------------------------------------------------------------ shared dialog
function netDialog(title, body, onOk, okLabel = "Save") {
  const dlg = $("#net-dialog");
  $("#net-dialog-title").textContent = title;
  const box = $("#net-dialog-body");
  box.innerHTML = "";
  box.append(...[].concat(body));
  $("#net-ok").textContent = okLabel;
  $("#net-dialog-error").hidden = true;
  state.net.onOk = onOk;
  dlg.showModal();
}
$("#net-cancel").addEventListener("click", () => $("#net-dialog").close());
$("#net-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#net-ok");
  btn.disabled = true;
  try {
    await state.net.onOk(e.target);
    $("#net-dialog").close();
  } catch (ex) {
    const m = $("#net-dialog-error");
    m.textContent = ex.message;
    m.hidden = false;
  } finally { btn.disabled = false; }
});
const field = (label, input, hint) => el("label", {}, label, input, hint ? el("span", { class: "muted small" }, hint) : null);

// ------------------------------------------------------------ devices
async function loadNetDevices() {
  try { state.net.devices = await api("/api/network/devices"); } catch (ex) { toast(ex.message); state.net.devices = []; }
  return state.net.devices;
}

async function netDevicesTab(pane) {
  const [devs, router] = await Promise.all([loadNetDevices(), api("/api/network/router").catch(() => ({}))]);
  state.net.router = router;
  state.net.blocked = new Set();
  if ((router.caps || []).includes("block")) {
    try { (await api("/api/network/router/blocked")).forEach((b) => state.net.blocked.add(b.mac)); } catch { /* router unreachable */ }
  }
  if (state.net.tab !== "devices") return;
  pane.innerHTML = "";
  if (state.net.overview) renderNetHead(state.net.overview);
  const kinds = state.net.overview?.kinds || {};
  const list = el("div", { class: "net-list" });
  const draw = () => {
    list.innerHTML = "";
    const q = state.net.filter.toLowerCase();
    const shown = devs.filter((d) => (!state.net.kind || d.kind === state.net.kind) && (!state.net.onlyNew || !d.known)
      && (!q || [d.name, d.ip, d.mac, d.vendor, d.hostname, d.kind_label].join(" ").toLowerCase().includes(q)));
    if (!devs.length) {
      list.append(el("div", { class: "empty-card" }, el("strong", {}, "No devices yet."),
        el("p", { class: "muted small" }, "Click Scan now: every device that is switched on is found and identified (it takes about a minute).")));
      return;
    }
    if (!shown.length) { list.append(el("p", { class: "muted small" }, "No devices match.")); return; }
    for (const d of shown) list.append(netDeviceRow(d, kinds));
  };
  const kindSel = el("select", { "aria-label": "Kind", onchange: (e) => { state.net.kind = e.target.value; draw(); } },
    el("option", { value: "" }, "All kinds"), ...Object.entries(kinds).map(([k, v]) => el("option", { value: k }, v)));
  kindSel.value = state.net.kind;
  const newCount = devs.filter((d) => !d.known).length;
  pane.append(el("div", { class: "net-toolbar" },
    el("input", { type: "search", placeholder: "Filter by name, address or maker…", value: state.net.filter,
      oninput: (e) => { state.net.filter = e.target.value; draw(); } }),
    kindSel,
    el("label", { class: "check small" }, el("input", { type: "checkbox", checked: state.net.onlyNew,
      onchange: (e) => { state.net.onlyNew = e.target.checked; draw(); } }), ` Only new (${newCount})`),
    newCount ? el("button", { type: "button", class: "small ghost", title: "Stop flagging these devices as new", onclick: async () => {
      await api("/api/network/devices/known-all", { method: "POST" }).catch((ex) => toast(ex.message));
      showNetTab("devices");
    } }, withIcon("check", "Mark all as known")) : null), list);
  draw();
}

// One button/tag per service found on the device: web pages open in a new tab, SSH opens a Console
// tab, anything else copies "address:port" (for database clients, file managers…).
function netServiceTags(d) {
  const svcs = d.services && d.services.length ? d.services
    : (d.port_labels || []).map(([p, label]) => ({ port: p, label, service: (d.http || {})[String(p)] ? "http" : "",
        url: (d.http || {})[String(p)] ? `http://${d.ip}:${p}/` : null, title: (d.http || {})[String(p)]?.title }));
  return svcs.map((s) => {
    const dflt = (s.service === "http" && s.port === 80) || (s.service === "https" && s.port === 443) || (s.service === "ssh" && s.port === 22);
    const portTxt = dflt || String(s.label || "").includes(`(${s.port})`) ? "" : ` :${s.port}`;
    if (s.url) {
      return el("a", { class: "tag tag-link", href: s.url, target: "_blank", rel: "noopener noreferrer",
        title: [s.title, s.server, s.url].filter(Boolean).join(" · ") },
      el("span", { "data-no-i18n": s.title && s.label === s.title ? "" : null }, s.label), portTxt);
    }
    if (s.service === "ssh" || s.ssh) {
      const ok = !!state.me?.console;
      return el("button", { type: "button", class: "tag tag-link tag-btn", disabled: !ok,
        title: ok ? `Open an SSH session in the Console${s.detail ? ` (${s.detail})` : ""}` : "The Console is turned off on this server",
        onclick: () => netSsh(d, s.port) }, iconEl("console"), " SSH", portTxt);
    }
    const addr = `${d.ip}:${s.port}`;
    return el("button", { type: "button", class: "tag tag-btn", title: `Click to copy ${addr}${s.detail ? ` · ${s.detail}` : ""}`,
      onclick: () => copyText(addr) }, s.label, portTxt);
  });
}

function netSsh(d, port) {
  const key = `sai.ssh.${d.mac}`;
  let saved = "";
  try { saved = localStorage.getItem(key) || localStorage.getItem("sai.ssh.last") || ""; } catch { /* private mode */ }
  const user = el("input", { required: true, value: saved || "root", spellcheck: "false", autocomplete: "off", class: "mono" });
  netDialog(`SSH to ${d.name || d.ip}`, [
    field("User name on the device", user, "You type the password (or key passphrase) in the Console."),
    el("p", { class: "muted small mono" }, `${d.ip}:${port}`),
  ], async () => {
    if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/.test(user.value.trim())) throw new Error("Invalid user name.");
    try { localStorage.setItem(key, user.value.trim()); localStorage.setItem("sai.ssh.last", user.value.trim()); } catch { /* private mode */ }
    openSshConsole(d.ip, port, user.value.trim(), d.name || d.ip);
  }, "Connect");
}

async function netDeviceAction(d, action) {
  const who = d.name || d.ip || d.mac;
  const msg = {
    kick: `Disconnect ${who} from the Wi-Fi now? It can reconnect after about a minute. Block it to keep it out.`,
    block: `Block ${who}? It loses internet access and can't use the router (no address, no DNS), and is kept off the Wi-Fi where the router supports it. Devices on the same switch or Wi-Fi can still reach it directly.`,
    unblock: `Unblock ${who}? It can use the network again.`,
  }[action];
  if (!confirm(msg)) return;
  try {
    await api(`/api/network/router/${action}`, { method: "POST", body: JSON.stringify({ mac: d.mac, name: d.name || "" }) });
    toast({ kick: `${who} was disconnected.`, block: `${who} is blocked.`, unblock: `${who} is unblocked.` }[action]);
  } catch (ex) { toast(ex.message); return; }
  if (state.net.tab === "devices") showNetTab("devices"); else showNetTab(state.net.tab);
}

async function netPortScan(d, btn) {
  btn.disabled = true;
  const label = btn.querySelector(".btn-in") || btn;
  try { await api(`/api/network/devices/${encodeURIComponent(d.mac)}/ports`, { method: "POST" }); }
  catch (ex) { toast(ex.message); btn.disabled = false; return; }
  const poll = async () => {
    let p;
    try { p = await api(`/api/network/devices/${encodeURIComponent(d.mac)}/ports`); } catch { p = { state: "failed" }; }
    if (p.state === "running") {
      label.textContent = `Scanning ports… ${p.pct}%`;
      setTimeout(poll, 1500);
      return;
    }
    if (p.state === "failed") toast(p.error || "The port scan failed.");
    else toast(`${p.found} open ports found on ${d.name || d.ip}.`);
    if (state.net.tab === "devices") showNetTab("devices");
  };
  poll();
}

function netDeviceRow(d, kinds) {
  const caps = state.net.router?.caps || [];
  const blocked = state.net.blocked?.has(d.mac);
  const own = ["this_server", "router"].includes(d.kind);
  const tags = netServiceTags(d);
  const title = d.name || (d.vendor && !d.vendor.startsWith("Private") ? `${d.vendor} device` : "Unknown device");
  const scanBtn = el("button", { type: "button", class: "small ghost", title: "Check all 65535 ports of this device (about a minute)",
    disabled: !d.ip, onclick: (e) => netPortScan(d, e.currentTarget) }, withIcon("refresh", "Scan all ports"));
  return el("div", { class: `net-row ${d.online ? "" : "offline"} ${blocked ? "blocked" : ""}` },
    el("span", { class: `icon-tile small-tile ${d.online && !blocked ? "accent" : ""}`, title: d.kind_label, html: icon(NET_ICON[d.kind] || "question") }),
    el("div", { class: "net-main" },
      el("div", {}, el("strong", { "data-no-i18n": "" }, title), " ",
        !d.known ? el("span", { class: "tag tag-accent" }, "New") : null, " ",
        blocked ? el("span", { class: "tag tag-warn" }, "Blocked") : null, " ",
        !d.online ? el("span", { class: "tag" }, "offline") : null),
      el("div", { class: "muted small mono" }, [d.ip, d.mac].filter(Boolean).join(" · ")),
      el("div", { class: "muted small" }, [d.kind_label, d.vendor, d.hostname && d.hostname !== d.name ? d.hostname : ""].filter(Boolean).join(" · "),
        d.why ? el("span", { title: d.why }, " ⓘ") : null),
      tags.length ? el("div", { class: "net-ports" }, ...tags) : null,
      d.full_port_scan ? el("div", { class: "muted small" }, `All ports checked ${netAgo(d.full_port_scan)}`) : null,
      d.notes ? el("div", { class: "small net-notes" }, d.notes) : null),
    el("div", { class: "net-actions" },
      el("button", { type: "button", class: "small ghost", title: "Rename, change the kind, add notes", onclick: () => netEditDevice(d, kinds) }, withIcon("edit", "Edit")),
      scanBtn,
      caps.includes("static") && !own ? el("button", { type: "button", class: "small ghost", title: "Always give this device the same address (DHCP reservation)",
        onclick: () => netReserve(d.mac, d.ip, d.name) }, withIcon("key", "Reserve address")) : null,
      caps.includes("kick") && !own && !blocked && d.online ? el("button", { type: "button", class: "small ghost", title: "Disconnect it from the Wi-Fi now (it can reconnect)",
        onclick: () => netDeviceAction(d, "kick") }, withIcon("logout", "Kick")) : null,
      caps.includes("block") && !own ? (blocked
        ? el("button", { type: "button", class: "small", onclick: () => netDeviceAction(d, "unblock") }, withIcon("check", "Unblock"))
        : el("button", { type: "button", class: "small ghost danger", title: "Keep it off the network until you unblock it",
            onclick: () => netDeviceAction(d, "block") }, withIcon("stop", "Block"))) : null,
      el("span", { class: "muted small" }, d.online ? "" : `last seen ${netAgo(d.last_seen)}`)));
}

function netEditDevice(d, kinds) {
  const name = el("input", { name: "label", value: d.label || "", maxlength: "80", placeholder: d.auto_name || "e.g. Living-room TV" });
  const kind = el("select", { name: "kind" }, ...Object.entries(kinds).map(([k, v]) => el("option", { value: k }, v)));
  kind.value = d.kind;
  const notes = el("textarea", { name: "notes", rows: "3", maxlength: "1000" });
  notes.value = d.notes || "";
  const known = el("input", { type: "checkbox", name: "known", checked: !!d.known });
  const forget = el("button", { type: "button", class: "small ghost danger", onclick: async () => {
    if (!confirm(`Forget ${d.name || d.ip}? It reappears (as new) if it's found again.`)) return;
    await api(`/api/network/devices/${encodeURIComponent(d.mac)}`, { method: "DELETE" }).catch((ex) => toast(ex.message));
    $("#net-dialog").close();
    showNetTab("devices");
  } }, withIcon("trash", "Forget this device"));
  netDialog(`${d.name || d.ip} · ${d.mac}`, [
    field("Name", name, d.auto_name ? `Found automatically: ${d.auto_name}` : "Leave empty to use the name found automatically."),
    field("Kind", kind), field("Notes", notes),
    el("label", { class: "check small" }, known, " Known device (not flagged as new)"),
    el("p", { class: "muted small", "data-no-i18n": "" }, [d.vendor, d.upnp?.modelName, d.upnp?.manufacturer].filter(Boolean).join(" · ")),
    forget,
  ], async () => {
    await api(`/api/network/devices/${encodeURIComponent(d.mac)}`, { method: "PATCH", body: JSON.stringify({
      label: name.value, kind: kind.value, notes: notes.value, known: known.checked }) });
    showNetTab("devices");
  });
}

function netReserve(mac = "", ip = "", name = "") {
  const f = { mac: el("input", { value: mac, required: true, class: "mono", placeholder: "aa:bb:cc:dd:ee:ff", spellcheck: "false" }),
    ip: el("input", { value: ip, required: true, class: "mono", placeholder: "192.168.1.50" }),
    name: el("input", { value: name || "", maxlength: "60" }) };
  netDialog("Reserve an address", [
    el("p", { class: "muted small" }, "The router will always give this device the same IP address. It takes effect when the device renews its lease (or reconnects)."),
    field("MAC address", f.mac), field("IP address", f.ip, "Pick a free address, ideally outside the automatic range."), field("Name", f.name, "(optional)"),
  ], async () => {
    await api("/api/network/router/static", { method: "POST", body: JSON.stringify({ mac: f.mac.value, ip: f.ip.value, name: f.name.value }) });
    toast("Address reserved.");
    if (state.net.tab === "router") showNetTab("router");
  }, "Reserve");
}

// ------------------------------------------------------------ router & DHCP
async function netRouterTab(pane) {
  let r;
  try { r = await api("/api/network/router"); } catch (ex) { pane.textContent = ex.message; return; }
  state.net.router = r;
  if (state.net.tab !== "router") return;
  pane.innerHTML = "";
  if (!r.config.type) { netRouterSetup(pane, r); return; }
  const gw = state.net.overview?.gateway;
  pane.append(el("div", { class: "net-card" },
    el("div", { class: "ai-card-head" },
      el("span", { class: `icon-tile ${r.connected ? "accent" : ""}`, html: icon("router") }),
      el("div", { class: "ai-card-title" },
        el("div", {}, el("strong", {}, r.types[r.config.type]?.label || r.config.type), " ",
          el("span", { class: `status-pill ${r.connected ? "ok" : "warn"}` }, r.connected ? "connected" : "not reachable")),
        el("div", { class: "muted small", "data-no-i18n": "" }, [r.model, r.version, r.config.address].filter(Boolean).join(" · ")),
        r.external_ip ? el("div", { class: "muted small" }, `Internet (WAN) address: ${r.external_ip}`) : null,
        r.error ? el("div", { class: "small error" }, r.error) : null),
      el("div", { class: "ai-actions" },
        gw ? el("a", { class: "small button-link", href: r.admin_url || `http://${gw}/`, target: "_blank", rel: "noopener noreferrer" }, "Router's admin page") : null,
        el("button", { type: "button", class: "small ghost", onclick: () => netRouterSetup(pane, r, true) }, withIcon("edit", "Change")),
        el("button", { type: "button", class: "small ghost danger", onclick: async () => {
          if (!confirm("Disconnect from the router? Its settings stay as they are; only the saved connection is removed.")) return;
          await api("/api/network/router", { method: "DELETE" }).catch((ex) => toast(ex.message));
          showNetTab("router");
        } }, "Disconnect")))));
  if (!r.connected) return;
  if (r.type === "manual") {
    pane.append(el("p", { class: "muted small" }, "Change DHCP and port forwarding in the router's own admin page. If it supports UPnP, choose “Automatic (UPnP)” to manage port forwarding from here."));
    return;
  }
  if (r.type === "upnp") pane.append(el("p", { class: "note small" }, "UPnP can forward ports but can't change DHCP. For DHCP, choose the router's type if it's OpenWrt or MikroTik, or use its admin page."));
  if (r.caps.includes("dhcp_settings")) pane.append(await netDhcpSettings());
  if (r.caps.includes("leases")) pane.append(await netLeases(r));
  if (r.caps.includes("forwards")) pane.append(await netForwards());
  if (r.caps.includes("block")) pane.append(await netBlockedList());
}

async function netBlockedList() {
  const box = el("div", { class: "net-card" }, el("h3", {}, "Blocked devices"),
    el("p", { class: "muted small" }, "Blocked devices get no internet and can't use the router (no address, no DNS); where the router manages the Wi-Fi they're also kept off it. Block a device from its row in Devices."));
  let list;
  try { list = await api("/api/network/router/blocked"); } catch (ex) { box.append(el("p", { class: "small error" }, ex.message)); return box; }
  if (!list.length) box.append(el("p", { class: "muted small" }, "No devices are blocked."));
  const byMac = Object.fromEntries((state.net.devices || []).map((d) => [d.mac, d]));
  for (const b of list) {
    const d = byMac[b.mac] || { mac: b.mac, name: b.name, ip: "" };
    box.append(el("div", { class: "net-lease" },
      el("span", { class: "mono" }, b.mac),
      el("span", { "data-no-i18n": "" }, d.name || b.name || "–"),
      el("span", { class: "mono muted small" }, d.ip || ""),
      el("span", {}),
      el("button", { type: "button", class: "small", onclick: () => netDeviceAction(d, "unblock") }, "Unblock")));
  }
  return box;
}

async function netRouterSetup(pane, r, editing = false) {
  pane.innerHTML = "";
  const types = r.types;
  const type = el("select", { name: "type" }, ...Object.entries(types).map(([k, v]) => el("option", { value: k }, v.label)));
  const about = el("p", { class: "muted small" });
  const address = el("input", { name: "address", value: r.config.address || "", placeholder: "192.168.1.1", spellcheck: "false" });
  const user = el("input", { name: "username", value: r.config.username || "", autocomplete: "off", spellcheck: "false" });
  const pass = el("input", { name: "password", type: "password", autocomplete: "new-password",
    placeholder: r.config.password_set ? "saved — leave empty to keep it" : "" });
  const verify = el("input", { type: "checkbox", checked: r.config.verify_tls });
  const authBox = el("div", { class: "two-col" }, field("User name", user), field("Password", pass));
  const addrBox = field("Router address", address, "Its IP address or https://address.");
  const sync = () => {
    const t = types[type.value];
    about.textContent = t.about;
    authBox.hidden = !t.auth;
    addrBox.hidden = !t.auth;
    verifyBox.hidden = !t.auth;
    if (t.auth && !user.value) user.value = type.value === "openwrt" ? "root" : "admin";
  };
  const verifyBox = el("label", { class: "check small" }, verify, " Check the router's HTTPS certificate (off: routers usually have self-signed ones)");
  type.addEventListener("change", sync);
  const msg = el("p", { class: "small" });
  const detectBtn = el("button", { type: "button", class: "small ghost", onclick: async () => {
    detectBtn.disabled = true;
    msg.className = "small busy"; msg.textContent = "Looking for the router…";
    try {
      const d = await api("/api/network/router/detect");
      type.value = d.suggested; if (!address.value && d.gateway) address.value = d.gateway; sync();
      msg.className = "small ok-text";
      msg.textContent = d.upnp ? `UPnP router found: ${[d.upnp.manufacturer, d.upnp.modelName].filter(Boolean).join(" ") || d.upnp.friendlyName}. ${d.hint}`
        : `Router at ${d.gateway || "?"}; no UPnP answer. ${d.hint}`;
    } catch (ex) { msg.className = "small error"; msg.textContent = ex.message; }
    detectBtn.disabled = false;
  } }, withIcon("refresh", "Detect my router"));
  type.value = r.config.type || "upnp";
  if (!address.value && state.net.overview?.gateway) address.value = state.net.overview.gateway;
  pane.append(el("div", { class: "net-card net-form" },
    el("h3", {}, editing ? "Router connection" : "Connect to your router"),
    el("p", { class: "muted small" }, "With a connection, SysAdmin.IA can show who has which address, reserve addresses, change the DHCP range and open ports for publishing services."),
    el("div", { class: "row" }, detectBtn), msg,
    field("Router type", type), about, addrBox, authBox, verifyBox,
    el("p", { class: "muted small" }, "The password is stored on this server and never shown again. Use a dedicated router user if the router supports it."),
    el("div", { class: "row" },
      el("button", { type: "button", class: "primary", onclick: async (e) => {
        e.currentTarget.disabled = true;
        msg.className = "small busy"; msg.textContent = "Connecting…";
        try {
          const s = await api("/api/network/router", { method: "PUT", body: JSON.stringify({ type: type.value, address: address.value,
            username: user.value, password: pass.value ? pass.value : (r.config.password_set ? null : ""), verify_tls: verify.checked }) });
          if (s.error) { msg.className = "small error"; msg.textContent = s.error; e.currentTarget.disabled = false; return; }
          toast("Router connected.");
          showNetTab("router");
        } catch (ex) { msg.className = "small error"; msg.textContent = ex.message; e.currentTarget.disabled = false; }
      } }, "Save and connect"),
      editing ? el("button", { type: "button", class: "ghost", onclick: () => showNetTab("router") }, "Cancel") : null)));
  sync();
}

async function netDhcpSettings() {
  const box = el("div", { class: "net-card net-form" }, el("h3", {}, "DHCP (automatic addresses)"));
  let s;
  try { s = await api("/api/network/router/dhcp"); } catch (ex) { box.append(el("p", { class: "small error" }, ex.message)); return box; }
  const start = el("input", { value: s.start, class: "mono" }), end = el("input", { value: s.end, class: "mono" });
  const lease = el("input", { value: s.lease_time, class: "mono", placeholder: "12h" });
  const dns = el("input", { value: s.dns || "", class: "mono", placeholder: "empty = the router itself" });
  box.append(
    el("p", { class: "muted small" }, `Network ${s.network}${s.enabled ? "" : " · DHCP is turned off on this router"}`),
    el("div", { class: "two-col" }, field("First address", start), field("Last address", end)),
    el("div", { class: "two-col" }, field("Lease time", lease, "e.g. 12h, 1d, 30m"), field("DNS servers given to devices", dns, "comma-separated, e.g. 1.1.1.1, 9.9.9.9")),
    el("div", { class: "row" }, el("button", { type: "button", onclick: async (e) => {
      if (!confirm("Change the router's DHCP settings? Devices pick up the change when they renew their address.")) return;
      e.currentTarget.disabled = true;
      try {
        await api("/api/network/router/dhcp", { method: "PUT", body: JSON.stringify({ start: start.value, end: end.value, lease_time: lease.value, dns: dns.value }) });
        toast("DHCP settings saved.");
      } catch (ex) { toast(ex.message); }
      e.currentTarget.disabled = false;
    } }, "Save DHCP settings")));
  return box;
}

async function netLeases(r) {
  const box = el("div", { class: "net-card" }, el("div", { class: "net-card-head" }, el("h3", {}, "Addresses given out"),
    r.caps.includes("static") ? el("button", { type: "button", class: "small", onclick: () => netReserve() }, withIcon("plus", "Reserve an address")) : null));
  let leases;
  try { leases = await api("/api/network/router/leases"); } catch (ex) { box.append(el("p", { class: "small error" }, ex.message)); return box; }
  if (!leases.length) { box.append(el("p", { class: "muted small" }, "No leases.")); return box; }
  const names = Object.fromEntries((state.net.devices || []).map((d) => [d.mac, d.name]));
  for (const l of leases.sort((a, b) => a.ip.localeCompare(b.ip, undefined, { numeric: true }))) {
    box.append(el("div", { class: "net-lease" },
      el("span", { class: "mono" }, l.ip),
      el("span", { "data-no-i18n": "" }, names[l.mac] || l.hostname || "–"),
      el("span", { class: "mono muted small" }, l.mac),
      l.static ? el("span", { class: "tag tag-accent" }, "reserved") : el("span", { class: "muted small" }, "automatic"),
      l.static && l.id ? el("button", { type: "button", class: "small ghost danger", onclick: async () => {
        if (!confirm(`Remove the reservation of ${l.ip}? The device gets an automatic address next time.`)) return;
        try { await api("/api/network/router/static/remove", { method: "POST", body: JSON.stringify({ id: l.id }) }); showNetTab("router"); }
        catch (ex) { toast(ex.message); }
      } }, "Remove reservation")
        : el("button", { type: "button", class: "small ghost", onclick: () => netReserve(l.mac, l.ip, names[l.mac] || l.hostname) }, "Reserve")));
  }
  return box;
}

async function netForwards() {
  const box = el("div", { class: "net-card" }, el("div", { class: "net-card-head" }, el("h3", {}, "Port forwarding"),
    el("button", { type: "button", class: "small", onclick: () => netAddForward() }, withIcon("plus", "Add a port forward"))),
  el("p", { class: "muted small" }, "Opens a port on your internet connection and sends it to a device on the LAN. Everything on that port becomes reachable from the internet."));
  let fw;
  try { fw = await api("/api/network/router/forwards"); } catch (ex) { box.append(el("p", { class: "small error" }, ex.message)); return box; }
  if (!fw.length) box.append(el("p", { class: "muted small" }, "No ports are forwarded."));
  for (const f of fw) {
    box.append(el("div", { class: "net-lease" },
      el("span", { class: "mono" }, `${f.proto.toUpperCase()} ${f.ext_port}`),
      el("span", { class: "mono" }, `→ ${f.int_ip}:${f.int_port}`),
      el("span", { "data-no-i18n": "" }, f.name || ""),
      f.enabled ? el("span", {}) : el("span", { class: "tag" }, "disabled"),
      el("button", { type: "button", class: "small ghost danger", onclick: async () => {
        if (!confirm(`Close port ${f.ext_port} (${f.proto})?`)) return;
        try { await api("/api/network/router/forwards/remove", { method: "POST", body: JSON.stringify({ id: f.id }) }); showNetTab("router"); }
        catch (ex) { toast(ex.message); }
      } }, "Remove")));
  }
  return box;
}

function netAddForward() {
  const devs = (state.net.devices || []).filter((d) => d.ip);
  const list = el("datalist", { id: "net-dev-ips" }, ...devs.map((d) => el("option", { value: d.ip }, d.name || d.vendor || d.mac)));
  const f = { name: el("input", { maxlength: "60", placeholder: "e.g. Minecraft server" }),
    proto: el("select", {}, el("option", { value: "tcp" }, "TCP"), el("option", { value: "udp" }, "UDP"), el("option", { value: "both" }, "TCP and UDP")),
    ext: el("input", { type: "number", min: "1", max: "65535", required: true }),
    ip: el("input", { list: "net-dev-ips", required: true, class: "mono", value: state.net.overview?.interfaces?.[0]?.ip || "" }),
    port: el("input", { type: "number", min: "1", max: "65535", required: true }) };
  f.ext.addEventListener("input", () => { if (!f.port.dataset.touched) f.port.value = f.ext.value; });
  f.port.addEventListener("input", () => { f.port.dataset.touched = "1"; });
  netDialog("Add a port forward", [list, field("Name", f.name), field("Protocol", f.proto),
    el("div", { class: "two-col" }, field("Port on the internet side", f.ext), field("Port on the device", f.port)),
    field("Device (LAN address)", f.ip),
    el("p", { class: "note warn small" }, "Anyone on the internet can then connect to that port. Only forward services that have their own login and are kept up to date. For websites, “Publish on the internet” adds HTTPS for you.")],
  async () => {
    await api("/api/network/router/forwards", { method: "POST", body: JSON.stringify({ name: f.name.value, proto: f.proto.value,
      ext_port: Number(f.ext.value), int_ip: f.ip.value, int_port: Number(f.port.value) }) });
    toast("Port forwarded.");
    showNetTab("router");
  }, "Add");
}

// ------------------------------------------------------------ publish on the internet
async function netPublishTab(pane) {
  let s;
  try { s = await api("/api/network/publish?check=true"); } catch (ex) { pane.textContent = ex.message; return; }
  if (state.net.tab !== "publish") return;
  state.net.publish = s;
  pane.innerHTML = "";
  const nat = s.nat === "cgnat";
  pane.append(el("div", { class: "net-card" },
    el("div", { class: "u-stats" },
      el("div", { class: "u-stat" }, el("div", { class: "label" }, "Public address"), el("div", { class: "u-value mono" }, s.public_ip || "–"),
        el("div", { class: "muted small" }, "what the internet sees")),
      el("div", { class: "u-stat" }, el("div", { class: "label" }, "Router's internet address"), el("div", { class: "u-value mono" }, s.router_ip || "–"),
        el("div", { class: "muted small" }, s.router_ip ? "from the router" : "connect the router (Router & DHCP) to compare")),
      el("div", { class: "u-stat" }, el("div", { class: "label" }, "This server"), el("div", { class: "u-value mono" }, s.lan_ip || "–"),
        el("div", { class: "muted small" }, "address on the LAN"))),
    nat ? el("p", { class: "note warn small" }, "The router's internet address isn't the public one: your provider shares addresses between customers (carrier-grade NAT) or there is another router in front. Port forwarding can't work from here — use Cloudflare Tunnel.") : null,
    !s.docker ? el("p", { class: "note warn small" }, "Publishing runs a small helper container: install Docker in the Containers module first.") : null));

  // services
  const svcBox = el("div", { class: "net-card" }, el("div", { class: "net-card-head" }, el("h3", {}, "Published services"),
    el("button", { type: "button", class: "small primary", disabled: !(s.cloudflare.connected || s.caddy.enabled), onclick: () => netPublishDialog(s) },
      withIcon("plus", "Publish a service"))));
  if (!s.services.length) svcBox.append(el("p", { class: "muted small" }, s.cloudflare.connected || s.caddy.enabled
    ? "Nothing is published yet." : "Set up one of the two ways below first."));
  for (const x of s.services) {
    svcBox.append(el("div", { class: "net-lease" },
      el("a", { href: `https://${x.hostname}/`, target: "_blank", rel: "noopener noreferrer", "data-no-i18n": "" }, x.hostname),
      el("span", { class: "mono small" }, `→ ${x.target.replace(/^http:\/\//, "")}`),
      el("span", { class: "tag" }, x.method === "cloudflare" ? "Cloudflare Tunnel" : "Port forwarding + HTTPS"),
      el("span", {}),
      el("button", { type: "button", class: "small ghost danger", onclick: async () => {
        if (!confirm(`Stop publishing ${x.hostname}? It's no longer reachable from the internet.`)) return;
        try { await api(`/api/network/publish/services/${x.id}`, { method: "DELETE" }); showNetTab("publish"); } catch (ex) { toast(ex.message); }
      } }, "Unpublish")));
  }
  pane.append(svcBox, netCloudflareCard(s), netCaddyCard(s, nat), netDdnsCard(s));
}

function netCloudflareCard(s) {
  const cf = s.cloudflare;
  const box = el("div", { class: "net-card net-form" }, el("div", { class: "net-card-head" },
    el("h3", {}, "Cloudflare Tunnel ", el("span", { class: "tag tag-accent" }, "recommended")),
    cf.connected ? el("span", { class: `status-pill ${cf.container === "running" ? "ok" : "warn"}` }, cf.container === "running" ? "connected" : cf.container || "connector missing") : null));
  box.append(el("p", { class: "muted small" }, "Your services get an https://name.yourdomain address through Cloudflare. Nothing is opened on the router, it works with any internet provider, and the server's address stays hidden. Needs a (free) Cloudflare account with your domain."));
  if (cf.connected) {
    box.append(el("p", { class: "small" }, `Domain ${cf.zone_name} · tunnel ${cf.tunnel_name}`),
      el("div", { class: "row" }, el("button", { type: "button", class: "small ghost danger", onclick: async (e) => {
        if (!confirm("Disconnect Cloudflare? Services published through it stop working, their DNS names are removed, and the tunnel is deleted.")) return;
        e.currentTarget.disabled = true;
        try { await api("/api/network/publish/cloudflare", { method: "DELETE" }); showNetTab("publish"); } catch (ex) { toast(ex.message); e.currentTarget.disabled = false; }
      } }, "Disconnect")));
    return box;
  }
  const token = el("input", { type: "password", autocomplete: "off", spellcheck: "false", placeholder: "Cloudflare API token" });
  const zone = el("select", { hidden: true });
  const msg = el("p", { class: "small" });
  const connect = el("button", { type: "button", class: "primary", hidden: true, onclick: async (e) => {
    e.currentTarget.disabled = true;
    msg.className = "small busy"; msg.textContent = "Creating the tunnel and starting its connector (downloads cloudflared the first time)…";
    try { await api("/api/network/publish/cloudflare", { method: "POST", body: JSON.stringify({ token: token.value, zone_id: zone.value }) });
          toast("Cloudflare Tunnel connected."); showNetTab("publish"); }
    catch (ex) { msg.className = "small error"; msg.textContent = ex.message; e.currentTarget.disabled = false; }
  } }, "Connect");
  box.append(
    el("ol", { class: "small net-steps" },
      el("li", {}, "Your domain must use Cloudflare's DNS (add it at dash.cloudflare.com)."),
      el("li", {}, "At dash.cloudflare.com/profile/api-tokens, create a custom token with “Account · Cloudflare Tunnel · Edit” and “Zone · DNS · Edit” (for your domain)."),
      el("li", {}, "Paste it here and click Check.")),
    el("div", { class: "row" }, token, el("button", { type: "button", onclick: async (e) => {
      e.currentTarget.disabled = true;
      msg.className = "small busy"; msg.textContent = "Checking the token…";
      try {
        const r = await api("/api/network/publish/cloudflare/check", { method: "POST", body: JSON.stringify({ token: token.value }) });
        zone.innerHTML = "";
        r.zones.forEach((z) => zone.add(new Option(z.name, z.id)));
        zone.hidden = connect.hidden = false;
        msg.className = "small ok-text"; msg.textContent = "Token OK. Choose the domain to use.";
      } catch (ex) { msg.className = "small error"; msg.textContent = ex.message; }
      e.currentTarget.disabled = false;
    } }, "Check")), msg, el("div", { class: "row" }, zone, connect));
  return box;
}

function netCaddyCard(s, nat) {
  const c = s.caddy;
  const box = el("div", { class: "net-card net-form" }, el("div", { class: "net-card-head" }, el("h3", {}, "Port forwarding + HTTPS"),
    c.enabled ? el("span", { class: `status-pill ${c.container === "running" ? "ok" : "warn"}` }, c.container || "proxy missing") : null));
  box.append(el("p", { class: "muted small" }, "Ports 80 and 443 of your internet connection are forwarded to this server, where Caddy gets free HTTPS certificates (Let's Encrypt) and sends each name to its service. Needs a public address (no carrier-grade NAT) and a DNS name pointing to it — dynamic DNS below keeps it up to date."));
  if (c.enabled) {
    box.append(el("p", { class: "small" }, [c.email ? `Certificate notices to ${c.email}` : "", c.forwarded ? "ports 80 and 443 forwarded on the router" : "forward ports 80 and 443 to this server in the router"].filter(Boolean).join(" · ")),
      el("div", { class: "row" }, el("button", { type: "button", class: "small ghost danger", onclick: async (e) => {
        if (!confirm("Remove the HTTPS proxy? Services published this way stop working; port forwards it created are closed.")) return;
        e.currentTarget.disabled = true;
        try { await api("/api/network/publish/caddy", { method: "DELETE" }); showNetTab("publish"); } catch (ex) { toast(ex.message); e.currentTarget.disabled = false; }
      } }, "Remove")));
    return box;
  }
  const email = el("input", { type: "email", placeholder: "you@example.com" });
  const canForward = (state.net.router?.caps || []).includes("forwards");
  const fwd = el("input", { type: "checkbox", checked: canForward, disabled: !canForward });
  const msg = el("p", { class: "small" });
  box.append(nat ? el("p", { class: "note warn small" }, "Not possible with this connection (carrier-grade NAT).") : null,
    field("Email for Let's Encrypt", email, "(optional) Used only for certificate expiry warnings."),
    el("label", { class: "check small" }, fwd, canForward ? " Forward ports 80 and 443 on the router automatically" : " Forward ports 80 and 443 automatically (connect the router first)"),
    msg, el("div", { class: "row" }, el("button", { type: "button", disabled: nat || !s.docker, onclick: async (e) => {
      e.currentTarget.disabled = true;
      msg.className = "small busy"; msg.textContent = "Starting Caddy (downloads it the first time)…";
      try { const r = await api("/api/network/publish/caddy", { method: "POST", body: JSON.stringify({ email: email.value, forward_ports: fwd.checked }) });
            toast(r.note || "HTTPS proxy ready."); showNetTab("publish"); }
      catch (ex) { msg.className = "small error"; msg.textContent = ex.message; e.currentTarget.disabled = false; }
    } }, "Set up")));
  return box;
}

function netDdnsCard(s) {
  const d = s.ddns || {};
  const prov = el("select", {}, el("option", { value: "" }, "Off"), el("option", { value: "cloudflare" }, "Cloudflare"), el("option", { value: "duckdns" }, "DuckDNS (free)"));
  prov.value = d.provider || "";
  const names = el("input", { value: (d.hostnames || []).join(", "), spellcheck: "false" });
  const token = el("input", { type: "password", autocomplete: "off" });
  const msg = el("p", { class: "small" });
  const sync = () => {
    names.placeholder = prov.value === "duckdns" ? "myhome (for myhome.duckdns.org)" : "home.example.com";
    token.placeholder = prov.value === "duckdns" ? (d.duckdns_token_saved ? "saved — leave empty to keep it" : "DuckDNS token")
      : (s.cloudflare.token_saved ? "saved — leave empty to keep it" : "Cloudflare API token (Zone · DNS · Edit)");
    names.parentElement.hidden = token.parentElement.hidden = !prov.value;
  };
  prov.addEventListener("change", sync);
  const box = el("div", { class: "net-card net-form" }, el("h3", {}, "Dynamic DNS"),
    el("p", { class: "muted small" }, "Home internet addresses change from time to time. Dynamic DNS keeps your names pointing at the current one (checked every 5 minutes). Needed for port forwarding; not for Cloudflare Tunnel."),
    d.provider ? el("p", { class: `small ${d.last_error ? "error" : "ok-text"}` }, d.last_error || (d.last_ip ? `Pointing to ${d.last_ip} · updated ${netAgo(d.last_update)}` : "Waiting for the first update")) : null,
    field("Provider", prov), field("Names to keep updated", names), field("Token", token), msg,
    el("div", { class: "row" }, el("button", { type: "button", onclick: async (e) => {
      e.currentTarget.disabled = true;
      try { await api("/api/network/publish/ddns", { method: "PUT", body: JSON.stringify({ provider: prov.value, hostnames: names.value, token: token.value || null }) });
            toast("Dynamic DNS saved."); showNetTab("publish"); }
      catch (ex) { msg.className = "small error"; msg.textContent = ex.message; e.currentTarget.disabled = false; }
    } }, "Save")));
  setTimeout(sync);
  return box;
}

async function netPublishDialog(s) {
  let targets = [];
  try { targets = await api("/api/network/publish/targets"); } catch { /* none */ }
  const methods = [];
  if (s.cloudflare.connected) methods.push(["cloudflare", "Cloudflare Tunnel"]);
  if (s.caddy.enabled) methods.push(["caddy", "Port forwarding + HTTPS"]);
  const method = el("select", {}, ...methods.map(([v, l]) => el("option", { value: v }, l)));
  const pick = el("select", {}, el("option", { value: "" }, "Type an address below…"), ...targets.map((t) => el("option", { value: t.target }, t.label)));
  const target = el("input", { required: true, class: "mono", placeholder: "127.0.0.1:8080 or 192.168.1.20:80", spellcheck: "false" });
  pick.addEventListener("change", () => { if (pick.value) target.value = pick.value; });
  const host = el("input", { required: true, spellcheck: "false", placeholder: s.cloudflare.zone_name ? `photos.${s.cloudflare.zone_name}` : "photos.example.com" });
  const hint = el("span", { class: "muted small" });
  const sync = () => {
    hint.textContent = method.value === "cloudflare" ? `Must end in ${s.cloudflare.zone_name}. The DNS name is created for you.`
      : `Its DNS must point to ${s.public_ip || "your public address"} (use Dynamic DNS below, or an A record at your DNS provider).`;
  };
  method.addEventListener("change", sync);
  sync();
  netDialog("Publish a service", [
    field("What to publish", pick), field("Address on the LAN", target, "Where the service runs: this server is 127.0.0.1; containers use their port on the server."),
    el("label", {}, "Public name", host, hint), field("How", method),
    el("p", { class: "note warn small" }, "Anyone on the internet can open it. Only publish apps that have their own login (or are meant to be public) and keep them updated."),
  ], async () => {
    const body = { hostname: host.value, target: target.value, method: method.value, allow_admin: false };
    try {
      await api("/api/network/publish/services", { method: "POST", body: JSON.stringify(body) });
    } catch (ex) {
      if (!/SysAdmin\.IA itself/.test(ex.message) || !confirm(`${ex.message}\n\nPublish it anyway?`)) throw ex;
      await api("/api/network/publish/services", { method: "POST", body: JSON.stringify({ ...body, allow_admin: true }) });
    }
    toast(`Published at https://${host.value}`);
    showNetTab("publish");
  }, "Publish");
}

// ------------------------------------------------------------ settings
function netSettingsTab(pane) {
  const o = state.net.overview || {};
  const s = o.settings || { watch: true, interval: 15, extra: "" };
  const watch = el("input", { type: "checkbox", checked: s.watch });
  const interval = el("input", { type: "number", min: "5", max: "1440", value: s.interval });
  const extra = el("input", { value: s.extra || "", placeholder: "e.g. 192.168.2.0/24", spellcheck: "false" });
  const msg = el("p", { class: "small" });
  pane.innerHTML = "";
  pane.append(el("div", { class: "net-card net-form" },
    el("label", { class: "check" }, watch, " Watch the network: scan regularly so new and offline devices are noticed"),
    field("Scan every … minutes", interval, "Quick check: new devices are identified, known ones only marked as online."),
    field("Other networks to scan", extra, "(optional) Private networks this server can reach besides its own, comma-separated (at most /22 each)."),
    msg, el("div", { class: "row" }, el("button", { type: "button", class: "primary", onclick: async () => {
      try {
        const r = await api("/api/network/settings", { method: "PUT", body: JSON.stringify({ watch: watch.checked, interval: Number(interval.value), extra: extra.value }) });
        state.net.overview.settings = r;
        msg.className = "small ok-text"; msg.textContent = "Saved.";
      } catch (ex) { msg.className = "small error"; msg.textContent = ex.message; }
    } }, "Save"))),
  el("div", { class: "net-card" }, el("h3", {}, "How devices are found"),
    el("p", { class: "muted small" }, "A scan briefly contacts every address of this server's network (no admin rights needed), then identifies what answered: the maker from the network card's address, names announced on the LAN (mDNS, UPnP, Windows names), the router's DNS, open ports and web page titles. Phones and laptops often use a private (randomized) network address, so they appear without a maker."),
    o.avahi ? null : el("p", { class: "note warn small" }, "avahi-browse isn't installed, so names announced via mDNS (printers, Apple devices, Chromecasts…) aren't read. Installing the next update (Admin → Updates) adds it."),
    el("ul", { class: "small muted" }, ...(o.interfaces || []).map((i) => el("li", { class: "mono" }, `${i.iface}: ${i.ip} in ${i.network}${i.scan !== i.network ? ` (scanning ${i.scan})` : ""}`)))));
}

window.loadNetwork = loadNetwork;
