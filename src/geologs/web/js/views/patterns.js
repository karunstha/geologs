// Places → Patterns: where you spend time, how you move between places, and where you'll likely go next.
import { api } from "../core/api.js";
import { $, safe } from "../core/dom.js";
import { durationText, plural } from "../core/format.js";
import { state } from "../core/state.js";
import { onEach, rowActions } from "../ui/actions.js";
import { placeIcon, zoneText } from "../ui/icons.js";
import { focusPlaceOnMap } from "./history.js";

// The server's reasons, said plainly.
const NO_PREDICTION = {
  "no location data": "Once locations arrive, GeoLogs learns where you tend to go next.",
  "current point is not at an observed stay":
    "A prediction appears when you're at a place you've stopped at before.",
  "fewer than three observed departures":
    "A prediction needs at least three departures from where you are now.",
  "transition evidence is older than 180 days":
    "Your trips from here are more than six months old, so no prediction is made.",
};

function renderPrediction(prediction) {
  const box = $("prediction");
  if (prediction.prediction) {
    box.className = "callout prediction";
    box.innerHTML = `<strong>After ${safe(prediction.from)}, you usually go to ${safe(prediction.prediction)}</strong><span>${Math.round(prediction.frequency * 100)}% of ${plural(prediction.observations, "departure")} from here, recent trips counting more.${prediction.current_fix_stale ? " Your latest location is a while old." : ""}</span>`;
  } else {
    box.className = "callout";
    box.innerHTML = `<strong>No prediction yet</strong><span>${safe(NO_PREDICTION[prediction.reason] || prediction.reason)}</span>`;
  }
}

export function renderPatterns() {
  const data = state.insights;
  if (!data) return;
  renderPrediction(data.prediction);
  const places = data.frequent_places.slice(0, 8);
  const placeBox = $("pattern-places");
  placeBox.innerHTML =
    places
      .map((place, index) => {
        const zone = zoneText(place.zone, place.display_name);
        return `<div class="list-item"><div class="place-main">${placeIcon(place)}<div><strong>${safe(place.display_name)}</strong><small>${safe([zone, plural(place.visits, "visit"), durationText(place.observed_minutes)].filter(Boolean).join(" · "))}</small></div></div>${rowActions([{ icon: "map", label: "Show on map", data: `data-pattern-place="${index}"` }])}</div>`;
      })
      .join("") || '<p class="empty">Common places appear after a few confirmed stays.</p>';
  onEach(placeBox, "pattern-place", (index) => focusPlaceOnMap(places[+index]));
  const moves = data.transitions.slice(0, 8);
  const moveBox = $("transitions");
  moveBox.innerHTML =
    moves
      .map(
        (move, index) =>
          `<div class="list-item"><div><strong>${safe(move.from_name)} <span class="arrow" aria-label="to">→</span> ${safe(move.to_name)}</strong><small>${plural(move.count, "time")}</small></div>${rowActions([{ icon: "map", label: `Show ${move.to_name} on map`, data: `data-transition="${index}"` }])}</div>`,
      )
      .join("") || '<p class="empty">Routines appear after trips between two or more places.</p>';
  onEach(moveBox, "transition", (index) => {
    const move = moves[+index];
    focusPlaceOnMap({ lat: move.to_lat, lon: move.to_lon, display_name: move.to_name });
  });
}

export async function refreshInsights() {
  state.insights = await api("/api/insights?limit=50");
  renderPatterns();
}
