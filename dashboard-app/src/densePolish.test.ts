import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { markupOf, parseBlocks, styleBlocks } from './lib/testing/css';
import StatGrid from './lib/ui/StatGrid.svelte';

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
    ['views/Overview.svelte', 2], // Execution integrity, in both of its states
    ['views/Subtasks.svelte', 1], // Upcoming schedules
    ['views/Harness.svelte', 1], // Most active censors
  ])('%s uses the flat tiles inside its card (%i)', (file, n) => {
    expect((markupOf(read(file)).match(/<StatGrid flat\b/g) ?? []).length).toBe(n);
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

describe('Retrieval flow bar', () => {
  it('each segment is a size container, and its count is hidden when the segment is too narrow for it', () => {
    const css = styleBlocks(read('views/Retrieval.svelte'));
    expect(parseBlocks(css).get('.funnel-seg')).toMatchObject({ 'container-type': 'inline-size' });
    expect(css).toMatch(/@container \(max-width: 2\.5rem\) \{\s*\.seg-label \{ display: none; \}\s*\}/);
  });
});
