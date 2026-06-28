import { writable } from 'svelte/store';

export const ROUTES = [
  'overview','graph','browser','decisions','activity','heartbeat','observability',
  'health','admission','rubric','execution','cache','density','consolidation','dag','subtasks',
] as const;
export type RouteName = typeof ROUTES[number];

export const currentRoute = writable<RouteName>('overview');

/**
 * Parse the current location hash into a known route.
 * Returns null for hashes that are page anchors (no leading `#/`), so the
 * caller can leave the current route unchanged (e.g. `#main-content` from
 * the skip-link should not reset navigation).
 * Returns 'overview' for an empty/missing hash or an unknown `#/...` hash.
 */
function parse(): RouteName | null {
  const raw = location.hash;
  // Empty hash (or just '#') → default to overview on initial load
  if (!raw || raw === '#') return 'overview';
  // Only treat hashes that start with '#/' as route navigation attempts.
  // Bare anchors like '#main-content' (no slash) are page fragments → ignore.
  if (!raw.startsWith('#/')) return null;
  const h = raw.slice(2); // strip '#/'
  if ((ROUTES as readonly string[]).includes(h)) return h as RouteName;
  // Unknown route path → fall back to overview (original behaviour)
  return 'overview';
}

let initialized = false;
export function initRouter() {
  const initial = parse();
  if (initial !== null) currentRoute.set(initial);
  if (initialized) return;                 // avoid stacking listeners across re-inits (tests + HMR)
  initialized = true;
  window.addEventListener('hashchange', () => {
    const route = parse();
    if (route !== null) currentRoute.set(route);
    // Unknown fragment (e.g. skip-link #main-content) → leave current route as-is
  });
}
