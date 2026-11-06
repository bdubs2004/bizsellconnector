// Search page map. Re-draws markers after every HTMX swap of #results.
(function () {
  var map = null, layer = null;

  function draw() {
    var el = document.getElementById("map-points");
    var box = document.getElementById("map");
    if (!el || !box || typeof L === "undefined") return;
    var points = [];
    try { points = JSON.parse(el.textContent || "[]"); } catch (e) { points = []; }
    if (!points.length) { box.hidden = true; return; }
    box.hidden = false;
    if (!map) {
      map = L.map(box, { scrollWheelZoom: false });
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 18, attribution: "&copy; OpenStreetMap contributors"
      }).addTo(map);
      layer = L.layerGroup().addTo(map);
    }
    layer.clearLayers();
    var bounds = [];
    points.forEach(function (p) {
      var m = L.circleMarker([p.lat, p.lon], {
        radius: 7, weight: 2,
        color: p.score >= 75 ? "#0f7b5f" : p.score >= 50 ? "#b7791f" : "#8a8f98",
        fillOpacity: 0.7
      });
      var a = document.createElement("a");
      a.href = "/business/" + p.id;
      a.textContent = p.name + " (" + p.score + ")";
      m.bindPopup(a);
      m.addTo(layer);
      bounds.push([p.lat, p.lon]);
    });
    map.invalidateSize();
    map.fitBounds(bounds, { padding: [24, 24], maxZoom: 11 });
  }

  document.addEventListener("DOMContentLoaded", function () {
    // On phones, start with filters collapsed so results are visible first.
    var d = document.querySelector(".filter-details");
    if (d && window.matchMedia("(max-width: 900px)").matches) d.open = false;
    draw();
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    if (e.detail && e.detail.target && e.detail.target.id === "results") draw();
  });
})();
