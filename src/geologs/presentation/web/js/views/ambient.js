// History's hero: the day's town, its weather, and an Unsplash photo of it behind the page.
import { api } from "../core/api.js";
import { $, safe } from "../core/dom.js";
import { imperial, localDateKey, speedText } from "../core/format.js";
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

function precipitation(mm) {
  if (mm == null) return "—";
  return imperial() ? `${(mm / 25.4).toFixed(mm >= 2.54 ? 1 : 2)} in` : `${mm >= 10 ? Math.round(mm) : mm.toFixed(1)} mm`;
}

// "07:16" from Open-Meteo's local "2026-10-17T07:16", in your clock style.
function localClock(value) {
  if (!value) return "—";
  const [hours, minutes] = value.slice(11, 16).split(":").map(Number);
  return new Date(2000, 0, 1, hours, minutes).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
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

function renderWeather(weather, today) {
  const box = $("weather");
  box.hidden = !weather;
  if (!weather) return;
  const { current, day } = weather;
  const facts = [];
  if (current) {
    facts.push(["High / low", `${temperature(day.high_c)} / ${temperature(day.low_c)}`]);
    facts.push(["Feels like", temperature(current.feels_like_c)]);
    facts.push(["Wind", speedText(current.wind_kmh)]);
    facts.push(
      day.rain_chance != null ? ["Rain chance", `${day.rain_chance}%`] : ["Humidity", `${current.humidity ?? "—"}%`],
    );
  } else {
    facts.push(["Precipitation", precipitation(day.precipitation_mm)]);
    facts.push(["Daylight", `${localClock(day.sunrise)} – ${localClock(day.sunset)}`]);
  }
  const big = current
    ? temperature(current.temperature_c)
    : `${temperature(day.high_c)}<small>/ ${temperature(day.low_c)}</small>`;
  box.innerHTML =
    `<span class="weather-icon">${weatherIcon(weather.icon)}</span>` +
    `<div class="weather-now"><strong class="weather-temp">${big}</strong><span class="weather-label">${safe(weather.label)}${today ? "" : " all day"}</span></div>` +
    `<dl class="weather-facts">${facts.map(([term, value]) => `<div><dt>${term}</dt><dd>${safe(value)}</dd></div>`).join("")}</dl>`;
  box.title = current ? `Updated ${localClock(current.time)} · Open-Meteo` : "Open-Meteo";
}

function renderPhoto(photo) {
  const hero = $("history-hero");
  const backdrop = $("backdrop");
  const credit = $("photo-credit");
  if (!photo) {
    hero.classList.remove("has-photo");
    backdrop.classList.remove("has-photo");
    credit.hidden = true;
    return;
  }
  credit.innerHTML = `Photo by <a href="${safe(photo.photographer_url || photo.link)}" target="_blank" rel="noopener">${safe(photo.photographer)}</a> on <a href="${safe(photo.link || "https://unsplash.com/?utm_source=geologs&utm_medium=referral")}" target="_blank" rel="noopener">Unsplash</a>`;
  credit.title = photo.description || "";
  credit.hidden = false;
  if (shown?.photo?.id === photo.id && hero.classList.contains("has-photo")) return;
  // Show the photo only once it has loaded, so the hero fades from its sky to the picture.
  hero.classList.remove("has-photo");
  const image = new Image();
  image.onload = () => {
    if (shown?.photo?.id !== photo.id) return;
    for (const el of [hero.querySelector(".hero-photo"), backdrop]) {
      el.style.setProperty("--photo-color", photo.color || "transparent");
      el.style.backgroundImage = `url("${photo.url.replace(/"/g, "%22")}")`;
    }
    hero.classList.add("has-photo");
    backdrop.classList.add("has-photo");
  };
  image.src = photo.url;
}

export function initAmbient() {
  $("history-hero").dataset.sky = fallbackSky();
}

export function renderAmbient(value) {
  shown = value;
  const hero = $("history-hero");
  hero.dataset.sky = value?.weather?.icon || fallbackSky();
  const name = placeName(value?.place);
  $("hero-place").hidden = !name;
  $("hero-place-name").textContent = name;
  renderWeather(value?.weather, value?.today);
  renderPhoto(value?.photo);
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
    if (shown) renderWeather(shown.weather, shown.today);
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
    // The hero keeps its sky; the next refresh tries again.
    if (mine === request) shownKey = null;
  }
}
