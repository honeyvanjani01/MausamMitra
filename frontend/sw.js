// @MausamMitra service worker — caches the app shell (HTML/CSS/JS/icons) so
// the interface loads instantly and works offline; API calls to your
// FastAPI backend always go to the network since weather data must be live.
//
// v4 change: switched the app-shell strategy from cache-first to
// network-first-with-cache-fallback. Cache-first meant that once a file was
// cached, the browser would keep serving that exact version FOREVER and
// never even check the network again — not fixed by a hard refresh, only by
// clearing site data or bumping this cache name. That silently broke every
// earlier round of UI fixes from ever actually showing up. Network-first
// still gives instant offline loads (falls back to cache when the network
// is unreachable) but always prefers the live file when online, which is
// what you want during active development anyway.
const CACHE_NAME = "mausammitra-shell-v4";
const SHELL_FILES = [
  "./index.html",
  "./styles.css",
  "./app.js",
  "./manifest.json",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Never cache API calls (chat/forecast/alerts must always be fresh/live).
  const isApiLike = ["/chat", "/forecast", "/alerts", "/geocode", "/reset", "/health"]
    .some((p) => url.pathname.includes(p));
  if (isApiLike) return;

  // App-shell files: network-first for always-fresh loads while online,
  // falling back to the cached copy only when the network fails (offline).
  event.respondWith(
    fetch(event.request).then((resp) => {
      const copy = resp.clone();
      caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy)).catch(() => {});
      return resp;
    }).catch(() => caches.match(event.request))
  );
});
