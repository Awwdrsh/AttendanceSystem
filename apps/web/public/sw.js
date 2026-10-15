/*
 * Hajiri service worker: makes the app installable and lets it open without a connection.
 *
 * - Only the app shell and static files are cached. API responses (/api/...) and the health check are never cached or
 *   served from cache: pay, attendance and personal data always come fresh from the server.
 * - Page navigations are network-first, so a new release is picked up straight away; the cached index.html is only
 *   used when the network is unreachable.
 * - Built assets (/assets/*) have content hashes in their names, so they are cached on first use (cache-first).
 */
const VERSION = 'hajiri-shell-v1';
const SHELL = ['/', '/manifest.webmanifest', '/favicon.svg', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))).then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname === '/health' || url.pathname === '/sw.js') return;

  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put('/', copy)); }
          return res;
        })
        .catch(() => caches.match('/').then((r) => r || new Response('Hajiri is offline. Check your connection and try again.', { status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' } }))),
    );
    return;
  }

  if (url.pathname.startsWith('/assets/') || /\.(?:svg|png|woff2?|webmanifest)$/.test(url.pathname)) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok && res.type === 'basic') { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
        return res;
      })),
    );
  }
});
