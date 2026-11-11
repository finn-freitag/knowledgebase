/* Injects PKDB results at the top of search engine result pages. */
/* global browser, pkdbDetectEngine */
"use strict";

(function () {
  const engine = pkdbDetectEngine();
  if (!engine) return;

  let lastQuery = null;
  let host = null;
  let shadow = null;
  let collapsed = false;
  let currentData = null;
  let placementTimer = null;

  const CSS = `
    :host { all: initial; display: block; }
    * { box-sizing: border-box; }
    .box { font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--fg);
           background: var(--bg); border: 1px solid var(--border); border-left: 4px solid #2f7d32;
           border-radius: 10px; margin: 0 0 18px 0; max-width: 720px; overflow: hidden;
           --bg: #fbfdfb; --fg: #1f1f1f; --muted: #5f6368; --border: #d7e3d8; --link: #1a0dab; --mark: #fff59d; --chip: #e8f5e9; }
    @media (prefers-color-scheme: dark) {
      .box { --bg: #1f2420; --fg: #e8eaed; --muted: #9aa0a6; --border: #344236; --link: #8ab4f8; --mark: #5d5a1f; --chip: #2a3a2c; }
    }
    .box.floating { position: fixed; top: 80px; right: 16px; width: 420px; max-height: 75vh; overflow: auto; z-index: 2147483000;
                    box-shadow: 0 6px 24px rgba(0,0,0,.25); }
    .head { display: flex; align-items: center; gap: 8px; padding: 8px 12px; cursor: pointer; user-select: none; }
    .head img { width: 18px; height: 18px; }
    .head .t { font-weight: 600; }
    .head .n { color: var(--muted); font-size: 12px; }
    .head .sp { flex: 1; }
    .head button { all: unset; cursor: pointer; color: var(--muted); padding: 0 4px; font-size: 13px; }
    .head button:hover { color: var(--fg); }
    .list { padding: 0 12px 6px 12px; }
    .collapsed .list { display: none; }
    .item { padding: 8px 0; border-top: 1px solid var(--border); }
    .item:first-child { border-top: none; }
    .item a.title { color: var(--link); text-decoration: none; font-size: 17px; line-height: 1.3; }
    .item a.title:hover { text-decoration: underline; }
    .path { color: var(--muted); font-size: 12px; margin: 1px 0 3px 0; word-break: break-all; }
    .snippet { color: var(--fg); font-size: 13.5px; }
    .snippet mark { background: var(--mark); color: inherit; padding: 0 1px; border-radius: 2px; }
    .chips { margin-top: 4px; display: flex; flex-wrap: wrap; gap: 4px; }
    .chip { background: var(--chip); border-radius: 10px; padding: 1px 8px; font-size: 12px; color: var(--fg); }
    .actions { margin-top: 4px; font-size: 12.5px; display: flex; gap: 12px; flex-wrap: wrap; }
    .actions a, .actions button { all: unset; color: var(--link); cursor: pointer; }
    .actions a:hover, .actions button:hover { text-decoration: underline; }
    iframe.map { width: 100%; height: 260px; border: 1px solid var(--border); border-radius: 8px; margin-top: 6px; display: block; }
    .err { padding: 8px 12px; color: #c62828; font-size: 13px; }
  `;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Snippets come pre-escaped from the server with <mark> tags; strip anything else defensively.
  function safeSnippet(html) {
    const parts = String(html || "").split(/(<\/?mark>)/);
    return parts.map((p) => (p === "<mark>" || p === "</mark>" ? p : p.replace(/[<>]/g, ""))).join("");
  }

  function ensureHost() {
    if (host && host.isConnected) return true;
    if (!host) {
      host = document.createElement("div");
      host.id = "pkdb-results-host";
      shadow = host.attachShadow({ mode: "closed" });
    }
    for (const [sel, mode] of engine.targets) {
      const el = document.querySelector(sel);
      if (!el) continue;
      if (mode === "before" && el.parentNode) el.parentNode.insertBefore(host, el);
      else el.prepend(host);
      return true;
    }
    return false;
  }

  function place() {
    clearTimeout(placementTimer);
    if (ensureHost()) {
      render(false);
      return;
    }
    // Results container not there yet: retry a few times, then fall back to a floating box
    let tries = 0;
    const retry = () => {
      if (ensureHost()) return render(false);
      if (++tries > 20) {
        document.body.appendChild(host);
        return render(true);
      }
      placementTimer = setTimeout(retry, 250);
    };
    placementTimer = setTimeout(retry, 250);
  }

  function mapFrame(mapId, highlight) {
    const url = browser.runtime.getURL("ui/mapview.html") + "#map=" + encodeURIComponent(mapId) +
      (highlight && highlight.length ? "&hl=" + highlight.join(",") : "");
    return '<iframe class="map" loading="lazy" src="' + esc(url) + '"></iframe>';
  }

  function render(floating) {
    const d = currentData;
    if (!d || !shadow) return;
    const pages = d.pages || [];
    const maps = d.maps || [];
    const total = pages.length + maps.length;
    const icon = browser.runtime.getURL("icons/icon.svg");
    let html = '<style>' + CSS + '</style><div class="box' + (floating ? " floating" : "") + (collapsed ? " collapsed" : "") + '">';
    html += '<div class="head" data-act="toggle"><img src="' + esc(icon) + '" alt=""><span class="t">Knowledge base</span>' +
      '<span class="n">' + total + (total === 1 ? " result" : " results") + '</span><span class="sp"></span>' +
      '<button data-act="toggle" title="Collapse / expand">' + (collapsed ? "▸" : "▾") + '</button>' +
      '<button data-act="close" title="Hide for this search">✕</button></div><div class="list">';

    let mapShown = !d.autoExpandMap;
    for (const p of pages) {
      html += '<div class="item"><a class="title" href="' + esc(p.link) + '">' + esc(p.title) + '</a>' +
        '<div class="path">📄 ' + esc(p.path) + (p.description ? " — " + esc(p.description) : "") + '</div>' +
        '<div class="snippet">' + safeSnippet(p.snippet) + '</div>';
      if (p.maps && p.maps.length) {
        html += '<div class="actions">';
        for (const mid of p.maps) html += '<button data-act="map" data-map="' + Number(mid) + '">🗺 Show map</button>';
        html += '</div><div class="mapslot"></div>';
      }
      html += "</div>";
    }
    for (const m of maps) {
      const hl = (m.features || []).map((f) => f.id);
      html += '<div class="item"><a class="title" href="' + esc(m.link) + '">🗺 ' + esc(m.title) + '</a>' +
        '<div class="path">Map' + (m.description ? " — " + esc(m.description) : "") + '</div>';
      const names = (m.features || []).map((f) => f.name).filter(Boolean).slice(0, 8);
      if (names.length) html += '<div class="chips">' + names.map((n) => '<span class="chip">' + esc(n) + "</span>").join("") + "</div>";
      html += '<div class="actions">';
      for (const pg of m.pages || []) html += '<a href="' + esc(pg.link) + '">📄 ' + esc(pg.title) + "</a>";
      if (mapShown) html += '<button data-act="map" data-map="' + Number(m.id) + '" data-hl="' + hl.join(",") + '">Show map</button>';
      html += '</div><div class="mapslot">' + (!mapShown ? mapFrame(m.id, hl) : "") + "</div></div>";
      mapShown = true;
    }
    html += "</div></div>";
    shadow.innerHTML = html;

    shadow.querySelectorAll("[data-act]").forEach((el) => {
      el.addEventListener("click", (ev) => {
        const act = el.getAttribute("data-act");
        if (act === "toggle") {
          ev.stopPropagation();
          collapsed = !collapsed;
          render(floating);
        } else if (act === "close") {
          ev.stopPropagation();
          host.remove();
          currentData = null;
        } else if (act === "map") {
          ev.preventDefault();
          const slot = el.closest(".item").querySelector(".mapslot");
          const hl = (el.getAttribute("data-hl") || "").split(",").filter(Boolean);
          slot.innerHTML = slot.innerHTML ? "" : mapFrame(el.getAttribute("data-map"), hl);
        }
      });
    });
  }

  async function check() {
    if (!engine.active()) {
      if (host) host.remove();
      lastQuery = null;
      return;
    }
    const q = engine.query();
    if (!q || q === lastQuery) {
      if (currentData && host && !host.isConnected) place();
      return;
    }
    lastQuery = q;
    currentData = null;
    if (host) host.remove();
    let resp;
    try {
      resp = await browser.runtime.sendMessage({ type: "search", q, engine: engine.id });
    } catch (e) {
      return;
    }
    if (q !== lastQuery || !resp || !resp.ok) return;
    const d = resp.data;
    if (!(d.pages && d.pages.length) && !(d.maps && d.maps.length)) return;
    currentData = d;
    place();
  }

  check();
  // Single page apps (Google, DDG, ...) change the URL without reloading
  let lastHref = location.href;
  setInterval(() => {
    if (location.href !== lastHref) {
      lastHref = location.href;
      check();
    } else if (currentData && host && !host.isConnected) {
      place();
    }
  }, 700);
  window.addEventListener("popstate", () => setTimeout(check, 50));
})();
