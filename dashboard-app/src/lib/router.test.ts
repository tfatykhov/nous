import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { currentRoute, currentSub, currentQuery, initRouter, routeHref, REDIRECTS, ROUTES, type RouteName } from './router';

function go(hash: string) {
  location.hash = hash;
  window.dispatchEvent(new HashChangeEvent('hashchange'));
}

describe('router', () => {
  beforeEach(() => { location.hash = ''; });
  it('defaults to overview when hash empty', () => { initRouter(); expect(get(currentRoute)).toBe('overview'); });
  it('reads the hash route', () => { location.hash = '#/model-calls'; initRouter(); expect(get(currentRoute)).toBe('model-calls'); });
  it('falls back to overview on unknown route', () => { location.hash = '#/nope'; initRouter(); expect(get(currentRoute)).toBe('overview'); });
  it('lists the 18 routes in navigation order', () => {
    expect(ROUTES.length).toBe(18);
    expect([...ROUTES]).toEqual([
      'overview', 'graph', 'browser', 'health', 'retrieval', 'consolidation',
      'work', 'model-calls', 'dag', 'subtasks', 'heartbeat',
      'decisions', 'rubric',
      'autonomy', 'intentions', 'harness',
      'observability', 'identity',
    ]);
  });
  it('includes the work route', () => { expect(ROUTES).toContain('work'); });
  it('includes the model-calls route', () => { expect(ROUTES).toContain('model-calls'); });
  it('includes the autonomy route', () => { expect(ROUTES).toContain('autonomy'); });
  it('includes the intentions route', () => { expect(ROUTES).toContain('intentions'); });
  it('includes the harness route', () => { expect(ROUTES).toContain('harness'); });
  it('includes the identity route', () => { expect(ROUTES).toContain('identity'); });
  it('unknown fragment (e.g. skip-link #main-content) leaves current route unchanged', () => {
    // Navigate to a known route first
    location.hash = '#/model-calls';
    initRouter();
    expect(get(currentRoute)).toBe('model-calls');
    // Simulate the skip-link setting a non-route fragment
    location.hash = '#main-content';
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    expect(get(currentRoute)).toBe('model-calls');  // must not reset to 'overview'
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
    go('#/model-calls');
    const seen: string[] = [];
    const unsub = currentRoute.subscribe((r) => {
      seen.push(`${r}|${get(currentSub)}|${get(currentQuery).toString()}`);
    });
    go('#/work/growth?d=7');
    unsub();
    expect(seen).toEqual(['model-calls||', 'work|growth|d=7']);
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

});

// F108 Phase 2 T14: the six retired routes redirect to where their screens live now. The hash is replaced once
// (no new history entry), then the stores are set, so an old bookmark lands on the new screen.
describe('router redirects', () => {
  beforeEach(() => { location.hash = ''; initRouter(); });

  const CASES: [string, RouteName, string][] = [
    ['#/activity', 'observability', ''],
    ['#/admission', 'health', 'writes'],
    ['#/memory-write-health', 'health', 'writes'],
    ['#/density', 'health', 'graph'],
    ['#/cache', 'model-calls', 'cache'],
    ['#/execution', 'work', 'changes'],
  ];

  for (const [from, route, sub] of CASES) {
    it(`${from} lands on ${route}${sub ? ` sub ${sub}` : ''} and rewrites the address bar`, () => {
      go(from);
      expect(get(currentRoute)).toBe(route);
      expect(get(currentSub)).toBe(sub);
      expect(get(currentQuery).toString()).toBe('');
      expect(location.hash).toBe(routeHref(route, sub));
    });
  }

  it('the table holds exactly the six retired routes, keyed by bare name, each to a live route', () => {
    expect(Object.keys(REDIRECTS).sort()).toEqual(['activity', 'admission', 'cache', 'density', 'execution', 'memory-write-health']);
    for (const [key, target] of Object.entries(REDIRECTS)) {
      expect(key).not.toContain('#');
      expect(ROUTES as readonly string[]).not.toContain(key);
      expect(ROUTES).toContain(target.route);
    }
  });

  it('an old bookmark with a sub and a query lands on the new screen without them', () => {
    go('#/cache/foo?x=1');
    expect(get(currentRoute)).toBe('model-calls');
    expect(get(currentSub)).toBe('cache');
    expect(get(currentQuery).toString()).toBe('');
    expect(location.hash).toBe('#/model-calls/cache');
  });

  it('a name that is only an Object property is no redirect: it falls back to overview and keeps the hash', () => {
    for (const name of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {
      go(`#/${name}`);
      expect(get(currentRoute)).toBe('overview');
      expect(location.hash).toBe(`#/${name}`);
    }
  });

  it('redirects on the first load too', () => {
    location.hash = '#/execution';
    initRouter();
    expect(get(currentRoute)).toBe('work');
    expect(get(currentSub)).toBe('changes');
    expect(location.hash).toBe('#/work/changes');
  });

  it('replaces the history entry instead of adding one', () => {
    go('#/overview');
    const before = history.length;
    go('#/density');
    expect(location.hash).toBe('#/health/graph');
    expect(history.length).toBe(before + 1); // the go() itself; the redirect adds none
  });

  it('the hashchange that follows the replace changes nothing', () => {
    go('#/admission');
    const seen: string[] = [];
    const unsubs = [
      currentRoute.subscribe((r) => seen.push(`route:${r}`)),
      currentSub.subscribe((s) => seen.push(`sub:${s}`)),
      currentQuery.subscribe((q) => seen.push(`query:${q.toString()}`)),
    ];
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    unsubs.forEach((u) => u());
    expect(seen).toEqual(['route:health', 'sub:writes', 'query:']); // the subscribe calls only
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
