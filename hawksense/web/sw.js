// HawkSense service worker: keeps the app shell available offline.
// Data is stored by the app itself in IndexedDB (see store.js), so API calls
// always go to the network and the app decides what to do when they fail.

const VERSION = 'hawksense-v11';
const IMAGES = 'hawksense-images'; // product images: content-addressed, kept across versions
// works under any prefix (e.g. https://home.example.com/hawksense/)
const API_PREFIX = new URL('api/', self.registration.scope).pathname;
const SHELL = [
  './',
  './index.html',
  './app.js',
  './store.js',
  './charts.js',
  './styles.css',
  './manifest.webmanifest',
  './icons/apple-touch-icon.png',
  './icons/favicon-32.png',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-maskable-512.png',
  './boot.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(VERSION);
    // cache: 'reload' bypasses the HTTP cache so a new version gets fresh files
    await cache.addAll(SHELL.map((url) => new Request(url, { cache: 'reload' })));
    if (!self.registration.active) await self.skipWaiting(); // first install: take over right away
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== VERSION && key !== IMAGES) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});

// Safari refuses redirected responses for navigations, so strip the flag.
async function clean(response) {
  if (!response || !response.redirected) return response;
  return new Response(await response.blob(), { status: response.status, statusText: response.statusText, headers: response.headers });
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method === 'GET' && url.origin === self.location.origin && /\/api\/images\/[0-9a-f]{64}$/.test(url.pathname)) {
    // a key never changes content: cache first (works offline); cached without the ?token=
    event.respondWith((async () => {
      const cache = await caches.open(IMAGES);
      const key = url.origin + url.pathname;
      const cached = await cache.match(key);
      if (cached) return cached;
      try {
        const res = await fetch(req);
        if (res.ok) await cache.put(key, res.clone());
        return res;
      } catch { return new Response('', { status: 504 }); }
    })());
    return;
  }
  if (req.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith(API_PREFIX)) return;

  if (req.mode === 'navigate') {
    // app shell: answer from cache instantly (works offline), refresh in the background
    event.respondWith((async () => {
      const cache = await caches.open(VERSION);
      const cached = await cache.match('./index.html');
      const network = fetch(req).then(async (res) => {
        if (res.ok) await cache.put('./index.html', (await clean(res)).clone());
        return res;
      }).catch(() => null);
      if (cached) { event.waitUntil(network); return cached; }
      return (await clean(await network)) || new Response('<h1>HawkSense is offline</h1><p>Open it once while online.</p>',
        { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
    })());
    return;
  }

  // static assets: stale-while-revalidate
  event.respondWith((async () => {
    const cache = await caches.open(VERSION);
    const cached = await cache.match(req);
    const network = fetch(req).then((res) => {
      if (res.ok && res.type === 'basic') cache.put(req, res.clone());
      return res;
    }).catch(() => null);
    if (cached) { event.waitUntil(network); return cached; }
    return (await network) || new Response('', { status: 504 });
  })());
});

// tapping an alert opens the app (on the item, when there is one)
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const item = event.notification.data && event.notification.data.item;
  const target = new URL(`./#/${item ? `item/${item}` : 'inbox'}`, self.registration.scope).href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const win = wins.find((w) => w.url.startsWith(self.registration.scope));
    if (win) { await win.focus(); return win.navigate(target); }
    return self.clients.openWindow(target);
  })());
});
