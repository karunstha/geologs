// Loading data for every view, and polling for new arrival reminders.
import { api } from "../core/api.js";
import { $, toast } from "../core/dom.js";
import { localDateKey } from "../core/format.js";
import { state } from "../core/state.js";
import { drawMap } from "../map/map.js";
import { refreshCoverage, refreshExplore, refreshInsights, renderCurrent } from "../views/history.js";
import { renderPlaces } from "../views/places.js";
import { renderReminders } from "../views/reminders.js";
import { renderSettings } from "../views/settings.js";

export async function refreshMeta() {
  let overview;
  [state.places, overview, state.reminders, state.reminderHistory, state.events, state.labels] =
    await Promise.all([
      api("/api/places"),
      api("/api/place-overview?limit=100"),
      api("/api/reminders"),
      api("/api/reminder-history?limit=200"),
      api("/api/events"),
      api("/api/place-labels"),
    ]);
  state.visitedPlaces = overview.visited;
  state.savedStats = overview.saved;
  renderPlaces();
  renderReminders();
  drawMap();
}

export async function refreshAll() {
  try {
    const [status, current, settings, dailySummaries] = await Promise.all([
      api("/api/status"),
      api("/api/current"),
      api("/api/settings"),
      api("/api/daily-summaries?limit=10"),
    ]);
    state.status = status;
    state.current = current;
    state.settings = settings;
    state.dailySummaries = dailySummaries;
    if (state.followToday) $("day-date").value = localDateKey();
    renderCurrent();
    renderSettings();
    $("geocode-hint").textContent = status.geocoding
      ? current
        ? "Results favor your last recorded location."
        : "Searches are worldwide until a location is recorded."
      : "Place search needs a geocoding provider.";
    await refreshMeta();
    await Promise.all([refreshExplore(), refreshInsights(), refreshCoverage()]);
  } catch (e) {
    toast(e.message);
  }
}

export async function pollEvents() {
  try {
    const events = await api("/api/events?since_id=" + state.lastEventId);
    const newest = events.reduce((max, e) => Math.max(max, e.id), state.lastEventId);
    if (state.lastEventId && events.length) {
      for (const event of events.reverse()) {
        toast(`${event.text} · ${event.place_name}`);
        if (Notification.permission === "granted")
          new Notification("Location reminder", { body: `${event.text} · ${event.place_name}` });
      }
    }
    state.lastEventId = newest;
    if (events.length) await refreshMeta();
  } catch (e) {
    /* Connection errors are surfaced on explicit actions. */
  }
}
