import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { PAPER_ROUTES } from './lib/look';
import type { RouteName } from './lib/router';

// Review Focus 5: a route turns paper (PAPER_ROUTES) only once every file it renders reads tokens. Each paper route
// lists the files it renders, the ones it embeds included (Health embeds Admission and the write-health page); every
// one of them must be in both guards of hexGuard.test.ts (style; script and markup). A file inside a dark island is
// not listed: it keeps the dark tokens on purpose.
const ROUTE_FILES: Partial<Record<RouteName, readonly string[]>> = {
  overview: [
    'views/Overview.svelte',
    'lib/viz/DayTimeline.svelte',
    'lib/viz/Chart.svelte',
    'lib/ui/StatGrid.svelte',
    'lib/ui/StaleBadge.svelte',
    'lib/ui/button/button.svelte',
  ],
  // The Changes tab's Ledger (views/Ledger.svelte, with its DataTable and FilterBar) sits in a dark island until PR 3.
  work: [
    'views/Work.svelte',
    'views/work/SessionList.svelte',
    'views/work/Patterns.svelte',
    'views/work/Trace.svelte',
    'views/work/FullResult.svelte',
    'lib/ui/SubNav.svelte',
    'lib/ui/Ground.svelte',
    'lib/ui/HoldUndo.svelte',
    'lib/ui/StatGrid.svelte',
    'lib/ui/StaleBadge.svelte',
    'lib/ui/button/button.svelte',
  ],
  graph: [
    'views/GraphView.svelte',
    'lib/viz/Graph.svelte',
    'lib/ui/BottomSheet.svelte',
    'lib/ui/StatGrid.svelte',
    'lib/ui/StaleBadge.svelte',
    'lib/ui/button/button.svelte',
  ],
  browser: ['views/Browser.svelte', 'lib/ui/DataTable.svelte'],
  // The Writes tab embeds the write-health page and Admission: both must be converted before health turns paper.
  health: [
    'views/Health.svelte',
    'views/health/GraphTab.svelte',
    'views/health/GrowthTab.svelte',
    'views/health/WritesTab.svelte',
    'views/MemoryWriteHealth.svelte',
    'views/Admission.svelte',
    'lib/ui/SubNav.svelte',
    'lib/ui/StatGrid.svelte',
    'lib/ui/StaleBadge.svelte',
    'lib/ui/FilterBar.svelte',
    'lib/ui/DataTable.svelte',
    'lib/ui/button/button.svelte',
    'lib/viz/Chart.svelte',
  ],
};

/** The guard list `name` of hexGuard.test.ts, read as text (importing a test file would run its tests twice). */
function guardList(name: string): string[] {
  const src = readFileSync(join(__dirname, 'hexGuard.test.ts'), 'utf-8');
  const body = src.match(new RegExp(`export const ${name}[^=]*=\\s*\\[([\\s\\S]*?)\\];`))?.[1];
  if (body == null) throw new Error(`${name} not found in hexGuard.test.ts`);
  return [...body.matchAll(/'([^']+)'/g)].map((m) => m[1]);
}

/** Every `route: file (guard)` a paper route renders without both guards; a paper route with no entry is one miss. */
function uncovered(
  routes: readonly string[],
  files: Partial<Record<string, readonly string[]>>,
  hex: readonly string[],
  script: readonly string[],
): string[] {
  return routes.flatMap((route) => {
    const list = files[route];
    if (!list) return [`${route}: no file list`];
    return list.flatMap((f) => [
      ...(hex.includes(f) ? [] : [`${route}: ${f} (style)`]),
      ...(script.includes(f) ? [] : [`${route}: ${f} (script and markup)`]),
    ]);
  });
}

describe('every paper route renders only guarded files', () => {
  const HEX = guardList('HEX_GUARDED');
  const SCRIPT = guardList('SCRIPT_GUARDED');

  it('reads both guard lists', () => {
    expect(HEX).toContain('views/Overview.svelte');
    expect(SCRIPT).toContain('views/Overview.svelte');
  });

  it('fails for a route with a file missing from a guard, or with no file list', () => {
    const files = { 'made-up': ['views/Overview.svelte', 'views/NotGuarded.svelte'] };
    expect(uncovered(['made-up', 'unlisted'], files, HEX, SCRIPT)).toEqual([
      'made-up: views/NotGuarded.svelte (style)',
      'made-up: views/NotGuarded.svelte (script and markup)',
      'unlisted: no file list',
    ]);
  });

  it('every file listed exists', () => {
    for (const list of Object.values(ROUTE_FILES)) {
      for (const f of list ?? []) expect(existsSync(join(__dirname, f)), f).toBe(true);
    }
  });

  it('PAPER_ROUTES: every file of every route is in both guards', () => {
    expect(uncovered(PAPER_ROUTES, ROUTE_FILES, HEX, SCRIPT)).toEqual([]);
  });
});
