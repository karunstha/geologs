// Compact icon buttons for list rows: each has a tooltip and an accessible name.
import { safe } from "../core/dom.js";

const ICONS = {
  map: '<svg viewBox="0 0 24 24"><path d="M19 10c0 4.6-7 10-7 10S5 14.6 5 10a7 7 0 0 1 14 0Z"/><circle cx="12" cy="10" r="2.4"/></svg>',
  edit: '<svg viewBox="0 0 24 24"><path d="M4.5 19.5h4l10-10-4-4-10 10Z"/><path d="m13 7 4 4"/></svg>',
  delete:
    '<svg viewBox="0 0 24 24"><path d="M5 7h14M10 11v6M14 11v6M6.5 7l.9 11.2A2 2 0 0 0 9.4 20h5.2a2 2 0 0 0 2-1.8L17.5 7M9.5 7V4.5h5V7"/></svg>',
  cancel:
    '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="m9.2 9.2 5.6 5.6M14.8 9.2l-5.6 5.6"/></svg>',
  day: '<svg viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="15" rx="2"/><path d="M8 3v4M16 3v4M4 10h16"/></svg>',
  open: '<svg viewBox="0 0 24 24"><path d="m9.5 6 6 6-6 6"/></svg>',
};

export function actionIcon(name) {
  return ICONS[name];
}

// actions: [{ icon, label, data: 'data-x="1"', tone: "danger" }]
export function rowActions(actions) {
  return `<div class="row-actions">${actions
    .map(
      ({ icon, label, data, tone }) =>
        `<button type="button" class="icon-button${tone === "danger" ? " danger" : ""}" ${data} aria-label="${safe(label)}" title="${safe(label)}">${ICONS[icon]}</button>`,
    )
    .join("")}</div>`;
}

// Attach one handler to every element in `root` carrying the data attribute `name`.
export function onEach(root, name, handler) {
  root.querySelectorAll(`[data-${name}]`).forEach((element) => {
    element.onclick = (event) =>
      handler(element.dataset[name.replace(/-(\w)/g, (_, c) => c.toUpperCase())], event);
  });
}
