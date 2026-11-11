/* Map preview shown inside search result pages. #map=<id>&hl=<feature ids> */
/* global PKDB, PKDBMap */
"use strict";
(async () => {
  const params = new URLSearchParams(location.hash.slice(1));
  const id = Number(params.get("map"));
  const hl = (params.get("hl") || "").split(",").filter(Boolean).map(Number);
  const msg = document.getElementById("msg");
  try {
    const data = await PKDB.request("/pkdb/api/maps/" + id);
    await PKDBMap.render(document.getElementById("map"), data, {
      highlight: hl, scrollWheelZoom: false,
      imageUrl: (layer) => PKDB.imageObjectUrl(layer.image.url),
    });
  } catch (e) {
    msg.textContent = "Map could not be loaded: " + e.message;
    msg.classList.remove("hidden");
  }
})();
