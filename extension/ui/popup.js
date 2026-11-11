/* global browser, PKDB */
"use strict";
const $ = (id) => document.getElementById(id);
const open = (path) => browser.tabs.create({ url: browser.runtime.getURL(path) }).then(() => window.close());

$("new").onclick = () => open("ui/submit.html");
$("review").onclick = () => open("ui/jobs.html");
$("settings").onclick = () => browser.runtime.openOptionsPage().then(() => window.close());
$("page").onclick = async () => {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  await browser.storage.local.set({ pendingDraft: { source_url: tab ? tab.url : "", title: tab ? tab.title : "" } });
  open("ui/submit.html");
};
$("wiki").onclick = async () => {
  const s = await PKDB.getSettings();
  if (s.serverUrl) browser.tabs.create({ url: s.serverUrl + "/" }).then(() => window.close());
};

(async () => {
  const s = await PKDB.getSettings();
  if (!s.serverUrl || !s.token) {
    $("status").innerHTML = "Not connected. Open the <b>settings</b> to log in.";
    return;
  }
  try {
    const data = await PKDB.request("/pkdb/api/submissions?limit=1", { settings: s });
    const n = data.counts.review || 0;
    if (n) { $("count").textContent = n; $("count").classList.remove("hidden"); }
    const busy = (data.counts.queued || 0) + (data.counts.processing || 0);
    $("status").textContent = "Connected as " + (s.user || "token") + (busy ? " · " + busy + " being processed" : "");
  } catch (e) {
    $("status").textContent = e.message;
  }
})();
