import { get, writable, type Readable } from 'svelte/store';

export const ROUTES = [
  'overview','graph','browser','decisions','activity','heartbeat','observability',
  'health','admission','rubric','execution','cache','density','consolidation','dag','subtasks',
  'identity','retrieval','harness','intentions','autonomy','memory-write-health','work',
] as const;
export type RouteName = typeof ROUTES[number];

/** Old route id → where it lives now. Empty until the navigation merge (F108 Phase 2 T14) fills and applies it. */
export const REDIRECTS: Record<string, { route: RouteName; sub?: string }> = {};

export const currentRoute = writable<RouteName>('overview');
const subStore = writable('');
const queryStore = writable(new URLSearchParams());
/** Everything after the route's `/`, still URL-encoded (`s/abc%2Fdef/turn1`); `''` for a bare route. */
export const currentSub: Readable<string> = { subscribe: subStore.subscribe };
/** The hash's `?query`. A new object only when the query string changes. */
export const currentQuery: Readable<URLSearchParams> = { subscribe: queryStore.subscribe };

interface Parsed { route: RouteName; sub: string; query: string }

/**
 * Parse the current location hash, `#/<route>[/<sub>][?<query>]`.
 * Returns null for hashes that are page anchors (no leading `#/`), so the
 * caller can leave the current route unchanged (e.g. `#main-content` from
 * the skip-link should not reset navigation).
 * Returns 'overview' for an empty/missing hash or an unknown `#/...` hash.
 */
function parse(): Parsed | null {
  const raw = location.hash;
  // Empty hash (or just '#') → default to overview on initial load
  if (!raw || raw === '#') return { route: 'overview', sub: '', query: '' };
  // Only treat hashes that start with '#/' as route navigation attempts.
  // Bare anchors like '#main-content' (no slash) are page fragments → ignore.
  if (!raw.startsWith('#/')) return null;
  const h = raw.slice(2); // strip '#/'
  const q = h.indexOf('?');
  const path = q === -1 ? h : h.slice(0, q);
  const slash = path.indexOf('/');
  const name = slash === -1 ? path : path.slice(0, slash);
  if ((ROUTES as readonly string[]).includes(name)) {
    return {
      route: name as RouteName,
      sub: slash === -1 ? '' : path.slice(slash + 1),
      query: q === -1 ? '' : h.slice(q + 1),
    };
  }
  // Unknown route path → fall back to overview (original behaviour)
  return { route: 'overview', sub: '', query: '' };
}

// Query and sub first, the route last: a subscriber woken by a new route already reads its sub and query.
function apply(loc: Parsed) {
  const next = new URLSearchParams(loc.query);
  if (next.toString() !== get(queryStore).toString()) queryStore.set(next);
  subStore.set(loc.sub);
  currentRoute.set(loc.route);
}

/**
 * A link to a route: `#/route[/sub][?query]`. An array `sub` is URL-encoded segment by segment and joined with `/`;
 * a string `sub` is used as is. The query goes through URLSearchParams, so a keyset cursor's `|` and `+` survive.
 */
export function routeHref(route: RouteName, sub?: string | string[], query?: Record<string, string>): string {
  const path = Array.isArray(sub) ? sub.map(encodeURIComponent).join('/') : (sub ?? '');
  const qs = new URLSearchParams(query).toString();
  return `#/${route}${path ? `/${path}` : ''}${qs ? `?${qs}` : ''}`;
}

let initialized = false;
export function initRouter() {
  const initial = parse();
  if (initial !== null) apply(initial);
  if (initialized) return;                 // avoid stacking listeners across re-inits (tests + HMR)
  initialized = true;
  window.addEventListener('hashchange', () => {
    const loc = parse();
    if (loc !== null) apply(loc);
    // Unknown fragment (e.g. skip-link #main-content) → leave current route as-is
  });
}
