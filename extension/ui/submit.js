/* "Add information" page: text + images (with georeferencing) + geodata files + drawn shapes. */
/* global browser, L, PKDB, PKDBMap */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = PKDB.esc;

const IMAGE_EXT = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp"]);
const GEO_EXT = new Set([".geojson", ".json", ".gpx", ".kml", ".kmz", ".gpkg", ".shp", ".shx", ".dbf", ".prj", ".cpg",
  ".qix", ".sbn", ".sbx", ".zip", ".fgb", ".gml", ".topojson", ".osm", ".pbf", ".csv", ".tab", ".mif", ".mid", ".dxf",
  ".sqlite", ".ods", ".xlsx"]);
const SHP_PARTS = new Set([".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx"]);

const state = {
  geoFiles: [],          // File[]
  geoMeta: {},           // key -> {title, description}
  geoPreview: {},        // key -> {sig, layer, info, error, loading}
  images: [],            // {id, file, url, title, description, corners, overlay, width, height}
  shapes: new Map(),     // leaflet id -> {layer, name, description}
  nextImageId: 1,
};

// ------------------------------------------------------------------------------------------
// Helpers
// ------------------------------------------------------------------------------------------
function ext(name) {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}
function stem(name) {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(0, i) : name;
}
function showMsg(el, text, kind) {
  el.innerHTML = text ? '<div class="msg ' + (kind || "info") + '">' + text + "</div>" : "";
}

/** Group geodata files exactly like the backend: shapefile parts by stem, everything else by file name. */
function groupGeoFiles() {
  const stemsWithShp = new Set(state.geoFiles.filter((f) => ext(f.name) === ".shp").map((f) => stem(f.name).toLowerCase()));
  const groups = new Map();
  for (const f of state.geoFiles) {
    const e = ext(f.name);
    const key = SHP_PARTS.has(e) && stemsWithShp.has(stem(f.name).toLowerCase()) ? stem(f.name) + ".shp" : f.name;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(f);
  }
  return groups;
}

// ------------------------------------------------------------------------------------------
// Main map with drawing tools
// ------------------------------------------------------------------------------------------
const map = L.map("map");
const bases = PKDBMap.baseLayers();
bases["OpenStreetMap"].addTo(map);
const previewGroup = L.featureGroup().addTo(map);
const imageGroup = L.layerGroup().addTo(map);
const drawnGroup = L.featureGroup().addTo(map);
L.control.layers(bases, { "Geodata files": previewGroup, "Georeferenced images": imageGroup, "Drawn shapes": drawnGroup },
  { collapsed: true }).addTo(map);
L.control.scale({ imperial: false }).addTo(map);

map.pm.addControls({
  position: "topleft",
  drawCircle: false, drawCircleMarker: false, drawText: false,
  cutPolygon: false, rotateMode: false,
});
map.pm.setGlobalOptions({ layerGroup: drawnGroup, pathOptions: { color: "#c62828", weight: 3 } });

(async () => {
  const s = await PKDB.getSettings();
  if (s.mapView && s.mapView.center) map.setView(s.mapView.center, s.mapView.zoom);
  else map.setView([51.16, 10.45], 6);
})();
map.on("moveend", () => {
  PKDB.saveSettings({ mapView: { center: map.getCenter(), zoom: map.getZoom() } });
});

map.on("pm:create", (e) => {
  const layer = e.layer;
  drawnGroup.addLayer(layer);
  state.shapes.set(L.stamp(layer), { layer, shape: e.shape, name: "", description: "" });
  layer.on("pm:remove", () => { state.shapes.delete(L.stamp(layer)); renderShapes(); });
  renderShapes();
});
map.on("pm:remove", (e) => {
  state.shapes.delete(L.stamp(e.layer));
  renderShapes();
});

function shapeIcon(shape) {
  return { Marker: "📍", Line: "〰", Polygon: "⬠", Rectangle: "▭" }[shape] || "◆";
}

function renderShapes() {
  const box = $("shapes");
  $("shapesBox").classList.toggle("hidden", state.shapes.size === 0);
  box.innerHTML = "";
  for (const [id, s] of state.shapes) {
    const row = document.createElement("div");
    row.className = "shape";
    row.innerHTML = '<span class="t">' + shapeIcon(s.shape) + "</span>" +
      '<input type="text" placeholder="Name" value="' + esc(s.name) + '">' +
      '<input type="text" placeholder="Description" value="' + esc(s.description) + '">' +
      '<button class="small" title="Zoom to">⌖</button><button class="small danger" title="Delete">✕</button>';
    const [name, desc] = row.querySelectorAll("input");
    name.oninput = () => {
      s.name = name.value;
      if (s.name) s.layer.bindTooltip(s.name); else s.layer.unbindTooltip();
    };
    desc.oninput = () => { s.description = desc.value; };
    const [zoom, del] = row.querySelectorAll("button");
    zoom.onclick = () => {
      if (s.layer.getBounds) map.fitBounds(s.layer.getBounds().pad(0.3), { maxZoom: 18 });
      else map.setView(s.layer.getLatLng(), Math.max(map.getZoom(), 16));
    };
    del.onclick = () => {
      drawnGroup.removeLayer(s.layer);
      state.shapes.delete(id);
      renderShapes();
    };
    box.appendChild(row);
  }
}

function drawnFeatureCollection() {
  const features = [];
  for (const s of state.shapes.values()) {
    const f = s.layer.toGeoJSON(15);
    f.properties = {};
    if (s.name.trim()) f.properties.name = s.name.trim();
    if (s.description.trim()) f.properties.description = s.description.trim();
    features.push(f);
  }
  return { type: "FeatureCollection", features };
}

$("placeForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("place").value.trim();
  if (!q) return;
  try {
    const r = await fetch("https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" + encodeURIComponent(q),
      { headers: { "Accept-Language": navigator.language } });
    const res = await r.json();
    if (!res.length) return showMsg($("fileMsg"), "Place not found: " + esc(q), "err");
    const b = res[0].boundingbox.map(Number);
    map.fitBounds([[b[0], b[2]], [b[1], b[3]]], { maxZoom: 17 });
  } catch (e) {
    showMsg($("fileMsg"), "Place search failed: " + esc(e.message), "err");
  }
});

// ------------------------------------------------------------------------------------------
// File handling
// ------------------------------------------------------------------------------------------
function addFiles(files) {
  const rejected = [];
  for (const f of files) {
    const e = ext(f.name);
    if (IMAGE_EXT.has(e) || (f.type.startsWith("image/") && !f.type.includes("svg"))) {
      addImage(f);
    } else if (GEO_EXT.has(e)) {
      if (!state.geoFiles.some((g) => g.name === f.name && g.size === f.size)) state.geoFiles.push(f);
    } else {
      rejected.push(f.name);
    }
  }
  showMsg($("fileMsg"), rejected.length ? "Unsupported file type: " + rejected.map(esc).join(", ") : "", "err");
  renderGeo();
  renderImages();
}

function addImage(file) {
  const img = { id: state.nextImageId++, file, url: URL.createObjectURL(file), title: stem(file.name), description: "", corners: null };
  const probe = new Image();
  probe.onload = () => { img.width = probe.naturalWidth; img.height = probe.naturalHeight; renderImages(); };
  probe.onerror = () => { img.noPreview = true; renderImages(); };
  probe.src = img.url;
  state.images.push(img);
}

const drop = $("drop");
$("pick").onclick = () => $("files").click();
$("files").onchange = () => { addFiles([...$("files").files]); $("files").value = ""; };
["dragenter", "dragover"].forEach((t) => document.addEventListener(t, (e) => {
  if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) {
    e.preventDefault();
    document.body.classList.add("dragging");
    if (drop.contains(e.target)) drop.classList.add("over");
  }
}));
["dragleave", "drop"].forEach((t) => document.addEventListener(t, (e) => {
  if (t === "drop" || !e.relatedTarget) {
    document.body.classList.remove("dragging");
    drop.classList.remove("over");
  }
}));
document.addEventListener("drop", (e) => {
  if (!e.dataTransfer || !e.dataTransfer.files.length) return;
  e.preventDefault();
  addFiles([...e.dataTransfer.files]);
});
document.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData ? e.clipboardData.files : [])];
  if (files.length) { e.preventDefault(); addFiles(files); }
});

// ---------- Geodata cards + preview ----------
function renderGeo() {
  const groups = groupGeoFiles();
  const list = $("geoList");
  list.innerHTML = "";
  for (const key of Object.keys(state.geoPreview)) {
    if (!groups.has(key)) {
      if (state.geoPreview[key].layer) previewGroup.removeLayer(state.geoPreview[key].layer);
      delete state.geoPreview[key];
    }
  }
  for (const [key, files] of groups) {
    const meta = state.geoMeta[key] || (state.geoMeta[key] = { title: stem(key), description: "" });
    const pv = state.geoPreview[key] || {};
    const sig = files.map((f) => f.name + f.size).sort().join("|");
    const card = document.createElement("div");
    card.className = "card";
    let info = '<span class="spinner"></span> reading…';
    if (pv.error) info = esc(pv.error);
    else if (pv.info) info = esc(pv.info);
    const isShpIncomplete = key.endsWith(".shp") && !files.some((f) => ext(f.name) === ".dbf");
    card.innerHTML = '<div class="icon">🗺</div><div>' +
      '<input type="text" placeholder="Title" value="' + esc(meta.title) + '">' +
      '<textarea placeholder="Description (optional)">' + esc(meta.description) + "</textarea>" +
      '<div class="meta">' + files.map((f) => esc(f.name)).join(", ") + "</div>" +
      '<div class="meta' + (pv.error ? " err" : "") + '">' + info + "</div>" +
      (isShpIncomplete ? '<div class="meta err">Shapefile: add the .dbf, .shx and .prj files too (or drop a .zip).</div>' : "") +
      '</div><div class="side"><button class="small" data-act="zoom" title="Zoom to">⌖</button>' +
      '<button class="small danger" data-act="del" title="Remove">✕</button></div>';
    const [title] = card.querySelectorAll("input");
    const desc = card.querySelector("textarea");
    title.oninput = () => { meta.title = title.value; };
    desc.oninput = () => { meta.description = desc.value; };
    card.querySelector('[data-act="del"]').onclick = () => {
      state.geoFiles = state.geoFiles.filter((f) => !files.includes(f));
      delete state.geoMeta[key];
      renderGeo();
    };
    card.querySelector('[data-act="zoom"]').onclick = () => {
      if (pv.layer && pv.layer.getBounds().isValid()) map.fitBounds(pv.layer.getBounds().pad(0.1));
    };
    list.appendChild(card);
    if (pv.sig !== sig && !pv.loading) previewGroupFiles(key, files, sig);
  }
}

async function previewGroupFiles(key, files, sig) {
  const old = state.geoPreview[key];
  if (old && old.layer) previewGroup.removeLayer(old.layer);
  state.geoPreview[key] = { sig, loading: true };
  const fd = new FormData();
  for (const f of files) fd.append("files", f, f.name);
  try {
    const res = await PKDB.request("/pkdb/api/geodata/preview", { body: fd });
    const ds = res.datasets.find((d) => d.key === key) || res.datasets[0];
    if (state.geoPreview[key] && state.geoPreview[key].sig !== sig) return;
    if (!ds || ds.error) {
      state.geoPreview[key] = { sig, error: (ds && ds.error) || "No data" };
    } else {
      const layer = L.featureGroup();
      let n = 0;
      ds.layers.forEach((lyr, i) => {
        n += lyr.count;
        const color = PKDBMap.PALETTE[(Object.keys(state.geoPreview).length + i) % PKDBMap.PALETTE.length];
        L.geoJSON(lyr.geojson, {
          pmIgnore: true,
          style: { color, weight: 3, fillOpacity: 0.2 },
          pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 5, color: "#fff", weight: 2, fillColor: color, fillOpacity: 0.9, pmIgnore: true }),
          onEachFeature: (f, l) => l.bindPopup(PKDBMap.popupHtml(f, lyr.name)),
        }).addTo(layer);
      });
      layer.addTo(previewGroup);
      const names = ds.layers.map((l) => l.name + " (" + l.count + (l.truncated ? "+, truncated" : "") + ")").join(", ");
      state.geoPreview[key] = { sig, layer, info: n + " features · " + names };
      if (layer.getBounds().isValid()) map.fitBounds(previewGroup.getBounds().pad(0.1), { maxZoom: 17 });
    }
  } catch (e) {
    state.geoPreview[key] = { sig, error: e.message };
  }
  renderGeo();
}

// ---------- Image cards ----------
function renderImages() {
  const list = $("imgList");
  list.innerHTML = "";
  for (const img of state.images) {
    const card = document.createElement("div");
    card.className = "card";
    const canGeoref = !img.noPreview;
    card.innerHTML = (img.noPreview ? '<div class="icon">🖼</div>' : '<img class="thumb" alt="" src="' + esc(img.url) + '">') +
      '<div><input type="text" placeholder="Title" value="' + esc(img.title) + '">' +
      '<textarea placeholder="Description (optional)">' + esc(img.description) + "</textarea>" +
      '<div class="meta">' + esc(img.file.name) + (img.width ? " · " + img.width + "×" + img.height + " px" : "") +
      (img.noPreview ? " · preview not possible in the browser (will be converted on the server)" : "") + "</div></div>" +
      '<div class="side">' +
      (canGeoref ? '<button class="small" data-act="georef">📍 ' + (img.corners ? "Edit georeference" : "Georeference") + "</button>" : "") +
      (img.corners ? '<span class="georef-ok">✓ on map</span>' : "") +
      '<button class="small danger" data-act="del">✕ Remove</button></div>';
    const title = card.querySelector("input");
    const desc = card.querySelector("textarea");
    title.oninput = () => { img.title = title.value; };
    desc.oninput = () => { img.description = desc.value; };
    card.querySelector('[data-act="del"]').onclick = () => {
      if (img.overlay) imageGroup.removeLayer(img.overlay);
      URL.revokeObjectURL(img.url);
      state.images = state.images.filter((i) => i !== img);
      renderImages();
    };
    const gb = card.querySelector('[data-act="georef"]');
    if (gb) gb.onclick = () => openGeoref(img);
    list.appendChild(card);
  }
}

function updateImageOverlay(img) {
  if (img.overlay) { imageGroup.removeLayer(img.overlay); img.overlay = null; }
  if (img.corners) {
    img.overlay = new PKDBMap.ProjectiveImageOverlay(img.url, img.corners, { opacity: 0.8 });
    imageGroup.addLayer(img.overlay);
  }
}

// ------------------------------------------------------------------------------------------
// Georeferencing tool (4 draggable corners + move handle)
// ------------------------------------------------------------------------------------------
let grMap = null;
let grState = null;

function handleIcon(cls, size) {
  return L.divIcon({ className: cls, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
}

function openGeoref(img) {
  $("georef").classList.remove("hidden");
  $("georefTitle").textContent = "Georeference: " + (img.title || img.file.name);
  if (!grMap) {
    grMap = L.map("georefMap");
    const b = PKDBMap.baseLayers();
    b["OpenStreetMap"].addTo(grMap);
    L.control.layers(b, {}, { collapsed: false }).addTo(grMap);
    L.control.scale({ imperial: false }).addTo(grMap);
  }
  grMap.invalidateSize();
  // Show the other attachments for orientation
  if (grState && grState.context) grMap.removeLayer(grState.context);
  const context = L.featureGroup();
  previewGroup.eachLayer((l) => { if (l.toGeoJSON) L.geoJSON(l.toGeoJSON(), { style: { color: "#c62828", weight: 2, fillOpacity: 0.05 }, pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 4 }) }).addTo(context); });
  drawnGroup.eachLayer((l) => { if (l.toGeoJSON) L.geoJSON(l.toGeoJSON(), { style: { color: "#6a1b9a", weight: 2, fillOpacity: 0.05 } }).addTo(context); });
  context.addTo(grMap);

  if (img.corners) grMap.fitBounds(L.latLngBounds(img.corners).pad(0.4));
  else grMap.setView(map.getCenter(), map.getZoom());

  grState = { img, context, overlay: null, handles: [], mover: null };
  setTimeout(() => {
    grMap.invalidateSize();
    placeOverlay(img.corners || initialCorners(img));
  }, 50);
}

function initialCorners(img) {
  const size = grMap.getSize();
  const ratio = (img.height || 3) / (img.width || 4);
  let w = size.x * 0.6;
  let h = w * ratio;
  if (h > size.y * 0.7) { h = size.y * 0.7; w = h / ratio; }
  const cx = size.x / 2, cy = size.y / 2;
  const pts = [[cx - w / 2, cy - h / 2], [cx + w / 2, cy - h / 2], [cx + w / 2, cy + h / 2], [cx - w / 2, cy + h / 2]];
  return pts.map((p) => {
    const ll = grMap.containerPointToLatLng(p);
    return [ll.lat, ll.lng];
  });
}

function placeOverlay(corners) {
  const st = grState;
  if (st.overlay) grMap.removeLayer(st.overlay);
  st.handles.forEach((h) => grMap.removeLayer(h));
  if (st.mover) grMap.removeLayer(st.mover);
  st.overlay = new PKDBMap.ProjectiveImageOverlay(st.img.url, corners, { opacity: Number($("opacity").value) }).addTo(grMap);
  const labels = ["top left", "top right", "bottom right", "bottom left"];
  st.handles = corners.map((c, i) => {
    const m = L.marker(c, { draggable: true, icon: handleIcon("georef-handle", 16), title: "Image corner: " + labels[i], zIndexOffset: 1000 });
    m.on("drag", () => {
      const cs = st.overlay.getCorners();
      cs[i] = m.getLatLng();
      st.overlay.setCorners(cs);
      st.mover.setLatLng(center(cs));
    });
    return m.addTo(grMap);
  });
  st.mover = L.marker(center(corners.map((c) => L.latLng(c))), { draggable: true, icon: handleIcon("georef-move", 22), title: "Move image", zIndexOffset: 1100 }).addTo(grMap);
  let start = null;
  st.mover.on("dragstart", () => {
    start = { p: grMap.latLngToLayerPoint(st.mover.getLatLng()), corners: st.overlay.getCorners().map((c) => grMap.latLngToLayerPoint(c)) };
  });
  st.mover.on("drag", () => {
    const d = grMap.latLngToLayerPoint(st.mover.getLatLng()).subtract(start.p);
    const cs = start.corners.map((p) => grMap.layerPointToLatLng(p.add(d)));
    st.overlay.setCorners(cs);
    cs.forEach((c, i) => st.handles[i].setLatLng(c));
  });
}

function center(cs) {
  const lat = cs.reduce((a, c) => a + L.latLng(c).lat, 0) / cs.length;
  const lng = cs.reduce((a, c) => a + L.latLng(c).lng, 0) / cs.length;
  return L.latLng(lat, lng);
}

function closeGeoref() {
  $("georef").classList.add("hidden");
}

$("opacity").oninput = () => { if (grState && grState.overlay) grState.overlay.setOpacity(Number($("opacity").value)); };
$("grFit").onclick = () => placeOverlay(initialCorners(grState.img));
$("grCancel").onclick = closeGeoref;
$("grRemove").onclick = () => {
  grState.img.corners = null;
  updateImageOverlay(grState.img);
  renderImages();
  closeGeoref();
};
$("grSave").onclick = () => {
  const img = grState.img;
  img.corners = grState.overlay.getCorners().map((c) => [Number(c.lat.toFixed(8)), Number(c.lng.toFixed(8))]);
  updateImageOverlay(img);
  renderImages();
  closeGeoref();
  if (img.overlay) map.fitBounds(img.overlay.getBounds().pad(0.2));
};
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("georef").classList.contains("hidden")) closeGeoref(); });

// ------------------------------------------------------------------------------------------
// Submit
// ------------------------------------------------------------------------------------------
function resetForm() {
  $("title").value = "";
  $("text").value = "";
  $("source").value = "";
  $("mapTitle").value = "";
  $("drawnTitle").value = "";
  state.geoFiles = [];
  state.geoMeta = {};
  for (const k of Object.keys(state.geoPreview)) if (state.geoPreview[k].layer) previewGroup.removeLayer(state.geoPreview[k].layer);
  state.geoPreview = {};
  state.images.forEach((i) => URL.revokeObjectURL(i.url));
  state.images = [];
  imageGroup.clearLayers();
  drawnGroup.clearLayers();
  state.shapes.clear();
  renderGeo();
  renderImages();
  renderShapes();
}

$("reset").onclick = () => { if (confirm("Clear the form?")) resetForm(); };

$("submit").onclick = async () => {
  const text = $("text").value.trim();
  if (!text) {
    showMsg($("submitMsg"), "Please enter a description or text – it is required.", "err");
    $("text").focus();
    return;
  }
  const groups = groupGeoFiles();
  const loading = [...groups.keys()].filter((k) => state.geoPreview[k] && state.geoPreview[k].loading);
  if (loading.length) {
    showMsg($("submitMsg"), "Please wait until the geodata preview has finished.", "info");
    return;
  }
  const meta = {
    title: $("title").value.trim(),
    text,
    source_url: $("source").value.trim(),
    apply_mode: $("mode").value,
    map_title: $("mapTitle").value.trim() || undefined,
    drawn: state.shapes.size ? drawnFeatureCollection() : undefined,
    drawn_title: $("drawnTitle").value.trim() || undefined,
    geofiles: [...groups.keys()].map((key) => ({ key, title: (state.geoMeta[key] || {}).title || "", description: (state.geoMeta[key] || {}).description || "" })),
    images: state.images.map((img, i) => ({ field: "image_" + i, title: img.title, description: img.description, corners: img.corners })),
  };
  const fd = new FormData();
  fd.append("meta", JSON.stringify(meta));
  for (const f of state.geoFiles) fd.append("geofiles", f, f.name);
  state.images.forEach((img, i) => fd.append("image_" + i, img.file, img.file.name));

  $("submit").disabled = true;
  $("busy").classList.remove("hidden");
  showMsg($("submitMsg"), "");
  try {
    const res = await PKDB.request("/pkdb/api/submissions", { body: fd });
    resetForm();
    const mode = meta.apply_mode === "auto" ? "The wiki will be updated automatically." : "You will be able to review the changes.";
    showMsg($("submitMsg"), "✓ Sent (#" + res.id + "). The LLM is working on it. " + mode +
      ' <a href="jobs.html#' + res.id + '">Open in review queue</a>', "ok");
    browser.runtime.sendMessage({ type: "refreshBadge" });
  } catch (e) {
    showMsg($("submitMsg"), "Sending failed: " + esc(e.message), "err");
  } finally {
    $("submit").disabled = false;
    $("busy").classList.add("hidden");
  }
};

// ------------------------------------------------------------------------------------------
// Init: default apply mode, draft from context menu
// ------------------------------------------------------------------------------------------
(async () => {
  try {
    const cfg = await PKDB.request("/pkdb/api/config");
    $("mode").value = cfg.apply_mode === "auto" ? "auto" : "review";
  } catch (e) {
    showMsg($("submitMsg"), esc(e.message) + ' – <a href="options.html">open settings</a>', "err");
  }
  const { pendingDraft } = await browser.storage.local.get("pendingDraft");
  if (pendingDraft) {
    await browser.storage.local.remove("pendingDraft");
    if (pendingDraft.title) $("title").value = pendingDraft.title;
    if (pendingDraft.text) $("text").value = pendingDraft.text;
    if (pendingDraft.source_url) $("source").value = pendingDraft.source_url;
    if (pendingDraft.imageUrl) {
      try {
        const r = await fetch(pendingDraft.imageUrl);
        const blob = await r.blob();
        const name = (new URL(pendingDraft.imageUrl).pathname.split("/").pop() || "image").slice(0, 80);
        addFiles([new File([blob], /\.[a-z]{3,4}$/i.test(name) ? name : name + ".jpg", { type: blob.type })]);
      } catch (e) {
        showMsg($("fileMsg"), "Could not load the image from the page – please save it and drop it here.", "err");
      }
    }
  }
})();
