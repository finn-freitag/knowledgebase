/* Background script: search requests from content scripts, context menu, review badge. */
/* global browser, PKDB */
"use strict";

const cache = new Map(); // query -> {at, data}
const CACHE_MS = 60 * 1000;

async function doSearch(q, engineId) {
  const s = await PKDB.getSettings();
  if (!s.serverUrl || !s.token) return { ok: false, error: "not configured" };
  if (engineId && s.engines[engineId] === false) return { ok: false, error: "disabled" };
  const key = q.toLowerCase();
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < CACHE_MS) return { ok: true, data: hit.data };
  try {
    const data = await PKDB.request("/pkdb/api/search?q=" + encodeURIComponent(q) + "&limit=" + (s.maxResults || 5),
      { settings: s });
    // Build links with the configured server URL (may differ from PUBLIC_URL, e.g. LAN IP vs. domain)
    for (const p of data.pages || []) p.link = s.serverUrl + "/" + p.path;
    for (const m of data.maps || []) {
      for (const pg of m.pages || []) pg.link = s.serverUrl + "/" + pg.path;
      m.link = (m.pages && m.pages[0]) ? m.pages[0].link : s.serverUrl + "/pkdb/embed/map/" + m.id;
    }
    data.autoExpandMap = s.autoExpandMap;
    cache.set(key, { at: Date.now(), data });
    if (cache.size > 100) cache.delete(cache.keys().next().value);
    return { ok: true, data };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

browser.runtime.onMessage.addListener((msg, sender) => {
  if (!msg || typeof msg !== "object") return undefined;
  if (msg.type === "search" && typeof msg.q === "string") return doSearch(msg.q.slice(0, 500), msg.engine);
  if (msg.type === "refreshBadge") return refreshBadge();
  if (msg.type === "clearCache") { cache.clear(); return Promise.resolve(true); }
  return undefined;
});

// ---------- Context menu: send selection / page / image to the submit form ----------
function setupMenus() {
  browser.contextMenus.removeAll().then(() => {
    browser.contextMenus.create({ id: "pkdb-selection", title: "Add selection to knowledge base", contexts: ["selection"] });
    browser.contextMenus.create({ id: "pkdb-page", title: "Add this page to knowledge base", contexts: ["page"] });
    browser.contextMenus.create({ id: "pkdb-image", title: "Add image to knowledge base", contexts: ["image"] });
  });
}
browser.runtime.onInstalled.addListener(() => {
  setupMenus();
  browser.alarms.create("pkdb-badge", { periodInMinutes: 2 });
  browser.runtime.openOptionsPage().catch(() => {});
});
browser.runtime.onStartup.addListener(() => {
  setupMenus();
  browser.alarms.create("pkdb-badge", { periodInMinutes: 2 });
});

browser.contextMenus.onClicked.addListener(async (info, tab) => {
  const draft = { source_url: (tab && tab.url) || info.pageUrl || "", title: (tab && tab.title) || "" };
  if (info.menuItemId === "pkdb-selection") draft.text = info.selectionText || "";
  if (info.menuItemId === "pkdb-image") draft.imageUrl = info.srcUrl;
  await browser.storage.local.set({ pendingDraft: draft });
  await browser.tabs.create({ url: browser.runtime.getURL("ui/submit.html") });
});

// ---------- Badge with number of submissions waiting for review ----------
async function refreshBadge() {
  const s = await PKDB.getSettings();
  if (!s.serverUrl || !s.token) {
    browser.action.setBadgeText({ text: "" });
    return 0;
  }
  try {
    const data = await PKDB.request("/pkdb/api/submissions?limit=1", { settings: s });
    const n = (data.counts.review || 0);
    const busy = (data.counts.queued || 0) + (data.counts.processing || 0);
    browser.action.setBadgeBackgroundColor({ color: n ? "#ef6c00" : "#2f7d32" });
    browser.action.setBadgeText({ text: n ? String(n) : busy ? "…" : "" });
    return n;
  } catch (e) {
    browser.action.setBadgeText({ text: "!" });
    browser.action.setBadgeBackgroundColor({ color: "#c62828" });
    return 0;
  }
}
browser.alarms.onAlarm.addListener((a) => { if (a.name === "pkdb-badge") refreshBadge(); });
browser.storage.onChanged.addListener((changes) => {
  if (changes.serverUrl || changes.token) { cache.clear(); refreshBadge(); }
});
refreshBadge();
