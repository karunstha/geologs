// Summaries: the saved-day index and a day's stored record.
import { api } from "../core/api.js";
import { $, copyText, safe, toast } from "../core/dom.js";
import { clock, distanceText, durationText, formatTime, localDateKey, longDate } from "../core/format.js";
import { state } from "../core/state.js";
import { modeIcon, modeInfo, modeName, placeIcon, statusPill, summaryIcons, zoneText } from "../ui/icons.js";
import { refreshExplore } from "./history.js";
import { showView } from "./shell.js";

export function summaryLine(item) {
  return `${item.trip_count} trip${item.trip_count === 1 ? "" : "s"} · ${Number(item.distance_km).toFixed(1)} km · ${item.place_count} place${item.place_count === 1 ? "" : "s"}${item.reminder_count ? ` · ${item.reminder_count} reminder${item.reminder_count === 1 ? "" : "s"}` : ""}`;
}

export function summaryDateParts(value) {
  const [year, month, day] = value.split("-").map(Number),
    date = new Date(year, month - 1, day);
  return {
    month: date.toLocaleDateString(undefined, { month: "long", year: "numeric" }),
    weekday: date.toLocaleDateString(undefined, { weekday: "short" }),
    day,
  };
}

export function summaryCatalogMarkup(items) {
  const groups = new Map();
  for (const item of items) {
    const parts = summaryDateParts(item.date);
    if (!groups.has(parts.month)) groups.set(parts.month, []);
    groups.get(parts.month).push({ ...item, ...parts });
  }
  return [...groups.entries()]
    .map(
      ([month, days]) =>
        `<section class="summary-month"><h3>${safe(month)}</h3>${days.map((item) => `<button type="button" class="summary-day${item.date === state.summaryDate ? " selected" : ""}" data-summary="${safe(item.date)}" aria-current="${item.date === state.summaryDate ? "date" : "false"}" aria-label="${safe(longDate(item.date) + ", " + summaryLine(item))}"><span class="summary-day-calendar"><span>${safe(item.weekday)}</span><strong>${item.day}</strong></span><span class="summary-day-copy">${item.points ? `<strong>${Number(item.distance_km).toFixed(1)} km</strong><small>${item.trip_count} trip${item.trip_count === 1 ? "" : "s"} · ${item.place_count} place${item.place_count === 1 ? "" : "s"}${item.reminder_count ? ` · ${item.reminder_count} reminder${item.reminder_count === 1 ? "" : "s"}` : ""}</small>` : '<strong class="muted">No data</strong><small>Nothing recorded</small>'}</span><span class="summary-day-arrow" aria-hidden="true">›</span></button>`).join("")}</section>`,
    )
    .join("");
}

export async function refreshSummaries() {
  state.summaryCatalog = await api("/api/daily-summaries?limit=1000");
  $("summary-count").textContent = state.summaryCatalog.length
    ? state.summaryCatalog.length.toLocaleString()
    : "";
  $("summary-count").hidden = !state.summaryCatalog.length;
  $("summary-date").max = localDateKey();
  $("summary-index").innerHTML =
    summaryCatalogMarkup(state.summaryCatalog) ||
    '<div class="summary-index-empty"><strong>No saved days yet</strong><p>A summary is saved after the first completed day. You can also jump to a past date to build one.</p></div>';
  $("summary-index")
    .querySelectorAll("[data-summary]")
    .forEach((button) => (button.onclick = () => openSummary(button.dataset.summary, false, true)));
  // Open the newest day that has something to show.
  const first = state.summaryCatalog.find((item) => item.points) || state.summaryCatalog[0];
  if (!state.summaryDate && first) openSummary(first.date);
}

export async function openSummary(date, build = false, reveal = false) {
  state.summaryDate = date;
  $("summary-date").value = date;
  document.querySelectorAll("#summary-index [data-summary]").forEach((button) => {
    const selected = button.dataset.summary === date;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-current", selected ? "date" : "false");
  });
  const box = $("summary-detail");
  box.setAttribute("aria-busy", "true");
  box.innerHTML = `<div class="summary-loading"><span class="summary-spinner" aria-hidden="true"></span><strong>${build ? "Building your summary" : "Opening your summary"}</strong><span>${safe(longDate(date))}</span></div>`;
  if (reveal && window.matchMedia("(max-width:950px)").matches)
    box.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      block: "start",
    });
  try {
    // Building writes the record (or refreshes it if its data changed); reading shows exactly what is stored.
    if (build) {
      await api("/api/daily-summary?" + new URLSearchParams({ date }));
      await refreshSummaries();
    }
    renderSummaryDetail(await api("/api/daily-summary/stored?" + new URLSearchParams({ date })));
  } catch (error) {
    if (state.summaryDate !== date) return;
    box.removeAttribute("aria-busy");
    box.innerHTML = `<div class="summary-missing"><span class="empty-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="15" rx="2"/><path d="M8 3v4M16 3v4M4 10h16M9 15h6"/></svg></span><div><span class="eyebrow">Not saved yet</span><h2>${safe(longDate(date))}</h2><p>${safe(error.message)}.</p></div><button type="button" class="button primary" id="build-summary">Build this summary</button></div>`;
    $("build-summary").onclick = () => openSummary(date, true);
  }
}

export function summaryMetric(label, value, icon) {
  return `<div><span class="summary-metric-icon" aria-hidden="true">${summaryIcons[icon]}</span><dt>${safe(label)}</dt><dd>${safe(value)}</dd></div>`;
}

export function summarySpan(start, end, date) {
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

export function summaryTimeline(value) {
  const events = [];
  for (const visit of value.visits || [])
    events.push({
      at: visit.arrival,
      type: "visit",
      title: visit.place || "Unnamed place",
      time: summarySpan(visit.arrival, visit.departure, value.date),
      detail: [
        zoneText(visit.zone, visit.place),
        durationText(visit.observed_minutes),
        visit.activity
          ? `${modeName(visit.activity)}${visit.distance_km ? " " + distanceText(visit.distance_km) : ""}`
          : "",
        (visit.moved_within || []).length
          ? "Moved within: " +
            visit.moved_within
              .map((move) => `${modeName(move.mode).toLowerCase()} ${distanceText(move.distance_km)}`)
              .join(", ")
          : "",
        visit.ongoing && value.date === localDateKey() ? "Still there" : "",
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
        ? `Round trip from ${trip.from || "a stay"}`
        : `${trip.from || "A stay"} → ${trip.to || "a stay"}`,
      time: `${clock(trip.start)} – ${clock(trip.end)}`,
      detail: [
        `${modeName(trip.mode)} ${distanceText(trip.distance_km)}`,
        durationText(trip.duration_minutes),
        trip.coverage_complete ? "" : "Coverage gap",
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
  if (!events.length)
    return '<div class="summary-section-empty">No visits or trips were stored for this day.</div>';
  return `<ol class="summary-timeline">${events.map((event) => `<li class="${event.type}"><div class="summary-timeline-rail">${event.icon}</div><div class="summary-timeline-card"><span class="summary-event-time">${safe(event.time)}</span><strong>${safe(event.title)}</strong><small>${safe(event.detail)}</small></div></li>`).join("")}</ol>`;
}

export function summaryPlaceRows(places) {
  if (!places.length) return '<div class="summary-section-empty">No places were stored for this day.</div>';
  return `<div class="summary-place-list">${places
    .map(
      (place) =>
        `<div class="summary-place-row">${placeIcon(place)}<div><strong>${safe(place.name || "Unnamed place")}${place.home ? '<span class="summary-home-tag">Home</span>' : ""}</strong><small>${safe(
          [
            zoneText(place.zone, place.name),
            `${place.visits} visit${place.visits === 1 ? "" : "s"}`,
            Object.entries(place.activities || {})
              .map(([mode, count]) => modeName(mode) + (count > 1 ? ` ×${count}` : ""))
              .join(", "),
            place.address && place.address !== place.name ? place.address : "",
          ]
            .filter(Boolean)
            .join(" · "),
        )}</small></div><span class="summary-place-duration">${safe(durationText(place.observed_minutes))}</span></div>`,
    )
    .join("")}</div>`;
}

export function summaryReminderRows(value) {
  const reminders = [
    ...(value.reminders_triggered || []).map((item) => ({
      ...item,
      status: "Triggered",
      at: item.triggered_at,
      tone: "success",
    })),
    ...(value.reminders_created || []).map((item) => ({
      ...item,
      status: "Created",
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
      status: "Still active",
      at: null,
      tone: "active",
    })),
  ];
  if (!reminders.length) return '<div class="summary-section-empty">No reminder activity.</div>';
  return `<div class="summary-reminder-list">${reminders.map((item) => `<div class="summary-reminder-row"><span class="summary-reminder-dot ${item.tone}" aria-hidden="true"></span><div><strong>${safe(item.text)}</strong><small>${safe([item.status, item.at ? clock(item.at) : "", item.place_name].filter(Boolean).join(" · "))}</small></div></div>`).join("")}</div>`;
}

export function summaryRow(title, detail) {
  return `<div class="list-item"><div><strong>${safe(title)}</strong>${detail ? `<small>${safe(detail)}</small>` : ""}</div></div>`;
}

export function renderSummaryDetail(record) {
  if (state.summaryDate !== record.date) return;
  const value = record.summary,
    totals = value.totals || {};
  const homeReturn = value.home_return?.summary;
  // The brief's summary is the same facts in plain sentences.
  const overviewText = record.brief?.summary || value.overview || "No overview was stored for this day.";
  const displayOverview =
    homeReturn && overviewText.endsWith(` ${homeReturn}`)
      ? overviewText.slice(0, -homeReturn.length - 1)
      : overviewText;
  const facts = [
    ["Distance", `${Number(totals.distance_km || 0).toFixed(1)} km`, "distance"],
    ["Time moving", durationText((totals.moving_hours || 0) * 60), "time"],
    ["Trips", totals.trips ?? 0, "trips"],
    ["Places", totals.places ?? 0, "places"],
  ];
  const secondaryFacts = [
    ["Sampled distance", `${Number(totals.sampled_distance_km || 0).toFixed(1)} km`],
    [
      "Location fixes",
      `${(totals.timeline_fixes ?? 0).toLocaleString()} key / ${(totals.raw_fixes ?? 0).toLocaleString()} raw`,
    ],
    ["Reminders", `${totals.reminders_triggered ?? 0} triggered / ${totals.reminders_created ?? 0} created`],
  ];
  const activity = Object.entries(value.activity_totals || {})
    .filter(([mode, item]) => item.count && modeInfo[mode])
    .map(
      ([mode, item]) =>
        `<span class="chip" style="--mode:${modeInfo[mode].color}">${modeIcon(mode)}<strong>${modeInfo[mode].noun}</strong><span>${distanceText(item.distance_km)} · ${durationText(item.minutes)}</span></span>`,
    )
    .join("");
  const gaps = (value.coverage_gaps || []).map((gap) =>
    summaryRow("No location data", `${clock(gap.start)} – ${clock(gap.end)} · ${durationText(gap.minutes)}`),
  );
  // Rough token estimate (about 4 characters each) to show what the brief saves an AI tool.
  const tokens = (text) => "≈" + Math.round(text.length / 4).toLocaleString() + " tokens",
    brief = record.brief;
  const briefBlock = `<section class="summary-block summary-brief"><div class="brief-head"><div><h3>Brief for AI tools</h3><p>A compact version for MCP tools and language models.</p></div>${brief ? `<small>${tokens(JSON.stringify(brief))} · full record ${tokens(JSON.stringify(value))}</small><button type="button" class="button secondary" id="copy-brief">Copy brief</button>` : ""}</div>
    ${brief ? `<pre>${safe(JSON.stringify(brief, null, 2))}</pre>` : '<p class="empty">No brief is stored with this record yet. Refresh to build it.</p>'}</section>`;
  $("summary-detail").removeAttribute("aria-busy");
  if (!(totals.raw_fixes > 0)) {
    $("summary-detail").innerHTML =
      `<div class="summary-hero"><div class="summary-head"><div><div class="summary-status-line">${statusPill("neutral", "No data")}<span>${safe(record.timezone)}</span></div><h2>${safe(longDate(record.date))}</h2><div class="summary-updated">Updated ${safe(formatTime(record.updated_at))}</div></div><div class="summary-actions"><button type="button" class="button secondary" id="summary-refresh">Refresh</button></div></div></div>
      <div class="summary-empty-state"><span class="empty-icon" aria-hidden="true">${summaryIcons.places}</span><h2>No location recorded</h2><p>No device sent a location on this day, so there are no visits or trips to summarize.${record.stale ? " Newer data has arrived since this was saved; refresh to include it." : ""}</p></div>`;
    $("summary-refresh").onclick = () => openSummary(record.date, true);
    return;
  }
  $("summary-detail").innerHTML =
    `<div class="summary-hero"><div class="summary-head"><div><div class="summary-status-line"><span class="summary-status ${record.stale ? "stale" : "current"}">${record.stale ? "Needs refresh" : "Saved summary"}</span><span>${safe(record.timezone)}</span></div><h2>${safe(longDate(record.date))}</h2><div class="summary-updated">Updated ${safe(formatTime(record.updated_at))}${record.created_at !== record.updated_at ? " · First saved " + safe(formatTime(record.created_at)) : ""}</div></div>
      <div class="summary-actions"><button type="button" class="button primary" id="summary-history">Open in History</button><button type="button" class="button secondary" id="summary-refresh">Refresh summary</button></div></div>
      <p class="summary-overview">${safe(displayOverview)}</p>${homeReturn ? `<div class="summary-home-return"><span aria-hidden="true">⌂</span><div><small>Cross-midnight home return</small><strong>${safe(homeReturn)}</strong></div></div>` : ""}</div>
    ${record.stale ? '<div class="summary-stale-note"><span aria-hidden="true">!</span><div><strong>This day has newer location data</strong><p>Refresh the summary to include the latest changes.</p></div></div>' : ""}
    <dl class="summary-facts">${facts.map(([label, text, icon]) => summaryMetric(label, text, icon)).join("")}</dl>
    <dl class="summary-secondary-facts">${secondaryFacts.map(([label, text]) => `<div><dt>${safe(label)}</dt><dd>${safe(text)}</dd></div>`).join("")}</dl>
    ${activity ? `<section class="summary-content-section summary-activity"><div class="summary-section-head"><span>Movement</span><h3>Activity</h3></div><div class="activity-chips">${activity}</div></section>` : ""}
    <section class="summary-content-section"><div class="summary-section-head"><span>In order</span><h3>Day timeline</h3></div>${summaryTimeline(value)}</section>
    <div class="summary-lower-grid"><section class="summary-content-section"><div class="summary-section-head"><span>Stops</span><h3>Places</h3></div>${summaryPlaceRows(value.places || [])}</section><section class="summary-content-section"><div class="summary-section-head"><span>To-dos</span><h3>Reminders</h3></div>${summaryReminderRows(value)}</section></div>
    <section class="summary-coverage ${gaps.length ? "has-gaps" : "complete"}"><span class="summary-coverage-icon" aria-hidden="true">${gaps.length ? "!" : "✓"}</span><div><strong>${gaps.length ? `${gaps.length} coverage gap${gaps.length === 1 ? "" : "s"}` : "Coverage looks complete"}</strong><p>${safe(value.coverage_note || "No gaps over 30 minutes.")}</p>${gaps.length ? `<div class="summary-coverage-gaps">${gaps.join("")}</div>` : ""}</div></section>
    <details class="summary-technical"><summary><span><strong>AI &amp; record details</strong><small>View the compact brief, token estimate, and stored JSON</small></span><span class="summary-disclosure" aria-hidden="true">⌄</span></summary><div class="summary-technical-body">${briefBlock}<details class="summary-json"><summary>Stored JSON <small>schema ${safe(value.schema_version ?? "?")}</small></summary><pre>${safe(JSON.stringify(value, null, 2))}</pre></details></div></details>`;
  $("summary-history").onclick = () => {
    state.focusPlace = null;
    state.followToday = record.date === localDateKey();
    $("day-date").value = record.date;
    showView("explore");
    refreshExplore().catch((error) => toast(error.message));
  };
  $("summary-refresh").onclick = () => openSummary(record.date, true);
  if (brief) $("copy-brief").onclick = () => copyText(JSON.stringify(brief, null, 2), "Brief copied");
}

export function initSummaries() {
  $("summary-open-form").onsubmit = (event) => {
    event.preventDefault();
    const date = $("summary-date").value;
    if (date) openSummary(date, !state.summaryCatalog?.some((item) => item.date === date), true);
  };
}
