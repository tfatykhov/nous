import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { markupOf, parseBlocks, styleBlocks } from './lib/testing/css';
import StatGrid, { balancedCols } from './lib/ui/StatGrid.svelte';

// Finish review fix 8, the dense-screen finish: a tile inside a card is a flat tint (no shadow on a shadow, the floor's
// nested card), Overview's 7-day change is a small note under the number, the donut is no taller than the timeline's
// card needs, and a flow-bar count that does not fit its segment is hidden (the legend names it).

const read = (f: string) => readFileSync(join(__dirname, f), 'utf-8');

describe('flat tiles inside a card', () => {
  it('StatGrid flat: a wash fill and no shadow', () => {
    const css = parseBlocks(styleBlocks(read('lib/ui/StatGrid.svelte')));
    expect(css.get('.stat-grid--flat .stat-card')).toEqual({ background: 'var(--wash)', 'box-shadow': 'none' });
    const { container } = render(StatGrid, { props: { stats: [{ label: 'A', value: 1 }], flat: true } });
    expect(container.querySelector('.stat-grid')?.classList.contains('stat-grid--flat')).toBe(true);
  });

  it.each([
    ['views/Overview.svelte', 2], // Execution integrity, in both of its states (three columns, see above)
    ['views/Subtasks.svelte', 1], // Upcoming schedules
    ['views/Harness.svelte', 1], // Most active censors
  ])('%s uses the flat tiles inside its card (%i)', (file, n) => {
    expect((markupOf(read(file)).match(/<StatGrid flat\b/g) ?? []).length).toBe(n);
  });
});

// Design batch 1, from the captures: a tile row never ends in one lone tile on the desktop either. Overview's two
// coloured-dot tiles and Decisions' Brier tile were a second grid under a StatGrid (Overview 5, 2, 2; Decisions 4, 1),
// and Execution integrity's six tiles were five and one. The extra tiles join the StatGrid through its `extra` snippet,
// and `cols` fixes a column count (Execution integrity: three, so six tiles are two rows of three).
describe('one tile grid per group', () => {
  it('StatGrid renders its extra tiles inside the grid, and cols sets equal columns above phone width', () => {
    // Above phone width (the 640px block sets two up, so the first .stat-grid rule is the one read here).
    expect(styleBlocks(read('lib/ui/StatGrid.svelte'))).toMatch(/\.stat-grid \{[^}]*grid-template-columns: repeat\(var\(--cols\), minmax\(0, 1fr\)\);/);
    const extra = createRawSnippet(() => ({ render: () => '<div class="stat-card x">extra</div>' }));
    const { container } = render(StatGrid, { props: { stats: [{ label: 'A', value: 1 }], cols: 3, extra } });
    const grid = container.querySelector('.stat-grid') as HTMLElement;
    expect(grid.querySelector('.x')?.parentElement).toBe(grid);
    expect(grid.style.getPropertyValue('--cols')).toBe('3');
  });

  // Design batch 2 (verdict pass 1): auto-fill at 200px fit five tiles at 1440 but four at 1280, so a row of five
  // became four and one. The column count now comes from the tile count, not the width: at most five a row, the rows
  // as even as they go, so no desktop width leaves one tile alone (the phone stays two up).
  it('the columns come from the tile count: up to five in one row, then the rows as even as they go', () => {
    expect([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map(balancedCols)).toEqual([1, 2, 3, 4, 5, 3, 4, 4, 5, 5, 4, 4]);
    for (let n = 2; n <= 20; n++) expect(n % balancedCols(n), `${n} tiles`).not.toBe(1);
    const { container } = render(StatGrid, { props: { stats: [1, 2, 3, 4, 5, 6].map((i) => ({ label: `S${i}`, value: i })) } });
    expect((container.querySelector('.stat-grid') as HTMLElement).style.getPropertyValue('--cols')).toBe('3');
  });

  it.each([['views/Overview.svelte', 'memory grid: seven tiles and two extra'], ['views/Decisions.svelte', 'four tiles and one extra']])(
    '%s: a grid with extra tiles names its columns (%s: one row of five, or five and four)', (f) => {
      expect(markupOf(read(f))).toMatch(/<StatGrid cols=\{5\} stats=\{\[[\s\S]*?\]\}>\s*\{#snippet extra\(\)\}/);
    });

  it('Overview: the dot tiles are in the memory grid (no second grid), Execution integrity is three columns', () => {
    const src = read('views/Overview.svelte');
    expect(src).not.toMatch(/special-cards/);
    expect(markupOf(src)).toMatch(/<StatGrid cols=\{5\} stats=\{\[[\s\S]*?\]\}>\s*\{#snippet extra\(\)\}[\s\S]*?Graph density[\s\S]*?Brier score[\s\S]*?\{\/snippet\}\s*<\/StatGrid>/);
    expect((markupOf(src).match(/<StatGrid flat cols=\{3\}/g) ?? []).length).toBe(2);
  });

  it('Decisions: the Brier tile is in the StatGrid (no second grid)', () => {
    const src = read('views/Decisions.svelte');
    expect(src).not.toMatch(/indicator-row/);
    expect(markupOf(src)).toMatch(/<StatGrid cols=\{5\} stats=\{\[[\s\S]*?\]\}>\s*\{#snippet extra\(\)\}[\s\S]*?Latest Brier score[\s\S]*?\{\/snippet\}\s*<\/StatGrid>/);
  });
});

describe('Overview', () => {
  it('the 7-day change is a small note, not part of the big number', () => {
    const src = read('views/Overview.svelte');
    expect(src).not.toMatch(/fmtWithDelta/);
    expect(src).toMatch(/\{ label: 'Facts', +value: fmt\(mem\.total_facts\), note: deltaNote\(deltas\.facts\.last_7_days\) \}/);
  });

  it('the donut and the bars beside it are 160px, the timeline card\'s plot scale (they were 240px)', () => {
    const m = markupOf(read('views/Overview.svelte'));
    const charts = [...m.matchAll(/<Chart\b[\s\S]*?height="(\d+)px"/g)].map((x) => x[1]);
    expect(charts).toEqual(['160', '160']);
  });
});

// Design batch 2, fix 8's leftovers (verdict pass 1): chart axis titles in sentence case, the Workflows subtitle as
// a plain sentence, and the Graph sheet's title in sentence case.
describe('copy inside charts and sheets', () => {
  const AXIS_TITLE = /title: \{ display: true, text: '([^']*)'/g;
  /** Words after the first that start with a capital and are not an acronym: Title Case. */
  const titleCased = (t: string) => t.split(/\s+/).slice(1).filter((w) => /^[A-Z][a-z]/.test(w));
  it('the check catches Title Case and spares sentence case and acronyms', () => {
    expect(titleCased('Actual Success Rate')).toEqual(['Success', 'Rate']);
    expect(titleCased('Brier score (lower is better)')).toEqual([]);
    expect(titleCased('Admission rate (%) UTC')).toEqual([]);
  });
  it.each(['views/Admission.svelte', 'views/Decisions.svelte', 'views/health/GraphTab.svelte', 'views/Rubric.svelte'])(
    '%s: every axis title is sentence case', (f) => {
      const titles = [...read(f).matchAll(AXIS_TITLE)].map((m) => m[1]);
      expect(titles.length).toBeGreaterThan(0);
      expect(titles.filter((t) => titleCased(t).length > 0)).toEqual([]);
    });
  it('the Workflows subtitle is a plain sentence', () => {
    const sub = markupOf(read('views/DagView.svelte')).match(/<p class="view-subtitle">([^<]*)<\/p>/)?.[1] ?? '';
    expect(sub).toMatch(/^[A-Z].*\.$/); // a sentence: a capital, a full stop, no Title Case (Nous is a name)
    expect(titleCased(sub).filter((w) => w !== 'Nous')).toEqual([]);
    expect(sub).not.toMatch(/Unified|DAGs/);
  });
  it('the Graph sheet\'s title is sentence case', () => {
    const src = read('views/GraphView.svelte');
    expect(src).toContain("capitalize(selectedHead.type) + ' detail' : 'Node detail'");
  });
});

describe('Retrieval flow bar', () => {
  it('each segment is a size container, and its count is hidden when the segment is too narrow for it', () => {
    const css = styleBlocks(read('views/Retrieval.svelte'));
    expect(parseBlocks(css).get('.funnel-seg')).toMatchObject({ 'container-type': 'inline-size' });
    expect(css).toMatch(/@container \(max-width: 2\.5rem\) \{\s*\.seg-label \{ display: none; \}\s*\}/);
  });
});
