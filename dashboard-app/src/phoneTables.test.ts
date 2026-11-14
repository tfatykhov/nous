import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { markupOf, parseBlocks, styleBlocks } from './lib/testing/css';

// Finish review fix 5: under 640px one table pattern. A list table is DataTable's stacked rows (cards mode), its expand
// button in the first labelled row instead of a row of its own; a genuinely wide numeric table scrolls in its own box
// (.table-scroll) with its first column fixed and a visible fade at the edge while there is more to see. No table is
// clipped at 390. A tile grid never leaves one lone tile on its last row.

const read = (f: string) => readFileSync(join(__dirname, f), 'utf-8');
const APP = parseBlocks(read('app.css'));
const STYLE = (f: string) => parseBlocks(styleBlocks(read(f)));

/** The rules inside a component's `@media (max-width: 640px)` block. */
function phoneRules(file: string): Map<string, Record<string, string>> {
  const css = styleBlocks(read(file));
  const at = css.indexOf('@media (max-width: 640px)');
  expect(at, `${file} has a 640px block`).toBeGreaterThan(-1);
  let depth = 0;
  let end = at;
  for (let i = css.indexOf('{', at); i < css.length; i++) {
    if (css[i] === '{') depth++;
    if (css[i] === '}' && --depth === 0) { end = i; break; }
  }
  return parseBlocks(css.slice(css.indexOf('{', at) + 1, end));
}

describe('phone tables', () => {
  it('a wide table scrolls in its own box: the first column stays, the edge fades (app.css .table-scroll)', () => {
    expect(APP.get('.table-scroll')).toMatchObject({ 'overflow-x': 'auto' });
    expect(APP.get('.table-scroll')?.background).toMatch(/var\(--surface\).*local.*scroll/);
    expect(APP.get('.table-scroll :is(th, td):first-child')).toMatchObject({ position: 'sticky', left: '0', background: 'var(--surface)' });
  });

  it.each([
    ['views/Autonomy.svelte', 6],
    ['views/Intentions.svelte', 6],
    ['views/DagView.svelte', 1],
    ['views/Retrieval.svelte', 3],
    ['views/Rubric.svelte', 1],
  ])('%s wraps its wide tables in .table-scroll (%i)', (file, n) => {
    const tags = markupOf(read(file)).match(/<div class="[^"]*\btable-scroll\b[^"]*">\s*<table/g) ?? [];
    expect(tags.length).toBeGreaterThanOrEqual(n);
  });

  it('list tables are stacked rows on a phone: Browse (all six) and the Recent DAGs', () => {
    const browse = markupOf(read('views/Browser.svelte'));
    expect(browse.match(/<DataTable\b/g)).toHaveLength(6);
    expect(browse.match(/mode="cards"/g)).toHaveLength(6);
    expect(markupOf(read('views/DagView.svelte'))).not.toMatch(/mode="scroll"/);
  });

  it('a stacked row has no row of its own for the expand button: it sits in the first labelled row', () => {
    const phone = phoneRules('lib/ui/DataTable.svelte');
    expect(phone.get('.dt--cards tr')).toMatchObject({ position: 'relative' });
    expect(phone.get('.dt--cards td.dt-disclose-col')).toMatchObject({ position: 'absolute', top: '0', right: '0' });
    expect(phone.get('.dt--cards td.dt-disclose-col + td')).toMatchObject({ 'padding-right': '3.5rem' });
  });

  it('a tile grid on a phone is two up, and an odd last tile takes the whole row', () => {
    // Any last tile, the extra tiles a view passes in included (design batch 1: Overview's and Decisions' dot tiles).
    expect(phoneRules('lib/ui/StatGrid.svelte').get('.stat-grid > :global(:last-child:nth-child(odd))'))
      .toMatchObject({ 'grid-column': '1 / -1' });
    // Scoring's seven dimension cards are two up at every width (three up left one alone on the desktop too).
    expect(STYLE('views/Rubric.svelte').get('.dim-cards')).toMatchObject({ 'grid-template-columns': 'repeat(2, minmax(0, 1fr))' });
    expect(STYLE('views/Rubric.svelte').get('.dim-card:last-child:nth-child(odd)')).toMatchObject({ 'grid-column': '1 / -1' });
    expect(phoneRules('lib/ui/StatGrid.svelte').get('.stat-grid')).toMatchObject({ 'grid-template-columns': 'repeat(2, minmax(0, 1fr))' });
  });
});

// Design batch 1, from the captures at 390 and 1440: what the first look at every screen showed.
describe('the details the captures showed', () => {
  it('Consolidation: the sleep journal has no table to scroll; on a phone a sleep\'s time sits above its sentence', () => {
    expect(markupOf(read('views/Consolidation.svelte'))).not.toMatch(/<table/);
    expect(STYLE('views/Consolidation.svelte').get('.clock')).toMatchObject({ 'white-space': 'nowrap' });
    expect(phoneRules('views/Consolidation.svelte').get('.sleep')).toMatchObject({ 'grid-template-columns': 'auto minmax(0, 1fr)', 'min-height': '44px' });
    expect(phoneRules('views/Consolidation.svelte').get('.what')).toMatchObject({ 'grid-column': '2' });
  });

  it('Intentions: the metric tiles are two up on a phone (they were one per row), an odd last tile across', () => {
    expect(phoneRules('views/Intentions.svelte').get('.tiles')).toMatchObject({ 'grid-template-columns': 'repeat(2, minmax(0, 1fr))' });
    expect(phoneRules('views/Intentions.svelte').get('.tiles > div:last-child:nth-child(odd)')).toMatchObject({ 'grid-column': '1 / -1' });
  });

  it('Retrieval: on a phone a legend family starts a new line (the hairline between families sat at line ends)', () => {
    expect(phoneRules('views/Retrieval.svelte').get('.disp-legend li.fam-sep')).toEqual({ 'flex-basis': '100%', height: '0', margin: '0' });
  });

  it.each(['views/health/GraphTab.svelte', 'views/health/GrowthTab.svelte'])('%s: the subtitle keeps its width; "updated" and Refresh wrap below it', (f) => {
    expect(STYLE(f).get('.view-head')).toMatchObject({ 'flex-wrap': 'wrap' });
    expect(STYLE(f).get('.subtitle')).toMatchObject({ flex: '1 1 20rem' });
  });

  it('Observability: a chart row with one chart gives it the full width (the error-rate card sat at half width)', () => {
    expect(STYLE('views/Observability.svelte').get('.chart-row > :only-child')).toEqual({ 'grid-column': '1 / -1' });
  });

  it('Health: the numbers in the three density tables are right-aligned in tabular figures, and the headers are sentence case', () => {
    expect(STYLE('views/health/GraphTab.svelte').get('.data-table .num')).toEqual({ 'text-align': 'right', 'font-variant-numeric': 'tabular-nums' });
    const m = markupOf(read('views/health/GraphTab.svelte'));
    expect((m.match(/<th class="num">/g) ?? []).length).toBe(4);
    expect((m.match(/<td class="num">/g) ?? []).length).toBe(4);
    expect(m).toContain('<th class="num">Auto-linked edges</th>');
  });
});

// Final step A3: the phone shell's two controls were under the 44px tap target (the menu 36px, the drawer's close 32px).
describe('the phone shell\'s controls are 44px tap targets', () => {
  it.each(['.hamburger', '.drawer-close'])('App.svelte %s is 44px square', (sel) => {
    expect(STYLE('App.svelte').get(sel)).toMatchObject({ width: '2.75rem', height: '2.75rem' });
  });

  it('the menu\'s attention dot keeps its place on the icon, and the drawer header keeps its height', () => {
    expect(STYLE('App.svelte').get('.hamburger-dot')).toMatchObject({ top: '0.5rem', right: '0.5rem' });
    expect(STYLE('App.svelte').get('.drawer-header')).toMatchObject({ padding: '0.75rem 1rem' });
  });
});
