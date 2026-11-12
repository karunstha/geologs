// Live location sharing: share where you are with people on this server (Account → Location
// sharing), and see the people who share with you on the map. Refreshed every minute; the server
// keeps each shared position about that fresh.
import { api, post } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { ago } from "../core/format.js";
import { state } from "../core/state.js";
import { fitDay, map, peopleLayer, peopleOnMap } from "../map/map.js";
import { onEach } from "../ui/actions.js";
import { refreshExplore } from "./history.js";
import { showView } from "./shell.js";

// One colour per person, kept by their id, so a person keeps theirs between refreshes.
const COLORS = ["#6d4fc2", "#c2410c", "#0f766e", "#b5761b", "#be185d", "#1d4ed8"];
const STALE_MS = 30 * 60 * 1000;
const UNTIL = { hour: "for 1 hour", today: "until the end of today", always: "until you stop it" };

const personName = (person) => person.name || person.email || "Someone";
const initial = (person) => personName(person).trim()[0]?.toUpperCase() || "?";
const color = (person) => COLORS[person.id % COLORS.length];

function until(share) {
  if (!share.expires_at) return "until stopped";
  const at = new Date(share.expires_at);
  const sameDay = at.toDateString() === new Date().toDateString();
  return `until ${at.toLocaleString(undefined, sameDay ? { hour: "numeric", minute: "2-digit" } : { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`;
}

function where(share) {
  const position = share.position;
  if (!position) return "Waiting for their location";
  const place = share.precision === "approximate"
    ? `Near ${position.place_name || "an unknown town"}`
    : position.moving
      ? "On the move"
      : position.place_name || "At an unnamed place";
  return `${place} · ${ago(position.fix_at)}`;
}

function drawPeople() {
  if (!peopleLayer) return;
  peopleLayer.clearLayers();
  for (const share of state.sharing?.received || []) {
    const { position, sharer } = share;
    if (!position) continue;
    const stale = Date.now() - Date.parse(position.fix_at) > STALE_MS;
    const popup = `<div class="map-popup"><strong>${safe(personName(sharer))}</strong><span>${safe(where(share))}</span>${
      share.precision === "approximate" ? "<span>Only their town is shared</span>" : ""
    }</div>`;
    if (share.precision === "approximate")
      L.circle([position.lat, position.lon], { radius: 800, color: color(sharer), weight: 2, dashArray: "5 5", fillOpacity: 0.08 })
        .bindPopup(popup)
        .addTo(peopleLayer);
    L.marker([position.lat, position.lon], {
      icon: L.divIcon({
        className: "",
        html: `<span class="person-pin${stale ? " stale" : ""}" style="--person:${color(sharer)}">${safe(initial(sharer))}</span>`,
        iconSize: [30, 30],
        iconAnchor: [15, 15],
      }),
      title: personName(sharer),
      zIndexOffset: 1000,
    })
      .bindPopup(popup)
      .addTo(peopleLayer);
  }
}

function renderSharing() {
  const data = state.sharing;
  if (!data) return;
  $("sharing-given").innerHTML =
    data.given
      .map(
        (share) =>
          `<li class="list-item"><div class="place-main"><span class="health-dot success" aria-hidden="true"></span><div><strong>${safe(personName(share.viewer))}</strong><small>${safe(
            [share.viewer.name ? share.viewer.email : "", share.precision === "approximate" ? "only your town" : "where you are", until(share)].filter(Boolean).join(" · "),
          )}</small></div></div><button type="button" class="button secondary small danger-text" data-stop-sharing="${share.id}">Stop</button></li>`,
      )
      .join("") || '<li class="empty-block"><strong>You share with no one</strong><span>Type someone\'s email above to show them where you are.</span></li>';
  $("sharing-received").innerHTML =
    data.received
      .map(
        (share) =>
          `<li class="list-item"><div class="place-main"><span class="person-pin" style="--person:${color(share.sharer)}" aria-hidden="true">${safe(initial(share.sharer))}</span><div><strong>${safe(personName(share.sharer))}</strong><small>${safe(
            [where(share), until(share)].join(" · "),
          )}</small></div></div><span class="inline-form">${
            share.position ? `<button type="button" class="button secondary small" data-show-person="${share.id}">Show on map</button>` : ""
          }<button type="button" class="button ghost small" data-leave-share="${share.id}">Stop seeing</button></span></li>`,
      )
      .join("") || '<li class="empty-block"><strong>No one shares with you</strong><span>When someone does, they show here and on your map.</span></li>';
  onEach($("sharing-given"), "stop-sharing", async (id) => {
    const share = data.given.find((item) => item.id === +id);
    if (!confirm(`Stop sharing your location with ${personName(share?.viewer || {})}?`)) return;
    await change("/api/location-sharing/stop", { id: +id }, "Sharing stopped");
  });
  onEach($("sharing-received"), "leave-share", async (id) => {
    const share = data.received.find((item) => item.id === +id);
    if (!confirm(`Stop seeing ${personName(share?.sharer || {})}'s location? They can share it with you again.`)) return;
    await change("/api/location-sharing/leave", { id: +id }, "You no longer see their location");
  });
  onEach($("sharing-received"), "show-person", (id) => {
    const share = data.received.find((item) => item.id === +id);
    if (!share?.position) return;
    showView("explore");
    setTimeout(() => {
      map.invalidateSize();
      map.setView([share.position.lat, share.position.lon], share.precision === "approximate" ? 13 : 16);
    }, 50);
  });
}

async function change(path, body, message) {
  try {
    state.sharing = await post(path, body);
    renderSharing();
    drawPeople();
    toast(message);
  } catch (error) {
    toast(error.message);
  }
}

export async function refreshPeople() {
  const before = peopleOnMap(state.day);
  try {
    state.sharing = await api("/api/location-sharing");
  } catch (_error) {
    return; // signed out, or offline: the page says so elsewhere
  }
  renderSharing();
  drawPeople();
  // Someone started or stopped sharing: the page may now show the map, or no longer need it.
  if (peopleOnMap(state.day) !== before) {
    await refreshExplore(false).catch(() => {});
    if (!before && !state.day?.points) fitDay(); // fitted to them (see dayBounds)
  }
}

// Signed out: nobody's position stays on the map or the page for whoever signs in next.
export function clearPeople() {
  state.sharing = null;
  peopleLayer?.clearLayers();
  $("sharing-given").innerHTML = "";
  $("sharing-received").innerHTML = "";
  $("sharing-form").reset();
}

export function initPeople() {
  $("sharing-form").onsubmit = async (event) => {
    event.preventDefault();
    const duration = $("sharing-duration").value;
    $("sharing-go").disabled = true;
    try {
      state.sharing = await post("/api/location-sharing", {
        email: $("sharing-email").value.trim(),
        precision: $("sharing-precision").value,
        duration,
      });
      $("sharing-email").value = "";
      renderSharing();
      toast(`Your location is shared ${UNTIL[duration]}`);
    } catch (error) {
      toast(error.message);
    } finally {
      $("sharing-go").disabled = false;
    }
  };
}
