// Reminders: creating, listing, and cancelling arrival reminders.
import { refreshMeta } from "../app/refresh.js";
import { api, post } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { ago, formatTime, fromLengthUnit, lengthText, radiusText } from "../core/format.js";
import { state } from "../core/state.js";
import { onEach, rowActions } from "../ui/actions.js";
import { placeResultsMarkup, resolvePlace, searchPlaces } from "../ui/place-search.js";
import { statusPill } from "../ui/icons.js";

function destinationKind() {
  return document.querySelector('input[name="reminder-kind"]:checked')?.value || "saved";
}

export function fillPlaceSelectors() {
  const select = $("reminder-place"),
    value = select.value;
  select.innerHTML =
    `<option value="">${state.places.length ? "Choose a saved place" : "No saved places yet"}</option>` +
    state.places.map((place) => `<option value="${place.id}">${safe(place.name)}</option>`).join("");
  select.value = [...select.options].some((option) => option.value === value) ? value : "";
  select.disabled = !state.places.length;
  $("reminder-no-places").hidden = !!state.places.length;
  toggleReminderDestination();
}

export function toggleReminderDestination() {
  const kind = destinationKind();
  $("reminder-saved-place").hidden = kind !== "saved";
  $("reminder-named-place").hidden = kind !== "named";
  $("reminder-custom-place").hidden = kind !== "location";
  $("reminder-place").required = kind === "saved";
  $("reminder-match-name").required = kind === "named";
  for (const id of ["reminder-lat", "reminder-lon"]) $(id).required = kind === "location";
  $("reminder-hint").textContent =
    kind === "named"
      ? "Fires the first time you stop for a few minutes near any place with this name."
      : "Fires once, the first time a new location arrives inside the area.";
}

function reminderWhere(reminder) {
  return reminder.match_name ? `Any place named “${reminder.match_name}”` : reminder.place_name;
}

// Where a reminder fires, with an icon for the kind: a saved place or location, or any place with a name.
function whereMarkup(reminder) {
  const icon = reminder.match_name
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.5 12.2V4.5a1 1 0 0 1 1-1h7.7l8.3 8.3-8.7 8.7Z"/><circle cx="8" cy="8" r="1.4"/></svg>'
    : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 10c0 4.6-7 10-7 10S5 14.6 5 10a7 7 0 0 1 14 0Z"/><circle cx="12" cy="10" r="2.4"/></svg>';
  const zone = !reminder.match_name && reminder.radius_m ? ` · within ${radiusText(reminder.radius_m)}` : "";
  return `<span class="reminder-where" title="${safe(reminder.place_address || reminder.destination_address || "")}">${icon}${safe(reminderWhere(reminder) + zone)}</span>`;
}

function setReminderDestination(place) {
  $("reminder-place-name").value = (place.name || place.place_name || "").slice(0, 120);
  $("reminder-lat").value = place.lat ?? "";
  $("reminder-lon").value = place.lon ?? "";
  $("reminder-address").value = place.address || "";
  $("reminder-category").value = place.category || "";
  $("reminder-provider-id").value = place.provider_place_id || "";
  $("reminder-search-results").innerHTML = "";
}

export function renderReminders() {
  $("active-reminder-count").textContent = state.reminders.length ? state.reminders.length.toLocaleString() : "";
  $("reminder-history-count").textContent = state.reminderHistory.length ? state.reminderHistory.length.toLocaleString() : "";
  const waiting = $("reminders-list");
  waiting.innerHTML =
    state.reminders
      .map(
        (reminder) =>
          `<div class="list-item reminder-row"><div class="reminder-main"><strong>${safe(reminder.text)}</strong><small>${whereMarkup(reminder)}<span title="${safe(formatTime(reminder.created_at))}">Added ${safe(ago(reminder.created_at))}</span></small></div>${rowActions([{ icon: "cancel", label: "Cancel reminder", data: `data-cancel-reminder="${reminder.id}"`, tone: "danger" }])}</div>`,
      )
      .join("") ||
    '<div class="empty-block"><strong>Nothing waiting</strong><span>Add a note for the next time you get somewhere: pick up salt at Walmart, water the plants at home.</span><button type="button" class="button primary small" data-new-reminder>New reminder</button></div>';
  waiting.querySelector("[data-new-reminder]")?.addEventListener("click", openReminderDialog);
  onEach(waiting, "cancel-reminder", async (id) => {
    try {
      await post("/api/reminders/cancel", { id: +id });
      await refreshMeta();
      toast("Reminder cancelled");
    } catch (error) {
      toast(error.message);
    }
  });
  $("reminder-history").innerHTML =
    state.reminderHistory
      .map((item) => {
        const fired = item.status === "triggered";
        const at = fired ? item.triggered_at || item.last_triggered_at : item.cancelled_at || item.created_at;
        return `<div class="list-item reminder-row"><div class="reminder-main"><strong>${safe(item.text)}</strong><small>${whereMarkup(item)}<span title="${safe(formatTime(at))}">${fired ? "Fired" : "Cancelled"} ${safe(ago(at))}</span></small></div><div class="reminder-end">${statusPill(fired ? "success" : "neutral", fired ? "Fired" : "Cancelled")}${rowActions([{ icon: "delete", label: "Delete from history", data: `data-delete-reminder="${item.id}"`, tone: "danger" }])}</div></div>`;
      })
      .join("") || '<div class="empty-block"><strong>No history yet</strong><span>Fired and cancelled reminders show here.</span></div>';
  const history = $("reminder-history");
  if (state.reminderHistory.length)
    history.insertAdjacentHTML("beforeend", '<div class="list-foot"><button type="button" class="button secondary small" data-clear-history>Clear history</button></div>');
  onEach(history, "delete-reminder", async (id) => {
    try {
      await post("/api/reminders/delete", { id: +id });
      await refreshMeta();
      toast("Deleted from history");
    } catch (error) {
      toast(error.message);
    }
  });
  history.querySelector("[data-clear-history]")?.addEventListener("click", async () => {
    const count = state.reminderHistory.length;
    if (!confirm(`Delete all ${count} reminder${count === 1 ? "" : "s"} in history? This can't be undone.`)) return;
    try {
      await post("/api/reminders/clear-history", {});
      await refreshMeta();
      toast("History cleared");
    } catch (error) {
      toast(error.message);
    }
  });
}

function showReminderTab(tab) {
  document.querySelectorAll("[data-reminder-tab]").forEach((button) => {
    const active = button.dataset.reminderTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $("reminders-list").hidden = tab !== "waiting";
  $("reminder-history").hidden = tab !== "history";
}

function openReminderDialog() {
  toggleReminderDestination();
  $("reminder-dialog").showModal();
}

function closeReminderDialog() {
  $("reminder-dialog").close();
  $("reminder-form").reset();
  $("reminder-search-results").innerHTML = "";
}

// The alerts button says what the browser allows rather than always offering to enable.
function renderAlertState() {
  const button = $("notifications");
  const label = $("notifications-label");
  if (!("Notification" in window)) {
    label.textContent = "Alerts unavailable";
    button.disabled = true;
    button.title = "This browser can't show notifications.";
    return;
  }
  const permission = Notification.permission;
  button.dataset.state = permission;
  button.disabled = permission === "granted";
  label.textContent =
    permission === "granted"
      ? "Browser alerts on"
      : permission === "denied"
        ? "Alerts blocked"
        : "Enable browser alerts";
  button.title =
    permission === "denied"
      ? "Allow notifications for this site in your browser settings."
      : permission === "granted"
        ? "Arrival reminders also appear as notifications while this tab is open."
        : "Show arrival reminders as notifications while this tab is open.";
}

async function searchDestination() {
  const query = $("reminder-place-search").value.trim();
  if (!query) {
    toast("Enter a place to search for");
    return;
  }
  const box = $("reminder-search-results");
  box.innerHTML = '<p class="empty">Searching near you…</p>';
  try {
    const results = await searchPlaces(query);
    box.innerHTML = placeResultsMarkup(results, "data-reminder-result");
    onEach(box, "reminder-result", async (index) => {
      try {
        setReminderDestination(await resolvePlace(results[+index]));
      } catch (error) {
        toast(error.message);
      }
    });
  } catch (error) {
    box.innerHTML = `<p class="empty">${safe(error.message)}</p>`;
  }
}

export function initReminders() {
  document
    .querySelectorAll('input[name="reminder-kind"]')
    .forEach((input) => (input.onchange = toggleReminderDestination));
  $("reminder-search-button").onclick = searchDestination;
  $("new-reminder").onclick = openReminderDialog;
  $("close-reminder-dialog").onclick = closeReminderDialog;
  $("cancel-reminder").onclick = closeReminderDialog;
  document.querySelectorAll("[data-reminder-tab]").forEach((button) => (button.onclick = () => showReminderTab(button.dataset.reminderTab)));
  $("reminder-place-search").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      searchDestination();
    }
  });
  $("reminder-use-current").onclick = () => {
    if (!state.current) {
      toast("No current location yet");
      return;
    }
    setReminderDestination({
      ...state.current,
      name: state.current.place_name || state.current.address || "Current location",
    });
    toast("Using your current location");
  };
  $("reminder-reverse").onclick = async () => {
    if (!$("reminder-lat").value || !$("reminder-lon").value) {
      toast("Enter coordinates first");
      return;
    }
    try {
      const params = new URLSearchParams({ lat: $("reminder-lat").value, lon: $("reminder-lon").value });
      const result = await api("/api/reverse?" + params);
      setReminderDestination({ ...result, lat: +$("reminder-lat").value, lon: +$("reminder-lon").value });
      toast(result.approximate ? "Found a nearby place; check it before saving" : "Place found");
    } catch (error) {
      toast(error.message);
    }
  };
  $("reminder-form").onsubmit = async (event) => {
    event.preventDefault();
    const text = $("reminder-text").value;
    const kind = destinationKind();
    const payload =
      kind === "named"
        ? { text, match_name: $("reminder-match-name").value }
        : kind === "location"
          ? {
              text,
              place_name: $("reminder-place-name").value,
              lat: +$("reminder-lat").value,
              lon: +$("reminder-lon").value,
              radius_m: Math.round(fromLengthUnit(+$("reminder-radius").value)),
              address: $("reminder-address").value,
              category: $("reminder-category").value,
              provider_place_id: $("reminder-provider-id").value,
            }
          : { text, place_id: +$("reminder-place").value };
    try {
      await post("/api/reminders", payload);
      closeReminderDialog();
      showReminderTab("waiting");
      await refreshMeta();
      toast("Reminder created");
    } catch (error) {
      toast(error.message);
    }
  };
  $("notifications").onclick = async () => {
    if (!("Notification" in window)) return;
    const permission = await Notification.requestPermission();
    renderAlertState();
    toast(permission === "granted" ? "Browser alerts are on" : "Browser alerts weren't turned on");
  };
  renderAlertState();
}
