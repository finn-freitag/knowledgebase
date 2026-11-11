/* Shared helpers for the background script and extension pages. */
/* global browser */
(function (global) {
  "use strict";

  const DEFAULTS = {
    serverUrl: "",
    token: "",
    user: "",
    engines: { google: true, duckduckgo: true, ecosia: true, bing: true, startpage: true, brave: true, qwant: true },
    maxResults: 5,
    autoExpandMap: true,
    mapView: null,
  };

  async function getSettings() {
    const s = await browser.storage.local.get(DEFAULTS);
    s.engines = Object.assign({}, DEFAULTS.engines, s.engines || {});
    s.serverUrl = (s.serverUrl || "").replace(/\/+$/, "");
    return s;
  }

  async function saveSettings(patch) {
    await browser.storage.local.set(patch);
  }

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }

  /** Call the PKDB API. path starts with /pkdb/api/... */
  async function request(path, opts) {
    opts = opts || {};
    const s = opts.settings || (await getSettings());
    if (!s.serverUrl) throw new ApiError("Server URL not configured – open the PKDB settings.", 0);
    const headers = Object.assign({}, opts.headers || {});
    if (s.token && !opts.noAuth) headers["Authorization"] = "Bearer " + s.token;
    let body = opts.body;
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.json);
    }
    let res;
    try {
      res = await fetch(s.serverUrl + path, { method: opts.method || (body ? "POST" : "GET"), headers, body,
        credentials: "omit", signal: opts.signal });
    } catch (e) {
      throw new ApiError("Cannot reach the server (" + e.message + ")", 0);
    }
    if (opts.raw) {
      if (!res.ok) throw new ApiError("HTTP " + res.status, res.status);
      return res;
    }
    let data = null;
    const text = await res.text();
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { detail: text.slice(0, 300) }; }
    if (!res.ok) {
      let msg = (data && data.detail) || ("HTTP " + res.status);
      if (typeof msg !== "string") msg = JSON.stringify(msg);
      if (res.status === 401) msg = "Not logged in or token invalid – open the PKDB settings. (" + msg + ")";
      throw new ApiError(msg, res.status);
    }
    return data;
  }

  /** Fetch a protected image and return an object URL usable in this document. */
  async function imageObjectUrl(path) {
    const res = await request(path, { raw: true });
    return URL.createObjectURL(await res.blob());
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function fmtDate(iso) {
    if (!iso) return "";
    try { return new Date(iso).toLocaleString(); } catch (e) { return iso; }
  }

  global.PKDB = { DEFAULTS, getSettings, saveSettings, request, imageObjectUrl, esc, fmtDate, ApiError };
})(typeof window !== "undefined" ? window : self);
