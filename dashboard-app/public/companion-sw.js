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

async function warmCache() {
  // Seed the offline cache AT INSTALL (codex P1): registration happens
  // after `load`, so the first visit's own fetches were uncontrolled — a
  // user who installs the PWA on visit one and next opens it offline
  // would otherwise find an empty cache and no shell at all. The hashed
  // asset URLs are discovered by parsing the entry HTML, so this static
  // worker needs no build-time precache manifest. Best-effort throughout:
  // a failed warm must never fail installation (runtime caching recovers
  // on the next online visit).
  const cache = await caches.open(CACHE);

  async function put(url) {
    try {
      const response = await fetch(url);
      if (response.ok) await cache.put(url, response);
    } catch {
      /* offline mid-install or transient failure — runtime caching recovers */
    }
  }

  try {
    const shellResponse = await fetch('/dashboard/v2/companion.html');
    if (shellResponse.ok) {
      await cache.put('/dashboard/v2/companion.html', shellResponse.clone());
      const html = await shellResponse.text();
      const assetUrls = new Set(
        [...html.matchAll(/(?:src|href)="(\/dashboard\/v2\/[^"]+)"/g)].map((m) => m[1]),
      );
      for (const url of assetUrls) await put(url);
    }
  } catch {
    /* best-effort */
  }
  await put('/dashboard/v2/companion.webmanifest');

  // Warm the surface snapshots so the offline feed exists from visit one.
  // The index is published LAST, and rewritten to list only surfaces whose
  // snapshots actually stored (codex P2): an index advertising a snapshot
  // the cache lacks would send the offline hydration cycle into a fetch
  // failure and a blank feed — a coherent partial index beats a complete
  // incoherent one.
  try {
    const index = await fetch('/a2ui/surfaces');
    if (index.ok) {
      const data = await index.json();
      const stored = [];
      for (const surface of data.surfaces ?? []) {
        const url = '/a2ui/surfaces/' + encodeURIComponent(surface.surface_id);
        try {
          const response = await fetch(url);
          if (response.ok) {
            await cache.put(url, response);
            stored.push(surface);
          }
        } catch {
          /* skip — the index below will not list it */
        }
      }
      // A PARTIAL index must not keep the full watermark (codex P1):
      // latest_seq covers the omitted surfaces' create events, so a
      // hydration that fell back to this index followed by a healthy
      // stream open would tail right past them — a live approval invisible
      // until the next stream failure. Zeroing the watermark makes the
      // first successful stream open hit the replay-window gap, which
      // returns the resync control — the full online re-hydration path
      // that already exists for exactly this.
      const partial = stored.length < (data.surfaces ?? []).length;
      await cache.put(
        '/a2ui/surfaces',
        new Response(
          JSON.stringify({
            ...data,
            surfaces: stored,
            latest_seq: partial ? 0 : data.latest_seq,
          }),
          { headers: { 'Content-Type': 'application/json' } },
        ),
      );
    }
  } catch {
    /* best-effort */
  }
}

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(warmCache());
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
