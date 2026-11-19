// ------------------------------------------------------------ Python environments (Automation → Python environments)
// Virtual environments the "Run Python" job operator can use. Each one has its own Python version and packages.
// Creating one and installing packages run as background tasks on the server; their output is shown live here.

const PYENV_POLL_MS = 1500;

async function loadPyEnvs() {
  clearTimeout(state.pyenvTimer);
  const list = $("#pyenv-list");
  let st;
  try { st = await api("/api/pyenvs"); }
  catch (ex) {
    list.innerHTML = "";
    list.append(el("div", { class: "card" }, el("p", { class: "error" }, ex.message)));
    return;
  }
  if (state.view !== "pyenvs") return;
  state.pyenvs = st;
  renderPyServer(st);
  renderPyTasks(st.tasks || []);
  list.innerHTML = "";
  if (!st.envs.length) {
    list.append(el("div", { class: "card empty-state" },
      el("p", {}, "No Python environments yet."),
      el("p", { class: "muted small" }, "Create one to give a Run Python task its own packages (and, with uv, its own Python version). "
        + "Tasks without an environment use the base Python.")));
  }
  for (const e of st.envs) list.append(pyEnvCard(e));
  if ((st.tasks || []).some((t) => t.state === "running")) {
    state.pyenvTimer = setTimeout(() => state.view === "pyenvs" && pollPyTasks(), PYENV_POLL_MS);
  }
}

// While something runs, only the task logs are refreshed (cheap); the full page reloads when a task ends.
async function pollPyTasks() {
  const running = (state.pyenvs?.tasks || []).filter((t) => t.state === "running");
  let ended = false;
  for (const t of running) {
    try {
      const nt = await api(`/api/pyenvs/tasks/${t.id}`);
      Object.assign(t, nt);
      if (nt.state !== "running") ended = true;
    } catch { ended = true; }
  }
  if (state.view !== "pyenvs") return;
  if (ended) { loadPyEnvs(); return; }
  renderPyTasks(state.pyenvs.tasks);
  state.pyenvTimer = setTimeout(() => state.view === "pyenvs" && pollPyTasks(), PYENV_POLL_MS);
}

function renderPyServer(st) {
  const box = $("#pyenv-server");
  box.innerHTML = "";
  const interp = st.interpreters || [];
  box.append(
    el("div", { class: "ai-card-head" },
      el("span", { class: "icon-tile accent", html: icon("python") }),
      el("div", { class: "ai-card-title" },
        el("b", {}, "Python on this server"),
        el("div", { class: "muted small" }, "Run Python tasks without an environment use the base Python.")),
      el("div", { class: "ai-actions" },
        st.uv ? null : el("button", { type: "button", class: "small", onclick: (e) => installUv(e.currentTarget) }, withIcon("download", "Install uv")))),
    el("dl", { class: "proc-facts small" },
      el("dt", { class: "muted" }, "Base Python"),
      el("dd", {}, st.base?.version ? `${st.base.version} · ` : "", el("code", { "data-no-i18n": "" }, st.base?.path || "–")),
      el("dt", { class: "muted" }, "Installed versions"),
      el("dd", { "data-no-i18n": "" }, interp.length ? interp.map((i) => i.version).join(" · ") : "–"),
      el("dt", { class: "muted" }, "uv"),
      el("dd", {}, st.uv ? `${st.uv} · ` : "Not installed", st.uv ? el("span", { class: "muted" }, "any Python version can be used") : null),
      el("dt", { class: "muted" }, "Stored in"),
      el("dd", {}, el("code", { "data-no-i18n": "" }, st.root || "–"))),
    st.uv ? null : el("p", { class: "muted small" },
      "uv is a small tool (installed in SysAdmin.IA's own folder, not system-wide) that downloads any Python version for an environment, e.g. 3.9 or 3.13. "
      + "Without it, environments can only use the versions installed above."));
}

function renderPyTasks(tasks) {
  const box = $("#pyenv-tasks");
  const recent = tasks.filter((t) => t.state === "running" || (t.finished && Date.now() / 1000 - t.finished < 600));
  box.hidden = !recent.length;
  const open = new Set($$("#pyenv-tasks details[open]").map((d) => d.dataset.id));
  box.innerHTML = "";
  for (const t of recent) {
    const what = {
      create: `Creating ${t.env}`, install: `Installing packages in ${t.env}`, uninstall: `Removing a package from ${t.env}`, uv: "Installing uv",
    }[t.kind] || t.kind;
    const [label, cls] = { running: ["running…", ""], done: ["done", "ok"], failed: ["failed", "crit"] }[t.state] || [t.state, ""];
    const pre = el("pre", { class: "update-notes mono small", "data-no-i18n": "" }, (t.log || []).join("\n") || "…");
    const d = el("details", { class: "proc-logs", "data-id": t.id, open: t.state === "running" || open.has(t.id) || null },
      el("summary", { class: "small" }, el("strong", {}, what), " ", el("span", { class: `status-pill ${cls}` }, label),
        t.error ? el("span", { class: "error" }, ` ${t.error}`) : null),
      pre);
    box.append(d);
    pre.scrollTop = pre.scrollHeight;
  }
}

function pyEnvCard(e) {
  const facts = [
    ["Python", e.version || "–"],
    ["Size", e.size != null ? fmtBytes(e.size) : "–"],
    ["Created", e.created ? fmtWhen(e.created) : "–"],
    ["Made with", e.tool === "uv" ? "uv" : "venv"],
    ["Used by", e.used_by?.length ? e.used_by.join(", ") : "No DAG yet"],
  ];
  const busy = e.busy;
  return el("div", { class: `card proc-card ${e.ok ? "" : "warn"}` },
    el("div", { class: "ai-card-head" },
      el("span", { class: `icon-tile ${e.ok ? "accent" : ""}`, html: icon("python") }),
      el("div", { class: "ai-card-title" },
        el("div", {}, el("strong", { "data-no-i18n": "" }, e.name), " ",
          busy ? el("span", { class: "status-pill" }, "busy…") : !e.ok ? el("span", { class: "status-pill warn" }, "broken") : null),
        el("div", { class: "muted small" }, e.description || "No description")),
      el("div", { class: "ai-actions" },
        el("button", { type: "button", class: "small", disabled: busy || !e.ok, onclick: () => pyPackagesDialog(e) }, withIcon("box", "Packages")),
        el("button", { type: "button", class: "small ghost", onclick: () => pyEditDialog(e) }, withIcon("edit", "Edit")),
        el("button", { type: "button", class: "small ghost danger", disabled: busy, onclick: () => pyDelete(e) }, withIcon("trash", "Delete")))),
    el("dl", { class: "proc-facts small" }, ...facts.flatMap(([k, v]) => [el("dt", { class: "muted" }, k),
      el("dd", { "data-no-i18n": ["Python", "Size", "Used by"].includes(k) && v !== "No DAG yet" ? "" : null }, String(v))])),
    !e.ok && !busy ? el("p", { class: "small error" }, "Its Python doesn't start (it may have been removed from the server). Delete it and create it again.") : null);
}

async function installUv(btn) {
  btn.disabled = true;
  try { await api("/api/pyenvs/uv", { method: "POST" }); toast("Installing uv…"); }
  catch (ex) { toast(ex.message, 6000); }
  loadPyEnvs();
}

// ---- new environment
function pyNewDialog() {
  const st = state.pyenvs || {};
  const sel = el("select", { name: "python" },
    el("option", { value: "" }, `Base Python${st.base?.version ? ` (${st.base.version})` : ""}`),
    ...(st.interpreters || []).filter((i) => i.path !== st.base?.path).map((i) => el("option", { value: i.path }, `Python ${i.version} · ${i.path}`)),
    el("option", { value: "@version" }, st.uv ? "Another version (downloaded with uv)…" : "Another version (needs uv)…"));
  const ver = el("input", { name: "version", placeholder: "3.12", spellcheck: "false", class: "mono", inputmode: "decimal" });
  const verRow = field("Python version", ver, st.uv ? "e.g. 3.9, 3.12 or 3.13.1. uv downloads it the first time (a few MB)."
    : "Install uv first (on this page), then any version can be used.");
  verRow.hidden = true;
  sel.addEventListener("change", () => { verRow.hidden = sel.value !== "@version"; if (!verRow.hidden) ver.focus(); });
  const name = el("input", { name: "name", required: true, spellcheck: "false", placeholder: "data-tools", maxlength: "40", class: "mono" });
  netDialog("New Python environment", [
    field("Name", name, "Lowercase letters, digits, . _ and -. Tasks refer to it by this name."),
    field("Python", sel),
    verRow,
    field("Packages", el("textarea", { name: "packages", rows: "3", spellcheck: "false", class: "mono", placeholder: "requests\npandas>=2.2" }),
      "Optional. One per line or separated by spaces, like pip install. You can add more later."),
    field("Description", el("input", { name: "description", maxlength: "300", placeholder: "What it's for" })),
  ], async (f) => {
    const python = sel.value === "@version" ? ver.value.trim() : sel.value;
    if (sel.value === "@version" && !python) throw new Error("Type a Python version, e.g. 3.12.");
    await api("/api/pyenvs", { method: "POST", body: JSON.stringify({
      name: f.name.value.trim(), python, packages: f.packages.value, description: f.description.value }) });
    toast("Creating the environment…");
    loadPyEnvs();
  }, "Create");
  setTimeout(() => name.focus(), 50);
}

// ---- edit (description; the name is what tasks refer to, so it can't change)
function pyEditDialog(e) {
  netDialog(`Edit ${e.name}`, [
    field("Name", el("input", { value: e.name, disabled: true, class: "mono" }), "The name can't change: tasks refer to it. Create a new environment instead."),
    field("Description", el("input", { name: "description", maxlength: "300", value: e.description || "", placeholder: "What it's for" })),
  ], async (f) => {
    await api(`/api/pyenvs/${encodeURIComponent(e.name)}`, { method: "PUT", body: JSON.stringify({ description: f.description.value }) });
    toast("Saved.");
    loadPyEnvs();
  });
}

async function pyDelete(e) {
  const used = e.used_by?.length ? `\n\nUsed by: ${e.used_by.join(", ")}. Those tasks will fail until you pick another environment.` : "";
  if (!confirm(`Delete the Python environment ${e.name} and all its packages?${used}`)) return;
  try {
    await api(`/api/pyenvs/${encodeURIComponent(e.name)}`, { method: "DELETE" });
    toast("Environment deleted.");
  } catch (ex) { toast(ex.message, 6000); }
  loadPyEnvs();
}

// ---- packages: list with remove buttons, and an install box
async function pyPackagesDialog(e) {
  const listBox = el("div", { class: "pyenv-pkgs" }, el("p", { class: "muted small" }, "Loading…"));
  const filter = el("input", { type: "search", placeholder: "Filter", class: "small", spellcheck: "false" });
  const pkgsInput = el("textarea", { name: "packages", rows: "2", spellcheck: "false", class: "mono", placeholder: "requests pandas>=2.2" });
  const upgrade = el("input", { type: "checkbox", name: "upgrade" });
  netDialog(`Packages in ${e.name}`, [
    el("div", { class: "pyenv-pkg-head" }, el("strong", { class: "small" }, `Python ${e.version || ""}`), filter),
    listBox,
    field("Install packages", pkgsInput, "One per line or separated by spaces, e.g. requests or numpy==2.1.0."),
    el("label", { class: "check" }, upgrade, " Upgrade them if they're already installed"),
  ], async () => {
    await api(`/api/pyenvs/${encodeURIComponent(e.name)}/install`, { method: "POST",
      body: JSON.stringify({ packages: pkgsInput.value, upgrade: upgrade.checked }) });
    toast("Installing…");
    loadPyEnvs();
  }, "Install");
  let pkgs = [];
  const draw = () => {
    const q = filter.value.trim().toLowerCase();
    const shown = pkgs.filter((p) => !q || p.name.toLowerCase().includes(q));
    listBox.innerHTML = "";
    if (!shown.length) { listBox.append(el("p", { class: "muted small" }, pkgs.length ? "No match." : "No packages.")); return; }
    listBox.append(el("table", { class: "pyenv-pkg-table small" }, el("tbody", {}, ...shown.map((p) => el("tr", {},
      el("td", { class: "mono", "data-no-i18n": "" }, p.name),
      el("td", { class: "mono muted", "data-no-i18n": "" }, p.version),
      el("td", {}, ["pip", "setuptools", "wheel"].includes(p.name.toLowerCase()) ? null
        : el("button", { type: "button", class: "icon-btn small ghost danger", title: "Remove", "aria-label": `Remove ${p.name}`,
          html: icon("x"), onclick: () => pyUninstall(e, p.name) })))))));
  };
  filter.addEventListener("input", draw);
  try { pkgs = await api(`/api/pyenvs/${encodeURIComponent(e.name)}/packages`); draw(); }
  catch (ex) { listBox.innerHTML = ""; listBox.append(el("p", { class: "error small" }, ex.message)); }
}

async function pyUninstall(e, pkg) {
  if (!confirm(`Remove ${pkg} from ${e.name}?`)) return;
  try {
    await api(`/api/pyenvs/${encodeURIComponent(e.name)}/uninstall`, { method: "POST", body: JSON.stringify({ package: pkg }) });
    $("#net-dialog").close();
    toast(`Removing ${pkg}…`);
  } catch (ex) { toast(ex.message, 6000); }
  loadPyEnvs();
}

$("#pyenv-add").addEventListener("click", pyNewDialog);
window.loadPyEnvs = loadPyEnvs;
