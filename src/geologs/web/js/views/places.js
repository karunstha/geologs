// Places: visit search and the saved and visited place lists.
import { api, post } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { ago, clock, dateLabel, durationText, localDateKey } from "../core/format.js";
import { state } from "../core/state.js";
import { modeName, placeIcon, zoneText } from "../ui/icons.js";
import { focusPlaceOnMap, refreshExplore } from "./history.js";
import { afterPlaceNamesChanged, editVisitedPlace, openPlaceDialog } from "./place-dialogs.js";
import { fillPlaceSelectors } from "./reminders.js";
import { showView } from "./shell.js";

export function renderPlaces() {
  fillPlaceSelectors();
  // Saved-place totals include Home, which the visited list leaves out.
  const usage = new Map(Object.entries(state.savedStats || {}).map(([id, stats]) => [+id, stats]));
  const saved = [...state.places].sort(
    (a, b) =>
      (usage.get(b.id)?.observed_minutes || 0) - (usage.get(a.id)?.observed_minutes || 0) ||
      a.name.localeCompare(b.name),
  );
  $("saved-place-count").textContent = saved.length.toLocaleString();
  $("saved-place-count").hidden = !saved.length;
  $("places-list").innerHTML =
    saved
      .map((p) => {
        const seen = usage.get(p.id);
        const activity = seen
          ? `${seen.visits} visit${seen.visits === 1 ? "" : "s"} · ${durationText(seen.observed_minutes)} inside`
          : "No confirmed visits yet";
        return `<div class="list-item"><div class="place-main">${placeIcon(p)}<div><strong>${safe(p.name)}</strong><small>${safe(activity)} · ${Math.round(p.radius_m)} m zone${p.address ? " · " + safe(p.address) : ""}</small></div></div><div class="actions"><button data-focus="${p.id}">Map</button><button data-edit="${p.id}">Edit</button><button data-delete-place="${p.id}" class="danger-button">Delete</button></div></div>`;
      })
      .join("") || '<div class="empty">No saved places yet.</div>';
  $("places-list")
    .querySelectorAll("[data-focus]")
    .forEach(
      (button) =>
        (button.onclick = () =>
          focusPlaceOnMap(state.places.find((place) => place.id === +button.dataset.focus))),
    );
  $("places-list")
    .querySelectorAll("[data-edit]")
    .forEach(
      (button) =>
        (button.onclick = () =>
          openPlaceDialog(state.places.find((place) => place.id === +button.dataset.edit))),
    );
  $("places-list")
    .querySelectorAll("[data-delete-place]")
    .forEach(
      (button) =>
        (button.onclick = async () => {
          const place = state.places.find((item) => item.id === +button.dataset.deletePlace);
          if (
            !place ||
            !confirm(
              `Delete “${place.name}”? Visits there will be named automatically again. Reminders that use it keep working.`,
            )
          )
            return;
          try {
            await post("/api/places/delete", { id: place.id });
            await afterPlaceNamesChanged("Place deleted");
          } catch (error) {
            toast(error.message);
          }
        }),
    );
  $("all-visited-places").innerHTML =
    state.visitedPlaces
      .map(
        (place, index) =>
          `<div class="list-item"><div class="place-main">${placeIcon(place)}<div><strong>${safe(place.display_name)}</strong><small>${zoneText(place.zone, place.display_name) ? safe(zoneText(place.zone, place.display_name)) + " · " : ""}${place.visits} visit${place.visits === 1 ? "" : "s"} · ${durationText(place.observed_minutes)} · last ${safe(ago(place.last_seen))}</small></div></div><div class="actions"><button data-map-visited="${index}">Map</button><button data-edit-visited="${index}">Edit place</button></div></div>`,
      )
      .join("") || '<div class="empty">No confirmed visited places yet.</div>';
  $("visited-place-count").textContent = state.visitedPlaces.length.toLocaleString();
  $("visited-place-count").hidden = !state.visitedPlaces.length;
  $("all-visited-places")
    .querySelectorAll("[data-map-visited]")
    .forEach(
      (button) => (button.onclick = () => focusPlaceOnMap(state.visitedPlaces[+button.dataset.mapVisited])),
    );
  $("all-visited-places")
    .querySelectorAll("[data-edit-visited]")
    .forEach(
      (button) => (button.onclick = () => editVisitedPlace(state.visitedPlaces[+button.dataset.editVisited])),
    );
}

export function renderVisitResults(result) {
  const box = $("visit-results");
  if (!result.visits.length) {
    box.innerHTML = `<p class="empty">No visits matched “${safe(result.query)}”. Try a brand, a place type such as supermarket or park, or a street name.</p>`;
    return;
  }
  const names = result.places.map((place) => safe(place.name)).join(", ");
  const shown = result.total_visits > result.visits.length ? ` · latest ${result.visits.length} shown` : "";
  box.innerHTML = `<p class="result-summary"><strong>${result.total_visits} visit${result.total_visits === 1 ? "" : "s"}</strong> to ${names}${shown}</p><ul class="visit-list">${result.visits
    .map((visit, index) => {
      const details = [
        `${clock(visit.started_at)} – ${clock(visit.ended_at)}`,
        durationText(visit.observed_minutes),
      ];
      if (visit.activity) details.push(modeName(visit.activity));
      if (visit.ongoing) details.push("now");
      return `<li><div class="visit-date"><strong>${safe(dateLabel(visit.date))}</strong><span>${safe(visit.weekday)}</span></div><div class="visit-info"><strong>${safe(visit.place)}</strong><small>${safe(details.join(" · "))}</small></div><button type="button" class="button secondary" data-visit-day="${index}">Show day</button></li>`;
    })
    .join("")}</ul>`;
  box
    .querySelectorAll("[data-visit-day]")
    .forEach((button) => (button.onclick = () => openVisitDay(result.visits[+button.dataset.visitDay])));
}

export function openVisitDay(visit) {
  state.pendingStayId = visit.stay_id;
  state.focusPlace = null;
  $("source").value = "";
  $("day-date").value = visit.date;
  state.followToday = visit.date === localDateKey();
  showView("explore");
  window.scrollTo({ top: 0, behavior: "smooth" });
  refreshExplore().catch((error) => toast(error.message));
}

export function initPlaces() {
  document.querySelectorAll("[data-places-tab]").forEach(
    (button) =>
      (button.onclick = () => {
        const tab = button.dataset.placesTab;
        document.querySelectorAll("[data-places-tab]").forEach((item) => {
          const active = item === button;
          item.classList.toggle("active", active);
          item.setAttribute("aria-selected", String(active));
        });
        document
          .querySelectorAll("[data-places-panel]")
          .forEach((panel) => panel.classList.toggle("active", panel.dataset.placesPanel === tab));
      }),
  );
  $("add-place").onclick = () => openPlaceDialog();
  $("visit-search-form").onsubmit = async (event) => {
    event.preventDefault();
    const query = $("visit-search").value.trim();
    if (!query) return;
    const box = $("visit-results");
    box.innerHTML = '<p class="empty">Searching…</p>';
    try {
      renderVisitResults(await api("/api/visits?" + new URLSearchParams({ q: query, limit: 50 })));
    } catch (error) {
      box.innerHTML = "";
      toast(error.message);
    }
  };
}
