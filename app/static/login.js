"use strict";
const form = document.getElementById("login-form");
const errEl = document.getElementById("login-error");

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
