// Weather icons, one per sky the server names (see domain/weather.py), drawn on a 24-unit grid.
const sun = (cx, cy, r, rays) =>
  `<g class="wi-sun"><circle cx="${cx}" cy="${cy}" r="${r}"/>` +
  (rays
    ? `<path fill="none" d="${[0, 45, 90, 135, 180, 225, 270, 315]
        .map((angle) => {
          const a = (angle * Math.PI) / 180;
          const [x1, y1] = [cx + Math.cos(a) * (r + 2), cy + Math.sin(a) * (r + 2)];
          const [x2, y2] = [cx + Math.cos(a) * (r + 3.6), cy + Math.sin(a) * (r + 3.6)];
          return `M${x1.toFixed(2)} ${y1.toFixed(2)}L${x2.toFixed(2)} ${y2.toFixed(2)}`;
        })
        .join("")}"/>`
    : "") +
  "</g>";
const moon = (shift = "") =>
  `<path class="wi-moon"${shift} d="M17.5 13.2A6.6 6.6 0 0 1 9.3 5a6.6 6.6 0 1 0 8.2 8.2Z"/>`;
// A cloud whose base runs from x=5 to x=19 at y = base.
const cloud = (base = 18) =>
  `<path class="wi-cloud" d="M7.2 ${base}h10a3.8 3.8 0 0 0 .5-7.57 5.3 5.3 0 0 0-10.2 1.38A3.1 3.1 0 0 0 7.2 ${base}Z"/>`;

const icons = {
  clear: sun(12, 12, 4.4, true),
  "clear-night": moon(' transform="translate(1.5 1.5)"'),
  partly: sun(8.6, 8.4, 3.2, true) + cloud(19.5),
  "partly-night": moon(' transform="translate(-2.8 -2.4) scale(.82)"') + cloud(19.5),
  cloudy: cloud(18),
  fog: cloud(14.5) + '<path class="wi-mist" d="M5 17.5h14M7 20.5h10"/>',
  drizzle: cloud(15) + '<path class="wi-drop" d="M8.5 18.2v.6M12 19.4v.6M15.5 18.2v.6M10.2 21.2v.6M13.8 21.2v.6"/>',
  rain: cloud(15) + '<path class="wi-drop" d="m8.6 17.6-1 2.8M12.4 17.6l-1 2.8M16.2 17.6l-1 2.8"/>',
  snow:
    cloud(15) +
    '<g class="wi-flake"><circle cx="8.4" cy="18.4" r=".9"/><circle cx="12" cy="20.6" r=".9"/><circle cx="15.6" cy="18.4" r=".9"/><circle cx="10.2" cy="22" r=".6"/><circle cx="13.8" cy="22" r=".6"/></g>',
  storm: cloud(15) + '<path class="wi-bolt" d="M12.8 15.2 10.2 19h3l-1.8 3.6"/>',
};

export function weatherIcon(name) {
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.cloudy}</svg>`;
}
