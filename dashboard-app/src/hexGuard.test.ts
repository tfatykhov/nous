import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { scriptBlocks, styleBlocks } from './lib/testing/css';

// The redesign's colours live in app.css, as tokens per ground. Inside the <style> of every file the redesign has
// touched there is no colour literal: no `#hex` and no `rgb(`/`rgba(`, only var(--token). A literal would keep its
// dark value on the paper ground (or the reverse). Each task appends the files it migrates; script-side chart
// colours are out of scope.
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

// Review I4: the views PR 1 migrated keep no colour literal in their <script> either (a canvas chart's colours are read
// from the ground's tokens at build time, lib/overviewCharts.ts). Script-side literals elsewhere are PR 2's.
export const SCRIPT_GUARDED: readonly string[] = [
  'views/Overview.svelte',
  'views/Work.svelte',
  'views/work/Trace.svelte',
  'views/work/SessionList.svelte',
  'views/work/Patterns.svelte',
  'views/work/FullResult.svelte',
  'lib/viz/DayTimeline.svelte',
];

describe('no colour literal in a migrated view script', () => {
  it('the helper keeps code and drops comments', () => {
    const src = "<script>\n// '#34d399' in a comment\nconst a = '#34d399'; /* rgba(1,2,3,.1) */\nconst u = 'http://x';\n</script>";
    const js = scriptBlocks(src);
    expect(js).toContain("const a = '#34d399';");
    expect(js).toContain("'http://x'");
    expect(js).not.toContain('in a comment');
    expect(js).not.toContain('rgba(1,2,3');
    expect(scriptBlocks("<script>const c = 'rgba(124,106,247,0.5)';</script>")).toMatch(LITERAL);
    expect(scriptBlocks("<script>const h = routeHref('dag'); const v = 'var(--green)';</script>")).not.toMatch(LITERAL);
  });

  it.each(SCRIPT_GUARDED)('%s', (file) => {
    const js = scriptBlocks(readFileSync(join(SRC, file), 'utf-8'));
    const hits = js.split('\n').filter((line) => LITERAL.test(line)).map((l) => l.trim());
    expect(hits).toEqual([]);
  });
});
