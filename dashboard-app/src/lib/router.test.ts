import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { currentRoute, currentSub, currentQuery, initRouter, routeHref, REDIRECTS, ROUTES } from './router';

function go(hash: string) {
  location.hash = hash;
  window.dispatchEvent(new HashChangeEvent('hashchange'));
}

describe('router', () => {
  beforeEach(() => { location.hash = ''; });
  it('defaults to overview when hash empty', () => { initRouter(); expect(get(currentRoute)).toBe('overview'); });
  it('reads the hash route', () => { location.hash = '#/cache'; initRouter(); expect(get(currentRoute)).toBe('cache'); });
  it('falls back to overview on unknown route', () => { location.hash = '#/nope'; initRouter(); expect(get(currentRoute)).toBe('overview'); });
  it('lists all 24 routes', () => { expect(ROUTES.length).toBe(24); });
  it('includes the memory write health route', () => { expect(ROUTES).toContain('memory-write-health'); });
  it('includes the work route', () => { expect(ROUTES).toContain('work'); });
  it('includes the model-calls route', () => { expect(ROUTES).toContain('model-calls'); });
  it('includes the autonomy route', () => { expect(ROUTES).toContain('autonomy'); });
  it('includes the intentions route', () => { expect(ROUTES).toContain('intentions'); });
  it('includes the harness route', () => { expect(ROUTES).toContain('harness'); });
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

// F108 Phase 2 grammar: #/<route>[/<sub>][?<query>].
// These use `work`, whose sub paths are the trace and the patterns.
describe('router sub paths and query', () => {
  beforeEach(() => { location.hash = ''; initRouter(); });

  it('splits the hash into route, sub (still URL-encoded) and query', () => {
    go('#/work/s/abc%2Fdef/turn1?x=1');
    expect(get(currentRoute)).toBe('work');
    expect(get(currentSub)).toBe('s/abc%2Fdef/turn1');
    expect(get(currentQuery).toString()).toBe('x=1');
    expect(get(currentQuery).get('x')).toBe('1');
  });

  it('a bare route has an empty sub and an empty query', () => {
    go('#/work/writes?x=1');
    go('#/work');
    expect(get(currentRoute)).toBe('work');
    expect(get(currentSub)).toBe('');
    expect(get(currentQuery).toString()).toBe('');
  });

  it('a trailing slash is an empty sub', () => {
    go('#/work/');
    expect(get(currentSub)).toBe('');
  });

  it('a query with no sub', () => {
    go('#/work?kind=task&lens=error');
    expect(get(currentSub)).toBe('');
    expect(get(currentQuery).get('kind')).toBe('task');
    expect(get(currentQuery).get('lens')).toBe('error');
  });

  it('an unknown route falls back to overview with no sub and no query', () => {
    go('#/work/writes?x=1');
    go('#/nope/a/b?y=2');
    expect(get(currentRoute)).toBe('overview');
    expect(get(currentSub)).toBe('');
    expect(get(currentQuery).toString()).toBe('');
  });

  it('a page anchor leaves route, sub and query alone', () => {
    go('#/work/writes?x=1');
    go('#main-content');
    expect(get(currentRoute)).toBe('work');
    expect(get(currentSub)).toBe('writes');
    expect(get(currentQuery).get('x')).toBe('1');
  });

  it('a subscriber woken by the new route already reads the new sub and query', () => {
    go('#/cache');
    const seen: string[] = [];
    const unsub = currentRoute.subscribe((r) => {
      seen.push(`${r}|${get(currentSub)}|${get(currentQuery).toString()}`);
    });
    go('#/work/growth?d=7');
    unsub();
    expect(seen).toEqual(['cache||', 'work|growth|d=7']);
  });

  it('does not notify query subscribers when the query string is unchanged', () => {
    go('#/work/graph?d=7');
    let calls = 0;
    const unsub = currentQuery.subscribe(() => { calls += 1; });
    go('#/work/writes?d=7');
    go('#/work/growth?d=7');
    unsub();
    expect(calls).toBe(1); // the subscribe call itself only
  });

  it('starts with no redirects (they are added with the final routes)', () => {
    expect(REDIRECTS).toEqual({});
  });
});

describe('routeHref', () => {
  it('builds a bare route link', () => {
    expect(routeHref('model-calls')).toBe('#/model-calls');
    expect(routeHref('model-calls', '')).toBe('#/model-calls');
    expect(routeHref('model-calls', [])).toBe('#/model-calls');
  });

  it('URL-encodes each array segment and joins them with /', () => {
    expect(routeHref('model-calls', ['c', 'a/b'])).toBe('#/model-calls/c/a%2Fb');
  });

  it('uses a string sub as is', () => {
    expect(routeHref('model-calls', 's/abc%2Fdef/turn1')).toBe('#/model-calls/s/abc%2Fdef/turn1');
  });

  it('builds the query with URLSearchParams and leaves out an empty one', () => {
    expect(routeHref('model-calls', 'writes', { kind: 'task', lens: 'error' })).toBe('#/model-calls/writes?kind=task&lens=error');
    expect(routeHref('model-calls', '', {})).toBe('#/model-calls');
  });

  it('a keyset cursor with | and + survives the round trip through the hash', () => {
    const cursor = '2026-11-08T22:20:24.963805+00:00|0b6c5f2e-1d2a-4c3b-9e8f-7a6b5c4d3e2f';
    const href = routeHref('model-calls', ['c', 'mcp-session'], { before: cursor });
    expect(href).toContain('%2B');
    expect(href).toContain('%7C');
    expect(href).not.toContain('+00:00');
    initRouter();
    go(href);
    expect(get(currentRoute)).toBe('model-calls');
    expect(get(currentSub)).toBe('c/mcp-session');
    expect(get(currentQuery).get('before')).toBe(cursor);
  });
});
