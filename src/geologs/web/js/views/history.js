// History: a day's stats, map, timeline, samples, patterns, and coverage.
import { api } from "../core/api.js";
import { $, copyText, safe, toast } from "../core/dom.js";
import {
  ago,
  clock,
  coordinateText,
  distanceText,
  durationText,
  formatPrecise,
  formatTime,
  kmh,
  localDateKey,
  shiftDateKey,
  zonedMidnight,
} from "../core/format.js";
import { state } from "../core/state.js";
import { drawMap, drawSelection, fitDay, map, revealMap, selectedItem } from "../map/map.js";
import { modeIcon, modeInfo, modeName, pinBadge, placeIcon, sourceName, zoneText } from "../ui/icons.js";
import { editVisitedPlace } from "./place-dialogs.js";
import { showView } from "./shell.js";
import { renderTimeline } from "./timeline.js";

let exploreRequest = 0;

export function roleName(role) {
  return (
    { arrival: "Arrival", departure: "Departure", latest: "Latest at stop", movement: "Movement" }[role] ||
    "Raw sample"
  );
}

export function params() {
  const date = $("day-date").value;
  const start = zonedMidnight(date);
  const end = zonedMidnight(shiftDateKey(date, 1));
  const q = new URLSearchParams({
    start: start.toISOString(),
    end: new Date(end.getTime() - 1).toISOString(),
  });
  if ($("source").value) q.set("source", $("source").value);
  return q;
}

export function renderChart() {
  const points = [...state.timeline]
    .reverse()
    .filter((p) => p.speed_mps != null)
    .slice(-100);
  const box = $("speed-chart");
  if (points.length < 2) {
    box.innerHTML = '<span class="empty">More timed samples are needed for a speed chart.</span>';
    return;
  }
  const top = Math.max(10, ...points.map((p) => p.speed_mps * 3.6));
  const poly = points
    .map(
      (p, i) =>
        `${((i / (points.length - 1)) * 600).toFixed(1)},${(110 - ((p.speed_mps * 3.6) / top) * 94).toFixed(1)}`,
    )
    .join(" ");
  box.innerHTML = `<svg viewBox="0 0 600 125" preserveAspectRatio="none" aria-label="Speed chart"><line x1="0" y1="110" x2="600" y2="110" stroke="#dce6eb"/><line x1="0" y1="63" x2="600" y2="63" stroke="#edf2f5"/><polyline fill="none" stroke="#177c91" stroke-width="3" points="${poly}"/><text x="0" y="12" fill="#98a7b4" font-size="10">${top.toFixed(0)} km/h</text><text x="0" y="123" fill="#98a7b4" font-size="10">${safe(formatTime(points[0].timestamp))}</text></svg>`;
}

export function renderSamples() {
  const rows = state.sampleView === "timeline" ? state.timeline : state.points;
  const start = state.rawPage * state.rawPageSize;
  $("sample-count").textContent =
    state.sampleView === "timeline"
      ? `${state.timeline.length.toLocaleString()} informational fixes from ${state.rawTotal.toLocaleString()} raw samples. Stationary periods keep their edges.`
      : rows.length
        ? `Showing ${(start + 1).toLocaleString()}–${(start + rows.length).toLocaleString()} of ${state.rawTotal.toLocaleString()} raw samples.`
        : `No raw samples on this page.`;
  const visible = state.sampleView === "timeline" ? rows.slice(0, 100) : rows;
  $("points-table").innerHTML =
    visible
      .map(
        (p, index) =>
          "<tr><td>" +
          safe(formatPrecise(p.timestamp)) +
          "</td><td>" +
          safe(state.sampleView === "timeline" ? roleName(p.role) : "Raw sample") +
          "</td><td>" +
          safe(p.source) +
          '</td><td><span class="mode-tag ' +
          safe(p.mode) +
          '">' +
          safe(modeName(p.mode)) +
          "</span></td><td>" +
          safe(kmh(p)) +
          (p.speed_origin === "estimated"
            ? " <small>est.</small>"
            : p.speed_origin === "reported_adjusted"
              ? " <small>adjusted</small>"
              : "") +
          "</td><td>" +
          p.lat.toFixed(5) +
          ", " +
          p.lon.toFixed(5) +
          "</td><td>" +
          (p.accuracy_m == null ? "—" : Math.round(p.accuracy_m) + " m") +
          '</td><td><button class="table-map" data-sample-map="' +
          index +
          '">Map</button></td></tr>',
      )
      .join("") || '<tr><td colspan="8" class="empty">No fixes on this day.</td></tr>';
  $("points-table")
    .querySelectorAll("[data-sample-map]")
    .forEach(
      (button) =>
        (button.onclick = () =>
          focusPlaceOnMap({
            ...visible[+button.dataset.sampleMap],
            display_name: formatTime(visible[+button.dataset.sampleMap].timestamp),
          })),
    );
  $("raw-pagination").hidden = state.sampleView !== "raw" || state.rawTotal <= state.rawPageSize;
  $("raw-page").textContent =
    `Page ${state.rawPage + 1} of ${Math.max(1, Math.ceil(state.rawTotal / state.rawPageSize))}`;
  $("raw-previous").disabled = state.rawPage === 0;
  $("raw-next").disabled = start + rows.length >= state.rawTotal;
}

export function renderDayPlaces(day) {
  const numbers = new Map(day.visited_places.map((place, index) => [place.cluster_id, index + 1]));
  $("visited-places").innerHTML =
    day.places_including_home
      .map((place) => {
        const activities = Object.entries(place.activities || {})
          .map(([mode, count]) => modeName(mode) + (count > 1 ? ` ×${count}` : ""))
          .join(", ");
        const details = [
          zoneText(place.zone, place.name || place.display_name),
          `First seen ${clock(place.first_seen)}`,
          `${durationText(place.observed_minutes)} observed`,
        ].filter(Boolean);
        if (place.visits > 1) details.push(`${place.visits} visits`);
        if (activities) details.push(activities);
        return `<div class="list-item"><div class="place-main">${pinBadge(numbers.get(place.cluster_id))}<div><strong>${safe(place.name || place.display_name)}</strong><small>${safe(details.join(" · "))}${place.address && place.address !== place.name ? "<br>" + safe(place.address) : ""}</small></div></div><div class="actions"><button data-day-place="${place.cluster_id}">Map</button><button data-edit-day-place="${place.cluster_id}">Edit</button></div></div>`;
      })
      .join("") || '<p class="empty">No confirmed stays on this day. Sparse fixes may miss a visit.</p>';
  $("visited-places")
    .querySelectorAll("[data-day-place]")
    .forEach(
      (button) =>
        (button.onclick = () => {
          const place = day.places_including_home.find(
            (item) => item.cluster_id === +button.dataset.dayPlace,
          );
          focusPlaceOnMap({ ...place, display_name: place.name || place.address || "Visited place" });
        }),
    );
  $("visited-places")
    .querySelectorAll("[data-edit-day-place]")
    .forEach(
      (button) =>
        (button.onclick = () =>
          editVisitedPlace(
            day.places_including_home.find((item) => item.cluster_id === +button.dataset.editDayPlace),
          )),
    );
}

export function renderSummary(day) {
  $("stat-distance").textContent = distanceText(day.movement_km ?? day.stats.distance_km);
  $("stat-moving").textContent = durationText(day.movement_minutes);
  $("stat-places").textContent = day.visited_places.length.toLocaleString();
  $("stat-trips").textContent = day.trips.length.toLocaleString();
  const activity = Object.entries(day.activity_totals)
    .filter(([, totals]) => totals.count)
    .map(
      ([mode, totals]) =>
        `<span class="chip" style="--mode:${modeInfo[mode].color}">${modeIcon(mode)}<strong>${modeInfo[mode].noun}</strong><span>${distanceText(totals.distance_km)} · ${durationText(totals.minutes)}</span></span>`,
    )
    .join("");
  $("activity-chips").innerHTML = activity;
  document.querySelector(".history-activity").hidden = !activity;
  const modes = new Set([
    ...day.trips.map((trip) => trip.mode),
    ...day.stays
      .flatMap((stay) => [stay.activity, ...(stay.internal_moves || []).map((move) => move.mode)])
      .filter(Boolean),
  ]);
  $("map-legend").innerHTML =
    '<span><i class="legend-line" style="--mode:#8ea8a2"></i>Recorded path</span>' +
    [...modes]
      .filter((mode) => modeInfo[mode])
      .map(
        (mode) =>
          `<span><i class="legend-line" style="--mode:${modeInfo[mode].color}"></i>${modeInfo[mode].noun}</span>`,
      )
      .join("");
}

export function renderExplore(day, coverage) {
  const hasHistory = day.points > 0;
  const hasArchive = coverage.sources.some((source) => source.points > 0);
  const hasMapContent = hasHistory || !!state.focusPlace || !!state.current;
  const [year, month, dayNumber] = day.date.split("-").map(Number);
  const chosen = new Date(year, month - 1, dayNumber, 12);
  $("history-title").textContent =
    day.date === localDateKey()
      ? "Today"
      : new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric" }).format(chosen);
  $("source-filter").hidden = coverage.sources.length < 2;
  $("next-day").disabled = day.date >= localDateKey();
  $("today").disabled = day.date === localDateKey();
  const empty = $("history-empty");
  empty.querySelector("strong").textContent = hasArchive ? "No fixes for this day" : "No locations recorded";
  $("history-empty-copy").textContent = hasArchive
    ? "Choose another date to explore your history."
    : "Connect a location source to see your history here.";
  $("summary").hidden = !hasArchive;
  $("history-details").hidden = !hasHistory;
  $("inspector").hidden = !hasHistory;
  $("history-empty").hidden = hasMapContent;
  $("map-footer").hidden = !day.route_segments.length;
  $("export").hidden = !hasHistory;
  $("locate").hidden = !(state.current || day.last_location);
  $("history-layout").classList.toggle("no-data", !hasMapContent);
  $("history-layout").classList.toggle("map-only", !hasHistory);
  $("day-context").hidden = false;
  $("day-context").textContent = hasHistory
    ? (day.points === 1
        ? "One fix at " + clock(day.first_fix)
        : "Fixes from " + clock(day.first_fix) + " to " + clock(day.last_fix)) +
      " · " +
      day.points.toLocaleString() +
      " samples · gaps are unobserved"
    : hasArchive
      ? "No fixes were recorded on this date. Choose another day to keep exploring."
      : "Connect a location source to begin building your private history.";
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
  renderSummary(day);
  renderTimeline(day);
  renderDayPlaces(day);
  $("map-detail-section").hidden = true;
  renderSamples();
  renderChart();
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
  if (!point) return;
  $("current-location").classList.toggle("stale", !!point.stale);
  $("current-place").innerHTML =
    placeIcon({ ...point, name: point.place_name }) +
    "<span>" +
    safe(point.place_name || point.address || "Unknown place") +
    "</span>";
  const source = sourceName(point.source);
  $("current-updated").textContent = [
    `Updated ${ago(point.timestamp)}`,
    source.device || source.kind,
    point.stale ? "may be out of date" : "",
  ]
    .filter(Boolean)
    .join(" · ");
  $("current-updated").title = formatTime(point.timestamp);
  document.querySelector(".live-badge").textContent = point.stale ? "Stale fix" : "Latest fix";
  $("current-coordinates").textContent = `${Number(point.lat).toFixed(6)}, ${Number(point.lon).toFixed(6)}`;
}

export function focusPlaceOnMap(place) {
  state.focusPlace = place;
  state.selected = null;
  const wasOpen =
    $("explore-view").classList.contains("active") && !$("history-layout").classList.contains("no-data");
  showView("explore");
  // Re-render only when the map was hidden; otherwise the lists keep their scroll position.
  if (!wasOpen && state.day && state.coverage) renderExplore(state.day, state.coverage);
  else {
    drawMap(false);
    $("map-note").textContent = "Location: " + (place.name || place.display_name || "selected point");
  }
  setTimeout(() => {
    map?.invalidateSize();
    map?.setView([place.lat, place.lon], 16);
    revealMap();
  }, 30);
}

export function renderInsights() {
  const data = state.insights;
  if (!data) return;
  const prediction = data.prediction;
  $("prediction").textContent = prediction.prediction
    ? `Likely next from ${prediction.from}: ${prediction.prediction} (${Math.round(prediction.frequency * 100)}% of ${prediction.observations} observed departures).${prediction.current_fix_stale ? " Latest location is stale; verify before acting." : ""}`
    : `Next place: insufficient evidence (${prediction.reason}).`;
  $("pattern-places").innerHTML =
    data.frequent_places
      .slice(0, 8)
      .map(
        (place, index) =>
          `<div class="list-item"><div class="place-main">${placeIcon(place)}<div><strong>${safe(place.display_name)}</strong><small>${zoneText(place.zone, place.display_name) ? safe(zoneText(place.zone, place.display_name)) + " · " : ""}${place.visits} visit${place.visits === 1 ? "" : "s"} · ${durationText(place.observed_minutes)}</small></div></div><button data-pattern-place="${index}">Map</button></div>`,
      )
      .join("") || '<div class="empty">Common places appear after confirmed stays.</div>';
  $("pattern-places")
    .querySelectorAll("[data-pattern-place]")
    .forEach(
      (button) =>
        (button.onclick = () => focusPlaceOnMap(data.frequent_places[+button.dataset.patternPlace])),
    );
  $("transitions").innerHTML =
    data.transitions
      .slice(0, 8)
      .map(
        (t, index) =>
          `<div class="list-item"><div><strong>${safe(t.from_name)} → ${safe(t.to_name)}</strong><small>${t.count} observed transition${t.count === 1 ? "" : "s"}</small></div><button data-transition="${index}">Map arrival</button></div>`,
      )
      .join("") || '<div class="empty">Transitions appear after visits to two or more places.</div>';
  $("transitions")
    .querySelectorAll("[data-transition]")
    .forEach(
      (button) =>
        (button.onclick = () => {
          const transition = data.transitions[+button.dataset.transition];
          focusPlaceOnMap({
            lat: transition.to_lat,
            lon: transition.to_lon,
            display_name: transition.to_name,
          });
        }),
    );
}

export async function refreshCoverage() {
  const data = await api("/api/coverage");
  $("coverage").innerHTML =
    data.sources
      .map(
        (s) =>
          `<div class="list-item"><div><strong>${safe(s.source)} · ${s.points.toLocaleString()} fixes</strong><small>${safe(formatTime(s.first))} → ${safe(formatTime(s.last))}</small></div></div>`,
      )
      .join("") +
      (data.gaps_over_24h.length
        ? `<p class="muted">${data.gaps_over_24h.length} gaps over 24 hours. Missing coverage cannot establish where you were.</p>`
        : "") || '<div class="empty">No source data yet.</div>';
}

export async function refreshInsights() {
  state.insights = await api("/api/insights?limit=50");
  renderInsights();
}

export async function refreshRawPage() {
  const query = params();
  query.set("limit", state.rawPageSize);
  query.set("offset", state.rawPage * state.rawPageSize);
  state.points = await api("/api/observations?" + query);
  renderSamples();
}

export async function refreshExplore(resetRawPage = true) {
  const request = ++exploreRequest;
  if (resetRawPage) state.rawPage = 0;
  const date = $("day-date").value;
  const source = $("source").value;
  const timezone = state.settings?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const dayQuery = new URLSearchParams({ date, timezone });
  if (source) dayQuery.set("source", source);
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
  const selected = $("source").value;
  $("source").innerHTML =
    '<option value="">All sources</option>' +
    coverage.sources
      .map((item) => '<option value="' + safe(item.source) + '">' + safe(item.source) + "</option>")
      .join("");
  $("source").value = selected;
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
}

export function moveDay(offset) {
  state.focusPlace = null;
  $("day-date").value = shiftDateKey($("day-date").value, offset);
  state.followToday = $("day-date").value === localDateKey();
  refreshExplore().catch((error) => toast(error.message));
}

export function linkedDate() {
  const match = location.hash.match(/date=(\d{4}-\d{2}-\d{2})/);
  return match && match[1] <= localDateKey() ? match[1] : null;
}

export function initHistory() {
  document.querySelectorAll(".detail-tab").forEach(
    (button) =>
      (button.onclick = () => {
        const name = button.dataset.panel;
        document.querySelectorAll(".detail-tab").forEach((tab) => {
          const active = tab === button;
          tab.classList.toggle("active", active);
          tab.setAttribute("aria-selected", String(active));
        });
        document
          .querySelectorAll(".detail-panel")
          .forEach((panel) => panel.classList.toggle("active", panel.dataset.panelView === name));
      }),
  );
  document.querySelectorAll("[data-sample-view]").forEach(
    (button) =>
      (button.onclick = () => {
        state.sampleView = button.dataset.sampleView;
        document
          .querySelectorAll("[data-sample-view]")
          .forEach((tab) => tab.classList.toggle("active", tab === button));
        renderSamples();
      }),
  );
  $("raw-previous").onclick = async () => {
    if (state.rawPage > 0) {
      state.rawPage--;
      try {
        await refreshRawPage();
      } catch (error) {
        toast(error.message);
      }
    }
  };
  $("raw-next").onclick = async () => {
    if ((state.rawPage + 1) * state.rawPageSize < state.rawTotal) {
      state.rawPage++;
      try {
        await refreshRawPage();
      } catch (error) {
        toast(error.message);
      }
    }
  };
  $("day-date").onchange = () => {
    state.focusPlace = null;
    state.followToday = $("day-date").value === localDateKey();
    refreshExplore().catch((error) => toast(error.message));
  };
  $("previous-day").onclick = () => moveDay(-1);
  $("next-day").onclick = () => moveDay(1);
  $("today").onclick = () => {
    state.focusPlace = null;
    $("day-date").value = localDateKey();
    state.followToday = true;
    refreshExplore().catch((error) => toast(error.message));
  };
  $("source").onchange = () => {
    state.focusPlace = null;
    refreshExplore().catch((error) => toast(error.message));
  };
  $("locate").onclick = () => {
    const point = state.current || state.day?.last_location;
    if (point && map) map.setView([point.lat, point.lon], 15);
  };
  $("current-coordinates").onclick = () => {
    if (state.current) copyText(coordinateText(state.current.lat, state.current.lon), "Coordinates copied");
  };
  $("export").onclick = async () => {
    try {
      const q = params().toString();
      const data = await api("/api/export.geojson" + (q ? "?" + q : ""));
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/geo+json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "geologs-export.geojson";
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    } catch (e) {
      toast(e.message);
    }
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
    if (!$("explore-view").classList.contains("active") || !$("auth-panel").hidden || $("place-dialog").open)
      return;
    if (event.target.closest("input,select,textarea,#map")) return;
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
      state.focusPlace = null;
      state.followToday = false;
      $("day-date").value = date;
      showView("explore");
      refreshExplore().catch((error) => toast(error.message));
    }
  });
  if (linkedDate()) {
    state.followToday = false;
    $("day-date").value = linkedDate();
  }
}
