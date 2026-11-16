"use strict";
/* SysAdmin.IA – display preferences (theme, accent colour, text size…) and interface language.
 *
 * Loaded in <head>, before the page is drawn, so the chosen theme applies from the first paint.
 * Preferences live in localStorage (per browser) and, once signed in, in the user's account
 * (app.js syncs them through /api/me/prefs), so they follow the user to other devices.
 *
 * Translation: the UI is written in English. A language file (static/i18n/<code>.js) registers a
 * table { "English text": "translation" }. This script swaps every visible text node and
 * title/placeholder/aria-label/label/alt attribute whose text is in the table, including text the
 * app adds later (a MutationObserver watches the page). Keys with {0}, {1}… are patterns:
 * "Removed {0}." → "Se eliminó {0}." (each {n} is itself translated when it is in the table).
 * Elements marked data-no-i18n (or translate="no"), code, terminals and chat messages are never
 * translated. To add a language: copy i18n/es.js and i18n/es-server.js to <code>.js and
 * <code>-server.js, translate the values, and add the code to LANGS below and in app/user_prefs.py.
 * Rule for app code: always put English in the DOM (this script translates it), and use t()
 * only for text that never reaches the DOM.
 */
(function () {
  const LANGS = { en: "English", es: "Español" };
  const DEFAULTS = { theme: "system", accent: "orange", text: "100", contrast: false, motion: true, lang: "en" };
  const CHOICES = {
    theme: ["system", "light", "dark", "black"],
    accent: ["orange", "blue", "green", "purple", "pink", "teal", "graphite"],
    text: ["90", "100", "112", "125"],
    lang: Object.keys(LANGS),
  };
  const KEY = "sai.prefs";
  const root = document.documentElement;

  function clean(p) {
    const out = { ...DEFAULTS };
    for (const k of Object.keys(DEFAULTS)) {
      const v = p && p[k];
      if (v === undefined || v === null) continue;
      if (CHOICES[k]) { if (CHOICES[k].includes(String(v))) out[k] = String(v); }
      else out[k] = !!v;
    }
    return out;
  }
  function loadLocal() {
    try { return clean(JSON.parse(localStorage.getItem(KEY) || "{}")); } catch { return { ...DEFAULTS }; }
  }
  function saveLocal() {
    try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch { /* private mode */ }
  }
  let prefs = loadLocal();

  // ------------------------------------------------------------ theme
  const darkMq = window.matchMedia ? matchMedia("(prefers-color-scheme: dark)") : null;
  function mode() {
    if (prefs.theme === "system") return darkMq && darkMq.matches ? "dark" : "light";
    return prefs.theme === "light" ? "light" : "dark";
  }
  function applyTheme() {
    root.dataset.mode = mode();            // light | dark: what the CSS colours follow
    root.dataset.theme = prefs.theme;      // system | light | dark | black
    root.dataset.accent = prefs.accent;
    root.dataset.text = prefs.text;
    root.toggleAttribute("data-contrast", !!prefs.contrast);
    root.toggleAttribute("data-reduce-motion", !prefs.motion);
    window.dispatchEvent(new CustomEvent("sai:theme", { detail: { mode: mode(), prefs: { ...prefs } } }));
  }
  if (darkMq) {
    const onSys = () => { if (prefs.theme === "system") applyTheme(); };
    darkMq.addEventListener ? darkMq.addEventListener("change", onSys) : darkMq.addListener(onSys);
  }
  applyTheme();

  // ------------------------------------------------------------ translation tables
  const tables = {};   // lang -> { map: Map, patterns: [] }
  const norm = (s) => s.replace(/\s+/g, " ").trim();
  const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  function register(lang, table) {          // may be called several times per language (tables merge)
    const t = tables[lang] || { map: new Map(), patterns: [] };
    for (const [k, v] of Object.entries(table)) {
      if (/\{\d\}/.test(k)) {
        const order = [];
        const src = norm(k).split(/(\{\d\})/).map((part) => {
          const m = part.match(/^\{(\d)\}$/);
          if (m) { order.push(+m[1]); return "(.*?)"; }
          return reEsc(part);
        }).join("");
        t.patterns.push({ re: new RegExp(`^${src}$`, "s"), order, out: v, weight: norm(k).replace(/\{\d\}/g, "").length });
      } else {
        t.map.set(norm(k), v);
      }
    }
    t.patterns.sort((a, b) => b.weight - a.weight);   // most specific first
    tables[lang] = t;
    if (lang === prefs.lang) retranslateAll();
  }

  function exact(s, t) { return t.map.get(s); }
  function viaPattern(s, t, depth) {
    for (const p of t.patterns) {
      const m = p.re.exec(s);
      if (!m) continue;
      const groups = {};
      p.order.forEach((n, i) => { groups[n] = m[i + 1]; });
      return p.out.replace(/\{(\d)\}/g, (_, n) => loose(groups[n] || "", t, depth + 1));
    }
    return undefined;
  }
  // For pattern values: exact → pattern → sentence by sentence → list items; else unchanged.
  function loose(s, t, depth = 0) {
    if (!s.trim() || depth > 4) return s;
    const lead = s.match(/^\s*/)[0], trail = s.match(/\s*$/)[0], core = s.trim();
    let out = exact(core, t) ?? viaPattern(core, t, depth);
    if (out === undefined && /\.$/.test(core)) {           // "item." where the table has "item"
      const bare = core.slice(0, -1);
      const x = exact(bare, t) ?? viaPattern(bare, t, depth + 1);
      if (x !== undefined) out = x + ".";
    }
    if (out === undefined) {
      const sentences = core.split(/(?<=[.!?])\s+(?=[A-Z])/);
      if (sentences.length > 1) out = sentences.map((x) => loose(x, t, depth + 1)).join(" ");
    }
    if (out === undefined) {
      const dot = /\.$/.test(core) ? "." : "";
      const body = dot ? core.slice(0, -1) : core;
      for (const sep of ["; ", ", ", " · "]) {
        if (!body.includes(sep)) continue;
        const parts = body.split(sep);
        const tr = parts.map((x) => exact(x, t) ?? viaPattern(x, t, depth + 1));
        if (tr.some((x) => x !== undefined)) { out = parts.map((x, i) => tr[i] ?? x).join(sep) + dot; break; }
      }
    }
    return out === undefined ? s : lead + out + trail;
  }
  // Whole-text translation (text nodes, attributes): undefined when there is no translation.
  function tr(s, lang = prefs.lang) {
    const t = tables[lang];
    if (!t || lang === "en" || typeof s !== "string") return undefined;
    const core = norm(s);
    if (!core || !/[A-Za-z]/.test(core)) return undefined;
    const out = exact(core, t) ?? viaPattern(core, t, 0);
    if (out === undefined) return undefined;
    return s.match(/^\s*/)[0] + out + s.match(/\s*$/)[0];
  }
  // For scripts: t("Saved.") or t("Removed {0}.", name)
  function t(s, ...args) {
    let out = tr(s) ?? s;
    if (args.length) out = out.replace(/\{(\d)\}/g, (_, n) => (args[n] ?? ""));
    return out;
  }

  // ------------------------------------------------------------ DOM translation
  // Never translated: code, terminals, chat messages and names people chose (files, chats, containers…).
  const SKIP = "script,style,pre,code,textarea,kbd,.mono,.xterm,.msg .content,.thinking-body,[data-no-i18n],[translate=no]," +
    "#conv-list .t,.fb-name,.vol-name strong,.dk-row-title strong,.ai-card-title strong,.user-who strong," +
    "#hostname,#host-os,#profile-btn-name,#profile-menu-name";
  const ATTRS = ["title", "placeholder", "aria-label", "label", "alt"];
  const textState = new WeakMap();   // Text node -> { orig, shown }
  const attrState = new WeakMap();   // Element -> { attr: { orig, shown } }
  const skipped = (el) => !el || !!el.closest(SKIP);

  function doText(n) {
    const st = textState.get(n);
    if (st && n.data === st.shown) return;        // our own change
    if (st) textState.delete(n);
    if (prefs.lang === "en" || !/[A-Za-z]/.test(n.data) || skipped(n.parentElement)) return;
    const out = tr(n.data);
    if (out === undefined || out === n.data) return;
    textState.set(n, { orig: n.data, shown: out });
    n.data = out;
  }
  function doAttr(el, a) {
    const v = el.getAttribute(a);
    let st = attrState.get(el);
    const cur = st && st[a];
    if (cur && v === cur.shown) return;
    if (cur) delete st[a];
    if (v === null || prefs.lang === "en" || skipped(el)) return;
    const out = tr(v);
    if (out === undefined || out === v) return;
    if (!st) { st = {}; attrState.set(el, st); }
    st[a] = { orig: v, shown: out };
    el.setAttribute(a, out);
  }
  function walk(node) {
    if (node.nodeType === 3) { doText(node); return; }
    if (node.nodeType !== 1) return;
    if (node.matches(SKIP)) return;
    for (const a of ATTRS) if (node.hasAttribute(a)) doAttr(node, a);
    const w = document.createTreeWalker(node, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode: (x) => (x.nodeType === 1 && x.matches(SKIP) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    let x = w.nextNode();
    while (x) {
      if (x.nodeType === 3) doText(x);
      else for (const a of ATTRS) if (x.hasAttribute(a)) doAttr(x, a);
      x = w.nextNode();
    }
  }
  // Put everything back in English, then translate again in the current language.
  function retranslateAll() {
    if (!document.body) return;
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let x = w.currentNode;
    while (x) {
      if (x.nodeType === 3) {
        const st = textState.get(x);
        if (st) { if (x.data === st.shown) x.data = st.orig; textState.delete(x); }
      } else {
        const st = attrState.get(x);
        if (st) {
          for (const [a, s] of Object.entries(st)) if (x.getAttribute(a) === s.shown) x.setAttribute(a, s.orig);
          attrState.delete(x);
        }
      }
      x = w.nextNode();
    }
    if (prefs.lang !== "en") walk(document.body);
    const title = document.querySelector("title");
    if (title) walk(title);
  }

  const observer = new MutationObserver((records) => {
    if (prefs.lang === "en" || !tables[prefs.lang]) return;
    for (const r of records) {
      if (r.type === "childList") r.addedNodes.forEach(walk);
      else if (r.type === "characterData") doText(r.target);
      else if (r.type === "attributes") doAttr(r.target, r.attributeName);
    }
  });
  observer.observe(root, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });

  // Native dialogs: translate the message (whole, or paragraph by paragraph).
  function trMsg(m) {
    if (typeof m !== "string" || prefs.lang === "en") return m;
    const whole = tr(m);
    if (whole !== undefined) return whole;
    return m.split(/(\n+)/).map((p) => (/^\n+$/.test(p) || !p.trim() ? p : (tr(p) ?? loose(p, tables[prefs.lang] || { map: new Map(), patterns: [] })))).join("");
  }
  for (const fn of ["alert", "confirm", "prompt"]) {
    const orig = window[fn].bind(window);
    window[fn] = (msg, ...rest) => orig(trMsg(msg), ...rest);
  }

  // ------------------------------------------------------------ language files
  // Each language: the interface text, and the text that comes from the server (catalogs, messages).
  const FILES = (lang) => [`/static/i18n/${lang}.js`, `/static/i18n/${lang}-server.js`];
  const loading = {};
  function loadLang(lang, sync = false) {
    if (lang === "en" || tables[lang]) return Promise.resolve();
    if (sync && document.readyState === "loading") {
      // parser-blocking, so the page never shows in English first
      for (const src of FILES(lang)) document.write(`<script src="${src}"><\/script>`);
      return Promise.resolve();
    }
    if (!loading[lang]) {
      loading[lang] = Promise.all(FILES(lang).map((src) => new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = src; s.onload = resolve; s.onerror = () => reject(new Error("Couldn't load the language file."));
        document.head.append(s);
      }))).catch((e) => { delete loading[lang]; throw e; });
    }
    return loading[lang];
  }
  root.lang = prefs.lang;
  loadLang(prefs.lang, true);
  document.addEventListener("DOMContentLoaded", retranslateAll);

  // ------------------------------------------------------------ public API
  const listeners = [];
  function set(changes, { save = true } = {}) {
    const before = prefs;
    prefs = clean({ ...prefs, ...changes });
    saveLocal();
    applyTheme();
    if (prefs.lang !== before.lang) {
      root.lang = prefs.lang;
      loadLang(prefs.lang).then(retranslateAll, () => retranslateAll());
    }
    if (save) listeners.forEach((f) => { try { f({ ...prefs }); } catch { /* ignore */ } });
    return { ...prefs };
  }
  window.SAI_PREFS = {
    LANGS, DEFAULTS, CHOICES,
    get: () => ({ ...prefs }),
    set,
    mode,
    onChange: (f) => listeners.push(f),
    // Preferences stored in the account win over this browser's copy.
    adopt(server) {
      if (server && Object.keys(server).length) set(server, { save: false });
      else if (JSON.stringify(prefs) !== JSON.stringify(DEFAULTS)) listeners.forEach((f) => f({ ...prefs }));
    },
  };
  window.SAI_I18N = { register, tr, t, retranslate: retranslateAll };
  window.t = t;
})();
