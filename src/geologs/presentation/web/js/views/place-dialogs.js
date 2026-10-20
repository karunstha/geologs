// Dialogs for adding a saved place and correcting a visited place's name.
import { refreshMeta } from "../app/refresh.js";
import { api, post } from "../core/api.js";
import { $, closeOnBackdrop, safe, toast } from "../core/dom.js";
import { clock, coordinateText, distanceText, fromLengthUnit, lengthText, metresBetween, radiusText, toLengthUnit } from "../core/format.js";
import { state } from "../core/state.js";
import { map, searchLayer } from "../map/map.js";
import { onEach } from "../ui/actions.js";
import { placeIcon } from "../ui/icons.js";
import { placeResultsMarkup, resolvePlace, searchPlaces } from "../ui/place-search.js";
import { refreshExplore } from "./history.js";
import { refreshInsights } from "./patterns.js";
import { showSettingsPage } from "./settings.js";
import { showView } from "./shell.js";

export function openPlaceDialog(place = {}) {
  $("place-form").reset();
  $("search-results").innerHTML = "";
  $("search").value = "";
  $("place-id").value = place.id || "";
  $("place-category").value = place.category || "";
  $("place-provider-id").value = place.provider_place_id || "";
  $("place-name").value = place.name || "";
  $("place-lat").value = place.lat ?? "";
  $("place-lon").value = place.lon ?? "";
  $("place-radius").value = Math.round(toLengthUnit(place.radius_m || 150));
  $("place-address").value = place.address || "";
  $("place-dialog-title").textContent = place.id ? "Edit place" : "Add place";
  $("place-dialog").showModal();
}

export function labelTarget(place, visits) {
  // Cover the visits being corrected (or the place's observed extent) with a margin.
  const center = visits.length === 1 ? visits[0] : place;
  const reach = visits.length
    ? Math.max(
        ...visits.map(
          (stay) => metresBetween(center.lat, center.lon, stay.lat, stay.lon) + (stay.radius_m || 0),
        ),
      )
    : place.extent_m || place.radius_m || 0;
  return {
    lat: +Number(center.lat).toFixed(6),
    lon: +Number(center.lon).toFixed(6),
    radius_m: Math.round(Math.min(1000, Math.max(50, reach ? reach + 25 : 50))),
  };
}

export function visitTimes(visits) {
  return visits.map((stay) => `${clock(stay.started_at)} – ${clock(stay.ended_at)}`).join(", ");
}

// The visits to a place on the day shown in History (place ids are only meaningful within that day).
export function dayVisits(place) {
  return state.day && place?.cluster_id != null
    ? state.day.stays.filter((stay) => stay.cluster_id === place.cluster_id)
    : [];
}

export function editVisitedPlace(place, visits = []) {
  // visits: the stays this fix is about. A pin or day row passes that day's visits; the
  // Places page has no particular visit, so only the every-visit choice applies there.
  if (!place) return;
  const target = labelTarget(place, visits);
  const labelIds = [
    ...new Set([place.label_id, ...visits.map((stay) => stay.label_id)].filter((id) => id != null)),
  ];
  const current = (state.labels || []).find((label) => label.id === labelIds[0]);
  state.labelEdit = { ...target, visits, labelIds, editing: labelIds.length === 1 ? labelIds[0] : null };
  $("label-form").reset();
  $("label-name").value = place.name || place.display_name || "";
  $("label-category").value = place.category || "";
  $("label-provider-id").value = place.provider_place_id || "";
  $("label-address").value = place.address || "";
  $("scope-visit-option").hidden = !visits.length;
  if (visits.length) {
    const [year, month, day] = (state.day?.date || "").split("-").map(Number);
    const dayName = state.day
      ? new Date(year, month - 1, day).toLocaleDateString(undefined, {
          weekday: "short",
          month: "short",
          day: "numeric",
        })
      : "";
    $("scope-visit-title").textContent =
      visits.length === 1 ? "Only this visit" : `Only the visits on ${dayName}`;
    $("scope-visit-detail").textContent =
      visitTimes(visits) + (visits.length === 1 ? "" : ` (${visits.length} visits)`);
  }
  $("scope-spot-detail").textContent = `Past and future visits within ${radiusText(target.radius_m)}`;
  const scope = !visits.length || (current && !current.started_at) ? "spot" : "visit";
  document.querySelector(`input[name="label-scope"][value="${scope}"]`).checked = true;
  // With no particular visit there is only one choice, so say it instead of offering it.
  $("label-scope").hidden = !visits.length;
  $("nearby-title").textContent = visits.length ? "Places around this visit" : "Places around here";
  $("label-where").textContent = visits.length
    ? `Pinned at ${coordinateText(target.lat, target.lon)}.`
    : `Applies to every visit within ${target.radius_m} m of ${coordinateText(target.lat, target.lon)}, past and future.`;
  $("reset-label").hidden = !labelIds.length;
  loadNearbyPlaces(target);
  $("label-dialog").showModal();
}

export async function loadNearbyPlaces(target) {
  const box = $("nearby-places"),
    request = ++loadNearbyPlaces.request;
  box.innerHTML = '<p class="empty">Looking for places around this visit…</p>';
  $("nearby-note").textContent = "";
  try {
    const radius = Math.max(500, target.radius_m + 50);
    const places = await api(
      "/api/nearby-places?" +
        new URLSearchParams({ lat: target.lat, lon: target.lon, radius_m: radius, limit: 15 }),
    );
    if (request !== loadNearbyPlaces.request) return;
    $("nearby-note").textContent = places.length
      ? `${places.length} within ${radius >= 1000 ? distanceText(radius / 1000) : lengthText(radius)}`
      : "";
    box.innerHTML =
      places
        .map(
          (place, index) =>
            `<div class="result-item"><div class="place-main">${placeIcon(place)}<div><strong>${safe(place.name)}</strong><span>${safe([place.distance_m != null ? lengthText(place.distance_m) + " away" : "", place.address && place.address !== place.name ? place.address : ""].filter(Boolean).join(" · "))}</span></div></div><button type="button" data-nearby="${index}">Use</button></div>`,
        )
        .join("") || '<p class="empty">No named places nearby. Type the name below.</p>';
    box.querySelectorAll("[data-nearby]").forEach(
      (button) =>
        (button.onclick = () => {
          const place = places[+button.dataset.nearby];
          $("label-name").value = place.name.slice(0, 120);
          $("label-category").value = place.category || "";
          $("label-provider-id").value = place.provider_place_id || "";
          $("label-address").value = place.address || "";
          box
            .querySelectorAll(".result-item")
            .forEach((item) => item.classList.toggle("chosen", item === button.closest(".result-item")));
          $("label-name").focus();
        }),
    );
  } catch (error) {
    if (request !== loadNearbyPlaces.request) return;
    // Without a places provider there is nothing to suggest; say so and point to the setting.
    const noProvider = /API_KEY|ACCESS_TOKEN|PHOTON_URL|provider/i.test(error.message);
    box.innerHTML = noProvider
      ? '<div class="callout"><strong>No suggestions without a places provider</strong><span>Type the name below, or choose a provider in Settings → Place names to see places nearby.</span><button type="button" class="button secondary small" id="nearby-settings">Open Place names settings</button></div>'
      : `<p class="empty">${safe(error.message)}</p>`;
    if (noProvider)
      $("nearby-settings").onclick = () => {
        $("label-dialog").close();
        showView("settings");
        showSettingsPage("settings-places");
      };
  }
}

export async function afterPlaceNamesChanged(message) {
  await refreshMeta();
  await Promise.all([refreshExplore(false), refreshInsights()]);
  toast(message);
}

export function initPlaceDialogs() {
  loadNearbyPlaces.request = 0;
  $("search-form").onsubmit = async (event) => {
    event.preventDefault();
    const query = $("search").value.trim();
    if (!query) return;
    const box = $("search-results");
    box.innerHTML = '<p class="empty">Searching near you…</p>';
    try {
      state.current = await api("/api/current");
      const results = await searchPlaces(query);
      box.innerHTML = placeResultsMarkup(results, "data-search");
      onEach(box, "search", async (index) => {
        try {
          const place = await resolvePlace(results[+index]);
          $("place-id").value = "";
          $("place-category").value = place.category || "";
          $("place-provider-id").value = place.provider_place_id || "";
          $("place-name").value = place.name.slice(0, 120);
          $("place-lat").value = place.lat;
          $("place-lon").value = place.lon;
          $("place-address").value = place.address || "";
          box
            .querySelectorAll(".result-item")
            .forEach((item, position) => item.classList.toggle("chosen", position === +index));
          if (map) {
            searchLayer.clearLayers();
            L.marker([place.lat, place.lon]).addTo(searchLayer);
            map.setView([place.lat, place.lon], 15);
          }
        } catch (error) {
          toast(error.message);
        }
      });
    } catch (error) {
      box.innerHTML = `<p class="empty">${safe(error.message)}</p>`;
    }
  };
  $("place-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await post("/api/places", {
        ...($("place-id").value ? { id: +$("place-id").value } : {}),
        name: $("place-name").value,
        lat: +$("place-lat").value,
        lon: +$("place-lon").value,
        radius_m: Math.round(fromLengthUnit(+$("place-radius").value)),
        address: $("place-address").value,
        category: $("place-category").value,
        provider_place_id: $("place-provider-id").value,
      });
      $("place-dialog").close();
      await refreshMeta();
      await Promise.all([refreshExplore(), refreshInsights()]);
      toast("Place saved");
    } catch (err) {
      toast(err.message);
    }
  };
  $("reverse").onclick = async () => {
    if (!$("place-lat").value || !$("place-lon").value) {
      toast("Enter coordinates first");
      return;
    }
    try {
      const q = new URLSearchParams({ lat: $("place-lat").value, lon: $("place-lon").value });
      const result = await api("/api/reverse?" + q);
      $("place-address").value = result.address || "";
      $("place-category").value = result.category || "";
      $("place-provider-id").value = result.provider_place_id || "";
      if (!$("place-name").value) $("place-name").value = (result.name || "").slice(0, 120);
      toast(result.approximate ? "Nearby cached place found — verify before saving" : "Place found");
    } catch (err) {
      toast(err.message);
    }
  };
  $("label-form").onsubmit = async (event) => {
    event.preventDefault();
    const target = state.labelEdit;
    if (!target) return;
    const onlyVisits =
      document.querySelector('input[name="label-scope"]:checked').value === "visit" && target.visits.length;
    // A visit-only fix covers the visits' time window, and their device when they share one.
    const sources = [...new Set(target.visits.map((stay) => stay.source))];
    const window = onlyVisits
      ? {
          started_at: target.visits.map((stay) => stay.started_at).sort()[0],
          ended_at: target.visits
            .map((stay) => stay.ended_at)
            .sort()
            .at(-1),
          source: sources.length === 1 ? sources[0] : "",
        }
      : {};
    try {
      await post("/api/place-labels", {
        ...(target.editing ? { id: target.editing } : {}),
        ...window,
        name: $("label-name").value,
        lat: target.lat,
        lon: target.lon,
        radius_m: target.radius_m,
        address: $("label-address").value,
        category: $("label-category").value,
        provider_place_id: $("label-provider-id").value,
      });
      $("label-dialog").close();
      await afterPlaceNamesChanged("Name saved for this place");
    } catch (error) {
      toast(error.message);
    }
  };
  $("reset-label").onclick = async () => {
    const target = state.labelEdit;
    if (!target?.labelIds.length) return;
    try {
      for (const id of target.labelIds) await post("/api/place-labels/delete", { id });
      $("label-dialog").close();
      await afterPlaceNamesChanged("Automatic name restored");
    } catch (error) {
      toast(error.message);
    }
  };
  $("close-label-dialog").onclick = $("cancel-label").onclick = () => $("label-dialog").close();
  closeOnBackdrop($("label-dialog"));
  $("close-place-dialog").onclick = $("cancel-place").onclick = () => $("place-dialog").close();
  closeOnBackdrop($("place-dialog"));
}
