// The Leaflet map: recorded paths, places, the current location, and the selected stay or trip.
import { api } from "../core/api.js";
import { $, copyText, safe, toast } from "../core/dom.js";
import { clock, coordinateText, durationText, formatTime, kmh, lengthText, localDateKey } from "../core/format.js";
import { state } from "../core/state.js";
import { modeInfo, modeName, placeIcons } from "../ui/icons.js";
import { params } from "../views/history.js";
import { dayVisits, editVisitedPlace } from "../views/place-dialogs.js";
import { renderModePicker, stayWhen } from "../views/timeline.js";

export let map, pathLayer, placeLayer, highlightLayer, currentMarker, searchLayer;

export function initMap() {
  if (!window.L) {
    $("map").innerHTML =
      '<div class="empty" style="padding:30px">Map library could not load. Check your internet connection.</div>';
    return;
  }
  map = L.map("map", { zoomControl: false }).setView([39.5, -98.35], 4);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19,
  }).addTo(map);
  map.on("popupopen", (event) => {
    const element = event.popup.getElement();
    element
      ?.querySelectorAll("[data-copy]")
      .forEach((button) => (button.onclick = () => copyText(button.dataset.copy, "Coordinates copied")));
    element?.querySelectorAll("[data-edit-place]").forEach(
      (button) =>
        (button.onclick = () => {
          map.closePopup();
          const place = state.day?.places_including_home.find(
            (item) => item.cluster_id === +button.dataset.editPlace,
          );
          editVisitedPlace(place, dayVisits(place));
        }),
    );
    element?.querySelectorAll("[data-edit-stay]").forEach(
      (button) =>
        (button.onclick = () => {
          map.closePopup();
          const stay = state.day?.stays.find((item) => item.id === +button.dataset.editStay);
          if (stay) editVisitedPlace({ ...stay, name: stay.display_name }, [stay]);
        }),
    );
  });
  pathLayer = L.layerGroup().addTo(map);
  placeLayer = L.layerGroup().addTo(map);
  highlightLayer = L.layerGroup().addTo(map);
  searchLayer = L.layerGroup().addTo(map);
  map.on("click", async (e) => {
    const q = params();
    q.set("lat", e.latlng.lat);
    q.set("lon", e.latlng.lng);
    q.set("radius_m", 250);
    q.set("limit", 10);
    try {
      const rows = await api("/api/near?" + q);
      $("map-detail-section").hidden = false;
      $("map-detail").innerHTML =
        rows
          .map(
            (p) =>
              `<div class="list-item"><div><strong>${safe(formatTime(p.timestamp))}</strong><small>${safe(modeName(p.mode))} · ${safe(kmh(p))} · ${lengthText(p.distance_m)} away · ${safe(coordinateText(p.lat, p.lon))}</small></div></div>`,
          )
          .join("") || `<p class="empty">No recorded fixes within ${lengthText(250)} on this day.</p>`;
    } catch (err) {
      toast(err.message);
    }
  });
}

export function mapPath(segment) {
  return segment.map((c) => [c[1], c[0]]);
}

export function pointPopup(title, lines, lat, lon, edit = "") {
  // Every point on the map shows its coordinates, ready to copy; visits can be renamed from here.
  const coordinates = coordinateText(lat, lon);
  return `<div class="map-popup"><strong>${safe(title)}</strong>${lines
    .filter(Boolean)
    .map((line) => `<span>${safe(line)}</span>`)
    .join(
      "",
    )}<div class="popup-actions"><button type="button" class="popup-copy" data-copy="${safe(coordinates)}" title="Copy coordinates">${safe(coordinates)}</button>${edit}</div></div>`;
}

// A pin stands for a place (all its visits); a selected stay is one visit's spot.
export function editPlaceAction(clusterId) {
  return `<button type="button" class="popup-action" data-edit-place="${clusterId}">Rename place</button>`;
}

export function editStayAction(stayId) {
  return `<button type="button" class="popup-action" data-edit-stay="${stayId}">Rename this visit</button>`;
}

export function tripKey(trip) {
  return `${trip.from_stay_id}-${trip.to_stay_id}`;
}

export function stayColor(stay) {
  return modeInfo[stay.activity]?.color || "#176d69";
}

export function selectedItem() {
  const day = state.day,
    selected = state.selected;
  if (!day || !selected) return null;
  if (selected.type === "stay") {
    const stay = day.stays.find((item) => item.id === selected.key);
    return stay ? { type: "stay", stay } : null;
  }
  const trip = day.trips.find((item) => tripKey(item) === selected.key);
  return trip ? { type: "trip", trip } : null;
}

export function dayBounds() {
  const day = state.day,
    bounds = [];
  for (const segment of day?.route_segments || []) bounds.push(...mapPath(segment));
  for (const place of day?.places_including_home || []) bounds.push([place.lat, place.lon]);
  if (state.current && (day?.date === localDateKey() || !day?.points))
    bounds.push([state.current.lat, state.current.lon]);
  return bounds;
}

export function fitDay() {
  if (!map) return;
  const bounds = dayBounds();
  if (bounds.length) map.fitBounds(L.latLngBounds(bounds).pad(0.12), { maxZoom: 15 });
  else if (state.places.length)
    map.fitBounds(L.latLngBounds(state.places.map((p) => [p.lat, p.lon])).pad(0.2), { maxZoom: 13 });
}

export function drawMap(fit = false) {
  if (!map) return;
  pathLayer.clearLayers();
  placeLayer.clearLayers();
  if (currentMarker) {
    map.removeLayer(currentMarker);
    currentMarker = null;
  }
  const day = state.day;
  // The whole recorded path sits underneath; trips and area stays draw over it in their mode colour.
  for (const segment of day?.route_segments || [])
    L.polyline(mapPath(segment), { color: "#8ea8a2", weight: 3, opacity: 0.7, interactive: false }).addTo(
      pathLayer,
    );
  for (const trip of day?.trips || []) {
    for (const segment of trip.route_segments || [])
      L.polyline(mapPath(segment), {
        color: modeInfo[trip.mode]?.color || "#176d69",
        weight: 4,
        opacity: 0.9,
      })
        .on("click", () => selectItem("trip", tripKey(trip)))
        .addTo(pathLayer);
  }
  for (const stay of day?.stays || []) {
    for (const move of stay.internal_moves || [])
      for (const segment of move.route_segments || [])
        L.polyline(mapPath(segment), {
          color: modeInfo[move.mode]?.color || "#176d69",
          weight: 3,
          opacity: 0.8,
          dashArray: "6 6",
        })
          .on("click", () => selectItem("stay", stay.id))
          .addTo(pathLayer);
    if (stay.kind !== "area") continue;
    L.circle([stay.lat, stay.lon], {
      radius: stay.radius_m || 50,
      color: stayColor(stay),
      weight: 1,
      opacity: 0.45,
      fillOpacity: 0.05,
      interactive: false,
    }).addTo(pathLayer);
    for (const segment of stay.route_segments || [])
      L.polyline(mapPath(segment), { color: stayColor(stay), weight: 4, opacity: 0.9 })
        .on("click", () => selectItem("stay", stay.id))
        .addTo(pathLayer);
  }
  for (const place of state.places) {
    L.circle([place.lat, place.lon], {
      radius: place.radius_m,
      color: "#9aaba5",
      weight: 1,
      fillOpacity: 0.04,
    })
      .bindPopup(
        pointPopup(
          place.name,
          ["Saved place · " + lengthText(place.radius_m) + " zone"],
          place.lat,
          place.lon,
        ),
      )
      .addTo(placeLayer);
  }
  if (state.focusPlace) {
    const label = state.focusPlace.name || state.focusPlace.display_name || "Observed place";
    L.circleMarker([state.focusPlace.lat, state.focusPlace.lon], {
      radius: 9,
      color: "#fff",
      weight: 3,
      fillColor: "#176d69",
      fillOpacity: 1,
    })
      .bindPopup(
        pointPopup(
          label,
          [state.focusPlace.timestamp ? formatTime(state.focusPlace.timestamp) : ""],
          state.focusPlace.lat,
          state.focusPlace.lon,
        ),
      )
      .addTo(placeLayer);
  }
  const numbers = new Map((day?.visited_places || []).map((place, index) => [place.cluster_id, index + 1]));
  for (const place of day?.places_including_home || []) {
    const number = numbers.get(place.cluster_id);
    const icon = L.divIcon({
      className: "visit-pin" + (number ? "" : " home"),
      html: number ? String(number) : placeIcons.home,
      iconSize: [28, 28],
      iconAnchor: [14, 14],
    });
    const minutes = durationText(place.observed_minutes);
    L.marker([place.lat, place.lon], { icon, title: place.name || place.display_name })
      .bindPopup(
        pointPopup(
          place.name || place.display_name,
          [
            place.address && place.address !== place.name ? place.address : "",
            minutes + " · " + place.visits + " visit" + (place.visits === 1 ? "" : "s"),
          ],
          place.lat,
          place.lon,
          editPlaceAction(place.cluster_id),
        ),
      )
      .addTo(placeLayer);
  }
  if (state.current) {
    const point = state.current;
    currentMarker = L.circleMarker([point.lat, point.lon], {
      radius: 7,
      color: "#fff",
      weight: 3,
      fillColor: "#dc7650",
      fillOpacity: 1,
    })
      .bindPopup(
        pointPopup(
          point.place_name || point.address || "Current location",
          ["Updated " + formatTime(point.timestamp)],
          point.lat,
          point.lon,
        ),
      )
      .addTo(map);
  }
  if (fit) fitDay();
  drawSelection(false);
}

export function drawSelection(zoom) {
  highlightLayer?.clearLayers();
  const item = selectedItem();
  const key = state.selected ? `${state.selected.type}:${state.selected.key}` : null;
  document.querySelectorAll("#day-timeline [data-item]").forEach((row) => {
    const active = row.dataset.item === key;
    row.classList.toggle("selected", active);
    row.setAttribute("aria-pressed", String(active));
  });
  renderModePicker();
  $("fit-day").hidden = !item;
  if (!item) {
    if (state.day) $("map-note").textContent = mapNote(state.day);
    return;
  }
  const subject = item.type === "trip" ? item.trip : item.stay;
  const color = item.type === "trip" ? modeInfo[item.trip.mode]?.color || "#176d69" : stayColor(item.stay);
  const tripNote = (trip) =>
    trip.loop
      ? `Round trip from ${trip.from_name || "stay"}${trip.turnaround?.name ? " via " + trip.turnaround.name : ""}`
      : `${modeInfo[trip.mode]?.label || "Trip"}: ${trip.from_name || "Stay"} → ${trip.to_name || "Stay"}`;
  $("map-note").textContent = item.type === "trip" ? tripNote(item.trip) : item.stay.display_name || "Stay";
  if (!map) return;
  const lines = [
    ...(subject.route_segments || []),
    ...(subject.internal_moves || []).flatMap((move) => move.route_segments || []),
  ].map(mapPath);
  for (const line of lines) {
    L.polyline(line, { color: "#fff", weight: 10, opacity: 0.95, interactive: false }).addTo(highlightLayer);
    L.polyline(line, { color, weight: 6, opacity: 1, interactive: false }).addTo(highlightLayer);
  }
  if (item.type === "stay")
    L.circleMarker([item.stay.lat, item.stay.lon], {
      radius: item.stay.kind === "area" ? 6 : 10,
      color: "#fff",
      weight: 3,
      fillColor: color,
      fillOpacity: 1,
    })
      .bindPopup(
        pointPopup(
          item.stay.display_name || "Stay",
          [stayWhen(item.stay, state.day) + " · " + durationText(item.stay.observed_minutes)],
          item.stay.lat,
          item.stay.lon,
          editStayAction(item.stay.id),
        ),
      )
      .addTo(highlightLayer);
  const turn = item.type === "trip" && item.trip.loop ? item.trip.turnaround : null;
  if (turn)
    L.circleMarker([turn.lat, turn.lon], {
      radius: 8,
      color: "#fff",
      weight: 3,
      fillColor: color,
      fillOpacity: 1,
    })
      .bindPopup(
        pointPopup(
          turn.stop_minutes ? turn.name || "Stop" : "Farthest point",
          [
            turn.stop_minutes
              ? `Stopped ${durationText(turn.stop_minutes)} · ${clock(turn.arrived_at)}`
              : clock(turn.arrived_at),
          ],
          turn.lat,
          turn.lon,
        ),
      )
      .addTo(highlightLayer);
  if (!zoom) return;
  if (item.type === "stay" && item.stay.kind === "area" && !item.stay.internal_moves?.length)
    map.fitBounds(L.latLng(item.stay.lat, item.stay.lon).toBounds((item.stay.radius_m || 100) * 2.4), {
      maxZoom: 16,
    });
  else if (lines.flat().length > 1) map.fitBounds(L.latLngBounds(lines.flat()).pad(0.2), { maxZoom: 16 });
  else map.setView([subject.lat ?? subject.from_lat, subject.lon ?? subject.from_lon], 16);
}

export function selectItem(type, key) {
  const same = state.selected?.type === type && state.selected?.key === key;
  state.selected = same ? null : { type, key };
  state.focusPlace = null;
  drawMap(false);
  if (same) fitDay();
  else drawSelection(true);
  revealMap();
}

export function mapNote(day) {
  return day.route_segments.length
    ? day.route_segments.length + " recorded path segment" + (day.route_segments.length === 1 ? "" : "s")
    : day.points
      ? day.timeline_points.toLocaleString() + " key fixes"
      : state.focusPlace
        ? "Location: " + (state.focusPlace.name || state.focusPlace.display_name || "selected fix")
        : "Recorded route";
}

export function revealMap() {
  // Bring the map on screen only when it is not already fully visible, so a list below keeps its place.
  const shell = document.querySelector(".map-shell"),
    box = shell.getBoundingClientRect();
  if (box.top < 0 || box.bottom > window.innerHeight)
    shell.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
