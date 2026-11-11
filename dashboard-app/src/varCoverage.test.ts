import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { PAPER_ROUTES } from './lib/look';
import { DARK_SEL, PAPER_SEL, parseBlocks, scriptBlocks } from './lib/testing/css';
import { DARK_FALLBACK } from './lib/chartColors';

// Redesign PR 4 and PR 5: a converted screen reads only tokens with a value in both looks. A token defined in the dark
// block alone would carry its dark value onto the paper ground (as the legacy type colours did before PR 2 gave them
// paper values). Every var(--x) in a converted file (style, script and markup) must be in the dark block, and on the
// paper ground unless it is structural (the same in both looks) or a property the file sets itself (`style:--fill=…`).

const SRC = __dirname;
const BLOCKS = parseBlocks(readFileSync(join(SRC, 'app.css'), 'utf-8'));
const DARK = BLOCKS.get(DARK_SEL) ?? {};
const PAPER = BLOCKS.get(PAPER_SEL) ?? {};

/** Defined once, the same under both looks. */
const STRUCTURAL = new Set(['--sidebar-width', '--sidebar-collapsed']);

const SCREENS: Record<string, string> = {
  decisions: 'views/Decisions.svelte',
  rubric: 'views/Rubric.svelte',
  autonomy: 'views/Autonomy.svelte',
  intentions: 'views/Intentions.svelte',
  harness: 'views/Harness.svelte',
  observability: 'views/Observability.svelte',
  identity: 'views/Identity.svelte',
};

function guardList(name: string): string[] {
  const src = readFileSync(join(SRC, 'hexGuard.test.ts'), 'utf-8');
  const body = src.match(new RegExp(`export const ${name}[^=]*=\\s*\\[([\\s\\S]*?)\\];`))?.[1] ?? '';
  return [...body.matchAll(/'([^']+)'/g)].map((m) => m[1]);
}

/** The tokens a source reads (a `var(--x)` anywhere, or a quoted `'--x'` in its script, which is built into var() or
 * tinted later: review M5), and the custom properties it sets itself (a `style:--x` directive or a `--x:` declaration). */
function tokensOf(src: string): { read: string[]; own: Set<string> } {
  const named = [...scriptBlocks(src).matchAll(/['"`](--[\w-]+)['"`]/g)].map((m) => m[1]);
  const read = [...new Set([...[...src.matchAll(/var\(\s*(--[\w-]+)/g)].map((m) => m[1]), ...named])];
  const own = new Set([...src.matchAll(/(?:style:(--[\w-]+)|[{;\s"](--[\w-]+)\s*:)/g)].map((m) => m[1] ?? m[2]));
  return { read, own };
}

/** Every token `src` reads that would carry its dark value onto paper (or is not defined at all); in a file that draws a
 * Chart, also every token its script names that DARK_FALLBACK lacks (it would resolve to '' in jsdom). */
function uncovered(src: string): string[] {
  const { read, own } = tokensOf(src);
  const missing = read.filter((t) => !own.has(t) && !(t in DARK && (t in PAPER || STRUCTURAL.has(t))));
  if (!/import Chart from/.test(src)) return missing;
  const js = scriptBlocks(src);
  const scripted = [...new Set([...js.matchAll(/var\(\s*(--[\w-]+)/g), ...js.matchAll(/['"`](--[\w-]+)['"`]/g)].map((m) => m[1]))];
  return [...new Set([...missing, ...scripted.filter((t) => !(t in DARK_FALLBACK))])];
}

const converted = Object.values(SCREENS).filter((f) => guardList('HEX_GUARDED').includes(f));

describe('a converted dense screen reads only tokens with a paper value', () => {
  it('also reads a token passed by name in the script, and holds a chart file\'s tokens to DARK_FALLBACK (review M5)', () => {
    // A name in a string is built into var() later (`var(${token})`) or tinted (tint('--x', a)).
    expect(uncovered("<script>const t = tint('--made-up-token', 0.2);</script>")).toEqual(['--made-up-token']);
    expect(uncovered("<script>const M = { a: '--nav-ink' };</script>")).toEqual(['--nav-ink']);
    expect(uncovered("<script>const M = { a: '--series-2' };</script>")).toEqual([]);
    // A file that draws a Chart: every token its script names must also resolve in jsdom (DARK_FALLBACK).
    const chartFile = "<script>import Chart from '../lib/viz/Chart.svelte';\nconst c = 'var(--kind-task)';</script>";
    expect(uncovered(chartFile)).toEqual(['--kind-task']);
    expect(uncovered(chartFile.replace('--kind-task', '--series-2'))).toEqual([]);
  });

  it('finds a token missing from either look, and spares the ones the file sets itself', () => {
    expect(uncovered('.a { color: var(--made-up-token); }')).toEqual(['--made-up-token']);
    expect(uncovered('.a { color: var(--nav-ink); }')).toEqual(['--nav-ink']); // rail-only: dark on a paper page
    expect(uncovered('.a { color: var(--text); width: var(--sidebar-width); }')).toEqual([]);
    expect(uncovered('<i style:--fill="var(--series-2)"></i><style>i { background: var(--fill); }</style>')).toEqual([]);
  });

  it('every converted screen of the seven is checked (a screen joins when its route turns paper)', () => {
    for (const [route, file] of Object.entries(SCREENS)) {
      if ((PAPER_ROUTES as readonly string[]).includes(route)) expect(converted, route).toContain(file);
    }
  });

  it.each(converted.length ? converted : ['(none converted yet)'])('%s', (file) => {
    if (!file.endsWith('.svelte')) return;
    expect(uncovered(readFileSync(join(SRC, file), 'utf-8'))).toEqual([]);
  });
});
