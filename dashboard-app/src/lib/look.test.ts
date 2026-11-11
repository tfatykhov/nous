import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { get } from 'svelte/store';
import { LOOK_KEY, PAPER_ROUTES, groundFor, look, resolveLook, setLook, getLook, type Look } from './look';
import { ROUTES, type RouteName } from './router';

const APP = resolve(__dirname, '../..');
const SRC = resolve(__dirname, '..');
const BOOT = readFileSync(join(APP, 'public/look-boot.js'), 'utf-8');

// [search, stored, look, persist]
const TABLE: [string, string | null, Look, boolean][] = [
  ['?look=paper', null, 'paper', true],
  ['?look=dark', 'paper', 'dark', true],
  ['', 'paper', 'paper', false],
  ['', 'dark', 'dark', false],
  ['', null, 'dark', false],
  ['?look=sepia', 'paper', 'paper', false], // an invalid value is ignored
  ['?look=', null, 'dark', false],
  ['?x=1&look=paper', null, 'paper', true],
  ['?look=PAPER', null, 'dark', false], // case-sensitive
  ['', 'sepia', 'dark', false],
  ['?look=paper', 'dark', 'paper', true], // the URL wins
  ['?look=dark&look=paper', null, 'dark', true], // a repeated parameter counts by its first value
  ['?x=1&look=paper&y=2', null, 'paper', true], // the other parameters stay when the look one is removed
];

/** Run public/look-boot.js against a fake page; returns the attribute it left and what it stored. */
function boot(search: string, stored: string | null, storage?: Storage, history?: Pick<History, 'replaceState' | 'state'>) {
  const attrs = new Map<string, string>();
  const document = {
    documentElement: {
      setAttribute: (k: string, v: string) => attrs.set(k, v),
      removeAttribute: (k: string) => attrs.delete(k),
      getAttribute: (k: string) => attrs.get(k) ?? null,
    },
  };
  const location = { search, pathname: '/dashboard/v2/', hash: '#/work/s/x' };
  const urls: string[] = [];
  const hist = history ?? { state: null, replaceState: (_s: unknown, _t: string, url?: string | URL | null) => { urls.push(String(url)); } };
  const data = new Map<string, string>();
  if (stored !== null) data.set(LOOK_KEY, stored);
  const writes: [string, string][] = [];
  const localStorage =
    storage ??
    ({
      getItem: (k: string) => data.get(k) ?? null,
      setItem: (k: string, v: string) => {
        writes.push([k, v]);
        data.set(k, v);
      },
    } as unknown as Storage);
  new Function('document', 'location', 'localStorage', 'history', BOOT)(document, location, localStorage, hist);
  return { attr: attrs.get('data-look') ?? null, writes, urls };
}

describe('resolveLook', () => {
  it.each(TABLE)('(%j, %j) -> %s, persist %s', (search, stored, want, persist) => {
    expect(resolveLook(search, stored)).toEqual({ look: want, persist });
  });
});

describe('look-boot.js', () => {
  it.each(TABLE)('agrees with resolveLook for (%j, %j)', (search, stored, want, persist) => {
    const { attr, writes } = boot(search, stored);
    expect(attr).toBe(want === 'paper' ? 'paper' : null); // absent = dark
    expect(writes).toEqual(persist ? [[LOOK_KEY, want]] : []);
  });

  // Review I1: a ?look= left in the URL would re-apply on every reload and overwrite the switch. Once applied it is
  // removed (the hash and the other parameters stay); an invalid value is left alone, as it changed nothing.
  it.each(TABLE)('removes an applied ?look= from the URL for (%j, %j)', (search, stored, _want, persist) => {
    const { urls } = boot(search, stored);
    if (!persist) return expect(urls).toEqual([]);
    const rest = new URLSearchParams(search);
    rest.delete('look');
    const q = rest.toString();
    expect(urls).toEqual([`/dashboard/v2/${q ? `?${q}` : ''}#/work/s/x`]);
  });

  it('the URL after ?x=1&look=paper&y=2 keeps x, y and the hash', () => {
    expect(boot('?x=1&look=paper&y=2', null).urls).toEqual(['/dashboard/v2/?x=1&y=2#/work/s/x']);
    expect(boot('?look=paper', null).urls).toEqual(['/dashboard/v2/#/work/s/x']);
  });

  it('a throwing history still applies the look', () => {
    const throwing = { state: null, replaceState: () => { throw new Error('SecurityError'); } };
    expect(boot('?look=paper', null, undefined, throwing).attr).toBe('paper');
  });

  it('never throws when storage throws (private mode), and the URL override still applies', () => {
    const throwing = new Proxy(
      {},
      {
        get() {
          throw new Error('SecurityError');
        },
      },
    ) as Storage;
    expect(() => boot('?look=paper', null, throwing)).not.toThrow();
    expect(boot('?look=paper', null, throwing).attr).toBe('paper');
    expect(boot('', null, throwing).attr).toBeNull();
  });

  it('is a classic script with no module syntax', () => {
    expect(BOOT).not.toMatch(/^\s*(import|export)\b/m);
  });
});

describe('setLook, getLook and the look store', () => {
  beforeEach(() => {
    setLook('dark');
    localStorage.clear();
  });

  it('paper sets the attribute and the storage; dark removes the attribute', () => {
    setLook('paper');
    expect(document.documentElement.dataset.look).toBe('paper');
    expect(localStorage.getItem(LOOK_KEY)).toBe('paper');
    expect(getLook()).toBe('paper');
    expect(get(look)).toBe('paper');
    setLook('dark');
    expect(document.documentElement.hasAttribute('data-look')).toBe(false);
    expect(localStorage.getItem(LOOK_KEY)).toBe('dark');
    expect(get(look)).toBe('dark');
  });

  it('follows a storage event for nous.look from another tab', () => {
    window.dispatchEvent(new StorageEvent('storage', { key: LOOK_KEY, newValue: 'paper' }));
    expect(document.documentElement.dataset.look).toBe('paper');
    expect(get(look)).toBe('paper');
    window.dispatchEvent(new StorageEvent('storage', { key: 'other', newValue: 'dark' }));
    expect(get(look)).toBe('paper'); // another key changes nothing
    window.dispatchEvent(new StorageEvent('storage', { key: LOOK_KEY, newValue: null }));
    expect(document.documentElement.hasAttribute('data-look')).toBe(false);
    expect(get(look)).toBe('dark');
  });

  it('storage failure still switches the look', () => {
    const real = Storage.prototype.setItem;
    Storage.prototype.setItem = () => {
      throw new Error('QuotaExceeded');
    };
    try {
      expect(() => setLook('paper')).not.toThrow();
      expect(document.documentElement.dataset.look).toBe('paper');
    } finally {
      Storage.prototype.setItem = real;
    }
  });
});

describe('groundFor', () => {
  it('is paper for the migrated routes and dark for every other', () => {
    expect(PAPER_ROUTES).toEqual(['overview', 'work', 'graph', 'browser', 'health', 'retrieval', 'consolidation', 'model-calls', 'dag', 'subtasks', 'heartbeat', 'decisions', 'rubric', 'autonomy', 'intentions', 'harness', 'observability', 'identity']);
    expect(groundFor('work')).toBe('paper');
    expect(groundFor('overview')).toBe('paper');
    expect(groundFor('graph')).toBe('paper');
    // Anything outside PAPER_ROUTES is dark: an id no route has, since by the end of PR 5 every route is paper.
    expect(groundFor('not-a-route' as RouteName)).toBe('dark');
    for (const r of ROUTES) expect(['paper', 'dark']).toContain(groundFor(r));
    expect(ROUTES.filter((r) => groundFor(r) === 'paper')).toEqual(['overview', 'graph', 'browser', 'health', 'retrieval', 'consolidation', 'work', 'model-calls', 'dag', 'subtasks', 'heartbeat', 'decisions', 'rubric', 'autonomy', 'intentions', 'harness', 'observability', 'identity']);
  });

  // After PR 5 the redesign has converted every screen: no route is left on the dark ground under the paper look.
  it('every route in the router\'s list is a paper route, and PAPER_ROUTES holds nothing else (all 18, once each)', () => {
    expect(ROUTES).toHaveLength(18);
    expect(new Set(PAPER_ROUTES).size, 'no route listed twice').toBe(PAPER_ROUTES.length);
    expect(ROUTES.filter((r) => !PAPER_ROUTES.includes(r)), 'routes still dark').toEqual([]);
    expect(PAPER_ROUTES.filter((r) => !(ROUTES as readonly string[]).includes(r)), 'not a route').toEqual([]);
    expect(PAPER_ROUTES).toHaveLength(18);
  });
});

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return sources(p);
    return /\.(svelte|ts)$/.test(e.name) && !e.name.endsWith('.test.ts') ? [p] : [];
  });
}

// Review Focus 2: the look must be on <html> before the first paint, and nothing at mount may flip it.
describe('no flash', () => {
  const html = readFileSync(join(APP, 'index.html'), 'utf-8');

  it('index.html loads look-boot.js as a plain sync script in <head>, before the module entry', () => {
    const head = html.slice(html.indexOf('<head>'), html.indexOf('</head>'));
    const tag = head.match(/<script\b[^>]*\bsrc="[^"]*look-boot\.js"[^>]*>\s*<\/script>/);
    expect(tag).not.toBeNull();
    expect(tag![0]).toContain('src="/dashboard/v2/look-boot.js"');
    expect(tag![0]).not.toMatch(/\b(defer|async|type)\b/);
    const bootAt = html.indexOf(tag![0]);
    const moduleAt = html.search(/<script\b[^>]*type="module"/);
    expect(moduleAt).toBeGreaterThan(-1);
    expect(bootAt).toBeLessThan(moduleAt);
  });

  it('only look.ts writes data-look', () => {
    const writers = sources(SRC)
      .filter((f) => /dataset\.look\s*=|setAttribute\(\s*['"]data-look|removeAttribute\(\s*['"]data-look/.test(readFileSync(f, 'utf-8')))
      .map((f) => relative(SRC, f).replace(/\\/g, '/'));
    expect(writers).toEqual(['lib/look.ts']);
    for (const f of ['main.ts', 'App.svelte']) {
      expect(readFileSync(join(SRC, f), 'utf-8')).not.toMatch(/dataset\.look|data-look/);
    }
  });

  it('the companion page does not load the boot script', () => {
    expect(readFileSync(join(APP, 'companion.html'), 'utf-8')).not.toContain('look-boot');
  });
});
