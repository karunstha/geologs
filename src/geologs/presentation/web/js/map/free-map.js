// OpenFreeMap's vector styles in the page's greys, for the MapLibre maps (the new Home's, and the replay's).
export const FREE_MAP = "https://tiles.openfreemap.org/styles/";
export const FREE_MAP_CREDIT =
  '&copy; <a href="https://openfreemap.org" target="_blank" rel="noopener">OpenFreeMap</a> &copy; <a href="https://www.openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors';
// Positron's land, water and buildings, in the page's greys (the dark style is already dark).
export const LIGHT_GREYS = {
  background: "#e4e4e1",
  park: "#dcdcd8",
  water: "#cfd0cf",
  landuse_residential: "#dfdfdb",
  landcover_wood: "#d9d9d5",
  building: "#d6d6d2",
};

export function darkTheme() {
  return document.documentElement.dataset.theme === "dark";
}

// Positron, or its dark style, for the page's theme (or the one asked for).
export function freeMapStyle(dark = darkTheme()) {
  return FREE_MAP + (dark ? "dark" : "positron");
}

// Recolour a loaded light style's land and water in LIGHT_GREYS.
export function greyMap(glMap, dark = darkTheme()) {
  if (dark) return;
  for (const layer of glMap.getStyle().layers) {
    const grey = LIGHT_GREYS[layer.id];
    if (grey) glMap.setPaintProperty(layer.id, layer.type === "background" ? "background-color" : "fill-color", grey);
  }
}
