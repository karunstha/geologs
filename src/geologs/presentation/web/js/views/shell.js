// Switching between the app's pages.
import { toast } from "../core/dom.js";
import { state } from "../core/state.js";
import { map } from "../map/map.js";
import { showPlacesMap } from "../map/places-map.js";
import { renderExplore } from "./history.js";
import { homeShown, newHomeEnabled } from "./home.js";
import { refreshSources } from "./settings.js";
import { refreshAdmin } from "./admin.js";
import { refreshSummaries } from "./summaries.js";

export function showView(view) {
  // The new Home stands in for the classic one wherever the app opens Home (see js/views/home.js).
  if (view === "explore" && newHomeEnabled()) view = "home";
  document.querySelectorAll(".view").forEach((el) => el.classList.toggle("active", el.id === view + "-view"));
  if (view === "settings") refreshSources().catch(() => {});
  document.querySelectorAll(".nav").forEach((el) => {
    const active = el.dataset.view === view;
    el.classList.toggle("active", active);
    if (active) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
  if (view === "explore" && map) setTimeout(() => map.invalidateSize(), 20);
  if (view === "home") homeShown();
  if (view === "places") showPlacesMap();
  if (view === "summaries") refreshSummaries().catch((error) => toast(error.message));
  if (view === "admin") refreshAdmin().catch((error) => toast(error.message));
}

// The top bar is clear at the top of the page and frosted once the page scrolls under it.
function initTopbar() {
  const bar = document.querySelector(".topbar");
  const update = () => bar.classList.toggle("scrolled", window.scrollY > 8);
  window.addEventListener("scroll", update, { passive: true });
  update();
}

export function initShell() {
  initTopbar();
  document.querySelectorAll(".nav").forEach(
    (button) =>
      (button.onclick = () => {
        if (button.dataset.view === "explore" || button.dataset.view === "home") {
          state.focusPlace = null;
          if (state.day && state.coverage) renderExplore(state.day, state.coverage);
        }
        showView(button.dataset.view);
      }),
  );
}
