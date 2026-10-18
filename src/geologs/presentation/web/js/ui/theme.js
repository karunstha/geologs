// Light, dark, auto (dark from sunset to sunrise where you last were) or the system's: your choice
// for this browser (Settings → Appearance, or the top-bar toggle). index.html applies it before
// first paint; this keeps it current.
const KEY = "geologs-theme";
// Today's sunrise and sunset, in minutes after local midnight, for the head script to read.
const SUN_KEY = "geologs-sun";
const system = window.matchMedia("(prefers-color-scheme: dark)");
const DEFAULT_SUN = { rise: 7 * 60, set: 19 * 60 };

export function themeChoice() {
  try {
    return localStorage.getItem(KEY) || "system";
  } catch (_error) {
    return "system";
  }
}

function storedSun() {
  try {
    return JSON.parse(localStorage.getItem(SUN_KEY)) || DEFAULT_SUN;
  } catch (_error) {
    return DEFAULT_SUN;
  }
}

// Sunrise and sunset (NOAA's approximation, within a few minutes) in local minutes after midnight.
// Polar day and night come back as always light (rise 0, set 1440) or always dark (rise = set).
function sunTimes(lat, lon, date = new Date()) {
  const start = new Date(date.getFullYear(), 0, 0);
  const day = Math.floor((date - start) / 86400000);
  const g = (2 * Math.PI / 365) * (day - 1);
  const eq = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g)
    - 0.014615 * Math.cos(2 * g) - 0.040849 * Math.sin(2 * g));
  const decl = 0.006918 - 0.399912 * Math.cos(g) + 0.070257 * Math.sin(g) - 0.006758 * Math.cos(2 * g)
    + 0.000907 * Math.sin(2 * g) - 0.002697 * Math.cos(3 * g) + 0.00148 * Math.sin(3 * g);
  const phi = (lat * Math.PI) / 180;
  const cosHa = Math.cos((90.833 * Math.PI) / 180) / (Math.cos(phi) * Math.cos(decl)) - Math.tan(phi) * Math.tan(decl);
  if (cosHa < -1) return { rise: 0, set: 1440 };
  if (cosHa > 1) return { rise: 720, set: 720 };
  const ha = (Math.acos(cosHa) * 180) / Math.PI;
  const local = (utc) => (((utc - date.getTimezoneOffset()) % 1440) + 1440) % 1440;
  return { rise: Math.round(local(720 - 4 * (lon + ha) - eq)), set: Math.round(local(720 - 4 * (lon - ha) - eq)) };
}

function daylight(sun = storedSun(), now = new Date()) {
  const minute = now.getHours() * 60 + now.getMinutes();
  return sun.rise <= sun.set ? minute >= sun.rise && minute < sun.set : minute >= sun.rise || minute < sun.set;
}

function apply() {
  const choice = themeChoice();
  const dark = choice === "dark" || (choice === "system" && system.matches) || (choice === "auto" && !daylight());
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.getElementById("theme-color").content = dark ? "#0e1113" : "#f6f7f8";
  const toggle = document.getElementById("theme-toggle");
  const label = dark ? "Switch to light theme" : "Switch to dark theme";
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
  document.querySelectorAll('input[name="theme"]').forEach((input) => (input.checked = input.value === choice));
}

export function setTheme(choice) {
  try {
    if (choice === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, choice);
  } catch (_error) {}
  apply();
}

// Called with the latest recorded position, so Auto follows the sun where you are.
export function setSunPosition(position) {
  if (!position || !Number.isFinite(position.lat) || !Number.isFinite(position.lon)) return;
  try {
    localStorage.setItem(SUN_KEY, JSON.stringify(sunTimes(position.lat, position.lon)));
  } catch (_error) {}
  apply();
}

export function initTheme() {
  apply();
  system.addEventListener("change", apply);
  // Auto turns over at sunrise and sunset without a reload.
  setInterval(() => themeChoice() === "auto" && apply(), 60000);
  document.getElementById("theme-toggle").onclick = () =>
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  document.querySelectorAll('input[name="theme"]').forEach((input) => (input.onchange = () => setTheme(input.value)));
}
