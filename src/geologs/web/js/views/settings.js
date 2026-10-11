// Settings: sources and tracker setup, time and speed, stays and activities, place names, summaries, and access.
import { refreshMeta } from "../app/refresh.js";
import { api, openAuth, post, setConnection } from "../core/api.js";
import { $, copyText, safe, toast } from "../core/dom.js";
import { ago, formatTime, localDateKey, plural, zoneOptions } from "../core/format.js";
import { state } from "../core/state.js";
import { actionIcon, onEach } from "../ui/actions.js";
import { modeIcon, modeInfo, sourceName, statusPill } from "../ui/icons.js";
import { refreshCurrent, refreshExplore } from "./history.js";
import { refreshInsights } from "./patterns.js";
import { showView } from "./shell.js";

export function populateTimezones(selected) {
  const select = $("summary-timezone");
  let zones = [];
  try {
    zones = Intl.supportedValuesOf("timeZone");
  } catch (_error) {
    zones = [
      "America/Chicago",
      "America/Denver",
      "America/Los_Angeles",
      "America/New_York",
      "America/Phoenix",
      "America/Toronto",
      "America/Vancouver",
      "Europe/London",
      "Europe/Paris",
      "Asia/Kolkata",
      "Asia/Tokyo",
      "Australia/Sydney",
    ];
  }
  zones = [...new Set(["UTC", ...zones, selected].filter(Boolean))];
  select.innerHTML = "";
  const groups = new Map();
  for (const zone of zones) {
    const area = zone.includes("/") ? zone.split("/")[0] : "General";
    if (!groups.has(area)) groups.set(area, []);
    groups.get(area).push(zone);
  }
  for (const [area, items] of groups) {
    const group = document.createElement("optgroup");
    group.label = area;
    for (const zone of items.sort()) {
      const option = document.createElement("option");
      option.value = zone;
      option.textContent = zone.replaceAll("_", " ");
      group.appendChild(option);
    }
    select.appendChild(group);
  }
  select.value = selected || "UTC";
}

// Matches ANALYSIS_DEFAULTS in core.py.
const STAY_DEFAULTS = { stay_min_minutes: 15, quick_stop_minutes: 3 };
const SPEED_DEFAULTS = { walking_max_kmh: 6.5, running_max_kmh: 15, cycling_max_kmh: 25, driving_min_kmh: 35 };
// Each mode's input, and the setting it edits.
const SPEED_FIELDS = [
  ["walking", "walking_max_kmh"],
  ["running", "running_max_kmh"],
  ["cycling", "cycling_max_kmh"],
  ["driving", "driving_min_kmh"],
];

function fillStays(values) {
  $("stay-min").value = values.stay_min_minutes;
  // A value saved outside the list (say 6 min) still shows as itself.
  const quick = String(values.quick_stop_minutes ?? 0);
  if (![...$("quick-stop").options].some((option) => option.value === quick))
    $("quick-stop").add(new Option(`${quick} min or more`, quick));
  $("quick-stop").value = quick;
  readStays();
}

function fillSpeeds(values) {
  for (const [mode, key] of SPEED_FIELDS) $(`speed-${mode}`).value = values[key];
  readSpeeds();
}

function showError(id, error, invalid, fields) {
  for (const field of fields) $(field).setAttribute("aria-invalid", invalid.has(field) ? "true" : "false");
  $(id).textContent = error;
  $(id).hidden = !error;
}

// Reads the stays form and returns its values, or an error it also shows.
function readStays() {
  const stay = +$("stay-min").value;
  const quick = +$("quick-stop").value;
  const invalid = new Set();
  let error = "";
  if (!Number.isInteger(stay) || stay < 5 || stay > 120) {
    invalid.add("stay-min");
    error = "Minimum stay must be a whole number of minutes from 5 to 120.";
  } else if (quick && quick >= stay) {
    invalid.add("quick-stop");
    error = "Quick stops must be shorter than the minimum stay.";
  }
  showError("stays-error", error, invalid, ["stay-min", "quick-stop"]);
  return error ? { error } : { stay_min_minutes: stay, quick_stop_minutes: quick };
}

function speedText(kmh) {
  return Number.isFinite(kmh) ? kmh.toLocaleString(undefined, { maximumFractionDigits: 1 }) : "…";
}

// Reads the speeds, shows each mode's range and the speed bar, and returns the values or an error.
function readSpeeds() {
  const speeds = Object.fromEntries(SPEED_FIELDS.map(([mode, key]) => [key, parseFloat($(`speed-${mode}`).value)]));
  const { walking_max_kmh: walk, running_max_kmh: run, cycling_max_kmh: ride, driving_min_kmh: drive } = speeds;
  $("speed-running-from").textContent = `Above ${speedText(walk)}, up to`;
  $("speed-cycling-from").textContent = `Above ${speedText(run)}, up to`;
  $("speed-driving-from").textContent = `Above ${speedText(ride)}, or stretches from`;
  const fields = SPEED_FIELDS.map(([mode]) => `speed-${mode}`);
  const invalid = new Set(fields.filter((id, index) => {
    const value = speeds[SPEED_FIELDS[index][1]];
    return !Number.isFinite(value) || value < 1 || value > 200;
  }));
  let error = invalid.size ? "Speeds must be between 1 and 200 km/h." : "";
  if (!error) {
    // The modes must rise in order; flag the first one that doesn't.
    const broken = [
      ["speed-running", walk < run, "Running must start above the walking speed."],
      ["speed-cycling", run < ride, "Cycling must start above the running speed."],
      ["speed-driving", ride <= drive, "Driving can't start below the cycling limit."],
    ].find(([, ok]) => !ok);
    if (broken) {
      invalid.add(broken[0]);
      error = broken[2];
    }
  }
  showError("speed-error", error, invalid, fields);
  if (!error) {
    // Driving gets a share past its own speed so it never shrinks to nothing.
    const widths = [walk, run - walk, ride - run, Math.max(drive, ride) * 1.25 - ride];
    $("speed-bands").innerHTML = SPEED_FIELDS.map(
      ([mode], index) => `<span style="--mode:${modeInfo[mode].color};flex-grow:${widths[index]}"></span>`,
    ).join("");
  }
  return error ? { error } : speeds;
}

// Saves analysis settings; when they changed, stays and trips are recalculated before the reply.
async function saveAnalysis(form, values, extra = {}) {
  const button = form.querySelector(".button.primary");
  const changed = Object.keys(values).some((key) => values[key] !== state.settings[key]);
  button.disabled = true;
  button.textContent = changed ? "Recalculating…" : "Saving…";
  try {
    state.settings = await post("/api/settings", { ...values, ...extra });
    renderSettings();
    if (changed) await Promise.all([refreshExplore(false), refreshInsights()]);
    toast(changed ? "Saved. Stays and trips were recalculated." : "Settings saved");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Save changes";
  }
}

const SETTINGS_PAGE_KEY = "geologs-settings-page";

// Settings is a set of pages; the one last opened comes back for this browser.
export function showSettingsPage(id) {
  const pages = [...document.querySelectorAll(".settings-section")];
  if (!pages.some((page) => page.id === id)) id = pages[0].id;
  for (const page of pages) page.hidden = page.id !== id;
  document.querySelectorAll(".settings-tab").forEach((tab) => {
    const active = tab.dataset.settingsPage === id;
    tab.classList.toggle("active", active);
    if (active) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  });
  try {
    sessionStorage.setItem(SETTINGS_PAGE_KEY, id);
  } catch (_error) {}
  document.querySelector(".settings-tab.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function renderSummarySchedule() {
  const schedule = state.settings.summary_schedule || "nightly";
  document.querySelectorAll('input[name="summary-schedule"]').forEach((input) => {
    input.checked = input.value === schedule;
  });
  $("summary-interval").value = String(state.settings.summary_interval_hours || 3);
}

// Whether something is configured: plain text with a tick, so it never reads as a button.
function configState(ready, text, title = "") {
  const icon = ready
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>'
    : '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 8v4.5M12 16h.01"/></svg>';
  return `<span class="config-state${ready ? " ready" : ""}"${title ? ` title="${safe(title)}"` : ""}>${icon}${safe(text)}</span>`;
}

const PROVIDER_NOTES = {
  geoapify: "Free plan with 3,000 lookups a day.",
  mapbox: "Generous free tier. Its terms allow names for temporary use only.",
  photon: "Your own OpenStreetMap server: private and unlimited.",
};

export function renderPlacesProvider() {
  const providers = state.status?.places_providers || [];
  const chosen = state.settings?.places_provider || "";
  const active = providers.find((provider) => provider.id === state.status?.geocoder);
  const options = [
    {
      id: "",
      label: "Automatic",
      note: active
        ? `Uses the first one that's set up: ${active.label} right now.`
        : "Uses the first provider that's set up.",
      configured: true,
    },
    ...providers.map((provider) => ({ ...provider, note: PROVIDER_NOTES[provider.id] || "" })),
  ];
  $("places-provider-options").innerHTML = options
    .map((option) => {
      const available = option.configured || option.id === chosen;
      const pill = option.id
        ? option.configured
          ? configState(true, "Key added")
          : configState(false, `Needs ${option.env}`, `Add ${option.env} to .env and restart`)
        : "";
      return `<label class="choice-card${available ? "" : " unavailable"}"><input type="radio" name="places-provider" value="${safe(option.id)}"${option.id === chosen ? " checked" : ""}${available ? "" : " disabled"}><span class="choice-body"><strong>${safe(option.label)}</strong><small>${safe(option.note)}</small></span>${pill}</label>`;
    })
    .join("");
  $("places-provider-help").textContent = providers.some((provider) => provider.configured)
    ? "Turns coordinates into place names for stays, places, and search."
    : "No provider is set up yet. Add an API key to .env and restart the server; until then places are named by address or not at all.";
  document
    .querySelectorAll('input[name="places-provider"]')
    .forEach((input) => (input.onchange = updateProviderNotice));
  updateProviderNotice();
}

function chosenProvider() {
  return document.querySelector('input[name="places-provider"]:checked')?.value ?? "";
}

export function updateProviderNotice() {
  // Automatic resolves to whatever the server chose, so check that too.
  const value = chosenProvider() || state.status?.geocoder;
  $("mapbox-terms").hidden = value !== "mapbox";
}

// Where each kind of tracker app sends locations, how it signs in, and how to set it up.
export const TRACKERS = [
  {
    kind: "owntracks",
    name: "OwnTracks",
    platform: "iOS and Android",
    path: "/api/ingest/owntracks",
    steps: [
      "In OwnTracks, open Preferences → Connection and set Mode to HTTP.",
      "Set Host (URL) to the address above.",
      "Under Identification, enter any username, your phone token as the password, and a device name. The device name labels this source.",
    ],
  },
  {
    kind: "overland",
    name: "Overland",
    platform: "iOS",
    path: "/api/ingest/overland?token=YOUR_PHONE_TOKEN",
    steps: [
      "In Overland settings, set Receiver Endpoint to the address above, with your phone token in place of YOUR_PHONE_TOKEN.",
      "Overland sends batches and clears them only once GeoLogs confirms, so nothing is lost offline.",
    ],
  },
  {
    kind: "osmand",
    name: "Traccar Client or OsmAnd",
    platform: "iOS and Android",
    path: "/api/ingest/osmand?token=YOUR_PHONE_TOKEN",
    steps: [
      "Set the server URL to the address above, with your phone token.",
      "The device identifier labels this source.",
    ],
  },
  {
    kind: "generic",
    name: "GPSLogger, Tasker, Shortcuts",
    platform: "Anything that can send a web request",
    path: "/api/ingest/generic?lat=%LAT&lon=%LON&timestamp=%TIMESTAMP&accuracy=%ACC&speed_mps=%SPD&device=phone&token=YOUR_PHONE_TOKEN",
    steps: [
      "Send lat, lon, timestamp, accuracy, speed_mps (or speed_kmh), and device as a query string, a form, or JSON.",
      "The example address uses GPSLogger’s placeholders.",
    ],
  },
  {
    kind: "home_assistant",
    name: "Home Assistant",
    platform: "Sends each position the moment it changes",
    path: "/api/ingest/generic",
    setup: homeAssistantSetup,
  },
];

const HA_BLUEPRINT_PATH = "/ha/geologs-location.yaml";

// One REST command and a blueprint: no polling, and positions arrive as Home Assistant sees them.
function homeAssistantSetup() {
  const origin = location.origin;
  const command = `rest_command:
  geologs_location:
    url: "${origin}/api/ingest/generic"
    method: post
    headers:
      authorization: !secret geologs_authorization
    content_type: application/json
    payload: >-
      {"source": "{{ source }}", "timestamp": "{{ timestamp }}", "external_id": "{{ timestamp }}",
       "lat": {{ lat }}, "lon": {{ lon }}, "accuracy": {{ accuracy }}}`;
  const secret = 'geologs_authorization: "Bearer YOUR_PHONE_TOKEN"';
  const importUrl =
    "https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=" + encodeURIComponent(origin + HA_BLUEPRINT_PATH);
  const block = (label, text) =>
    `<div class="code-block"><pre class="mono" aria-label="${safe(label)}">${safe(text)}</pre><button type="button" class="button secondary small" data-copy-text="${safe(text)}">Copy</button></div>`;
  return `<ol class="steps rich">
    <li><span>Add this to <code>configuration.yaml</code>:</span>${block("REST command for configuration.yaml", command)}</li>
    <li><span>Add your phone token to <code>secrets.yaml</code>:</span>${block("Line for secrets.yaml", secret)}</li>
    <li><span>Restart Home Assistant, or reload <strong>REST commands</strong> under Developer tools → YAML.</span></li>
    <li><span>Import the automation, then choose <strong>Create automation</strong> and pick your trackers:</span>
      <a class="button primary small ha-import" href="${safe(importUrl)}" target="_blank" rel="noopener">Import into Home Assistant</a></li>
  </ol>
  <p class="help">Home Assistant fetches the blueprint from <a href="${safe(HA_BLUEPRINT_PATH)}" target="_blank" rel="noopener">${safe(origin + HA_BLUEPRINT_PATH)}</a>, so it must be able to reach this address; if it can't, download that file and import it by hand. Positions arrive under the same names as <code>./geologs poll-ha</code>, so history continues: stop that cron job once this works.</p>`;
}

function failing(device) {
  return (
    device.last_status >= 400 && (!device.last_success_at || device.last_attempt_at > device.last_success_at)
  );
}

function deviceLabel(device) {
  if (!device.device) return "Unidentified device";
  const name = sourceName(device.device);
  return name.device || name.kind;
}

// One status for an app across its devices: a single rejected phone is worth calling out.
export function trackerStatus(devices) {
  if (!devices.length) return { tone: "neutral", label: "Not connected" };
  const rejected = devices.filter(failing);
  const recent = devices.filter(
    (device) =>
      !failing(device) &&
      device.last_success_at &&
      Date.now() - Date.parse(device.last_success_at) < 6 * 3600e3,
  );
  if (rejected.length && recent.length)
    return { tone: "error", label: `${plural(rejected.length, "device")} failing` };
  if (rejected.length) return { tone: "error", label: "Failing" };
  if (recent.length) return { tone: "success", label: "Receiving" };
  return { tone: "warning", label: "Quiet" };
}

function deviceRows(devices) {
  if (!devices.length)
    return '<p class="tracker-status">Nothing has reached GeoLogs from this app yet. Once it sends a location, it shows up here.</p>';
  return `<ul class="device-list">${devices
    .map((device) => {
      const rejected = failing(device);
      const quiet = !rejected && Date.now() - Date.parse(device.last_success_at) > 6 * 3600e3;
      const tone = rejected ? "error" : quiet ? "warning" : "success";
      const detail = rejected
        ? `Rejected ${ago(device.last_attempt_at)}: ${device.last_message || "error " + device.last_status}`
        : [
            `Last data ${ago(device.last_success_at)}`,
            plural(device.received || 0, "fix", "fixes") + " received",
            device.last_message || "",
          ]
            .filter(Boolean)
            .join(" · ");
      return `<li><span class="health-dot ${tone}" aria-hidden="true"></span><span><strong>${safe(deviceLabel(device))}</strong><small title="${safe(formatTime(device.last_attempt_at))}">${safe(detail)}</small></span><button type="button" class="icon-button" data-forget-device="${safe(JSON.stringify([device.kind, device.device]))}" aria-label="Forget ${safe(deviceLabel(device))}" title="Forget this device">${actionIcon("cancel")}</button></li>`;
    })
    .join("")}</ul>`;
}

// Trackers most trusted first: the ones you ranked, then any new ones in the order Sources lists them.
function priorityOrder() {
  const known = (state.sources?.sources || []).map((item) => item.source);
  const ranked = (state.settings?.source_priority || []).filter((source) => known.includes(source));
  return [...ranked, ...known.filter((source) => !ranked.includes(source))];
}

function trackerLabel(source) {
  const name = sourceName(source);
  return name.device ? `${name.kind} · ${name.device}` : name.kind;
}

const CHEVRON = (direction) =>
  `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${direction < 0 ? "m6 15 6-6 6 6" : "m6 9 6 6 6-6"}"/></svg>`;

// Health follows the newest fix itself: an old backfill that arrived a minute ago is not live.
function sourceHealth(item) {
  const age = Date.now() - Date.parse(item.last_fix);
  return age < 3600e3 ? ["success", "Live"] : age < 86400e3 ? ["warning", "Quiet"] : ["neutral", "Idle"];
}

function sourceDetails(item) {
  const lag = (Date.parse(item.last_received) - Date.parse(item.last_fix)) / 60000;
  return [
    `received ${ago(item.last_received)}`,
    lag > 15 ? `newest fix from ${formatTime(item.last_fix)}` : "",
    `${Number(item.fixes_24h).toLocaleString()} in the last 24 h`,
    `${Number(item.fixes).toLocaleString()} since ${new Date(item.first_fix).toLocaleDateString(undefined, { month: "short", year: "numeric", ...zoneOptions() })}`,
  ].filter(Boolean);
}

// Every tracker, most trusted first, with its health; reorder by dragging or the arrows.
export function renderPriority(focusSource, focusMove) {
  const list = $("priority-list");
  if (list.querySelector(".dragging")) return; // the 30 s Sources refresh must not drop a drag in progress
  const order = priorityOrder();
  const bySource = new Map((state.sources?.sources || []).map((item) => [item.source, item]));
  const several = order.length > 1;
  list.hidden = !order.length;
  list.classList.toggle("single", !several);
  $("priority-note").hidden = !!order.length;
  $("priority-help").textContent = several
    ? "Most trusted first. When trackers disagree about the same moment, the higher one wins; the others only fill times it has nothing to say. Drag or use the arrows to reorder."
    : "Once a second tracker sends locations, you can choose which one wins when they disagree.";
  $("priority-help").hidden = !order.length;
  list.innerHTML = order
    .map((source, index) => {
      const name = sourceName(source),
        label = trackerLabel(source),
        item = bySource.get(source);
      const [tone, health] = sourceHealth(item);
      const moves = several
        ? `<button type="button" class="icon-button" data-move="-1" aria-label="Move ${safe(label)} up"${index === 0 ? " disabled" : ""}>${CHEVRON(-1)}</button><button type="button" class="icon-button" data-move="1" aria-label="Move ${safe(label)} down"${index === order.length - 1 ? " disabled" : ""}>${CHEVRON(1)}</button>`
        : "";
      return `<li class="priority-item" draggable="${several}" data-source="${safe(source)}">${several ? `<span class="drag-handle" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="9" cy="6" r="1.3"/><circle cx="15" cy="6" r="1.3"/><circle cx="9" cy="12" r="1.3"/><circle cx="15" cy="12" r="1.3"/><circle cx="9" cy="18" r="1.3"/><circle cx="15" cy="18" r="1.3"/></svg></span><span class="priority-rank" title="${index === 0 ? "Preferred when trackers disagree" : ""}">${index + 1}</span>` : ""}<span class="health-dot ${tone}" title="${health}" aria-hidden="true"></span><span class="priority-name" title="${safe(source)}"><strong>${safe(name.kind)}${name.device ? ` <span class="muted">· ${safe(name.device)}</span>` : ""}</strong><small>${safe([health, ...sourceDetails(item)].join(" · "))}</small></span><span class="priority-moves">${moves}<button type="button" class="icon-button danger" data-remove-source aria-label="Remove ${safe(label)} and its data" title="Remove this tracker">${actionIcon("delete")}</button></span></li>`;
    })
    .join("");
  list.querySelectorAll("[data-move]").forEach((button) => {
    button.onclick = () => {
      const source = button.closest("li").dataset.source;
      const next = [...order];
      const from = next.indexOf(source),
        to = from + +button.dataset.move;
      next.splice(to, 0, next.splice(from, 1)[0]);
      savePriority(next, source, button.dataset.move);
    };
  });
  list.querySelectorAll("[data-remove-source]").forEach((button) => {
    button.onclick = () => removeTracker(button.closest("li").dataset.source);
  });
  // Keep focus on the tracker you just moved, so arrowing it along works from the keyboard.
  if (focusSource) {
    const item = [...list.children].find((li) => li.dataset.source === focusSource);
    const button = item?.querySelector(`[data-move="${focusMove}"]:not([disabled])`) || item?.querySelector("[data-move]:not([disabled])");
    button?.focus();
  }
}

// Removing a tracker deletes its history, so say exactly what goes before doing it.
async function removeTracker(source) {
  let preview;
  try {
    preview = await post("/api/sources/delete", { source });
  } catch (error) {
    toast(error.message);
    return;
  }
  const label = trackerLabel(source);
  const span = `${new Date(preview.first).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric", ...zoneOptions() })} – ${new Date(preview.last).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric", ...zoneOptions() })}`;
  $("delete-samples-title").textContent = `Remove ${label}?`;
  $("delete-samples-summary").textContent = `${plural(preview.samples, "sample")} · ${span}`;
  $("delete-samples-effects").innerHTML = [
    `<strong>This can't be undone.</strong> All of this tracker's samples are deleted, with the stays, trips, and corrections made from them.`,
    preview.stored_summaries
      ? `${plural(preview.stored_summaries, "saved daily summary", "saved daily summaries")} are rebuilt from your other trackers.`
      : "No saved daily summaries depend on it.",
    "If the device sends again, it comes back as a new tracker. To stop it, turn it off in its app.",
  ]
    .map((line) => `<li>${line}</li>`)
    .join("");
  const triggers = preview.reminder_triggers;
  $("delete-samples-reminders").hidden = !triggers.length;
  $("delete-samples-reminders").innerHTML = triggers.length
    ? `<strong>It set off ${plural(triggers.length, "reminder")}</strong><span>Those triggers are removed from the reminder inbox; the reminders themselves are unchanged.</span>`
    : "";
  const button = $("confirm-delete-samples");
  button.textContent = `Remove tracker`;
  button.disabled = false;
  button.onclick = async () => {
    button.disabled = true;
    button.textContent = "Removing…";
    try {
      await post("/api/sources/delete", { source, confirm: true });
      $("delete-samples-dialog").close();
      state.settings = await api("/api/settings");
      await refreshSources();
      await Promise.all([refreshExplore(false), refreshInsights(), refreshCurrent()]);
      toast(`Removed ${label}`);
    } catch (error) {
      button.disabled = false;
      button.textContent = "Try again";
      toast(error.message);
    }
  };
  $("delete-samples-dialog").showModal();
}

async function savePriority(order, focusSource, focusMove) {
  // Trackers that sent nothing lately keep their place at the end of the list.
  const unseen = (state.settings.source_priority || []).filter((source) => !order.includes(source));
  try {
    state.settings = await post("/api/settings", { source_priority: [...order, ...unseen] });
    renderPriority(focusSource, focusMove);
    toast(`${trackerLabel(order[0])} is preferred when trackers disagree`);
    await Promise.all([refreshExplore(false), refreshInsights(), refreshCurrent()]);
  } catch (error) {
    toast(error.message);
    renderPriority();
  }
}

function initPriorityDrag() {
  const list = $("priority-list");
  let dragged = null;
  list.addEventListener("dragstart", (event) => {
    dragged = event.target.closest(".priority-item");
    if (!dragged) return;
    dragged.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", dragged.dataset.source);
  });
  list.addEventListener("dragover", (event) => {
    if (!dragged) return;
    event.preventDefault();
    const over = event.target.closest(".priority-item");
    if (!over || over === dragged) return;
    const box = over.getBoundingClientRect();
    over.parentNode.insertBefore(dragged, event.clientY > box.top + box.height / 2 ? over.nextSibling : over);
  });
  list.addEventListener("drop", (event) => event.preventDefault());
  list.addEventListener("dragend", () => {
    if (!dragged) return;
    dragged.classList.remove("dragging");
    dragged = null;
    const order = [...list.children].map((item) => item.dataset.source);
    if (order.join("\n") !== priorityOrder().join("\n")) savePriority(order);
    else renderPriority();
  });
}

export function renderSources() {
  const data = state.sources;
  if (!data) return;
  const sources = data.sources || [];
  const newest = sources[0];
  $("sources-summary").textContent = sources.length
    ? `${plural(sources.length, "source")} · last location received ${ago(newest.last_received)}`
    : "No locations received yet. Connect an app below.";
  const byKind = new Map();
  for (const device of data.devices || []) {
    // Home Assistant sends through the generic endpoint (the blueprint) or the API (the poller).
    const kind = device.kind === "api" || String(device.device || "").startsWith("home_assistant:") ? "home_assistant" : device.kind;
    if (!byKind.has(kind)) byKind.set(kind, []);
    byKind.get(kind).push(device);
  }
  $("ingest-token-note").hidden = !!data.ingest_token;
  $("access-ingest-badge").innerHTML = configState(!!data.ingest_token, data.ingest_token ? "Set" : "Not set");
  const open = new Set(
    [...document.querySelectorAll("#tracker-endpoints details[open]")].map((item) => item.dataset.kind),
  );
  $("tracker-endpoints").innerHTML = TRACKERS.map((tracker) => {
    const devices = byKind.get(tracker.kind) || [];
    const status = trackerStatus(devices);
    const url = location.origin + tracker.path;
    return `<details class="tracker" data-kind="${tracker.kind}"${open.has(tracker.kind) ? " open" : ""}><summary><span class="tracker-title"><strong>${safe(tracker.name)}</strong><small>${safe(tracker.platform)}</small></span>${statusPill(status.tone, status.label)}<svg class="chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></summary>
      <div class="tracker-body">${deviceRows(devices)}<div class="tracker-setup"><h4>Set up</h4>${
        tracker.setup
          ? tracker.setup()
          : `<div class="copy-field"><input class="input mono" readonly value="${safe(url)}" aria-label="${safe(tracker.name)} address"><button type="button" class="button secondary small" data-copy-endpoint="${safe(url)}">Copy</button></div><ol class="steps">${tracker.steps.map((step) => `<li>${safe(step)}</li>`).join("")}</ol>`
      }</div></div></details>`;
  }).join("");
  onEach($("tracker-endpoints"), "copy-endpoint", (url) => copyText(url, "Address copied"));
  onEach($("tracker-endpoints"), "copy-text", (text) => copyText(text, "Copied"));
  onEach($("tracker-endpoints"), "forget-device", async (value) => {
    const [kind, device] = JSON.parse(value);
    try {
      await post("/api/ingest-devices/delete", { kind, device });
      await refreshSources();
      toast("Device forgotten. It comes back if it sends again.");
    } catch (error) {
      toast(error.message);
    }
  });
  renderPriority();
}

export async function refreshSources() {
  state.sources = await api("/api/sources");
  renderSources();
}

export function renderSettings() {
  if (!state.settings) return;
  renderPlacesProvider();
  populateTimezones(state.settings.timezone);
  $("speed-strategy").value = state.settings.speed_strategy;
  $("speed-offset").value = state.settings.reported_speed_offset_kmh;
  updateSpeedSettingsVisibility();
  fillStays(state.settings);
  fillSpeeds(state.settings);
  renderSummarySchedule();
}

export function updateSpeedSettingsVisibility() {
  const reported = $("speed-strategy").value === "reported";
  $("speed-offset").disabled = !reported;
  $("speed-offset-field").classList.toggle("disabled-field", !reported);
}

export function initSettings() {
  initPriorityDrag();
  for (const [mode] of SPEED_FIELDS)
    $(`speed-${mode}-name`).innerHTML = `${modeIcon(mode)}${safe(modeInfo[mode].noun)}`;
  document.querySelectorAll(".settings-tab").forEach((tab) => (tab.onclick = () => showSettingsPage(tab.dataset.settingsPage)));
  let page = "";
  try {
    page = sessionStorage.getItem(SETTINGS_PAGE_KEY) || "";
  } catch (_error) {}
  showSettingsPage(page);
  $("stays-form").oninput = readStays;
  $("stays-defaults").onclick = () => fillStays(STAY_DEFAULTS);
  $("stays-form").onsubmit = (event) => {
    event.preventDefault();
    const values = readStays();
    if (!values.error) saveAnalysis($("stays-form"), values);
  };
  $("movement-form").oninput = readSpeeds;
  $("speed-strategy").onchange = updateSpeedSettingsVisibility;
  $("movement-defaults").onclick = () => {
    fillSpeeds(SPEED_DEFAULTS);
    $("speed-strategy").value = "reported";
    $("speed-offset").value = 0;
    updateSpeedSettingsVisibility();
  };
  $("movement-form").onsubmit = (event) => {
    event.preventDefault();
    const values = readSpeeds();
    if (values.error) return;
    // Speed source and adjustment apply to new samples only, so they never recalculate.
    saveAnalysis($("movement-form"), values, {
      speed_strategy: $("speed-strategy").value,
      reported_speed_offset_kmh: +$("speed-offset").value,
    });
  };
  // Choosing an interval implies saving throughout the day.
  $("summary-interval").onchange = () => {
    document.querySelector('input[name="summary-schedule"][value="interval"]').checked = true;
  };
  $("summary-form").onsubmit = async (event) => {
    event.preventDefault();
    const timezoneChanged = $("summary-timezone").value !== state.settings.timezone;
    try {
      state.settings = await post("/api/settings", {
        timezone: $("summary-timezone").value.trim(),
        summary_schedule: document.querySelector('input[name="summary-schedule"]:checked')?.value || "nightly",
        summary_interval_hours: +$("summary-interval").value,
      });
      renderSettings();
      if (timezoneChanged) {
        if (state.followToday) $("day-date").value = localDateKey();
        await refreshExplore();
      }
      toast("Settings saved");
    } catch (error) {
      toast(error.message);
    }
  };
  $("connection").onclick = () => {
    showView("settings");
    showSettingsPage("settings-sources");
  };
  $("refresh-sources").onclick = () =>
    refreshSources()
      .then(() => toast("Sources updated"))
      .catch((error) => toast(error.message));
  $("sign-out").onclick = async () => {
    try {
      await fetch("/api/session", { method: "DELETE", credentials: "same-origin" });
    } catch (_error) {}
    sessionStorage.removeItem("geologs-token");
    state.authDismissed = false;
    setConnection("warning", "Signed out");
    openAuth("You are signed out. Enter the access token to sign in again.");
  };
  setInterval(() => {
    if ($("settings-view").classList.contains("active") && !document.hidden) refreshSources().catch(() => {});
  }, 30000);
  $("places-form").onsubmit = async (event) => {
    event.preventDefault();
    try {
      state.settings = await post("/api/settings", { places_provider: chosenProvider() });
      state.status = await api("/api/status");
      renderSettings();
      toast("Place names provider saved");
      // Names can change with the provider; refresh what shows them.
      await refreshMeta();
      await Promise.all([refreshExplore(false), refreshInsights()]);
    } catch (error) {
      toast(error.message);
    }
  };
}
