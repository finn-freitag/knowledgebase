/* global browser, PKDB */
"use strict";
const $ = (id) => document.getElementById(id);
const ENGINES = { google: "Google", duckduckgo: "DuckDuckGo", ecosia: "Ecosia", bing: "Bing", startpage: "Startpage", brave: "Brave Search", qwant: "Qwant" };

function showMsg(text, kind) {
  const el = $("conn");
  el.textContent = text;
  el.className = "msg " + (kind || "info");
}

function originOf(url) {
  try {
    const u = new URL(url);
    if (!/^https?:$/.test(u.protocol)) return null;
    return u.origin;
  } catch (e) { return null; }
}

// Must be called synchronously inside a click handler (Firefox requires a user gesture)
function requestHostPermission(url) {
  const origin = originOf(url);
  if (!origin) return Promise.reject(new Error("Please enter a valid http(s) URL"));
  return browser.permissions.request({ origins: [origin + "/*"] }).then((granted) => {
    if (!granted) throw new Error("Permission to access " + origin + " was not granted");
    return origin;
  });
}

async function refresh() {
  const s = await PKDB.getSettings();
  $("server").value = s.serverUrl;
  const logged = !!s.token;
  $("loggedIn").classList.toggle("hidden", !logged);
  $("loggedOut").classList.toggle("hidden", logged);
  $("who").textContent = s.user || "API token";
  $("engines").innerHTML = Object.entries(ENGINES).map(([id, name]) =>
    '<label><input type="checkbox" data-engine="' + id + '"' + (s.engines[id] !== false ? " checked" : "") + "> " + name + "</label>").join("");
  $("max").value = s.maxResults;
  $("autoMap").checked = !!s.autoExpandMap;
}

$("login").onclick = () => {
  const url = $("server").value.trim().replace(/\/+$/, "");
  requestHostPermission(url).then(async (origin) => {
    showMsg("Logging in…");
    const settings = Object.assign(await PKDB.getSettings(), { serverUrl: url, token: "" });
    const res = await PKDB.request("/pkdb/api/auth/login", {
      settings, noAuth: true, json: { username: $("user").value.trim(), password: $("pass").value, device: "Firefox extension" },
    });
    await PKDB.saveSettings({ serverUrl: url, token: res.token, user: res.user });
    $("pass").value = "";
    showMsg("Logged in successfully.", "ok");
    browser.runtime.sendMessage({ type: "refreshBadge" });
    refresh();
  }).catch((e) => showMsg(e.message, "err"));
};

$("saveToken").onclick = () => {
  const url = $("server").value.trim().replace(/\/+$/, "");
  requestHostPermission(url).then(async () => {
    const token = $("token").value.trim();
    const settings = Object.assign(await PKDB.getSettings(), { serverUrl: url, token });
    await PKDB.request("/pkdb/api/auth/me", { settings });
    await PKDB.saveSettings({ serverUrl: url, token, user: "" });
    showMsg("Token saved and verified.", "ok");
    refresh();
  }).catch((e) => showMsg(e.message, "err"));
};

$("test").onclick = async () => {
  try {
    const me = await PKDB.request("/pkdb/api/auth/me");
    const w = me.wiki || {};
    showMsg("Connection OK. Wiki: " + w.state + (w.message ? " (" + w.message + ")" : "") +
      " · LLM: " + me.config.llm_model + " · default mode: " + me.config.apply_mode, w.state === "ready" ? "ok" : "info");
  } catch (e) { showMsg(e.message, "err"); }
};

$("logout").onclick = async () => {
  try { await PKDB.request("/pkdb/api/auth/logout", { method: "POST" }); } catch (e) { /* ignore */ }
  await PKDB.saveSettings({ token: "", user: "" });
  showMsg("Logged out.", "info");
  refresh();
};

$("server").addEventListener("change", async () => {
  const url = $("server").value.trim().replace(/\/+$/, "");
  const s = await PKDB.getSettings();
  if (url !== s.serverUrl && s.token) {
    await PKDB.saveSettings({ serverUrl: url, token: "", user: "" });
    showMsg("Server changed – please log in again.", "info");
    refresh();
  }
});

$("saveSearch").onclick = async () => {
  const engines = {};
  document.querySelectorAll("[data-engine]").forEach((cb) => { engines[cb.getAttribute("data-engine")] = cb.checked; });
  await PKDB.saveSettings({ engines, maxResults: Math.max(1, Math.min(20, Number($("max").value) || 5)), autoExpandMap: $("autoMap").checked });
  browser.runtime.sendMessage({ type: "clearCache" });
  $("saved").classList.remove("hidden");
  setTimeout(() => $("saved").classList.add("hidden"), 1500);
};

refresh();
