import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseBlocks, styleBlocks } from './lib/testing/css';

// Finish review fix 2: one selected treatment for every switch, segmented control and filter pill, the white raised
// pill the Work filters and the sub-navigation use. The saturated blue is left to links and the question frame.

const SELECTED: [string, string][] = [
  ['lib/ui/SubNav.svelte', '.subnav-link.active'],
  ['views/work/SessionList.svelte', '.seg button.on'],
  ['lib/ui/FilterBar.svelte', '.filter-btn.active'],
  ['views/Retrieval.svelte', '.chip-btn.on'],
  ['companion/Companion.svelte', '.chip.active'],
];

const rule = (file: string, sel: string) => parseBlocks(styleBlocks(readFileSync(join(__dirname, file), 'utf-8'))).get(sel);

describe('one selected treatment: the white raised pill', () => {
  it.each(SELECTED)('%s %s', (file, sel) => {
    expect(rule(file, sel), `${file} ${sel}`).toMatchObject({
      background: 'var(--tab-active-bg)',
      color: 'var(--text)',
      'font-weight': '600',
      'box-shadow': 'var(--tab-active-shadow)',
    });
    expect(JSON.stringify(rule(file, sel))).not.toMatch(/--accent\b|--accent-glow|--accent-dim/);
  });

  it('a pill group sitting inside a white card has a tinted track, so the white pill shows (Retrieval)', () => {
    expect(rule('views/Retrieval.svelte', '.filters')).toMatchObject({ background: 'var(--wash)' });
  });
});
