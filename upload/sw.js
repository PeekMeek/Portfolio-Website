// Keeps the app's own files on the phone so it opens instantly and can be installed.
// Uploads and project data always go to GitHub directly and are never cached here.
const CACHE = "progress-app-v1";
const SHELL = ["./", "./manifest.webmanifest", "./icon-192.png", "./icon-512.png", "./apple-touch-icon.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;  // GitHub calls go straight through
  // the app itself: try the network first (so updates arrive), fall back to the saved copy offline
  e.respondWith(fetch(e.request)
    .then(r => {
      if (r.ok && SHELL.some(p => url.pathname.endsWith(p.replace("./", "/")) || url.pathname.endsWith("/upload/"))) {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return r;
    })
    .catch(() => caches.match(e.request)));
});
