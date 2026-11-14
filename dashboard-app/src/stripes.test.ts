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
/** A side or top border wider than 1px, written as a length (a token such as var(--side-rule) is the legacy switch), or
 * the same stripe drawn as an inset shadow offset 2px or more on one axis with no blur (final step: Model calls'
 * selected row was `inset 3px 0 0 var(--accent)`). An all-round inset ring (`inset 0 0 0 1px`) is not a stripe. */
const STRIPE = new RegExp([
  String.raw`border-(?:left|right|top)(?:-width)?:\s*(?:[2-9]|\d{2,})px`,
  String.raw`inset\s+-?(?:[2-9]|\d{2,})px\s+0(?:px)?\s+0(?:px)?\b`,
  String.raw`inset\s+0(?:px)?\s+-?(?:[2-9]|\d{2,})px\s+0(?:px)?\b`,
].join('|'));

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
      'border-left-width: 4px;', 'box-shadow: inset 3px 0 0 var(--accent);', 'box-shadow: inset -4px 0 0 red;',
      'box-shadow: inset 0 2px 0 var(--x);']) expect(bad, bad).toMatch(STRIPE);
    for (const ok of ['border-left: 1px solid var(--border);', 'border-top: var(--side-rule) solid var(--ink);', 'border: 2px solid x;',
      'box-shadow: inset 0 0 0 1px var(--x);', 'box-shadow: inset 1px 0 0 var(--x);', 'box-shadow: 0 2px 0 var(--x);']) {
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

  it('the three callouts are tinted blocks at the world radius', () => {
    const obs = parseBlocks(style('views/Observability.svelte'));
    expect(obs.get('.anomaly')).toMatchObject({ background: 'var(--wash)', 'border-radius': 'var(--card-radius-sm)' });
    expect(obs.get('.trace-detail')).toMatchObject({ background: 'var(--wash)', 'border-radius': 'var(--card-radius-sm)' });
    expect(parseBlocks(style('views/DagView.svelte')).get('.detail-postmortem')).toMatchObject({ background: 'var(--wash)',
      'border-radius': 'var(--card-radius-sm)' });
    // Consolidation's recorded before and after (a change in a shape the report does not know) are plain wash blocks.
    expect(parseBlocks(style('views/consolidation/SleepReport.svelte')).get('pre')).toMatchObject({ background: 'var(--wash)',
      'border-radius': 'var(--radius-sm)' });
  });

  it('a drift anomaly says its severity in a tinted pill', () => {
    const src = readFileSync(join(SRC, 'views/Observability.svelte'), 'utf-8');
    expect(src).toContain('<span class="anomaly-level status-pill" class:status-pill--bad={a.severity === \'alert\'} class:status-pill--warn={a.severity !== \'alert\'}>');
  });

  it('Model calls\' selected row is the selected tint the other lists use, with no stripe', () => {
    expect(parseBlocks(style('views/modelcalls/CallList.svelte')).get("a.row[aria-current='true']")).toEqual({ background: 'var(--accent-glow)' });
  });

  // Final step A7: Workflows' waiting card wore a purple 35% outline and a 4px tinted halo, a treatment no other card has.
  // It is a standard card: the purple dot, the heading and its count say that it waits on you.
  it('the Workflows waiting card is a standard card, with no tinted outline or halo', () => {
    expect(parseBlocks(style('views/DagView.svelte')).get('.waiting')).toMatchObject({ border: 'var(--card-border)', 'box-shadow': 'var(--card-shadow)' });
    expect(style('views/DagView.svelte')).not.toMatch(/0 0 0 4px/);
  });

  it('the companion ScoreCard keeps its top rule only in a legacy theme', () => {
    expect(parseBlocks(style('companion/catalog/ScoreCardView.svelte')).get('.score')?.['border-top']).toBe('var(--side-rule) solid var(--ink)');
  });
});
