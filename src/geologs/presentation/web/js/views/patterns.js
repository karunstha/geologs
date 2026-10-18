// Places → Routines: how you move between places, and where you'll likely go next.
import { api } from "../core/api.js";
import { $, safe } from "../core/dom.js";
import { plural } from "../core/format.js";
import { state } from "../core/state.js";
import { onEach, rowActions } from "../ui/actions.js";
import { focusRoute } from "../map/places-map.js";

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
  const moves = data.transitions.slice(0, 8);
  const moveBox = $("transitions");
  moveBox.innerHTML =
    moves
      .map(
        (move, index) =>
          `<div class="list-item"><div><strong>${safe(move.from_name)} <span class="arrow" aria-label="to">→</span> ${safe(move.to_name)}</strong><small>${plural(move.count, "time")}</small></div>${rowActions([{ icon: "map", label: "Show on the map", data: `data-transition="${index}"` }])}</div>`,
      )
      .join("") || '<p class="empty">Trips between your places show here.</p>';
  onEach(moveBox, "transition", (index) => focusRoute(moves[+index]));
}

export async function refreshInsights() {
  state.insights = await api("/api/insights?limit=50");
  renderPatterns();
}
