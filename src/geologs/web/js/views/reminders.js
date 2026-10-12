// Reminders: creating, listing, and cancelling arrival reminders.
import { refreshMeta } from "../app/refresh.js";
import { api, post } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { ago, formatTime, fromLengthUnit } from "../core/format.js";
import { state } from "../core/state.js";
import { onEach, rowActions } from "../ui/actions.js";
import { placeResultsMarkup, resolvePlace, searchPlaces } from "../ui/place-search.js";
import { placeIcon, statusPill } from "../ui/icons.js";

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
  $("active-reminder-count").textContent = state.reminders.length
    ? state.reminders.length.toLocaleString()
    : "";
  $("reminder-history-count").textContent = state.reminderHistory.length
    ? state.reminderHistory.length.toLocaleString()
    : "";
  const waiting = $("reminders-list");
  waiting.innerHTML =
    state.reminders
      .map(
        (reminder) =>
          `<div class="list-item"><div class="place-main">${placeIcon(reminder)}<div><strong>${safe(reminder.text)}</strong><small title="${safe(formatTime(reminder.created_at))}">${safe(reminder.match_name ? reminderWhere(reminder) : "At " + reminder.place_name)} · added ${safe(ago(reminder.created_at))}</small></div></div>${rowActions([{ icon: "cancel", label: "Cancel reminder", data: `data-cancel-reminder="${reminder.id}"`, tone: "danger" }])}</div>`,
      )
      .join("") ||
    '<div class="empty-block"><strong>Nothing waiting</strong><span>Reminders you create appear here until they fire.</span></div>';
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
        const at = fired ? item.triggered_at : item.cancelled_at || item.created_at;
        return `<div class="list-item"><div class="place-main">${placeIcon(item)}<div><strong>${safe(item.text)}</strong><small title="${safe(formatTime(at))}">${safe(reminderWhere(item))} · ${safe(ago(at))}</small></div></div>${statusPill(fired ? "success" : "neutral", fired ? "Fired" : "Cancelled")}</div>`;
      })
      .join("") ||
    '<div class="empty-block"><strong>No history yet</strong><span>Reminders that fire or are cancelled are kept here.</span></div>';
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
      $("reminder-form").reset();
      $("reminder-search-results").innerHTML = "";
      toggleReminderDestination();
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
