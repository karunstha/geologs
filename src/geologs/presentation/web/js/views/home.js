// The new Home: the day at a glance (glass cards over the town's photo and a grey, still map), then the
// classic Home's own timeline and details. Those are not copies: the classic Home's head, day controls,
// map, timeline and details are moved in here once at start, so every edit and tool works as before.
// Setting localStorage "geologs-home" to "classic" leaves the classic Home untouched and shows it instead.
import { api } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import {
  clock,
  distanceText,
  durationText,
  imperial,
  localDateKey,
  moneyText,
  plural,
  shiftDateKey,
  zonedMidnight,
} from "../core/format.js";
import { state } from "../core/state.js";
import { dayBounds, drawSelection, map, selectItem, setCoveredRight, tripKey } from "../map/map.js";
import { modeIcon, modeInfo, placeIcon, visitNumbers } from "../ui/icons.js";
import { moveDay } from "./history.js";
import { showView } from "./shell.js";

export function newHomeEnabled() {
  try {
    return localStorage.getItem("geologs-home") !== "classic";
  } catch (_error) {
    return true;
  }
}

const FREE_MAP = "https://tiles.openfreemap.org/styles/";
const FREE_MAP_CREDIT =
  '&copy; <a href="https://openfreemap.org" target="_blank" rel="noopener">OpenFreeMap</a> &copy; <a href="https://www.openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors';
// Positron's land, water and buildings, in the page's greys (the dark style is already dark).
const LIGHT_GREYS = {
  background: "#e4e4e1",
  park: "#dcdcd8",
  water: "#cfd0cf",
  landuse_residential: "#dfdfdb",
  landcover_wood: "#d9d9d5",
  building: "#d6d6d2",
};
// The map's controls, off while it is the hero's still backdrop.
const MAP_HANDLERS = ["dragging", "touchZoom", "doubleClickZoom", "scrollWheelZoom", "boxZoom", "keyboard", "tapHold"];
const ICONS = {
  explore: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>',
  done: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3H3v6M15 21h6v-6M3 3l7 7M21 21l-7-7"/></svg>',
};

let glMap = null;
// Whether the vector style's layers can be changed yet (isStyleLoaded waits for every tile too).
let styleReady = false;
// null until the first setExploring, so that call always sets everything up.
let exploring = null;
let fittedKey = null;

// "1h 05m" rather than "1 h 5 min": the glance cards' numbers are large.
function shortDuration(minutes) {
  const total = Math.round(minutes || 0);
  return total < 60 ? `${total}m` : `${Math.floor(total / 60)}h ${String(total % 60).padStart(2, "0")}m`;
}

// A big number with its units ("h", "m", "mi", "$") in small type.
function smallUnits(text) {
  return safe(text).replace(/[^\d\s.,:\-–]+/g, (unit) => `<small>${unit}</small>`);
}

// The day's span in the configured timezone (today's ends now), and how many minutes of it a stay covers.
function daySpan(day) {
  const start = zonedMidnight(day.date).getTime();
  const end = Math.min(zonedMidnight(shiftDateKey(day.date, 1)).getTime(), Date.now());
  return {
    start,
    end,
    minutes: Math.max(1, (end - start) / 60000),
    covered: (from, to) =>
      Math.max(0, (Math.min(end, to ? Date.parse(to) : end) - Math.max(start, Date.parse(from))) / 60000),
    holds: (value) => {
      const at = Date.parse(value);
      return at >= start && at < end;
    },
  };
}

// Something each trip has (its distance, its fuel), shared across the hours it ran.
function byHour(day, amount) {
  const start = zonedMidnight(day.date).getTime(),
    hours = Array(24).fill(0);
  for (const trip of day.trips) {
    const total = amount(trip);
    if (!total) continue;
    const from = Date.parse(trip.start),
      to = Math.max(Date.parse(trip.end), from + 1);
    for (let at = from; at < to; ) {
      const hour = Math.floor((at - start) / 3600000),
        next = Math.min(to, start + (hour + 1) * 3600000);
      if (next <= at) break;
      if (hour >= 0 && hour < 24) hours[hour] += (total * (next - at)) / (to - from);
      at = next;
    }
  }
  return hours;
}

// The shown day's month, day by day, for the Distance and Fuel cards: the saved summaries of its
// days (the catalog), with the shown day's own live numbers in place of its saved ones.
const months = new Map();
const MONTH_REFRESH_MS = 60 * 1000;

function monthRows(day) {
  const key = day.date.slice(0, 7);
  const cached = months.get(key);
  const current = key === localDateKey().slice(0, 7);
  if ((!cached || (current && Date.now() - cached.at > MONTH_REFRESH_MS)) && !cached?.loading) {
    const next = { ...cached, at: Date.now(), loading: true };
    months.set(key, next);
    const query = new URLSearchParams({ start: key + "-01", end: key + "-31", limit: 40 });
    api("/api/daily-summaries?" + query)
      .then((rows) => months.set(key, { at: Date.now(), rows }))
      .catch(() => months.set(key, { at: Date.now(), rows: cached?.rows || [] }))
      .then(() => state.day?.date.startsWith(key) && renderMonth(state.day));
  }
  return months.get(key)?.rows || [];
}

// A month's bars, one a day, the shown day lit.
function bars(svg, values, lit) {
  const max = Math.max(...values);
  svg.toggleAttribute("hidden", !(max > 0));
  if (!(max > 0)) return;
  const width = 200 / values.length;
  svg.innerHTML = values
    .map((value, i) => {
      const height = value > 0 ? Math.max(2, (value / max) * 50) : 0;
      return `<rect class="home-month-bar${i === lit ? " lit" : ""}${value > 0 ? "" : " none"}" x="${(i * width + width * 0.15).toFixed(2)}" y="${(54 - (height || 1.5)).toFixed(2)}" width="${(width * 0.7).toFixed(2)}" height="${height || 1.5}" rx="1.2"/>`;
    })
    .join("");
}

function renderMonth(day) {
  const [year, month] = day.date.split("-").map(Number);
  const days = new Date(year, month, 0).getDate();
  const lit = Number(day.date.slice(8)) - 1;
  const distance = Array(days).fill(0),
    fuel = Array(days).fill(0);
  for (const row of monthRows(day)) {
    const i = Number(row.date.slice(8)) - 1;
    if (i >= 0 && i < days) {
      distance[i] = row.distance_km || 0;
      fuel[i] = row.fuel_cost || 0;
    }
  }
  distance[lit] = day.movement_km ?? day.stats.distance_km ?? 0;
  fuel[lit] = day.fuel?.cost || 0;
  const sum = (values) => values.reduce((a, b) => a + b, 0);
  const label = year === Number(localDateKey().slice(0, 4)) && month === Number(localDateKey().slice(5, 7)) ? "this month" : new Date(year, month - 1, 1).toLocaleDateString(undefined, { month: "long" });
  // The numbers are the shown day's; only the bars are the month.
  $("home-distance-title").textContent = "Distance";
  $("home-distance").innerHTML = smallUnits(distanceText(distance[lit]));
  $("home-distance-note").textContent = `${distanceText(sum(distance))} ${label}`;
  bars($("home-distance-chart"), distance, lit);
  $("home-fuel-title").textContent = "Fuel";
  const spent = sum(fuel);
  $("home-fuel").innerHTML = smallUnits(moneyText(fuel[lit], day.fuel?.currency));
  $("home-fuel-note").textContent = `${moneyText(spent, day.fuel?.currency)} ${label}`;
  bars($("home-fuel-chart"), fuel, lit);
}

function renderGlance(day) {
  const numbers = visitNumbers(day).byStay;
  const span = daySpan(day);
  // Stays the timeline numbers are places; the rest are Home, as the timeline and map show them.
  const stays = [...day.stays]
    .sort((a, b) => a.started_at.localeCompare(b.started_at))
    .map((stay) => ({
      stay,
      home: !numbers.has(stay.id),
      name: numbers.has(stay.id) ? stay.display_name || "Stay" : "Home",
      minutes: span.covered(stay.started_at, stay.ended_at),
    }));
  const moving = day.movement_minutes || 0;
  const away = stays.filter((item) => !item.home).reduce((sum, item) => sum + item.minutes, 0) + moving;
  const today = day.date === localDateKey();

  // Time away needs a Home to be away from: a saved place of that name, or a stay at it today.
  const homeKnown =
    stays.some((item) => item.home) ||
    (state.places || []).some((place) => /^(my )?home$/i.test(String(place.name || "").trim()));
  $("home-stats").innerHTML = [
    ["In motion", shortDuration(moving)],
    homeKnown ? ["Away from home", shortDuration(away)] : [stays.length === 1 ? "Stay" : "Stays", stays.length.toLocaleString()],
    [day.visited_places.length === 1 ? "Place" : "Places", day.visited_places.length.toLocaleString()],
  ]
    .map(([label, value]) => `<div><dt>${label}</dt><dd>${smallUnits(value)}</dd></div>`)
    .join("");
  const modes = Object.entries(day.activity_totals).filter(([mode, totals]) => totals.count && modeInfo[mode]);
  $("home-modes").innerHTML = modes
    .map(
      ([mode, totals]) =>
        `<span class="home-mode">${modeIcon(mode)}<strong>${modeInfo[mode].noun}</strong><span>${totals.count} · ${distanceText(totals.distance_km)} · ${shortDuration(totals.minutes)}</span></span>`,
    )
    .join("");
  $("home-modes").hidden = !modes.length;

  renderMonth(day);
  renderBrief(day, stays);

  // Where the day went: the two places held longest, any others together, and time on the move.
  const places = new Map();
  for (const item of stays) places.set(item.name, (places.get(item.name) || 0) + item.minutes);
  const ranked = [...places].sort((a, b) => b[1] - a[1]);
  const bars = ranked.slice(0, 2);
  const others = ranked.slice(2).reduce((sum, [, minutes]) => sum + minutes, 0);
  if (others >= 1) bars.push(["Other places", others]);
  if (moving >= 1) bars.push(["Moving", moving]);
  const untracked = span.minutes - stays.reduce((sum, item) => sum + item.minutes, 0) - moving;
  $("home-share-note").textContent = [
    plural(ranked.length, "place"),
    today ? "so far today" : "24 hours",
    untracked / span.minutes >= 0.02 ? `${Math.round((untracked / span.minutes) * 100)}% untracked` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const longest = Math.max(1, ...bars.map(([, minutes]) => minutes));
  $("home-share").innerHTML = bars
    .map(
      ([name, minutes], index) =>
        `<div class="home-bar${index === 0 ? " top" : ""}" title="${safe(name)}: ${safe(durationText(minutes))}"><strong>${Math.round((minutes / span.minutes) * 100)}%</strong><i style="height:${Math.round(8 + (minutes / longest) * 92)}px"></i><span>${safe(name)}</span></div>`,
    )
    .join("");

  // Movement by hour, from each trip's distance.
  const hours = byHour(day, (trip) => trip.distance_km);
  const peak = Math.max(...hours);
  $("home-hours-unit").textContent = imperial() ? "mi" : "km";
  $("home-hours").innerHTML =
    hours
      .map(
        (km, hour) =>
          `<i class="${km < 0.05 ? "none" : ""}" style="height:${peak ? Math.max(2, (km / peak) * 100) : 2}%" title="${hour}:00 · ${safe(distanceText(km))}"></i>`,
      )
      .join("") +
    (peak > 0
      ? `<span class="home-peak" style="left:${((hours.indexOf(peak) + 0.5) / 24) * 100}%">${safe(distanceText(peak))}</span>`
      : "");
}

// The still Home's timeline: just when, where and how, over the map. Pressing one opens the map on it,
// with the full timeline beside it.
function renderBrief(day, stays) {
  const items = [
    ...stays.map((item) => ({ at: item.stay.started_at, item })),
    ...day.trips.map((trip) => ({ at: trip.start, trip })),
  ].sort((a, b) => a.at.localeCompare(b.at) || (a.item ? -1 : 1));
  $("home-brief").innerHTML = items
    .map(({ at, item, trip }) => {
      if (item)
        return `<li><button type="button" class="home-brief-stay" data-item="stay:${item.stay.id}">${placeIcon(item.home ? { name: "Home" } : { ...item.stay, name: item.stay.display_name })}<span><strong>${safe(item.name)}</strong><small>${safe(clock(at))} · ${safe(shortDuration(item.stay.observed_minutes))}</small></span></button></li>`;
      const info = modeInfo[trip.mode] || { label: "Moved", color: "#6f7b76" };
      return `<li><button type="button" class="home-brief-trip" data-item="trip:${safe(tripKey(trip))}" style="--mode:${info.color}" title="${safe(`${info.label} · ${clock(at)}`)}">${modeIcon(trip.mode)}<small>${safe(`${distanceText(trip.distance_km)} · ${shortDuration(trip.duration_minutes)}`)}</small></button></li>`;
    })
    .join("");
}

function openBriefItem(button) {
  const [type, ...rest] = button.dataset.item.split(":");
  const key = rest.join(":");
  setExploring(true);
  // The full timeline is in place now, so the zoom keeps clear of it.
  if (!(state.selected?.type === type && String(state.selected.key) === key))
    selectItem(type, type === "stay" ? +key : key);
}

// How much of the map's right edge the timeline covers: on a wide screen it sits over the map, still
// or exploring; on a narrow one it is below it.
function timelineCover() {
  const timeline = $("home-timeline");
  if (!exploring || $("home-hero").hidden || timeline.hidden || !matchMedia("(min-width: 901px)").matches) return 0;
  return Math.max(0, $("map").getBoundingClientRect().right - timeline.getBoundingClientRect().left);
}

function updateCover() {
  const cover = timelineCover();
  setCoveredRight(cover && cover + 16);
  return cover;
}

// Fit the day to the part of the map the cards and the timeline leave clear.
function restFit() {
  if (!map || $("home-hero").hidden) return;
  const cover = updateCover();
  const bounds = dayBounds();
  if (!bounds.length) return;
  let left = 40,
    right = cover ? cover + 28 : 40,
    top = exploring ? 70 : 56,
    bottom = exploring ? 70 : 36;
  const box = $("map").getBoundingClientRect();
  if (!exploring && matchMedia("(min-width: 901px)").matches && !$("home-glance").hidden) {
    left = document.querySelector(".home-stack").getBoundingClientRect().right - box.left + 28;
    right = box.right - $("home-brief").getBoundingClientRect().left + 28;
    if (box.width - left - right < 160) left = right = 40;
  }
  map.fitBounds(L.latLngBounds(bounds), { paddingTopLeft: [left, top], paddingBottomRight: [right, bottom], maxZoom: 15, duration: 0.7 });
}

function setInteractive(on) {
  for (const name of MAP_HANDLERS) map?.[name]?.[on ? "enable" : "disable"]();
}

// Place names only while exploring; the still map is shapes alone.
function showLabels() {
  if (!styleReady) return;
  for (const layer of glMap.getStyle().layers)
    if (layer.type === "symbol") glMap.setLayoutProperty(layer.id, "visibility", exploring ? "visible" : "none");
}

function styleMap() {
  styleReady = true;
  const light = document.documentElement.dataset.theme !== "dark";
  for (const layer of glMap.getStyle().layers) {
    const grey = light && LIGHT_GREYS[layer.id];
    if (grey) glMap.setPaintProperty(layer.id, layer.type === "background" ? "background-color" : "fill-color", grey);
  }
  showLabels();
}

// Exploring makes the map whole and interactive: the glance cards step aside and labels and zoom return.
// The map never changes size (only what is drawn over it does), so the view eases from one fit to the
// other instead of jumping.
function setExploring(on, keepView = false) {
  if (on === exploring && !keepView) return;
  exploring = on;
  $("home-hero").classList.toggle("exploring", on);
  $("home-explore").innerHTML = on ? `${ICONS.done}<span>Done</span>` : `${ICONS.explore}<span>Explore map</span>`;
  $("home-explore").setAttribute("aria-pressed", String(on));
  // On a phone the map also changes height; Leaflet is told before it fits.
  if (matchMedia("(max-width: 900px)").matches) map?.invalidateSize();
  setInteractive(on);
  showLabels();
  updateCover();
  if (!keepView) map?.closePopup();
  if (!on && state.selected) {
    state.selected = null;
    drawSelection(false);
  }
  // A selected stay or trip has been zoomed to already; a popup keeps the view it opened on.
  if (keepView || (on && state.selected)) return;
  restFit();
}

// The map in OpenFreeMap's vector style (grey Positron, or its dark style), through Leaflet so every
// path, pin and popup is drawn as before. If it does not load, the classic tiles come back, greyed.
function useGreyMap() {
  if (!map || !L.maplibreGL || !window.maplibregl) return;
  let tiles = null;
  map.eachLayer((layer) => {
    if (layer instanceof L.TileLayer) tiles = layer;
  });
  const styleFor = () => FREE_MAP + (document.documentElement.dataset.theme === "dark" ? "dark" : "positron");
  let style = styleFor(),
    layer = null,
    losses = 0;
  const unmount = () => {
    if (layer) map.removeLayer(layer);
    layer = glMap = null;
    styleReady = false;
  };
  const useTiles = () => {
    unmount();
    tiles?.addTo(map);
    $("home-view").classList.remove("vector-map");
  };
  const mount = () => {
    style = styleFor();
    const current = (layer = L.maplibreGL({ style, attribution: FREE_MAP_CREDIT }).addTo(map));
    glMap = layer.getMaplibreMap();
    const fallback = setTimeout(useTiles, 15000);
    glMap.once("load", () => clearTimeout(fallback));
    glMap.on("style.load", styleMap);
    // Safari drops a WebGL context when the GPU is short of memory, and MapLibre does not draw again
    // after it: start a fresh map instead, and after a few losses settle for the plain tiles.
    glMap.on("webglcontextlost", () => {
      clearTimeout(fallback);
      setTimeout(() => {
        if (layer !== current) return;
        unmount();
        if (++losses > 3) useTiles();
        else mount();
      }, 250);
    });
  };
  mount();
  if (tiles) map.removeLayer(tiles);
  $("home-view").classList.add("vector-map");
  new MutationObserver(() => {
    if (!glMap || styleFor() === style) return;
    styleReady = false;
    glMap.setStyle((style = styleFor()));
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
}

function renderHome() {
  const day = state.day;
  if (!day) return;
  const hasHistory = day.points > 0,
    showMap = hasHistory || !!state.focusPlace;
  $("home-hero").hidden = !showMap;
  $("home-hero").classList.toggle("map-only", !hasHistory);
  $("home-glance").hidden = !hasHistory;
  $("home-explore").hidden = !hasHistory;
  $("home-lower").hidden = !hasHistory;
  $("home-timeline").hidden = !hasHistory;
  if (hasHistory) renderGlance(day);
  if (!showMap) return;
  map?.invalidateSize();
  // A place shown from elsewhere is for looking around; a new day starts still and fitted.
  const key = `${day.date}|${day.source || ""}`;
  if (state.focusPlace || !hasHistory) {
    if (!exploring) setExploring(true, true);
  } else if (key !== fittedKey) {
    fittedKey = key;
    if (exploring) setExploring(false);
    else restFit();
  }
}

// Called by showView when Home comes on screen: the map was hidden, so it is measured again.
export function homeShown() {
  setTimeout(() => {
    map?.invalidateSize();
    restFit();
  }, 30);
}

// Settings → Units & theme → Home screen. The new Home moves the classic one's pieces at start, so
// switching reloads the page rather than moving them back.
function initHomeChoice(enabled) {
  document.querySelectorAll('input[name="home-style"]').forEach((input) => {
    input.checked = input.value === (enabled ? "new" : "classic");
    input.onchange = () => {
      try {
        if (input.value === "classic") localStorage.setItem("geologs-home", "classic");
        else localStorage.removeItem("geologs-home");
      } catch (_error) {
        toast("This browser won't save the choice (private window or blocked site data).");
        input.checked = false;
        return;
      }
      location.reload();
    };
  });
}

export function initHome() {
  const enabled = newHomeEnabled();
  initHomeChoice(enabled);
  document.querySelector('.nav[data-view="home"]').hidden = !enabled;
  document.querySelector('.nav[data-view="explore"]').hidden = enabled;
  if (!enabled) return;
  const classic = $("explore-view");
  for (const [slot, node] of [
    ["home-head", $("photo-credit")],
    ["home-head", classic.querySelector(".history-head")],
    ["home-head", classic.querySelector(".day-bar")],
    ["home-empty", $("day-empty")],
    ["home-map", classic.querySelector(".map-shell")],
    ["home-timeline", $("inspector")],
  ])
    if (node) $(slot).append(node);
  // The classic Home dims itself while a day loads; this page follows it.
  new MutationObserver(() => $("home-view").classList.toggle("loading", classic.classList.contains("loading"))).observe(
    classic,
    { attributes: true, attributeFilter: ["class"] },
  );
  // Choosing a stay or trip (in the timeline or on the map) opens the map to show it.
  new MutationObserver(() => {
    if (state.selected && !exploring && $("home-view").classList.contains("active")) setExploring(true);
  }).observe($("day-timeline"), { subtree: true, attributes: true, attributeFilter: ["class"] });
  useGreyMap();
  // Popups keep clear of the map's faded edge and the bar over its top while exploring.
  L.Popup.prototype.options.autoPanPaddingTopLeft = L.point(30, 100);
  L.Popup.prototype.options.autoPanPaddingBottomRight = L.point(30, 56);
  setExploring(false);
  // A place or stop pressed on the still map opens its popup on the whole map, clear of the cards.
  map?.on("popupopen", () => {
    if (!exploring && $("home-view").classList.contains("active")) setExploring(true, true);
  });
  $("home-explore").onclick = () => setExploring(!exploring);
  $("home-brief").onclick = (event) => {
    const button = event.target.closest("[data-item]");
    if (button) openBriefItem(button);
  };
  // The map's height follows the window (home.css fills it), so whenever the hero changes size the map is
  // measured again and the day fitted again, unless a chosen stay or trip is being shown.
  let resizeTimer = 0;
  new ResizeObserver(() => {
    map?.invalidateSize();
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!(exploring && state.selected)) restFit();
    }, 200);
  }).observe($("home-hero"));
  document.addEventListener("geologs:day-drawn", renderHome);
  // The classic Home's keys: ← and → step through days, Esc closes the map.
  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (!$("home-view").classList.contains("active") || !$("auth-panel").hidden) return;
    if (document.querySelector("dialog[open]") || event.target.closest("input,select,textarea,#map")) return;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      moveDay(-1);
    } else if (event.key === "ArrowRight" && !$("next-day").disabled) {
      event.preventDefault();
      moveDay(1);
    } else if (event.key === "Escape" && exploring && !$("home-glance").hidden) setExploring(false);
  });
  showView("home");
}
