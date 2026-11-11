import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ROUTES } from './lib/router';
import { ROOT_SEL, block, markupOf, parseBlocks, scriptBlocks } from './lib/testing/css';
import { TOKEN_FALLBACK } from './lib/chartColors';

// Redesign PR 4 and PR 5: a converted screen reads only declared tokens. Every var(--x) in a converted file (style,
// script and markup) must be declared at :root, unless it is a property the file sets itself (`style:--fill=…`). A
// rail-only token (--nav-*) would read nothing on the page.

const SRC = __dirname;
const ROOT = block(parseBlocks(readFileSync(join(SRC, 'app.css'), 'utf-8')), ROOT_SEL, 50);

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

/** A component's code: its script and its markup (the source without its style), where a chart's props are written. */
const codeOf = (src: string) => scriptBlocks(src) + '\n' + markupOf(src);

/** The tokens a source reads (a `var(--x)` anywhere, or a quoted `'--x'` in its script or markup, which is built into
 * var() or tinted later: PR 4 review M5, re-review N2), and the custom properties it sets itself (a `style:--x` directive
 * or a `--x:` declaration). */
function tokensOf(src: string): { read: string[]; own: Set<string> } {
  const named = [...codeOf(src).matchAll(/['"`](--[\w-]+)['"`]/g)].map((m) => m[1]);
  const read = [...new Set([...[...src.matchAll(/var\(\s*(--[\w-]+)/g)].map((m) => m[1]), ...named])];
  const own = new Set([...src.matchAll(/(?:style:(--[\w-]+)|[{;\s"](--[\w-]+)\s*:)/g)].map((m) => m[1] ?? m[2]));
  return { read, own };
}

/** Every token `src` reads that is not declared at :root; in a file that draws a Chart, also every token its script or
 * markup names that TOKEN_FALLBACK lacks (it would resolve to '' in jsdom). */
function uncovered(src: string): string[] {
  const { read, own } = tokensOf(src);
  const missing = read.filter((t) => !own.has(t) && !(t in ROOT));
  if (!/import Chart from/.test(src)) return missing;
  const js = codeOf(src);
  const scripted = [...new Set([...js.matchAll(/var\(\s*(--[\w-]+)/g), ...js.matchAll(/['"`](--[\w-]+)['"`]/g)].map((m) => m[1]))];
  return [...new Set([...missing, ...scripted.filter((t) => !(t in TOKEN_FALLBACK))])];
}

const converted = Object.values(SCREENS).filter((f) => guardList('HEX_GUARDED').includes(f));

describe('a converted dense screen reads only declared tokens', () => {
  it('also reads a token passed by name in the script, and holds a chart file\'s tokens to TOKEN_FALLBACK (review M5)', () => {
    // A name in a string is built into var() later (`var(${token})`) or tinted (tint('--x', a)).
    expect(uncovered("<script>const t = tint('--made-up-token', 0.2);</script>")).toEqual(['--made-up-token']);
    expect(uncovered("<script>const M = { a: '--nav-ink' };</script>")).toEqual(['--nav-ink']);
    expect(uncovered("<script>const M = { a: '--series-2' };</script>")).toEqual([]);
    // A file that draws a Chart: every token its script names must also resolve in jsdom (TOKEN_FALLBACK).
    const chartFile = "<script>import Chart from '../lib/viz/Chart.svelte';\nconst c = 'var(--kind-task)';</script>";
    expect(uncovered(chartFile)).toEqual(['--kind-task']);
    expect(uncovered(chartFile.replace('--kind-task', '--series-2'))).toEqual([]);
    // PR 4 re-review N2: a chart's tokens written inline in the markup (Decisions and Scoring pass some that way).
    const inline = "<script>import Chart from '../lib/viz/Chart.svelte';</script>\n";
    expect(uncovered(inline + "<Chart options={{ scales: { r: { grid: { color: 'var(--kind-task)' } } } }} />")).toEqual(['--kind-task']);
    expect(uncovered(inline + "<Chart data={{ datasets: [{ backgroundColor: tint('--made-up', 0.2) }] }} />")).toEqual(['--made-up']);
    expect(uncovered(inline + "<Chart options={{ scales: { r: { grid: { color: 'var(--border)' } } } }} />")).toEqual([]);
  });

  it('finds a token missing from :root, and spares the ones the file sets itself', () => {
    expect(uncovered('.a { color: var(--made-up-token); }')).toEqual(['--made-up-token']);
    expect(uncovered('.a { color: var(--nav-ink); }')).toEqual(['--nav-ink']); // rail-only: nothing on the page
    expect(uncovered('.a { color: var(--text); width: var(--sidebar-width); }')).toEqual([]);
    expect(uncovered('<i style:--fill="var(--series-2)"></i><style>i { background: var(--fill); }</style>')).toEqual([]);
  });

  it('every converted screen of the seven is checked', () => {
    for (const [route, file] of Object.entries(SCREENS)) {
      if ((ROUTES as readonly string[]).includes(route)) expect(converted, route).toContain(file);
    }
  });

  it.each(converted.length ? converted : ['(none converted yet)'])('%s', (file) => {
    if (!file.endsWith('.svelte')) return;
    expect(uncovered(readFileSync(join(SRC, file), 'utf-8'))).toEqual([]);
  });
});
