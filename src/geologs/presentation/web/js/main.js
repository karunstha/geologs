// GeoLogs browser app: wire each view, draw the map, load data, then keep it fresh.
import { pollEvents, refreshAll } from "./app/refresh.js";
import { $ } from "./core/dom.js";
import { localDateKey } from "./core/format.js";
import { state } from "./core/state.js";
import { initMap } from "./map/map.js";
import { initTheme } from "./ui/theme.js";
import { initAccount } from "./views/account.js";
import { initAdmin } from "./views/admin.js";
import { initAuth } from "./views/auth.js";
import { initHistory, refreshCurrent, refreshExplore } from "./views/history.js";
import { refreshInsights } from "./views/patterns.js";
import { initPlaceDialogs } from "./views/place-dialogs.js";
import { initPlaces } from "./views/places.js";
import { initReminders } from "./views/reminders.js";
import { initSettings } from "./views/settings.js";
import { initShell } from "./views/shell.js";
import { initStayEdits } from "./views/stay-edits.js";
import { initSummaries } from "./views/summaries.js";

initTheme();
initShell();
initAccount();
initAdmin();
// Before loading anything: a link from an email (a reset, an invite) opens its own page.
await initAuth();
initHistory();
initPlaces();
initPlaceDialogs();
initReminders();
initSummaries();
initSettings();
initStayEdits();
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
