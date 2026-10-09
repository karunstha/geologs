// Switching between the app's pages.
import { toast } from "../core/dom.js";
import { state } from "../core/state.js";
import { map } from "../map/map.js";
import { renderExplore } from "./history.js";
import { refreshSources } from "./settings.js";
import { refreshSummaries } from "./summaries.js";

export function showView(view) {
  document.querySelectorAll(".view").forEach((el) => el.classList.toggle("active", el.id === view + "-view"));
  if (view === "settings") refreshSources().catch(() => {});
  document.querySelectorAll(".nav").forEach((el) => {
    const active = el.dataset.view === view;
    el.classList.toggle("active", active);
    if (active) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
  if (view === "explore" && map) setTimeout(() => map.invalidateSize(), 20);
  if (view === "summaries") refreshSummaries().catch((error) => toast(error.message));
}

export function initShell() {
  document.querySelectorAll(".nav").forEach(
    (button) =>
      (button.onclick = () => {
        if (button.dataset.view === "explore") {
          state.focusPlace = null;
          if (state.day && state.coverage) renderExplore(state.day, state.coverage);
        }
        showView(button.dataset.view);
      }),
  );
}
