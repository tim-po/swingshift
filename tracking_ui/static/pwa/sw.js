/* Loopyard PWA service worker — QoL R6 / M2.
 *
 * Goal: fast, offline-friendly loads of the app SHELL + static assets, WITHOUT
 * ever standing between the UI and live data.
 *
 *   - LIVE data + sockets ALWAYS hit the network, never the cache: any /api/*,
 *     /ws/*, /__gate/*, or /mac-origin/* request is left entirely to the browser
 *     (the SW does not call respondWith), so loop data + the terminal WebSocket
 *     stay live. (WebSocket upgrades aren't dispatched to `fetch` at all — the
 *     /ws/ guard is belt-and-suspenders for any polling fallback.)
 *   - Navigations / documents: NETWORK-FIRST — always try the network so a fresh
 *     shell wins online, fall back to the cached shell when offline.
 *   - Static assets (vendored xterm.js, icons, manifest): STALE-WHILE-REVALIDATE
 *     — serve instantly from cache, refresh in the background.
 *   - Only GET is ever cached; mutations pass straight through.
 *
 * Bump VERSION to roll the cache; `activate` deletes every cache from an older
 * version. Served from "/" so its scope is the whole origin.
 */
const VERSION = 'loopyard-v6-1';
const SHELL = VERSION + '-shell';
const ASSETS = VERSION + '-assets';

// Precached on install. Added individually (allSettled) so a URL that 404s in a
// given deployment — e.g. /loops is gate-only and absent from the clean release
// — can't abort the whole install.
const PRECACHE = [
  '/',
  '/loops',
  '/manifest.webmanifest',
  '/static/vendor/xterm/xterm.js',
  '/static/vendor/xterm/xterm.css',
  '/static/vendor/xterm/addon-fit.js',
  '/static/pwa/loopyard-192.png',
  '/static/pwa/loopyard-512.png',
];

const LIVE_PREFIXES = ['/api/', '/ws/', '/__gate/', '/mac-origin/'];

function isLive(pathname) {
  for (let i = 0; i < LIVE_PREFIXES.length; i++) {
    if (pathname.startsWith(LIVE_PREFIXES[i])) return true;
  }
  return false;
}

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL);
    await Promise.allSettled(
      PRECACHE.map((u) => cache.add(new Request(u, { cache: 'reload' })))
    );
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;                     // never cache mutations
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;      // same-origin only
  if (isLive(url.pathname)) return;                     // live data/sockets: hands off

  const wantsHtml = req.mode === 'navigate' ||
    (req.headers.get('accept') || '').indexOf('text/html') !== -1;

  if (wantsHtml) {
    // NETWORK-FIRST: fresh shell online, cached shell offline.
    event.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        const cache = await caches.open(SHELL);
        cache.put(req, fresh.clone());
        return fresh;
      } catch (err) {
        const cache = await caches.open(SHELL);
        return (await cache.match(req)) ||
               (await cache.match('/')) ||
               new Response('Offline — Loopyard shell is not cached yet.',
                 { status: 503, headers: { 'Content-Type': 'text/plain' } });
      }
    })());
    return;
  }

  // STALE-WHILE-REVALIDATE for static assets.
  event.respondWith((async () => {
    const cache = await caches.open(ASSETS);
    const cached = await cache.match(req);
    const network = fetch(req).then((res) => {
      if (res && res.ok) cache.put(req, res.clone());
      return res;
    }).catch(() => null);
    return cached || (await network) || new Response('', { status: 504 });
  })());
});
