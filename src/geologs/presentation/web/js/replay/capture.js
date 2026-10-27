// Pictures of a replay: the 1200 × 630 card chat apps show for a shared link, and the overlay drawn on each
// frame of a replay's video. Both are drawn on a 2D canvas over a copy of the map, so the map's own
// canvas has to keep its pixels (preserveDrawingBuffer).
import { distanceText, durationText } from "../core/format.js";
import { freeMapStyle, greyMap } from "../map/free-map.js";
import { modeInfo } from "../ui/icons.js";

const CARD = { width: 1200, height: 630 };
const FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
const TEAL = "#0f766e";
const NO_MODE_COLOR = "#176d69";
// How long a card may wait for the map's tiles before it is given up.
const CARD_TIMEOUT_MS = 20000;

function roundedRect(ctx, x, y, width, height, radius) {
  ctx.beginPath();
  ctx.roundRect(x, y, width, height, radius);
}

// Text cut to fit `width`, with an ellipsis.
function fitted(ctx, text, width) {
  if (ctx.measureText(text).width <= width) return text;
  let cut = text;
  while (cut && ctx.measureText(cut + "…").width > width) cut = cut.slice(0, -1);
  return cut + "…";
}

// The map's credit, which a picture of it must carry too.
export function drawCredit(ctx, width, height, scale = 1) {
  ctx.font = `${12 * scale}px ${FONT}`;
  const text = "© OpenStreetMap contributors · OpenFreeMap",
    pad = 6 * scale,
    textWidth = ctx.measureText(text).width;
  ctx.fillStyle = "rgb(255 255 255 / 0.8)";
  ctx.fillRect(width - textWidth - pad * 2, height - 20 * scale, textWidth + pad * 2, 20 * scale);
  ctx.fillStyle = "#3b4148";
  ctx.textBaseline = "middle";
  ctx.fillText(text, width - textWidth - pad, height - 10 * scale);
}

// The card for a shared link: the link's own replay (never more of the day) on a light map, its title and
// totals on a band across the bottom. Resolves to a JPEG Blob, or null when the map cannot be drawn.
export function drawCard(data, title) {
  if (!window.maplibregl) return Promise.resolve(null);
  const box = document.createElement("div");
  box.style.cssText = `position:fixed;left:-${CARD.width + 100}px;top:0;width:${CARD.width}px;height:${CARD.height}px`;
  document.body.append(box);
  const map = new maplibregl.Map({
    container: box,
    style: freeMapStyle(false),
    interactive: false,
    attributionControl: false,
    preserveDrawingBuffer: true,
    pixelRatio: 1,
    fadeDuration: 0,
  });
  const done = (result) => {
    map.remove();
    box.remove();
    return result;
  };
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(done(null)), CARD_TIMEOUT_MS);
    map.on("error", () => {});
    map.once("load", () => {
      greyMap(map, false);
      const ways = data.moves.map((move) => ({
        type: "Feature",
        properties: { color: modeInfo[move.mode]?.color || NO_MODE_COLOR },
        geometry: { type: "LineString", coordinates: move.track.map((point) => [point[1], point[2]]) },
      }));
      const stops = data.stays.map((stay) => ({ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [stay.lon, stay.lat] } }));
      map.addSource("ways", { type: "geojson", data: { type: "FeatureCollection", features: ways } });
      map.addSource("stops", { type: "geojson", data: { type: "FeatureCollection", features: stops } });
      const round = { "line-cap": "round", "line-join": "round" };
      map.addLayer({ id: "casing", type: "line", source: "ways", layout: round, paint: { "line-color": "#fff", "line-width": 9 } });
      map.addLayer({ id: "ways", type: "line", source: "ways", layout: round, paint: { "line-color": ["get", "color"], "line-width": 5 } });
      map.addLayer({
        id: "stops",
        type: "circle",
        source: "stops",
        paint: { "circle-radius": 7, "circle-color": TEAL, "circle-stroke-color": "#fff", "circle-stroke-width": 3 },
      });
      const points = ways.flatMap((way) => way.geometry.coordinates);
      const lons = points.map((point) => point[0]),
        lats = points.map((point) => point[1]);
      map.fitBounds(
        [
          [Math.min(...lons), Math.min(...lats)],
          [Math.max(...lons), Math.max(...lats)],
        ],
        { padding: { top: 70, bottom: 190, left: 90, right: 90 }, maxZoom: 15.5, duration: 0 },
      );
      map.once("idle", () => {
        clearTimeout(timer);
        const canvas = document.createElement("canvas");
        canvas.width = CARD.width;
        canvas.height = CARD.height;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(map.getCanvas(), 0, 0, CARD.width, CARD.height);
        // The band: title, then the totals, on white fading up into the map.
        const fade = ctx.createLinearGradient(0, CARD.height - 230, 0, CARD.height);
        fade.addColorStop(0, "rgb(255 255 255 / 0)");
        fade.addColorStop(0.45, "rgb(255 255 255 / 0.94)");
        fade.addColorStop(1, "#fff");
        ctx.fillStyle = fade;
        ctx.fillRect(0, CARD.height - 230, CARD.width, 230);
        ctx.textBaseline = "alphabetic";
        ctx.fillStyle = "#14181d";
        ctx.font = `650 52px ${FONT}`;
        ctx.fillText(fitted(ctx, title, CARD.width - 120), 60, CARD.height - 92);
        ctx.fillStyle = "#525a64";
        ctx.font = `500 30px ${FONT}`;
        ctx.fillText(`${distanceText(data.distance_km)} · ${durationText(data.moving_minutes)} moving`, 60, CARD.height - 46);
        // A replay mark, top left: a play button in GeoLogs' teal.
        roundedRect(ctx, 48, 40, 150, 48, 24);
        ctx.fillStyle = TEAL;
        ctx.fill();
        ctx.fillStyle = "#fff";
        ctx.beginPath();
        ctx.moveTo(70, 52);
        ctx.lineTo(70, 76);
        ctx.lineTo(90, 64);
        ctx.fill();
        ctx.font = `650 22px ${FONT}`;
        ctx.textBaseline = "middle";
        ctx.fillText("Replay", 102, 65);
        drawCredit(ctx, CARD.width, CARD.height);
        canvas.toBlob((blob) => resolve(done(blob)), "image/jpeg", 0.86);
      });
    });
  });
}

export async function blobBase64(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let text = "";
  for (let start = 0; start < bytes.length; start += 0x8000)
    text += String.fromCharCode(...bytes.subarray(start, start + 0x8000));
  return btoa(text);
}

// A video format this browser can record: MP4 where it can (it plays everywhere), else WebM.
export function videoFormat() {
  if (!window.MediaRecorder || !HTMLCanvasElement.prototype.captureStream) return null;
  return (
    [
      ["video/mp4;codecs=avc1.42E01E", "mp4"],
      ["video/mp4", "mp4"],
      ["video/webm;codecs=vp9", "webm"],
      ["video/webm", "webm"],
    ].find(([type]) => MediaRecorder.isTypeSupported(type)) || null
  );
}

// One frame of a replay's video: the map as it is, the dot, and what the replay's panels say.
// `scale` is the video's pixels per CSS pixel of the map.
export function drawFrame(ctx, mapCanvas, { dot, color, title, summary, status, detail, stats }, scale) {
  const { width, height } = ctx.canvas;
  ctx.drawImage(mapCanvas, 0, 0, width, height);
  if (dot) {
    ctx.beginPath();
    ctx.arc(dot.x * scale, dot.y * scale, 9 * scale, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = 3 * scale;
    ctx.strokeStyle = "#fff";
    ctx.stroke();
  }
  const pad = 16 * scale;
  // The title, top left.
  ctx.font = `650 ${18 * scale}px ${FONT}`;
  const titleTextWidth = ctx.measureText(title).width;
  ctx.font = `${13 * scale}px ${FONT}`;
  const titleWidth = Math.max(titleTextWidth, ctx.measureText(summary).width);
  roundedRect(ctx, pad, pad, titleWidth + 36 * scale, 64 * scale, 18 * scale);
  ctx.fillStyle = "rgb(255 255 255 / 0.94)";
  ctx.fill();
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = "#14181d";
  ctx.font = `650 ${18 * scale}px ${FONT}`;
  ctx.fillText(title, pad + 18 * scale, pad + 30 * scale);
  ctx.fillStyle = "#525a64";
  ctx.font = `${13 * scale}px ${FONT}`;
  ctx.fillText(summary, pad + 18 * scale, pad + 50 * scale);
  // What is happening, and the day so far, bottom left.
  const panelWidth = Math.min(360 * scale, width - pad * 2),
    panelHeight = 116 * scale,
    top = height - panelHeight - pad;
  roundedRect(ctx, pad, top, panelWidth, panelHeight, 18 * scale);
  ctx.fillStyle = "rgb(255 255 255 / 0.94)";
  ctx.fill();
  ctx.fillStyle = "#14181d";
  ctx.font = `650 ${15 * scale}px ${FONT}`;
  ctx.fillText(fitted(ctx, status, panelWidth - 36 * scale), pad + 18 * scale, top + 30 * scale);
  ctx.fillStyle = "#525a64";
  ctx.font = `${12.5 * scale}px ${FONT}`;
  ctx.fillText(fitted(ctx, detail, panelWidth - 36 * scale), pad + 18 * scale, top + 50 * scale);
  stats.forEach(([label, value], index) => {
    const x = pad + 18 * scale + index * ((panelWidth - 36 * scale) / 3);
    ctx.fillStyle = "#525a64";
    ctx.font = `600 ${11 * scale}px ${FONT}`;
    ctx.fillText(label.toUpperCase(), x, top + 80 * scale);
    ctx.fillStyle = "#14181d";
    ctx.font = `650 ${17 * scale}px ${FONT}`;
    ctx.fillText(value, x, top + 102 * scale);
  });
  drawCredit(ctx, width, height, scale);
}
