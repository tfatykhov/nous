// F092 Phase 4: companion service worker — runtime caching only.
//
// No build-time precache manifest on purpose: asset filenames are
// content-hashed, so cache-first is correct by construction for them and
// there is nothing to invalidate. Strategy per request class:
//
//   - /dashboard/v2/assets/*           cache-first   (immutable by hash)
//   - companion.html / icons / fonts   network-first (shell must update)
//   - GET /a2ui/surfaces*              network-first with cache fallback —
//     the OFFLINE SNAPSHOT CACHE (spec §14 Phase 4): offline, the last
//     known surface index + snapshots render read-only; actions fail and
//     paint inline, which is the honest offline story.
//   - /a2ui/stream (SSE), /a2ui/action, /a2ui/call, everything non-GET:
//     NEVER touched — pass through to the network.
//
// Web Push is deliberately absent (spec §12.0/§13): Telegram is the push
// channel with deep links; a second delivery channel splits notification
// state — the same failure mode §5.4 exists to prevent.

const CACHE = 'nous-companion-v1';

self.addEventListener('install', (event) => {
  // Activate the new worker immediately — the shell is network-first, so
  // there is no stale-precache handoff to protect.
  self.skipWaiting();
  event.waitUntil(Promise.resolve());
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
      await self.clients.claim();
    })(),
  );
});

function isImmutableAsset(url) {
  return url.pathname.startsWith('/dashboard/v2/assets/');
}

function isShell(url) {
  return (
    url.pathname === '/dashboard/v2/companion.html' ||
    url.pathname === '/dashboard/v2/companion.webmanifest' ||
    url.pathname.startsWith('/dashboard/v2/icons/') ||
    url.pathname === '/dashboard/v2/favicon.svg'
  );
}

function isSnapshotApi(url) {
  // Index + per-surface snapshots only. The SSE stream, actions and the
  // RPC channel must never be served from cache.
  return url.pathname === '/a2ui/surfaces' || url.pathname.startsWith('/a2ui/surfaces/');
}

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) {
    const cache = await caches.open(CACHE);
    cache.put(request, response.clone());
  }
  return response;
}

async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    throw err;
  }
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (isImmutableAsset(url)) {
    event.respondWith(cacheFirst(request));
  } else if (isShell(url) || isSnapshotApi(url)) {
    event.respondWith(networkFirst(request));
  }
  // Everything else (SSE, actions, /a2ui/call, dashboard API) passes
  // through untouched.
});
