// The new Home: the day at a glance (glass cards over the town's photo and a grey, still map), then the
// classic Home's own timeline and details. Those are not copies: the classic Home's head, day controls,
// map, timeline and details are moved in here once at start, so every edit and tool works as before.
// Setting localStorage "geologs-home" to "classic" leaves the classic Home untouched and shows it instead.
import { $, safe } from "../core/dom.js";
import {
  clock,
  distanceText,
  durationText,
  imperial,
  localDateKey,
  moneyText,
  plural,
  shiftDateKey,
  volumeText,
  zonedMidnight,
} from "../core/format.js";
import { state } from "../core/state.js";
import { dayBounds, drawSelection, map } from "../map/map.js";
import { modeIcon, modeInfo, visitNumbers } from "../ui/icons.js";
import { moveDay } from "./history.js";
import { showView } from "./shell.js";

export function newHomeEnabled() {
  try {
    return localStorage.getItem("geologs-home") !== "classic";
  } catch (_error) {
    return true;
  }
}

const KM_PER_MILE = 1.609344;
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
  home: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 11 12 4l8 7v9h-5v-6H9v6H4Z"/></svg>',
  pin: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 10c0 4.6-7 10-7 10S5 14.6 5 10a7 7 0 0 1 14 0Z"/><circle cx="12" cy="10" r="2.4"/></svg>',
  signal: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12a10 10 0 0 1 14 0M8.5 15.5a5 5 0 0 1 7 0"/><circle cx="12" cy="19" r="1"/></svg>',
  bell: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 16.5h12l-1.6-2.6V10a4.4 4.4 0 0 0-8.8 0v3.9Z"/><path d="M10 19.5h4"/></svg>',
};

let glMap = null;
// Whether the vector style's layers can be changed yet (isStyleLoaded waits for every tile too).
let styleReady = false;
let exploring = false;
let fittedKey = null;
let settleTimer = 0;

// "1h 05m" rather than "1 h 5 min": the glance cards' numbers are large.
function shortDuration(minutes) {
  const total = Math.round(minutes || 0);
  return total < 60 ? `${total}m` : `${Math.floor(total / 60)}h ${String(total % 60).padStart(2, "0")}m`;
}

// The day's distance to one decimal, with its unit apart, for the Overview's large number.
function distanceParts(km) {
  return imperial() ? [(km / KM_PER_MILE).toFixed(1), "mi"] : [km.toFixed(1), "km"];
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

function spark(svg, values) {
  const max = Math.max(...values);
  // An SVG element has no hidden property, only the attribute.
  svg.toggleAttribute("hidden", !(max > 0));
  if (!(max > 0)) return;
  const line =
    "M" +
    values.map((value, i) => `${((i / (values.length - 1)) * 200).toFixed(1)},${(52 - (value / max) * 46).toFixed(1)}`).join("L");
  const ticks = values.map((_, i) => `<circle cx="${((i / (values.length - 1)) * 200).toFixed(1)}" cy="55" r="0.9"/>`).join("");
  svg.innerHTML = `<g class="home-spark-ticks">${ticks}</g><path class="home-spark-area" d="${line}L200,56L0,56Z"/><path class="home-spark-line" d="${line}"/>`;
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
  const [distance, unit] = distanceParts(day.movement_km ?? day.stats.distance_km ?? 0);
  $("home-stats").innerHTML = [
    ["Distance", distance, unit],
    ["In motion", shortDuration(moving)],
    homeKnown ? ["Away from home", shortDuration(away)] : [stays.length === 1 ? "Stay" : "Stays", stays.length.toLocaleString()],
    [day.visited_places.length === 1 ? "Place" : "Places", day.visited_places.length.toLocaleString()],
  ]
    .map(([label, value, small]) => `<div><dt>${label}</dt><dd>${safe(value)}${small ? `<small>${small}</small>` : ""}</dd></div>`)
    .join("");
  const modes = Object.entries(day.activity_totals).filter(([mode, totals]) => totals.count && modeInfo[mode]);
  $("home-modes").innerHTML = modes
    .map(
      ([mode, totals]) =>
        `<span class="home-mode">${modeIcon(mode)}<strong>${modeInfo[mode].noun}</strong><span>${totals.count} · ${distanceText(totals.distance_km)} · ${shortDuration(totals.minutes)}</span></span>`,
    )
    .join("");
  $("home-modes").hidden = !modes.length;

  $("home-trips").textContent = day.trips.length.toLocaleString();
  $("home-trips-note").textContent =
    modes.map(([mode, totals]) => `${totals.count} ${modeInfo[mode].label.toLowerCase()}${totals.count === 1 ? "" : "s"}`).join(" · ") ||
    "No trips";
  spark($("home-trips-spark"), byHour(day, (trip) => trip.distance_km));

  // Fuel when a drive was costed; otherwise the longest stay away from home.
  if (day.fuel) {
    $("home-second-title").textContent = "Fuel";
    $("home-second").textContent = moneyText(day.fuel.cost, day.fuel.currency);
    $("home-second-note").textContent = `${volumeText(day.fuel.litres)} · ${plural(day.fuel.drives, "drive")}`;
    let spent = 0;
    spark($("home-second-spark"), byHour(day, (trip) => trip.fuel?.cost).map((cost) => (spent += cost)));
  } else {
    const longest = stays.filter((item) => !item.home).sort((a, b) => b.minutes - a.minutes)[0];
    $("home-second-title").textContent = "Longest stay";
    $("home-second").textContent = longest ? shortDuration(longest.minutes) : "—";
    $("home-second-note").textContent = longest ? longest.name : "Nowhere but home";
    spark($("home-second-spark"), []);
  }

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

  // Day notes: getting home, coverage, and reminders.
  const notes = [];
  const last = stays.at(-1);
  if (!last) notes.push([ICONS.pin, "No stays recorded", ""]);
  else if (!day.trips.length)
    notes.push([last.home ? ICONS.home : ICONS.pin, last.home ? "Home all day" : `At ${last.name} all day`, "No trips"]);
  else if (last.home) notes.push([ICONS.home, `Back home at ${clock(last.stay.started_at)}`, `After ${durationText(away)} away`]);
  else
    notes.push([
      ICONS.pin,
      today && !last.stay.ended_at ? `Now at ${last.name}` : `Ended the day at ${last.name}`,
      `Since ${clock(last.stay.started_at)}`,
    ]);
  const gaps = day.coverage_gaps || [];
  const missing = gaps.reduce((sum, gap) => sum + (gap.minutes || 0), 0);
  notes.push([
    ICONS.signal,
    gaps.length ? `${durationText(missing)} without location` : "Full coverage",
    gaps.length
      ? gaps
          .slice(0, 3)
          .map((gap) => `${clock(gap.start)} – ${clock(gap.end)}`)
          .join(" · ") + (gaps.length > 3 ? ` · ${gaps.length - 3} more` : "")
      : `${plural(day.points, "fix", "fixes")} recorded, no gaps`,
  ]);
  const fired = (state.reminderHistory || []).filter(
    (item) => item.status === "triggered" && span.holds(item.triggered_at || item.last_triggered_at),
  );
  notes.push([
    ICONS.bell,
    fired.length ? `${plural(fired.length, "reminder")} fired` : "No reminders fired",
    fired
      .slice(0, 2)
      .map((item) => item.text)
      .join(" · "),
  ]);
  $("home-notes").innerHTML = notes
    .map(
      ([icon, title, detail]) =>
        `<li><span class="home-note-icon">${icon}</span><span><strong>${safe(title)}</strong>${detail ? `<small>${safe(detail)}</small>` : ""}</span></li>`,
    )
    .join("");
}

// Fit the day to the part of the map the glance cards leave clear.
function restFit() {
  if (!map || exploring || $("home-hero").hidden) return;
  const bounds = dayBounds();
  if (!bounds.length) return;
  let left = 28,
    right = 28;
  const box = $("map").getBoundingClientRect();
  if (matchMedia("(min-width: 901px)").matches && !$("home-glance").hidden) {
    left = document.querySelector(".home-stack").getBoundingClientRect().right - box.left + 28;
    right = box.right - document.querySelector(".home-share").getBoundingClientRect().left + 28;
    if (box.width - left - right < 160) left = right = 28;
  }
  map.fitBounds(L.latLngBounds(bounds), { paddingTopLeft: [left, 56], paddingBottomRight: [right, 36], maxZoom: 15 });
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

// Exploring makes the map whole and interactive: the glance cards step aside, labels and zoom return.
function setExploring(on, keepView = false) {
  exploring = on;
  $("home-hero").classList.toggle("exploring", on);
  $("home-explore").innerHTML = on ? `${ICONS.done}<span>Done</span>` : `${ICONS.explore}<span>Explore map</span>`;
  $("home-explore").setAttribute("aria-pressed", String(on));
  setInteractive(on);
  showLabels();
  if (!on && state.selected) {
    state.selected = null;
    drawSelection(false);
  }
  // Refit once the hero has its new size.
  clearTimeout(settleTimer);
  settleTimer = setTimeout(() => {
    map?.invalidateSize();
    if (keepView) return;
    if (on && state.selected) drawSelection(true);
    else if (!on) restFit();
  }, 260);
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
  let style = styleFor();
  const layer = L.maplibreGL({ style, attribution: FREE_MAP_CREDIT }).addTo(map);
  glMap = layer.getMaplibreMap();
  if (tiles) map.removeLayer(tiles);
  $("home-view").classList.add("vector-map");
  const fallback = setTimeout(() => {
    map.removeLayer(layer);
    glMap = null;
    styleReady = false;
    tiles?.addTo(map);
    $("home-view").classList.remove("vector-map");
  }, 15000);
  glMap.once("load", () => clearTimeout(fallback));
  glMap.on("style.load", styleMap);
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
    ["home-details", $("history-details")],
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
  setExploring(false);
  $("home-explore").onclick = () => setExploring(!exploring);
  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(restFit, 200);
  });
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
