// Light or dark: your choice for this browser (Settings → Appearance, or the top-bar toggle),
// else the system's. index.html applies it before first paint; this keeps it current.
const KEY = "geologs-theme";
const system = window.matchMedia("(prefers-color-scheme: dark)");

export function themeChoice() {
  try {
    return localStorage.getItem(KEY) || "system";
  } catch (_error) {
    return "system";
  }
}

function apply() {
  const choice = themeChoice();
  const dark = choice === "dark" || (choice === "system" && system.matches);
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

export function initTheme() {
  apply();
  system.addEventListener("change", apply);
  document.getElementById("theme-toggle").onclick = () =>
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  document.querySelectorAll('input[name="theme"]').forEach((input) => (input.onchange = () => setTheme(input.value)));
}
