// Places: visit search, saved zones, places visited, and patterns.
import { api, post } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { ago, clock, dateLabel, durationText, formatTime, localDateKey, plural, radiusText } from "../core/format.js";
import { state } from "../core/state.js";
import { onEach, rowActions } from "../ui/actions.js";
import { modeName, placeIcon, zoneText } from "../ui/icons.js";
import { focusPlaceOnMap, refreshExplore } from "./history.js";
import { afterPlaceNamesChanged, editVisitedPlace, openPlaceDialog } from "./place-dialogs.js";
import { fillPlaceSelectors } from "./reminders.js";
import { showView } from "./shell.js";

function renderSavedPlaces() {
  // Saved-place totals cover everything inside the zone, Home included.
  const usage = new Map(Object.entries(state.savedStats || {}).map(([id, stats]) => [+id, stats]));
  const saved = [...state.places].sort(
    (a, b) =>
      (usage.get(b.id)?.observed_minutes || 0) - (usage.get(a.id)?.observed_minutes || 0) ||
      a.name.localeCompare(b.name),
  );
  $("saved-place-count").textContent = saved.length ? saved.length.toLocaleString() : "";
  const box = $("places-list");
  box.innerHTML =
    saved
      .map((place) => {
        const seen = usage.get(place.id);
        const details = [
          seen
            ? `${plural(seen.visits, "visit")} · ${durationText(seen.observed_minutes)} inside`
            : "No visits yet",
          `${radiusText(place.radius_m)} zone`,
          seen?.last_seen ? `last ${ago(seen.last_seen)}` : "",
        ].filter(Boolean);
        return `<div class="list-item"><div class="place-main">${placeIcon(place)}<div><strong>${safe(place.name)}</strong><small>${safe(details.join(" · "))}</small>${place.address ? `<span>${safe(place.address)}</span>` : ""}</div></div>${rowActions(
          [
            { icon: "map", label: "Show on map", data: `data-focus="${place.id}"` },
            { icon: "edit", label: "Edit place", data: `data-edit="${place.id}"` },
            {
              icon: "delete",
              label: "Delete place",
              data: `data-delete-place="${place.id}"`,
              tone: "danger",
            },
          ],
        )}</div>`;
      })
      .join("") ||
    `<div class="empty-block"><strong>No saved places yet</strong><span>Save places like Home, work, or a campus. Visits inside keep their own names; the zone adds up your time there and can fire arrival reminders.</span><button type="button" class="button secondary small" data-add-first-place>Add a place</button></div>`;
  const find = (id) => state.places.find((place) => place.id === +id);
  onEach(box, "focus", (id) => focusPlaceOnMap(find(id)));
  onEach(box, "edit", (id) => openPlaceDialog(find(id)));
  onEach(box, "add-first-place", () => openPlaceDialog());
  onEach(box, "delete-place", async (id) => {
    const place = find(id);
    if (
      !place ||
      !confirm(
        `Delete “${place.name}”? Visits inside it keep their names. Reminders that use it keep working.`,
      )
    )
      return;
    try {
      await post("/api/places/delete", { id: place.id });
      await afterPlaceNamesChanged("Place deleted");
    } catch (error) {
      toast(error.message);
    }
  });
}

function renderVisitedPlaces() {
  const places = state.visitedPlaces;
  $("visited-place-count").textContent = places.length ? places.length.toLocaleString() : "";
  const box = $("all-visited-places");
  // A thin bar shows each place's time against the place you spend most time at.
  const most = Math.max(1, ...places.map((place) => place.observed_minutes || 0));
  box.innerHTML =
    places
      .map((place, index) => {
        const details = [
          zoneText(place.zone, place.display_name),
          plural(place.visits, "visit"),
          `last ${ago(place.last_seen)}`,
        ].filter(Boolean);
        const share = Math.max(2, Math.round(((place.observed_minutes || 0) / most) * 100));
        return `<div class="list-item"><div class="place-main">${placeIcon(place)}<div class="place-text"><div class="place-line"><strong>${safe(place.display_name)}</strong><b class="place-time">${safe(durationText(place.observed_minutes))}</b></div><small title="${safe(formatTime(place.last_seen))}">${safe(details.join(" · "))}</small><div class="time-bar" aria-hidden="true"><i style="width:${share}%"></i></div></div></div>${rowActions(
          [
            { icon: "map", label: "Show on map", data: `data-map-visited="${index}"` },
            { icon: "edit", label: "Rename this place", data: `data-rename-visited="${index}"` },
          ],
        )}</div>`;
      })
      .join("") ||
    `<div class="empty-block"><strong>No places yet</strong><span>A place appears here after a stay of ${state.settings?.stay_min_minutes ?? 15} minutes or more.</span></div>`;
  onEach(box, "map-visited", (index) => focusPlaceOnMap(places[+index]));
  // From here there is no particular visit, so a correction applies to every visit to the spot.
  onEach(box, "rename-visited", (index) => editVisitedPlace(places[+index], []));
}

export function renderPlaces() {
  fillPlaceSelectors();
  renderSavedPlaces();
  renderVisitedPlaces();
}

function showResults(html) {
  const box = $("visit-results");
  box.hidden = !html;
  box.innerHTML = html;
}

function renderVisitResults(result) {
  if (!result.visits.length) {
    showResults(
      `<div class="results-head"><div><h2>No visits matched “${safe(result.query)}”</h2><p>Try a brand, a kind of place such as supermarket or park, or a street name.</p></div></div>`,
    );
    return;
  }
  // The place is named on each row only when the search matched more than one.
  const several = result.places.length > 1;
  const title = several
    ? `${plural(result.total_visits, "visit")} to ${plural(result.places.length, "place")}`
    : `${plural(result.total_visits, "visit")} to ${result.places[0].name}`;
  const summary = [
    several ? result.places.map((place) => place.name).join(", ") : result.places[0].address,
    result.total_visits > result.visits.length ? `latest ${result.visits.length} shown` : "",
  ].filter(Boolean);
  showResults(`<div class="results-head"><div><h2>${safe(title)}</h2>${summary.length ? `<p>${safe(summary.join(" · "))}</p>` : ""}</div></div><ul class="visit-list">${result.visits
    .map((visit, index) => {
      const details = [
        several ? visit.place : zoneText(visit.zone, visit.place),
        `${clock(visit.started_at)} – ${clock(visit.ended_at)}`,
        durationText(visit.observed_minutes),
        visit.activity ? modeName(visit.activity) : "",
      ].filter(Boolean);
      return `<li><button type="button" class="visit-row" data-visit-day="${index}" aria-label="Open ${safe(dateLabel(visit.date))} in History"><span class="visit-date"><strong>${safe(dateLabel(visit.date))}</strong><span>${safe(visit.weekday)}${visit.ongoing ? " · now" : ""}</span></span><span class="visit-info">${safe(details.join(" · "))}</span><span class="row-chevron" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m9.5 6 6 6-6 6"/></svg></span></button></li>`;
    })
    .join("")}</ul>`);
  onEach($("visit-results"), "visit-day", (index) => openVisitDay(result.visits[+index]));
}

function openVisitDay(visit) {
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
  document.querySelectorAll("[data-places-tab]").forEach((button) => {
    button.onclick = () => {
      const tab = button.dataset.placesTab;
      document.querySelectorAll("[data-places-tab]").forEach((item) => {
        const active = item === button;
        item.classList.toggle("active", active);
        item.setAttribute("aria-selected", String(active));
      });
      document
        .querySelectorAll("[data-places-panel]")
        .forEach((panel) => panel.classList.toggle("active", panel.dataset.placesPanel === tab));
    };
  });
  $("add-place").onclick = () => openPlaceDialog();
  $("visit-search-form").onsubmit = async (event) => {
    event.preventDefault();
    const query = $("visit-search").value.trim();
    if (!query) {
      showResults("");
      return;
    }
    showResults('<p class="empty">Searching…</p>');
    try {
      renderVisitResults(await api("/api/visits?" + new URLSearchParams({ q: query, limit: 50 })));
    } catch (error) {
      showResults("");
      toast(error.message);
    }
  };
  // Clearing the search box clears its results.
  const clear = () => {
    $("visit-search").value = "";
    $("visit-search-clear").hidden = true;
    showResults("");
    $("visit-search").focus();
  };
  $("visit-search-clear").onclick = clear;
  $("visit-search").addEventListener("input", () => {
    const empty = !$("visit-search").value.trim();
    $("visit-search-clear").hidden = empty;
    if (empty) showResults("");
  });
  $("visit-search").addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("visit-search").value) clear();
  });
}
