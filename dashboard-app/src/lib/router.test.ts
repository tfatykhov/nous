import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { currentRoute, initRouter, ROUTES } from './router';

describe('router', () => {
  beforeEach(() => { location.hash = ''; });
  it('defaults to overview when hash empty', () => { initRouter(); expect(get(currentRoute)).toBe('overview'); });
  it('reads the hash route', () => { location.hash = '#/cache'; initRouter(); expect(get(currentRoute)).toBe('cache'); });
  it('falls back to overview on unknown route', () => { location.hash = '#/nope'; initRouter(); expect(get(currentRoute)).toBe('overview'); });
  it('lists all 17 routes', () => { expect(ROUTES.length).toBe(17); });
  it('includes the identity route', () => { expect(ROUTES).toContain('identity'); });
  it('unknown fragment (e.g. skip-link #main-content) leaves current route unchanged', () => {
    // Navigate to a known route first
    location.hash = '#/cache';
    initRouter();
    expect(get(currentRoute)).toBe('cache');
    // Simulate the skip-link setting a non-route fragment
    location.hash = '#main-content';
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    expect(get(currentRoute)).toBe('cache');  // must not reset to 'overview'
  });
});
