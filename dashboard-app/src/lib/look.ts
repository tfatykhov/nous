import { writable, type Readable } from 'svelte/store';
import type { RouteName } from './router';

/**
 * The dashboard's look: `dark` (today's, the default) or `paper` (the redesign, staged). It lives on `<html>` as
 * `data-look="paper"`; no attribute means dark. `public/look-boot.js` sets it before the app loads (so the page never
 * flashes the other look), by the same rule as `resolveLook`; after that only `setLook` and the `storage` listener
 * below change it.
 */
export type Look = 'dark' | 'paper';
export const LOOK_KEY = 'nous.look';
/** The routes the redesign has migrated: their `<main>` is a paper ground under the paper look. */
export const PAPER_ROUTES: readonly RouteName[] = ['overview', 'work', 'graph', 'browser', 'health', 'retrieval', 'consolidation'];

function isLook(value: unknown): value is Look {
  return value === 'paper' || value === 'dark';
}

/**
 * `?look=paper|dark` in the query wins and is persisted; else the stored look; else dark. Any other value is
 * ignored (case-sensitive), and a repeated `look` parameter counts by its first value.
 */
export function resolveLook(search: string, stored: string | null): { look: Look; persist: boolean } {
  const fromUrl = new URLSearchParams(search).get('look');
  if (isLook(fromUrl)) return { look: fromUrl, persist: true };
  if (isLook(stored)) return { look: stored, persist: false };
  return { look: 'dark', persist: false };
}

/** The ground of a route's `<main>`: paper for a migrated route, dark for every other. */
export function groundFor(route: RouteName): 'paper' | 'dark' {
  return PAPER_ROUTES.includes(route) ? 'paper' : 'dark';
}

/** The look `<html>` carries now. */
export function getLook(): Look {
  return document.documentElement.dataset.look === 'paper' ? 'paper' : 'dark';
}

const store = writable<Look>(typeof document === 'undefined' ? 'dark' : getLook());
export const look: Readable<Look> = { subscribe: store.subscribe };

function apply(next: Look) {
  if (next === 'paper') document.documentElement.setAttribute('data-look', 'paper');
  else document.documentElement.removeAttribute('data-look');
  store.set(next);
}

/** Switch the look and remember it (a storage failure, as in private mode, only loses the memory). */
export function setLook(next: Look): void {
  apply(next);
  try {
    localStorage.setItem(LOOK_KEY, next);
  } catch {
    // Storage unavailable: the look still applies to this page.
  }
}

// Another tab switched the look (or the key was cleared): follow it, without writing storage back.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (e: StorageEvent) => {
    if (e.key !== LOOK_KEY && e.key !== null) return;
    apply(isLook(e.newValue) ? e.newValue : 'dark');
  });
}
