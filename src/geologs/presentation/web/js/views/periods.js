// Summaries → Month and Year: a period told as a story. Photos of the towns you spent time in,
// what it added up to, a calendar coloured by how far you went, and the places that made it.
import { api } from "../core/api.js";
import { $, safe } from "../core/dom.js";
import { distanceText, durationText, localDateKey, longDate, moneyText, plural } from "../core/format.js";
import { state } from "../core/state.js";
import { modeIcon, modeInfo, modeName, placeIcon } from "../ui/icons.js";

const SCALE_KEY = "geologs-summary-scale";
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let request = 0;
let retry;
let openDay = () => {};

export function savedScale() {
  try {
    return localStorage.getItem(SCALE_KEY) || "day";
  } catch (_error) {
    return "day";
  }
}

function currentPeriod(scale) {
  const today = localDateKey();
  return scale === "year" ? today.slice(0, 4) : today.slice(0, 7);
}

function periodTitle(period) {
  if (period.length === 4) return period;
  const [year, month] = period.split("-").map(Number);
  return new Date(year, month - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

function shiftPeriod(period, offset) {
  if (period.length === 4) return String(Number(period) + offset);
  const [year, month] = period.split("-").map(Number);
  const date = new Date(year, month - 1 + offset, 1);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

// Day, Month or Year: which part of the page shows, and the controls that go with it.
export function showScale(scale, period) {
  state.summaryScale = scale;
  try {
    localStorage.setItem(SCALE_KEY, scale);
  } catch (_error) {}
  document.querySelectorAll("[data-summary-scale]").forEach((button) => {
    const active = button.dataset.summaryScale === scale;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const day = scale === "day";
  $("summaries-day").hidden = !day;
  $("summary-open-form").hidden = !day;
  $("summaries-period").hidden = day;
  $("period-step").hidden = day;
  if (day) return;
  const wanted = period || (state.summaryPeriod?.length === (scale === "year" ? 4 : 7) ? state.summaryPeriod : null);
  openPeriod(wanted || currentPeriod(scale));
}

export async function openPeriod(period) {
  clearTimeout(retry);
  state.summaryPeriod = period;
  const latest = currentPeriod(period.length === 4 ? "year" : "month");
  $("period-label").textContent = periodTitle(period);
  $("period-newer").disabled = period >= latest;
  const box = $("summaries-period");
  const mine = ++request;
  if (!box.dataset.period || box.dataset.period !== period)
    box.innerHTML = `<div class="card summary-loading"><span class="summary-spinner" aria-hidden="true"></span><strong>Adding up ${safe(periodTitle(period))}</strong></div>`;
  box.dataset.period = period;
  try {
    const value = await api("/api/period-summary?" + new URLSearchParams({ period }));
    if (mine !== request) return;
    renderPeriod(value);
    // Days and towns still to add come in a few at a time; ask again until they are all in.
    if (value.pending.days || value.pending.towns) retry = setTimeout(() => openPeriod(period), 1200);
  } catch (error) {
    if (mine !== request) return;
    box.innerHTML = `<div class="card summary-empty-state"><h2>Couldn't add up ${safe(periodTitle(period))}</h2><p>${safe(error.message)}</p></div>`;
  }
}

function heroMarkup(value) {
  const towns = value.towns.filter((town) => town.photo);
  const photos = towns.slice(0, 4);
  const totals = value.totals;
  const story = [
    plural(totals.days, "day") + " recorded",
    distanceText(totals.distance_km || 0),
    plural(totals.places || 0, "place"),
    value.towns.length ? `in ${plural(value.towns.length, "town")}` : "",
  ].filter(Boolean);
  const mosaic = photos.length
    ? `<div class="period-mosaic n${photos.length}">${photos
        .map(
          (town) =>
            `<figure style="--photo:url('${safe(town.photo.thumb || town.photo.url).replace(/'/g, "%27")}');--tone:${safe(town.photo.color || "#335")}"><figcaption>${safe(town.name)}</figcaption></figure>`,
        )
        .join("")}</div>`
    : '<div class="period-mosaic empty" aria-hidden="true"></div>';
  const credits = photos.length
    ? `<p class="period-credits">Photos: ${photos
        .map((town) => `<a href="${safe(town.photo.link || "https://unsplash.com/")}" target="_blank" rel="noopener">${safe(town.photo.photographer)}</a>`)
        .join(", ")} on <a href="https://unsplash.com/?utm_source=geologs&amp;utm_medium=referral" target="_blank" rel="noopener">Unsplash</a></p>`
    : "";
  return `<section class="card flush period-hero">${mosaic}<div class="period-hero-text"><span class="period-kicker">${value.kind === "year" ? "Your year" : "Your month"}</span><h2>${safe(periodTitle(value.period))}</h2><p>${safe(story.join(" · "))}</p>${credits}</div></section>`;
}

function factsMarkup(value) {
  const totals = value.totals;
  const facts = [
    ["Distance", distanceText(totals.distance_km || 0)],
    ["Moving", durationText((totals.moving_hours || 0) * 60)],
    ["Trips", (totals.trips || 0).toLocaleString()],
    ["Places", (totals.places || 0).toLocaleString(), value.new_place_count ? `${value.new_place_count} new` : ""],
    ...(totals.fuel_cost ? [["Fuel", moneyText(totals.fuel_cost, totals.currency || undefined)]] : []),
  ];
  return `<dl class="period-facts">${facts
    .map(([label, text, note]) => `<div><dt>${safe(label)}</dt><dd>${safe(text)}</dd>${note ? `<small>${safe(note)}</small>` : ""}</div>`)
    .join("")}</dl>`;
}

function shade(distance, most) {
  // A day with data but no trips still shows faintly, apart from a day with nothing.
  return distance > 0 ? Math.round(18 + 82 * Math.sqrt(distance / most)) : 8;
}

function monthGrid(monthKey, byDate, most, compact) {
  const [year, month] = monthKey.split("-").map(Number);
  const first = new Date(year, month - 1, 1);
  const days = new Date(year, month, 0).getDate();
  const lead = (first.getDay() + 6) % 7;
  const today = localDateKey();
  let cells = "";
  for (let index = 0; index < lead; index++) cells += '<span class="cal-blank"></span>';
  for (let day = 1; day <= days; day++) {
    const key = `${monthKey}-${String(day).padStart(2, "0")}`;
    const item = byDate.get(key);
    const level = item ? shade(item.distance_km, most) : 0;
    const future = key > today;
    const label = item ? `${longDate(key)}: ${distanceText(item.distance_km)}, ${plural(item.trips, "trip")}` : longDate(key);
    cells += compact
      ? `<span class="cal-cell${item ? " has" : ""}${future ? " future" : ""}" style="--level:${level}%" title="${safe(label)}"></span>`
      : `<button type="button" class="cal-cell${item ? " has" : ""}${future ? " future" : ""}" style="--level:${level}%" data-open-day="${key}" title="${safe(label)}"${item ? "" : " disabled"}><span>${day}</span>${item?.distance_km ? `<small>${safe(distanceText(item.distance_km))}</small>` : ""}</button>`;
  }
  return cells;
}

function calendarMarkup(value) {
  const byDate = new Map(value.days.map((day) => [day.date, day]));
  const most = Math.max(1, ...value.days.map((day) => day.distance_km));
  if (value.kind === "month")
    return `<section class="card period-card period-calendar"><div class="period-card-head"><h3>Every day</h3><span class="cal-key"><i style="--level:18%"></i><i style="--level:55%"></i><i style="--level:100%"></i> farther</span></div><div class="cal-weekdays">${WEEKDAYS.map((day) => `<span>${day}</span>`).join("")}</div><div class="cal-grid">${monthGrid(value.period, byDate, most, false)}</div></section>`;
  return `<section class="card period-card period-calendar"><div class="period-card-head"><h3>Every month</h3><span class="cal-key"><i style="--level:18%"></i><i style="--level:55%"></i><i style="--level:100%"></i> farther</span></div><div class="year-grid">${value.months
    .map(
      (month) =>
        `<button type="button" class="year-month" data-open-month="${month.month}"${month.days ? "" : " disabled"}><span class="year-month-head"><strong>${safe(new Date(Number(month.month.slice(0, 4)), Number(month.month.slice(5)) - 1, 1).toLocaleDateString(undefined, { month: "short" }))}</strong><small>${month.days ? safe(distanceText(month.distance_km)) : "—"}</small></span><span class="cal-grid mini">${monthGrid(month.month, byDate, most, true)}</span></button>`,
    )
    .join("")}</div></section>`;
}

function townsMarkup(value) {
  if (!value.towns.length) return "";
  return `<section class="card period-card"><div class="period-card-head"><h3>Towns</h3>${value.pending.towns ? '<small class="period-pending">Finding more…</small>' : ""}</div><div class="town-list">${value.towns
    .slice(0, 8)
    .map(
      (town) =>
        `<div class="town">${town.photo ? `<span class="town-photo" style="background-image:url('${safe(town.photo.thumb || town.photo.url).replace(/'/g, "%27")}')"></span>` : '<span class="town-photo none" aria-hidden="true"></span>'}<span class="town-text"><strong>${safe(town.name)}</strong><small>${safe([town.region, town.country].filter(Boolean).join(", "))}</small></span><span class="town-time">${safe(durationText(town.observed_minutes))}<small>${plural(town.places, "place")}</small></span></div>`,
    )
    .join("")}</div></section>`;
}

function placesMarkup(value) {
  const most = Math.max(1, ...value.places.map((place) => place.observed_minutes));
  const list = value.places.slice(0, 15);
  return `<section class="card period-card"><div class="period-card-head"><h3>Where you spent time</h3>${value.home ? `<small>Plus ${safe(durationText(value.home.observed_minutes))} at home</small>` : ""}</div>${
    list.length
      ? `<ol class="period-places">${list
          .map(
            (place, index) =>
              `<li><span class="period-rank">${index + 1}</span>${placeIcon(place)}<span class="period-place-text"><strong>${safe(place.name || "Unnamed place")}</strong><small>${safe([plural(place.visits, "visit"), plural(place.days, "day")].join(" · "))}</small><span class="time-bar" aria-hidden="true"><i style="width:${Math.max(3, Math.round((place.observed_minutes / most) * 100))}%"></i></span></span><b class="place-time">${safe(durationText(place.observed_minutes))}</b></li>`,
          )
          .join("")}</ol>`
      : '<p class="empty">No places away from home.</p>'
  }</section>`;
}

function newPlacesMarkup(value) {
  if (!value.new_places.length) return "";
  return `<section class="card period-card"><div class="period-card-head"><h3>First visits</h3><small>${plural(value.new_place_count, "new place")}</small></div><div class="new-places">${value.new_places
    .slice(0, 16)
    .map((place) => `<span class="new-place">${placeIcon(place)}<span>${safe(place.display_name)}</span></span>`)
    .join("")}</div></section>`;
}

function highlightsMarkup(value) {
  const items = [];
  const busiest = value.highlights.busiest_day;
  if (busiest)
    items.push(["Farthest day", longDate(busiest.date), `${distanceText(busiest.distance_km)} · ${plural(busiest.trips, "trip")}`, busiest.date]);
  const trip = value.highlights.longest_trip;
  if (trip)
    items.push(["Longest trip", `${trip.from || "A stay"} → ${trip.to || "a stay"}`, `${modeName(trip.mode)} ${distanceText(trip.distance_km)} · ${longDate(trip.date)}`, trip.date]);
  const days = value.weekdays.filter((day) => day.days);
  if (days.length > 2) {
    const top = days.reduce((best, day) => (day.distance_km / day.days > best.distance_km / best.days ? day : best));
    items.push(["Busiest weekday", WEEKDAYS[top.weekday], `${distanceText(top.distance_km / top.days)} on average`]);
  }
  if (!items.length) return "";
  return `<section class="period-highlights">${items
    .map(
      ([label, title, detail, date]) =>
        `<${date ? `button type="button" data-open-day="${date}"` : "div"} class="card highlight"><span>${safe(label)}</span><strong>${safe(title)}</strong><small>${safe(detail)}</small></${date ? "button" : "div"}>`,
    )
    .join("")}</section>`;
}

function travelMarkup(value) {
  const modes = Object.entries(value.activity).filter(([mode, item]) => modeInfo[mode] && item.distance_km > 0);
  const total = modes.reduce((sum, [, item]) => sum + item.distance_km, 0);
  const days = value.weekdays;
  const most = Math.max(0.1, ...days.map((day) => (day.days ? day.distance_km / day.days : 0)));
  const modeBlock = modes.length
    ? `<div class="mode-share" aria-hidden="true">${modes.map(([mode, item]) => `<i style="flex:${item.distance_km};background:${modeInfo[mode].color}"></i>`).join("")}</div><ul class="mode-list">${modes
        .sort((a, b) => b[1].distance_km - a[1].distance_km)
        .map(([mode, item]) => `<li>${modeIcon(mode)}<strong>${safe(modeInfo[mode].noun)}</strong><span>${safe(distanceText(item.distance_km))} · ${safe(durationText(item.minutes))}</span><b>${Math.round((item.distance_km / total) * 100)}%</b></li>`)
        .join("")}</ul>`
    : '<p class="empty">No trips.</p>';
  const rhythm = `<div class="weekday-bars">${days
    .map((day) => {
      const average = day.days ? day.distance_km / day.days : 0;
      return `<span title="${safe(`${WEEKDAYS[day.weekday]}: ${distanceText(average)} a day`)}"><i style="height:${Math.max(4, Math.round((average / most) * 100))}%"></i><small>${WEEKDAYS[day.weekday].slice(0, 1)}</small></span>`;
    })
    .join("")}</div>`;
  return `<section class="card period-card period-travel"><div><div class="period-card-head"><h3>How you got around</h3></div>${modeBlock}</div><div><div class="period-card-head"><h3>Week rhythm</h3><small>Distance a day</small></div>${rhythm}</div></section>`;
}

function renderPeriod(value) {
  const box = $("summaries-period");
  if (!value.days_with_data) {
    box.innerHTML = `<div class="card summary-empty-state"><h2>Nothing recorded in ${safe(periodTitle(value.period))}</h2><p>No device sent a location then.</p></div>`;
    return;
  }
  const pending = value.pending.days
    ? `<div class="period-progress" role="status"><span class="summary-spinner" aria-hidden="true"></span>Adding up ${plural(value.pending.days, "more day")}…</div>`
    : "";
  box.innerHTML = `${heroMarkup(value)}${pending}${factsMarkup(value)}${highlightsMarkup(value)}<div class="period-grid"><div class="period-column">${calendarMarkup(value)}${travelMarkup(value)}</div><div class="period-column">${townsMarkup(value)}${placesMarkup(value)}${newPlacesMarkup(value)}</div></div>`;
  box.querySelectorAll("[data-open-day]").forEach((button) => (button.onclick = () => openDay(button.dataset.openDay)));
  box.querySelectorAll("[data-open-month]").forEach(
    (button) =>
      (button.onclick = () => {
        state.summaryPeriod = button.dataset.openMonth;
        showScale("month", button.dataset.openMonth);
      }),
  );
}

export function initPeriods(openDaySummary) {
  openDay = (date) => {
    showScale("day");
    openDaySummary(date, true);
  };
  document.querySelectorAll("[data-summary-scale]").forEach((button) => {
    button.onclick = () => showScale(button.dataset.summaryScale);
  });
  $("period-older").onclick = () => openPeriod(shiftPeriod(state.summaryPeriod, -1));
  $("period-newer").onclick = () => openPeriod(shiftPeriod(state.summaryPeriod, 1));
  window.addEventListener("keydown", (event) => {
    if (!$("summaries-view").classList.contains("active") || state.summaryScale === "day") return;
    if (event.target.closest("input, textarea, select")) return;
    if (event.key === "ArrowLeft") $("period-older").click();
    if (event.key === "ArrowRight" && !$("period-newer").disabled) $("period-newer").click();
  });
}

// Opening Summaries: the scale last used here, and its period brought up to date.
export function refreshPeriod() {
  if (!state.summaryScale) showScale(savedScale());
  else if (state.summaryScale !== "day" && state.summaryPeriod) openPeriod(state.summaryPeriod);
}
