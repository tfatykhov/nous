import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { markupOf, scriptBlocks, styleBlocks } from './lib/testing/css';

// The redesign's colours live in app.css, as tokens per ground. Inside the <style> of every file the redesign has
// touched there is no colour literal: no `#hex` and no `rgb(`/`rgba(`, only var(--token). A literal would keep its
// dark value on the paper ground (or the reverse). Each task appends the files it migrates; the script and the markup
// are SCRIPT_GUARDED's (below).
export const HEX_GUARDED: readonly string[] = [
  'lib/ui/Nav.svelte',
  'lib/ui/SubNav.svelte',
  'lib/ui/DataTable.svelte',
  'lib/ui/FilterBar.svelte',
  'lib/ui/LookToggle.svelte',
  'App.svelte',
  'lib/ui/StatGrid.svelte',
  'lib/ui/StaleBadge.svelte',
  'lib/ui/card/card.svelte',
  'lib/ui/button/button.svelte',
  'lib/ui/badge/badge.svelte',
  'views/work/Trace.svelte',
  'views/Work.svelte',
  'views/work/SessionList.svelte',
  'views/work/Patterns.svelte',
  'views/work/FullResult.svelte',
  'views/Overview.svelte',
  'lib/viz/DayTimeline.svelte',
  'lib/viz/Chart.svelte',
  'lib/ui/Ground.svelte',
  'lib/ui/HoldUndo.svelte',
  'views/GraphView.svelte',
  'lib/viz/Graph.svelte',
  'lib/ui/BottomSheet.svelte',
  'views/Browser.svelte',
  'views/Health.svelte',
  'views/health/GraphTab.svelte',
  'views/health/GrowthTab.svelte',
  'views/health/WritesTab.svelte',
  'views/MemoryWriteHealth.svelte',
  'views/Admission.svelte',
  'views/Retrieval.svelte',
  'views/Consolidation.svelte',
];

const SRC = __dirname;
const LITERAL = /#[0-9a-fA-F]{3,8}\b|rgba?\(/;

describe('no colour literal in a migrated component style', () => {
  it('the pattern catches hex and rgb(a), and spares tokens, ids and comments', () => {
    for (const bad of ['color: #fff;', 'background: #12304f;', 'border: 1px solid rgba(0, 0, 0, 0.1);', 'fill: rgb(1 2 3);']) {
      expect(bad).toMatch(LITERAL);
    }
    for (const ok of ['color: var(--text);', ':global(#app) { display: flex; }', 'grid-area: main;']) {
      expect(ok).not.toMatch(LITERAL);
    }
    expect(styleBlocks('<style>/* #a99df9 on the glow */ a { color: var(--x); }</style>')).not.toMatch(LITERAL);
  });

  it.each(HEX_GUARDED)('%s', (file) => {
    const path = join(SRC, file);
    expect(existsSync(path), `${file} is missing`).toBe(true);
    const css = styleBlocks(readFileSync(path, 'utf-8'));
    const hits = css.split('\n').filter((line) => LITERAL.test(line)).map((l) => l.trim());
    expect(hits).toEqual([]);
  });
});

// Review I4: the views the redesign migrated keep no colour literal in their <script> or their markup either. A canvas
// colour is a token (`var(--x)`, or the `tint(--x, a)` sentinel) that lib/chartColors.ts resolves against the ground
// the chart sits in; DOM and SVG colours use var() and color-mix() directly. chartColors.ts holds the one table of dark
// literals (its DARK_FALLBACK), proven equal to app.css by chartColors.test.ts, so it is not listed here.
export const SCRIPT_GUARDED: readonly string[] = [
  'views/Overview.svelte',
  'views/Work.svelte',
  'views/work/Trace.svelte',
  'views/work/SessionList.svelte',
  'views/work/Patterns.svelte',
  'views/work/FullResult.svelte',
  'lib/viz/DayTimeline.svelte',
  'lib/viz/Chart.svelte',
  'lib/ui/StatGrid.svelte',
  'lib/ui/StaleBadge.svelte',
  'lib/ui/SubNav.svelte',
  'lib/ui/Ground.svelte',
  'lib/ui/HoldUndo.svelte',
  'lib/ui/button/button.svelte',
  'views/GraphView.svelte',
  'lib/viz/Graph.svelte',
  'lib/ui/BottomSheet.svelte',
  'views/Browser.svelte',
  'lib/ui/DataTable.svelte',
  'views/Health.svelte',
  'views/health/GraphTab.svelte',
  'views/health/GrowthTab.svelte',
  'views/health/WritesTab.svelte',
  'views/MemoryWriteHealth.svelte',
  'views/Admission.svelte',
  'lib/ui/FilterBar.svelte',
  'views/Retrieval.svelte',
  'views/Consolidation.svelte',
];

/** A literal colour, or a two-hex-digit alpha glued onto one (`c + '33'`, `"{color}20"`, `` `${color}30` ``), which
 * cannot work on a var(). */
const LITERAL_SCRIPT = /#[0-9a-fA-F]{3,8}\b|rgba?\(|\+\s*['"`][0-9a-fA-F]{2}['"`]|\}[0-9a-fA-F]{2}['"`]/;

describe('no colour literal in a migrated view script or markup', () => {
  it('the helpers keep code and drop comments', () => {
    const src = "<script>\n// '#34d399' in a comment\nconst a = '#34d399'; /* rgba(1,2,3,.1) */\nconst u = 'http://x';\n</script>";
    const js = scriptBlocks(src);
    expect(js).toContain("const a = '#34d399';");
    expect(js).toContain("'http://x'");
    expect(js).not.toContain('in a comment');
    expect(js).not.toContain('rgba(1,2,3');
    expect(scriptBlocks("<script>const c = 'rgba(124,106,247,0.5)';</script>")).toMatch(LITERAL);
    expect(scriptBlocks("<script>const h = routeHref('dag'); const v = 'var(--green)';</script>")).not.toMatch(LITERAL);
    const sv = '<script>const x = 1;</script>\n<!-- #34d399 in a comment -->\n<p class="a">{x}</p>\n<style>p { color: #fff; }</style>';
    expect(markupOf(sv)).toContain('<p class="a">{x}</p>');
    expect(markupOf(sv)).not.toMatch(/const x|#34d399|#fff/);
  });

  it('the script pattern catches hex, rgb(a) and an alpha suffix, and spares tokens and tints', () => {
    for (const bad of ["const c = '#34d399';", 'const t = rgba(1,2,3,.1);', "const t = color + '33';", "outcomeColor(o) + '20'",
      'style:background="{typeColor(head.type)}20"', 'fill: `${color}30`']) {
      expect(bad, bad).toMatch(LITERAL_SCRIPT);
    }
    for (const ok of ["const v = 'var(--series-1)';", "const t = tint('--series-1', 0.1);", "routeHref('dag')", 'href="#/health/graph"',
      "const n = count + '1';", 'style:background={typeVar(head.type)}', '{n} seeds']) {
      expect(ok, ok).not.toMatch(LITERAL_SCRIPT);
    }
  });

  it.each(SCRIPT_GUARDED)('%s', (file) => {
    const path = join(SRC, file);
    expect(existsSync(path), `${file} is missing`).toBe(true);
    const source = readFileSync(path, 'utf-8');
    const lines = [...scriptBlocks(source).split('\n'), ...markupOf(source).split('\n')];
    const hits = lines.filter((line) => LITERAL_SCRIPT.test(line)).map((l) => l.trim());
    expect(hits).toEqual([]);
  });
});
