// History's backdrop: the day's town, and an Unsplash photo of it in its weather, fading down the top of the page.
import { api } from "../core/api.js";
import { $, safe } from "../core/dom.js";
import { localDateKey } from "../core/format.js";
import { state } from "../core/state.js";

let shownKey = null;
let fetchedAt = 0;
let request = 0;
let shown = null;
// Today's photo and sky are asked for again after this long; a past day's never change.
const TODAY_REFRESH_MS = 10 * 60 * 1000;

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

const UNSPLASH = "https://unsplash.com/";
const REFERRAL = "utm_source=geologs&utm_medium=referral";

// Only links back to Unsplash itself are used, whatever the server sent.
function unsplashLink(url, fallback) {
  return typeof url === "string" && url.startsWith(UNSPLASH) ? url : fallback;
}

// Unsplash asks that a photo credit its photographer and Unsplash, with links back.
function showCredit(photo) {
  const credit = $("photo-credit");
  const photographer = unsplashLink(photo.photographer_url, `${UNSPLASH}?${REFERRAL}`);
  const home = `${UNSPLASH}?${REFERRAL}`;
  credit.innerHTML = `Photo by <a href="${safe(photographer)}" target="_blank" rel="noopener">${safe(photo.photographer)}</a> on <a href="${safe(home)}" target="_blank" rel="noopener">Unsplash</a>`;
  credit.title = photo.description || "";
  credit.hidden = false;
}

function renderPhoto(photo, previous) {
  const backdrop = $("backdrop");
  if (!photo) {
    backdrop.classList.remove("has-photo");
    $("photo-credit").hidden = true;
    return;
  }
  if (previous?.photo?.id === photo.id && backdrop.classList.contains("has-photo")) return;
  // Swap in the photo only once it has loaded, so the backdrop never flashes empty.
  const image = new Image();
  image.onload = () => {
    if (shown?.photo?.id !== photo.id) return;
    backdrop.style.setProperty("--photo", `url("${photo.url.replace(/"/g, "%22")}")`);
    backdrop.classList.add("has-photo");
    showCredit(photo);
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
  if (key === shownKey && (!today || Date.now() - fetchedAt < TODAY_REFRESH_MS)) return;
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
