import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseBlocks, styleBlocks } from './lib/testing/css';
import { filesUnder, sourceFiles } from './lib/testing/sources';

// Finish review fix 6 (the craft floor): no coloured side stripe. A callout, alert or list item is a tinted block at the
// world's radius, and a severity is said in a tinted pill. The companion's legacy themes keep their rules through
// --side-rule (0 on paper, 3px in a legacy theme).

const SRC = __dirname;
const style = (f: string) => styleBlocks(readFileSync(join(SRC, f), 'utf-8'));
/** A side or top border wider than 1px, written as a length (a token such as var(--side-rule) is the legacy switch). */
const STRIPE = /border-(?:left|right|top)(?:-width)?:\s*(?:[2-9]|\d{2,})px/;

/** Neutral rails drawn as an axis (a timeline's or a tree's, with the dots or the branches on it), not a stripe on a
 * block: a 2px rule in the border grey, on these selectors only. */
const RAILS: [string, string][] = [
  ['views/Heartbeat.svelte', '.timeline'],
  ['views/Retrieval.svelte', '.edge-list'],
  ['companion/catalog/TimelineView.svelte', 'li'],
];
const RAIL_RULE = '2px solid var(--border)';

describe('no coloured side stripe', () => {
  it('the pattern catches a 2px or wider side or top border and spares a 1px rule and the legacy token', () => {
    for (const bad of ['border-left: 3px solid var(--yellow);', 'border-right: 2px solid red;', 'border-top: 3px solid var(--ink);',
      'border-left-width: 4px;']) expect(bad, bad).toMatch(STRIPE);
    for (const ok of ['border-left: 1px solid var(--border);', 'border-top: var(--side-rule) solid var(--ink);', 'border: 2px solid x;']) {
      expect(ok, ok).not.toMatch(STRIPE);
    }
  });

  it.each([...sourceFiles().filter((f) => f.endsWith('.svelte')), ...filesUnder().filter((f) => f.startsWith('companion/') && f.endsWith('.svelte'))])(
    '%s', (file) => {
      const rail = RAILS.some(([f]) => f === file);
      const hits = style(file).split('\n').filter((l) => STRIPE.test(l) && !(rail && l.includes(RAIL_RULE)));
      expect(hits.map((l) => l.trim())).toEqual([]);
    });

  it.each(RAILS)('%s %s is still a neutral rail (no stale allowance)', (file, sel) => {
    expect(parseBlocks(style(file)).get(sel)?.['border-left']).toBe(RAIL_RULE);
  });

  it('the four callouts are tinted blocks at the world radius', () => {
    const obs = parseBlocks(style('views/Observability.svelte'));
    expect(obs.get('.anomaly')).toMatchObject({ background: 'var(--wash)', 'border-radius': 'var(--card-radius-sm)' });
    expect(obs.get('.trace-detail')).toMatchObject({ background: 'var(--wash)', 'border-radius': 'var(--card-radius-sm)' });
    expect(parseBlocks(style('views/DagView.svelte')).get('.detail-postmortem')).toMatchObject({ background: 'var(--wash)',
      'border-radius': 'var(--card-radius-sm)' });
    const con = parseBlocks(style('views/Consolidation.svelte'));
    expect(con.get('.diff-before pre')).toMatchObject({ background: 'var(--err-bg)' });
    expect(con.get('.diff-after pre')).toMatchObject({ background: 'var(--ok-bg)' });
  });

  it('a drift anomaly says its severity in a tinted pill', () => {
    const src = readFileSync(join(SRC, 'views/Observability.svelte'), 'utf-8');
    expect(src).toContain('<span class="anomaly-level status-pill" class:status-pill--bad={a.severity === \'alert\'} class:status-pill--warn={a.severity !== \'alert\'}>');
  });

  it('the companion ScoreCard keeps its top rule only in a legacy theme', () => {
    expect(parseBlocks(style('companion/catalog/ScoreCardView.svelte')).get('.score')?.['border-top']).toBe('var(--side-rule) solid var(--ink)');
  });
});
