/*
 * ROVIMEN dashboard Service Worker
 * --------------------------------
 *
 * Serves the dashboard's own static files (everything under '/static/':
 * JS, CSS, images) cache-first with a background refresh, so a repeat visit
 * paints without waiting for them. Templates load them as '?v=<build>' URLs,
 * so a deploy asks for new URLs and never gets an old file.
 *
 * Every other request goes to the network untouched: pages (auth-gated
 * HTML), the APIs, stacks, clips, timelapses, /media/ files, streams and
 * ranged video. The worker used to keep a network-first copy of all of those,
 * so stack images and clip blobs filled the browser's storage and nothing
 * ever removed them.
 *
 * Cache versioning
 * ----------------
 * The /sw.js route (dashboard/routes/misc.py) replaces the token in
 * CACHE_VERSION below with the dashboard's BUILD_VERSION, so every deploy
 * serves new bytes. The browser installs the new worker, and its activate handler
 * deletes every other `rovimen-shell-*` cache.
 *
 * This file must stay a classic script (no import/export): the pages
 * register it with navigator.serviceWorker.register('/sw.js'), and a worker
 * that fails to parse never installs or updates. tests/js/sw.test.js loads
 * it as plain script text.
 */

const CACHE_VERSION = '__BUILD_VERSION__';
const CACHE_NAME = `rovimen-shell-${CACHE_VERSION}`;

// Resources to warm the cache with on install. The HTML shell ('/') is
// deliberately NOT here — it is auth-gated and must always go to the
// network so the gate (and any 302 → /login) runs on every load. Every entry
// must exist: one 404 fails the whole install.
const SHELL_URLS = [
  '/static/dashboard-common.js',
  '/static/twilight-slider.js',
  '/static/dashboard-rms.js',
  '/static/dashboard-vdb.js',
  '/static/dashboard-archive.js',
  '/static/dashboard-sysadmin.js',
  '/static/dashboard-station.js',
  '/static/dashboard.css',
];

// ─── install ────────────────────────────────────────────────────────────────
// Pre-warm the cache with the shell. skipWaiting() promotes this SW to
// active immediately, so a returning user gets the new behavior without
// having to close every tab.
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting()),
  );
});

// ─── activate ───────────────────────────────────────────────────────────────
// Drop every old `rovimen-shell-*` cache that isn't us, then claim all open
// clients so this SW controls them on the very first load (without needing
// a manual reload).
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(
      names
        .filter((name) => name.startsWith('rovimen-shell-') && name !== CACHE_NAME)
        .map((name) => caches.delete(name)),
    );
    await self.clients.claim();
  })());
});

// ─── helpers ────────────────────────────────────────────────────────────────

function isStaticAsset(pathname) {
  return pathname.startsWith('/static/');
}

// Cache-first: return cached response if present, kick off a background
// refresh either way so the next call has fresh bytes. Only successful
// (status 200, basic/same-origin) responses are written back to the cache.
async function cacheFirstWithRevalidate(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  const networkFetch = fetch(request).then((response) => {
    if (response && response.status === 200 && response.type === 'basic') {
      cache.put(request, response.clone()).catch(() => {});
    }
    return response;
  }).catch(() => null);

  if (cached) {
    // Don't await — background revalidation, but swallow rejections so the
    // SW doesn't surface "unhandled" errors when offline.
    networkFetch.catch(() => {});
    return cached;
  }
  // Cold miss — wait for network. If network also fails, surface the error.
  const fresh = await networkFetch;
  if (fresh) return fresh;
  return new Response('Offline and not cached', { status: 503, statusText: 'Offline' });
}

// ─── fetch router ───────────────────────────────────────────────────────────
// Only same-origin GETs for /static/ files are answered here; returning
// without respondWith() hands every other request to the browser as if no
// worker were installed (Range, SSE, ETag and the auth gate all behave as
// normal).
self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  // Navigations (top-level HTML) always go to the network, so the auth gate
  // runs on every page load.
  if (request.mode === 'navigate') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (!isStaticAsset(url.pathname)) return;
  event.respondWith(cacheFirstWithRevalidate(request));
});
