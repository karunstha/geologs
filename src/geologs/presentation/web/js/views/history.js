// History: one day at a time, with its stats, map, timeline, and details.
import { api } from "../core/api.js";
import { $, copyText, safe, toast } from "../core/dom.js";
import {
  ago,
  clock,
  coordinateText,
  dayTitle,
  distanceText,
  durationText,
  formatTime,
  localDateKey,
  fuelText,
  plural,
  shiftDateKey,
  zonedMidnight,
} from "../core/format.js";
import { state } from "../core/state.js";
import { drawMap, drawSelection, fitDay, map, revealMap, selectedItem } from "../map/map.js";
import { modeIcon, modeInfo, placeIcon, sourceName } from "../ui/icons.js";
import { initAmbient, refreshAmbient } from "./ambient.js";
import { initDayDetails, renderDayDetails } from "./day-details.js";
import { showSettingsPage } from "./settings.js";
import { showView } from "./shell.js";
import { renderTimeline } from "./timeline.js";

let exploreRequest = 0;

export function params() {
  const date = $("day-date").value;
  const start = zonedMidnight(date);
  const end = zonedMidnight(shiftDateKey(date, 1));
  const query = new URLSearchParams({
    start: start.toISOString(),
    end: new Date(end.getTime() - 1).toISOString(),
  });
  return query;
}

function renderStats(day) {
  $("stat-distance").textContent = distanceText(day.movement_km ?? day.stats.distance_km);
  $("stat-moving").textContent = durationText(day.movement_minutes);
  $("stat-places").textContent = day.visited_places.length.toLocaleString();
  $("stat-trips").textContent = day.trips.length.toLocaleString();
  $("stat-fuel-cell").hidden = !day.fuel;
  if (day.fuel) {
    $("stat-fuel").textContent = fuelText(day.fuel.cost, day.fuel.litres, day.fuel.currency);
    $("stat-fuel").title = `Over ${plural(day.fuel.drives, "drive")}`;
  }
  const activity = Object.entries(day.activity_totals)
    .filter(([, totals]) => totals.count)
    .map(
      ([mode, totals]) =>
        `<span class="chip" style="--mode:${modeInfo[mode].color}">${modeIcon(mode)}<strong>${modeInfo[mode].noun}</strong><span>${distanceText(totals.distance_km)} · ${durationText(totals.minutes)}</span></span>`,
    )
    .join("");
  $("activity-chips").innerHTML = activity;
  document.querySelector(".history-activity").hidden = !activity;
  // Trips each have their own colour (see tripColors); area stays keep their activity's.
  const modes = new Set(day.stays.map((stay) => stay.activity).filter(Boolean));
  $("map-legend").innerHTML =
    '<span><i class="legend-line" style="--mode:#8ea8a2"></i>Recorded path</span>' +
    (day.trips.length ? '<span><i class="legend-line legend-trips"></i>Trips, light to dark</span>' : "") +
    [...modes]
      .filter((mode) => modeInfo[mode])
      .map(
        (mode) =>
          `<span><i class="legend-line" style="--mode:${modeInfo[mode].color}"></i>${modeInfo[mode].noun}</span>`,
      )
      .join("") +
    (day.trips.some((trip) => trip.route_fills?.length)
      ? '<span><i class="legend-line legend-fill" style="--mode:#8ea8a2"></i>Likely route</span>'
      : "") +
    (day.trips.some((trip) => trip.route_gaps?.length)
      ? '<span><i class="legend-line legend-gap" style="--mode:#8ea8a2"></i>Unrecorded</span>'
      : "");
}

// The most recent local day any source recorded, for "jump to latest".
function latestDayWithData(coverage) {
  const last = coverage.sources
    .map((source) => source.last)
    .filter(Boolean)
    .sort()
    .at(-1);
  return last ? localDateKey(new Date(last)) : null;
}

function renderEmptyDay(day, coverage) {
  const hasArchive = coverage.sources.some((source) => source.points > 0);
  const latest = latestDayWithData(coverage);
  const today = localDateKey();
  const actions = [];
  if (!hasArchive) {
    $("day-empty-title").textContent = "No locations yet";
    $("day-empty-copy").textContent =
      "Connect your phone or a tracker app, and each day appears here with its places, trips, and route.";
    actions.push(
      '<button type="button" class="button primary" data-empty-action="setup">Set up a tracker</button>',
    );
  } else {
    $("day-empty-title").textContent =
      day.date === today ? "Nothing recorded yet today" : `Nothing recorded on ${dayTitle(day.date)}`;
    $("day-empty-copy").textContent =
      day.date === today
        ? "Locations appear here as soon as one of your devices sends them."
        : "No device sent a location on this day.";
    // Offer the latest day with data, and today when that's a different day.
    if (latest && latest !== day.date && latest !== today)
      actions.push(
        `<button type="button" class="button primary" data-empty-action="latest" data-date="${latest}">Go to ${safe(dayTitle(latest))}</button>`,
      );
    if (day.date !== today)
      actions.push(
        `<button type="button" class="button ${actions.length ? "secondary" : "primary"}" data-empty-action="today">Go to today</button>`,
      );
  }
  $("day-empty-actions").innerHTML = actions.join("");
  $("day-empty-actions")
    .querySelectorAll("[data-empty-action]")
    .forEach((button) => {
      button.onclick = () => {
        const action = button.dataset.emptyAction;
        if (action === "setup") {
          showView("settings");
          showSettingsPage("settings-sources");
        } else goToDate(action === "today" ? today : button.dataset.date);
      };
    });
}

export function renderExplore(day, coverage) {
  const hasHistory = day.points > 0;
  const showMap = hasHistory || !!state.focusPlace;
  $("history-title").textContent = day.date === localDateKey() ? "Today" : dayTitle(day.date);
  $("day-context").textContent = hasHistory
    ? [
        day.points === 1
          ? `One fix at ${clock(day.first_fix)}`
          : `${clock(day.first_fix)} – ${clock(day.last_fix)}`,
        plural(day.points, "sample"),
        (day.timeline_sources || []).length > 1 ? plural(day.timeline_sources.length, "source") : "",
      ]
        .filter(Boolean)
        .join(" · ")
    : "";
  $("day-context").hidden = !hasHistory;
  $("next-day").disabled = day.date >= localDateKey();
  $("today").disabled = day.date === localDateKey();
  $("day-empty").hidden = showMap;
  $("summary").hidden = !hasHistory;
  $("history-layout").hidden = !showMap;
  $("history-layout").classList.toggle("map-only", !hasHistory);
  $("inspector").hidden = !hasHistory;
  $("history-details").hidden = !hasHistory;
  $("map-footer").hidden = !day.route_segments.length;
  $("locate").hidden = !(state.current || day.last_location);
  if (!showMap) renderEmptyDay(day, coverage);
  // Keep the map where the viewer left it on background refreshes; fit only for a new day or source.
  const viewKey = `${day.date}|${day.source || ""}`;
  const newView = state.fitKey !== viewKey;
  if (newView) {
    state.fitKey = viewKey;
    state.selected = null;
  }
  let zoomToSelection = false;
  if (state.pendingStayId != null) {
    state.selected = { type: "stay", key: state.pendingStayId };
    state.pendingStayId = null;
    zoomToSelection = true;
  }
  if (!selectedItem()) state.selected = null;
  renderStats(day);
  refreshAmbient(day);
  renderTimeline(day);
  renderDayDetails(day);
  $("map-detail-section").hidden = true;
  const redraw = () => {
    drawMap(newView && !zoomToSelection);
    if (zoomToSelection) {
      drawSelection(true);
      document.querySelector("#day-timeline .selected")?.scrollIntoView({ block: "nearest" });
    }
  };
  if (map && (newView || zoomToSelection))
    setTimeout(() => {
      map.invalidateSize();
      redraw();
    }, 20);
  else redraw();
}

export function renderCurrent() {
  const point = state.current;
  $("current-location").hidden = !point;
  $("current-meta").hidden = !point;
  if (!point) return;
  $("current-location").classList.toggle("stale", !!point.stale);
  $("current-place").innerHTML =
    placeIcon({ ...point, name: point.place_name }) +
    `<span>${safe(point.place_name || point.address || "Unknown place")}</span>`;
  const source = sourceName(point.source);
  $("current-updated").textContent = [
    `Updated ${ago(point.timestamp)}`,
    source.device || source.kind,
    point.stale ? "may be out of date" : "",
  ]
    .filter(Boolean)
    .join(" · ");
  $("current-updated").title = formatTime(point.timestamp);
  $("current-coordinates").textContent = coordinateText(point.lat, point.lon);
}

export function focusPlaceOnMap(place) {
  state.focusPlace = place;
  state.selected = null;
  const wasOpen = $("explore-view").classList.contains("active") && !$("history-layout").hidden;
  showView("explore");
  // Re-render only when the map was hidden; otherwise the lists keep their scroll position.
  if (!wasOpen && state.day && state.coverage) renderExplore(state.day, state.coverage);
  else drawMap(false);
  $("map-note").textContent = place.name || place.display_name || "Selected location";
  setTimeout(() => {
    map?.invalidateSize();
    map?.setView([place.lat, place.lon], 16);
    revealMap();
  }, 30);
}

export async function refreshExplore(resetRawPage = true) {
  const request = ++exploreRequest;
  if (resetRawPage) state.rawPage = 0;
  const date = $("day-date").value;
  const timezone = state.settings?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const dayQuery = new URLSearchParams({ date, timezone });
  const query = params();
  const rawQuery = new URLSearchParams(query);
  rawQuery.set("limit", state.rawPageSize);
  rawQuery.set("offset", state.rawPage * state.rawPageSize);
  // Background refreshes stay quiet; a day or source change dims the page while it loads.
  if (resetRawPage) $("explore-view").classList.add("loading");
  let day, timeline, points, coverage;
  try {
    [day, timeline, points, coverage] = await Promise.all([
      api("/api/day?" + dayQuery),
      api("/api/timeline?" + query),
      api("/api/observations?" + rawQuery),
      api("/api/coverage"),
    ]);
  } finally {
    if (request === exploreRequest) $("explore-view").classList.remove("loading");
  }
  if (request !== exploreRequest) return;
  state.day = day;
  state.timeline = timeline;
  state.points = points;
  state.rawTotal = day.points;
  state.coverage = coverage;
  // A past day is bookmarkable as #date=YYYY-MM-DD; today keeps a clean URL.
  const hash = day.date === localDateKey() ? "" : "#date=" + day.date;
  if (location.hash !== hash) history.replaceState(null, "", location.pathname + location.search + hash);
  renderExplore(day, coverage);
}

export async function refreshCurrent() {
  state.current = await api("/api/current");
  renderCurrent();
  drawMap();
  refreshAmbient();
}

function goToDate(date) {
  state.focusPlace = null;
  $("day-date").value = date;
  state.followToday = date === localDateKey();
  showView("explore");
  refreshExplore().catch((error) => toast(error.message));
}

export function moveDay(offset) {
  goToDate(shiftDateKey($("day-date").value, offset));
}

export function linkedDate() {
  const match = location.hash.match(/date=(\d{4}-\d{2}-\d{2})/);
  return match && match[1] <= localDateKey() ? match[1] : null;
}

export function initHistory() {
  initAmbient();
  initDayDetails();
  $("day-date").max = localDateKey();
  $("day-date").onchange = () => {
    if ($("day-date").value) goToDate($("day-date").value);
  };
  $("previous-day").onclick = () => moveDay(-1);
  $("next-day").onclick = () => moveDay(1);
  $("today").onclick = () => goToDate(localDateKey());
  $("locate").onclick = () => {
    const point = state.current || state.day?.last_location;
    if (point && map) map.setView([point.lat, point.lon], 15);
  };
  $("current-coordinates").onclick = () => {
    if (state.current) copyText(coordinateText(state.current.lat, state.current.lon), "Coordinates copied");
  };
  $("day-date").value = localDateKey();
  $("fit-day").onclick = () => {
    state.selected = null;
    drawSelection(false);
    fitDay();
  };
  $("close-map-detail").onclick = () => {
    $("map-detail-section").hidden = true;
  };
  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (!$("explore-view").classList.contains("active") || !$("auth-panel").hidden) return;
    if (document.querySelector("dialog[open]") || event.target.closest("input,select,textarea,#map")) return;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      moveDay(-1);
    } else if (event.key === "ArrowRight" && !$("next-day").disabled) {
      event.preventDefault();
      moveDay(1);
    } else if (event.key === "Escape" && state.selected) {
      state.selected = null;
      drawSelection(false);
      fitDay();
    }
  });
  window.addEventListener("hashchange", () => {
    const date = linkedDate();
    if (date && date !== $("day-date").value) {
      state.followToday = false;
      goToDate(date);
    }
  });
  if (linkedDate()) {
    state.followToday = false;
    $("day-date").value = linkedDate();
  }
}
