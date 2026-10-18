// Travel modes, place kinds, and source names, with their icons and status pills.
import { safe } from "../core/dom.js";
import { KIND_INFO, kindOf } from "./place-kinds.js";

export const modeInfo = {
  driving: {
    label: "Drive",
    noun: "Driving",
    color: "#3567ad",
    icon: '<svg viewBox="0 0 24 24"><path d="M5 11l1.6-4.2A2 2 0 0 1 8.5 5.5h7a2 2 0 0 1 1.9 1.3L19 11"/><path d="M3.5 11h17v5.5h-17z"/><path d="M6 16.5v2M18 16.5v2"/><circle cx="7.5" cy="13.7" r=".6"/><circle cx="16.5" cy="13.7" r=".6"/></svg>',
  },
  cycling: {
    label: "Bike",
    noun: "Cycling",
    color: "#8352bd",
    icon: '<svg viewBox="0 0 24 24"><circle cx="6" cy="16" r="3.5"/><circle cx="18" cy="16" r="3.5"/><path d="M6 16l3.5-7h6L18 16M9.5 9l3 7H6M14 5.5h2.5l-1 3.5"/></svg>',
  },
  running: {
    label: "Run",
    noun: "Running",
    color: "#cf5f22",
    icon: '<svg viewBox="0 0 24 24"><circle cx="15.5" cy="4.5" r="1.8"/><path d="M5 20.5l3.5-.5 2.2-3.8M10.7 16.2l3.3 2.3v3M7 11.5l2.5-3.3 4.3.8 2.4 3.3 3 .7M10.8 16.2l2.9-7.2"/></svg>',
  },
  passenger: {
    label: "Passenger",
    noun: "Passenger",
    color: "#3d8aa6",
    icon: '<svg viewBox="0 0 24 24"><circle cx="12" cy="3.6" r="1.6"/><path d="M5 13l1.5-4A2 2 0 0 1 8.4 7.7h7.2a2 2 0 0 1 1.9 1.3L19 13"/><path d="M3.5 13h17v4.5h-17z"/><path d="M6 17.5v2M18 17.5v2"/></svg>',
  },
  transit: {
    label: "Transit",
    noun: "Transit",
    color: "#a83f78",
    icon: '<svg viewBox="0 0 24 24"><rect x="5" y="3.5" width="14" height="14" rx="2.5"/><path d="M5 11h14M8 17.5v2.5M16 17.5v2.5"/><circle cx="8.5" cy="14.2" r=".7"/><circle cx="15.5" cy="14.2" r=".7"/></svg>',
  },
  walking: {
    label: "Walk",
    noun: "Walking",
    color: "#2d8a57",
    icon: '<svg viewBox="0 0 24 24"><circle cx="13" cy="4.5" r="1.8"/><path d="M10 21l1.8-6.2-2.3-2.3.9-4.3 3 .9 1.6 3 2.4.9M11.8 14.8l2.9 2.7.8 3.5M10.4 8.2L7.4 10v3"/></svg>',
  },
};

export function modeName(mode) {
  return mode === "running_cycling"
    ? "Running / cycling"
    : modeInfo[mode]?.noun || (mode ? mode[0].toUpperCase() + mode.slice(1) : "Unknown");
}

export function modeIcon(mode) {
  const info = modeInfo[mode];
  return info
    ? `<span class="mode-icon" style="--mode:${info.color}" aria-hidden="true">${info.icon}</span>`
    : "";
}

// Saved places are zones; a place inside one names it unless it is the zone itself.
export function zoneText(zone, name) {
  return zone && zone !== name ? "In " + zone : "";
}

export function placeKind(place = {}) {
  return kindOf(place);
}

// A place's kind as a tinted icon, for lists.
export function placeIcon(place) {
  const info = KIND_INFO[kindOf(place)];
  return `<span class="place-icon" style="--kind:${info.color}" title="${safe(info.label)}" aria-hidden="true"><svg viewBox="0 0 24 24">${info.icon}</svg></span>`;
}

// A stop on the day's map: the number you reached it in (or every number of a revisited place,
// "1·3"), in the colour of its kind. Home has no number, only its house.
export function stopPin(place, label) {
  if (!label) return `<span class="stop-pin home">${placeIcon({ name: "Home" })}</span>`;
  const info = KIND_INFO[kindOf(place)];
  const many = String(label).length > 2 ? " many" : "";
  return `<span class="stop-pin${many}" style="--kind:${info.color}" title="${safe(info.label)}">${label}</span>`;
}

// Each trip of a day gets its own colour, in the order they happened, so one reads apart from the
// next on the map and in the timeline; its mode is still told by its icon.
export const TRIP_COLORS = ["#3567ad", "#d0661f", "#7b4fb8", "#1d8a82", "#c23f78", "#5b8f2a", "#b04a32", "#a8841a"];

export function tripColors(day) {
  const trips = [...(day?.trips || [])].sort((a, b) => a.start.localeCompare(b.start));
  return new Map(trips.map((trip, index) => [`${trip.from_stay_id}-${trip.to_stay_id}`, TRIP_COLORS[index % TRIP_COLORS.length]]));
}

// Stops numbered in the order you reached them: each stay's number, and every number of
// each place, so a return visit is the next number, not the place's first one again.
export function visitNumbers(day) {
  const visited = new Set((day?.visited_places || []).map((place) => place.cluster_id));
  const byStay = new Map(),
    byPlace = new Map();
  let next = 1;
  for (const stay of [...(day?.stays || [])].sort((a, b) => a.started_at.localeCompare(b.started_at))) {
    if (!visited.has(stay.cluster_id)) continue; // Home
    byStay.set(stay.id, next);
    byPlace.set(stay.cluster_id, [...(byPlace.get(stay.cluster_id) || []), next]);
    next += 1;
  }
  return { byStay, byPlace: new Map([...byPlace].map(([place, numbers]) => [place, numbers.join("·")])) };
}

export const summaryIcons = {
  distance:
    '<svg viewBox="0 0 24 24"><path d="M6 19a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM18 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"/><path d="M8.5 14.5c2.5-1 2.5-4 1-5.5S10 5 15 7"/></svg>',
  time: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/></svg>',
  trips:
    '<svg viewBox="0 0 24 24"><path d="M5 11 6.6 7a2 2 0 0 1 1.9-1.3h7A2 2 0 0 1 17.4 7l1.6 4"/><path d="M4 11h16v6H4zM7 17v2M17 17v2"/></svg>',
  places:
    '<svg viewBox="0 0 24 24"><path d="M19 10c0 4.5-7 10-7 10S5 14.5 5 10a7 7 0 1 1 14 0Z"/><circle cx="12" cy="10" r="2.2"/></svg>',
  samples: '<svg viewBox="0 0 24 24"><path d="M4 17h3l2-5 3 3 3-8 2 5h3"/></svg>',
  reminders:
    '<svg viewBox="0 0 24 24"><path d="M6 16h12l-1.5-2.5V10a4.5 4.5 0 0 0-9 0v3.5L6 16Z"/><path d="M10 19h4"/></svg>',
};

export const SOURCE_KINDS = {
  owntracks: "OwnTracks",
  overland: "Overland",
  osmand: "Traccar / OsmAnd",
  generic: "Generic",
  home_assistant: "Home Assistant",
  manual: "Manual",
  import: "Import",
  takeout: "Google Takeout",
};

export function sourceName(source) {
  const [kind, ...rest] = String(source || "").split(":");
  return SOURCE_KINDS[kind]
    ? { kind: SOURCE_KINDS[kind], device: rest.join(":") }
    : { kind: source, device: "" };
}

export function statusPill(tone, label) {
  return `<span class="status-pill ${tone}">${safe(label)}</span>`;
}
