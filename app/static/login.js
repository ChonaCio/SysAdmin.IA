"use strict";
const form = document.getElementById("login-form");
const errEl = document.getElementById("login-error");

// Language and light/dark switch (remembered in this browser; the account's choice applies after signing in).
const P = window.SAI_PREFS;
const langSel = document.getElementById("login-lang");
for (const [code, name] of Object.entries(P.LANGS)) langSel.add(new Option(name, code));
langSel.value = P.get().lang;
langSel.addEventListener("change", () => P.set({ lang: langSel.value }));
const themeBtn = document.getElementById("login-theme");
function themeLabel() { themeBtn.textContent = P.mode() === "dark" ? "☀ Light mode" : "☾ Dark mode"; }
themeBtn.addEventListener("click", () => { P.set({ theme: P.mode() === "dark" ? "light" : "dark" }); themeLabel(); });
themeLabel();

fetch("/api/setup-status").then(r => r.json()).then(s => {
  if (!s.has_users) document.getElementById("no-users").hidden = false;
}).catch(() => {});

fetch("/api/connection").then(r => r.json()).then(c => {
  if (c.secure) return;
  document.getElementById("insecure-note").hidden = false;
  if (c.https_url) {
    const a = document.getElementById("https-link");
    a.href = c.https_url;
    a.hidden = false;
  }
}).catch(() => {});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errEl.hidden = true;
  const data = Object.fromEntries(new FormData(form));
  const btn = form.querySelector("button");
  btn.disabled = true;
  try {
    const r = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-SAI-CSRF": "1" },
      body: JSON.stringify(data),
    });
    if (r.ok) { location.href = "/"; return; }
    const body = await r.json().catch(() => ({}));
    errEl.textContent = body.detail || "Sign-in failed.";
    errEl.hidden = false;
  } catch {
    errEl.textContent = "Network error.";
    errEl.hidden = false;
  } finally {
    btn.disabled = false;
  }
});
