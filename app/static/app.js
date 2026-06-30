/* flight-tracker web UI — vanilla JS + Leaflet.
 * Talks to the FastAPI app on the same host:
 *   GET /api/aircraft  (~2s)  — map + all tracked aircraft
 *   GET /api/config           — config form
 *   POST /api/config          — save changes
 *   GET /api/diag      (~3s)  — diagnostics
 *   GET /api/current          — featured fallback
 *   WS  /ws                   — live featured push
 * All URLs are relative / location.host so it works at the device IP.
 */
"use strict";

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

/* ---------- small formatters ---------- */
const COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                 "S","SSW","SW","WSW","W","WNW","NW","NNW"];
const compass = (b) => (b == null ? "" : COMPASS[Math.round(b / 22.5) % 16]);
const num = (v, d = 0) => (v == null || v === "" || Number.isNaN(+v))
  ? null : (+v).toFixed(d);
const km = (v) => (v == null ? "&ndash;" : `${(+v).toFixed(1)} km`);
const ft = (v) => {
  if (v === "ground") return "ground";
  const n = num(v, 0);
  return n == null ? "&ndash;" : `${(+n).toLocaleString()} ft`;
};
const dur = (m) => {
  if (m == null) return null;
  const h = Math.floor(m / 60), mm = Math.round(m % 60);
  return h ? `${h}h ${mm}m` : `${mm}m`;
};
function trend(rate) {
  if (rate == null || Math.abs(rate) < 64) return { cls: "lvl", glyph: "→", txt: "level" };
  return rate > 0
    ? { cls: "up",   glyph: "↗", txt: `+${Math.round(rate)} fpm` }
    : { cls: "down", glyph: "↘", txt: `${Math.round(rate)} fpm` };
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------- map setup ---------- */
const map = L.map("map", {
  zoomControl: true,
  attributionControl: true,
  zoomSnap: 0,               // continuous zoom — no snapping/jumping to integer levels
  zoomDelta: 0.5,            // finer +/- button & double-click step
  wheelPxPerZoomLevel: 3,    // trackpad sensitivity — lower = more zoom per scroll
  wheelDebounceTime: 8,      // react faster to small trackpad deltas
}).setView([20, 0], 2);   // neutral world view until the receiver/airport location arrives
const _ctAttr = '&copy; OpenStreetMap, &copy; CARTO';
const baseLayers = {
  "Dark": L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    { attribution: _ctAttr, subdomains: "abcd", maxZoom: 20 }),
  "Dark (plain)": L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png",
    { attribution: _ctAttr, subdomains: "abcd", maxZoom: 20 }),
  "Streets": L.tileLayer("https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
    { attribution: _ctAttr, subdomains: "abcd", maxZoom: 20 }),   // street + landmark names
};
baseLayers["Dark"].addTo(map);     // default basemap (dark with labels)

const sectorLayer  = L.layerGroup().addTo(map);
const siteLayer    = L.layerGroup().addTo(map);
const runwayLayer  = L.layerGroup().addTo(map);   // runway strips + extended-centerline corridors
const trailLayer   = L.layerGroup().addTo(map);   // flight paths, under the plane markers
const acLayer      = L.layerGroup().addTo(map);
const airwayLayer  = L.layerGroup();   // aviation overlays (bundled navdata) — off by default
const navaidLayer  = L.layerGroup();
const fixLayer     = L.layerGroup();

// Layer switcher (top-right): pick a basemap + toggle the data overlays.
L.control.layers(baseLayers, {
  "Watch sector": sectorLayer,
  "Runways": runwayLayer,
  "Flight trails": trailLayer,
  "Airways": airwayLayer,
  "Navaids": navaidLayer,
  "Fixes": fixLayer,
}, { collapsed: true, position: "topright" }).addTo(map);

// Remember the map layer choices across reloads (per browser, localStorage).
const _LAYERS_KEY = "ft.map.layers.v1";
const _overlays = {
  "Watch sector": sectorLayer, "Runways": runwayLayer, "Flight trails": trailLayer,
  "Airways": airwayLayer, "Navaids": navaidLayer, "Fixes": fixLayer,
};
function _saveLayerPrefs() {
  const on = Object.keys(_overlays).filter((n) => map.hasLayer(_overlays[n]));
  let base = "Dark";
  for (const n in baseLayers) if (map.hasLayer(baseLayers[n])) base = n;
  try { localStorage.setItem(_LAYERS_KEY, JSON.stringify({ overlays: on, base })); } catch (e) {}
}
(function _restoreLayerPrefs() {
  let s = null;
  try { s = JSON.parse(localStorage.getItem(_LAYERS_KEY) || "null"); } catch (e) { return; }
  if (!s) return;
  if (s.base && baseLayers[s.base]) {
    for (const n in baseLayers) map.removeLayer(baseLayers[n]);
    baseLayers[s.base].addTo(map);
  }
  if (Array.isArray(s.overlays)) {
    for (const n in _overlays) {
      const want = s.overlays.includes(n), has = map.hasLayer(_overlays[n]);
      if (want && !has) _overlays[n].addTo(map);
      else if (!want && has) map.removeLayer(_overlays[n]);
    }
  }
})();
map.on("overlayadd overlayremove baselayerchange", _saveLayerPrefs);

/* ---------- aviation overlays: bundled navdata.json for the configured airport (off by default) ---------- */
const _ndFixIcon = L.divIcon({ className: "", iconSize: [9, 9], iconAnchor: [4, 4], html: '<i class="nd-fix"></i>' });
fetch("navdata.json").then((r) => (r.ok ? r.json() : null)).then((nd) => {
  if (!nd) return;   // no overlay generated yet — layers stay empty (see tools/build_navdata.py)
  (nd.airways || []).forEach((a) =>
    L.polyline([a.a, a.b], { color: "#bfa14a", weight: 1, opacity: 0.6 })
      .bindTooltip(a.name, { sticky: true, className: "nd-tip awy" }).addTo(airwayLayer));
  (nd.navaids || []).forEach((n) =>
    L.circleMarker([n.lat, n.lon], { radius: 3, color: "#5fb0d6", weight: 1.5, fill: false })
      .bindTooltip(n.id, { permanent: true, direction: "right", offset: [4, 0], className: "nd-tip nav" })
      .addTo(navaidLayer));
  (nd.fixes || []).forEach((f) =>
    L.marker([f.lat, f.lon], { icon: _ndFixIcon })
      .bindTooltip(f.id, { permanent: true, direction: "right", offset: [5, 0], className: "nd-tip fix" })
      .addTo(fixLayer));
}).catch((e) => console.warn("[navdata] load failed", e));

let receiverMarker = null, airportMarker = null;
let didAutoCenter  = false;
const acMarkers    = new Map();   // hex -> marker
const acTrails     = new Map();   // hex -> path polyline
let lastFeatured   = null;        // hex of featured (from /ws), to highlight on map

/* divIcon for an aircraft, rotated to track */
function planeIcon(ac, isFeat) {
  const cls = ["ac-icon"];
  if (isFeat) cls.push("feat");
  if (ac.military) cls.push("mil");
  const rot = ac.track == null ? 0 : ac.track;
  const label = ac.flight ? esc(ac.flight.trim()) : (ac.hex || "");
  return L.divIcon({
    className: "",
    iconSize: [22, 22],
    iconAnchor: [11, 11],
    html: `<div class="${cls.join(" ")}">
             <span class="glyph" style="transform:rotate(${rot}deg)"><svg viewBox="-12 -12 24 24" width="18" height="18" style="display:block"><path d="M0,-11 L2,-3 L11,2 L11,4 L2,2 L2,7 L4.5,9.5 L4.5,11 L0,9 L-4.5,11 L-4.5,9.5 L-2,7 L-2,2 L-11,4 L-11,2 L-2,-3 Z" fill="currentColor"/></svg></span>
             <span class="ac-label">${label}</span>
           </div>`,
  });
}

function siteIcon(glyph, label, labelClass) {
  return L.divIcon({
    className: "",
    iconSize: [24, 24],
    iconAnchor: [12, 12],
    html: `<div class="site-icon">${glyph}</div>` +
          (label ? `<span class="ap-label ${labelClass || ""}">${esc(label)}</span>` : ""),
  });
}

function acPopup(ac) {
  const route = (ac.origin || ac.destination)
    ? `<div class="p-route">${esc(ac.origin || "?")} <span class="arrow">&rarr;</span> ${esc(ac.destination || "?")}</div>`
    : "";
  const row = (l, r) => r == null || r === "" ? "" :
    `<tr><td class="l">${l}</td><td class="r">${r}</td></tr>`;
  const bearing = ac.bearing_from_me_deg != null
    ? `${ac.bearing_from_me_deg}° ${compass(ac.bearing_from_me_deg)}` : null;
  const opType = [ac.type, ac.type_desc].filter(Boolean).join(" · ");
  return `<div class="popup">
    <div class="p-cs ${ac.military ? "mil" : ""}">${esc((ac.flight || "").trim() || ac.hex || "unknown")}
      ${ac.military ? '<span class="p-mil-tag">MIL</span>' : ""}</div>
    <div class="p-sub">${esc(ac.hex || "")}${ac.operator ? " · " + esc(ac.operator) : ""}</div>
    ${route}
    <table>
      ${row("Type", esc(opType) || null)}
      ${row("Reg", esc(ac.registration))}
      ${row("Airline", esc(ac.airline))}
      ${row("Altitude", ft(ac.alt_baro))}
      ${row("Speed", num(ac.gs, 0) != null ? num(ac.gs, 0) + " kt" : null)}
      ${row("Dist (me)", km(ac.distance_km))}
      ${row("Dist (apt)", km(ac.distance_to_airport_km))}
      ${row("Bearing", bearing)}
    </table>
  </div>`;
}

/* draw watch-sector wedge polygon */
function destPoint(lat, lon, brgDeg, distKm) {
  const R = 6371.0088, d = distKm / R, b = brgDeg * Math.PI / 180;
  const p1 = lat * Math.PI / 180, l1 = lon * Math.PI / 180;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1),
                             Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [p2 * 180 / Math.PI, ((l2 * 180 / Math.PI) + 540) % 360 - 180];
}

function drawSector(rx, watch) {
  sectorLayer.clearLayers();
  if (!rx || !watch) return;
  const { center_deg = 0, half_angle_deg = 180, min_km = 0, max_km = 60 } = watch;
  const full = half_angle_deg >= 180;
  if (full) {
    // whole circle band: outer ring (+ inner hole if min_km>0)
    L.circle([rx.lat, rx.lon], { radius: max_km * 1000, color: "#38bdf8",
      weight: 1, fillColor: "#38bdf8", fillOpacity: 0.06 }).addTo(sectorLayer);
    if (min_km > 0)
      L.circle([rx.lat, rx.lon], { radius: min_km * 1000, color: "#38bdf8",
        weight: 1, dashArray: "4", fill: false }).addTo(sectorLayer);
    return;
  }
  const step = 4;
  const pts = [];
  const a0 = center_deg - half_angle_deg, a1 = center_deg + half_angle_deg;
  // outer arc
  for (let a = a0; a <= a1 + 0.01; a += step) pts.push(destPoint(rx.lat, rx.lon, a, max_km));
  // inner arc (reverse)
  if (min_km > 0)
    for (let a = a1; a >= a0 - 0.01; a -= step) pts.push(destPoint(rx.lat, rx.lon, a, min_km));
  else
    pts.push([rx.lat, rx.lon]);
  L.polygon(pts, { color: "#fbbf24", weight: 1.5, fillColor: "#fbbf24",
    fillOpacity: 0.10, dashArray: "5,5" }).addTo(sectorLayer);
  // center line
  L.polyline([[rx.lat, rx.lon], destPoint(rx.lat, rx.lon, center_deg, max_km)],
    { color: "#fbbf24", weight: 1, opacity: 0.5, dashArray: "2,6" }).addTo(sectorLayer);
}

/* place receiver + airport markers */
function drawSites(rx, ap) {
  siteLayer.clearLayers();
  if (rx) {
    receiverMarker = L.marker([rx.lat, rx.lon], {
      icon: siteIcon("📡", null), zIndexOffset: 1000,
    }).bindPopup(`<div class="popup"><b>Receiver</b><br>${rx.lat.toFixed(4)}, ${rx.lon.toFixed(4)}</div>`);
    siteLayer.addLayer(receiverMarker);
  }
  if (ap && ap.lat != null && ap.lon != null) {
    airportMarker = L.marker([ap.lat, ap.lon], {
      icon: siteIcon("🛬", ap.code || "APT"), zIndexOffset: 900,
    }).bindPopup(`<div class="popup"><b>${esc(ap.code || "Airport")}</b><br>${(+ap.lat).toFixed(4)}, ${(+ap.lon).toFixed(4)}</div>`);
    siteLayer.addLayer(airportMarker);
  }
}

/* ---------- /api/aircraft polling ---------- */
async function pollAircraft() {
  try {
    const r = await fetch("api/aircraft", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    renderAircraft(data);
  } catch (e) {
    // endpoint may be unavailable; keep last render, stay quiet
  }
}

/* smoothly slide a marker to a new position over `duration` ms (FR24-style glide) */
function slideMarker(m, to, duration) {
  const from = m.getLatLng();
  if (!from) { m.setLatLng(to); return; }
  if (Math.abs(from.lat - to[0]) < 1e-7 && Math.abs(from.lng - to[1]) < 1e-7) return;
  if (m._slideRAF) cancelAnimationFrame(m._slideRAF);
  const t0 = performance.now();
  const flat = from.lat, flng = from.lng, dlat = to[0] - flat, dlng = to[1] - flng;
  const step = (now) => {
    const k = Math.min(1, (now - t0) / duration);
    m.setLatLng([flat + dlat * k, flng + dlng * k]);
    m._slideRAF = k < 1 ? requestAnimationFrame(step) : null;
  };
  m._slideRAF = requestAnimationFrame(step);
}

const RWY_CORRIDOR_KM = 8;   // mirrors runways.py _CORRIDOR_KM (the final-approach corridor)
function drawRunways(ap, runway) {
  runwayLayer.clearLayers();
  if (!ap || ap.lat == null || ap.lon == null || !runway || !Array.isArray(runway.list)) return;
  const visible = Array.isArray(runway.visible) ? runway.visible : [];
  for (const r of runway.list) {
    if (typeof r.brg !== "number") continue;
    const isActive = r.id === runway.active;
    const recip = (r.brg + 180) % 360;          // approach side (planes fly final from here)
    // physical runway strip — a short segment through the airport along the runway axis
    L.polyline([destPoint(ap.lat, ap.lon, r.brg, 1.3), destPoint(ap.lat, ap.lon, recip, 1.3)],
      { color: "#cfd6e4", weight: 3, opacity: 0.85, interactive: false }).addTo(runwayLayer);
    // the active runway's detection corridor (extended centerline ± RWY_CORRIDOR_KM)
    if (isActive) {
      const ext = destPoint(ap.lat, ap.lon, recip, 16);
      const nl = destPoint(ap.lat, ap.lon, (r.brg + 90) % 360, RWY_CORRIDOR_KM);
      const nr = destPoint(ap.lat, ap.lon, (r.brg + 270) % 360, RWY_CORRIDOR_KM);
      const fl = destPoint(ext[0], ext[1], (r.brg + 90) % 360, RWY_CORRIDOR_KM);
      const fr = destPoint(ext[0], ext[1], (r.brg + 270) % 360, RWY_CORRIDOR_KM);
      L.polygon([nl, fl, fr, nr], { color: "#39d98a", weight: 0, fillColor: "#39d98a",
        fillOpacity: 0.08, interactive: false }).addTo(runwayLayer);
    }
    // extended approach centerline (dashed ray on the approach side) — active is highlighted
    const ext = destPoint(ap.lat, ap.lon, recip, 16);
    L.polyline([[ap.lat, ap.lon], ext], { color: isActive ? "#39d98a" : "#6b7488",
      weight: isActive ? 2.5 : 1, opacity: isActive ? 0.9 : 0.35, dashArray: "6,6",
      interactive: false }).addTo(runwayLayer);
    if (isActive) {
      L.marker(ext, { interactive: false, icon: L.divIcon({ className: "rwy-lbl",
        iconSize: null, html: `RWY ${esc(r.id)}${visible.includes(r.id) ? " &#128065;" : ""}` }) })
        .addTo(runwayLayer);
    }
  }
}

function renderAircraft(data) {
  const rx = data.receiver;
  drawSites(rx, data.airport);
  drawSector(rx, data.watch);
  drawRunways(data.airport, data.runway);

  // Center once on the receiver, else the configured airport, when its location first arrives.
  if (!didAutoCenter) {
    const ap = data.airport;
    if (rx) { map.setView([rx.lat, rx.lon], 9); didAutoCenter = true; }
    else if (ap && ap.lat != null && ap.lon != null) { map.setView([ap.lat, ap.lon], 9); didAutoCenter = true; }
  }

  const list = Array.isArray(data.aircraft) ? data.aircraft : [];
  const seen = new Set();

  for (const ac of list) {
    if (ac.lat == null || ac.lon == null) continue;
    seen.add(ac.hex);
    const isFeat = ac.featured === true || ac.hex === lastFeatured;
    // flight path trail
    if (Array.isArray(ac.trail) && ac.trail.length > 1) {
      const style = isFeat ? { color: "#ffd23f", weight: 2, opacity: 0.8 }
                           : { color: "#36c5ff", weight: 1.5, opacity: 0.45 };
      let pl = acTrails.get(ac.hex);
      if (pl) { pl.setLatLngs(ac.trail); pl.setStyle(style); }
      else {
        pl = L.polyline(ac.trail, { ...style, interactive: false });
        trailLayer.addLayer(pl); acTrails.set(ac.hex, pl);
      }
    }
    let m = acMarkers.get(ac.hex);
    const icon = planeIcon(ac, isFeat);
    if (m) {
      slideMarker(m, [ac.lat, ac.lon], 1100);
      m.setIcon(icon);
      m.setZIndexOffset(isFeat ? 800 : 0);
      m.setPopupContent(acPopup(ac));
    } else {
      m = L.marker([ac.lat, ac.lon], { icon, zIndexOffset: isFeat ? 800 : 0 })
        .bindPopup(acPopup(ac));
      acLayer.addLayer(m);
      acMarkers.set(ac.hex, m);
    }
  }
  // prune gone aircraft + their trails
  for (const [hex, m] of acMarkers) {
    if (!seen.has(hex)) { acLayer.removeLayer(m); acMarkers.delete(hex); }
  }
  for (const [hex, pl] of acTrails) {
    if (!seen.has(hex)) { trailLayer.removeLayer(pl); acTrails.delete(hex); }
  }

  $("#ac-count").textContent = list.length;
}

/* ---------- featured panel ---------- */
let distanceMode = "from_me";

function renderFeatured(featured) {
  const card = $("#featured-card");
  const body = $("#featured-body");
  lastFeatured = featured ? featured.hex : null;

  if (!featured) {
    card.classList.add("empty");
    body.innerHTML = `<div class="feat-empty-msg">No flight in your watch sector right now.</div>`;
    return;
  }
  card.classList.remove("empty");

  const cs = (featured.flight || "").trim() || featured.hex || "unknown";
  const t  = trend(featured.baro_rate);
  const tags = [];
  if (featured.landed)     tags.push('<span class="tag win">LANDED</span>');
  if (featured.military)   tags.push('<span class="tag mil">MILITARY</span>');
  if (featured.is_arrival && !featured.landed) tags.push('<span class="tag arr">ARRIVAL</span>');
  if (featured.is_departure) tags.push('<span class="tag dep">DEPARTURE</span>');
  const _rwy = featured.landing_runway || featured.departure_runway;
  if (_rwy) {
    const _k = featured.landing_runway ? "ARR" : "DEP";
    tags.push(featured.window_visible
      ? `<span class="tag win">${_k} RWY ${esc(_rwy)} &middot; PASSING BY</span>`
      : `<span class="tag oth">${_k} RWY ${esc(_rwy)} &middot; OTHER SIDE</span>`);
  }

  const origin = featured.origin || null;
  const dest   = featured.destination || null;
  const routeHtml = (origin || dest)
    ? `<span class="ap">${esc(origin || "?")}</span>
       <span class="arrow">&rarr;</span>
       <span class="ap">${esc(dest || "?")}</span>`
    : `<span class="ap unknown">route unknown</span>`;

  const idLine = [featured.type, featured.registration].filter(Boolean).map(esc).join(" · ");
  const operator = featured.operator || featured.airline;

  // distance per distance_mode
  let distKV = "";
  const dMe  = featured.distance_km;
  const dApt = featured.distance_to_airport_km;
  if (distanceMode === "to_airport") {
    distKV = kv("Dist to airport", dApt == null ? "&ndash;" : km(dApt));
  } else if (distanceMode === "both") {
    distKV = kv("Dist (me / apt)",
      `${dMe == null ? "&ndash;" : (+dMe).toFixed(1)} <small>/ ${dApt == null ? "&ndash;" : (+dApt).toFixed(1)} km</small>`);
  } else {
    distKV = kv("Distance", dMe == null ? "&ndash;" : km(dMe));
  }

  const brg = featured.bearing_from_me_deg;
  const brgKV = brg == null ? kv("Bearing", "&ndash;")
    : kv("Bearing", `${brg}° <small>${compass(brg)}</small>`);

  const durTxt = dur(featured.duration_est_min);
  const durKV = durTxt
    ? kv("Duration", `${durTxt}${featured.duration_is_estimate ? " <small>est</small>" : ""}`)
    : "";

  body.innerHTML = `
    <div class="feat-head">
      <span class="feat-callsign">${esc(cs)}</span>
      ${operator ? `<span class="feat-airline">${esc(operator)}</span>` : ""}
    </div>
    ${tags.length ? `<div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">${tags.join("")}</div>` : ""}
    <div class="feat-route">${routeHtml}</div>
    ${idLine ? `<div class="feat-airline" style="margin:-6px 0 8px">${idLine}${featured.type_desc ? " · " + esc(featured.type_desc) : ""}</div>` : ""}
    <div class="feat-grid">
      ${kv("Altitude", `${ft(featured.alt_baro)} <span class="trend ${t.cls}">${t.glyph}</span>`)}
      ${kv("Ground speed", num(featured.gs, 0) != null ? `${num(featured.gs, 0)}<small> kt</small>` : "&ndash;")}
      ${distKV}
      ${brgKV}
      ${kv("Vertical", `<span class="trend ${t.cls}">${t.txt}</span>`)}
      ${durKV}
    </div>`;
}

function kv(k, v) {
  return `<div class="kv"><div class="k">${k}</div><div class="v">${v}</div></div>`;
}

/* ---------- /api/current fallback ---------- */
async function pollCurrent() {
  try {
    const r = await fetch("api/current", { cache: "no-store" });
    if (!r.ok) return;
    const s = await r.json();
    renderFeatured(s.featured || null);
    if (s.count != null) $("#ac-count").textContent = s.count;
  } catch (e) { /* ignore */ }
}

/* ---------- WebSocket (live featured) ---------- */
let ws = null, wsRetry = 0, wsKeepalive = null, currentTimer = null, acTimer = null;

function setActiveRunway(rwy, visible) {
  const el = $("#active-rwy");
  if (!el) return;
  el.textContent = rwy || "–";
  const isVis = rwy && Array.isArray(visible) && visible.includes(rwy);
  el.className = "rwy-badge" + (rwy ? (isVis ? " win" : " oth") : "");
}

function setConn(state) {
  const dot = $("#conn-dot"), txt = $("#conn-text");
  dot.className = "conn-dot " + (state === "live" ? "live" : state === "down" ? "down" : "");
  txt.textContent = state === "live" ? "live" : state === "down" ? "offline" : "connecting";
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  try { ws = new WebSocket(`${proto}://${location.host}/ws`); }
  catch (e) { scheduleReconnect(); return; }

  ws.onopen = () => {
    wsRetry = 0;
    setConn("live");
    // stop the fallback pollers while WS is healthy (WS drives featured + map)
    if (currentTimer) { clearInterval(currentTimer); currentTimer = null; }
    if (acTimer) { clearInterval(acTimer); acTimer = null; }
    clearInterval(wsKeepalive);
    wsKeepalive = setInterval(() => { try { ws.send("ping"); } catch (_) {} }, 25000);
  };
  ws.onmessage = (e) => {
    try {
      const s = JSON.parse(e.data);
      renderFeatured(s.featured || null);
      if (s.count != null) $("#ac-count").textContent = s.count;
      if (s.runway) setActiveRunway(s.runway.active, s.runway.visible);
      if (Array.isArray(s.aircraft)) renderAircraft(s);   // live map update via WS
    } catch (_) {}
  };
  ws.onclose = () => { setConn("down"); cleanupWS(); scheduleReconnect(); };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}

function cleanupWS() {
  clearInterval(wsKeepalive); wsKeepalive = null;
  // resume fallback polling so the panel + map keep updating while WS is down
  if (!currentTimer) currentTimer = setInterval(pollCurrent, 3000);
  if (!acTimer) acTimer = setInterval(pollAircraft, 3000);
}

function scheduleReconnect() {
  wsRetry = Math.min(wsRetry + 1, 6);
  const delay = Math.min(1000 * 2 ** (wsRetry - 1), 15000);
  setTimeout(connectWS, delay);
}

/* ---------- config form ---------- */
const form = $("#config-form");

function fillForm(cfg) {
  const set = (name, val) => { const el = form.elements[name]; if (el && val != null) el.value = val; };
  set("lat", cfg.lat);
  set("lon", cfg.lon);
  set("home_airport", cfg.home_airport);
  set("airport_lat", cfg.airport_lat);
  set("airport_lon", cfg.airport_lon);
  set("select_rule", cfg.select_rule);
  const radio = (name, val) => {
    const el = form.querySelector(`input[name="${name}"][value="${val}"]`);
    if (el) el.checked = true;
  };
  radio("traffic_mode", cfg.traffic_mode || "all");
  radio("distance_mode", cfg.distance_mode || "from_me");
  distanceMode = cfg.distance_mode || "from_me";
  const w = cfg.watch || {};
  set("watch.center_deg", w.center_deg);
  set("watch.half_angle_deg", w.half_angle_deg);
  set("watch.min_km", w.min_km);
  set("watch.max_km", w.max_km);
  set("brightness", cfg.brightness);
  const bv = $("#bright-val"); if (bv && cfg.brightness != null) bv.textContent = cfg.brightness;
  const chk = (name, val) => { const el = form.elements[name]; if (el) el.checked = !!val; };
  chk("auto_brightness", cfg.auto_brightness);
  chk("notify_flash", cfg.notify_flash);
  chk("hide_no_callsign", cfg.hide_no_callsign);
  chk("hide_general_aviation", cfg.hide_general_aviation);
  const p = cfg.panel || {};
  set("panel.layout", p.layout);
  set("panel.scroll_speed_px", p.scroll_speed_px);
  const ssv = $("#scroll-speed-val"); if (ssv && p.scroll_speed_px != null) ssv.textContent = p.scroll_speed_px;
  set("panel.idle_behavior", p.idle_behavior);
  set("panel.idle_text", p.idle_text);
  set("panel.route_extra", p.route_extra);
  const sf = p.scroll_fields || [];
  form.querySelectorAll('input[name="panel.scroll_fields"]').forEach((el) => { el.checked = sf.includes(el.value); });
  // Visible-runways picker — checkboxes built from the airport's runways (cfg.runways).
  const vr = $("#visible-runways");
  if (vr && Array.isArray(cfg.runways)) {     // only rebuild when runways are present (never wipe)
    const have = cfg.visible_runways || [];
    vr.innerHTML = cfg.runways.map((r) =>
      `<label class="chk"><input type="checkbox" class="vrwy" value="${esc(r)}"${have.includes(r) ? " checked" : ""} /><span>${esc(r)}</span></label>`
    ).join("");
  }
  fillAirband(cfg);
  const mx = cfg.matrix || {};
  const mset = (id, v) => { const e = $(id); if (e && v != null) e.value = v; };
  mset("#mx-refresh", mx.refresh_hz); mset("#mx-gpio", mx.gpio_slowdown);
  mset("#mx-bits", mx.pwm_bits); mset("#mx-lsb", mx.pwm_lsb_ns); mset("#mx-dither", mx.pwm_dither_bits);
  mset("#mx-hw", mx.hardware);
}

async function loadConfig() {
  try {
    const r = await fetch("api/config", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    fillForm(await r.json());
  } catch (e) {
    note("Config endpoint unavailable", "err");
  }
}

function note(msg, cls) {
  const el = $("#save-note");
  el.textContent = msg;
  el.className = "save-note " + (cls || "");
  if (cls === "ok") setTimeout(() => { el.textContent = ""; el.className = "save-note"; }, 2500);
}

/* build the POST body from the current form state (nulls dropped) */
function configBody() {
  const f = form.elements;
  const numOrNull = (n) => f[n].value === "" ? null : +f[n].value;
  const body = {
    lat: numOrNull("lat"),
    lon: numOrNull("lon"),
    home_airport: f["home_airport"].value || null,
    airport_lat: numOrNull("airport_lat"),
    airport_lon: numOrNull("airport_lon"),
    select_rule: f["select_rule"].value,
    traffic_mode: (form.querySelector('input[name="traffic_mode"]:checked') || {}).value,
    distance_mode: (form.querySelector('input[name="distance_mode"]:checked') || {}).value,
    brightness: numOrNull("brightness"),
    auto_brightness: f["auto_brightness"] ? f["auto_brightness"].checked : null,
    notify_flash: f["notify_flash"] ? f["notify_flash"].checked : null,
    hide_no_callsign: f["hide_no_callsign"] ? f["hide_no_callsign"].checked : null,
    hide_general_aviation: f["hide_general_aviation"] ? f["hide_general_aviation"].checked : null,
    watch: {
      center_deg: numOrNull("watch.center_deg"),
      half_angle_deg: numOrNull("watch.half_angle_deg"),
      min_km: numOrNull("watch.min_km"),
      max_km: numOrNull("watch.max_km"),
    },
    panel: {
      layout: f["panel.layout"] ? f["panel.layout"].value : null,
      scroll_speed_px: numOrNull("panel.scroll_speed_px"),
      idle_behavior: f["panel.idle_behavior"] ? f["panel.idle_behavior"].value : null,
      idle_text: f["panel.idle_text"] ? f["panel.idle_text"].value : null,
      route_extra: f["panel.route_extra"] ? f["panel.route_extra"].value : null,
      scroll_fields: [...form.querySelectorAll('input[name="panel.scroll_fields"]:checked')].map((e) => e.value),
    },
  };
  // visible runways (only if the picker has rendered, so we never wipe before load)
  const vrwy = [...document.querySelectorAll("#visible-runways .vrwy")];
  if (vrwy.length) body.visible_runways = vrwy.filter((c) => c.checked).map((c) => c.value);
  // drop nulls so we only send changed/known fields
  Object.keys(body).forEach((k) => body[k] == null && delete body[k]);
  Object.keys(body.watch).forEach((k) => body.watch[k] == null && delete body.watch[k]);
  if (!Object.keys(body.watch).length) delete body.watch;
  Object.keys(body.panel).forEach((k) => body.panel[k] == null && delete body.panel[k]);
  if (!Object.keys(body.panel).length) delete body.panel;
  return body;
}

/* POST the current form. `silent` (auto-save) skips the button-disable + the form
   refill so it never fights the user's typing; the Save button passes silent=false. */
async function saveConfig(silent) {
  const sbtn = $("#save-btn");                 // may be absent (auto-save only)
  if (sbtn && !silent) sbtn.disabled = true;
  note("Saving…");
  try {
    const r = await fetch("api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(configBody()),
    });
    if (!r.ok) throw new Error(r.status);
    const cfg = await r.json();
    if (!silent) fillForm(cfg);   // normalise only on explicit save (won't disrupt typing)
    note(silent ? "Auto-saved" : "Saved", "ok");
    pollAircraft();               // redraw sector/airport immediately
  } catch (e) {
    note("Save failed (" + e.message + ")", "err");
  } finally {
    if (sbtn && !silent) sbtn.disabled = false;
  }
}

// Auto-save: debounced POST on every edit (typing, selects, radio segments).
let saveTimer = null;
const autoSave = () => { clearTimeout(saveTimer); saveTimer = setTimeout(() => saveConfig(true), 800); };

form.addEventListener("submit", (ev) => { ev.preventDefault(); saveConfig(false); });
form.addEventListener("input", (ev) => {
  if (ev.target.name === "distance_mode") distanceMode = ev.target.value;  // live preview
  if (ev.target.name === "brightness") { const v = $("#bright-val"); if (v) v.textContent = ev.target.value; }
  if (ev.target.name === "panel.scroll_speed_px") { const v = $("#scroll-speed-val"); if (v) v.textContent = ev.target.value; }
  autoSave();
});
form.addEventListener("change", autoSave);   // selects / radios that emit 'change'

/* ---------- diagnostics ---------- */
async function pollDiag() {
  try {
    const r = await fetch("api/diag", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    $("#d-messages").textContent  = d.messages != null ? (+d.messages).toLocaleString() : "–";
    $("#d-aircraft").textContent  = d.aircraft_count ?? "–";
    $("#d-range").textContent     = d.max_range_km != null ? (+d.max_range_km).toFixed(1) + " km" : "–";
    const gps = $("#d-gps");
    const g = d.gps || {};
    gps.textContent = d.gps_fix
      ? `${g.mode === 3 ? "3D" : "2D"}${g.sats != null ? " · " + g.sats + " sats" : ""}`
      : "no fix";
    gps.className = "v " + (d.gps_fix ? "ok" : "bad");
    if (d.receiver)
      $("#d-receiver").textContent = `${(+d.receiver.lat).toFixed(4)}, ${(+d.receiver.lon).toFixed(4)}`
        + (d.receiver.gps ? " (GPS)" : "");
    if (d.max_range_km != null) $("#range-km").textContent = (+d.max_range_km).toFixed(0);
    if (d.aircraft_count != null) $("#ac-count").textContent = d.aircraft_count;
  } catch (e) {
    $("#d-messages").textContent = "–";
  }
}

/* ---------- airband (tower audio) ---------- */
const twrAudio = $("#twr-audio"), twrBtn = $("#twr-listen"), twrCard = $("#airband-card");
let twrPlaying = false, twrUrl = null;

async function pollAirband() {
  try {
    const r = await fetch("api/airband", { cache: "no-store" });
    if (!r.ok) return;
    const a = await r.json();
    if (!a.enabled) { if (twrCard) twrCard.style.display = "none"; return; }
    if (twrCard) twrCard.style.display = "";
    // Icecast is published on the device host at :port — build URL from current host.
    twrUrl = `${location.protocol}//${location.hostname}:${a.port || 8000}/${a.mount || "atc.mp3"}`;
    const dot = $("#twr-state");
    if (dot) dot.className = "twr-dot " + (a.online ? "live" : "down");
    const lbl = $("#twr-label");
    if (lbl) lbl.textContent = a.online ? (a.title ? `on air · ${a.title}` : "on air") : "offline";
  } catch (e) { /* ignore */ }
}

if (twrBtn) twrBtn.addEventListener("click", () => {
  if (!twrUrl) return;
  if (twrPlaying) {
    twrAudio.pause(); twrAudio.removeAttribute("src"); twrAudio.load();
    twrPlaying = false; twrBtn.innerHTML = "▶ Listen";
    return;
  }
  twrAudio.src = twrUrl + "?t=" + Date.now();   // cache-bust the live stream
  twrAudio.play()
    .then(() => { twrPlaying = true; twrBtn.innerHTML = "◼ Stop"; })
    .catch(() => { $("#twr-label").textContent = "tap again to play"; });
});
if (twrAudio) twrAudio.addEventListener("error", () => {
  if (twrPlaying) { twrPlaying = false; twrBtn.innerHTML = "▶ Listen"; }
});

/* ---------- airband freq editor + speaker test ---------- */
const freqRows = $("#freq-rows");
function renderFreqRow(mhz = "", label = "") {
  if (!freqRows) return;
  const row = document.createElement("div");
  row.className = "freq-row";
  row.innerHTML =
    `<input type="number" class="f-mhz" min="108" max="137" step="0.001" value="${esc(mhz)}" placeholder="118.300" />`
    + `<input type="text" class="f-label" maxlength="8" value="${esc(label)}" placeholder="TWR" />`
    + `<button type="button" class="f-del ghost" title="remove">×</button>`;
  freqRows.appendChild(row);
}
function fillAirband(cfg) {
  if (!freqRows) return;
  const a = (cfg && cfg.airband) || {};
  freqRows.innerHTML = "";
  (a.freqs || []).forEach((r) => renderFreqRow(r.mhz, r.label || ""));
  const g = $("#airband-gain"); if (g && a.gain != null) g.value = a.gain;
  const sq = $("#airband-squelch"); if (sq) sq.value = (a.squelch_snr != null ? a.squelch_snr : 9);
  if (cfg && cfg.volume != null) {
    const v = $("#airband-volume"); if (v) v.value = cfg.volume;
    const vv = $("#vol-val"); if (vv) vv.textContent = cfg.volume + "%";
  }
  const sub = $("#twr-sub");
  if (sub) sub.textContent = "scan · " + (a.freqs || [])
    .map((r) => `${r.label || ""} ${(+r.mhz).toFixed(2)}`.trim()).join(" · ");
}
function airbandBody() {
  const freqs = [...document.querySelectorAll("#freq-rows .freq-row")].map((row) => ({
    mhz: parseFloat(row.querySelector(".f-mhz").value),
    label: row.querySelector(".f-label").value.trim(),
  })).filter((r) => !isNaN(r.mhz));
  const body = { freqs, gain: parseFloat($("#airband-gain").value) };
  const sq = parseFloat(($("#airband-squelch") || {}).value);
  if (!isNaN(sq)) body.squelch_snr = sq;
  return body;
}
function airbandNote(m, c) {
  const e = $("#airband-note"); if (e) { e.textContent = m; e.className = "save-note " + (c || ""); }
}
async function saveAirband() {
  const btn = $("#airband-save"); if (btn) btn.disabled = true;
  airbandNote("Applying… (airband restarts, audio drops briefly)");
  try {
    const r = await fetch("api/airband/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(airbandBody()),
    });
    if (!r.ok) throw new Error(r.status);
    const res = await r.json();
    if (res.airband) fillAirband({ airband: res.airband });
    airbandNote(res.restarted ? "Applied — airband restarting…" : "Saved (not on balena)", "ok");
  } catch (e) { airbandNote("Apply failed (" + e.message + ")", "err"); }
  finally { if (btn) btn.disabled = false; }
}
async function testBeep() {
  const btn = $("#airband-beep"); if (btn) btn.disabled = true;
  airbandNote("Testing speaker… (listen for a beep in a few seconds)");
  try {
    const r = await fetch("api/airband/test-beep", { method: "POST" });
    if (!r.ok) throw new Error(r.status);
    const res = await r.json();
    airbandNote(res.restarted ? "Speaker restarting — beep incoming" : "Not on balena (no speaker)", "ok");
  } catch (e) { airbandNote("Test failed (" + e.message + ")", "err"); }
  finally { setTimeout(() => { if (btn) btn.disabled = false; }, 3000); }
}
if ($("#freq-add")) $("#freq-add").addEventListener("click", () => renderFreqRow());
if (freqRows) freqRows.addEventListener("click", (ev) => {
  if (ev.target.classList.contains("f-del")) ev.target.closest(".freq-row").remove();
});
if ($("#airband-save")) $("#airband-save").addEventListener("click", saveAirband);

/* ---------- USB sound-card volume (live; the speaker polls the value, no restart) ---------- */
const volSlider = $("#airband-volume");
let volTimer = null;
if (volSlider) volSlider.addEventListener("input", () => {
  const v = parseInt(volSlider.value, 10);
  const vv = $("#vol-val"); if (vv) vv.textContent = v + "%";
  clearTimeout(volTimer);
  volTimer = setTimeout(() => {
    fetch("api/volume", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: v }),
    }).catch(() => {});
  }, 200);
});
if ($("#airband-beep")) $("#airband-beep").addEventListener("click", testBeep);

/* ---------- test flight (fake data for tuning the display) ---------- */
const tfBtn = $("#test-flight-btn");
let tfActive = false;
if (tfBtn) tfBtn.addEventListener("click", async () => {
  const want = !tfActive;
  try {
    const r = await fetch("api/test-flight", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(want ? {} : { clear: true }),
    });
    if (!r.ok) throw new Error(r.status);
    tfActive = want;
    tfBtn.innerHTML = tfActive ? "◼ Stop test flight" : "▶ Test flight (fake data)";
    tfBtn.classList.toggle("active", tfActive);
  } catch (e) { /* leave state as-is on error */ }
});

/* ---------- test clock (force the idle flip-clock on the panel) ---------- */
const tcBtn = $("#test-clock-btn");
let tcActive = false;
if (tcBtn) tcBtn.addEventListener("click", async () => {
  const want = !tcActive;
  try {
    const r = await fetch("api/test-clock", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(want ? {} : { clear: true }),
    });
    if (!r.ok) throw new Error(r.status);
    tcActive = want;
    tcBtn.innerHTML = tcActive ? "◼ Stop clock test" : "&#9654; Show clock (test)";
    tcBtn.classList.toggle("active", tcActive);
  } catch (e) { /* leave state as-is on error */ }
});

/* ---------- panel tuning (matrix PWM) — Apply restarts the display ---------- */
const mxApply = $("#mx-apply");
function mxNote(m, c) { const e = $("#mx-note"); if (e) { e.textContent = m; e.className = "save-note " + (c || ""); } }
if (mxApply) mxApply.addEventListener("click", async () => {
  mxApply.disabled = true;
  mxNote("Applying… the panel restarts (~5s)");
  const num = (id) => { const v = parseInt(($(id) || {}).value, 10); return isNaN(v) ? undefined : v; };
  const body = {
    refresh_hz: num("#mx-refresh"), gpio_slowdown: num("#mx-gpio"), pwm_bits: num("#mx-bits"),
    pwm_lsb_ns: num("#mx-lsb"), pwm_dither_bits: num("#mx-dither"),
    hardware: ($("#mx-hw") || {}).value || undefined,
  };
  Object.keys(body).forEach((k) => body[k] === undefined && delete body[k]);
  try {
    const r = await fetch("api/matrix", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(r.status);
    const res = await r.json();
    if (res.matrix) {
      const m = res.matrix, s = (id, v) => { const e = $(id); if (e && v != null) e.value = v; };
      s("#mx-refresh", m.refresh_hz); s("#mx-gpio", m.gpio_slowdown); s("#mx-bits", m.pwm_bits);
      s("#mx-lsb", m.pwm_lsb_ns); s("#mx-dither", m.pwm_dither_bits); s("#mx-hw", m.hardware);
    }
    mxNote(res.restarted ? "Applied — panel restarting…" : "Saved (not on balena)", "ok");
  } catch (e) { mxNote("Failed (" + e.message + ")", "err"); }
  finally { setTimeout(() => { mxApply.disabled = false; }, 6000); }
});

/* ---------- flight history (SQLite): filters + detail + replay ---------- */
const histLayer = L.layerGroup().addTo(map);    // a selected past flight's track / replay
let histFlights = [];
let replayTimer = null;
let histSeq = 0;          // guards against overlapping flight selections (stale-fetch race)
let histViewing = false;  // a past flight is currently selected/shown
let histPrevView = null;  // map view before selecting, to restore on "back to live"
let histShown = 25;       // how many rows currently rendered (pagination)
const HIST_PAGE = 25;
function startOfTodaySec() { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; }
function histNote(m) { const e = $("#hist-note"); if (e) e.textContent = m || ""; }
function fmtTime(s) {
  return new Date(s * 1000).toLocaleString([],
    { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function histRow(f) {
  const route = (f.origin || "?") + "›" + (f.destination || "?");
  const ph = f.phase === "arrival" ? "arr" : f.phase === "departure" ? "dep" : "ovf";
  return `<div class="hist-row" data-id="${esc(f.id)}"><span class="hist-cs">${esc(f.callsign || f.hex)} · ${esc(route)}</span>`
    + `<span class="hist-meta">${esc(f.type || "")} · ${ph}${f.window_visible ? " · seen" : ""} · ${esc(fmtTime(f.started_at))}</span></div>`;
}
function renderHistList() {
  const q = ($("#hist-search").value || "").trim().toLowerCase();
  const phase = $("#hist-phase").value;
  const range = ($("#hist-range") || {}).value || "today";
  const todayStart = startOfTodaySec();
  const filtered = histFlights.filter((f) => {
    if (range === "today" && f.started_at < todayStart) return false;
    if (phase && f.phase !== phase) return false;
    if (!q) return true;
    return [f.callsign, f.hex, f.origin, f.destination, f.type, f.registration, f.operator]
      .some((v) => (v || "").toLowerCase().includes(q));
  });
  const page = filtered.slice(0, histShown);
  $("#hist-list").innerHTML = page.length ? page.map(histRow).join("")
    : "<div class='feat-empty-msg'>No flights" + (range === "today" ? " today" : "") + ".</div>";
  const more = $("#hist-more");
  if (more) {
    const rest = filtered.length - page.length;
    more.hidden = rest <= 0;
    more.textContent = "Load more (" + rest + ")";
  }
}
function histReset() { histShown = HIST_PAGE; renderHistList(); }
async function loadHistory() {
  try {
    const r = await fetch("api/history?limit=200", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    histFlights = (await r.json()).flights || [];
    if (!histFlights.length) { $("#hist-list").innerHTML = "<div class='feat-empty-msg'>No flights recorded yet.</div>"; histNote(""); return; }
    histReset(); histNote("");
  } catch (e) { histNote("unavailable"); }
}
function stopReplay() { if (replayTimer) { clearInterval(replayTimer); replayTimer = null; } }
function flightDetail(f) {
  const dur = f.ended_at && f.started_at ? Math.round((f.ended_at - f.started_at) / 60) : null;
  const rwy = f.landing_runway || f.departure_runway;
  const rows = [
    ["Route", (f.origin || "?") + " → " + (f.destination || "?")],
    ["Aircraft", [f.type, f.registration].filter(Boolean).join(" · ") || "–"],
    ["Operator", f.operator || "–"], ["Phase", f.phase || "–"],
    ["Runway", (rwy || "–") + (f.window_visible ? " (seen)" : "")],
    ["Alt range", (f.min_alt != null ? f.min_alt : "?") + "–" + (f.max_alt != null ? f.max_alt : "?") + " ft"],
    ["Closest", f.closest_km != null ? f.closest_km + " km" : "–"],
    ["When", fmtTime(f.started_at) + (dur != null ? " · " + dur + " min" : "")],
  ];
  return `<div class="hist-det-cs">${esc(f.callsign || f.hex)}</div><div class="hist-det-grid">`
    + rows.map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v">${esc(String(v))}</div>`).join("")
    + `</div><div class="airband-row"><button type="button" id="hist-replay" class="ghost">&#9654; Replay</button>`
    + `<button type="button" id="hist-clear" class="ghost">&#10005; Back to live</button>`
    + `<span id="hist-replay-pos" class="hist-meta"></span></div>`;
}
function clearFlight() {                 // deselect the past flight, return to the live view
  stopReplay();
  histSeq++;                             // invalidate any in-flight track fetch
  histLayer.clearLayers();
  const det = $("#hist-detail"); if (det) det.hidden = true;
  histNote("");
  if (histViewing && histPrevView) map.setView(histPrevView.center, histPrevView.zoom);
  histViewing = false;
}
async function showFlight(id) {
  stopReplay();
  const seq = ++histSeq;
  const f = histFlights.find((x) => String(x.id) === String(id));
  const det = $("#hist-detail");
  if (!f) { if (det) det.hidden = true; histNote("refresh — flight not in list"); return; }
  if (!histViewing) histPrevView = { center: map.getCenter(), zoom: map.getZoom() };
  histViewing = true;
  histNote("loading track…");
  let track = [];
  try {
    const r = await fetch(`api/flight/${id}/track`, { cache: "no-store" });
    if (r.ok) track = (await r.json()).track || [];
  } catch (e) { /* ignore */ }
  if (seq !== histSeq) return;            // a newer selection started — drop this stale fetch
  histNote(track.length + " points");
  if (det) { det.innerHTML = flightDetail(f); det.hidden = false; }
  histLayer.clearLayers();
  if (track.length > 1) {
    const pl = L.polyline(track.map((p) => [p.lat, p.lon]), { color: "#ff7ad9", weight: 2, opacity: 0.85 });
    histLayer.addLayer(pl); map.fitBounds(pl.getBounds(), { padding: [40, 40], maxZoom: 11 });
  }
  const rep = $("#hist-replay");
  if (rep) rep.addEventListener("click", () => replayTrack(track));
  const clr = $("#hist-clear");
  if (clr) clr.addEventListener("click", clearFlight);
}
function replayTrack(track) {
  stopReplay();
  if (track.length < 2) return;
  histLayer.clearLayers();
  const full = track.map((p) => [p.lat, p.lon]);
  L.polyline(full, { color: "#ff7ad9", weight: 1, opacity: 0.3, interactive: false }).addTo(histLayer);
  const grown = L.polyline([full[0]], { color: "#ff7ad9", weight: 2.5, opacity: 0.95, interactive: false }).addTo(histLayer);
  const mk = L.circleMarker(full[0], { radius: 5, color: "#fff", weight: 1, fillColor: "#ff7ad9", fillOpacity: 1 }).addTo(histLayer);
  map.fitBounds(L.polyline(full).getBounds(), { padding: [40, 40], maxZoom: 11 });
  const stepMs = Math.max(40, Math.min(180, Math.round(12000 / track.length)));
  let i = 0;
  replayTimer = setInterval(() => {
    if (++i >= full.length) { stopReplay(); return; }
    grown.setLatLngs(full.slice(0, i + 1));
    mk.setLatLng(full[i]);
    const p = track[i], e = $("#hist-replay-pos");
    if (e) e.textContent = [p.alt_baro != null ? p.alt_baro + "ft" : "", p.gs != null ? Math.round(p.gs) + "kt" : ""].filter(Boolean).join(" · ");
  }, stepMs);
}
if ($("#hist-list")) $("#hist-list").addEventListener("click", (ev) => {
  const row = ev.target.closest(".hist-row"); if (row) showFlight(row.dataset.id);
});
if ($("#hist-search")) $("#hist-search").addEventListener("input", histReset);
if ($("#hist-phase")) $("#hist-phase").addEventListener("change", histReset);
if ($("#hist-range")) $("#hist-range").addEventListener("change", histReset);
if ($("#hist-refresh")) $("#hist-refresh").addEventListener("click", loadHistory);
if ($("#hist-more")) $("#hist-more").addEventListener("click", () => { histShown += HIST_PAGE; renderHistList(); });
loadHistory();

/* ---------- collapsible cards ---------- */
$$(".card > h2[data-toggle]").forEach((h) =>
  h.addEventListener("click", () => h.parentElement.classList.toggle("collapsed")));

/* ---------- boot ---------- */
loadConfig();
pollAircraft();
pollDiag();
pollCurrent();          // initial featured before WS connects
connectWS();   // WS drives the live map + featured (poll fallbacks kick in if WS drops)
setInterval(pollDiag, 3000);
pollAirband();
setInterval(pollAirband, 5000);
// fix Leaflet sizing once layout settles (esp. on phones)
setTimeout(() => map.invalidateSize(), 300);
window.addEventListener("resize", () => map.invalidateSize());
