import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { markupOf, scriptBlocks, styleBlocks } from './lib/testing/css';
import { sourceFiles } from './lib/testing/sources';
import { TOKEN_FALLBACK } from './lib/chartColors';

// The dashboard's colours live in app.css, as tokens. No source file holds a colour literal: no `#hex`, `rgb(`/`rgba(`,
// hsl or oklch in a component's <style>, <script> or markup, or in a module's code; only var(--token). PR 7: the guards
// walk every source file (lib/testing/sources.ts) instead of the two lists the screens joined one PR at a time, so a new
// file is guarded the moment it exists. A file that must hold a literal is in ALLOWED below, with its reason and the
// lines it may hold; an allowance that no longer matches anything fails (none is stale).

const SRC = __dirname;
const SOURCES = sourceFiles();
const COMPONENTS = SOURCES.filter((f) => f.endsWith('.svelte'));

const read = (file: string) => readFileSync(join(SRC, file), 'utf-8');
/** What a file runs and shows, comments removed: a component's script and markup, or a module's code. */
function codeLines(file: string): string[] {
  const src = read(file);
  if (file.endsWith('.svelte')) return [...scriptBlocks(src).split('\n'), ...markupOf(src).split('\n')];
  return scriptBlocks(`<script>${src}</script>`).split('\n');
}

type Guard = 'script literal' | 'tailwind colour';
/** What a guard lets the files `files` hold: the text `allow` matches is removed from each line before the guard reads
 * it, so anything else on the same line still fails. */
type Allowance = { files: RegExp; guard: Guard; allow: RegExp; reason: string };
/** The shadcn primitives' unmapped colour classes (app.css's @theme inline maps no foreground, muted or popover colour). */
const UNMAPPED = /(?<![\w-])(?:text-foreground|text-muted-foreground|bg-muted|bg-popover|text-popover-foreground)(?:\/\d+)?(?![\w-])/g;
const ALLOWED: Allowance[] = [
  {
    files: /^lib\/chartColors\.ts$/, guard: 'script literal', allow: /^\s*'--[\w-]+': '(?:#[0-9a-f]{6}|rgba\(\d+, \d+, \d+, [\d.]+\))',\s*$/g,
    reason: "TOKEN_FALLBACK's rows: the :root value of each token a chart names, for a canvas drawn where no stylesheet " +
      'applies (jsdom); chartColors.test.ts proves every row equal to app.css',
  },
  {
    files: /^lib\/(?:chartColors|overviewCharts)\.ts$/, guard: 'script literal', allow: /`rgba\(\$\{/g,
    reason: 'a resolved token written as rgba() at an alpha for the canvas: the channels are computed, not a colour',
  },
  {
    files: /^lib\/ui\/drawer\//, guard: 'tailwind colour', allow: UNMAPPED,
    reason: "shadcn's drawer as generated: its foreground, muted and popover classes map to no token and emit no " +
      'colour (BottomSheet gives the drawer its surface)',
  },
  {
    files: /^lib\/ui\/drawer\/drawer-overlay\.svelte$/, guard: 'tailwind colour', allow: /(?<![\w-])bg-black\/10(?![\w-])/g,
    reason: "the overlay's scrim, black at 10%: a shade of the page under it, not a palette colour",
  },
];

/** The lines of `file` that hit `pattern`, once the allowances for `guard` are removed. */
function hits(file: string, guard: Guard | null, lines: string[], pattern: RegExp): string[] {
  const allow = ALLOWED.filter((a) => a.guard === guard && a.files.test(file));
  return lines.filter((l) => pattern.test(allow.reduce((s, a) => s.replace(a.allow, ''), l))).map((l) => l.trim());
}

const LITERAL = /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\((?!\s*var\()|oklch\((?!\s*var\()/; // hsl(var(--primary)) is a token

describe('no colour literal in a component style', () => {
  it('the pattern catches hex and rgb(a), and spares tokens, ids and comments', () => {
    for (const bad of ['color: #fff;', 'background: #12304f;', 'border: 1px solid rgba(0, 0, 0, 0.1);', 'fill: rgb(1 2 3);', 'color: hsl(210 50% 40%);', 'color: oklch(0.6 0.1 200);']) {
      expect(bad).toMatch(LITERAL);
    }
    for (const ok of ['color: var(--text);', ':global(#app) { display: flex; }', 'grid-area: main;', 'background: hsl(var(--primary));']) {
      expect(ok).not.toMatch(LITERAL);
    }
    expect(styleBlocks('<style>/* #123456 in a note */ a { color: var(--x); }</style>')).not.toMatch(LITERAL);
  });

  it.each(COMPONENTS)('%s', (file) => {
    expect(hits(file, null, styleBlocks(read(file)).split('\n'), LITERAL)).toEqual([]);
  });
});

// Code review M10: a CSS colour name is a literal too (`border: 1px solid white`, `color-mix(…, black)`). It is read in
// the value of a declaration only, so a selector, a property name (`white-space`) and a token name (`--gray-1`) are not.
// CSS colour names are case-insensitive (`Navy`), and a value can run over several lines (a wrapped shadow list).
const NAMED = /(?<![\w-])(?:white|black|red|green|blue|gr[ae]y|silver|orange|yellow|purple|pink|navy|teal|maroon|olive|gold|brown)(?![\w-])/i;
/** The values of a stylesheet's declarations. The text is cut at `{`, `}` and `;`: a piece ended by `;` or `}` (or the
 * text's end) is a declaration, `prop: value` over as many lines as it takes; a piece ended by `{` is a selector. */
function declValues(css: string): string[] {
  return [...css.matchAll(/([^{};]*)([{};]|$)/g)]
    .filter((m) => m[2] !== '{')
    .map((m) => /^\s*-?-?[a-zA-Z][\w-]*\s*:([\s\S]*)$/.exec(m[1])?.[1].trim())
    .filter((v): v is string => v !== undefined && v !== '');
}
const namedHits = (css: string) => declValues(css).filter((v) => NAMED.test(v));

describe('no named colour in a component style', () => {
  it('the pattern catches a colour name in a value, and spares property names, tokens, keywords and selectors', () => {
    for (const bad of ['  border: 1px solid white;', '  background: color-mix(in srgb, var(--accent) 80%, black);', '.x { color: grey; }',
      '  outline-color: Navy;', '  box-shadow: 0 0 0 2px var(--surface), 0 0 0 4px blue',
      '.x {\n  box-shadow:\n    0 0 0 2px var(--surface),\n    0 0 0 4px white;\n}']) {
      expect(namedHits(bad), bad).toHaveLength(1);
    }
    for (const ok of ['  white-space: nowrap;', '  background: transparent;', '  fill: currentColor;', '  color: var(--gray-1);',
      '  border-color: var(--red);', '  --tone: var(--green);', '.chip.t-red:hover {', '.a:not(.white),\n.b {\n  color: var(--text);\n}',
      '@media (max-width: 40rem) {\n  .x { animation: pulse-red 1s; }\n}', '  grid-area: main;']) {
      expect(namedHits(ok), ok).toEqual([]);
    }
  });

  it.each(COMPONENTS)('%s', (file) => {
    expect(namedHits(styleBlocks(read(file)))).toEqual([]);
  });
});

// Review M3 (PR 2): the status fills (--green, --yellow, --red) are marks; as text they fall below 4.5:1 on the page
// (red is 4.35:1 on #eef2f3). Text takes --ok-text, --warn-text or --err-text.
// PR 5 re-review R3: the mark tokens of N1 as well (`--oc-*`, `--series-N`, `--signal-*`), and var() with a space or a
// fallback.
const STATUS_FILL_AS_TEXT = /(^|[^-\w])color:\s*var\(\s*--(?:red|green|yellow|oc-[\w-]+|series-\d+|signal-[\w-]+)(?![\w-])/;

describe('a component style never sets a status fill as a text colour', () => {
  it('the pattern catches text, and spares borders, backgrounds and accent colours', () => {
    expect('.x { color: var(--red); }').toMatch(STATUS_FILL_AS_TEXT);
    expect('  color: var(--green);').toMatch(STATUS_FILL_AS_TEXT);
    // PR 5 re-review R3: the mark tokens N1 added to the markup check, and var() with a space or a fallback.
    for (const bad of ['.x { color: var(--oc-success); }', '.x { color: var(--series-2); }', '.x { color: var(--signal-praised); }',
      '.x { color: var( --oc-success ); }', '.x { color: var(--oc-success, green); }']) {
      expect(bad, bad).toMatch(STATUS_FILL_AS_TEXT);
    }
    for (const ok of ['border-color: var(--red);', 'background-color: var(--green);', 'accent-color: var(--yellow);', 'color: var(--err-text);',
      'color: var(--st-partial);']) {
      expect(ok, ok).not.toMatch(STATUS_FILL_AS_TEXT);
    }
  });

  it.each(COMPONENTS)('%s', (file) => {
    const css = styleBlocks(read(file));
    expect(css.split('\n').filter((line) => STATUS_FILL_AS_TEXT.test(line)).map((l) => l.trim())).toEqual([]);
  });
});

// Review I4: no colour literal in a <script>, the markup or a module either. A canvas colour is a token (`var(--x)`, or
// the `tint(--x, a)` sentinel) that lib/chartColors.ts resolves against the element the chart sits in; DOM and SVG
// colours use var() and color-mix() directly.

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

describe('no colour literal in a script, the markup or a module', () => {
  it('the helpers keep code and drop comments', () => {
    const src = "<script>\n// '#34d399' in a comment\nconst a = '#34d399'; /* rgba(1,2,3,.1) */\nconst u = 'http://x';\n</script>";
    const js = scriptBlocks(src);
    expect(js).toContain("const a = '#34d399';");
    expect(js).toContain("'http://x'");
    expect(js).not.toContain('in a comment');
    expect(js).not.toContain('rgba(1,2,3');
    expect(scriptBlocks("<script>const c = 'rgba(1,2,3,0.5)';</script>")).toMatch(LITERAL);
    expect(scriptBlocks("<script>const h = routeHref('dag'); const v = 'var(--green)';</script>")).not.toMatch(LITERAL);
    const sv = '<script>const x = 1;</script>\n<!-- #34d399 in a comment -->\n<p class="a">{x}</p>\n<style>p { color: #fff; }</style>';
    expect(markupOf(sv)).toContain('<p class="a">{x}</p>');
    expect(markupOf(sv)).not.toMatch(/const x|#34d399|#fff/);
  });

  it('a module is read as code, comments removed', () => {
    const mod = "// '#34d399' in a comment\n/** `#12304f` in a doc */\nexport const a = '#34d399';\n";
    expect(scriptBlocks(`<script>${mod}</script>`).split('\n').filter((l) => LITERAL_SCRIPT.test(l)).map((l) => l.trim()))
      .toEqual(["export const a = '#34d399';"]);
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

  it.each(SOURCES)('%s', (file) => {
    expect(hits(file, 'script literal', codeLines(file), LITERAL_SCRIPT)).toEqual([]);
  });
});

// Review I1, second gap: a Tailwind colour class is a literal too. A palette colour (bg-black/10, text-red-500) is a
// fixed value outside the tokens; foreground, muted, muted-foreground and popover are deliberately unmapped (app.css's
// @theme inline), so those classes emit nothing.
const TAILWIND_COLOUR = new RegExp(
  String.raw`(?<![\w-])(?:bg|text|border|ring|outline|fill|stroke|from|via|to|decoration|shadow|accent|caret|divide|placeholder)-` +
    String.raw`(?:black|white|(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3})(?![\w-])` +
    String.raw`|(?<![\w-])(?:text-foreground|text-muted-foreground|bg-muted|bg-popover|text-popover-foreground)(?![\w-])`,
);

describe('no Tailwind colour class in a markup, script or module', () => {
  it('the pattern catches palette and unmapped colour classes, and spares tokens and sizes', () => {
    for (const bad of ['class="bg-black/10 fixed"', 'class="text-red-500"', 'class="text-foreground flex"', 'class="bg-muted mx-auto"',
      'class="hover:bg-white"', 'class="bg-popover text-popover-foreground"']) {
      expect(bad, bad).toMatch(TAILWIND_COLOUR);
    }
    for (const ok of ['class="bg-primary text-[var(--on-accent)]"', 'class="bg-clip-padding text-sm"',
      'class="border-[var(--border)] shadow-[var(--card-shadow)]"', 'class="bg-muted-x"']) {
      expect(ok, ok).not.toMatch(TAILWIND_COLOUR);
    }
  });

  // Review R1: shadcn components keep their classes in a module script (tailwind-variants), so the script is read too.
  it.each(SOURCES)('%s', (file) => {
    expect(hits(file, 'tailwind colour', codeLines(file), TAILWIND_COLOUR)).toEqual([]);
  });

  it('a class string in a module script is caught', () => {
    const src = '<script lang="ts" module>\n  const v = tv({ variants: { ghost: "hover:bg-muted hover:text-muted-foreground" } });\n</script>';
    expect(scriptBlocks(src)).toMatch(TAILWIND_COLOUR);
  });
});

// PR 5 re-review R3: a mark token can also reach text through a utility class (`text-[var(--oc-success)]`, Tailwind 4's
// `text-(--oc-success)`), the form a view must use to colour a shared Button, since a scoped class cannot reach it.
// Text tokens (`--err-text`, `--on-accent`) are spared, and so is a non-colour utility (`text-[length:…]`).
// Code review M5: `text-destructive` too, shadcn's red fill (--destructive is hsl(4 66% 50%), the --red mark).
const MARK_AS_TEXT_CLASS =
  /(?<![\w-])text-(?:\[(?:color:)?var\(\s*|\()--(?:red|green|yellow|oc-[\w-]+|series-\d+|signal-[\w-]+)(?![\w-])|(?<![\w-])text-destructive(?![\w-])/;

describe('no markup, script or module colours text with a mark token through a utility class', () => {
  it('the pattern catches the bracket, short and typed forms, and spares text tokens, borders and lengths', () => {
    for (const bad of ['class="text-[var(--oc-success)]"', 'class="hover:text-[var(--red)]"', 'class="text-(--oc-success)"',
      'class="text-[color:var(--oc-success)]"', 'class="text-destructive"', 'destructive: "bg-[var(--err-bg)] hover:text-destructive"']) {
      expect(bad, bad).toMatch(MARK_AS_TEXT_CLASS);
    }
    for (const ok of ['class="text-[var(--err-text)]"', 'class="text-(--err-text)"', 'class="text-[var(--on-accent)]"',
      'class="border-[var(--red)]"', 'class="border-(--red)"', 'class="text-[length:var(--series-2)]"',
      'class="text-destructive-foreground"', 'class="border-destructive ring-destructive/20"']) {
      expect(ok, ok).not.toMatch(MARK_AS_TEXT_CLASS);
    }
  });

  it.each(SOURCES)('%s', (file) => {
    expect(codeLines(file).filter((line) => MARK_AS_TEXT_CLASS.test(line)).map((l) => l.trim())).toEqual([]);
  });
});

// Review M1 (PR 3): a status fill used as text can also come from the markup, through a map of mark colours
// (`style:color={URGENCY_COLOR[u]}`, the form Heartbeat had) or a literal fill token. A text colour reads a text map,
// named `*_INK` by convention (Heartbeat's URGENCY_INK), or a text token.
// Only a text colour counts: a `style:color` directive's value, or the `color:` declaration of an inline style attribute
// (not `border-color`, and not a `color:` key in an object literal, as Admission's box-plot marks have).
// PR 4 review M5: a map of mark token names (`*_TOKEN`) and the helpers that build a mark's var() from one (`fill(`,
// `seriesVar(`) are marks too. PR 4 re-review N1: so are the plural maps (`OUTCOME_COLORS`, `SIGNAL_TOKENS`), the series
// and dimension helpers (`seriesToken(`, `dimColor(`, `signalToken(`) and a literal mark token (`--oc-*`, `--series-N`,
// `--signal-*`). Still out of reach of a line pattern, and left to review: a bare constant (`style:color={MARK}`), a `}`
// or `"` earlier in the expression, and a directive split over two lines.
const FILL_MAP_AS_TEXT =
  /(style:color=[{"][^}"]*|style="[^"]*?(?<![-\w])color:[^";]*)(\b[A-Z][A-Z_]*_(?:COLORS?|TOKENS?)\b|\b(?:fill|seriesVar|seriesToken|dimColor|signalToken)\(|var\(\s*--(?:red|green|yellow|oc-[\w-]+|series-\d+|signal-[\w-]+)(?![\w-]))/;

describe('no markup colours text with a mark map or a status fill', () => {
  it('the pattern catches a colour map or a fill as text, and spares ink maps, borders and backgrounds', () => {
    for (const bad of ['<span style:color={URGENCY_COLOR[row.urgency]}>', '<span style="color:{URGENCY_COLOR[row.urgency] ?? \'x\'}">',
      '<b style:color="var(--red)">', '<i style="color: var(--yellow)">',
      // PR 4 review M5: a mark map of token names, and the helpers that build a mark's var() from one.
      '<span style:color={fill(OUTCOME_TOKEN[k])}>', '<span style:color={seriesVar(p.rule)}>', '<b style:color={CONTEXT_TOKEN[k]}>',
      // PR 4 re-review N1: the plural maps the views use, the series and dimension helpers, and a literal mark token.
      // PR 5 re-review R3: var() with a space or a fallback.
      '<b style:color="var( --oc-success )">', '<b style:color="var(--oc-success, green)">',
      '<span style:color={OUTCOME_COLORS[o]}>', '<span style:color={`var(${SIGNAL_TOKENS[t]})`}>', '<b style:color={signalToken(t)}>',
      '<b style:color={dimColor(i)}>', '<b style:color={seriesToken(i)}>', '<b style:color="var(--oc-success)">', '<i style="color: var(--series-2)">']) {
      expect(bad, bad).toMatch(FILL_MAP_AS_TEXT);
    }
    for (const ok of ['<span style:color={URGENCY_INK[row.urgency]}>', '<span style:border-color={STATE_COLOR[st]}>',
      '<span style:background={URGENCY_COLOR[f.urgency]}>', '<span style="border-color: var(--red)">', '<b style:color={dispositionColor(d)}>',
      "{#each [{ label: 'Admitted', stats: entry.admitted, color: 'var(--green)' }] as { label, stats, color }}",
      // A mark's fill set as a custom property is not text.
      '<div class="bar-fill" style:--fill={fill(OUTCOME_TOKEN[b.key])}>', '<span class="rule-pill" style:--fill={seriesVar(p.rule)}>']) {
      expect(ok, ok).not.toMatch(FILL_MAP_AS_TEXT);
    }
  });

  it.each(COMPONENTS)('%s', (file) => {
    const lines = markupOf(read(file)).split('\n');
    expect(lines.filter((line) => FILL_MAP_AS_TEXT.test(line)).map((l) => l.trim())).toEqual([]);
  });
});

describe('the walk and the allowances', () => {
  it('reads every component and module the walk finds (lib/testing/sources.ts)', () => {
    expect(COMPONENTS.length).toBeGreaterThan(50); // 58 since code review M6 deleted 26 unused components
    expect(SOURCES.length).toBeGreaterThan(COMPONENTS.length);
  });

  const PATTERN: Record<Guard, RegExp> = { 'script literal': LITERAL_SCRIPT, 'tailwind colour': TAILWIND_COLOUR };

  it('an allowance removes only its own text: anything else on the line still fails', () => {
    const [fallback, , unmapped] = ALLOWED;
    expect(hits('lib/chartColors.ts', 'script literal', ["  '--accent': '#2346c8',", "  '--x': '#2346c8', c = '#fff',"], LITERAL_SCRIPT))
      .toEqual(["'--x': '#2346c8', c = '#fff',"]);
    expect(fallback.files.test('lib/overviewCharts.ts')).toBe(false);
    expect(hits('lib/ui/drawer/drawer-description.svelte', 'tailwind colour', ['class="text-muted-foreground text-red-500"'], TAILWIND_COLOUR))
      .toEqual(['class="text-muted-foreground text-red-500"']);
    expect(hits('views/Overview.svelte', 'tailwind colour', ['class="bg-popover"'], TAILWIND_COLOUR)).toEqual(['class="bg-popover"']);
    expect(unmapped.files.test('lib/ui/drawer/drawer-content.svelte')).toBe(true);
  });

  // Code review M6: the card, table and dialog primitives and Placeholder were rendered by nothing, and the unmapped-class
  // allowance covered them, so the first view to use one would have shown ink where it meant muted. They are deleted, and
  // the allowances name the drawer, the one generated primitive the dashboard renders (BottomSheet).
  it('the unused primitives are gone, and the allowances name the drawer only', () => {
    for (const f of ['lib/ui/card/index.ts', 'lib/ui/table/index.ts', 'lib/ui/dialog/index.ts', 'lib/ui/Placeholder.svelte']) {
      expect(existsSync(join(SRC, f)), f).toBe(false);
    }
    const [, , unmapped, scrim] = ALLOWED;
    for (const f of ['lib/ui/dialog/dialog-content.svelte', 'lib/ui/card/card-description.svelte', 'lib/ui/table/table-caption.svelte']) {
      expect(unmapped.files.test(f), f).toBe(false);
    }
    expect(scrim.files.test('lib/ui/drawer/drawer-overlay.svelte')).toBe(true);
    for (const f of ['lib/ui/dialog/dialog-overlay.svelte', 'lib/ui/drawer/drawer-content.svelte']) expect(scrim.files.test(f), f).toBe(false);
  });

  // Code review M2: the TOKEN_FALLBACK allowance goes by the shape of a line, and chartColors.test.ts proves the table
  // row by row. A second table of the same shape (`const LEGEND = { '--accent': '#ff0000' }`) would be stripped by the
  // allowance and proved by nothing, so the module may hold exactly as many rows of that shape as the table has keys.
  it('chartColors.ts holds exactly as many fallback-shaped lines as TOKEN_FALLBACK has rows', () => {
    const row = new RegExp(ALLOWED[0].allow.source);
    const rows = (lines: string[]) => lines.filter((l) => row.test(l)).length;
    const n = Object.keys(TOKEN_FALLBACK).length;
    expect(rows(codeLines('lib/chartColors.ts'))).toBe(n);
    expect(rows([...codeLines('lib/chartColors.ts'), 'const LEGEND = {', "  '--accent': '#ff0000',", '};'])).toBe(n + 1);
  });

  // Each allowance still matches something a guard would otherwise fail on, in a file the walk reads (none is stale).
  it.each(ALLOWED.map((a, i) => [i, a.files.source, a.guard] as const))('allowance %i (%s, %s) is still needed', (i) => {
    const a = ALLOWED[i];
    const files = SOURCES.filter((f) => a.files.test(f));
    expect(files.length, 'the allowance names no walked source').toBeGreaterThan(0);
    const needed = files.some((f) => codeLines(f).some((l) => PATTERN[a.guard].test(l) && new RegExp(a.allow.source).test(l)));
    expect(needed, `${a.reason}: matches nothing a guard fails on (stale)`).toBe(true);
  });
});
