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
  'views/ModelCalls.svelte',
  'views/modelcalls/CallList.svelte',
  'views/modelcalls/CallDetail.svelte',
  'views/modelcalls/CallBlock.svelte',
  'views/Cache.svelte',
  'views/DagView.svelte',
  'lib/viz/Dag.svelte',
  'views/Subtasks.svelte',
];

const SRC = __dirname;
const LITERAL = /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\((?!\s*var\()|oklch\((?!\s*var\()/; // hsl(var(--primary)) is a token

describe('no colour literal in a migrated component style', () => {
  it('the pattern catches hex and rgb(a), and spares tokens, ids and comments', () => {
    for (const bad of ['color: #fff;', 'background: #12304f;', 'border: 1px solid rgba(0, 0, 0, 0.1);', 'fill: rgb(1 2 3);', 'color: hsl(210 50% 40%);', 'color: oklch(0.6 0.1 200);']) {
      expect(bad).toMatch(LITERAL);
    }
    for (const ok of ['color: var(--text);', ':global(#app) { display: flex; }', 'grid-area: main;', 'background: hsl(var(--primary));']) {
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

// Review M3 (PR 2): on paper the status fills (--green, --yellow, --red) are marks; as text they fall below 4.5:1 on the
// page (red is 4.35:1 on #eef2f3). Text takes --ok-text, --warn-text or --err-text, which equal the fills under dark.
const STATUS_FILL_AS_TEXT = /(^|[^-\w])color:\s*var\(--(red|green|yellow)\)/;

describe('a migrated style never sets a status fill as a text colour', () => {
  it('the pattern catches text, and spares borders, backgrounds and accent colours', () => {
    expect('.x { color: var(--red); }').toMatch(STATUS_FILL_AS_TEXT);
    expect('  color: var(--green);').toMatch(STATUS_FILL_AS_TEXT);
    for (const ok of ['border-color: var(--red);', 'background-color: var(--green);', 'accent-color: var(--yellow);', 'color: var(--err-text);']) {
      expect(ok, ok).not.toMatch(STATUS_FILL_AS_TEXT);
    }
  });

  it.each(HEX_GUARDED)('%s', (file) => {
    const css = styleBlocks(readFileSync(join(SRC, file), 'utf-8'));
    expect(css.split('\n').filter((line) => STATUS_FILL_AS_TEXT.test(line)).map((l) => l.trim())).toEqual([]);
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
  'lib/ui/badge/badge.svelte', // review R1: Overview renders it through a re-exporting index.ts
  'views/ModelCalls.svelte',
  'views/modelcalls/CallList.svelte',
  'views/modelcalls/CallDetail.svelte',
  'views/modelcalls/CallBlock.svelte',
  'views/Cache.svelte',
  'views/DagView.svelte',
  'lib/viz/Dag.svelte',
  'views/Subtasks.svelte',
];

/** A literal colour (hex, rgb/hsl/oklch, a named colour in a fill/stroke attribute), or a two-hex-digit alpha glued onto
 * one (`c + '33'`, `"{color}20"`, `{c}55;` mid-string, `` `${color}30` ``), which cannot work on a var(). The suffix is
 * found by what follows the brace (not a letter, digit, `_` or `-`), not by a closing quote (review I2). */
const LITERAL_SCRIPT = new RegExp(
  [
    String.raw`#[0-9a-fA-F]{3,8}\b`,
    String.raw`rgba?\(`,
    String.raw`hsla?\((?!\s*var\()`, // hsl(var(--primary)) is a token
    String.raw`oklch\((?!\s*var\()`,
    String.raw`\+\s*['"` + '`' + String.raw`][0-9a-fA-F]{2}['"` + '`' + ']',
    String.raw`\}[0-9a-fA-F]{2}(?![0-9a-zA-Z_-])`,
    String.raw`\b(?:fill|stroke|stop-color|flood-color|lighting-color)=["'](?!(?:none|currentColor|transparent|inherit)["'])[a-zA-Z]+["']`,
  ].join('|'),
);

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

  it('the script pattern catches hex, rgb(a), hsl(a), oklch, a named fill or stroke and an alpha suffix; spares tokens and tints', () => {
    for (const bad of ["const c = '#34d399';", 'const t = rgba(1,2,3,.1);', "const t = color + '33';", "outcomeColor(o) + '20'",
      'style:background="{typeColor(head.type)}20"', 'fill: `${color}30`',
      // Review I2: a suffix in the middle of a string, other colour functions, a named colour attribute.
      'style="border-color: {c}55; color: {c}"', 'style="background: {s.color}30; flex: 1"', '`${c}55 solid`',
      'fill="hsl(210 50% 40%)"', 'fill="white"', "stroke='black'", 'const c = hsla(1, 2%, 3%, .5);', 'color: oklch(0.6 0.1 200)']) {
      expect(bad, bad).toMatch(LITERAL_SCRIPT);
    }
    for (const ok of ["const v = 'var(--series-1)';", "const t = tint('--series-1', 0.1);", "routeHref('dag')", 'href="#/health/graph"',
      "const n = count + '1';", 'style:background={typeVar(head.type)}', '{n} seeds', 'fill="none" stroke="currentColor"',
      '`${n}ms`', '{count} items', '{a}{b}', "`${x}px`", 'background: hsl(var(--primary))']) {
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

// Review I1, second gap: a Tailwind colour class is a literal too. A palette colour (bg-black/10, text-red-500) keeps
// one value under both looks; foreground, muted, muted-foreground and popover are deliberately unmapped (app.css's
// @theme inline), so those classes emit nothing.
const TAILWIND_COLOUR = new RegExp(
  String.raw`(?<![\w-])(?:bg|text|border|ring|outline|fill|stroke|from|via|to|decoration|shadow|accent|caret|divide|placeholder)-` +
    String.raw`(?:black|white|(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3})(?![\w-])` +
    String.raw`|(?<![\w-])(?:text-foreground|text-muted-foreground|bg-muted|bg-popover|text-popover-foreground)(?![\w-])`,
);

describe('no Tailwind colour class in a migrated markup or script', () => {
  it('the pattern catches palette and unmapped colour classes, and spares tokens and sizes', () => {
    for (const bad of ['class="bg-black/10 fixed"', 'class="text-red-500"', 'class="text-foreground flex"', 'class="bg-muted mx-auto"',
      'class="hover:bg-white"', 'class="bg-popover text-popover-foreground"']) {
      expect(bad, bad).toMatch(TAILWIND_COLOUR);
    }
    for (const ok of ['class="bg-primary text-[var(--on-accent)]"', 'class="bg-clip-padding text-sm"', 'class="text-destructive"',
      'class="border-[var(--border)] shadow-[var(--card-shadow)]"', 'class="bg-muted-x"']) {
      expect(ok, ok).not.toMatch(TAILWIND_COLOUR);
    }
  });

  // Review R1: shadcn components keep their classes in a module script (tailwind-variants), so the script is read too.
  it.each(SCRIPT_GUARDED)('%s', (file) => {
    const source = readFileSync(join(SRC, file), 'utf-8');
    const lines = [...scriptBlocks(source).split('\n'), ...markupOf(source).split('\n')];
    expect(lines.filter((line) => TAILWIND_COLOUR.test(line)).map((l) => l.trim())).toEqual([]);
  });

  it('a class string in a module script is caught', () => {
    const src = '<script lang="ts" module>\n  const v = tv({ variants: { ghost: "hover:bg-muted hover:text-muted-foreground" } });\n</script>';
    expect(scriptBlocks(src)).toMatch(TAILWIND_COLOUR);
  });

  it('the drawer primitives (allowed in paperRouteCoverage) are where these classes live today', () => {
    const overlay = markupOf(readFileSync(join(SRC, 'lib/ui/drawer/drawer-overlay.svelte'), 'utf-8'));
    expect(overlay).toMatch(TAILWIND_COLOUR);
  });
});
