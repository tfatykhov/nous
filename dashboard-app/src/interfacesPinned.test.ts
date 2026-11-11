import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import * as lookModule from './lib/look';
import * as chartColors from './lib/chartColors';
import * as css from './lib/testing/css';
import { DARK_SEL, PAPER_SEL, parseBlocks } from './lib/testing/css';

// Redesign PR 4 (Decisions and Autonomy groups) and PR 5 (System group) build on names that PR 1 to 3 define. If a
// review renames one, this file fails first, with the name, instead of a screen silently reading nothing.

const SRC = __dirname;
const BLOCKS = parseBlocks(readFileSync(join(SRC, 'app.css'), 'utf-8'));
const DARK = BLOCKS.get(DARK_SEL) ?? {};
const PAPER = BLOCKS.get(PAPER_SEL) ?? {};

// "Interfaces from PR 1" and "Interfaces from PR 2-3" of the PR 4/5 plan.
const PR1_TOKENS = [
  '--bg', '--surface', '--surface-hover', '--border', '--accent', '--accent-text', '--text', '--muted', '--green', '--red',
  '--yellow', '--waiting', '--pending', '--font-ui', '--font-mono', '--radius-sm',
  '--ok-text', '--ok-bg', '--warn-text', '--warn-bg', '--err-text', '--err-bg', '--stat-ok', '--stat-warn',
  '--notice-bg', '--notice-border', '--card-radius', '--card-radius-sm', '--card-shadow', '--card-border', '--ctl-radius',
  '--label-transform', '--label-tracking', '--on-accent', '--focus-ring',
];
const PR23_TOKENS = [
  '--st-neutral', '--st-completed', '--st-partial', '--tone-bad', '--save-ok', '--save-err', '--wash', '--track-strong',
  '--badge-good-bg', '--badge-warn-bg', '--badge-bad-bg', '--chart-text', '--chart-grid', '--series-0',
];
// The dark values the PR 4/5 screens rely on for parity (the old literals of their files).
const DARK_VALUES: Record<string, string> = {
  '--series-1': '#60a5fa', '--series-2': '#34d399', '--series-3': '#a78bfa', '--series-4': '#fb923c',
  '--series-5': '#f87171', '--series-6': '#fbbf24', '--series-7': '#6b6b8a', '--series-8': '#e2e2f0',
  '--series-9': '#22d3ee', '--series-10': '#e879f9', '--series-11': '#f472b6', '--series-12': '#9ca3af',
  '--series-13': '#c4c4d8', '--series-0': '#7c6af7',
  '--st-neutral': '#6b7280', '--st-completed': '#4ade80', '--st-partial': '#fb923c',
  '--tone-bad': '#ef4444', '--save-ok': '#4ade80', '--save-err': '#f87171',
  '--wash': 'rgba(255, 255, 255, 0.03)', '--track-strong': 'rgba(255, 255, 255, 0.08)',
  '--badge-good-bg': 'rgba(52, 211, 153, 0.15)', '--badge-warn-bg': 'rgba(251, 191, 36, 0.15)',
  '--badge-bad-bg': 'rgba(248, 113, 113, 0.15)',
  '--notice-bg': 'rgba(245, 158, 11, 0.08)', '--notice-border': 'rgba(245, 158, 11, 0.4)',
  '--stat-ok': '#10b981', '--stat-warn': '#f59e0b', '--card-radius': '12px', '--card-radius-sm': '8px',
};

/** Every token a component's script or markup names: `var(--x)`, and a quoted `'--x'` (built into var() or tinted later).
 * The markup counts because a chart's props may be written inline there (PR 4 re-review N2). */
function scriptTokens(src: string): string[] {
  const js = css.scriptBlocks(src) + '\n' + css.markupOf(src);
  return [...new Set([...js.matchAll(/var\(\s*(--[\w-]+)/g), ...js.matchAll(/['"`](--[\w-]+)['"`]/g)].map((m) => m[1]))];
}
const SEVEN = ['Decisions', 'Rubric', 'Autonomy', 'Intentions', 'Harness', 'Observability', 'Identity'].map((v) => `views/${v}.svelte`);
const guarded = [...(readFileSync(join(SRC, 'hexGuard.test.ts'), 'utf-8').match(/export const HEX_GUARDED[^=]*=\s*\[([\s\S]*?)\];/)?.[1] ?? '')
  .matchAll(/'([^']+)'/g)].map((m) => m[1]);
/** The converted views of PR 4 and PR 5 that draw a Chart. */
const chartViews = SEVEN.filter((f) => guarded.includes(f) && /import Chart from/.test(readFileSync(join(SRC, f), 'utf-8')));

describe('interfaces PR 4 and PR 5 build on', () => {
  it('every token named by the plan is defined in the dark block and on the paper ground', () => {
    for (const t of [...PR1_TOKENS, ...PR23_TOKENS]) {
      expect(DARK[t], `${t} dark`).toBeDefined();
      expect(PAPER[t], `${t} paper`).toBeDefined();
    }
  });

  it('the dark values the screens need for parity are unchanged', () => {
    for (const [t, v] of Object.entries(DARK_VALUES)) expect(DARK[t], t).toBe(v);
  });

  it('look.ts, chartColors.ts and the CSS test helpers export the names the plan uses', () => {
    expect(Array.isArray(lookModule.PAPER_ROUTES)).toBe(true);
    expect(typeof lookModule.groundFor).toBe('function');
    expect(typeof lookModule.setLook).toBe('function');
    expect(typeof lookModule.look.subscribe).toBe('function');
    for (const name of ['cssColor', 'tint', 'resolveColors', 'isPaperGround', 'seriesToken', 'withPaperDefaults'] as const) {
      expect(typeof chartColors[name], name).toBe('function');
    }
    expect(typeof chartColors.DARK_FALLBACK).toBe('object');
    expect(chartColors.tint('--series-1', 0.5)).toBe('tint(--series-1, 0.5)');
    expect(chartColors.seriesToken(7)).toBe('var(--series-8)');
    for (const name of ['parseBlocks', 'styleBlocks', 'scriptBlocks', 'markupOf'] as const) {
      expect(typeof css[name], name).toBe('function');
    }
    expect(css.DARK_SEL).toBe(':root, [data-ground="dark"]');
    expect(css.PAPER_SEL).toBe('html[data-look="paper"] [data-ground="paper"]');
  });

  it('DARK_FALLBACK holds every token a chart on these screens reads (a missing one resolves to "" in jsdom)', () => {
    // Review M5: read off the converted views that draw a Chart (every token their script names, by var() or by name),
    // plus Chart.svelte's own paper defaults, not a hand-kept list.
    const used = new Set(['--chart-text', '--chart-grid']);
    for (const file of chartViews) for (const t of scriptTokens(readFileSync(join(SRC, file), 'utf-8'))) used.add(t);
    expect(chartViews.length).toBeGreaterThanOrEqual(1);
    expect(used.has('--signal-self-corrected')).toBe(chartViews.includes('views/Rubric.svelte'));
    // PR 4 re-review N2: the markup is read too, where Decisions and Scoring write some chart props inline.
    expect(scriptTokens("<Chart options={{ grid: { color: 'var(--kind-task)' } }} data={{ c: tint('--made-up', 0.2) }} />"))
      .toEqual(['--kind-task', '--made-up']);
    for (const t of used) expect(chartColors.DARK_FALLBACK[t], t).toBe(DARK[t]);
  });

  it('the guards: both lists in hexGuard.test.ts, and the route walk in paperRouteCoverage.test.ts', () => {
    // The plan's ROUTE_FILES table was replaced by a walk of each route's imports (PR 2 review I1): a route's files
    // need no entry, only both guards.
    const guard = readFileSync(join(SRC, 'hexGuard.test.ts'), 'utf-8');
    expect(guard).toMatch(/export const HEX_GUARDED[^=]*=\s*\[/);
    expect(guard).toMatch(/export const SCRIPT_GUARDED[^=]*=\s*\[/);
    const coverage = readFileSync(join(SRC, 'paperRouteCoverage.test.ts'), 'utf-8');
    expect(coverage).toContain('function routeViews()');
    expect(coverage).toContain('function rendered(entry: string)');
    expect(coverage).toContain('uncovered(PAPER_ROUTES, FILES, HEX, SCRIPT)');
  });

  it('the shared components exist, and Badge and Button carry the variants the screens use', () => {
    for (const f of ['lib/ui/StatGrid.svelte', 'lib/ui/DataTable.svelte', 'lib/ui/FilterBar.svelte', 'lib/ui/StaleBadge.svelte',
      'lib/viz/Chart.svelte', 'lib/ui/button/button.svelte', 'lib/ui/badge/badge.svelte']) {
      expect(existsSync(join(SRC, f)), f).toBe(true);
    }
    const badge = readFileSync(join(SRC, 'lib/ui/badge/badge.svelte'), 'utf-8');
    for (const v of ['ok', 'warn', 'error', 'secondary']) expect(badge, v).toMatch(new RegExp(`\\b${v}: "`));
    const button = readFileSync(join(SRC, 'lib/ui/button/button.svelte'), 'utf-8');
    expect(button).toMatch(/\boutline: "/);
  });
});
