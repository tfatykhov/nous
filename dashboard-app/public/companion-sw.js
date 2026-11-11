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
//   - media (Range / video / audio requests, and any .mp4/.webm path, e.g.
//     a video opened as a page) is never touched — a 206 cannot be cached
//     and iOS needs the origin's own Range answers.
//
// Web Push is deliberately absent (spec §12.0/§13): Telegram is the push
// channel with deep links; a second delivery channel splits notification
// state — the same failure mode §5.4 exists to prevent.

// v2 (dashboard redesign PR 6, the paper default): the worker's bytes change,
// so an installed client re-runs `install` (re-warms the new shell) and
// `activate` retires v1, but only once v2 can launch offline on its own
// (codex P2, see `retireOlder`). Assets are content-hashed and companion.html
// is network-first, so the bump is for the warm, not for correctness.
const CACHE = 'nous-companion-v2';
const SHELL = '/dashboard/v2/companion.html';
const INDEX = '/a2ui/surfaces';

/** The hashed assets an entry HTML loads: a launch needs every one. The
 * manifest, icons and favicon it also links are best-effort. */
function requiredAssets(html) {
  return [...new Set([...html.matchAll(/(?:src|href)="(\/dashboard\/v2\/assets\/[^"]+)"/g)].map((m) => m[1]))];
}

/** True when `html` is the shell (it loads at least one hashed asset; a sign-in
 * or error page loads none) and `lookup` finds every asset it loads. */
async function launches(html, lookup) {
  const required = requiredAssets(html);
  if (required.length === 0) return false;
  for (const url of required) if (!(await lookup(url))) return false;
  return true;
}

/** Whether `cache` ALONE can launch the shell. The cached HTML proves nothing
 * by itself: the runtime's network-first fetch stores a new HTML before its
 * assets, and an asset whose hash did not change is served from the older
 * cache and never written here. */
async function shellComplete(cache) {
  const html = await cache.match(SHELL);
  return !!html && (await launches(await html.text(), (url) => cache.match(url)));
}

async function store(cache, url) {
  try {
    const response = await fetch(url);
    if (!response.ok) return false;
    await cache.put(url, response);
    return true;
  } catch {
    return false; // offline mid-install or a transient failure
  }
}

/** Fetch the shell into `cache`; true only when every asset it needs is stored.
 * The HTML goes in LAST, so an interrupted warm never leaves an entry whose
 * assets are missing. */
async function warmShell(cache) {
  try {
    const shell = await fetch(SHELL);
    if (!shell.ok) return false;
    const html = await shell.clone().text();
    const required = requiredAssets(html);
    if (required.length === 0) return false;
    for (const url of required) {
      if (!(await cache.match(url)) && !(await store(cache, url))) return false;
    }
    const extras = [...html.matchAll(/(?:src|href)="(\/dashboard\/v2\/[^"]+)"/g)].map((m) => m[1]);
    for (const url of new Set([...extras, '/dashboard/v2/companion.webmanifest'])) {
      if (!required.includes(url)) await store(cache, url);
    }
    await cache.put(SHELL, shell);
    return true;
  } catch {
    return false;
  }
}

/** A cached copy from the caches this one replaces. */
async function older(url) {
  for (const name of await caches.keys()) {
    if (name === CACHE) continue;
    const hit = await (await caches.open(name)).match(url);
    if (hit) return hit;
  }
  return undefined;
}

/** A snapshot that did not refresh: keep this cache's copy, else carry the
 * older cache's in, so an upgrade never loses the offline feed. */
async function keep(cache, url) {
  if (await cache.match(url)) return true;
  const previous = await older(url);
  if (!previous) return false;
  try {
    await cache.put(url, previous);
    return true;
  } catch {
    return false;
  }
}

function snapshotUrl(surface) {
  return INDEX + '/' + encodeURIComponent(surface.surface_id);
}

// The index is published LAST, and rewritten to list only surfaces whose
// snapshots this cache holds (codex P2): an index advertising a snapshot the
// cache lacks would send the offline hydration cycle into a fetch failure and
// a blank feed — a coherent partial index beats a complete incoherent one.
//
// A PARTIAL index must not masquerade as a complete one (codex P1 rounds 3+4):
// its latest_seq covers the omitted surfaces' create events, and a zeroed
// watermark alone is NOT a guaranteed resync — the outbox age-prune deletes
// old rows even for live surfaces, so on a low-traffic agent the remaining
// rows can fit the replay window and a healthy stream opens without ever
// replaying the omission. Two defenses, layered for version skew: an explicit
// `partial` marker the transport acts on (it re-hydrates from the network the
// moment the stream proves connectivity), and the zeroed watermark as the
// best-effort fallback for a stale client bundle that predates the marker.
async function putIndex(cache, data, stored, partial) {
  await cache.put(
    INDEX,
    new Response(
      JSON.stringify({ ...data, surfaces: stored, partial, latest_seq: partial ? 0 : data.latest_seq }),
      { headers: { 'Content-Type': 'application/json' } },
    ),
  );
}

/** Before the older caches go, make this cache's feed stand on its own: its
 * index (else the older cache's, carried in) lists only snapshots it holds,
 * carrying from the older cache any it lacks. The runtime's network-first
 * fetch can store an index without its snapshots. Local only. */
async function carryIndex(cache) {
  const own = await cache.match(INDEX);
  const source = own ?? (await older(INDEX));
  if (!source) return;
  let data;
  try {
    data = await source.json();
  } catch {
    return; // an unreadable index is no feed to keep
  }
  const listed = data.surfaces ?? [];
  const stored = [];
  let stale = false; // a carried snapshot older than this index's watermark
  for (const surface of listed) {
    const url = snapshotUrl(surface);
    if (await cache.match(url)) {
      stored.push(surface);
    } else if (await keep(cache, url)) {
      stored.push(surface);
      if (own) stale = true;
    }
  }
  if (own && !stale && stored.length === listed.length) return; // whole already
  // Carried whole, the old index and its snapshots are the pair v1 served.
  await putIndex(cache, data, stored, stale || data.partial === true || stored.length < listed.length);
}

/** Warm the surface snapshots so the offline feed exists from visit one. A
 * snapshot that fails to refresh is carried from the older cache: it is older
 * than the fresh watermark, so the index then says partial. When the index
 * itself fails, the older feed stays where it is (the offline fallback reads
 * it there) and moves in only as the older cache retires (`retireOlder`). */
async function warmSnapshots(cache) {
  let data = null;
  try {
    const index = await fetch(INDEX);
    if (index.ok) data = await index.json();
  } catch {
    /* offline */
  }
  if (!data) return;
  const listed = data.surfaces ?? [];
  const stored = [];
  let carried = false;
  for (const surface of listed) {
    const url = snapshotUrl(surface);
    if (await store(cache, url)) {
      stored.push(surface);
    } else if (await keep(cache, url)) {
      stored.push(surface);
      carried = true;
    }
  }
  await putIndex(cache, data, stored, carried || stored.length < listed.length);
}

async function warmCache() {
  // Seed the offline cache AT INSTALL (codex P1): registration happens
  // after `load`, so the first visit's own fetches were uncontrolled — a
  // user who installs the PWA on visit one and next opens it offline
  // would otherwise find an empty cache and no shell at all. The hashed
  // asset URLs are discovered by parsing the entry HTML, so this static
  // worker needs no build-time precache manifest. A failed warm must never
  // fail installation (runtime caching recovers on the next online visit);
  // it only keeps the older cache alive (`retireOlder`).
  try {
    const cache = await caches.open(CACHE);
    await warmShell(cache);
    await warmSnapshots(cache);
  } catch {
    /* best-effort */
  }
}

/** Delete the older caches only once this one can launch offline on its own
 * (codex P2): the warm is best-effort, and an install that lost the network
 * after the HTML, or mid-deploy, leaves this cache unable to launch; deleting
 * the old one then would leave an offline launch with no working shell. An
 * incomplete shell is warmed again here (the shell only, so a waiting page is
 * not held on the whole feed); if that fails too, the older cache stays until
 * the next activation. */
async function retireOlder() {
  const others = (await caches.keys()).filter((k) => k !== CACHE);
  if (others.length === 0) return;
  const cache = await caches.open(CACHE);
  if (!(await shellComplete(cache)) && !(await warmShell(cache))) return;
  await carryIndex(cache);
  await Promise.all(others.map((k) => caches.delete(k)));
}

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(warmCache());
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      try {
        await retireOlder();
      } catch {
        /* keep every cache; the next activation tries again */
      }
      await self.clients.claim();
    })(),
  );
});

/** This worker's cache first, then any other: caches.match searches caches in
 * the order they were created, so a kept older cache would otherwise answer. */
async function fromCache(request) {
  return (await (await caches.open(CACHE)).match(request)) ?? (await caches.match(request));
}

/** Offline, the shell from the first cache (this worker's, then the older
 * ones) whose assets are all cached somewhere, as cache-first serves them: a
 * partial v2 must not launch blank while v1 still holds a working shell. With
 * no complete shell anywhere, whatever copy exists. */
async function cachedShell(request) {
  for (const name of [CACHE, ...(await caches.keys()).filter((k) => k !== CACHE)]) {
    const html = await (await caches.open(name)).match(SHELL);
    if (html && (await launches(await html.clone().text(), (url) => caches.match(url)))) return html;
  }
  return fromCache(request);
}

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
  const cached = await fromCache(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) {
    const cache = await caches.open(CACHE);
    cache.put(request, response.clone());
  }
  return response;
}

async function networkFirst(request, fallback = fromCache) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    const cached = await fallback(request);
    if (cached) return cached;
    throw err;
  }
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (
    request.headers.has('range') ||
    request.destination === 'video' ||
    request.destination === 'audio' ||
    /\.(mp4|webm)$/i.test(url.pathname)
  ) {
    return;
  }
  if (isImmutableAsset(url)) {
    event.respondWith(cacheFirst(request));
  } else if (url.pathname === SHELL) {
    event.respondWith(networkFirst(request, cachedShell));
  } else if (isShell(url) || isSnapshotApi(url)) {
    event.respondWith(networkFirst(request));
  }
  // Everything else (SSE, actions, /a2ui/call, dashboard API) passes
  // through untouched.
});
