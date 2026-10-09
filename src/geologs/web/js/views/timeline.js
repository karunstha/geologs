// The History day timeline and its travel-mode corrections.
import { post } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { clock, distanceText, durationText } from "../core/format.js";
import { state } from "../core/state.js";
import { selectItem, selectedItem, stayColor, tripKey } from "../map/map.js";
import { modeIcon, modeInfo, modeName, pinBadge, zoneText } from "../ui/icons.js";
import { refreshExplore } from "./history.js";

export function stayActivityText(stay) {
  if (stay.kind !== "area") return "";
  const distance = stay.distance_m ? ` · ${distanceText(stay.distance_m / 1000)}` : "";
  return stay.activity
    ? `${modeInfo[stay.activity]?.noun || stay.activity}${distance}${stay.activity_corrected ? " · set by you" : ""}`
    : "Moving around";
}

export const TRIP_MODES = ["walking", "running", "cycling", "driving", "transit"],
  AREA_MODES = ["walking", "running", "cycling"];

export function modeStrip(subject, options, current, corrected, detected, caption) {
  // subject: what the correction is for (source, target, start, end).
  const buttons = options
    .map(
      (mode) =>
        `<button type="button" class="mode-option${mode === current ? " active" : ""}" style="--mode:${modeInfo[mode].color}" data-set-mode="${mode}" aria-pressed="${mode === current}">${modeIcon(mode)}${safe(modeInfo[mode].label)}</button>`,
    )
    .join("");
  const note = corrected
    ? `<span class="mode-note">Set by you${detected ? ` · detected ${safe(modeName(detected).toLowerCase())}` : ""}</span><button type="button" class="button ghost small" data-set-mode="auto">Use detected</button>`
    : '<span class="mode-note">Detected from speed</span>';
  return `<li class="tl-mode-picker" data-mode-subject='${safe(JSON.stringify(subject))}'><div class="mode-head"><span class="mode-caption">${safe(caption)}</span></div><div class="mode-options" role="group" aria-label="${safe(caption)}">${buttons}</div><div class="mode-meta">${note}</div></li>`;
}

export function renderModePicker() {
  document.querySelectorAll("#day-timeline .tl-mode-picker").forEach((item) => item.remove());
  const item = selectedItem();
  if (!item) return;
  const row = document
    .querySelector(`#day-timeline [data-item="${state.selected.type}:${state.selected.key}"]`)
    ?.closest("li");
  if (!row) return;
  let html = "";
  if (item.type === "trip") {
    const trip = item.trip;
    html = modeStrip(
      { source: trip.source, target: "trip", started_at: trip.start, ended_at: trip.end },
      TRIP_MODES,
      trip.mode,
      trip.mode_corrected,
      trip.detected_mode,
      "How did you travel?",
    );
  } else {
    const stay = item.stay;
    if (stay.kind === "area")
      html += modeStrip(
        { source: stay.source, target: "stay", started_at: stay.started_at, ended_at: stay.ended_at },
        AREA_MODES,
        stay.activity,
        stay.activity_corrected,
        stay.detected_activity,
        "How did you get around?",
      );
    for (const move of stay.internal_moves || [])
      html += modeStrip(
        { source: move.source || stay.source, target: "trip", started_at: move.start, ended_at: move.end },
        TRIP_MODES,
        move.mode,
        move.mode_corrected,
        null,
        `Moved ${distanceText(move.distance_km)} within, at ${clock(move.start)}`,
      );
  }
  if (!html) return;
  row.insertAdjacentHTML("afterend", html);
  document.querySelectorAll("#day-timeline .tl-mode-picker [data-set-mode]").forEach(
    (button) =>
      (button.onclick = async () => {
        const subject = JSON.parse(button.closest(".tl-mode-picker").dataset.modeSubject);
        try {
          await post("/api/mode-overrides", { ...subject, mode: button.dataset.setMode });
          toast(
            button.dataset.setMode === "auto"
              ? "Using the detected mode"
              : `Set to ${modeInfo[button.dataset.setMode].noun.toLowerCase()}`,
          );
          await refreshExplore(false);
        } catch (error) {
          toast(error.message);
        }
      }),
  );
}

export function stayWhen(stay, day) {
  const early = new Date(stay.started_at) < new Date(day.start),
    late = new Date(stay.ended_at) > new Date(day.end);
  if (early && stay.ongoing) return "Since yesterday";
  if (early && late) return "All day";
  if (early) return `Until ${clock(stay.ended_at)}`;
  if (late) return `From ${clock(stay.started_at)}`;
  if (stay.ongoing) return `Since ${clock(stay.started_at)}`;
  return `${clock(stay.started_at)} – ${clock(stay.ended_at)}`;
}

export function activityTag(stay) {
  const text = stayActivityText(stay);
  return text
    ? `<span class="activity-tag" style="--mode:${stayColor(stay)}">${stay.activity ? modeIcon(stay.activity) : ""}${safe(text)}</span>`
    : "";
}

export function roundTripNote(trip) {
  // Say where a round trip went, so Home -> Home reads as an errand rather than a repeat.
  const turn = trip.turnaround,
    left = trip.from_name ? `Left ${trip.from_name}` : "Left";
  if (turn?.stop_minutes)
    return `${left} · stopped ${durationText(turn.stop_minutes)} ${turn.name ? "near " + turn.name : distanceText(turn.distance_m / 1000) + " away"}`;
  if (turn) return `${left} · went ${distanceText(turn.distance_m / 1000)} out and came back`;
  return `${left} and came back`;
}

export function movedWithin(stay) {
  // Walking between buildings or moving the car belongs to the visit, not to a trip "to" where you already were.
  const moves = (stay.internal_moves || []).map(
    (move) => `${modeInfo[move.mode]?.label || "Move"} ${distanceText(move.distance_km)}`,
  );
  return moves.length ? `<span class="tl-within">Moved within · ${safe(moves.join(" · "))}</span>` : "";
}

export function renderTimeline(day) {
  const numbers = new Map(day.visited_places.map((place, index) => [place.cluster_id, index + 1]));
  const items = [
    ...day.stays.map((stay) => ({ type: "stay", at: stay.started_at, stay })),
    ...day.trips.map((trip) => ({ type: "trip", at: trip.start, trip })),
    ...(day.coverage_gaps || []).map((gap) => ({ type: "gap", at: gap.start, gap })),
  ].sort((a, b) => new Date(a.at) - new Date(b.at) || (a.type === "stay" ? -1 : 1));
  const counts = [
    `${day.stays.length} stay${day.stays.length === 1 ? "" : "s"}`,
    `${day.trips.length} trip${day.trips.length === 1 ? "" : "s"}`,
  ];
  if ((day.timeline_sources || []).length > 1) counts.push(`${day.timeline_sources.length} sources`);
  $("timeline-count").textContent = day.stays.length || day.trips.length ? counts.join(" · ") : "";
  $("timeline-count").hidden = !(day.stays.length || day.trips.length);
  $("timeline-count").title =
    (day.timeline_sources || []).length > 1
      ? `Combined from ${day.timeline_sources.join(", ")}; where sources overlap, the one with the most samples is shown.`
      : "";
  $("day-timeline").innerHTML =
    items
      .map((item) => {
        if (item.type === "gap")
          return `<li class="tl-gap"><span>No location data</span><small>${safe(clock(item.gap.start))} – ${safe(clock(item.gap.end))} · ${safe(durationText(item.gap.minutes))}</small></li>`;
        if (item.type === "stay") {
          const stay = item.stay;
          return `<li><button type="button" class="tl-stay" data-item="stay:${stay.id}" aria-pressed="false">${pinBadge(numbers.get(stay.cluster_id))}<span class="tl-body"><strong>${safe(stay.display_name || stay.place_name || "Observed stay")}</strong><small>${safe([zoneText(stay.zone, stay.display_name), stayWhen(stay, day), durationText(stay.observed_minutes)].filter(Boolean).join(" · "))}</small>${activityTag(stay)}${movedWithin(stay)}</span></button></li>`;
        }
        const trip = item.trip,
          info = modeInfo[trip.mode] || { label: "Trip", color: "#6f7b76" };
        const notes = [`${clock(trip.start)} – ${clock(trip.end)}`, durationText(trip.duration_minutes)];
        if (!trip.coverage_complete) notes.push("coverage gap");
        if (trip.mode_corrected) notes.push("set by you");
        // A trip that ends where it began left and came back; say so rather than implying travel to where you already were.
        const title = trip.loop
          ? `Round trip · ${info.label} ${distanceText(trip.distance_km)}`
          : `${info.label} · ${distanceText(trip.distance_km)}`;
        const from = trip.loop ? `<span class="tl-within">${safe(roundTripNote(trip))}</span>` : "";
        return `<li><button type="button" class="tl-trip" data-item="trip:${tripKey(trip)}" aria-pressed="false" style="--mode:${info.color}">${modeIcon(trip.mode)}<span class="tl-body"><strong>${safe(title)}</strong><small>${safe(notes.join(" · "))}</small>${from}</span></button></li>`;
      })
      .join("") ||
    '<li class="empty">No stays yet on this day. A stay needs 15 minutes of accurate samples in one place or area.</li>';
  $("day-timeline")
    .querySelectorAll("[data-item]")
    .forEach(
      (button) =>
        (button.onclick = () => {
          const [type, ...rest] = button.dataset.item.split(":");
          const key = rest.join(":");
          selectItem(type, type === "stay" ? +key : key);
        }),
    );
}
