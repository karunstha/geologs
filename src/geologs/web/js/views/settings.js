// Settings: sources and tracker setup, time and speed, stays and activities, place names, summaries, and access.
import { refreshMeta } from "../app/refresh.js";
import { api, openAuth, post, setConnection } from "../core/api.js";
import { $, copyText, safe, toast } from "../core/dom.js";
import {
  ago,
  distanceText,
  economyUnit,
  formatTime,
  fromEconomyUnit,
  fromPerVolume,
  fromSpeedUnit,
  lengthUnit,
  localDateKey,
  moneyText,
  plural,
  speedUnit,
  toEconomyUnit,
  toLengthUnit,
  toPerVolume,
  toSpeedUnit,
  toVolumeUnit,
  volumeUnit,
  zoneOptions,
} from "../core/format.js";
import { refreshAll } from "../app/refresh.js";
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
  for (const [mode, key] of SPEED_FIELDS) $(`speed-${mode}`).value = round1(toSpeedUnit(values[key]));
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

function speedNumber(value) {
  return Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 1 }) : "…";
}

const round1 = (value) => Math.round(value * 10) / 10;

// A speed field shown in your units, read back in km/h. Untouched, it keeps the stored
// value exactly, so saving never nudges 6.5 km/h to 6.4 through 4.0 mph.
function speedFromField(id, stored) {
  const shown = parseFloat($(id).value);
  if (!Number.isFinite(shown)) return NaN;
  return stored != null && round1(toSpeedUnit(stored)) === shown ? stored : round1(fromSpeedUnit(shown));
}

// Reads the speeds, shows each mode's range and the speed bar, and returns the values or an error.
function readSpeeds() {
  const speeds = Object.fromEntries(
    SPEED_FIELDS.map(([mode, key]) => [key, speedFromField(`speed-${mode}`, state.settings?.[key])]),
  );
  const { walking_max_kmh: walk, running_max_kmh: run, cycling_max_kmh: ride, driving_min_kmh: drive } = speeds;
  const shown = (kmhValue) => speedNumber(round1(toSpeedUnit(kmhValue)));
  $("speed-running-from").textContent = `Above ${shown(walk)}, up to`;
  $("speed-cycling-from").textContent = `Above ${shown(run)}, up to`;
  $("speed-driving-from").textContent = `Above ${shown(ride)}, or stretches from`;
  const fields = SPEED_FIELDS.map(([mode]) => `speed-${mode}`);
  const invalid = new Set(fields.filter((id, index) => {
    const value = speeds[SPEED_FIELDS[index][1]];
    return !Number.isFinite(value) || value < 1 || value > 200;
  }));
  let error = invalid.size ? `Speeds must be between ${shown(1)} and ${shown(200)} ${speedUnit()}.` : "";
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
  if (id === "settings-car" && state.settings) refreshFuel().catch((error) => toast(error.message));
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
      "Under Identification, enter any username, your device token as the password, and a device name. The device name labels this source.",
    ],
  },
  {
    kind: "overland",
    name: "Overland",
    platform: "iOS",
    path: "/api/ingest/overland?token=YOUR_PHONE_TOKEN",
    steps: [
      "In Overland settings, set Receiver Endpoint to the address above, with your device token in place of YOUR_PHONE_TOKEN.",
      "Overland sends batches and clears them only once GeoLogs confirms, so nothing is lost offline.",
    ],
  },
  {
    kind: "osmand",
    name: "Traccar Client or OsmAnd",
    platform: "iOS and Android",
    path: "/api/ingest/osmand?token=YOUR_PHONE_TOKEN",
    steps: [
      "Set the server URL to the address above, with your device token.",
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
const HA_ADDRESS_KEY = "geologs-ha-address";
// A token made from the Home Assistant steps, shown in them until the page reloads.
let haToken = "";

// The address Home Assistant uses for GeoLogs, which may differ from the one in this browser.
function haAddress() {
  let saved = "";
  try {
    saved = localStorage.getItem(HA_ADDRESS_KEY) || "";
  } catch (_error) {}
  return saved || location.origin;
}

function isLoopback(address) {
  try {
    const host = new URL(address).hostname.replace(/^\[|\]$/g, "");
    return host === "localhost" || host.endsWith(".localhost") || host === "::1" || /^127\./.test(host);
  } catch (_error) {
    return false;
  }
}

function copyBlock(label, text) {
  return `<div class="code-block"><pre class="mono" aria-label="${safe(label)}">${safe(text)}</pre><button type="button" class="button secondary small" data-copy-text="${safe(text)}">Copy</button></div>`;
}

// What Home Assistant sends: the same for the blueprint and the pasted automation.
const HA_SEND = `  - service: rest_command.geologs_location
    data:
      source: "home_assistant:{{ trigger.entity_id }}"
      timestamp: "{{ trigger.to_state.last_updated.isoformat() }}"
      lat: "{{ trigger.to_state.attributes.latitude }}"
      lon: "{{ trigger.to_state.attributes.longitude }}"
      accuracy: "{{ trigger.to_state.attributes.gps_accuracy | default('null', true) }}"`;

function haSnippets(address) {
  const base = address.replace(/\/+$/, "");
  const command = `rest_command:
  geologs_location:
    url: "${base}/api/ingest/generic"
    method: post
    headers:
      authorization: !secret geologs_authorization
    content_type: application/json
    payload: >-
      {"source": "{{ source }}", "timestamp": "{{ timestamp }}", "external_id": "{{ timestamp }}",
       "lat": {{ lat }}, "lon": {{ lon }}, "accuracy": {{ accuracy }}}`;
  const secret = `geologs_authorization: "Bearer ${haToken || "YOUR_PHONE_TOKEN"}"`;
  const automation = `alias: Send locations to GeoLogs
mode: queued
max: 50
trigger:
  - platform: state
    entity_id:
      - device_tracker.your_phone
condition:
  - condition: template
    value_template: >-
      {{ trigger.to_state is not none
         and trigger.to_state.attributes.latitude is number
         and trigger.to_state.attributes.longitude is number }}
action:
${HA_SEND}`;
  const importUrl =
    "https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=" + encodeURIComponent(base + HA_BLUEPRINT_PATH);
  return `<ol class="steps rich">
    <li><span>Add this to <code>configuration.yaml</code>:</span>${copyBlock("REST command for configuration.yaml", command)}</li>
    <li><span>Add a device token to <code>secrets.yaml</code>${haToken ? " (this one was just created for Home Assistant; it won't be shown again)" : ""}:</span>${copyBlock("Line for secrets.yaml", secret)}${haToken ? "" : '<button type="button" class="button secondary small ha-token" data-ha-token>Create a token for Home Assistant</button>'}</li>
    <li><span>Restart Home Assistant, or reload <strong>REST commands</strong> under Developer tools → YAML.</span></li>
    <li><span>Add the automation. In Home Assistant, open <a href="https://my.home-assistant.io/redirect/automations/" target="_blank" rel="noopener">Automations</a>, choose <strong>Create automation</strong> → <strong>Create new automation</strong>, then <strong>⋮</strong> → <strong>Edit in YAML</strong>, paste this, and put your trackers in place of <code>device_tracker.your_phone</code>:</span>${copyBlock("Automation YAML", automation)}
      <span class="help">Or import it as a blueprint and pick trackers from a list. Home Assistant downloads it from <a href="${safe(base + HA_BLUEPRINT_PATH)}" target="_blank" rel="noopener">${safe(base + HA_BLUEPRINT_PATH)}</a>, so that address must work from Home Assistant; “Unknown error” means it couldn't.</span>
      <a class="button secondary small ha-import" href="${safe(importUrl)}" target="_blank" rel="noopener">Import blueprint into Home Assistant</a></li>
  </ol>
  <p class="help">Positions arrive under the same names as <code>./geologs poll-ha</code>, so history continues: stop that cron job once this works.</p>`;
}

// One REST command and an automation: no polling, and positions arrive as Home Assistant sees them.
function homeAssistantSetup() {
  const address = haAddress();
  return `<label class="field ha-address"><span>GeoLogs address, as Home Assistant reaches it</span><input id="ha-address" class="input mono" type="url" value="${safe(address)}" spellcheck="false" autocomplete="off"></label>
  <p id="ha-address-warning" class="callout warning"${isLoopback(address) ? "" : " hidden"}><strong>Home Assistant can't reach localhost</strong><span>This browser opened GeoLogs as ${safe(new URL(location.origin).host)}, which on Home Assistant's machine means Home Assistant itself. Use the address it can reach, such as http://192.168.1.20:8766 or your Tailscale name.</span></p>
  <div id="ha-snippets">${haSnippets(address)}</div>`;
}

function wireHomeAssistantSetup() {
  const input = $("ha-address");
  if (!input) return;
  input.oninput = () => {
    const address = input.value.trim() || location.origin;
    try {
      if (input.value.trim() && input.value.trim() !== location.origin) localStorage.setItem(HA_ADDRESS_KEY, address);
      else localStorage.removeItem(HA_ADDRESS_KEY);
    } catch (_error) {}
    $("ha-address-warning").hidden = !isLoopback(address);
    renderHaSnippets(address);
  };
  wireHaToken();
}

function renderHaSnippets(address) {
  $("ha-snippets").innerHTML = haSnippets(address);
  onEach($("ha-snippets"), "copy-text", (text) => copyText(text, "Copied"));
  wireHaToken();
}

function wireHaToken() {
  const button = document.querySelector("#ha-snippets [data-ha-token]");
  if (!button) return;
  button.onclick = async () => {
    button.disabled = true;
    try {
      const created = await post("/api/ingest-tokens", { name: "Home Assistant", source: "home_assistant:" });
      haToken = created.token;
      renderHaSnippets($("ha-address").value.trim() || location.origin);
      await refreshTokens();
      toast("Token created for Home Assistant. Copy the secrets.yaml line now.");
    } catch (error) {
      button.disabled = false;
      toast(error.message);
    }
  };
}

// What a token may send as: one tracker, every tracker of an app, or anything.
const TOKEN_LOCKS = [
  ["", "Any tracker"],
  ["home_assistant:", "Any Home Assistant tracker"],
  ["owntracks:", "Any OwnTracks device"],
  ["overland:", "Any Overland device"],
  ["osmand:", "Any Traccar or OsmAnd device"],
  ["generic:", "Any GPSLogger, Tasker, or Shortcuts device"],
];

function lockLabel(source) {
  if (!source) return "any tracker";
  const kind = TOKEN_LOCKS.find(([value]) => value === source);
  return kind ? kind[1][0].toLowerCase() + kind[1].slice(1) : trackerLabel(source);
}

function renderTokens() {
  const data = state.ingestTokens;
  if (!data) return;
  const tokens = data.tokens || [];
  const rows = tokens.map((token) => {
    const used = token.last_used_at
      ? `last used ${ago(token.last_used_at)}${token.last_source ? ` as ${trackerLabel(token.last_source)}` : ""}`
      : "never used";
    const details = [`Can send as ${lockLabel(token.source)}`, `${token.hint}…`, `created ${new Date(token.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric", ...zoneOptions() })}`, used];
    return `<li class="list-item"><div class="place-main"><span class="health-dot ${token.last_used_at && Date.now() - Date.parse(token.last_used_at) < 86400e3 ? "success" : "neutral"}" aria-hidden="true"></span><div><strong>${safe(token.name)}</strong><small>${safe(details.join(" · "))}</small></div></div><button type="button" class="button secondary small danger-text" data-revoke-token="${token.id}">Revoke</button></li>`;
  });
  if (data.env_token)
    rows.push(
      `<li class="list-item"><div class="place-main"><span class="health-dot neutral" aria-hidden="true"></span><div><strong>From .env <span class="muted">· GEOLOGS_INGEST_TOKEN</span></strong><small>Shared by every device that has it, and can't be revoked here. Remove it from .env once each device has its own token.</small></div></div></li>`,
    );
  $("token-list").innerHTML =
    rows.join("") ||
    '<li class="empty-block"><strong>No device tokens yet</strong><span>Until you create one, apps need your main access token, which can also read your whole history.</span></li>';
  onEach($("token-list"), "revoke-token", async (id) => {
    const token = tokens.find((item) => item.id === +id);
    if (!confirm(`Revoke “${token?.name}”? Devices using it stop getting through at once. This can't be undone.`)) return;
    try {
      await post("/api/ingest-tokens/delete", { id: +id });
      await refreshTokens();
      toast("Token revoked");
    } catch (error) {
      toast(error.message);
    }
  });
  const chosen = $("token-source").value;
  const trackers = (state.sources?.sources || []).map((item) => item.source);
  $("token-source").innerHTML =
    TOKEN_LOCKS.map(([value, label]) => `<option value="${safe(value)}">${safe(label)}</option>`).join("") +
    (trackers.length
      ? `<optgroup label="Only this tracker">${trackers.map((source) => `<option value="${safe(source)}">${safe(trackerLabel(source))}</option>`).join("")}</optgroup>`
      : "");
  $("token-source").value = chosen;
  const count = tokens.length;
  $("ingest-token-note").hidden = !!(count || data.env_token);
}

export async function refreshTokens() {
  state.ingestTokens = await api("/api/ingest-tokens");
  renderTokens();
}

function initTokens() {
  $("token-form").onsubmit = async (event) => {
    event.preventDefault();
    const name = $("token-name").value.trim();
    if (!name) return;
    try {
      const created = await post("/api/ingest-tokens", { name, source: $("token-source").value });
      $("token-name").value = "";
      $("token-reveal").hidden = false;
      $("token-reveal").innerHTML = `<strong>Copy the token for ${safe(created.name)} now</strong><span>It is shown only once. Put it where the app asks for a token or password.</span><div class="copy-field"><input class="input mono" readonly value="${safe(created.token)}" aria-label="New token"><button type="button" class="button secondary small" data-copy-new-token>Copy</button><button type="button" class="button ghost small" data-hide-token>Done</button></div>`;
      $("token-reveal").querySelector("[data-copy-new-token]").onclick = () => copyText(created.token, "Token copied");
      $("token-reveal").querySelector("[data-hide-token]").onclick = () => {
        $("token-reveal").hidden = true;
        $("token-reveal").innerHTML = "";
      };
      await refreshTokens();
    } catch (error) {
      toast(error.message);
    }
  };
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
  renderTokens();
  // The 30 s refresh must not wipe what you are typing (the Home Assistant address).
  if ($("tracker-endpoints").contains(document.activeElement) && document.activeElement.matches("input")) {
    renderPriority();
    return;
  }
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
  wireHomeAssistantSetup();
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
  [state.sources, state.ingestTokens] = await Promise.all([api("/api/sources"), api("/api/ingest-tokens")]);
  renderSources();
}

// Unit words in labels across the app, and the radius fields' ranges, follow the Units setting.
function applyUnitLabels() {
  document.querySelectorAll(".speed-unit").forEach((item) => (item.textContent = speedUnit()));
  document.querySelectorAll(".length-unit").forEach((item) => (item.textContent = lengthUnit()));
  $("speed-offset-unit").textContent = speedUnit();
  for (const id of ["place-radius", "reminder-radius"]) {
    $(id).min = Math.round(toLengthUnit(20));
    $(id).max = Math.round(toLengthUnit(5000));
  }
  // The reminder form's default radius (150 m) until you type your own; the place dialog fills its own.
  const reminder = $("reminder-radius");
  if (!reminder.dataset.edited) reminder.value = Math.round(toLengthUnit(150));
  reminder.oninput = () => (reminder.dataset.edited = "1");
}

function renderUnitsChoice() {
  document.querySelectorAll('input[name="units"]').forEach((input) => (input.checked = input.value === (state.settings.units || "metric")));
}

const GRADE_LABELS = { regular: "regular", midgrade: "midgrade", premium: "premium", diesel: "diesel" };

// Car & fuel: the form, the price status, and what you spent lately.
function renderFuel() {
  const data = state.fuel;
  if (!data || !state.settings) return;
  const settings = state.settings;
  $("fuel-economy-unit").textContent = economyUnit();
  $("fuel-economy").placeholder = settings.units === "imperial" ? "e.g. 30" : "e.g. 8";
  const economy = toEconomyUnit(settings.fuel_economy_l100km);
  if (!$("car-form").contains(document.activeElement)) {
    $("fuel-economy").value = economy ? round1(economy) : "";
    $("fuel-grade").value = settings.fuel_grade;
    $("fuel-currency").value = settings.currency;
    document.querySelectorAll('input[name="fuel-price-source"]').forEach((input) => (input.checked = input.value === settings.fuel_price_source));
    $("fuel-price").value = settings.fuel_price_source === "manual" && data.price_per_l ? (Math.round(toPerVolume(data.price_per_l) * 1000) / 1000) : "";
  }
  $("fuel-price-unit").textContent = `per ${volumeUnit()}`;
  const auto = (document.querySelector('input[name="fuel-price-source"]:checked')?.value || settings.fuel_price_source) === "auto";
  const priceText = data.price_per_l
    ? `${moneyText(toPerVolume(data.price_per_l), data.currency)}/${volumeUnit()} ${GRADE_LABELS[settings.fuel_grade]}, week of ${new Date(data.price_date + "T12:00").toLocaleDateString(undefined, { month: "short", day: "numeric" })}`
    : "";
  $("fuel-auto-note").textContent = !data.eia_key
    ? "Needs a free EIA key: register at eia.gov/opendata, add EIA_API_KEY to .env, and restart. US only."
    : data.area
      ? `${data.area_label}${data.state ? ` (from ${data.state})` : ""}${auto && priceText ? `: ${priceText}` : ""}. Updated weekly by the US Energy Information Administration.`
      : "The weekly US average for your region; choose the region below if it can't be found from your location.";
  $("fuel-area-row").hidden = !auto;
  const areas = Object.entries(data.areas || {});
  $("fuel-area").innerHTML =
    `<option value="">Automatic${data.area && !settings.fuel_area ? ` (${data.area_label})` : ""}</option>` +
    areas.map(([code, label]) => `<option value="${code}">${safe(label)}</option>`).join("");
  $("fuel-area").value = settings.fuel_area || "";
  const spend = $("fuel-spend");
  const month = data.last_30_days,
    week = data.last_7_days;
  spend.hidden = !month?.configured;
  if (month?.configured) {
    const block = (label, costs) =>
      `<div><span>${label}</span><strong>${moneyText(costs.cost, costs.currency)}</strong><small>${plural(costs.drives, "drive")} · ${distanceText(costs.driving_km)} · ${(Math.round(toVolumeUnit(costs.litres) * 10) / 10).toLocaleString()} ${volumeUnit()}</small></div>`;
    spend.innerHTML = `<div class="fuel-stats"><div class="fuel-average"><span>Average per day</span><strong>${moneyText(month.cost_per_day, month.currency)}</strong><small>over the last ${plural(month.covered_days, "day")}</small></div>${block("Last 7 days", week)}${block("Last 30 days", month)}</div>${auto && priceText ? `<p class="help">At ${priceText}.</p>` : ""}`;
  }
}

export async function refreshFuel() {
  state.fuel = await api("/api/fuel");
  renderFuel();
}

function readFuelForm() {
  const economy = $("fuel-economy").value.trim();
  const source = document.querySelector('input[name="fuel-price-source"]:checked')?.value || "auto";
  const price = $("fuel-price").value.trim();
  const payload = {
    fuel_economy_l100km: economy ? Math.round(fromEconomyUnit(+economy) * 1000) / 1000 : null,
    fuel_grade: $("fuel-grade").value,
    fuel_price_source: source,
    fuel_area: $("fuel-area").value,
    currency: $("fuel-currency").value.trim(),
  };
  let error = "";
  if (economy && !(+economy > 0)) error = "Enter your car's fuel economy as a number.";
  else if (source === "manual" && !(+price > 0)) error = `Enter what you pay per ${volumeUnit() === "gal" ? "gallon" : "litre"}.`;
  else if (!payload.currency) error = "Enter a currency, such as $.";
  if (source === "manual" && +price > 0) payload.price_per_l = Math.round(fromPerVolume(+price) * 10000) / 10000;
  $("fuel-error").textContent = error;
  $("fuel-error").hidden = !error;
  return error ? null : payload;
}

function initFuel() {
  document.querySelectorAll('input[name="fuel-price-source"]').forEach((input) => (input.onchange = renderFuel));
  $("fuel-price").onfocus = () => {
    document.querySelector('input[name="fuel-price-source"][value="manual"]').checked = true;
    renderFuel();
  };
  $("car-form").onsubmit = async (event) => {
    event.preventDefault();
    const payload = readFuelForm();
    if (!payload) return;
    const button = $("car-form").querySelector(".button.primary");
    button.disabled = true;
    button.textContent = "Saving…";
    try {
      const result = await post("/api/fuel", payload);
      state.settings = await api("/api/settings");
      state.fuel = result;
      document.activeElement?.blur();
      renderFuel();
      const refresh = result.refresh;
      toast(
        refresh?.reason === "no_key"
          ? "Saved. Automatic prices need EIA_API_KEY in .env."
          : refresh?.reason === "no_area"
            ? "Saved. Choose a price region: your location isn't in a US region."
            : "Fuel settings saved",
      );
      await refreshExplore(false);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
      button.textContent = "Save changes";
    }
  };
  document.querySelectorAll('input[name="units"]').forEach(
    (input) =>
      (input.onchange = async () => {
        try {
          state.settings = await post("/api/settings", { units: input.value });
          await refreshAll();
          await refreshFuel();
          toast(input.value === "imperial" ? "Showing miles, mph, feet, and gallons" : "Showing kilometres, km/h, metres, and litres");
        } catch (error) {
          toast(error.message);
        }
      }),
  );
}

export function renderSettings() {
  if (!state.settings) return;
  renderPlacesProvider();
  populateTimezones(state.settings.timezone);
  $("speed-strategy").value = state.settings.speed_strategy;
  $("speed-offset").value = round1(toSpeedUnit(state.settings.reported_speed_offset_kmh));
  applyUnitLabels();
  renderUnitsChoice();
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
  initFuel();
  initTokens();
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
      reported_speed_offset_kmh: speedFromField("speed-offset", state.settings.reported_speed_offset_kmh) || 0,
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
