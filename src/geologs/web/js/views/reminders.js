// Reminders: creating, listing, and cancelling arrival reminders.
import { refreshMeta } from "../app/refresh.js";
import { api, post } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { formatTime } from "../core/format.js";
import { state } from "../core/state.js";
import { placeIcon } from "../ui/icons.js";

export function fillPlaceSelectors() {
  const select = $("reminder-place"),
    value = select.value;
  select.innerHTML =
    '<option value="">Select a saved place</option>' +
    state.places.map((p) => `<option value="${p.id}">${safe(p.name)}</option>`).join("") +
    '<option value="other">Other place or coordinates…</option><option value="named">Any place named…</option>';
  select.value = [...select.options].some((option) => option.value === value) ? value : "";
  toggleReminderDestination();
}

export function toggleReminderDestination() {
  const choice = $("reminder-place").value,
    custom = choice === "other",
    named = choice === "named";
  $("reminder-custom-place").hidden = !custom;
  $("reminder-named-place").hidden = !named;
  $("reminder-match-name").required = named;
  for (const id of ["reminder-lat", "reminder-lon"]) $(id).required = custom;
}

export function reminderWhere(reminder) {
  return reminder.match_name ? `Any place named “${reminder.match_name}”` : reminder.place_name;
}

export function setReminderDestination(place) {
  $("reminder-place-name").value = (place.name || place.place_name || "").slice(0, 120);
  $("reminder-lat").value = place.lat ?? "";
  $("reminder-lon").value = place.lon ?? "";
  $("reminder-address").value = place.address || "";
  $("reminder-category").value = place.category || "";
  $("reminder-provider-id").value = place.provider_place_id || "";
  $("reminder-search-results").innerHTML = "";
}

export function renderReminders() {
  $("active-reminder-count").textContent = state.reminders.length.toLocaleString();
  $("active-reminder-count").hidden = !state.reminders.length;
  $("reminder-history-count").textContent = state.reminderHistory.length.toLocaleString();
  $("reminder-history-count").hidden = !state.reminderHistory.length;
  $("reminders-list").innerHTML =
    state.reminders
      .map(
        (r) =>
          `<div class="list-item"><div class="place-main">${placeIcon(r)}<div><strong>${safe(r.text)}</strong><small>${safe(r.match_name ? reminderWhere(r) : "At " + r.place_name)} · Created ${safe(formatTime(r.created_at))}</small></div></div><button data-cancel-reminder="${r.id}">Cancel</button></div>`,
      )
      .join("") || '<div class="empty">No active reminders.</div>';
  $("reminders-list")
    .querySelectorAll("[data-cancel-reminder]")
    .forEach(
      (button) =>
        (button.onclick = async () => {
          try {
            await post("/api/reminders/cancel", { id: +button.dataset.cancelReminder });
            await refreshMeta();
            toast("Reminder cancelled");
          } catch (error) {
            toast(error.message);
          }
        }),
    );
  $("reminder-history").innerHTML =
    state.reminderHistory
      .map(
        (item) =>
          `<div class="list-item"><div class="place-main">${placeIcon(item)}<div><strong>${safe(item.text)}</strong><small>${safe(reminderWhere(item))} · ${item.status === "triggered" ? "Triggered " + safe(formatTime(item.triggered_at)) : "Cancelled " + safe(formatTime(item.cancelled_at || item.created_at))}</small></div></div></div>`,
      )
      .join("") || '<div class="empty">Triggered and cancelled reminders will appear here.</div>';
}

export function initReminders() {
  $("reminder-place").onchange = toggleReminderDestination;
  $("reminder-search-button").onclick = async () => {
    const query = $("reminder-place-search").value.trim();
    if (!query) {
      toast("Enter a place name");
      return;
    }
    try {
      const q = new URLSearchParams({ q: query });
      if (state.current) {
        q.set("lat", state.current.lat);
        q.set("lon", state.current.lon);
      }
      const results = await api("/api/geocode?" + q);
      $("reminder-search-results").innerHTML =
        results
          .map(
            (place, index) =>
              `<div class="result-item"><div class="place-main">${placeIcon(place)}<div><strong>${safe(place.name)}</strong><span>${safe(place.address || "")} · ${Number(place.lat).toFixed(5)}, ${Number(place.lon).toFixed(5)}</span></div></div><button type="button" data-reminder-result="${index}">Use</button></div>`,
          )
          .join("") || '<p class="empty">No matches.</p>';
      $("reminder-search-results")
        .querySelectorAll("[data-reminder-result]")
        .forEach(
          (button) =>
            (button.onclick = () => setReminderDestination(results[+button.dataset.reminderResult])),
        );
    } catch (error) {
      toast(error.message);
    }
  };
  $("reminder-use-current").onclick = () => {
    if (!state.current) {
      toast("No current location is available");
      return;
    }
    setReminderDestination({
      ...state.current,
      name: state.current.place_name || state.current.address || "Current location",
    });
    toast("Current coordinates selected");
  };
  $("reminder-reverse").onclick = async () => {
    if (!$("reminder-lat").value || !$("reminder-lon").value) {
      toast("Enter coordinates first");
      return;
    }
    try {
      const q = new URLSearchParams({ lat: $("reminder-lat").value, lon: $("reminder-lon").value });
      const result = await api("/api/reverse?" + q);
      setReminderDestination({ ...result, lat: +$("reminder-lat").value, lon: +$("reminder-lon").value });
      toast(result.approximate ? "Nearby cached place found — verify before creating" : "Place found");
    } catch (error) {
      toast(error.message);
    }
  };
  $("reminder-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const custom = $("reminder-place").value === "other";
      const payload =
        $("reminder-place").value === "named"
          ? { text: $("reminder-text").value, match_name: $("reminder-match-name").value }
          : custom
            ? {
                text: $("reminder-text").value,
                place_name: $("reminder-place-name").value,
                lat: +$("reminder-lat").value,
                lon: +$("reminder-lon").value,
                radius_m: +$("reminder-radius").value,
                address: $("reminder-address").value,
                category: $("reminder-category").value,
                provider_place_id: $("reminder-provider-id").value,
              }
            : { text: $("reminder-text").value, place_id: +$("reminder-place").value };
      await post("/api/reminders", payload);
      $("reminder-form").reset();
      $("reminder-search-results").innerHTML = "";
      toggleReminderDestination();
      await refreshMeta();
      toast("Reminder created");
    } catch (err) {
      toast(err.message);
    }
  };
  $("notifications").onclick = async () => {
    if (!("Notification" in window)) {
      toast("Browser notifications are unavailable");
      return;
    }
    const permission = await Notification.requestPermission();
    toast(permission === "granted" ? "Browser alerts enabled" : "Browser alerts were not enabled");
  };
}
