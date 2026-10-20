// Stops you add on a trip and stays you remove, each undoable; kept by source and time window.
import { post } from "../core/api.js";
import { $, closeOnBackdrop, safe, toast } from "../core/dom.js";
import { clock, clockPrecise, durationText, speedText } from "../core/format.js";
import { state } from "../core/state.js";
import { sourceName } from "../ui/icons.js";
import { refreshExplore } from "./history.js";
import { refreshInsights } from "./patterns.js";

// Fixes this close to where a stretch began count as standing still, for the suggestion.
const STILL_RADIUS_M = 40;

function metres(a, b) {
  const rad = Math.PI / 180;
  const x = (b.lon - a.lon) * rad * Math.cos(((a.lat + b.lat) / 2) * rad);
  const y = (b.lat - a.lat) * rad;
  return Math.hypot(x, y) * 6371000;
}

// A source's fixes in time order between two times, from the day's key fixes (trips keep every fix).
function fixesBetween(source, start, end) {
  return state.timeline
    .filter((fix) => fix.source === source && Date.parse(fix.timestamp) >= start && Date.parse(fix.timestamp) <= end)
    .sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
}

function deviceName(source) {
  const name = sourceName(source);
  return name.device || name.kind;
}

// The longest stretch where you stayed within STILL_RADIUS_M, outside stays already
// on the timeline: the likely missed stop. A quiet gap between two close fixes counts.
function stillestStretch(fixes, source) {
  const known = (state.day?.stays || [])
    .filter((stay) => stay.source === source)
    .map((stay) => [Date.parse(stay.started_at), Date.parse(stay.ended_at)]);
  const inStay = (fix) => known.some(([from, to]) => from <= Date.parse(fix.timestamp) && Date.parse(fix.timestamp) <= to);
  let best = null;
  for (let i = 0; i < fixes.length; i++) {
    if (inStay(fixes[i])) continue;
    let j = i;
    while (j + 1 < fixes.length && !inStay(fixes[j + 1]) && metres(fixes[i], fixes[j + 1]) <= STILL_RADIUS_M)
      j++;
    const seconds = (Date.parse(fixes[j].timestamp) - Date.parse(fixes[i].timestamp)) / 1000;
    if (j > i && (!best || seconds > best.seconds)) best = { from: i, to: j, seconds };
  }
  return best && best.seconds >= 60 ? best : null;
}

async function refreshAfterEdit() {
  await Promise.all([refreshExplore(false), refreshInsights()]);
}

export async function undoStayEdit(id, message = "Change undone") {
  try {
    await post("/api/stay-edits/delete", { id });
    await refreshAfterEdit();
    toast(message);
  } catch (error) {
    toast(error.message);
  }
}

async function saveEdit(edit, message) {
  const saved = await post("/api/stay-edits", edit);
  await refreshAfterEdit();
  toast(message, { label: "Undo", run: () => undoStayEdit(saved.id) });
}

export async function removeStay(stay) {
  try {
    if (stay.kind === "manual") {
      await post("/api/stay-edits/delete", { id: stay.edit_id });
      await refreshAfterEdit();
      toast("Added stop removed");
      return;
    }
    await saveEdit(
      { action: "remove", source: stay.source, started_at: stay.started_at, ended_at: stay.ended_at },
      stay.kind === "stop" ? "Quick stop removed" : "Stay removed",
    );
  } catch (error) {
    toast(error.message);
  }
}

// Keep a stop the detector found but could not confirm with a place.
export async function keepPossibleStop(stop) {
  try {
    await saveEdit(
      { action: "add", source: stop.source, started_at: stop.started_at, ended_at: stop.ended_at },
      "Stop kept",
    );
  } catch (error) {
    toast(error.message);
  }
}

// Pick when you arrived and left from `fixes` (one source, in time order), then save it as a stay.
function openStayDialog({ source, fixes, title, note, from, to }) {
  if (fixes.length < 2) {
    toast("There are too few samples there to place a stay. It needs one when you arrived and one when you left.");
    return;
  }
  const options = fixes
    .map((fix, index) => {
      const kmh = fix.speed_mps == null ? "" : ` · ${speedText(fix.speed_mps * 3.6)}`;
      return `<option value="${index}">${safe(clockPrecise(fix.timestamp) + kmh)}</option>`;
    })
    .join("");
  $("stop-from").innerHTML = options;
  $("stop-to").innerHTML = options;
  const best = from == null ? stillestStretch(fixes, source) : null;
  $("stop-from").value = String(from ?? best?.from ?? 0);
  $("stop-to").value = String(to ?? best?.to ?? 1);
  $("stop-suggestion").hidden = !best;
  $("stop-suggestion").textContent = best
    ? `Suggested: you were still for ${durationText(best.seconds / 60)} from ${clock(fixes[best.from].timestamp)}.`
    : "";
  $("stop-dialog-title").textContent = title;
  $("stop-dialog-note").textContent = note;
  const update = () => {
    const first = +$("stop-from").value,
      last = +$("stop-to").value;
    const ok = last > first;
    $("stop-error").hidden = ok;
    $("stop-error").textContent = ok ? "" : "Pick a time to leave after you arrived.";
    $("stop-detail").textContent = ok
      ? `${durationText((Date.parse(fixes[last].timestamp) - Date.parse(fixes[first].timestamp)) / 60000)}, placed where your samples were then.`
      : "";
    return ok;
  };
  $("stop-from").onchange = $("stop-to").onchange = update;
  update();
  $("stop-form").onsubmit = async (event) => {
    event.preventDefault();
    if (!update()) return;
    const button = $("stop-form").querySelector(".button.primary");
    button.disabled = true;
    try {
      await saveEdit(
        {
          action: "add",
          source,
          started_at: fixes[+$("stop-from").value].timestamp,
          ended_at: fixes[+$("stop-to").value].timestamp,
        },
        "Stay added",
      );
      $("stop-dialog").close();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  };
  $("stop-dialog").showModal();
}

// A stop on a selected trip.
export function openStopDialog(trip) {
  openStayDialog({
    source: trip.source,
    fixes: fixesBetween(trip.source, Date.parse(trip.start), Date.parse(trip.end)),
    title: "Add a stop",
    note: `On the trip ${trip.from_name ? "from " + trip.from_name : ""}${trip.to_name ? " to " + trip.to_name : ""}, ${clock(trip.start)} – ${clock(trip.end)}. The trip splits in two around the stop.`,
  });
}

// A stay anywhere on the shown day, from the Timeline's Add a stay button.
export function openDayStayDialog() {
  const day = state.day;
  if (!day) return;
  const source = day.timeline_sources?.[0] || Object.keys(day.samples_by_source || {})[0] || "";
  openStayDialog({
    source,
    fixes: fixesBetween(source, Date.parse(day.start), Date.parse(day.end)),
    title: "Add a stay",
    note: `Pick when you arrived and when you left, from ${deviceName(source)}'s samples. Trips around it are split to match.`,
  });
}

// A stay spanning the samples ticked in the Samples tab.
export function openSampleStayDialog(samples) {
  const sources = [...new Set(samples.map((sample) => sample.source))];
  if (sources.length > 1) {
    toast("Pick samples from one device to make a stay.");
    return;
  }
  const fixes = [...samples].sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
  openStayDialog({
    source: sources[0],
    fixes,
    title: "Make a stay",
    note: `From the ${fixes.length} samples you picked. Trips around it are split to match.`,
    from: 0,
    to: fixes.length - 1,
  });
}

// The day's changes, for the Data tab, so a removed stay can always be brought back.
export function stayEditsSection(day) {
  const edits = day.stay_edits || [];
  if (!edits.length) return "";
  const rows = edits
    .map((edit) => {
      const what = edit.action === "add" ? "Added a stay" : "Removed a stay";
      const minutes = (Date.parse(edit.ended_at) - Date.parse(edit.started_at)) / 60000;
      return `<div class="list-item"><div><strong>${what}</strong><small>${safe(`${clock(edit.started_at)} – ${clock(edit.ended_at)} · ${durationText(minutes)}`)}</small></div><button type="button" class="button secondary small" data-undo-edit="${edit.id}">Undo</button></div>`;
    })
    .join("");
  return `<section><h3>Your changes to stays</h3><div class="list">${rows}</div></section>`;
}

export function initStayEdits() {
  $("add-stay").onclick = openDayStayDialog;
  const close = () => $("stop-dialog").close();
  $("close-stop-dialog").onclick = $("cancel-stop").onclick = close;
  closeOnBackdrop($("stop-dialog"), close);
}
