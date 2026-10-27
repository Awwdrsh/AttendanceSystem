/*
 * Hajiri service worker: makes the app installable and lets it open without a connection.
 *
 * - Only static files (/static/...) and the offline page are cached. Pages, partials (/_/...), API responses and the
 *   health check are never served from cache: pay, attendance and personal data always come fresh from the server.
 * - Page navigations are network-first; when the network is unreachable the last page shell is shown with the
 *   offline banner, or a plain offline message.
 * - Static files are cached on first use (cache-first); a new release changes VERSION and clears the old cache.
 */
const VERSION = 'hajiri-django-v1';
const SHELL = ['/manifest.webmanifest', '/favicon.svg', '/icon-192.png', '/icon-512.png', '/static/css/hajiri.css', '/static/js/app.js',
  '/static/js/theme.js', '/static/vendor/htmx/htmx.min.js', '/static/vendor/alpine/alpine.min.js', '/static/vendor/tabler/tabler-icons.min.css'];
const OFFLINE = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Offline</title>' +
  '<body style="font-family:system-ui;padding:40px;text-align:center;color:#161c26;background:#f3f5f7"><h1 style="font-size:20px">Hajiri is offline</h1>' +
  '<p>Check your connection and try again.</p></body>';

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
  if (url.pathname.startsWith('/_/') || url.pathname.startsWith('/api/') || url.pathname === '/health' || url.pathname === '/sw.js') return;

  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => new Response(OFFLINE, { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
    return;
  }

  if (url.pathname.startsWith('/static/') || /\.(?:svg|png|woff2?|webmanifest)$/.test(url.pathname)) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok && res.type === 'basic') { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
        return res;
      })),
    );
  }
});
