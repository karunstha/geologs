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

// Time of day to the second, for raw samples.
export function clockPrecise(value) {
  return new Date(value).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    ...zoneOptions(),
  });
}

// "Thursday, October 8" for a YYYY-MM-DD date.
export function dayTitle(value) {
  const [year, month, day] = value.split("-").map(Number);
  return new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric" }).format(
    new Date(year, month - 1, day, 12),
  );
}

export function plural(count, word, many = word + "s") {
  return `${Number(count).toLocaleString()} ${count === 1 ? word : many}`;
}

export function durationText(minutes) {
  const total = Math.round(minutes || 0);
  if (total < 1) return "<1 min";
  const hours = Math.floor(total / 60),
    rest = total % 60;
  return hours ? `${hours} h${rest ? ` ${rest} min` : ""}` : `${rest} min`;
}

// Units: everything is stored metric; Settings → Appearance chooses what you read.
const KM_PER_MILE = 1.609344;
const FEET_PER_METRE = 3.28084;
const LITRES_PER_GALLON = 3.785411784;

export function imperial() {
  return state.settings?.units === "imperial";
}

export function distanceText(km) {
  if (imperial()) {
    const miles = km / KM_PER_MILE;
    return miles >= 10 ? `${miles.toFixed(0)} mi` : miles >= 0.1 ? `${miles.toFixed(1)} mi` : `${Math.round((km * 1000 * FEET_PER_METRE) / 10) * 10} ft`;
  }
  return km >= 10 ? `${km.toFixed(0)} km` : km >= 0.1 ? `${km.toFixed(1)} km` : `${Math.round(km * 1000)} m`;
}

// A short length such as a radius or an accuracy: metres or feet.
export function lengthText(metres) {
  if (metres == null) return "—";
  return imperial() ? `${Math.round(metres * FEET_PER_METRE).toLocaleString()} ft` : `${Math.round(metres).toLocaleString()} m`;
}

// A zone's or search's reach, rounded the way people say it: "within 0.4 mi", not "within 1,969 ft".
export function radiusText(metres) {
  if (metres == null) return "—";
  if (imperial()) {
    const feet = metres * FEET_PER_METRE;
    return feet < 1000 ? `${Math.round(feet / 10) * 10} ft` : `${(metres / 1000 / KM_PER_MILE).toFixed(1)} mi`;
  }
  return metres < 1000 ? `${Math.round(metres / 10) * 10} m` : `${(metres / 1000).toFixed(1)} km`;
}

export const lengthUnit = () => (imperial() ? "ft" : "m");
export const toLengthUnit = (metres) => (imperial() ? metres * FEET_PER_METRE : metres);
export const fromLengthUnit = (value) => (imperial() ? value / FEET_PER_METRE : value);

export const speedUnit = () => (imperial() ? "mph" : "km/h");
export const toSpeedUnit = (kmhValue) => (imperial() ? kmhValue / KM_PER_MILE : kmhValue);
export const fromSpeedUnit = (value) => (imperial() ? value * KM_PER_MILE : value);

// A speed given in km/h, in your units.
export function speedText(kmhValue, digits = 0) {
  return kmhValue == null ? "—" : `${toSpeedUnit(kmhValue).toFixed(digits)} ${speedUnit()}`;
}

export function kmh(point) {
  return point?.speed_mps == null ? "—" : speedText(point.speed_mps * 3.6, 1);
}

// Fuel: economy is stored in L/100 km and prices per litre.
export const economyUnit = () => (imperial() ? "mpg" : "L/100 km");
export const toEconomyUnit = (l100km) => (l100km ? (imperial() ? 235.214583 / l100km : l100km) : null);
export const fromEconomyUnit = (value) => (value ? (imperial() ? 235.214583 / value : value) : null);
export const volumeUnit = () => (imperial() ? "gal" : "L");
export const toVolumeUnit = (litres) => (imperial() ? litres / LITRES_PER_GALLON : litres);
export const toPerVolume = (perLitre) => (imperial() ? perLitre * LITRES_PER_GALLON : perLitre);
export const fromPerVolume = (value) => (imperial() ? value / LITRES_PER_GALLON : value);
export const volumeText = (litres) => `${(Math.round(toVolumeUnit(litres || 0) * 10) / 10).toLocaleString()} ${volumeUnit()}`;

const COMMON_CURRENCIES = ["USD", "EUR", "GBP", "CAD", "AUD", "INR", "JPY", "CNY", "NPR"];
// Symbols saved before currencies were chosen from a list.
const LEGACY_SYMBOLS = { $: "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "₨": "NPR" };

export const currencyCode = (value) => LEGACY_SYMBOLS[value] || value;

// <option>s for every currency this browser knows, the common ones first, labelled "US Dollar (USD, $)".
export function currencyOptions(selected) {
  let codes = COMMON_CURRENCIES;
  try {
    codes = Intl.supportedValuesOf("currency");
  } catch (_error) {}
  let names = null;
  try {
    names = new Intl.DisplayNames(undefined, { type: "currency" });
  } catch (_error) {}
  const label = (code) => {
    const symbol = (() => {
      try {
        return new Intl.NumberFormat(undefined, { style: "currency", currency: code }).formatToParts(0).find((part) => part.type === "currency")?.value;
      } catch (_error) {
        return "";
      }
    })();
    const name = names?.of(code) || code;
    return symbol && symbol !== code ? `${name} (${code}, ${symbol})` : `${name} (${code})`;
  };
  const option = (code) => `<option value="${code}">${label(code).replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`)}</option>`;
  const common = COMMON_CURRENCIES.filter((code) => codes.includes(code));
  const rest = codes.filter((code) => !common.includes(code)).sort((a, b) => label(a).localeCompare(label(b)));
  const known = codes.includes(currencyCode(selected));
  const custom = selected && !known ? `<option value="${selected.replace(/"/g, "&quot;")}">${selected.replace(/</g, "&lt;")} (saved earlier)</option>` : "";
  return `${custom}<optgroup label="Common">${common.map(option).join("")}</optgroup><optgroup label="All currencies">${rest.map(option).join("")}</optgroup>`;
}

// A fuel cost with the fuel it bought beside it: "$4.20 · 1.2 gal".
export function fuelText(cost, litres, currency) {
  return litres == null ? moneyText(cost, currency) : `${moneyText(cost, currency)} · ${volumeText(litres)}`;
}

export function moneyText(amount, currency = state.settings?.currency || "USD") {
  if (amount == null) return "—";
  // An ISO code is formatted the way this browser writes that currency, with its plain symbol ($12.40, €12.40).
  if (/^[A-Z]{3}$/.test(currency)) {
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency, currencyDisplay: "narrowSymbol" }).format(amount);
    } catch (_error) {}
  }
  const value = Number(amount).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  // A symbol goes in front ($12.40); a code goes after (12.40 EUR).
  return /^[A-Za-z]{2,}$/.test(currency) ? `${value} ${currency}` : `${currency}${value}`;
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
