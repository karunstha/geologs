// The replay player: a dot travels each way of a day (an /api/replay or /api/shared answer) on a MapLibre
// map, leaving the path behind it in each mode's colour, and pauses briefly at every stop. The day's moving
// time plays in about half a minute; stops take a moment each however long they were. It needs no sign-in
// and no app state, so a shared link's page (js/share.js) plays with it too; it builds its own dialog.
import { $, safe } from "../core/dom.js";
import { distanceText, durationText, speedText } from "../core/format.js";
import { freeMapStyle, greyMap } from "../map/free-map.js";
import { modeIcon, modeInfo, modeName } from "../ui/icons.js";
import { drawFrame, videoFormat } from "./capture.js";

// A video's longer side at most, and how long it holds the whole day at the end.
const VIDEO_MAX_PX = 1920;
const VIDEO_END_MS = 1800;
// Seconds the day's moving time plays in at 1×, the least any one way gets, and each stop's pause.
const MOVING_SECONDS = 28;
const LEAST_MOVE_SECONDS = 1.6;
const STOP_SECONDS = 1.1;
const SPEEDS = [0.5, 1, 2, 4];
// How close the camera follows each mode: slower ways are seen closer.
const FOLLOW_ZOOM = { driving: 13.2, passenger: 13.2, transit: 12.8, cycling: 14.6, running: 15.2, walking: 15.4 };
const NO_MODE_COLOR = "#176d69";
// A place nothing named shows as its coordinates; on the map that says nothing.
const COORDINATES = /^-?\d+\.\d+, -?\d+\.\d+$/;

const ICONS = {
  close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  follow:
    '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/></svg>',
  share:
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12M7.5 7.5 12 3l4.5 4.5M5 13v6.5h14V13"/></svg>',
  video:
    '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="6" width="13" height="12" rx="2.5"/><path d="m16 10.5 5-3v9l-5-3"/></svg>',
};
const DIALOG = `
  <div id="replay-map" class="replay-map"></div>
  <header class="replay-head replay-panel">
    <div><h2 id="replay-title">Replay</h2><p id="replay-summary"></p></div>
    <button id="replay-video" class="replay-share-button" type="button" title="Save the replay as a video">${ICONS.video}<span>Video</span></button>
    <button id="replay-share" class="replay-share-button" type="button" hidden>${ICONS.share}<span>Share</span></button>
    <button id="replay-close" class="icon-button" type="button" aria-label="Close replay" title="Close (Esc)">${ICONS.close}</button>
  </header>
  <section class="replay-now replay-panel" aria-label="Now in the replay">
    <div id="replay-status" class="replay-status" aria-live="off"></div>
    <dl class="replay-stats">
      <div><dt id="replay-clock-label">Time</dt><dd id="replay-clock"></dd></div>
      <div><dt>Distance</dt><dd id="replay-distance"></dd></div>
      <div><dt>Moving</dt><dd id="replay-moving"></dd></div>
    </dl>
  </section>
  <footer class="replay-controls replay-panel">
    <button id="replay-play" class="replay-play" type="button" aria-label="Play" autofocus></button>
    <div class="replay-track">
      <div id="replay-ribbon" class="replay-ribbon" aria-hidden="true"></div>
      <input id="replay-scrub" class="replay-scrub" type="range" min="0" max="1000" step="1" value="0" aria-label="Replay position">
    </div>
    <div id="replay-speeds" class="replay-speeds" role="group" aria-label="Speed">${SPEEDS.map(
      (speed) => `<button type="button" data-speed="${speed}" aria-pressed="false">${speed}×</button>`,
    ).join("")}</div>
    <button id="replay-follow" class="replay-follow" type="button" aria-pressed="true" title="Follow the dot">${ICONS.follow}<span>Follow</span></button>
  </footer>`;

let glMap = null,
  head = null,
  replay = null,
  frame = 0;

const color = (mode) => modeInfo[mode]?.color || NO_MODE_COLOR;
const placeName = (name) => (name && !COORDINATES.test(name) ? name : "");
// As on Home, a way nothing recorded has no mode, and counts toward no distance or moving time.
const counted = (move) => !!modeInfo[move.mode];
const line = (track, mode) => ({
  type: "Feature",
  properties: { color: color(mode) },
  geometry: { type: "LineString", coordinates: track.map((point) => [point[1], point[2]]) },
});
const collection = (features) => ({ type: "FeatureCollection", features });

// The replay's beats in order: each way, and a stop before, between and after them. `at` and `length` are
// playback seconds; a way's `from` and `to` are its real seconds after the day's start.
function beats(data) {
  const moves = data.moves.filter((move) => move.track.length);
  const moving = moves.reduce((sum, move) => sum + Math.max(0, move.end - move.start), 0) || 1;
  // The stay the dot waits at, if one covers the time: a shared link leaves some out (see shared_replay).
  const stopAt = (seconds, near, place) => ({
    kind: "stop",
    stay: data.stays.find((stay) => stay.start <= seconds && seconds <= stay.end) || null,
    place,
    point: near,
    length: STOP_SECONDS,
  });
  const list = [];
  moves.forEach((move, index) => {
    const first = move.track[0],
      last = move.track[move.track.length - 1];
    // An activity around a place (a lake run) is the stop it happened at, under way.
    if (move.kind !== "activity" && (index === 0 || moves[index - 1].end < move.start))
      list.push(stopAt(index ? (moves[index - 1].end + move.start) / 2 : move.start, first, index ? "A stop" : "Start"));
    list.push({
      kind: "move",
      move,
      from: move.start,
      to: move.end,
      length: Math.max(LEAST_MOVE_SECONDS, ((move.end - move.start) / moving) * MOVING_SECONDS),
    });
    if (index === moves.length - 1 && move.kind !== "activity") list.push(stopAt(move.end, last, "Finish"));
  });
  let at = 0;
  for (const beat of list) {
    beat.at = at;
    at += beat.length;
  }
  return { list, length: at };
}

// Where a way's dot is at real second `seconds`: [lon, lat], and the index of the track point before it.
function along(track, seconds) {
  let low = 0,
    high = track.length - 1;
  if (seconds <= track[0][0]) return { lonLat: [track[0][1], track[0][2]], index: 0 };
  if (seconds >= track[high][0]) return { lonLat: [track[high][1], track[high][2]], index: high };
  while (high - low > 1) {
    const middle = (low + high) >> 1;
    if (track[middle][0] <= seconds) low = middle;
    else high = middle;
  }
  const a = track[low],
    b = track[high],
    share = b[0] > a[0] ? (seconds - a[0]) / (b[0] - a[0]) : 0;
  return { lonLat: [a[1] + (b[1] - a[1]) * share, a[2] + (b[2] - a[2]) * share], index: low };
}

function metres(a, b) {
  const rad = Math.PI / 180,
    x = (b[0] - a[0]) * rad * Math.cos(((a[1] + b[1]) / 2) * rad),
    y = (b[1] - a[1]) * rad;
  return Math.hypot(x, y) * 6371000;
}

// The day's clock at real second `seconds`, in the day's own timezone; a shared link may give only the
// time since its first way instead ("1:05").
function clockAt(seconds) {
  if (replay.data.elapsed) {
    const minutes = Math.max(0, Math.round(seconds / 60));
    return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}`;
  }
  const options = { hour: "numeric", minute: "2-digit" };
  try {
    new Intl.DateTimeFormat(undefined, { timeZone: replay.data.timezone });
    options.timeZone = replay.data.timezone;
  } catch (_error) {
    /* the browser's own zone */
  }
  return new Date(replay.origin + seconds * 1000).toLocaleTimeString([], options);
}

function beatAt(time) {
  const list = replay.beats.list;
  let index = Math.min(replay.index, list.length - 1);
  while (index > 0 && list[index].at > time) index--;
  while (index < list.length - 1 && list[index].at + list[index].length <= time) index++;
  return index;
}

// The beat's real second at playback second `time`.
function realAt(beat, time) {
  const share = Math.min(1, Math.max(0, (time - beat.at) / beat.length));
  return beat.kind === "move" ? beat.from + share * (beat.to - beat.from) : null;
}

function draw() {
  const { beats: plan, data } = replay;
  const time = replay.time,
    index = beatAt(time),
    beat = plan.list[index];
  replay.index = index;
  // Ways already travelled, then the one under way as far as the dot.
  const done = plan.list.slice(0, index).filter((item) => item.kind === "move");
  const trail = done.map((item) => line(item.move.track, item.move.mode));
  let lonLat,
    mode = null,
    speed = null,
    km = done.filter((item) => counted(item.move)).reduce((sum, item) => sum + (item.move.distance_km || 0), 0),
    minutes = done.filter((item) => counted(item.move)).reduce((sum, item) => sum + (item.to - item.from) / 60, 0),
    real;
  if (beat.kind === "move") {
    const { move } = beat;
    real = realAt(beat, time);
    const place = along(move.track, real);
    lonLat = place.lonLat;
    mode = move.mode;
    trail.push(line([...move.track.slice(0, place.index + 1), [real, ...lonLat]], move.mode));
    if (counted(move)) {
      const share = move.end > move.start ? (real - move.start) / (move.end - move.start) : 1;
      km += (move.distance_km || 0) * share;
      minutes += (real - move.start) / 60;
    }
    // The speed over the stretch about the dot, if anything recorded it.
    const before = along(move.track, real - 45),
      after = along(move.track, real + 45),
      span = Math.min(real + 45, move.end) - Math.max(real - 45, move.start);
    if (move.recorded && span > 10) speed = (metres(before.lonLat, after.lonLat) / span) * 3.6;
  } else {
    lonLat = beat.point.slice(1);
    real = beat.point[0];
  }
  glMap.getSource("replay-trail")?.setData(collection(trail));
  const reached = data.stays.filter((stay) => stay.start <= real + 1);
  glMap.getSource("replay-stops")?.setData(
    collection(
      reached.map((stay) => ({
        type: "Feature",
        properties: { name: placeName(stay.name) },
        geometry: { type: "Point", coordinates: [stay.lon, stay.lat] },
      })),
    ),
  );
  head.setLngLat(lonLat);
  head.getElement().style.setProperty("--mode", color(mode || done.at(-1)?.move.mode));
  head.getElement().classList.toggle("stopped", beat.kind === "stop");
  if (replay.follow && replay.playing) {
    const target = beat.kind === "move" ? FOLLOW_ZOOM[mode] || 14 : replay.zoom;
    replay.zoom += (target - replay.zoom) * Math.min(1, replay.step * 1.4);
    glMap.jumpTo({ center: lonLat, zoom: replay.zoom });
  }
  // What the dot is doing, and the day so far.
  if (beat.kind === "move") {
    const { move } = beat;
    const name = placeName(move.kind === "trip" ? move.to_name : move.from_name);
    $("replay-status").innerHTML = `${modeIcon(move.mode)}<span><strong>${safe(modeName(move.mode))}${
      name ? ` ${move.kind === "trip" ? "to" : "at"} ${safe(name)}` : ""
    }</strong><small>${speed ? safe(speedText(speed)) : move.recorded ? "" : "Not recorded, so the way is guessed"}</small></span>`;
  } else {
    const stay = beat.stay;
    $("replay-status").innerHTML = `<span class="replay-pin" aria-hidden="true"></span><span><strong>${safe(
      placeName(stay?.name) || beat.place,
    )}</strong><small>${stay ? `${safe(clockAt(stay.start))} – ${safe(clockAt(stay.end))}` : ""}</small></span>`;
  }
  $("replay-clock").textContent = clockAt(real);
  $("replay-distance").textContent = distanceText(km);
  $("replay-moving").textContent = durationText(minutes);
  $("replay-scrub").value = String(Math.round((time / plan.length) * 1000));
  $("replay-scrub").setAttribute("aria-valuetext", clockAt(real));
}

function tick(now) {
  frame = 0;
  if (!replay?.playing) return;
  replay.step = Math.min(0.1, (now - (replay.last || now)) / 1000);
  replay.last = now;
  replay.time = Math.min(replay.beats.length, replay.time + replay.step * replay.speed);
  draw();
  if (replay.time >= replay.beats.length) {
    setPlaying(false);
    fitDay();
    // A video holds the whole day a moment, then is saved.
    if (replay.recording) setTimeout(() => stopRecording(true), VIDEO_END_MS);
    return;
  }
  frame = requestAnimationFrame(tick);
}

function setPlaying(on) {
  if (!replay) return;
  if (on && replay.time >= replay.beats.length) {
    replay.time = 0;
    replay.index = 0;
  }
  replay.playing = on;
  replay.last = 0;
  $("replay-play").innerHTML = on
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14M16 5v14"/></svg>'
    : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4.5v15l12-7.5z"/></svg>';
  $("replay-play").setAttribute("aria-label", on ? "Pause" : "Play");
  if (on && !frame) {
    $("replay-share-panel")?.remove();
    if (replay.follow) replay.zoom = glMap.getZoom();
    frame = requestAnimationFrame(tick);
  }
}

function fitDay() {
  const points = replay.data.moves.flatMap((move) => move.track.map((point) => [point[1], point[2]]));
  for (const stay of replay.data.stays) points.push([stay.lon, stay.lat]);
  if (!points.length) return;
  const lons = points.map((point) => point[0]),
    lats = points.map((point) => point[1]);
  const phone = matchMedia("(max-width: 700px)").matches;
  glMap.fitBounds(
    [
      [Math.min(...lons), Math.min(...lats)],
      [Math.max(...lons), Math.max(...lats)],
    ],
    {
      padding: { top: phone ? 150 : 110, bottom: phone ? 190 : 150, left: 50, right: 50 },
      maxZoom: 15.5,
      duration: replay.playing ? 0 : 700,
    },
  );
}

// The day's ways, faint, under the trail; the trail; the stops reached so far, with their names.
function addLayers() {
  const ways = replay.data.moves.filter((move) => move.track.length).map((move) => line(move.track, move.mode));
  const sources = {
    "replay-ways": collection(ways),
    "replay-trail": collection([]),
    "replay-stops": collection([]),
  };
  for (const [id, data] of Object.entries(sources)) {
    if (glMap.getSource(id)) glMap.getSource(id).setData(data);
    else glMap.addSource(id, { type: "geojson", data });
  }
  if (glMap.getLayer("replay-ways")) return;
  const dark = document.documentElement.dataset.theme === "dark";
  const round = { "line-cap": "round", "line-join": "round" };
  glMap.addLayer({
    id: "replay-ways",
    type: "line",
    source: "replay-ways",
    layout: round,
    paint: { "line-color": ["get", "color"], "line-width": 3, "line-opacity": dark ? 0.35 : 0.28 },
  });
  glMap.addLayer({
    id: "replay-trail-casing",
    type: "line",
    source: "replay-trail",
    layout: round,
    paint: { "line-color": dark ? "#0e1113" : "#ffffff", "line-width": 8, "line-opacity": 0.85 },
  });
  glMap.addLayer({
    id: "replay-trail",
    type: "line",
    source: "replay-trail",
    layout: round,
    paint: { "line-color": ["get", "color"], "line-width": 4.5 },
  });
  glMap.addLayer({
    id: "replay-stops",
    type: "circle",
    source: "replay-stops",
    paint: {
      "circle-radius": 6,
      "circle-color": dark ? "#5ed1c2" : "#0f766e",
      "circle-stroke-color": dark ? "#0e1113" : "#ffffff",
      "circle-stroke-width": 2.5,
    },
  });
  glMap.addLayer({
    id: "replay-stop-names",
    type: "symbol",
    source: "replay-stops",
    layout: {
      "text-field": ["get", "name"],
      "text-font": ["Noto Sans Regular"],
      "text-size": 12.5,
      "text-offset": [0, 1.1],
      "text-anchor": "top",
      "text-max-width": 12,
    },
    paint: {
      "text-color": dark ? "#e7eaed" : "#14181d",
      "text-halo-color": dark ? "#0e1113" : "#ffffff",
      "text-halo-width": 1.6,
    },
  });
}

// The scrubber's track: each way in its mode's colour, stops as gaps, all at their playback length.
function renderRibbon() {
  const length = replay.beats.length;
  $("replay-ribbon").innerHTML = replay.beats.list
    .filter((beat) => beat.kind === "move")
    .map(
      (beat) =>
        `<span style="left:${(beat.at / length) * 100}%;width:${(beat.length / length) * 100}%;--mode:${color(beat.move.mode)}"></span>`,
    )
    .join("");
}

function mountMap() {
  if (glMap) return;
  glMap = new maplibregl.Map({
    container: "replay-map",
    style: freeMapStyle(),
    // The style credits OpenFreeMap and OpenStreetMap itself; the credit sits clear of the controls.
    attributionControl: false,
    // A video copies each frame of the map (see recordVideo).
    preserveDrawingBuffer: true,
    center: [-98.35, 39.5],
    zoom: 3,
  });
  glMap.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  glMap.addControl(new maplibregl.AttributionControl({ compact: true }), "top-right");
  const dot = document.createElement("div");
  dot.className = "replay-dot";
  head = new maplibregl.Marker({ element: dot });
  glMap.on("style.load", () => {
    greyMap(glMap);
    if (!replay) return;
    addLayers();
    draw();
  });
  // A drag or zoom by hand stops the camera following.
  glMap.on("dragstart", () => setFollow(false));
  glMap.on("wheel", () => setFollow(false));
  new MutationObserver(() => glMap.setStyle(freeMapStyle())).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
}

function setFollow(on) {
  if (!replay) return;
  replay.follow = on;
  $("replay-follow").setAttribute("aria-pressed", String(on));
  if (on) replay.zoom = glMap.getZoom();
  else if (!replay.playing) fitDay();
}

function setSpeed(speed) {
  replay.speed = speed;
  for (const button of document.querySelectorAll("#replay-speeds button"))
    button.setAttribute("aria-pressed", String(+button.dataset.speed === speed));
}

// The replay dialog, made and wired on first use.
export function replayDialog() {
  let dialog = $("replay-dialog");
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.id = "replay-dialog";
  dialog.className = "replay";
  dialog.setAttribute("aria-labelledby", "replay-title");
  dialog.innerHTML = DIALOG;
  document.body.append(dialog);
  dialog.addEventListener("close", closeReplay);
  // A shared link's page is the replay: there is nothing behind it to close back to.
  dialog.addEventListener("cancel", (event) => replay?.standalone && event.preventDefault());
  $("replay-close").onclick = () => dialog.close();
  $("replay-share").onclick = () => replay?.share?.();
  $("replay-video").onclick = () => (replay?.recording ? stopRecording(true) : recordVideo());
  $("replay-play").onclick = () => setPlaying(!replay.playing);
  $("replay-follow").onclick = () => setFollow(!replay.follow);
  $("replay-speeds").onclick = (event) => {
    const button = event.target.closest("[data-speed]");
    if (button) setSpeed(+button.dataset.speed);
  };
  const scrub = $("replay-scrub");
  let resume = false;
  scrub.addEventListener("pointerdown", () => {
    resume = replay.playing;
    setPlaying(false);
  });
  scrub.addEventListener("input", () => seek((scrub.value / 1000) * replay.beats.length));
  scrub.addEventListener("change", () => resume && setPlaying(true));
  // Space plays and pauses (a focused button keeps it, to press itself); ← and → step 5%, as the scrubber does.
  dialog.addEventListener("keydown", (event) => {
    if (!replay || event.target.closest("#replay-share-panel")) return;
    if (event.key === " " && !event.target.closest("button")) {
      event.preventDefault();
      setPlaying(!replay.playing);
    } else if ((event.key === "ArrowLeft" || event.key === "ArrowRight") && event.target !== scrub) {
      event.preventDefault();
      seek(replay.time + (event.key === "ArrowLeft" ? -1 : 1) * replay.beats.length * 0.05);
    }
  });
  return dialog;
}

// Pause, as a panel over the replay (the Share panel) wants.
export function pauseReplay() {
  setPlaying(false);
}

// Play a replay in the replay dialog. `standalone` is a shared link's page (no closing); with `share`, a
// Share button calls it. Returns why it cannot play, or "" once it does.
export function openReplay(data, { title, standalone = false, share = null } = {}) {
  if (!window.maplibregl) return "The map library could not load. Check your internet connection.";
  const plan = beats(data);
  if (!plan.list.length) return "Nothing moved this day, so there is nothing to replay.";
  const dialog = replayDialog();
  $("replay-title").textContent = title;
  $("replay-summary").textContent = `${distanceText(data.distance_km)} · ${durationText(data.moving_minutes)} moving`;
  $("replay-clock-label").textContent = data.elapsed ? "Elapsed" : "Time";
  $("replay-close").hidden = standalone;
  $("replay-share").hidden = !share;
  $("replay-video").hidden = !videoFormat();
  // Your own replay is the whole day; a link's page shows only what the link does.
  $("replay-video").title = standalone
    ? "Save the replay as a video"
    : "Save the replay as a video. It shows the whole day, Home too: to post one without, make a link and save it from the link's page.";
  dialog.classList.toggle("standalone", standalone);
  dialog.showModal();
  mountMap();
  glMap.resize();
  replay = {
    data,
    standalone,
    share,
    beats: plan,
    origin: Date.parse(data.start || 0),
    time: 0,
    index: 0,
    playing: false,
    follow: true,
    speed: 1,
    zoom: 13,
    step: 0,
  };
  setSpeed(1);
  setFollow(true);
  renderRibbon();
  if (glMap.isStyleLoaded()) addLayers();
  // The dot needs a place before it goes on the map.
  draw();
  head.addTo(glMap);
  fitDay();
  setPlaying(false);
  // Start once the map has settled on the day.
  setTimeout(() => replay?.data === data && dialog.open && !$("replay-share-panel") && setPlaying(true), 900);
  return "";
}

function closeReplay() {
  stopRecording(false);
  setPlaying(false);
  $("replay-share-panel")?.remove();
  cancelAnimationFrame(frame);
  frame = 0;
  replay = null;
}

// Record the replay from the start, as it plays (at the speed chosen, following the dot), and save the
// video when it ends or Stop is pressed. Each frame is drawn on a canvas of its own: the map, the dot,
// and what the panels say (see capture.drawFrame). A browser tab in the background stops drawing, so the
// video waits for it.
function recordVideo() {
  const format = videoFormat();
  if (!format || !replay) return;
  $("replay-share-panel")?.remove();
  const source = glMap.getCanvas(),
    scale = Math.min(VIDEO_MAX_PX / Math.max(source.clientWidth, source.clientHeight), source.width / source.clientWidth);
  const even = (value) => Math.round(value / 2) * 2;
  const canvas = document.createElement("canvas");
  canvas.width = even(source.clientWidth * scale);
  canvas.height = even(source.clientHeight * scale);
  const ctx = canvas.getContext("2d");
  const recorder = new MediaRecorder(canvas.captureStream(30), { mimeType: format[0], videoBitsPerSecond: 8_000_000 });
  const chunks = [],
    name = ($("replay-title").textContent.trim() || "replay").replace(/[^\w-]+/g, "-").replace(/^-|-$/g, "").toLowerCase();
  recorder.ondataavailable = (event) => event.data.size && chunks.push(event.data);
  recorder.onstop = () => {
    if (!recorder.save) return;
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob(chunks, { type: format[0].split(";")[0] }));
    link.download = `${name || "replay"}.${format[1]}`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 60000);
  };
  replay.recording = recorder;
  $("replay-dialog").classList.add("recording");
  $("replay-video").querySelector("span").textContent = "Stop";
  setFollow(true);
  seek(0);
  setPlaying(true);
  recorder.start(1000);
  const paint = () => {
    if (replay?.recording !== recorder) return;
    const status = $("replay-status");
    drawFrame(
      ctx,
      source,
      {
        dot: glMap.project(head.getLngLat()),
        color: head.getElement().style.getPropertyValue("--mode") || NO_MODE_COLOR,
        title: $("replay-title").textContent,
        summary: $("replay-summary").textContent,
        status: status.querySelector("strong")?.textContent || "",
        detail: status.querySelector("small")?.textContent || "",
        stats: [...document.querySelectorAll(".replay-stats > div")].map((item) => [
          item.querySelector("dt").textContent,
          item.querySelector("dd").textContent,
        ]),
      },
      canvas.width / source.clientWidth,
    );
    requestAnimationFrame(paint);
  };
  paint();
}

// Stop recording, and save what was recorded (or not, when the replay is closed).
function stopRecording(save) {
  const recorder = replay?.recording;
  if (!recorder) return;
  replay.recording = null;
  recorder.save = save;
  recorder.stop();
  $("replay-dialog").classList.remove("recording");
  $("replay-video").querySelector("span").textContent = "Video";
}

function seek(time) {
  replay.time = Math.min(replay.beats.length, Math.max(0, time));
  replay.index = 0;
  draw();
  if (replay.follow) glMap.jumpTo({ center: head.getLngLat() });
}
