import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { markupOf, parseBlocks, styleBlocks } from './lib/testing/css';
import StatGrid, { STAT_GAP, STAT_MIN_TILE, statLayout } from './lib/ui/StatGrid.svelte';

// Finish review fix 8, the dense-screen finish: a tile inside a card is a flat tint (no shadow on a shadow, the floor's
// nested card), Overview's 7-day change is a small note under the number, the donut is no taller than the timeline's
// card needs, and a flow-bar count that does not fit its segment is hidden (the legend names it).

const read = (f: string) => readFileSync(join(__dirname, f), 'utf-8').replace(/\r\n/g, '\n'); // any checkout

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
    // cols is the most columns (Codex round 3): five stats and one extra tile at most three a row are 3 + 3.
    const { container } = render(StatGrid, { props: { stats: [1, 2, 3, 4, 5].map((i) => ({ label: `S${i}`, value: i })), cols: 3, extra } });
    const grid = container.querySelector('.stat-grid') as HTMLElement;
    expect(grid.querySelector('.x')?.parentElement).toBe(grid);
    expect(grid.style.getPropertyValue('--cols')).toBe('3');
  });

  // Codex round 3 (#904, P2): columns from the tile count alone forced five tiles into 532px at an 800px window (the
  // rail takes 220px). The layout now respects both the tile count and the grid's own width: as many columns as fit
  // at the minimum tile width (the widest real content, "0.028 good" or a six-digit total with its note), at most five,
  // in as few rows as hold the tiles and as even as they go. A last row of one tile spans the row.
  it('statLayout: no tile narrower than the minimum, as few rows as fit, as even as they go, and no lone tile', () => {
    for (let n = 1; n <= 12; n++) {
      for (const width of [150, 320, 532, 756, 1012, 1172, 2000]) {
        const { cols, spanLast } = statLayout(n, width);
        const fit = Math.max(1, Math.floor((width + STAT_GAP) / (STAT_MIN_TILE + STAT_GAP)));
        const cap = Math.min(5, fit, n);
        expect(cols, `${n} tiles in ${width}px`).toBeLessThanOrEqual(Math.max(1, fit));
        if (cols > 1) expect(cols * STAT_MIN_TILE + (cols - 1) * STAT_GAP, `${n} tiles in ${width}px`).toBeLessThanOrEqual(width);
        expect(Math.ceil(n / cols), `${n} tiles in ${width}px: rows`).toBe(Math.ceil(n / cap));
        expect(spanLast, `${n} tiles in ${width}px: a last row of one spans`).toBe(n > 1 && cols > 1 && n % cols === 1);
      }
    }
    // The cases named in the finding: 5 at 532px is 3 + 2; 9 is 3 + 3 + 3 there and 5 + 4 at 1012px.
    expect(statLayout(5, 532).cols).toBe(3);
    expect(statLayout(9, 532).cols).toBe(3);
    expect(statLayout(9, 1012).cols).toBe(5);
    expect(statLayout(6, 0).cols).toBe(3); // no width measured (jsdom): the tile count alone, as before
    expect(STAT_MIN_TILE).toBeGreaterThanOrEqual(150);
  });

  it('StatGrid counts its own and its extra tiles, measures its own width, and spans a lone last tile', () => {
    const src = read('lib/ui/StatGrid.svelte');
    expect(src).toMatch(/new ResizeObserver\(\(\[entry\]\) => \{ width = entry\.contentRect\.width; \}\);\s*ro\.observe\(grid\);/);
    expect(src).toMatch(/class:stat-grid--span-last=\{layout\.spanLast\}/);
    expect(styleBlocks(src)).toMatch(/\.stat-grid--span-last > :global\(:last-child\) \{\s*grid-column: 1 \/ -1;\s*\}/);
    const extra = createRawSnippet(() => ({ render: () => '<div class="stat-card x">extra</div>' }));
    const { container } = render(StatGrid, { props: { stats: [1, 2, 3, 4].map((i) => ({ label: `S${i}`, value: i })), extra } });
    expect((container.querySelector('.stat-grid') as HTMLElement).style.getPropertyValue('--cols')).toBe('5'); // 4 + 1 extra
  });

  it.each(['views/Overview.svelte', 'views/Decisions.svelte'])('%s: the grid with extra tiles no longer fixes its columns', (f) => {
    expect(markupOf(read(f))).toMatch(/<StatGrid stats=\{\[[\s\S]*?\]\}>\s*\{#snippet extra\(\)\}/);
  });

  it('Overview: the dot tiles are in the memory grid (no second grid), Execution integrity is three columns', () => {
    const src = read('views/Overview.svelte');
    expect(src).not.toMatch(/special-cards/);
    expect(markupOf(src)).toMatch(/<StatGrid stats=\{\[[\s\S]*?\]\}>\s*\{#snippet extra\(\)\}[\s\S]*?Graph density[\s\S]*?Brier score[\s\S]*?\{\/snippet\}\s*<\/StatGrid>/);
    expect((markupOf(src).match(/<StatGrid flat cols=\{3\}/g) ?? []).length).toBe(2);
  });

  it('Decisions: the Brier tile is in the StatGrid (no second grid)', () => {
    const src = read('views/Decisions.svelte');
    expect(src).not.toMatch(/indicator-row/);
    expect(markupOf(src)).toMatch(/<StatGrid stats=\{\[[\s\S]*?\]\}>\s*\{#snippet extra\(\)\}[\s\S]*?Latest Brier score[\s\S]*?\{\/snippet\}\s*<\/StatGrid>/);
  });
});

describe('Overview', () => {
  // Codex round 3, seen at a 1024px window: the Health card's link "Sessions where a tool failed, last 24 h" could not
  // wrap (white-space: nowrap) and pushed the page 67px sideways. A card link wraps at its spaces.
  it('a link in a card row may wrap, so a narrow card never widens the page', () => {
    expect(parseBlocks(styleBlocks(read('views/Overview.svelte'))).get('.item .k a')?.['white-space']).toBeUndefined();
  });

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
