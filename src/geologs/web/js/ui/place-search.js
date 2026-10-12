// Searching for a place by name, near where you are, for the reminder and saved-place forms.
import { distanceText } from "../core/format.js";
import { api } from "../core/api.js";
import { safe } from "../core/dom.js";
import { state } from "../core/state.js";
import { placeIcon } from "./icons.js";

export async function searchPlaces(query) {
  const params = new URLSearchParams({ q: query });
  // The server falls back to your latest fix; sending the current one keeps the page and server in step.
  if (state.current) {
    params.set("lat", state.current.lat);
    params.set("lon", state.current.lon);
  }
  return api("/api/geocode?" + params);
}

function distanceLabel(metres) {
  if (metres == null) return "";
  return `${distanceText(metres / 1000)} away`;
}

// Rows for a result list; each "Use" button carries `attribute` with the result's index.
export function placeResultsMarkup(results, attribute) {
  return (
    results
      .map((place, index) => {
        const details = [
          distanceLabel(place.distance_m),
          place.address && place.address !== place.name ? place.address : "",
        ]
          .filter(Boolean)
          .join(" · ");
        return `<div class="result-item"><div class="place-main">${placeIcon(place)}<div><strong>${safe(place.name)}</strong><span>${safe(details)}</span></div></div><button type="button" ${attribute}="${index}">Use</button></div>`;
      })
      .join("") ||
    '<p class="empty">No matches nearby or elsewhere. Try another name, or enter coordinates.</p>'
  );
}

// A result with its coordinates: Mapbox suggestions arrive without them, so fetch them for the pick.
export async function resolvePlace(place) {
  if (place.lat != null && place.lon != null) return place;
  const params = new URLSearchParams({ id: place.provider_place_id });
  if (place.session) params.set("session", place.session);
  const details = await api("/api/geocode/place?" + params);
  return { ...place, lat: details.lat, lon: details.lon, address: place.address || details.address };
}
