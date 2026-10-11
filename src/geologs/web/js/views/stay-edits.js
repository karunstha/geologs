// Stops you add on a trip and stays you remove, each undoable; kept by source and time window.
import { post } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { clock, clockPrecise, durationText } from "../core/format.js";
import { state } from "../core/state.js";
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

// The trip's fixes in time order, from the day's key fixes (trips keep every fix).
function tripFixes(trip) {
  const start = Date.parse(trip.start),
    end = Date.parse(trip.end);
  return state.timeline
    .filter((fix) => fix.source === trip.source && Date.parse(fix.timestamp) >= start && Date.parse(fix.timestamp) <= end)
    .sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
}

// The longest stretch where you stayed within STILL_RADIUS_M: the likely missed stop.
function stillestStretch(fixes) {
  let best = null;
  for (let i = 0; i < fixes.length; i++) {
    let j = i;
    while (j + 1 < fixes.length && metres(fixes[i], fixes[j + 1]) <= STILL_RADIUS_M && (fixes[j + 1].speed_mps ?? 0) < 2)
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

export function openStopDialog(trip) {
  const fixes = tripFixes(trip);
  if (fixes.length < 2) {
    toast("This trip has too few samples to place a stop on.");
    return;
  }
  const options = fixes
    .map((fix, index) => {
      const kmh = fix.speed_mps == null ? "" : ` · ${Math.round(fix.speed_mps * 3.6)} km/h`;
      return `<option value="${index}">${safe(clockPrecise(fix.timestamp) + kmh)}</option>`;
    })
    .join("");
  $("stop-from").innerHTML = options;
  $("stop-to").innerHTML = options;
  const best = stillestStretch(fixes);
  $("stop-from").value = String(best ? best.from : 0);
  $("stop-to").value = String(best ? best.to : Math.min(1, fixes.length - 1));
  $("stop-suggestion").hidden = !best;
  $("stop-suggestion").textContent = best
    ? `Suggested: you were still for ${durationText(best.seconds / 60)} from ${clock(fixes[best.from].timestamp)}.`
    : "";
  $("stop-dialog-note").textContent =
    `On the trip ${trip.from_name ? "from " + trip.from_name : ""}${trip.to_name ? " to " + trip.to_name : ""}, ${clock(trip.start)} – ${clock(trip.end)}. The trip splits in two around the stop.`;
  const update = () => {
    const from = +$("stop-from").value,
      to = +$("stop-to").value;
    const ok = to > from;
    $("stop-error").hidden = ok;
    $("stop-error").textContent = ok ? "" : "Pick a time to leave after you arrived.";
    $("stop-detail").textContent = ok
      ? `${durationText((Date.parse(fixes[to].timestamp) - Date.parse(fixes[from].timestamp)) / 60000)} stop, placed where your samples were then.`
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
          source: trip.source,
          started_at: fixes[+$("stop-from").value].timestamp,
          ended_at: fixes[+$("stop-to").value].timestamp,
        },
        "Stop added",
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

// The day's changes, for the Data tab, so a removed stay can always be brought back.
export function stayEditsSection(day) {
  const edits = day.stay_edits || [];
  if (!edits.length) return "";
  const rows = edits
    .map((edit) => {
      const what = edit.action === "add" ? "Added a stop" : "Removed a stay";
      const minutes = (Date.parse(edit.ended_at) - Date.parse(edit.started_at)) / 60000;
      return `<div class="list-item"><div><strong>${what}</strong><small>${safe(`${clock(edit.started_at)} – ${clock(edit.ended_at)} · ${durationText(minutes)}`)}</small></div><button type="button" class="button secondary small" data-undo-edit="${edit.id}">Undo</button></div>`;
    })
    .join("");
  return `<section><h3>Your changes to stays</h3><div class="list">${rows}</div></section>`;
}

export function initStayEdits() {
  const close = () => $("stop-dialog").close();
  $("close-stop-dialog").onclick = $("cancel-stop").onclick = close;
  $("stop-dialog").onclick = (event) => {
    if (event.target === $("stop-dialog")) close();
  };
}
