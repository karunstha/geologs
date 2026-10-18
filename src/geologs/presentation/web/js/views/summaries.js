// Summaries: the saved-day index and a day's stored record.
import { api } from "../core/api.js";
import { $, copyText, safe, toast } from "../core/dom.js";
import {
  ago,
  clock,
  dayTitle,
  distanceText,
  durationText,
  formatTime,
  localDateKey,
  longDate,
  fuelText,
  moneyText,
  plural,
  volumeText,
} from "../core/format.js";
import { state } from "../core/state.js";
import { actionIcon } from "../ui/actions.js";
import { modeIcon, modeInfo, modeName, placeIcon, summaryIcons, zoneText } from "../ui/icons.js";
import { refreshExplore } from "./history.js";
import { initPeriods, refreshPeriod } from "./periods.js";
import { showView } from "./shell.js";

export function summaryLine(item) {
  return [
    plural(item.trip_count, "trip"),
    distanceText(Number(item.distance_km)),
    plural(item.place_count, "place"),
    item.reminder_count ? plural(item.reminder_count, "reminder") : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

function catalogMarkup(items) {
  const groups = new Map();
  for (const item of items) {
    const [year, month, day] = item.date.split("-").map(Number);
    const date = new Date(year, month - 1, day);
    const heading = date.toLocaleDateString(undefined, { month: "long", year: "numeric" });
    if (!groups.has(heading)) groups.set(heading, []);
    groups
      .get(heading)
      .push({ ...item, day, weekday: date.toLocaleDateString(undefined, { weekday: "short" }) });
  }
  return [...groups.entries()]
    .map(
      ([heading, days]) =>
        `<section class="summary-month"><h3>${safe(heading)}</h3>${days
          .map((item) => {
            const selected = item.date === state.summaryDate;
            return `<button type="button" class="summary-day${selected ? " selected" : ""}" data-summary="${safe(item.date)}" aria-current="${selected ? "date" : "false"}" aria-label="${safe(longDate(item.date) + ", " + summaryLine(item))}"><span class="summary-day-calendar"><span>${safe(item.weekday)}</span><strong>${item.day}</strong></span><span class="summary-day-copy"><strong>${safe(distanceText(Number(item.distance_km)))}</strong><small>${safe([plural(item.trip_count, "trip"), plural(item.place_count, "place")].join(" · "))}</small></span><span class="summary-day-arrow" aria-hidden="true">${actionIcon("open")}</span></button>`;
          })
          .join("")}</section>`,
    )
    .join("");
}

// What driving cost lately, beside the days it adds up from.
async function renderFuelSpend() {
  const box = $("summary-fuel");
  if (!state.settings?.fuel_economy_l100km) {
    box.hidden = true;
    return;
  }
  try {
    const month = (await api("/api/fuel")).last_30_days;
    box.hidden = !month.configured || !month.drives;
    if (!box.hidden)
      box.innerHTML = `<span>Fuel, last 30 days</span><strong>${safe(moneyText(month.cost, month.currency))}</strong><small>${safe(moneyText(month.cost_per_day, month.currency))} a day on average · ${plural(month.drives, "drive")} · ${safe(volumeText(month.litres))}</small>`;
  } catch (_error) {
    box.hidden = true;
  }
}

export async function refreshSummaries() {
  refreshPeriod();
  renderFuelSpend();
  state.summaryCatalog = await api("/api/daily-summaries?limit=1000");
  $("summary-count").textContent = state.summaryCatalog.length
    ? state.summaryCatalog.length.toLocaleString()
    : "";
  $("summary-date").max = localDateKey();
  const index = $("summary-index");
  index.innerHTML =
    catalogMarkup(state.summaryCatalog) ||
    '<div class="empty-block"><strong>No saved days yet</strong><span>Open any day and it is saved here.</span></div>';
  index.querySelectorAll("[data-summary]").forEach((button) => {
    button.onclick = () => openSummary(button.dataset.summary, true);
  });
  // The tab opens on today (saved now, so it is current), else the latest saved day.
  if (!state.summaryDate) openSummary(localDateKey(), false, true);
}

function markSelected(date) {
  document.querySelectorAll("#summary-index [data-summary]").forEach((button) => {
    const selected = button.dataset.summary === date;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-current", selected ? "date" : "false");
    if (selected) button.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
}

// Show a saved day, or build it when it isn't saved yet. A day with nothing recorded is never stored.
export async function openSummary(date, reveal = false, fallback = false) {
  state.summaryDate = date;
  $("summary-date").value = date;
  markSelected(date);
  const box = $("summary-detail");
  box.setAttribute("aria-busy", "true");
  box.innerHTML = `<div class="summary-loading"><span class="summary-spinner" aria-hidden="true"></span><strong>Opening ${safe(dayTitle(date))}</strong></div>`;
  if (reveal && window.matchMedia("(max-width:900px)").matches)
    box.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      block: "start",
    });
  try {
    // Opening a day brings its summary up to date, whatever the schedule: building
    // reuses the saved record when nothing changed, so this is cheap.
    const known = state.summaryCatalog?.some((item) => item.date === date);
    const built = await api("/api/daily-summary?" + new URLSearchParams({ date }));
    if (state.summaryDate !== date) return;
    if (!(built.totals?.raw_fixes > 0)) {
      if (fallback && state.summaryCatalog?.[0]) return openSummary(state.summaryCatalog[0].date);
      renderNothingRecorded(date);
      return;
    }
    const record = await api("/api/daily-summary/stored?" + new URLSearchParams({ date }));
    if (state.summaryDate !== date) return;
    if (!known) await refreshSummaries();
    renderSummaryDetail(record);
  } catch (error) {
    if (state.summaryDate !== date) return;
    box.removeAttribute("aria-busy");
    box.innerHTML = `<div class="summary-empty-state"><span class="empty-icon" aria-hidden="true">${summaryIcons.places}</span><h2>Couldn't open ${safe(dayTitle(date))}</h2><p>${safe(error.message)}</p><button type="button" class="button secondary" id="summary-retry">Try again</button></div>`;
    $("summary-retry").onclick = () => openSummary(date);
  }
}

function renderNothingRecorded(date) {
  const box = $("summary-detail");
  box.removeAttribute("aria-busy");
  box.innerHTML = `<div class="summary-empty-state"><span class="empty-icon" aria-hidden="true">${summaryIcons.places}</span><h2>Nothing recorded on ${safe(dayTitle(date))}</h2><p>No device sent a location that day.</p>${state.summaryCatalog?.length ? '<button type="button" class="button secondary" id="summary-latest">Open the latest saved day</button>' : ""}</div>`;
  if (state.summaryCatalog?.length)
    $("summary-latest").onclick = () => openSummary(state.summaryCatalog[0].date);
}

function metric(label, value, icon) {
  return `<div><span class="summary-metric-icon" aria-hidden="true">${summaryIcons[icon]}</span><dt>${safe(label)}</dt><dd>${safe(value)}</dd></div>`;
}

function span(start, end, date) {
  // A stay that began the day before reads "Until …", one that runs past midnight "From …".
  const early = localDateKey(new Date(start)) < date,
    late = localDateKey(new Date(end)) > date;
  return early && late
    ? "All day"
    : early
      ? `Until ${clock(end)}`
      : late
        ? `From ${clock(start)}`
        : `${clock(start)} – ${clock(end)}`;
}

function timelineMarkup(value) {
  const events = [];
  for (const visit of value.visits || [])
    events.push({
      at: visit.arrival,
      type: "visit",
      title: visit.place || "Unnamed place",
      time: span(visit.arrival, visit.departure, value.date),
      detail: [
        zoneText(visit.zone, visit.place),
        durationText(visit.observed_minutes),
        visit.activity
          ? `${modeName(visit.activity)}${visit.distance_km ? " " + distanceText(visit.distance_km) : ""}`
          : "",
        (visit.moved_within || []).length
          ? "moved within: " +
            visit.moved_within
              .map((move) => `${modeName(move.mode).toLowerCase()} ${distanceText(move.distance_km)}`)
              .join(", ")
          : "",
        visit.ongoing && value.date === localDateKey() ? "still there" : "",
      ]
        .filter(Boolean)
        .join(" · "),
      icon: placeIcon({ name: visit.place }),
    });
  for (const trip of value.trips || [])
    events.push({
      at: trip.start,
      type: "trip",
      title: trip.round_trip
        ? `Round trip from ${trip.from || "a stay"}${trip.via ? ` via ${trip.via}` : ""}`
        : `${trip.from || "A stay"} → ${trip.to || "a stay"}`,
      time: `${clock(trip.start)} – ${clock(trip.end)}`,
      detail: [
        `${modeName(trip.mode)} ${distanceText(trip.distance_km)}`,
        durationText(trip.duration_minutes),
        trip.fuel_cost != null ? `${fuelText(trip.fuel_cost, trip.fuel_litres, value.totals?.currency || undefined)} fuel` : "",
        trip.coverage_complete ? "" : "partly unrecorded",
      ]
        .filter(Boolean)
        .join(" · "),
      icon: modeIcon(trip.mode),
    });
  for (const gap of value.coverage_gaps || [])
    events.push({
      at: gap.start,
      type: "gap",
      title: "No location data",
      time: `${clock(gap.start)} – ${clock(gap.end)}`,
      detail: durationText(gap.minutes),
      icon: '<span class="summary-gap-icon" aria-hidden="true">···</span>',
    });
  events.sort((a, b) => String(a.at).localeCompare(String(b.at)));
  if (!events.length) return '<div class="summary-section-empty">No visits or trips on this day.</div>';
  return `<ol class="summary-timeline">${events.map((event) => `<li class="${event.type}"><div class="summary-timeline-rail">${event.icon}</div><div class="summary-timeline-card"><span class="summary-event-time">${safe(event.time)}</span><strong>${safe(event.title)}</strong><small>${safe(event.detail)}</small></div></li>`).join("")}</ol>`;
}

function placesMarkup(places) {
  if (!places.length) return '<div class="summary-section-empty">No places on this day.</div>';
  return `<div class="summary-place-list">${places
    .map((place) => {
      const name = place.name || "Unnamed place";
      // "Home" already says it; other names for home get a tag.
      const tag =
        place.home && !/^(my )?home$/i.test(name) ? '<span class="summary-home-tag">Home</span>' : "";
      const details = [
        zoneText(place.zone, place.name),
        plural(place.visits, "visit"),
        Object.entries(place.activities || {})
          .map(([mode, count]) => modeName(mode) + (count > 1 ? ` ×${count}` : ""))
          .join(", "),
        place.address && place.address !== place.name ? place.address : "",
      ].filter(Boolean);
      return `<div class="summary-place-row">${placeIcon(place)}<div><strong>${safe(name)}${tag}</strong><small>${safe(details.join(" · "))}</small></div><span class="summary-place-duration">${safe(durationText(place.observed_minutes))}</span></div>`;
    })
    .join("")}</div>`;
}

function remindersMarkup(value) {
  const reminders = [
    ...(value.reminders_triggered || []).map((item) => ({
      ...item,
      status: "Fired",
      at: item.triggered_at,
      tone: "success",
    })),
    ...(value.reminders_created || []).map((item) => ({
      ...item,
      status: "Added",
      at: item.created_at,
      tone: "created",
    })),
    ...(value.reminders_cancelled || []).map((item) => ({
      ...item,
      status: "Cancelled",
      at: item.cancelled_at,
      tone: "muted",
    })),
    ...(value.active_reminders_at_end || []).map((item) => ({
      ...item,
      status: "Still waiting",
      at: null,
      tone: "active",
    })),
  ];
  if (!reminders.length) return '<div class="summary-section-empty">No reminder activity.</div>';
  return `<div class="summary-reminder-list">${reminders
    .map(
      (item) =>
        `<div class="summary-reminder-row"><span class="summary-reminder-dot ${item.tone}" aria-hidden="true"></span><div><strong>${safe(item.text)}</strong><small>${safe([item.status, item.at ? clock(item.at) : "", item.place_name].filter(Boolean).join(" · "))}</small></div></div>`,
    )
    .join("")}</div>`;
}

function coverageMarkup(value) {
  const gaps = value.coverage_gaps || [];
  if (!gaps.length) return ""; // full coverage is the normal case; only gaps are worth a word
  const missing = gaps.reduce((sum, gap) => sum + gap.minutes, 0);
  return `<section class="summary-coverage has-gaps"><span class="summary-coverage-icon" aria-hidden="true">!</span><div><strong>${safe(durationText(missing))} without location, in ${plural(gaps.length, "gap")}</strong><p>${gaps.map((gap) => `${safe(clock(gap.start))} – ${safe(clock(gap.end))}`).join(" · ")}.</p></div></section>`;
}

function neighbours(date) {
  const dates = (state.summaryCatalog || []).map((item) => item.date);
  return {
    older: dates.find((item) => item < date),
    newer: [...dates].reverse().find((item) => item > date),
  };
}

export function renderSummaryDetail(record) {
  if (state.summaryDate !== record.date) return;
  const value = record.summary,
    totals = value.totals || {};
  if (!(totals.raw_fixes > 0)) {
    renderNothingRecorded(record.date);
    return;
  }
  const homeReturn = value.home_return?.summary;
  const facts = [
    ["Distance", distanceText(Number(totals.distance_km || 0)), "distance"],
    ["Time moving", durationText((totals.moving_hours || 0) * 60), "time"],
    ["Trips", totals.trips ?? 0, "trips"],
    ["Places", totals.places ?? 0, "places"],
    ...(totals.fuel_cost != null ? [["Fuel", fuelText(totals.fuel_cost, totals.fuel_litres, totals.currency || undefined), "trips"]] : []),
  ];
  const reminderActivity =
    (value.reminders_triggered || []).length + (value.reminders_created || []).length +
    (value.reminders_cancelled || []).length + (value.active_reminders_at_end || []).length;
  const activity = Object.entries(value.activity_totals || {})
    .filter(([mode, item]) => item.count && modeInfo[mode])
    .map(
      ([mode, item]) =>
        `<span class="chip" style="--mode:${modeInfo[mode].color}">${modeIcon(mode)}<strong>${modeInfo[mode].noun}</strong><span>${distanceText(item.distance_km)} · ${durationText(item.minutes)}</span></span>`,
    )
    .join("");
  // Rough token estimate (about 4 characters each) to show what the brief saves an AI tool.
  const tokens = (text) => "≈" + Math.round(text.length / 4).toLocaleString() + " tokens";
  const brief = record.brief;
  const briefBlock = `<section class="summary-block summary-brief"><div class="brief-head"><div><h3>Brief for AI tools</h3><p>This day in a few lines, for MCP tools.</p></div>${brief ? `<small>${tokens(JSON.stringify(brief))} · full record ${tokens(JSON.stringify(value))}</small><button type="button" class="button secondary small" id="copy-brief">Copy brief</button>` : ""}</div>
    ${brief ? `<pre>${safe(JSON.stringify(brief, null, 2))}</pre>` : '<p class="empty">No brief is stored with this record yet. Refresh to build it.</p>'}</section>`;
  const { older, newer } = neighbours(record.date);
  const box = $("summary-detail");
  box.removeAttribute("aria-busy");
  box.innerHTML = `<div class="summary-hero"><div class="summary-head"><div class="summary-title"><h2>${safe(longDate(record.date))}</h2></div>
      <div class="summary-actions"><div class="day-step"><button type="button" class="icon-button bordered" id="summary-older" aria-label="Older day" title="Older day"${older ? "" : " disabled"}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14.5 6-6 6 6 6"/></svg></button><button type="button" class="icon-button bordered" id="summary-newer" aria-label="Newer day" title="Newer day"${newer ? "" : " disabled"}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9.5 6 6 6-6 6"/></svg></button></div><button type="button" class="button primary" id="summary-history">Open in History</button></div></div>
      ${homeReturn ? `<div class="summary-home-return"><span aria-hidden="true">⌂</span><div><small>Home after midnight</small><strong>${safe(homeReturn)}</strong></div></div>` : ""}</div>
    ${record.stale ? '<div class="summary-stale-note"><span aria-hidden="true">!</span><div><strong>Newer location data arrived while you were viewing this day</strong><p><button type="button" class="button secondary small" id="summary-refresh">Update summary</button></p></div></div>' : ""}
    <dl class="summary-facts">${facts.map(([label, text, icon]) => metric(label, text, icon)).join("")}</dl>
    ${activity ? `<section class="summary-content-section summary-activity"><div class="summary-section-head"><h3>Activity</h3></div><div class="activity-chips">${activity}</div></section>` : ""}
    <section class="summary-content-section"><div class="summary-section-head"><h3>Timeline</h3></div>${timelineMarkup(value)}</section>
    <div class="summary-lower-grid${reminderActivity ? "" : " single"}"><section class="summary-content-section"><div class="summary-section-head"><h3>Places</h3></div>${placesMarkup(value.places || [])}</section>${reminderActivity ? `<section class="summary-content-section"><div class="summary-section-head"><h3>Reminders</h3></div>${remindersMarkup(value)}</section>` : ""}</div>
    ${coverageMarkup(value)}
    <details class="summary-technical"><summary><span><strong>For AI tools and developers</strong><small>The compact brief, its size, and the stored record</small></span><span class="summary-disclosure" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg></span></summary><div class="summary-technical-body"><p class="summary-record-meta">Saved ${safe(ago(record.updated_at))} (${safe(formatTime(record.updated_at))}) · ${safe(record.timezone)} · ${(totals.timeline_fixes ?? 0).toLocaleString()} key fixes of ${(totals.raw_fixes ?? 0).toLocaleString()} raw · sampled distance ${safe(distanceText(Number(totals.sampled_distance_km || 0)))}</p>${briefBlock}<details class="summary-json"><summary>Stored record <small>JSON · schema ${safe(value.schema_version ?? "?")}</small></summary><pre>${safe(JSON.stringify(value, null, 2))}</pre></details></div></details>`;
  $("summary-history").onclick = () => {
    state.focusPlace = null;
    state.followToday = record.date === localDateKey();
    $("day-date").value = record.date;
    showView("explore");
    refreshExplore().catch((error) => toast(error.message));
  };
  if (record.stale) $("summary-refresh").onclick = async () => {
    try {
      await api("/api/daily-summary?" + new URLSearchParams({ date: record.date }));
      await refreshSummaries();
      await openSummary(record.date);
      toast("Summary refreshed");
    } catch (error) {
      toast(error.message);
    }
  };
  if (older) $("summary-older").onclick = () => openSummary(older);
  if (newer) $("summary-newer").onclick = () => openSummary(newer);
  if (brief) $("copy-brief").onclick = () => copyText(JSON.stringify(brief, null, 2), "Brief copied");
}

export function initSummaries() {
  initPeriods(openSummary);
  $("summary-open-form").onsubmit = (event) => {
    event.preventDefault();
    const date = $("summary-date").value;
    if (date) openSummary(date, true);
  };
  // Picking a date opens it; there is no separate button to press.
  $("summary-date").onchange = () => {
    const date = $("summary-date").value;
    if (date && date !== state.summaryDate) openSummary(date, true);
  };
}
