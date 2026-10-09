// Settings: sources and tracker setup, analysis, place names, summaries, and access.
import { refreshMeta } from "../app/refresh.js";
import { api, openAuth, post, setConnection } from "../core/api.js";
import { $, copyText, safe, toast } from "../core/dom.js";
import { ago, formatTime, localDateKey, longDate, plural, zoneOptions } from "../core/format.js";
import { state } from "../core/state.js";
import { actionIcon, onEach } from "../ui/actions.js";
import { sourceName, statusPill } from "../ui/icons.js";
import { refreshExplore } from "./history.js";
import { refreshInsights } from "./patterns.js";
import { showView } from "./shell.js";
import { openSummary, summaryLine } from "./summaries.js";

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

export function renderDailySummaries() {
  const box = $("daily-summaries");
  box.innerHTML =
    state.dailySummaries
      .filter((item) => item.points)
      .slice(0, 5)
      .map(
        (item) =>
          `<button type="button" class="list-item row-link" data-open-summary="${safe(item.date)}"><span><strong>${safe(longDate(item.date))}</strong><small>${safe(summaryLine(item))}</small></span><span class="row-chevron" aria-hidden="true">${actionIcon("open")}</span></button>`,
      )
      .join("") ||
    '<div class="empty-block"><strong>No saved days yet</strong><span>The first summary is saved after midnight following your first recorded day.</span></div>';
  onEach(box, "open-summary", (date) => {
    showView("summaries");
    openSummary(date, true);
  });
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
          ? statusPill("success", "Set up")
          : `<span class="status-pill neutral" title="Add ${safe(option.env)} to .env and restart">Needs ${safe(option.env)}</span>`
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
    kind: "api",
    name: "Home Assistant poller & API",
    platform: "POST /api/points",
    path: "/api/points",
    steps: [
      "Run ./geologs poll-ha from cron to read a Home Assistant device tracker, or POST JSON points with a Bearer token.",
    ],
  },
];

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
      return `<li><span class="health-dot ${tone}" aria-hidden="true"></span><span><strong>${safe(deviceLabel(device))}</strong><small title="${safe(formatTime(device.last_attempt_at))}">${safe(detail)}</small></span></li>`;
    })
    .join("")}</ul>`;
}

export function renderSources() {
  const data = state.sources;
  if (!data) return;
  const sources = data.sources || [];
  const newest = sources[0];
  $("sources-summary").textContent = sources.length
    ? `${plural(sources.length, "source")} · last location received ${ago(newest.last_received)}`
    : "No locations received yet. Connect an app below.";
  $("source-list").innerHTML =
    sources
      .map((item) => {
        // Health follows the newest fix itself: an old backfill that arrived a minute ago is not live.
        const age = Date.now() - Date.parse(item.last_fix),
          tone = age < 3600e3 ? "success" : age < 86400e3 ? "warning" : "neutral";
        const name = sourceName(item.source);
        const lag = (Date.parse(item.last_received) - Date.parse(item.last_fix)) / 60000;
        const details = [
          `received ${ago(item.last_received)}`,
          lag > 15 ? `newest fix from ${formatTime(item.last_fix)}` : "",
          `${Number(item.fixes_24h).toLocaleString()} in the last 24 h`,
          `${Number(item.fixes).toLocaleString()} since ${new Date(item.first_fix).toLocaleDateString(undefined, { month: "short", year: "numeric", ...zoneOptions() })}`,
        ].filter(Boolean);
        return `<div class="list-item"><div class="place-main"><span class="health-dot ${tone}" aria-hidden="true"></span><div><strong>${safe(name.kind)}${name.device ? ` <span class="muted">· ${safe(name.device)}</span>` : ""}</strong><small>${safe(details.join(" · "))}</small></div></div>${statusPill(tone, tone === "success" ? "Live" : tone === "warning" ? "Quiet" : "Idle")}</div>`;
      })
      .join("") ||
    '<div class="empty-block"><strong>No sources yet</strong><span>Locations appear here as soon as a device sends one.</span></div>';
  const byKind = new Map();
  for (const device of data.devices || []) {
    if (!byKind.has(device.kind)) byKind.set(device.kind, []);
    byKind.get(device.kind).push(device);
  }
  $("ingest-token-note").hidden = !!data.ingest_token;
  $("access-ingest-badge").innerHTML = statusPill(
    data.ingest_token ? "success" : "warning",
    data.ingest_token ? "Set up" : "Not set",
  );
  const open = new Set(
    [...document.querySelectorAll("#tracker-endpoints details[open]")].map((item) => item.dataset.kind),
  );
  $("tracker-endpoints").innerHTML = TRACKERS.map((tracker) => {
    const devices = byKind.get(tracker.kind) || [];
    const status = trackerStatus(devices);
    const url = location.origin + tracker.path;
    return `<details class="tracker" data-kind="${tracker.kind}"${open.has(tracker.kind) ? " open" : ""}><summary><span class="tracker-title"><strong>${safe(tracker.name)}</strong><small>${safe(tracker.platform)}</small></span>${statusPill(status.tone, status.label)}<svg class="chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></summary>
      <div class="tracker-body">${deviceRows(devices)}<div class="tracker-setup"><h4>Set up</h4><div class="copy-field"><input class="input mono" readonly value="${safe(url)}" aria-label="${safe(tracker.name)} address"><button type="button" class="button secondary small" data-copy-endpoint="${safe(url)}">Copy</button></div><ol class="steps">${tracker.steps.map((step) => `<li>${safe(step)}</li>`).join("")}</ol></div></div></details>`;
  }).join("");
  onEach($("tracker-endpoints"), "copy-endpoint", (url) => copyText(url, "Address copied"));
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
  renderDailySummaries();
}

export function updateSpeedSettingsVisibility() {
  const reported = $("speed-strategy").value === "reported";
  $("speed-offset").disabled = !reported;
  $("speed-offset-field").classList.toggle("disabled-field", !reported);
}

export function initSettings() {
  $("explore-summaries").onclick = () => showView("summaries");
  $("connection").onclick = () => {
    showView("settings");
    document.getElementById("settings-sources").scrollIntoView({ behavior: "smooth", block: "start" });
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
  document.querySelectorAll(".settings-nav a").forEach(
    (link) =>
      (link.onclick = (event) => {
        event.preventDefault();
        document.querySelector(link.getAttribute("href")).scrollIntoView({
          behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
          block: "start",
        });
      }),
  );
  if ("IntersectionObserver" in window) {
    // Highlight the section being read in the Settings navigation.
    const sectionObserver = new IntersectionObserver(
      (entries) => {
        for (const entry of entries)
          if (entry.isIntersecting)
            document
              .querySelectorAll(".settings-nav a")
              .forEach((link) =>
                link.classList.toggle("active", link.getAttribute("href") === "#" + entry.target.id),
              );
      },
      { rootMargin: "-20% 0px -70% 0px" },
    );
    document.querySelectorAll(".settings-section").forEach((section) => sectionObserver.observe(section));
  }
  setInterval(() => {
    if ($("settings-view").classList.contains("active") && !document.hidden) refreshSources().catch(() => {});
  }, 30000);
  $("speed-strategy").onchange = updateSpeedSettingsVisibility;
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
  $("settings-form").onsubmit = async (event) => {
    event.preventDefault();
    try {
      state.settings = await post("/api/settings", {
        timezone: $("summary-timezone").value.trim(),
        speed_strategy: $("speed-strategy").value,
        reported_speed_offset_kmh: +$("speed-offset").value,
      });
      if (state.followToday) $("day-date").value = localDateKey();
      renderSettings();
      await refreshExplore();
      toast("Settings saved");
    } catch (error) {
      toast(error.message);
    }
  };
}
