/*
 * PKDB map rendering, shared by the wiki embed viewer (backend) and the browser extension.
 * Requires Leaflet 1.9 (global L).
 */
(function (global) {
  "use strict";

  // ---------------------------------------------------------------------------------------
  // Projective transform: maps the image rectangle onto 4 arbitrary corner points (CSS matrix3d)
  // ---------------------------------------------------------------------------------------
  function adj(m) {
    return [
      m[4] * m[8] - m[5] * m[7], m[2] * m[7] - m[1] * m[8], m[1] * m[5] - m[2] * m[4],
      m[5] * m[6] - m[3] * m[8], m[0] * m[8] - m[2] * m[6], m[2] * m[3] - m[0] * m[5],
      m[3] * m[7] - m[4] * m[6], m[1] * m[6] - m[0] * m[7], m[0] * m[4] - m[1] * m[3],
    ];
  }
  function multmm(a, b) {
    const c = new Array(9);
    for (let i = 0; i < 3; i++) {
      for (let j = 0; j < 3; j++) {
        let s = 0;
        for (let k = 0; k < 3; k++) s += a[3 * i + k] * b[3 * k + j];
        c[3 * i + j] = s;
      }
    }
    return c;
  }
  function multmv(m, v) {
    return [
      m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
      m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
      m[6] * v[0] + m[7] * v[1] + m[8] * v[2],
    ];
  }
  function basisToPoints(p) {
    const m = [p[0][0], p[1][0], p[2][0], p[0][1], p[1][1], p[2][1], 1, 1, 1];
    const v = multmv(adj(m), [p[3][0], p[3][1], 1]);
    return multmm(m, [v[0], 0, 0, 0, v[1], 0, 0, 0, v[2]]);
  }
  /** src/dst: 4 points [x,y] in order TL, TR, BR, BL. Returns CSS matrix3d string. */
  function projectiveMatrix(src, dst) {
    const s = basisToPoints(src);
    const d = basisToPoints(dst);
    const t = multmm(d, adj(s));
    for (let i = 0; i < 9; i++) t[i] = t[i] / t[8];
    const m = [t[0], t[3], 0, t[6], t[1], t[4], 0, t[7], 0, 0, 1, 0, t[2], t[5], 0, t[8]];
    return "matrix3d(" + m.map((x) => x.toFixed(10)).join(",") + ")";
  }

  // ---------------------------------------------------------------------------------------
  // Leaflet layer: image placed by its 4 corners [[lat,lng] TL, TR, BR, BL]
  // ---------------------------------------------------------------------------------------
  const ProjectiveImageOverlay = L.Layer.extend({
    options: { opacity: 1, interactive: false, className: "" },

    initialize: function (url, corners, options) {
      this._url = url;
      this._corners = corners.map((c) => L.latLng(c));
      L.setOptions(this, options);
    },

    onAdd: function (map) {
      const img = (this._img = L.DomUtil.create("img", "leaflet-image-layer leaflet-zoom-hide " + this.options.className));
      img.style.position = "absolute";
      img.style.left = "0";
      img.style.top = "0";
      img.style.maxWidth = "none";
      img.style.maxHeight = "none";
      img.style.transformOrigin = "0 0";
      img.style.opacity = this.options.opacity;
      img.style.pointerEvents = "none";
      img.alt = this.options.alt || "";
      img.onload = () => this._update();
      img.src = this._url;
      this.getPane().appendChild(img);
      map.on("zoomend viewreset resize", this._update, this);
      if (img.complete) this._update();
    },

    onRemove: function (map) {
      map.off("zoomend viewreset resize", this._update, this);
      L.DomUtil.remove(this._img);
      this._img = null;
    },

    setCorners: function (corners) {
      this._corners = corners.map((c) => L.latLng(c));
      this._update();
      return this;
    },
    getCorners: function () { return this._corners.slice(); },
    setOpacity: function (o) {
      this.options.opacity = o;
      if (this._img) this._img.style.opacity = o;
      return this;
    },
    setUrl: function (url) {
      this._url = url;
      if (this._img) this._img.src = url;
      return this;
    },
    getBounds: function () { return L.latLngBounds(this._corners); },
    getElement: function () { return this._img; },

    _update: function () {
      const img = this._img;
      if (!img || !this._map || !img.naturalWidth) return;
      const w = img.naturalWidth, h = img.naturalHeight;
      img.style.width = w + "px";
      img.style.height = h + "px";
      const dst = this._corners.map((c) => {
        const p = this._map.latLngToLayerPoint(c);
        return [p.x, p.y];
      });
      img.style.transform = projectiveMatrix([[0, 0], [w, 0], [w, h], [0, h]], dst);
    },
  });

  // ---------------------------------------------------------------------------------------
  // Rendering of map data returned by /pkdb/api/maps/{id} or /pkdb/embed/map/{id}/data
  // ---------------------------------------------------------------------------------------
  const PALETTE = ["#2f7d32", "#1565c0", "#c62828", "#ef6c00", "#6a1b9a", "#00838f", "#ad1457", "#5d4037"];
  const HIDDEN_PROPS = new Set(["stroke", "stroke-width", "stroke-opacity", "fill", "fill-opacity",
    "marker-color", "marker-size", "marker-symbol", "color", "_pkdb"]);

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function linkify(s) {
    return esc(s).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  }

  function popupHtml(feature, layerName) {
    const p = feature.properties || {};
    const name = p.name || p.Name || p.NAME || p.title || "";
    const desc = p.description || p.desc || "";
    let html = '<div class="pkdb-popup">';
    if (name) html += "<strong>" + esc(name) + "</strong><br>";
    if (desc) html += '<div class="pkdb-desc">' + linkify(desc) + "</div>";
    const rows = Object.keys(p)
      .filter((k) => !HIDDEN_PROPS.has(k) && !["name", "Name", "NAME", "title", "description", "desc"].includes(k))
      .filter((k) => p[k] !== null && p[k] !== "" && typeof p[k] !== "object")
      .slice(0, 25)
      .map((k) => "<tr><th>" + esc(k) + "</th><td>" + linkify(p[k]) + "</td></tr>");
    if (rows.length) html += '<table class="pkdb-props">' + rows.join("") + "</table>";
    if (layerName) html += '<div class="pkdb-layername">' + esc(layerName) + "</div>";
    return html + "</div>";
  }

  function baseLayers() {
    return {
      "OpenStreetMap": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      }),
      "Satellite (Esri)": L.tileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
          maxZoom: 19, attribution: "Tiles &copy; Esri, Maxar, Earthstar Geographics",
        }),
      "Topographic (OpenTopoMap)": L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
        maxZoom: 17, attribution: '&copy; OpenStreetMap, SRTM | &copy; <a href="https://opentopomap.org">OpenTopoMap</a>',
      }),
    };
  }

  function styleFor(feature, color, highlighted) {
    const p = feature.properties || {};
    const c = p.stroke || p.color || p["marker-color"] || color;
    return {
      color: highlighted ? "#ffb300" : c,
      weight: highlighted ? 5 : (p["stroke-width"] || 3),
      opacity: p["stroke-opacity"] || 0.9,
      fillColor: highlighted ? "#ffb300" : (p.fill || c),
      fillOpacity: p["fill-opacity"] != null ? p["fill-opacity"] : 0.25,
    };
  }

  function addVectorLayer(geojson, color, name, highlight) {
    const hl = new Set((highlight || []).map(Number));
    const hlLayers = [];
    const layer = L.geoJSON(geojson, {
      style: (f) => styleFor(f, color, hl.has(Number(f.id))),
      pointToLayer: (f, latlng) => {
        const isHl = hl.has(Number(f.id));
        const st = styleFor(f, color, isHl);
        return L.circleMarker(latlng, Object.assign(st, { radius: isHl ? 9 : 6, fillOpacity: 0.85, weight: 2,
          color: "#fff", fillColor: st.fillColor }));
      },
      onEachFeature: (f, l) => {
        l.bindPopup(popupHtml(f, name), { maxWidth: 320 });
        const p = f.properties || {};
        const label = p.name || p.Name || p.NAME || p.title;
        if (label) l.bindTooltip(String(label), { sticky: true });
        if (hl.has(Number(f.id))) hlLayers.push(l);
      },
    });
    return { layer, hlLayers };
  }

  /**
   * Render a map.
   * opts.imageUrl(layer) -> Promise<string>|string  (lets the extension load images with auth)
   * opts.highlight: feature ids to emphasise; opts.scrollWheelZoom; opts.layerControl (default true)
   */
  async function render(el, data, opts) {
    opts = opts || {};
    const map = L.map(el, { scrollWheelZoom: opts.scrollWheelZoom !== false, zoomControl: true });
    const bases = baseLayers();
    bases["OpenStreetMap"].addTo(map);
    const overlays = {};
    let bounds = null;
    let hlBounds = null;
    const extend = (b) => { if (b && b.isValid()) bounds = bounds ? bounds.extend(b) : L.latLngBounds(b.getSouthWest(), b.getNorthEast()); };

    let colorIdx = 0;
    for (const lyr of data.layers || []) {
      if (lyr.kind === "image" && lyr.image) {
        const url = opts.imageUrl ? await opts.imageUrl(lyr) : lyr.image.url;
        const ov = new ProjectiveImageOverlay(url, lyr.image.corners, { opacity: 0.85, alt: lyr.name }).addTo(map);
        overlays["🖼 " + esc(lyr.name || "Image")] = ov;
        extend(ov.getBounds());
      } else if (lyr.geojson) {
        const color = (lyr.style && lyr.style.color) || PALETTE[colorIdx++ % PALETTE.length];
        const { layer, hlLayers } = addVectorLayer(lyr.geojson, color, lyr.name, opts.highlight);
        layer.addTo(map);
        overlays['<span style="color:' + color + '">■</span> ' + esc(lyr.name || "Layer")] = layer;
        extend(layer.getBounds());
        for (const h of hlLayers) {
          const b = h.getBounds ? h.getBounds() : L.latLngBounds([h.getLatLng(), h.getLatLng()]);
          hlBounds = hlBounds ? hlBounds.extend(b) : L.latLngBounds(b.getSouthWest(), b.getNorthEast());
        }
      }
    }
    if (opts.layerControl !== false) L.control.layers(bases, overlays, { collapsed: true }).addTo(map);
    L.control.scale({ imperial: false }).addTo(map);
    const target = hlBounds || bounds;
    if (target && target.isValid()) map.fitBounds(target.pad(0.15), { maxZoom: 17 });
    else map.setView([51.16, 10.45], 6);
    return map;
  }

  global.PKDBMap = { render, ProjectiveImageOverlay, projectiveMatrix, popupHtml, baseLayers, PALETTE, esc };
})(typeof window !== "undefined" ? window : this);
