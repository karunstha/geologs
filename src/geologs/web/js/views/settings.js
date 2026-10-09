// Settings: sources and tracker setup, analysis, place names, summaries, and access.
import { refreshMeta } from "../app/refresh.js";
import { api, openAuth, post, setConnection } from "../core/api.js";
import { $, copyText, safe, toast } from "../core/dom.js";
import { ago, formatTime, localDateKey, longDate, zoneOptions } from "../core/format.js";
import { state } from "../core/state.js";
import { sourceName, statusPill } from "../ui/icons.js";
import { refreshExplore, refreshInsights } from "./history.js";
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
  $("daily-summaries").innerHTML =
    state.dailySummaries
      .slice(0, 5)
      .map(
        (item) =>
          `<div class="list-item"><div><strong>${safe(longDate(item.date))}</strong><small>${item.points ? safe(summaryLine(item)) : "No location data"}</small></div><button type="button" data-open-summary="${safe(item.date)}">View</button></div>`,
      )
      .join("") || '<div class="empty">A summary is saved after the first completed day.</div>';
  $("daily-summaries")
    .querySelectorAll("[data-open-summary]")
    .forEach(
      (button) =>
        (button.onclick = () => {
          showView("summaries");
          openSummary(button.dataset.openSummary, false, true);
        }),
    );
}

export function renderPlacesProvider() {
  const providers = state.status?.places_providers || [],
    chosen = state.settings?.places_provider || "";
  const select = $("places-provider");
  select.innerHTML =
    '<option value="">Automatic · first configured</option>' +
    providers
      .map(
        (provider) =>
          `<option value="${safe(provider.id)}"${provider.configured || provider.id === chosen ? "" : " disabled"}>${safe(provider.label)}${provider.configured ? "" : " · set " + safe(provider.env)}</option>`,
      )
      .join("");
  select.value = chosen;
  const active = providers.find((provider) => provider.id === state.status?.geocoder);
  $("places-provider-help").textContent = active
    ? `Currently using ${active.label}.`
    : "No provider is configured; set an API key in .env and restart the server.";
  updateProviderNotice();
}

export function updateProviderNotice() {
  // Automatic resolves to whatever the server chose, so check that too.
  const value = $("places-provider").value || state.status?.geocoder;
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

export function trackerStatus(app) {
  if (!app) return { tone: "neutral", label: "Not connected", detail: "No requests from this app yet." };
  if (app.last_status >= 400 && (!app.last_success_at || app.last_attempt_at > app.last_success_at))
    return {
      tone: "error",
      label: "Failing",
      detail: `Last request ${ago(app.last_attempt_at)} was rejected: ${app.last_message || "error " + app.last_status}.`,
    };
  const quiet = Date.now() - Date.parse(app.last_success_at) > 6 * 3600e3,
    source = sourceName(app.last_success_source);
  return {
    tone: quiet ? "warning" : "success",
    label: quiet ? "Quiet" : "Receiving",
    detail: [
      `Last data ${ago(app.last_success_at)}`,
      app.last_success_source ? `from ${source.device || source.kind}` : "",
      `${Number(app.received || 0).toLocaleString()} fix${Number(app.received) === 1 ? "" : "es"} received`,
      app.last_message || "",
    ]
      .filter(Boolean)
      .join(" · "),
  };
}

export function renderSources() {
  const data = state.sources;
  if (!data) return;
  const sources = data.sources || [],
    newest = sources[0];
  $("sources-summary").textContent = sources.length
    ? `${sources.length} source${sources.length === 1 ? "" : "s"} · last location received ${ago(newest.last_received)}`
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
          `Received ${ago(item.last_received)}`,
          lag > 15 ? `newest fix from ${formatTime(item.last_fix)}` : "",
          `${Number(item.fixes_24h).toLocaleString()} in the last 24 h`,
          `${Number(item.fixes).toLocaleString()} total since ${new Date(item.first_fix).toLocaleDateString(undefined, { month: "short", year: "numeric", ...zoneOptions() })}`,
        ].filter(Boolean);
        return `<div class="list-item"><div class="place-main"><span class="health-dot ${tone}" aria-hidden="true"></span><div><strong>${safe(name.kind)}${name.device ? ` <span class="muted">· ${safe(name.device)}</span>` : ""}</strong><small>${safe(details.join(" · "))}</small></div></div>${statusPill(tone, tone === "success" ? "Live" : tone === "warning" ? "Quiet" : "Idle")}</div>`;
      })
      .join("") || '<div class="empty">Locations will appear here as soon as a device sends one.</div>';
  const apps = new Map((data.apps || []).map((app) => [app.kind, app]));
  $("ingest-token-note").hidden = !!data.ingest_token;
  $("ingest-token-note").textContent =
    "Set GEOLOGS_INGEST_TOKEN in .env to give phones a send-only token. Until then, apps need your main access token, which can also read your history.";
  $("access-ingest-badge").outerHTML =
    `<span id="access-ingest-badge">${statusPill(data.ingest_token ? "success" : "warning", data.ingest_token ? "Configured" : "Not set")}</span>`;
  const open = new Set(
    [...document.querySelectorAll("#tracker-endpoints details[open]")].map((item) => item.dataset.kind),
  );
  $("tracker-endpoints").innerHTML = TRACKERS.map((tracker) => {
    const status = trackerStatus(apps.get(tracker.kind)),
      url = location.origin + tracker.path;
    return `<details class="tracker" data-kind="${tracker.kind}"${open.has(tracker.kind) ? " open" : ""}><summary><span class="tracker-title"><strong>${safe(tracker.name)}</strong><small>${safe(tracker.platform)}</small></span>${statusPill(status.tone, status.label)}<svg class="chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></summary>
      <div class="tracker-body"><p class="tracker-status ${status.tone}">${safe(status.detail)}</p><div class="copy-field"><input class="input mono" readonly value="${safe(url)}" aria-label="${safe(tracker.name)} address"><button type="button" class="button secondary small" data-copy-endpoint="${safe(url)}">Copy</button></div><ol class="steps">${tracker.steps.map((step) => `<li>${safe(step)}</li>`).join("")}</ol></div></details>`;
  }).join("");
  $("tracker-endpoints")
    .querySelectorAll("[data-copy-endpoint]")
    .forEach((button) => (button.onclick = () => copyText(button.dataset.copyEndpoint, "Address copied")));
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
  $("places-provider").onchange = updateProviderNotice;
  $("places-form").onsubmit = async (event) => {
    event.preventDefault();
    try {
      state.settings = await post("/api/settings", { places_provider: $("places-provider").value });
      state.status = await api("/api/status");
      renderSettings();
      toast("Places provider saved");
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
