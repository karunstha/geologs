// History's backdrop: the day's town, its weather, and an Unsplash photo of it fading down the top of the page.
import { api } from "../core/api.js";
import { $, safe } from "../core/dom.js";
import { imperial, localDateKey } from "../core/format.js";
import { state } from "../core/state.js";
import { weatherIcon } from "../ui/weather-icons.js";

let shownKey = null;
let fetchedAt = 0;
let request = 0;
let shown = null;
// Today's weather is asked for again after this long; a past day's never changes.
const TODAY_REFRESH_MS = 10 * 60 * 1000;

function temperature(celsius) {
  if (celsius == null) return "—";
  return `${Math.round(imperial() ? (celsius * 9) / 5 + 32 : celsius)}°`;
}

function placeName(place) {
  if (!place) return "";
  const parts = [place.city, place.region && place.region !== place.city ? place.region : null];
  if (!parts[0] && !parts[1]) parts.push(place.country);
  return parts.filter(Boolean).join(", ");
}

// Before the server answers, or with no position at all: day or night by the clock.
function fallbackSky() {
  const hour = new Date().getHours();
  return hour >= 7 && hour < 19 ? "clear" : "clear-night";
}

function renderWeather(weather) {
  const box = $("weather");
  box.hidden = !weather;
  if (!weather) return;
  const { current, day } = weather;
  const range = `H ${temperature(day.high_c)} · L ${temperature(day.low_c)}`;
  box.innerHTML =
    `<span class="weather-icon">${weatherIcon(weather.icon)}</span>` +
    (current ? `<strong class="weather-temp">${temperature(current.temperature_c)}</strong>` : "") +
    `<span class="weather-text"><strong>${safe(weather.label)}</strong><span>${range}</span></span>`;
}

function renderPhoto(photo, previous) {
  const backdrop = $("backdrop");
  if (!photo) {
    backdrop.classList.remove("has-photo");
    return;
  }
  if (previous?.photo?.id === photo.id && backdrop.classList.contains("has-photo")) return;
  // Swap in the photo only once it has loaded, so the backdrop never flashes empty.
  const image = new Image();
  image.onload = () => {
    if (shown?.photo?.id !== photo.id) return;
    backdrop.style.setProperty("--photo", `url("${photo.url.replace(/"/g, "%22")}")`);
    backdrop.classList.add("has-photo");
  };
  image.src = photo.url;
}

export function initAmbient() {
  $("backdrop").dataset.sky = fallbackSky();
}

export function renderAmbient(value) {
  const previous = shown;
  shown = value;
  $("backdrop").dataset.sky = value?.weather?.icon || fallbackSky();
  const name = placeName(value?.place);
  $("hero-place").hidden = !name;
  $("hero-place-name").textContent = name;
  renderWeather(value?.weather);
  renderPhoto(value?.photo, previous);
}

// The position a day is told by: where you are now for today, else where that day's history ends.
function dayPosition(day) {
  if (day.date === localDateKey() && state.current) return state.current;
  return day.last_location || (day.date === localDateKey() ? null : state.current);
}

export async function refreshAmbient(day = state.day) {
  if (!day) return;
  const point = dayPosition(day);
  if (!point) {
    shownKey = null;
    renderAmbient(null);
    return;
  }
  const key = `${day.date}|${point.lat.toFixed(2)},${point.lon.toFixed(2)}`;
  const today = day.date === localDateKey();
  if (key === shownKey && (!today || Date.now() - fetchedAt < TODAY_REFRESH_MS)) {
    // Same day and place: only redraw, in case the units changed.
    if (shown) renderWeather(shown.weather);
    return;
  }
  shownKey = key;
  fetchedAt = Date.now();
  const mine = ++request;
  const timezone = state.settings?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const query = new URLSearchParams({ lat: point.lat, lon: point.lon, date: day.date, timezone });
  try {
    const value = await api("/api/ambient?" + query);
    if (mine === request) renderAmbient(value);
  } catch (_error) {
    // The backdrop keeps its sky; the next refresh tries again.
    if (mine === request) shownKey = null;
  }
}
