import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { currentRoute, initRouter, ROUTES } from './router';

describe('router', () => {
  beforeEach(() => { location.hash = ''; });
  it('defaults to overview when hash empty', () => { initRouter(); expect(get(currentRoute)).toBe('overview'); });
  it('reads the hash route', () => { location.hash = '#/cache'; initRouter(); expect(get(currentRoute)).toBe('cache'); });
  it('falls back to overview on unknown route', () => { location.hash = '#/nope'; initRouter(); expect(get(currentRoute)).toBe('overview'); });
  it('lists all 15 routes', () => { expect(ROUTES.length).toBe(15); });
});
