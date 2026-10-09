// GeoLogs browser app: wire each view, draw the map, load data, then keep it fresh.
import { pollEvents, refreshAll } from "./app/refresh.js";
import { $ } from "./core/dom.js";
import { localDateKey } from "./core/format.js";
import { state } from "./core/state.js";
import { initMap } from "./map/map.js";
import { initAuth } from "./views/auth.js";
import { initHistory, refreshCurrent, refreshExplore, refreshInsights } from "./views/history.js";
import { initPlaceDialogs } from "./views/place-dialogs.js";
import { initPlaces } from "./views/places.js";
import { initReminders } from "./views/reminders.js";
import { initSettings } from "./views/settings.js";
import { initShell } from "./views/shell.js";
import { initSummaries } from "./views/summaries.js";

initShell();
initAuth();
initHistory();
initPlaces();
initPlaceDialogs();
initReminders();
initSummaries();
initSettings();
initMap();

refreshAll().then(() => {
  state.lastEventId = state.events.reduce((max, e) => Math.max(max, e.id), 0);
});

// New arrival reminders every 10 s; the current day every minute; patterns every 5 minutes.
setInterval(pollEvents, 10000);
setInterval(() => {
  if (state.followToday && $("day-date").value !== localDateKey()) $("day-date").value = localDateKey();
  refreshCurrent().catch(() => {});
  refreshExplore(false).catch(() => {});
}, 60000);
setInterval(() => refreshInsights().catch(() => {}), 300000);
