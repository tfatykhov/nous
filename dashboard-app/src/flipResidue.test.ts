import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { styleBlocks } from './lib/testing/css';

// Dashboard redesign PR 7: paper is the only look. Nothing of the look switch may remain: no attribute, key, boot
// script, module, route list or selector scoped to it. A selector still written behind `html[data-look]` would never
// match again and its rule would vanish without a failing test, so the scan reads every source, test, public file and
// page (Review Focus 1).

const APP = resolve(__dirname, '..');
const SRC = __dirname;

/** The switch's names. `/look'` is any import path ending in the deleted module (`./look`, `../look`, `$lib/look`). */
const RESIDUE = /data-look|data-ground|nous\.look|look-boot|LookToggle|PAPER_ROUTES|setLook|getLook|resolveLook|groundFor|html\[data-|\$lib\/look|\/look(?:\.ts)?['"]/;

/** The two files that must spell the patterns to forbid them. */
const EXEMPT = ['src/flipResidue.test.ts', 'src/companionIsolation.test.ts'];

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    return e.isDirectory() ? walk(p) : [p];
  });
}

function scanned(): string[] {
  const pub = readdirSync(join(APP, 'public'), { withFileTypes: true })
    .filter((e) => e.isFile() || (e.isDirectory() && e.name !== 'lib'))
    .flatMap((e) => (e.isDirectory() ? walk(join(APP, 'public', e.name)) : [join(APP, 'public', e.name)]))
    .filter((f) => !/\.(png|ico|jpe?g|webp)$/i.test(f));
  return [...walk(SRC), ...pub, ...['index.html', 'companion.html', 'README.md', 'vite.config.ts', 'package.json'].map((f) => join(APP, f))]
    .map((f) => relative(APP, f).replace(/\\/g, '/'));
}

describe('no residue of the look switch', () => {
  it('the pattern catches each name of the switch, and spares plain words', () => {
    for (const bad of ['data-look="paper"', '[data-ground="rail"]', "localStorage['nous.look']", '/dashboard/v2/look-boot.js',
      "import LookToggle from", 'PAPER_ROUTES', 'setLook(', 'getLook()', 'resolveLook(', 'groundFor(', 'html[data-theme="x"]',
      "from '$lib/look';", "from '../look';", "from './look';", "from '../lib/look';", "import('./look.ts')"]) {
      expect(bad, bad).toMatch(RESIDUE);
    }
    for (const fine of ['the new look', 'html:root .page-ground', 'class="rail"', 'lookup', "from './lookup';", 'outlook']) {
      expect(fine, fine).not.toMatch(RESIDUE);
    }
  });

  it('the two exempt files exist and still spell a pattern (no stale exemption)', () => {
    for (const f of EXEMPT) {
      expect(existsSync(join(APP, f)), f).toBe(true);
      expect(readFileSync(join(APP, f), 'utf-8'), f).toMatch(RESIDUE);
    }
  });

  it('reads the sources, the tests, the public files and the pages', () => {
    const files = scanned();
    expect(files.length).toBeGreaterThan(200);
    for (const f of ['src/app.css', 'src/App.svelte', 'src/tokens.test.ts', 'index.html', 'companion.html', 'README.md', 'public/favicon.svg']) {
      expect(files, f).toContain(f);
    }
    expect(files.some((f) => f.startsWith('public/lib/'))).toBe(false); // vendored Chart.js, d3, cytoscape
  });

  it('no file holds a name of the switch', () => {
    const hits = scanned()
      .filter((f) => !EXEMPT.includes(f))
      .filter((f) => RESIDUE.test(readFileSync(join(APP, f), 'utf-8')));
    expect(hits).toEqual([]);
  });

  it('the deleted files are gone', () => {
    for (const f of ['public/look-boot.js', 'src/lib/look.ts', 'src/lib/look.test.ts', 'src/lib/ui/LookToggle.svelte',
      'src/lib/ui/LookToggle.test.ts', 'src/darkParity.test.ts', 'src/App.look.test.ts', 'src/interfacesPinned.test.ts']) {
      expect(existsSync(join(APP, f)), f).toBe(false);
    }
  });
});

describe('index.html', () => {
  const html = readFileSync(join(APP, 'index.html'), 'utf-8');

  it('loads no boot script, and declares the light scheme and the rail blue for the status area', () => {
    expect(html).not.toMatch(/<script\b[^>]*look-boot/);
    expect(html).toMatch(/<meta name="color-scheme" content="light"\s*\/?>/);
    expect(html).toMatch(/<meta name="theme-color" content="#12304f"\s*\/?>/);
  });
});

// Review Focus 3: the view selectors that sat behind `html[data-look="paper"] [data-ground="paper"]` (0,2,1) are anchored
// at `html:root .page-ground` (0,2,1), so no rule changed rank. Every other :global( form in the views is pinned here as
// it is today: a new one fails (leakedGlobals.test.ts holds the same anchor forms).
const ANCHORS = [':global(html:root .page-ground)', ':global(html:root)'];
const PINNED: Record<string, string[]> = {
  'views/Browser.svelte': [':global(.detail-grid)', ':global(.detail-grid > span)', ':global(.dl)', ':global(.mono)', ':global(.mono)', ':global(.muted-text)'],
  'views/Identity.svelte': [':global(.detail-grid)', ':global(.dl)', ':global(.mono)'],
  'views/Overview.svelte': [':global([data-slot="badge"])'],
  'views/work/Trace.svelte': [':global(*)', ':global(*)'],
};

describe('the anchor of the paper rules in the views', () => {
  const views = walk(join(SRC, 'views')).filter((f) => f.endsWith('.svelte'));
  const globalsOf = (f: string) => [...styleBlocks(readFileSync(f, 'utf-8')).matchAll(/:global\([^)]*\)/g)].map((m) => m[0]);

  it('reads every view', () => {
    expect(views.length).toBeGreaterThan(25);
  });

  it('every :global( in a view is an anchor or one of the pinned forms of its file', () => {
    const extra: Record<string, string[]> = {};
    for (const f of views) {
      const rel = relative(SRC, f).replace(/\\/g, '/');
      const others = globalsOf(f).filter((g) => !ANCHORS.includes(g)).sort();
      const pinned = [...(PINNED[rel] ?? [])].sort();
      if (JSON.stringify(others) !== JSON.stringify(pinned)) extra[rel] = others;
    }
    expect(extra).toEqual({});
  });

  // PR 7 T6 folded the dark base rules into the anchored ones and dropped the anchor where nothing in the file can
  // out-rank the plain rule; the three anchors left are counted here per file.
  const ANCHORS_LEFT: Record<string, number> = {
    // Kept on purpose (each with its reason in the file): Browser's .btn out-ranks .btn-sm, Retrieval's .chart-card
    // out-ranks .list-card, Memory write health's h2 out-ranks .verdict-title.
    'views/Browser.svelte': 1, 'views/Retrieval.svelte': 1, 'views/MemoryWriteHealth.svelte': 1,
  };

  it('the anchors left are the counted ones, and the old scoped form is gone', () => {
    const left: Record<string, number> = {};
    for (const f of views) {
      const n = globalsOf(f).filter((g) => ANCHORS.includes(g)).length;
      if (n) left[relative(SRC, f).replace(/\\/g, '/')] = n;
    }
    expect(left).toEqual(ANCHORS_LEFT);
    expect(views.flatMap(globalsOf).filter((g) => /data-/.test(g) && !g.includes('data-slot'))).toEqual([]);
  });
});

// Review Focus 4: a browser that stored the dark look, or a bookmark with ?look=dark, still gets the one look; nothing
// is written back, and a storage event for the old key changes nothing.
vi.mock('./views/Overview.svelte', () => ({ default: () => {} }));
vi.mock('./views/GraphView.svelte', () => ({ default: () => {} }));
vi.mock('./views/Browser.svelte', () => ({ default: () => {} }));
vi.mock('./views/Decisions.svelte', () => ({ default: () => {} }));
vi.mock('./views/Heartbeat.svelte', () => ({ default: () => {} }));
vi.mock('./views/Observability.svelte', () => ({ default: () => {} }));
vi.mock('./views/Retrieval.svelte', () => ({ default: () => {} }));
vi.mock('./views/Health.svelte', () => ({ default: () => {} }));
vi.mock('./views/Rubric.svelte', () => ({ default: () => {} }));
vi.mock('./views/Work.svelte', () => ({ default: () => {} }));
vi.mock('./views/ModelCalls.svelte', () => ({ default: () => {} }));
vi.mock('./views/Consolidation.svelte', () => ({ default: () => {} }));
vi.mock('./views/DagView.svelte', () => ({ default: () => {} }));
vi.mock('./views/Subtasks.svelte', () => ({ default: () => {} }));
vi.mock('./views/Identity.svelte', () => ({ default: () => {} }));
vi.mock('./views/Harness.svelte', () => ({ default: () => {} }));
vi.mock('./views/Intentions.svelte', () => ({ default: () => {} }));
vi.mock('./views/Autonomy.svelte', () => ({ default: () => {} }));

describe('a stored or bookmarked look is ignored', () => {
  beforeEach(() => {
    cleanup();
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {}))); // the attention poll never answers
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    localStorage.clear();
    history.replaceState(null, '', '/');
  });

  it('nous.look=dark in storage and ?look=dark in the URL: no attribute on <html>, main is the page ground, nothing written', async () => {
    localStorage.setItem('nous.look', 'dark');
    history.replaceState(null, '', '/dashboard/v2/?look=dark#/overview');
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const removeItem = vi.spyOn(Storage.prototype, 'removeItem');
    const before = Array.from(document.documentElement.attributes).map((a) => `${a.name}=${a.value}`);
    const { default: App } = await import('./App.svelte');
    const { container } = render(App);
    await tick();
    expect(document.documentElement.hasAttribute('data-look')).toBe(false);
    expect(Array.from(document.documentElement.attributes).map((a) => `${a.name}=${a.value}`)).toEqual(before);
    expect(container.querySelector('main#main-content')?.classList.contains('page-ground')).toBe(true);
    expect(setItem).not.toHaveBeenCalled();
    expect(removeItem).not.toHaveBeenCalled();
    expect(localStorage.getItem('nous.look')).toBe('dark'); // left alone, not cleaned (ruling)
    expect(location.search).toBe('?look=dark'); // the stray parameter is inert and stays in the address bar (ruling)

    window.dispatchEvent(new StorageEvent('storage', { key: 'nous.look', newValue: 'paper' }));
    window.dispatchEvent(new StorageEvent('storage', { key: 'nous.look', newValue: 'dark' }));
    await tick();
    expect(document.documentElement.hasAttribute('data-look')).toBe(false);
    expect(setItem).not.toHaveBeenCalled();
  });
});
