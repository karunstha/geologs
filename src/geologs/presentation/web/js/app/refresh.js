// Loading data for every view, and polling for new arrival reminders.
import { api } from "../core/api.js";
import { $, toast } from "../core/dom.js";
import { localDateKey } from "../core/format.js";
import { state } from "../core/state.js";
import { drawMap } from "../map/map.js";
import { refreshExplore, renderCurrent } from "../views/history.js";
import { refreshInsights } from "../views/patterns.js";
import { renderPlaces } from "../views/places.js";
import { renderReminders } from "../views/reminders.js";
import { renderSettings, showSettingsPage } from "../views/settings.js";
import { setSunPosition } from "../ui/theme.js";

export async function refreshMeta() {
  let overview;
  [state.places, overview, state.reminders, state.reminderHistory, state.events, state.labels] =
    await Promise.all([
      api("/api/places"),
      api("/api/place-overview?limit=5000"),
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

// Who is signed in, where the page names them.
function showUser() {
  const user = state.user;
  const label = user ? `${user.name || user.email || "Owner"}${user.role === "admin" ? " (admin)" : ""}` : "Account";
  $("auth-button").title = user ? `Account: ${label}` : "Sign in";
  $("auth-button").setAttribute("aria-label", $("auth-button").title);
  // Admin is a group of Settings pages, there only for admins.
  const admin = !!(user && user.role === "admin");
  document.querySelector('.settings-group[data-group="admin"]').hidden = !admin;
  document.querySelector('[data-settings-group="admin"]').hidden = !admin;
  // An Admin page the last person left open closes for someone who is not an admin.
  const page = document.querySelector(".settings-section:not([hidden])");
  if (page && !admin) showSettingsPage(page.id);
  $("access-browser").textContent = user
    ? `Signed in as ${user.email || user.name || "the owner"}.`
    : "Not signed in.";
}

export async function refreshAll() {
  try {
    const [me, status, current, settings] = await Promise.all([
      api("/api/me"),
      api("/api/status"),
      api("/api/current"),
      api("/api/settings"),
    ]);
    state.user = me.user;
    showUser();
    state.status = status;
    state.current = current;
    setSunPosition(current);
    state.settings = settings;
    if (state.followToday) $("day-date").value = localDateKey();
    renderCurrent();
    renderSettings();
    $("geocode-hint").textContent = status.geocoding
      ? current
        ? "Results favor your last recorded location."
        : "Searches are worldwide until a location is recorded."
      : "Place search needs a geocoding provider.";
    await refreshMeta();
    await Promise.all([refreshExplore(), refreshInsights()]);
  } catch (e) {
    // The sign-in screen already says what is needed.
    if ($("auth-panel").hidden) toast(e.message);
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
    // A watched tracker that went quiet or came back (services/tracker_alerts.py).
    const notices = await api("/api/tracker-notices?since_id=" + (state.lastTrackerNotice ?? 0));
    if (state.lastTrackerNotice != null)
      for (const notice of notices) {
        const text = notice.kind === "quiet" ? `${notice.label} has sent nothing for over ${notice.hours} h` : `${notice.label} is sending again`;
        toast(text);
        if ("Notification" in window && Notification.permission === "granted") new Notification("Tracker", { body: text });
      }
    state.lastTrackerNotice = notices.reduce((max, notice) => Math.max(max, notice.id), state.lastTrackerNotice ?? 0);
  } catch (e) {
    /* Connection errors are surfaced on explicit actions. */
  }
}
