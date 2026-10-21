// The service worker that lets GeoLogs open without a connection.
//
// Online, every request goes to the network as usual; what comes back is kept, so that offline the
// app (its page, styles, modules and map libraries) still opens, showing the views you last looked
// at. Of your data, only what those views read is kept (DATA_PATHS), in this browser alone; it is
// deleted when you sign out (js/views/account.js deletes DATA) and whenever the server says you are
// signed out. Nothing that writes, and nothing that only makes sense live (imports, exports, searches,
// tracker notices), is kept.
const SHELL = "geologs-shell-v1";
const DATA = "geologs-data-v1";
const DATA_PATHS = new Set([
  "/api/me", "/api/status", "/api/current", "/api/settings", "/api/day", "/api/timeline", "/api/observations", "/api/coverage",
  "/api/places", "/api/place-labels", "/api/place-overview", "/api/reminders", "/api/reminder-history", "/api/events",
  "/api/daily-summary", "/api/daily-summaries", "/api/period-summary", "/api/insights", "/api/ambient",
]);
// Libraries the page loads from a CDN (pinned versions, so a kept copy never goes stale).
const LIBRARIES = ["https://unpkg.com/"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((cache) => cache.add("/")).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((name) => ![SHELL, DATA].includes(name)).map((name) => caches.delete(name))))
      .then(() => self.clients.claim()),
  );
});

function kept(request, url) {
  if (request.method !== "GET") return null;
  if (url.origin === location.origin) {
    if (url.pathname === "/" || url.pathname.startsWith("/css/") || url.pathname.startsWith("/js/") || url.pathname.startsWith("/icons/"))
      return SHELL;
    return DATA_PATHS.has(url.pathname) ? DATA : null;
  }
  return LIBRARIES.some((prefix) => url.href.startsWith(prefix)) ? SHELL : null;
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // The page itself, whatever its #part or query: one copy.
  const page = event.request.mode === "navigate" && url.origin === location.origin && url.pathname === "/";
  const name = page ? SHELL : kept(event.request, url);
  if (!name) return;
  const key = page ? "/" : event.request;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.status === 401) caches.delete(DATA);
        else if (response.ok) {
          const copy = response.clone();
          caches.open(name).then((cache) => cache.put(key, copy));
        }
        return response;
      })
      .catch(async () => {
        const saved = await caches.match(key, { cacheName: name });
        if (!saved) throw new Error("offline, and not kept");
        // Marked, so the page can say it is showing what was kept rather than live data.
        if (name !== DATA) return saved;
        const headers = new Headers(saved.headers);
        headers.set("X-GeoLogs-Offline", "1");
        return new Response(await saved.blob(), { status: saved.status, headers });
      }),
  );
});
