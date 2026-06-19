import { writable } from 'svelte/store';

export const ROUTES = [
  'overview','graph','browser','decisions','activity','heartbeat','observability',
  'health','admission','rubric','execution','cache','density','dag','subtasks',
] as const;
export type RouteName = typeof ROUTES[number];

export const currentRoute = writable<RouteName>('overview');

function parse(): RouteName {
  const h = location.hash.replace(/^#\/?/, '');
  return (ROUTES as readonly string[]).includes(h) ? (h as RouteName) : 'overview';
}

let initialized = false;
export function initRouter() {
  currentRoute.set(parse());
  if (initialized) return;                 // avoid stacking listeners across re-inits (tests + HMR)
  initialized = true;
  window.addEventListener('hashchange', () => currentRoute.set(parse()));
}
