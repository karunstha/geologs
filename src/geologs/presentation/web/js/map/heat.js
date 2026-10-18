// A heat layer for Leaflet: soft blobs where places are, hotter where you went more, drawn on one
// canvas and coloured cool to hot. Points are [lat, lon, weight] with weight from 0 to 1.
const STOPS = [
  [0, "rgba(37, 99, 235, 0)"],
  [0.25, "#2563eb"],
  [0.5, "#14b8a6"],
  [0.7, "#facc15"],
  [0.85, "#f97316"],
  [1, "#dc2626"],
];
let palette;

function colours() {
  if (palette) return palette;
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 1;
  const context = canvas.getContext("2d");
  const gradient = context.createLinearGradient(0, 0, 256, 0);
  for (const [at, colour] of STOPS) gradient.addColorStop(at, colour);
  context.fillStyle = gradient;
  context.fillRect(0, 0, 256, 1);
  palette = context.getImageData(0, 0, 256, 1).data;
  return palette;
}

export function heatLayer(points = [], options = {}) {
  const Layer = L.Layer.extend({
    onAdd(map) {
      this._map = map;
      this._canvas = L.DomUtil.create("canvas", "heat-layer leaflet-zoom-hide");
      map.getPanes().overlayPane.append(this._canvas);
      map.on("moveend zoomend resize", this._draw, this);
      this._draw();
    },
    onRemove(map) {
      this._canvas.remove();
      map.off("moveend zoomend resize", this._draw, this);
    },
    setPoints(next) {
      points = next;
      if (this._map) this._draw();
      return this;
    },
    _draw() {
      const map = this._map;
      const size = map.getSize();
      const canvas = this._canvas;
      // A hidden map has no size; it is drawn once it is shown.
      if (!size.x || !size.y) return;
      const ratio = window.devicePixelRatio || 1;
      canvas.width = size.x * ratio;
      canvas.height = size.y * ratio;
      canvas.style.width = `${size.x}px`;
      canvas.style.height = `${size.y}px`;
      L.DomUtil.setPosition(canvas, map.containerPointToLayerPoint([0, 0]));
      const context = canvas.getContext("2d");
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, size.x, size.y);
      // Blobs grow a little as you zoom in, so a street's places read apart.
      const radius = (options.radius || 22) * Math.min(1.8, Math.max(0.7, (map.getZoom() - 8) / 6 + 0.8));
      const view = map.getBounds().pad(0.2);
      for (const [lat, lon, weight] of points) {
        if (!view.contains([lat, lon])) continue;
        const at = map.latLngToContainerPoint([lat, lon]);
        const blob = context.createRadialGradient(at.x, at.y, 0, at.x, at.y, radius);
        blob.addColorStop(0, `rgba(0,0,0,${Math.min(1, 0.18 + weight * 0.82)})`);
        blob.addColorStop(1, "rgba(0,0,0,0)");
        context.fillStyle = blob;
        context.fillRect(at.x - radius, at.y - radius, radius * 2, radius * 2);
      }
      // The blobs' darkness becomes colour: faint is cool, dense is hot.
      const image = context.getImageData(0, 0, canvas.width, canvas.height);
      const pixels = image.data;
      const ramp = colours();
      for (let index = 3; index < pixels.length; index += 4) {
        const alpha = pixels[index];
        if (!alpha) continue;
        const offset = alpha * 4;
        pixels[index - 3] = ramp[offset];
        pixels[index - 2] = ramp[offset + 1];
        pixels[index - 1] = ramp[offset + 2];
        pixels[index] = Math.min(230, alpha * 1.6);
      }
      context.putImageData(image, 0, 0);
    },
  });
  return new Layer();
}
