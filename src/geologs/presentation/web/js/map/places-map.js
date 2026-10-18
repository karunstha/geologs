// The Places map: where you have been as heat (Visited), your saved zones (Saved), and the routes
// you take between places, hotter the more often (Routines). The lists beside it follow its view.
import { api } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { durationText, plural, radiusText } from "../core/format.js";
import { state } from "../core/state.js";
import { KIND_INFO, kindOf } from "../ui/place-kinds.js";
import { heatLayer } from "./heat.js";

let map, heat, dots, zones, routes, focus;
let mode = "visited";
// The tabs each get a first view of their own; after that the map stays where you left it.
const fitted = new Set();
let routeRequest = 0;
let routeTimer;
const listeners = new Set();

// Cool to hot, for routes by how often you took them.
const ROUTE_RAMP = ["#3b82f6", "#14b8a6", "#eab308", "#f97316", "#dc2626"];

function rampColour(share) {
  return ROUTE_RAMP[Math.min(ROUTE_RAMP.length - 1, Math.floor(share * ROUTE_RAMP.length))];
}

// Called whenever the view settles, so the lists can show what is in it.
export function onPlacesMapMove(listener) {
  listeners.add(listener);
}

export function placesInView(places) {
  if (!map) return places;
  const view = map.getBounds();
  return places.filter((place) => view.contains([place.lat, place.lon]));
}

export function initPlacesMap() {
  if (!window.L || map) return;
  map = L.map("places-map", { zoomControl: false, worldCopyJump: true }).setView([39.5, -98.35], 4);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19,
  }).addTo(map);
  heat = heatLayer([], { radius: 24 });
  dots = L.layerGroup();
  zones = L.layerGroup();
  routes = L.layerGroup();
  focus = L.layerGroup().addTo(map);
  map.on("moveend", () => {
    if (mode === "routines") loadRoutesSoon();
    updateDots();
    listeners.forEach((listener) => listener());
  });
  map.on("zoomend", updateDots);
  setPlacesMapMode(mode);
}

// The page became visible: Leaflet needs its size again, and the first time a view to start from.
export function showPlacesMap() {
  if (!map) initPlacesMap();
  if (!map) return;
  setTimeout(() => {
    map.invalidateSize();
    if (!fitted.has(mode)) fitToMode();
  }, 30);
}

// Where you spend most of your time: the places that hold nine tenths of it, not every far-off trip.
function everyday(places) {
  const ordered = [...places].sort((a, b) => (b.observed_minutes || 0) - (a.observed_minutes || 0));
  const total = ordered.reduce((sum, place) => sum + (place.observed_minutes || 0), 0);
  const kept = [];
  let sum = 0;
  for (const place of ordered) {
    if (kept.length >= 3 && sum >= total * 0.9) break;
    kept.push(place);
    sum += place.observed_minutes || 0;
  }
  return kept;
}

function fitToMode(all = false) {
  const items = mode === "saved" ? state.places : state.visitedPlaces;
  if (!items?.length) {
    if (state.current) map.setView([state.current.lat, state.current.lon], 12);
    return;
  }
  const shown = all || mode === "saved" ? items : everyday(items);
  map.fitBounds(L.latLngBounds(shown.map((place) => [place.lat, place.lon])).pad(0.2), { maxZoom: mode === "saved" ? 15 : 14 });
  fitted.add(mode);
}

// The Fit button: everything the tab holds, far-off trips too.
export function fitPlacesMap() {
  fitToMode(true);
}

function weightOf(place, most) {
  return Math.log1p(place.visits || 1) / Math.log1p(most);
}

// Small dots over the heat once you are close enough to tell places apart, to click for a name.
function updateDots() {
  if (!map || mode !== "visited") return;
  dots.clearLayers();
  if (map.getZoom() < 13) return;
  for (const place of placesInView(state.visitedPlaces || []).slice(0, 400)) {
    const info = KIND_INFO[kindOf(place)];
    L.circleMarker([place.lat, place.lon], {
      radius: 5,
      color: "#fff",
      weight: 2,
      fillColor: info.color,
      fillOpacity: 1,
    })
      .bindPopup(placePopup(place))
      .addTo(dots);
  }
}

function placePopup(place) {
  const details = [plural(place.visits, "visit"), durationText(place.observed_minutes)];
  return `<div class="map-popup"><strong>${safe(place.display_name || place.name)}</strong><span>${safe(details.join(" · "))}</span>${place.address && place.address !== place.display_name ? `<span>${safe(place.address)}</span>` : ""}</div>`;
}

export function drawVisited() {
  if (!map) return;
  const places = state.visitedPlaces || [];
  const most = Math.max(2, ...places.map((place) => place.visits || 1));
  heat.setPoints(places.map((place) => [place.lat, place.lon, weightOf(place, most)]));
  updateDots();
  if (!fitted.has(mode) && $("places-view").classList.contains("active")) fitToMode();
}

export function drawSaved() {
  if (!map) return;
  zones.clearLayers();
  for (const place of state.places || []) {
    const info = KIND_INFO[kindOf(place)];
    L.circle([place.lat, place.lon], {
      radius: place.radius_m,
      color: info.color,
      weight: 1.5,
      fillColor: info.color,
      fillOpacity: 0.12,
      interactive: false,
    }).addTo(zones);
    const icon = L.divIcon({
      className: "visit-pin saved-pin",
      html: `<span class="place-icon" style="--kind:${info.color}"><svg viewBox="0 0 24 24">${info.icon}</svg></span>`,
      iconSize: [36, 36],
      iconAnchor: [18, 18],
    });
    L.marker([place.lat, place.lon], { icon, title: place.name })
      .bindPopup(`<div class="map-popup"><strong>${safe(place.name)}</strong><span>${safe(radiusText(place.radius_m))} zone</span>${place.address ? `<span>${safe(place.address)}</span>` : ""}</div>`)
      .addTo(zones);
  }
}

function loadRoutesSoon() {
  clearTimeout(routeTimer);
  routeTimer = setTimeout(loadRoutes, 250);
}

let shownRoutes = [];

async function loadRoutes() {
  if (!map) return;
  const request = ++routeRequest;
  const view = map.getBounds();
  const query = new URLSearchParams({
    south: Math.max(-90, view.getSouth()),
    west: Math.max(-180, view.getWest()),
    north: Math.min(90, view.getNorth()),
    east: Math.min(180, view.getEast()),
    limit: 400,
  });
  try {
    const result = await api("/api/route-map?" + query);
    if (request !== routeRequest || mode !== "routines") return;
    shownRoutes = result.routes;
    drawRoutes();
  } catch (error) {
    toast(error.message);
  }
}

// Learned routes where there are any; else the usual trips between places as arcs.
function drawRoutes() {
  routes.clearLayers();
  const most = Math.max(1, ...shownRoutes.map((route) => route.trips));
  const ordered = [...shownRoutes].sort((a, b) => a.trips - b.trips);
  for (const route of ordered) {
    const share = Math.log1p(route.trips) / Math.log1p(Math.max(most, 2));
    const style = {
      color: rampColour(share),
      weight: 2.5 + share * 5,
      opacity: 0.45 + share * 0.45,
      lineCap: "round",
      lineJoin: "round",
    };
    for (const line of route.lines) {
      L.polyline(line, style)
        .bindTooltip(`${safe(route.from_name || "A place")} → ${safe(route.to_name || "a place")} · ${plural(route.trips, "trip")}`, { sticky: true })
        .addTo(routes);
    }
  }
  if (!shownRoutes.length) drawArcs();
  showLegend();
}

function arc(from, to) {
  // A gentle curve, so a trip there and the one back read as two.
  const [lat1, lon1] = from,
    [lat2, lon2] = to;
  const mid = [(lat1 + lat2) / 2 + (lon2 - lon1) * 0.18, (lon1 + lon2) / 2 - (lat2 - lat1) * 0.18];
  const points = [];
  for (let step = 0; step <= 24; step++) {
    const t = step / 24,
      u = 1 - t;
    points.push([u * u * lat1 + 2 * u * t * mid[0] + t * t * lat2, u * u * lon1 + 2 * u * t * mid[1] + t * t * lon2]);
  }
  return points;
}

function drawArcs() {
  const moves = (state.insights?.transitions || []).filter((move) => move.from_lat != null);
  const most = Math.max(2, ...moves.map((move) => move.count));
  for (const move of [...moves].reverse()) {
    const share = Math.log1p(move.count) / Math.log1p(most);
    L.polyline(arc([move.from_lat, move.from_lon], [move.to_lat, move.to_lon]), {
      color: rampColour(share),
      weight: 2 + share * 5,
      opacity: 0.5 + share * 0.4,
      lineCap: "round",
    })
      .bindTooltip(`${safe(move.from_name)} → ${safe(move.to_name)} · ${plural(move.count, "time")}`, { sticky: true })
      .addTo(routes);
  }
}

function showLegend() {
  const legend = $("places-map-legend");
  if (mode === "visited") {
    legend.innerHTML = '<span>Fewer visits</span><span class="heat-scale" aria-hidden="true"></span><span>More</span>';
  } else if (mode === "routines") {
    legend.innerHTML = shownRoutes.length
      ? '<span>Rarely taken</span><span class="heat-scale" aria-hidden="true"></span><span>Often</span>'
      : '<span>Rarely</span><span class="heat-scale" aria-hidden="true"></span><span>Often · usual trips, until routes are learned</span>';
  } else {
    legend.textContent = "Zones you saved, with their reach";
  }
}

// Switch what the map shows, with the tab.
export function setPlacesMapMode(next) {
  mode = next;
  if (!map) return;
  focus.clearLayers();
  for (const layer of [heat, dots, zones, routes]) if (map.hasLayer(layer)) map.removeLayer(layer);
  if (mode === "visited") {
    heat.addTo(map);
    dots.addTo(map);
    drawVisited();
  } else if (mode === "saved") {
    zones.addTo(map);
    drawSaved();
  } else {
    routes.addTo(map);
    loadRoutes();
  }
  $("places-map-note").textContent = { visited: "Where you have been", saved: "Your saved places", routines: "Routes you take" }[mode];
  showLegend();
  if (!fitted.has(mode) && $("places-view").classList.contains("active")) fitToMode();
}

// Move the map to a place from a list, with a ring around it.
export function focusPlace(place, zoom = 16) {
  if (!map || !place) return;
  focus.clearLayers();
  L.circleMarker([place.lat, place.lon], { radius: 16, color: "#0f766e", weight: 3, fill: false, interactive: false }).addTo(focus);
  if (place.radius_m && mode === "saved") {
    map.flyToBounds(L.latLng(place.lat, place.lon).toBounds(place.radius_m * 3), { maxZoom: 17, duration: 0.6 });
  } else {
    map.flyTo([place.lat, place.lon], Math.max(map.getZoom(), zoom), { duration: 0.6 });
  }
  document.querySelector(".places-map-shell")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// Show one usual trip: both places in view, and its routes drawn bold.
export function focusRoute(move) {
  if (!map || move.from_lat == null) return;
  focus.clearLayers();
  const ends = [
    [move.from_lat, move.from_lon],
    [move.to_lat, move.to_lon],
  ];
  map.flyToBounds(L.latLngBounds(ends).pad(0.3), { maxZoom: 15, duration: 0.6 });
  for (const end of ends)
    L.circleMarker(end, { radius: 7, color: "#fff", weight: 2.5, fillColor: "#0f766e", fillOpacity: 1, interactive: false }).addTo(focus);
  const same = (route) =>
    (route.from_name === move.from_name && route.to_name === move.to_name) ||
    (route.from_name === move.to_name && route.to_name === move.from_name);
  const matching = shownRoutes.filter(same);
  if (matching.length)
    for (const route of matching)
      for (const line of route.lines) L.polyline(line, { color: "#0f766e", weight: 7, opacity: 0.9, interactive: false }).addTo(focus);
  else L.polyline(arc(...ends), { color: "#0f766e", weight: 5, opacity: 0.9, dashArray: "2 9", interactive: false }).addTo(focus);
}
