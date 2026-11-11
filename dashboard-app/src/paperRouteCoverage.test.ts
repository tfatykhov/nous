import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { PAPER_ROUTES } from './lib/look';

// Review Focus 5: a route turns paper (PAPER_ROUTES) only once every file it renders reads tokens. The files are not a
// hand-kept list (review I1): each route's view is read off App.svelte's route switch, and every .svelte file reached
// from it through relative and $lib imports (index.ts and .js-to-.ts included) must be in both guards of
// hexGuard.test.ts (style; script and markup), or in ALLOWED below with its reason. An allowed file is not walked into.
const SRC = __dirname;

const ALLOWED: { match: (file: string) => boolean; reason: string }[] = [
  {
    match: (f) => f === 'views/Ledger.svelte',
    reason: "Work's Changes tab renders the Ledger inside a dark island (data-ground=\"dark\") until PR 3 converts it",
  },
  {
    match: (f) => f.startsWith('lib/ui/drawer/'),
    reason:
      "shadcn's drawer primitives: BottomSheet gives the sheet its surface (.nous-sheet) and its route's ground; their " +
      'own colour classes (bg-popover, text-popover-foreground, bg-muted) map to no token, so emit no colour, and the ' +
      "overlay's bg-black/10 scrim is the same under both looks",
  },
];
const allowed = (f: string) => ALLOWED.some((a) => a.match(f));

/** The guard list `name` of hexGuard.test.ts, read as text (importing a test file would run its tests twice). */
function guardList(name: string): string[] {
  const src = readFileSync(join(SRC, 'hexGuard.test.ts'), 'utf-8');
  const body = src.match(new RegExp(`export const ${name}[^=]*=\\s*\\[([\\s\\S]*?)\\];`))?.[1];
  if (body == null) throw new Error(`${name} not found in hexGuard.test.ts`);
  return [...body.matchAll(/'([^']+)'/g)].map((m) => m[1]);
}

/** route -> its view file, read off App.svelte: `{#if $currentRoute === 'x'} <Comp />` and `import Comp from './views/…'`. */
function routeViews(): Record<string, string> {
  const app = readFileSync(join(SRC, 'App.svelte'), 'utf-8');
  const files = Object.fromEntries([...app.matchAll(/import\s+(\w+)\s+from\s+'\.\/(views\/[^']+\.svelte)'/g)].map((m) => [m[1], m[2]]));
  return Object.fromEntries(
    [...app.matchAll(/\$currentRoute === '([\w-]+)'\}\s*<(\w+)\s*\/>/g)].map((m) => [m[1], files[m[2]]]),
  );
}

/** A module specifier from `file` to a source file under src/, or null (a package, or not found). */
function resolveImport(file: string, spec: string): string | null {
  let base: string;
  if (spec.startsWith('$lib/')) base = join(SRC, 'lib', spec.slice(5));
  else if (spec.startsWith('.')) base = resolve(dirname(join(SRC, file)), spec);
  else return null;
  const candidates = [base, base.replace(/\.js$/, '.ts'), `${base}.ts`, join(base, 'index.ts')];
  const hit = candidates.find((c) => existsSync(c) && statSync(c).isFile());
  return hit ? relative(SRC, hit).split('\\').join('/') : null;
}

/** Every .svelte file `entry` renders: its imports, followed through .ts modules, stopping at allowed files. */
function rendered(entry: string): { svelte: string[]; allowedReached: string[] } {
  const seen = new Set<string>();
  const svelte: string[] = [];
  const allowedReached: string[] = [];
  const walk = (file: string) => {
    if (seen.has(file)) return;
    seen.add(file);
    if (file.endsWith('.svelte')) {
      if (allowed(file)) {
        allowedReached.push(file);
        return;
      }
      svelte.push(file);
    }
    const src = readFileSync(join(SRC, file), 'utf-8');
    for (const m of src.matchAll(/^\s*import\s+(?!type\b)[^'"]*?['"]([^'"]+)['"]/gm)) {
      const next = resolveImport(file, m[1]);
      if (next) walk(next);
    }
  };
  walk(entry);
  return { svelte, allowedReached };
}

/** Every `route: file (guard)` a paper route renders without both guards; a route with no view is one miss. */
function uncovered(
  routes: readonly string[],
  files: Partial<Record<string, readonly string[]>>,
  hex: readonly string[],
  script: readonly string[],
): string[] {
  return routes.flatMap((route) => {
    const list = files[route];
    if (!list) return [`${route}: no view`];
    return list.flatMap((f) => [
      ...(hex.includes(f) ? [] : [`${route}: ${f} (style)`]),
      ...(script.includes(f) ? [] : [`${route}: ${f} (script and markup)`]),
    ]);
  });
}

describe('every paper route renders only guarded files', () => {
  const HEX = guardList('HEX_GUARDED');
  const SCRIPT = guardList('SCRIPT_GUARDED');
  const VIEWS = routeViews();
  const FILES = Object.fromEntries(Object.entries(VIEWS).map(([r, v]) => [r, rendered(v).svelte]));

  it('reads both guard lists and every route\'s view off App.svelte', () => {
    expect(HEX).toContain('views/Overview.svelte');
    expect(SCRIPT).toContain('views/Overview.svelte');
    expect(Object.keys(VIEWS)).toHaveLength(18);
    expect(VIEWS.graph).toBe('views/GraphView.svelte');
    expect(VIEWS['model-calls']).toBe('views/ModelCalls.svelte');
  });

  it('follows imports through index.ts, $lib and .js specifiers (the Graph sheet\'s drawer, the Health tabs)', () => {
    const graph = rendered('views/GraphView.svelte');
    expect(graph.svelte).toEqual(expect.arrayContaining(['lib/viz/Graph.svelte', 'lib/ui/BottomSheet.svelte', 'lib/ui/button/button.svelte']));
    expect(graph.allowedReached).toEqual(expect.arrayContaining(['lib/ui/drawer/drawer-content.svelte', 'lib/ui/drawer/drawer-overlay.svelte']));
    expect(rendered('views/Health.svelte').svelte).toEqual(expect.arrayContaining(['views/Admission.svelte', 'views/MemoryWriteHealth.svelte']));
    // An island stops the walk: the Ledger's own imports are not Work's.
    expect(rendered('views/Work.svelte').allowedReached).toContain('views/Ledger.svelte');
  });

  it('fails for a route that renders a file missing from a guard, or a route with no view', () => {
    const files = { 'made-up': ['views/Overview.svelte', 'views/NotGuarded.svelte'] };
    expect(uncovered(['made-up', 'unlisted'], files, HEX, SCRIPT)).toEqual([
      'made-up: views/NotGuarded.svelte (style)',
      'made-up: views/NotGuarded.svelte (script and markup)',
      'unlisted: no view',
    ]);
  });

  it('PAPER_ROUTES: every file each route renders is in both guards, or allowed with a reason', () => {
    expect(uncovered(PAPER_ROUTES, FILES, HEX, SCRIPT)).toEqual([]);
  });

  it('every allowance is still reached by some paper route (none is stale)', () => {
    const reached = PAPER_ROUTES.flatMap((r) => rendered(VIEWS[r]).allowedReached);
    for (const a of ALLOWED) expect(reached.some((f) => a.match(f)), a.reason).toBe(true);
  });
});
