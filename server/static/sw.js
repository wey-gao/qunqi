const CACHE = 'aichat-v1';
const ASSETS = ['/manifest.webmanifest', '/icon-192.png', '/icon-512.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== location.origin) return;
  e.respondWith(
    fetch(req).then(r => {
      if (req.destination === 'image' || req.url.endsWith('.webmanifest')) {
        const cp = r.clone(); caches.open(CACHE).then(c => c.put(req, cp));
      }
      return r;
    }).catch(() => caches.match(req))
  );
});
