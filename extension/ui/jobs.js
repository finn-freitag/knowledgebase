/* Review queue: list submissions, show proposed wiki changes as diffs, edit/accept/reject them. */
/* global browser, Diff, L, PKDB, PKDBMap */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = PKDB.esc;
const BUSY = new Set(["queued", "processing", "applying", "receiving"]);
const STATUS_TEXT = {
  receiving: "uploading", queued: "queued", processing: "LLM working", review: "waiting for review",
  applying: "saving", applied: "applied", rejected: "rejected", failed: "failed",
};

let selected = Number(location.hash.slice(1)) || null;
let current = null;      // submission detail
let edits = {};          // op id -> {accepted, path, title, description, tags, new_content, tab}
let pollTimer = null;
let jobMap = null;
let settings = null;

async function loadList() {
  try {
    const f = $("filter").value;
    const data = await PKDB.request("/pkdb/api/submissions?limit=200" + (f ? "&status=" + encodeURIComponent(f) : ""));
    $("list").innerHTML = data.items.length ? "" : '<p class="muted">Nothing here.</p>';
    for (const it of data.items) {
      const el = document.createElement("div");
      el.className = "job" + (it.id === selected ? " sel" : "");
      el.innerHTML = '<div class="t">#' + it.id + " " + esc(it.title || it.text.slice(0, 80)) + "</div>" +
        '<div class="d"><span class="badge ' + esc(it.status) + '">' + esc(STATUS_TEXT[it.status] || it.status) + "</span> " +
        esc(PKDB.fmtDate(it.created_at)) + "</div>";
      el.onclick = () => select(it.id);
      $("list").appendChild(el);
    }
    return data;
  } catch (e) {
    $("list").innerHTML = '<div class="msg err">' + esc(e.message) + '</div><a href="options.html">Settings</a>';
    return null;
  }
}

async function select(id) {
  selected = id;
  history.replaceState(null, "", "#" + id);
  edits = {};
  document.querySelectorAll(".job").forEach((el) => el.classList.toggle("sel", el.textContent.startsWith("#" + id + " ")));
  await loadDetail();
}

async function loadDetail() {
  clearTimeout(pollTimer);
  if (!selected) return;
  try {
    current = await PKDB.request("/pkdb/api/submissions/" + selected);
  } catch (e) {
    $("detail").innerHTML = '<div class="msg err">' + esc(e.message) + "</div>";
    return;
  }
  renderDetail();
  if (BUSY.has(current.status)) {
    pollTimer = setTimeout(async () => { await loadDetail(); loadList(); }, 3000);
  }
}

function diffHtml(oldText, newText) {
  const parts = Diff.diffLines(oldText || "", newText || "");
  let html = "";
  for (const p of parts) {
    const lines = p.value.replace(/\n$/, "").split("\n");
    if (p.added) html += lines.map((l) => '<div class="add">+ ' + esc(l) + "</div>").join("");
    else if (p.removed) html += lines.map((l) => '<div class="del">- ' + esc(l) + "</div>").join("");
    else if (lines.length > 8) {
      html += lines.slice(0, 3).map((l) => '<div class="ctx">  ' + esc(l) + "</div>").join("");
      html += '<div class="skip">… ' + (lines.length - 6) + " unchanged lines …</div>";
      html += lines.slice(-3).map((l) => '<div class="ctx">  ' + esc(l) + "</div>").join("");
    } else html += lines.map((l) => '<div class="ctx">  ' + esc(l) + "</div>").join("");
  }
  return html || '<div class="ctx">(no changes)</div>';
}

function opState(op) {
  if (!edits[op.id]) {
    edits[op.id] = {
      accepted: true, path: op.path, title: op.title, description: op.description,
      tags: (op.tags || []).join(", "), new_content: op.new_content, tab: "diff",
    };
  }
  return edits[op.id];
}

function renderDetail() {
  if (jobMap) { jobMap.remove(); jobMap = null; }
  const s = current;
  const r = s.result || {};
  let html = '<div class="row"><h2 class="grow" style="margin:0">#' + s.id + " " + esc(s.title || "Untitled") + "</h2>" +
    '<span class="badge ' + esc(s.status) + '">' + esc(STATUS_TEXT[s.status] || s.status) + "</span></div>" +
    '<div class="muted" style="font-size:12.5px;margin:4px 0 8px">' + esc(PKDB.fmtDate(s.created_at)) +
    (s.source_url ? ' · <a href="' + esc(s.source_url) + '" target="_blank" rel="noopener">source</a>' : "") +
    " · mode: " + esc(s.apply_mode) + "</div>";
  html += '<details><summary class="muted">Submitted text</summary><div class="src">' + esc(s.text) + "</div></details>";
  if (s.map_id) html += '<details id="mapDetails"><summary class="muted">Attached map</summary><div id="jobmap"></div></details>';
  if (s.error) html += '<div class="msg err">' + esc(s.error) + "</div>";
  if (BUSY.has(s.status)) html += '<div class="msg info"><span class="spinner"></span> ' + esc(STATUS_TEXT[s.status]) + "… this page updates automatically.</div>";
  if (r.summary) html += '<div class="msg info"><b>LLM summary:</b> ' + esc(r.summary) + "</div>";
  for (const w of r.warnings || []) html += '<div class="msg err">⚠ ' + esc(w) + "</div>";

  if (r.applied) {
    html += "<h3>Saved pages</h3><ul>" + r.applied.map((a) =>
      "<li>" + (a.ok ? "✓ " + esc(a.action) + ' <a href="' + esc(settings.serverUrl + "/" + a.path) + '" target="_blank">' + esc(a.title) + "</a> <span class=muted>/" + esc(a.path) + "</span>"
        : '✗ <span style="color:var(--danger)">' + esc(a.path) + ": " + esc(a.error) + "</span>") + "</li>").join("") + "</ul>";
  }

  const reviewable = (s.status === "review" || s.status === "failed") && r.operations;
  if (r.operations) {
    html += "<h3 style=\"margin-top:14px\">Proposed changes (" + r.operations.length + ")</h3>";
    for (const op of r.operations) {
      const st = opState(op);
      html += '<div class="op" data-op="' + op.id + '"><div class="op-head">' +
        (reviewable ? '<input type="checkbox" data-f="accepted"' + (st.accepted ? " checked" : "") + ' title="Apply this change">' : "") +
        '<span class="action ' + esc(op.action) + '">' + esc(op.action === "append" ? "append to" : op.action) + "</span>" +
        "<b>" + esc(st.title) + '</b><span class="muted">/' + esc(st.path) + "</span></div>" +
        '<div class="op-body">';
      if (reviewable) {
        html += '<div class="op-fields">' +
          '<div><label>Path</label><input type="text" data-f="path" value="' + esc(st.path) + '"' + (op.page_id ? " disabled title=\"Existing page\"" : "") + "></div>" +
          '<div><label>Title</label><input type="text" data-f="title" value="' + esc(st.title) + '"></div>' +
          '<div><label>Description</label><input type="text" data-f="description" value="' + esc(st.description) + '"></div>' +
          '<div><label>Tags <span class="hint">(comma separated)</span></label><input type="text" data-f="tags" value="' + esc(st.tags) + '"></div>' +
          "</div>";
      }
      html += '<div class="tabs"><button class="' + (st.tab === "diff" ? "on" : "") + '" data-tab="diff">Changes</button>' +
        (reviewable ? '<button class="' + (st.tab === "edit" ? "on" : "") + '" data-tab="edit">Edit text</button>' : "") +
        '<button class="' + (st.tab === "full" ? "on" : "") + '" data-tab="full">Full new text</button></div>';
      if (st.tab === "edit" && reviewable) html += '<textarea class="content" data-f="new_content">' + esc(st.new_content) + "</textarea>";
      else if (st.tab === "full") html += '<div class="diff">' + st.new_content.split("\n").map((l) => "<div>" + esc(l) + "</div>").join("") + "</div>";
      else html += '<div class="diff">' + diffHtml(op.old_content, st.new_content) + "</div>";
      html += "</div></div>";
    }
  }

  if (!BUSY.has(s.status)) {
    html += '<div class="bar">';
    if (reviewable) {
      html += '<div class="row"><button class="primary" id="apply">✓ Apply selected changes</button>' +
        '<button class="danger" id="reject">Reject</button><span class="grow"></span><button id="del" class="small danger">Delete submission</button></div>';
    } else {
      html += '<div class="row"><span class="grow"></span><button id="del" class="small danger">Delete submission</button></div>';
    }
    html += '<details style="margin-top:8px"' + (s.status === "failed" && !r.operations ? " open" : "") + '><summary class="muted">Let the LLM try again</summary>' +
      '<label for="feedback">Feedback / instructions for the next attempt <span class="hint">(optional)</span></label>' +
      '<textarea id="feedback" rows="3" placeholder="e.g. Create a separate page for the nature reserve and keep the council page short.">' + esc(s.feedback || "") + "</textarea>" +
      '<div class="row" style="margin-top:6px"><button id="retry">↻ Run again</button></div></details>';
    html += '<div id="actMsg"></div></div>';
  }
  $("detail").innerHTML = html;
  bindDetail();
}

function bindDetail() {
  document.querySelectorAll(".op").forEach((el) => {
    const id = Number(el.getAttribute("data-op"));
    const st = edits[id];
    el.querySelectorAll("[data-f]").forEach((inp) => {
      const f = inp.getAttribute("data-f");
      const ev = inp.type === "checkbox" ? "change" : "input";
      inp.addEventListener(ev, () => { st[f] = inp.type === "checkbox" ? inp.checked : inp.value; });
    });
    el.querySelectorAll("[data-tab]").forEach((b) => {
      b.onclick = () => { st.tab = b.getAttribute("data-tab"); renderDetail(); };
    });
  });
  const md = $("mapDetails");
  if (md) {
    md.addEventListener("toggle", async () => {
      if (!md.open || jobMap) return;
      try {
        const data = await PKDB.request("/pkdb/api/maps/" + current.map_id);
        jobMap = await PKDBMap.render($("jobmap"), data, { imageUrl: (l) => PKDB.imageObjectUrl(l.image.url) });
      } catch (e) { $("jobmap").textContent = e.message; }
    });
    md.addEventListener("toggle", () => { if (!md.open && jobMap) { jobMap.remove(); jobMap = null; } });
  }
  const act = (id, fn) => { const b = $(id); if (b) b.onclick = fn; };
  act("apply", async () => {
    const ops = current.result.operations.map((op) => {
      const st = edits[op.id];
      return { id: op.id, accepted: !!st.accepted, path: st.path, title: st.title, description: st.description,
        tags: st.tags.split(",").map((t) => t.trim()).filter(Boolean), new_content: st.new_content };
    });
    if (!ops.some((o) => o.accepted)) return showAct("Select at least one change.", "err");
    await run("apply", () => PKDB.request("/pkdb/api/submissions/" + current.id + "/apply", { json: { operations: ops } }));
  });
  act("reject", () => run("reject", () => PKDB.request("/pkdb/api/submissions/" + current.id + "/reject", { method: "POST" })));
  act("retry", () => run("retry", () => PKDB.request("/pkdb/api/submissions/" + current.id + "/retry", { json: { feedback: $("feedback").value } })));
  act("del", async () => {
    if (!confirm("Delete this submission? Pages already saved in the wiki stay unchanged.")) return;
    await run("del", () => PKDB.request("/pkdb/api/submissions/" + current.id, { method: "DELETE" }));
    selected = null;
    $("detail").innerHTML = '<p class="muted">Deleted.</p>';
  });
}

function showAct(text, kind) {
  const el = $("actMsg");
  if (el) el.innerHTML = '<div class="msg ' + kind + '">' + esc(text) + "</div>";
}

async function run(name, fn) {
  document.querySelectorAll(".bar button").forEach((b) => { b.disabled = true; });
  try {
    await fn();
    edits = {};
    if (name !== "del") await loadDetail();
    await loadList();
    browser.runtime.sendMessage({ type: "refreshBadge" });
  } catch (e) {
    showAct(e.message, "err");
    document.querySelectorAll(".bar button").forEach((b) => { b.disabled = false; });
  }
}

$("filter").onchange = loadList;
$("reload").onclick = () => { loadList(); loadDetail(); };

(async () => {
  settings = await PKDB.getSettings();
  const data = await loadList();
  if (!selected && data && data.items.length) {
    const firstReview = data.items.find((i) => i.status === "review");
    if (firstReview) selected = firstReview.id;
  }
  if (selected) select(selected);
  setInterval(loadList, 20000);
})();
