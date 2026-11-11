import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { markupOf, parseBlocks, styleBlocks } from './lib/testing/css';
import { sourceFiles } from './lib/testing/sources';

// Finish review fix 8: one page header order on every screen: the sub-navigation above the title, and at the right
// "updated" then Refresh, once. A screen that fetches gets a page Refresh; a lower Refresh that refreshes what the page
// one does goes.

const markup = (f: string) => markupOf(readFileSync(join(__dirname, f), 'utf-8'));
const REFRESH = /<(?:Button|button)\b(?:=>|[^>])*>\s*(?:\{[^}]*'Refresh'[^}]*\}|Refresh)\s*<\/(?:Button|button)>/g;

/** Every screen (or tab) with a page header that fetches through a poll store. */
const PAGES = ['views/Admission.svelte', 'views/Autonomy.svelte', 'views/Cache.svelte', 'views/Consolidation.svelte',
  'views/DagView.svelte', 'views/Decisions.svelte', 'views/GraphView.svelte', 'views/Harness.svelte', 'views/Heartbeat.svelte',
  'views/Intentions.svelte', 'views/Ledger.svelte', 'views/MemoryWriteHealth.svelte', 'views/Observability.svelte',
  'views/Overview.svelte', 'views/Retrieval.svelte', 'views/Rubric.svelte', 'views/Subtasks.svelte', 'views/health/GraphTab.svelte',
  'views/health/GrowthTab.svelte', 'views/modelcalls/CallList.svelte', 'views/work/Patterns.svelte', 'views/work/SessionList.svelte',
  'views/work/Trace.svelte'];

// Design batch 2 (verdict pass 1): on a phone Workflows and Heartbeat kept "updated" and Refresh beside the title and
// squeezed the subtitle, while Autonomy, Guards, Consolidation and Intentions wrapped them under it; and Intentions
// put them under its long subtitle on the desktop too. One shared rule in app.css: the header wraps, and the title
// block starts at 20rem and grows, so the right-hand group sits at the right when there is room and below when not.
describe('one header rule', () => {
  const APP = parseBlocks(readFileSync(join(__dirname, 'app.css'), 'utf-8'));
  it('app.css: the header wraps, and its title block starts at 20rem and grows', () => {
    expect(APP.get('.view-head')).toEqual({ 'flex-wrap': 'wrap' });
    expect(APP.get('.view-head > :first-child')).toEqual({ flex: '1 1 20rem', 'min-width': '0' });
  });
  it.each(sourceFiles().filter((f) => f.endsWith('.svelte') && /class="view-head"/.test(readFileSync(join(__dirname, f), 'utf-8'))))(
    '%s: its own header rule neither stops the wrap nor fixes the title width', (f) => {
      const css = parseBlocks(styleBlocks(readFileSync(join(__dirname, f), 'utf-8')));
      expect(css.get('.view-head')?.['flex-wrap'] ?? 'wrap').toBe('wrap');
      for (const [sel, decl] of css) if (/^\.view-head\s*>/.test(sel)) expect(decl.flex, sel).toBeUndefined();
    });
});

describe('one page header order', () => {
  it.each(PAGES)('%s: one Refresh, right after the "updated" badge', (file) => {
    const m = markup(file);
    const refreshes = [...m.matchAll(REFRESH)];
    expect(refreshes, 'exactly one Refresh').toHaveLength(1);
    const badge = m.indexOf('<StaleBadge');
    expect(badge, 'an updated badge').toBeGreaterThan(-1);
    const between = m.slice(badge, refreshes[0].index);
    expect(between.match(/<StaleBadge[^>]*\/>\s*$/), 'the badge, then the Refresh').not.toBeNull();
  });

  it('the sub-navigation sits above the title (Health, Browse; Work and Model calls already did)', () => {
    const health = markup('views/Health.svelte');
    expect(health.indexOf('<SubNav')).toBeLessThan(health.indexOf('<h1'));
    const browse = markup('views/Browser.svelte');
    expect(browse.indexOf('role="tablist"')).toBeLessThan(browse.indexOf('<h1'));
  });

  it('"updated" is said once on Health\'s graph tab (the orphan banner had its own)', () => {
    expect(markup('views/health/GraphTab.svelte').match(/<StaleBadge/g)).toHaveLength(1);
  });
});
