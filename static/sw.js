// Deliberately conservative: this app is a live restaurant floor/reservation
// dashboard, so caching dynamic pages or /api/* responses risks staff
// seeing stale table/booking state and double-booking a table. This service
// worker only exists to make the PWA installable and keep the static
// app shell (CSS, icons, manifest) available if the network hiccups --
// every HTML page and API call always goes straight to the network.
const CACHE_NAME = "siena-shell-v1";
const SHELL_ASSETS = [
  "/static/style.css",
  "/static/siena-logo.png",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/apple-touch-icon.png",
  "/static/manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || !SHELL_ASSETS.includes(url.pathname)) {
    return; // not a shell asset -- let the browser handle it normally (network)
  }
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
