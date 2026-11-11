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
    ['views/Consolidation.svelte', 1],
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
    for (const [file, sel] of [['lib/ui/StatGrid.svelte', '.stat-card'], ['views/Overview.svelte', '.special-cards > *']] as const) {
      const phone = phoneRules(file);
      expect(phone.get(`${sel}:last-child:nth-child(odd)`), `${file} ${sel}`).toMatchObject({ 'grid-column': '1 / -1' });
    }
    // Scoring's seven dimension cards are two up at every width (three up left one alone on the desktop too).
    expect(STYLE('views/Rubric.svelte').get('.dim-cards')).toMatchObject({ 'grid-template-columns': 'repeat(2, minmax(0, 1fr))' });
    expect(STYLE('views/Rubric.svelte').get('.dim-card:last-child:nth-child(odd)')).toMatchObject({ 'grid-column': '1 / -1' });
    expect(phoneRules('lib/ui/StatGrid.svelte').get('.stat-grid')).toMatchObject({ 'grid-template-columns': 'repeat(2, minmax(0, 1fr))' });
  });
});
