// The detail tabs under the History map: the day's places, speed, samples, and recorded data.
import { api, post } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { clock, clockPrecise, durationText, formatTime, kmh, lengthText, longDate, plural, speedUnit, toSpeedUnit } from "../core/format.js";
import { state } from "../core/state.js";
import { onEach, rowActions } from "../ui/actions.js";
import { modeInfo, modeName, placeIcon, sourceName, visitNumbers, zoneText } from "../ui/icons.js";
import { focusPlaceOnMap, params, refreshExplore } from "./history.js";
import { refreshInsights } from "./patterns.js";
import { dayVisits, editVisitedPlace } from "./place-dialogs.js";
import { showSettingsPage } from "./settings.js";
import { showView } from "./shell.js";
import { openSampleStayDialog, stayEditsSection, undoStayEdit } from "./stay-edits.js";

const ROLES = { arrival: "Arrival", departure: "Departure", latest: "Latest at stop", movement: "Moving" };

export function renderDayPlaces(day) {
  const numbers = visitNumbers(day).byPlace;
  const box = $("visited-places");
  box.innerHTML =
    day.places_including_home
      .map((place) => {
        const name = place.name || place.display_name;
        const activities = Object.entries(place.activities || {})
          .map(([mode, count]) => modeName(mode) + (count > 1 ? ` ×${count}` : ""))
          .join(", ");
        const details = [
          zoneText(place.zone, name),
          `${durationText(place.observed_minutes)}`,
          place.visits > 1 ? `${place.visits} visits` : "",
          `first seen ${clock(place.first_seen)}`,
          activities,
        ].filter(Boolean);
        const address =
          place.address && place.address !== place.name ? `<span>${safe(place.address)}</span>` : "";
        return `<div class="list-item"><div class="place-main">${numbers.has(place.cluster_id) ? placeIcon(place) : placeIcon({ name: "Home" })}<div><strong>${safe(name)}</strong><small>${safe(details.join(" · "))}</small>${address}</div></div>${rowActions(
          [
            { icon: "map", label: "Show on map", data: `data-day-place="${place.cluster_id}"` },
            { icon: "edit", label: "Rename this place", data: `data-rename-day-place="${place.cluster_id}"` },
          ],
        )}</div>`;
      })
      .join("") ||
    '<p class="empty">No confirmed stays on this day. Sparse fixes can miss a short visit.</p>';
  const find = (id) => day.places_including_home.find((item) => item.cluster_id === +id);
  onEach(box, "day-place", (id) => {
    const place = find(id);
    focusPlaceOnMap({ ...place, display_name: place.name || place.address || "Visited place" });
  });
  onEach(box, "rename-day-place", (id) => editVisitedPlace(find(id), dayVisits(find(id))));
}

// The trip, or else area stay, that covers a moment: it colours the speed line.
function modeAt(day, time) {
  const trip = day.trips.find((item) => Date.parse(item.start) <= time && time <= Date.parse(item.end));
  if (trip) return trip.mode;
  const stay = day.stays.find(
    (item) =>
      item.kind === "area" && Date.parse(item.started_at) <= time && time <= Date.parse(item.ended_at),
  );
  return stay?.activity || null;
}

// A round scale with four or five steps: 0, 10, 20, 30, 40, 50 rather than 0, 13, 25, 38.
function niceScale(value) {
  const step = [5, 10, 20, 25, 50, 100].find((size) => value / size <= 5) || Math.ceil(value / 500) * 100;
  const ceiling = Math.max(step, Math.ceil(value / step) * step);
  const ticks = [];
  for (let tick = 0; tick <= ceiling; tick += step) ticks.push(tick);
  return { ceiling, ticks };
}

// What a fix was part of, in the day's own terms.
function contextAt(day, time) {
  const trip = day.trips.find((item) => Date.parse(item.start) <= time && time <= Date.parse(item.end));
  if (trip) return `${modeInfo[trip.mode]?.label || "Trip"}${trip.loop ? " round trip" : " trip"}`;
  const stay = day.stays.find(
    (item) => Date.parse(item.started_at) <= time && time <= Date.parse(item.ended_at),
  );
  return stay ? `At ${stay.display_name || "a stay"}` : "Between stays";
}

const STOP_MINUTES = 10;

// Speed during the day's drives only, from the first one's start to the last one's end.
export function renderSpeedChart(day) {
  const box = $("speed-chart");
  const drives = day.trips
    .filter((trip) => trip.mode === "driving")
    .map((trip) => [Date.parse(trip.start), Date.parse(trip.end)])
    .sort((a, b) => a[0] - b[0]);
  const driveAt = (time) => drives.findIndex(([from, to]) => from <= time && time <= to);
  const fixes = [...state.timeline]
    .reverse()
    .filter((fix) => fix.speed_mps != null)
    .map((fix) => ({ time: Date.parse(fix.timestamp), kmh: toSpeedUnit(fix.speed_mps * 3.6) }))
    .map((fix) => ({ ...fix, drive: driveAt(fix.time) }))
    .filter((fix) => fix.drive >= 0);
  if (fixes.length < 2) {
    box.innerHTML = `<p class="empty">${drives.length ? "Speed shows up once a drive has at least two timed fixes." : "No driving on this day."}</p>`;
    return;
  }
  const start = drives[0][0];
  const end = Math.max(drives.at(-1)[1], start + 60e3);
  const moving = fixes.filter((fix) => fix.kmh >= 2);
  const top = Math.max(...fixes.map((fix) => fix.kmh));
  const average = moving.length ? moving.reduce((sum, fix) => sum + fix.kmh, 0) / moving.length : 0;
  const { ceiling, ticks } = niceScale(top);
  const W = 1000,
    H = 300;
  const x = (time) => (((time - start) / (end - start)) * W).toFixed(1);
  const y = (kmh) => (H - (Math.min(kmh, ceiling) / ceiling) * H).toFixed(1);
  const gaps = (day.coverage_gaps || [])
    .map((gap) => [Date.parse(gap.start), Date.parse(gap.end)])
    .filter(([from, to]) => to > start && from < end);
  const crossesGap = (a, b) => gaps.some(([from, to]) => a <= from && b >= to);
  // One path per run of fixes within a drive. Time between drives, or a long interval inside one
  // (key fixes skip the inside of stops), is drawn flat at zero, never as a slope across it.
  const runs = [],
    stops = [];
  for (let index = 1; index < fixes.length; index++) {
    const a = fixes[index - 1],
      b = fixes[index];
    if (crossesGap(a.time, b.time)) continue;
    if (a.drive !== b.drive || b.time - a.time > STOP_MINUTES * 60e3) {
      stops.push([a, b]);
      continue;
    }
    const last = runs.at(-1);
    if (last && last.end === a) last.points.push(b);
    else runs.push({ points: [a, b] });
    runs.at(-1).end = b;
  }
  const color = modeInfo.driving.color;
  const lines =
    stops
      .map(
        ([a, b]) =>
          `<polyline points="${x(a.time)},${y(a.kmh)} ${x(a.time)},${H} ${x(b.time)},${H} ${x(b.time)},${y(b.kmh)}" class="stop"/>`,
      )
      .join("") +
    runs
      .map(
        (run) =>
          `<polyline points="${run.points.map((fix) => `${x(fix.time)},${y(fix.kmh)}`).join(" ")}" stroke="${color}"/>`,
      )
      .join("");
  // Time labels at a round step, so about six fit along the drives.
  const stepMinutes = [5, 10, 15, 30, 60, 120, 180].find((size) => (end - start) / (size * 60e3) <= 6) || 360;
  const step = stepMinutes * 60e3;
  const labels = [];
  for (let time = Math.ceil(start / step) * step; time <= end; time += step) labels.push(time);
  const timeLabel = (time) =>
    new Date(time).toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
      ...(state.settings?.timezone ? { timeZone: state.settings.timezone } : {}),
    });
  const gapBands = gaps
    .map(
      ([from, to]) =>
        `<rect x="${x(Math.max(from, start))}" y="0" width="${(x(Math.min(to, end)) - x(Math.max(from, start))).toFixed(1)}" height="${H}" class="gap-band"/>`,
    )
    .join("");
  box.innerHTML = `<div class="chart-summary"><div><span>Top speed</span><strong>${Math.round(top)} ${speedUnit()}</strong></div><div><span>Average while moving</span><strong>${moving.length ? Math.round(average) + " " + speedUnit() : "—"}</strong></div><div><span>Drives</span><strong>${drives.length.toLocaleString()}</strong></div></div>
    <figure class="speed-chart" aria-label="Speed while driving, up to ${Math.round(top)} ${speedUnit()}">
      <div class="chart-y">${ticks
        .slice()
        .reverse()
        .map((tick) => `<span>${tick}</span>`)
        .join("")}</div>
      <div class="chart-plot"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">${gapBands}${ticks
        .map((tick) => `<line x1="0" x2="${W}" y1="${y(tick)}" y2="${y(tick)}" class="grid"/>`)
        .join("")}<g class="speed-lines">${lines}</g></svg></div>
      <div class="chart-x">${labels.map((time) => `<span style="left:${((time - start) / (end - start)) * 100}%">${safe(timeLabel(time))}</span>`).join("")}</div>
      <figcaption>${speedUnit()} · while driving, ${safe(timeLabel(start))} to ${safe(timeLabel(end))}${gaps.length ? " · shaded where location is missing" : ""}</figcaption>
    </figure>`;
}

export function renderSamples() {
  const timeline = state.sampleView === "timeline";
  const rows = timeline ? state.timeline : state.points;
  const start = state.rawPage * state.rawPageSize;
  $("sample-count").textContent = timeline
    ? `${plural(state.timeline.length, "key fix", "key fixes")} from ${plural(state.rawTotal, "raw sample")}. Long stops keep only their edges.`
    : rows.length
      ? `${(start + 1).toLocaleString()}–${(start + rows.length).toLocaleString()} of ${plural(state.rawTotal, "raw sample")}`
      : "No raw samples on this page.";
  const visible = timeline ? rows.slice(0, 200) : rows;
  const shownIds = new Set(visible.map((point) => point.id));
  for (const id of state.selectedSamples) if (!shownIds.has(id)) state.selectedSamples.delete(id);
  const body = $("points-table");
  body.innerHTML =
    visible
      .map((point, index) => {
        const source = sourceName(point.source);
        const origin =
          point.speed_origin === "estimated"
            ? ' <small title="Calculated from positions">est.</small>'
            : point.speed_origin === "reported_adjusted"
              ? ' <small title="Device speed with your adjustment">adj.</small>'
              : "";
        const ticked = state.selectedSamples.has(point.id);
        return `<tr${ticked ? ' class="selected"' : ""}><td class="select-col"><input type="checkbox" data-select-sample="${point.id}" aria-label="Select the sample at ${safe(clockPrecise(point.timestamp))}"${ticked ? " checked" : ""}></td><td title="${safe(formatTime(point.timestamp))}">${safe(clockPrecise(point.timestamp))}</td><td>${safe(timeline ? ROLES[point.role] || "Sample" : "Raw sample")}</td><td title="${safe(point.source)}">${safe(source.device || source.kind)}</td><td>${safe(contextAt(state.day, Date.parse(point.timestamp)))}</td><td class="numeric">${kmh(point)}${origin}</td><td class="mono">${point.lat.toFixed(5)}, ${point.lon.toFixed(5)}</td><td class="numeric">${lengthText(point.accuracy_m)}</td><td>${rowActions([
          { icon: "map", label: "Show on map", data: `data-sample-map="${index}"` },
          { icon: "delete", label: "Delete this sample", data: `data-delete-sample="${point.id}"`, tone: "danger" },
        ])}</td></tr>`;
      })
      .join("") || '<tr><td colspan="9" class="empty">No fixes on this day.</td></tr>';
  onEach(body, "sample-map", (index) =>
    focusPlaceOnMap({ ...visible[+index], display_name: formatTime(visible[+index].timestamp) }),
  );
  onEach(body, "delete-sample", (id) => deleteSamples([+id]));
  body.querySelectorAll("[data-select-sample]").forEach((input) => {
    input.onchange = () => {
      const id = +input.dataset.selectSample;
      if (input.checked) state.selectedSamples.add(id);
      else state.selectedSamples.delete(id);
      input.closest("tr").classList.toggle("selected", input.checked);
      renderSampleSelection(visible.length);
    };
  });
  renderSampleSelection(visible.length);
  $("raw-pagination").hidden = timeline || state.rawTotal <= state.rawPageSize;
  $("raw-page").textContent =
    `Page ${state.rawPage + 1} of ${Math.max(1, Math.ceil(state.rawTotal / state.rawPageSize))}`;
  $("raw-previous").disabled = state.rawPage === 0;
  $("raw-next").disabled = start + rows.length >= state.rawTotal;
}

function renderSampleSelection(shown) {
  const count = state.selectedSamples.size;
  $("sample-selection").hidden = !count;
  $("sample-selected-count").textContent = `${plural(count, "sample")} selected`;
  const all = $("select-all-samples");
  all.disabled = !shown;
  all.checked = shown > 0 && count === shown;
  all.indeterminate = count > 0 && count < shown;
}

function sourceList(sources) {
  const names = sources.map((source) => {
    const name = sourceName(source);
    return name.device || name.kind;
  });
  return names.length > 2 ? `${names.slice(0, -1).join(", ")}, and ${names.at(-1)}` : names.join(" and ");
}

// Ask the server what would go, spell it out, and delete only once confirmed.
async function deleteSamples(ids) {
  let preview;
  try {
    preview = await post("/api/observations/delete", { ids });
  } catch (error) {
    toast(error.message);
    return;
  }
  const count = preview.samples;
  const devices = sourceList(preview.sources);
  const when =
    count === 1
      ? formatTime(preview.first)
      : `${clockPrecise(preview.first)} – ${clockPrecise(preview.last)}${preview.dates.length === 1 ? ` on ${longDate(preview.dates[0])}` : ""}`;
  $("delete-samples-title").textContent = `Delete ${count === 1 ? "this sample" : plural(count, "sample")}?`;
  $("delete-samples-summary").textContent = `From ${devices} · ${when}`;
  const days = preview.dates.map(longDate).join(", ");
  $("delete-samples-effects").innerHTML = [
    `<strong>This can't be undone.</strong> ${count === 1 ? "The sample is" : "The samples are"} removed from your archive for good.`,
    `Stays, trips, and visited places from ${safe(devices)} are recalculated without ${count === 1 ? "it" : "them"}, which can take a moment.`,
    `Daily summaries for ${safe(days)} are rebuilt to match.`,
  ]
    .map((line) => `<li>${line}</li>`)
    .join("");
  const triggers = preview.reminder_triggers;
  $("delete-samples-reminders").hidden = !triggers.length;
  $("delete-samples-reminders").innerHTML = triggers.length
    ? `<strong>${count === 1 ? "This sample" : triggers.length === 1 ? "One of these samples" : "Some of these samples"} set off ${triggers.length === 1 ? "a reminder" : plural(triggers.length, "reminder")}</strong><span>${triggers
        .map((item) => `“${safe(item.reminder)}” at ${safe(clock(item.triggered_at))}`)
        .join(", ")}. The trigger is removed from the reminder inbox; the reminder itself is unchanged.</span>`
    : "";
  const button = $("confirm-delete-samples");
  button.textContent = `Delete ${count === 1 ? "sample" : plural(count, "sample")}`;
  button.disabled = false;
  button.onclick = async () => {
    button.disabled = true;
    button.textContent = "Deleting…";
    try {
      await post("/api/observations/delete", { ids, confirm: true });
      $("delete-samples-dialog").close();
      state.selectedSamples.clear();
      // Step back a page if this one is now past the end.
      const remaining = state.rawTotal - count;
      if (state.rawPage > 0 && state.rawPage * state.rawPageSize >= remaining) state.rawPage -= 1;
      await Promise.all([refreshExplore(false), refreshInsights()]);
      toast(`Deleted ${plural(count, "sample")}`);
    } catch (error) {
      button.disabled = false;
      button.textContent = "Try again";
      toast(error.message);
    }
  };
  $("delete-samples-dialog").showModal();
}

// What was recorded on this day, by which device, and where it is missing.
export function renderDayData(day) {
  const counts = Object.entries(day.samples_by_source || {}).sort((a, b) => b[1] - a[1]);
  const shown = new Set(day.timeline_sources || []);
  const sources = counts
    .map(([source, count]) => {
      const name = sourceName(source);
      const share = day.points ? Math.round((count / day.points) * 100) : 0;
      return `<div class="list-item"><div class="place-main"><span class="health-dot success" aria-hidden="true"></span><div><strong>${safe(name.kind)}${name.device ? ` <span class="muted">· ${safe(name.device)}</span>` : ""}</strong><small>${plural(count, "fix", "fixes")} · ${share}% of the day's samples${counts.length > 1 && shown.has(source) ? " · used for the timeline" : ""}</small></div></div></div>`;
    })
    .join("");
  const gaps = (day.coverage_gaps || [])
    .map(
      (gap) =>
        `<li><strong>${safe(clock(gap.start))} – ${safe(clock(gap.end))}</strong><span>${safe(durationText(gap.minutes))} without location</span></li>`,
    )
    .join("");
  $("day-data").innerHTML =
    `<section><h3>Recorded by</h3><div class="list">${sources || '<p class="empty">No source recorded this day.</p>'}</div></section>
    <section><h3>Coverage</h3>${
      gaps
        ? `<ul class="gap-list">${gaps}</ul><p class="help">Stays and trips are inferred from samples; where none arrived, GeoLogs can't say where you were.</p>`
        : '<p class="callout success">No gaps longer than 30 minutes on this day.</p>'
    }</section>
    ${stayEditsSection(day)}
    <div class="data-foot"><button type="button" class="button secondary small" id="open-sources">All devices and sources</button></div>`;
  $("open-sources").onclick = () => {
    showView("settings");
    showSettingsPage("settings-sources");
  };
  onEach($("day-data"), "undo-edit", (id) => undoStayEdit(+id));
}

export function renderDayDetails(day) {
  renderDayPlaces(day);
  renderSpeedChart(day);
  renderSamples();
  renderDayData(day);
}

async function refreshRawPage() {
  const query = params();
  query.set("limit", state.rawPageSize);
  query.set("offset", state.rawPage * state.rawPageSize);
  state.points = await api("/api/observations?" + query);
  renderSamples();
}

export function initDayDetails() {
  document.querySelectorAll("#history-details .detail-tab").forEach((button) => {
    button.onclick = () => {
      const name = button.dataset.panel;
      document.querySelectorAll("#history-details .detail-tab").forEach((tab) => {
        const active = tab === button;
        tab.classList.toggle("active", active);
        tab.setAttribute("aria-selected", String(active));
      });
      document
        .querySelectorAll("#history-details .detail-panel")
        .forEach((panel) => panel.classList.toggle("active", panel.dataset.panelView === name));
    };
  });
  document.querySelectorAll("[data-sample-view]").forEach((button) => {
    button.onclick = () => {
      state.sampleView = button.dataset.sampleView;
      document
        .querySelectorAll("[data-sample-view]")
        .forEach((tab) => tab.classList.toggle("active", tab === button));
      renderSamples();
    };
  });
  const page = (step) => async () => {
    const next = state.rawPage + step;
    if (next < 0 || next * state.rawPageSize >= state.rawTotal) return;
    state.rawPage = next;
    try {
      await refreshRawPage();
    } catch (error) {
      toast(error.message);
    }
  };
  $("raw-previous").onclick = page(-1);
  $("raw-next").onclick = page(1);
  $("select-all-samples").onchange = () => {
    const boxes = [...$("points-table").querySelectorAll("[data-select-sample]")];
    for (const input of boxes) {
      input.checked = $("select-all-samples").checked;
      input.closest("tr").classList.toggle("selected", input.checked);
      if (input.checked) state.selectedSamples.add(+input.dataset.selectSample);
      else state.selectedSamples.delete(+input.dataset.selectSample);
    }
    renderSampleSelection(boxes.length);
  };
  $("clear-sample-selection").onclick = () => {
    state.selectedSamples.clear();
    renderSamples();
  };
  $("delete-selected-samples").onclick = () => deleteSamples([...state.selectedSamples]);
  $("stay-from-samples").onclick = () => {
    const rows = state.sampleView === "timeline" ? state.timeline : state.points;
    openSampleStayDialog(rows.filter((row) => state.selectedSamples.has(row.id)));
  };
  const closeDelete = () => $("delete-samples-dialog").close();
  $("close-delete-samples").onclick = $("cancel-delete-samples").onclick = closeDelete;
  $("delete-samples-dialog").onclick = (event) => {
    if (event.target === $("delete-samples-dialog")) closeDelete();
  };
}
