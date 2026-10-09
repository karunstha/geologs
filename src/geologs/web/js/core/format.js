// Dates, times, durations, distances, and coordinates, in the configured timezone.
import { state } from "./state.js";

export function zoneOptions() {
  const timeZone = state.settings?.timezone;
  if (!timeZone) return {};
  try {
    new Intl.DateTimeFormat(undefined, { timeZone });
    return { timeZone };
  } catch (_error) {
    return {};
  }
}

export function formatTime(value) {
  return value
    ? new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short", ...zoneOptions() })
    : "—";
}

export function formatPrecise(value) {
  return value ? new Date(value).toLocaleString(undefined, zoneOptions()) : "—";
}

export function clock(value) {
  return new Date(value).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", ...zoneOptions() });
}

export function durationText(minutes) {
  const total = Math.round(minutes || 0);
  if (total < 1) return "<1 min";
  const hours = Math.floor(total / 60),
    rest = total % 60;
  return hours ? `${hours} h${rest ? ` ${rest} min` : ""}` : `${rest} min`;
}

export function distanceText(km) {
  return km >= 10 ? `${km.toFixed(0)} km` : km >= 0.1 ? `${km.toFixed(1)} km` : `${Math.round(km * 1000)} m`;
}

export function kmh(point) {
  return point?.speed_mps == null ? "—" : `${(point.speed_mps * 3.6).toFixed(1)} km/h`;
}

export function localDateKey(date = new Date()) {
  const timezone = state.settings?.timezone;
  if (timezone) {
    try {
      const parts = Object.fromEntries(
        new Intl.DateTimeFormat("en-US", {
          timeZone: timezone,
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
        })
          .formatToParts(date)
          .map((part) => [part.type, part.value]),
      );
      return `${parts.year}-${parts.month}-${parts.day}`;
    } catch (_error) {}
  }
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");
}

export function shiftDateKey(value, offset) {
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day + offset));
  return [
    date.getUTCFullYear(),
    String(date.getUTCMonth() + 1).padStart(2, "0"),
    String(date.getUTCDate()).padStart(2, "0"),
  ].join("-");
}

export function zonedMidnight(dateKey) {
  // Midnight of dateKey in the configured timezone, not the browser's.
  const [year, month, day] = dateKey.split("-").map(Number);
  const target = Date.UTC(year, month - 1, day);
  const timeZone = zoneOptions().timeZone;
  if (!timeZone) return new Date(year, month - 1, day);
  const format = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  let guess = target;
  for (let step = 0; step < 3; step++) {
    const parts = Object.fromEntries(
      format.formatToParts(new Date(guess)).map((part) => [part.type, part.value]),
    );
    const shown = Date.UTC(
      +parts.year,
      +parts.month - 1,
      +parts.day,
      +parts.hour % 24,
      +parts.minute,
      +parts.second,
    );
    guess += target - shown;
  }
  return new Date(guess);
}

export function ago(value) {
  if (!value) return "never";
  const seconds = Math.max(0, (Date.now() - Date.parse(value)) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  if (seconds < 86400 * 14) return `${Math.floor(seconds / 86400)} day${seconds < 172800 ? "" : "s"} ago`;
  return new Date(value).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    ...zoneOptions(),
  });
}

export function coordinateText(lat, lon) {
  return `${Number(lat).toFixed(6)}, ${Number(lon).toFixed(6)}`;
}

export function metresBetween(aLat, aLon, bLat, bLon) {
  const rad = Math.PI / 180,
    dLat = (bLat - aLat) * rad,
    dLon = (bLon - aLon) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(aLat * rad) * Math.cos(bLat * rad) * Math.sin(dLon / 2) ** 2;
  return 12742000 * Math.asin(Math.min(1, Math.sqrt(h)));
}

export function longDate(value) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

export function dateLabel(value) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}
