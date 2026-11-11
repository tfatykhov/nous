import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, relative } from 'node:path';
import { ROOT_SEL, RAIL_SEL, block, markupOf, parseBlocks, scriptBlocks, styleBlocks } from './lib/testing/css';
import { DAG_NODE_STATUSES } from './lib/status';
import { LANES } from './lib/work';
import { FRAME_IDS } from './lib/frames';
import { TOKEN_FALLBACK } from './lib/chartColors';

// Dashboard redesign PR 7 (Review Focus 2): a token is read as `var(--x)`, as a quoted name (`tint('--x', a)`,
// `readToken(el, '--x')`), and as a family built at runtime (`var(--st-${status})`). Both directions are guarded:
// (a) every token a source reads is declared (at :root, at .rail, in @theme, or set by a component itself), and every
//     member a runtime build can reach is declared;
// (b) every token :root or .rail declares is read somewhere (a family read counts for every member of the family), or
//     sits on a short allowlist with its reason.

const SRC = __dirname;
const CSS = readFileSync(join(SRC, 'app.css'), 'utf-8');
const BLOCKS = parseBlocks(CSS);
const ROOT = block(BLOCKS, ROOT_SEL, 50);
const RAIL = block(BLOCKS, RAIL_SEL, 10);
const THEME = { ...(BLOCKS.get('@theme') ?? {}), ...(BLOCKS.get('@theme inline') ?? {}) };
const names = (b: Record<string, string>) => Object.keys(b).filter((k) => k.startsWith('--'));
const DECLARED = new Set([...names(ROOT), ...names(RAIL), ...names(THEME)]);

/** Every non-test source of the dashboard (not the companion, which has its own stylesheet). */
function sources(dir = SRC): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    const rel = relative(SRC, p).replace(/\\/g, '/');
    if (e.isDirectory()) return rel === 'companion' || rel === 'lib/testing' ? [] : sources(p);
    return /\.(svelte|ts)$/.test(e.name) && !e.name.endsWith('.test.ts') ? [p] : [];
  });
}
/** A source without its comments: a .svelte file's script, markup and style blocks, or a .ts file's code. A comment that
 * names a token (`var(--x)` in a doc string) is not a read. */
function code(file: string, text: string): string {
  if (file.endsWith('.svelte')) return [scriptBlocks(text), markupOf(text), styleBlocks(text)].join('\n');
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|\s)\/\/.*$/gm, '$1');
}
const FILES = sources().map((f) => {
  const rel = relative(SRC, f).replace(/\\/g, '/');
  return [rel, code(rel, readFileSync(f, 'utf-8'))] as const;
});
/** app.css less its comments and its @theme blocks (a mapping there is not a use: see the allowlist). */
const CSS_READS = CSS.replace(/\/\*[\s\S]*?\*\//g, '').replace(/@theme(?:\s+inline)?\s*\{[^}]*\}/g, '');

type Scan = { reads: Set<string>; families: Set<string>; own: Set<string> };

/** The token names a text reads (whole names) and builds (`--prefix-${…}`, a family), and the custom properties it sets
 * itself (`style:--x`, a `--x:` declaration, `setProperty('--x'`). */
function scan(text: string, subtractOwn = true): Scan {
  const reads = new Set<string>();
  const families = new Set<string>();
  const own = new Set<string>();
  const read = (n: string, rest: string) => (rest.startsWith('${') ? families.add(n) : reads.add(n));
  for (const m of text.matchAll(/var\(\s*(--[a-zA-Z0-9-]+)(\$\{)?/g)) read(m[1], m[2] ?? '');
  for (const m of text.matchAll(/['"`](--[a-zA-Z0-9-]+)(\$\{|['"`])/g)) read(m[1], m[2]);
  for (const m of text.matchAll(/tint\(\s*['"]?(--[a-zA-Z0-9-]+)/g)) reads.add(m[1]);
  for (const m of text.matchAll(/style:(--[a-zA-Z0-9-]+)/g)) own.add(m[1]);
  for (const m of text.matchAll(/(?:^|[\s{;"'`(])(--[a-zA-Z0-9-]+)\s*:(?!:)/gm)) own.add(m[1]);
  for (const m of text.matchAll(/setProperty\(\s*['"`](--[a-zA-Z0-9-]+)/g)) own.add(m[1]);
  if (subtractOwn) for (const n of own) reads.delete(n);
  return { reads, families, own };
}

const SCANS = FILES.map(([f, text]) => [f, scan(text)] as const);
const OWN = new Set(SCANS.flatMap(([, s]) => [...s.own]));
const CSS_SCAN = scan(CSS_READS, false); // app.css declares the tokens it reads: none of them is "own"
const READS = new Set([...SCANS.flatMap(([, s]) => [...s.reads]), ...CSS_SCAN.reads]);
const FAMILY_READS = new Set(SCANS.flatMap(([, s]) => [...s.families]));
/** The tokens a scan reads that no stylesheet declares (`scan` has already removed what the file sets itself). */
const undeclared = (s: Scan) => [...s.reads].filter((t) => !DECLARED.has(t));

const dash = (s: string) => s.replaceAll('_', '-');
const graphSrc = FILES.find(([f]) => f === 'views/GraphView.svelte')![1];
const listIn = (src: string, decl: RegExp) => [...(src.match(decl)?.[1] ?? '').matchAll(/'([^']+)'/g)].map((m) => m[1]);

/** Every family a source builds, with the members its build can reach (each from the code's own list). */
const FAMILIES: Record<string, string[]> = {
  '--series-': Array.from({ length: 13 }, (_, i) => String(i + 1)), // seriesToken cycles 1..13; Cache picks among them
  '--f-': [...FRAME_IDS, 'other'],
  '--lane-': [...LANES],
  '--st-': [...DAG_NODE_STATUSES.filter((s) => s !== 'awaiting_input'), 'partial'].map(dash), // awaiting input is --waiting
  '--gv-': [...listIn(graphSrc, /const ALL_TYPES = \[([^\]]*)\]/), 'other'],
  '--rel-': listIn(graphSrc, /const RELATIONS = new Set\(\[([\s\S]*?)\]\)/).map(dash),
};

/** Declared but read by no source, on purpose: the shadcn HSL variables that @theme inline maps to utilities
 * (bg-card, bg-primary, border-border ...). Whether a utility is used is not a token read. */
const MAPPED_SHADCN = ['--background', '--card', '--card-foreground', '--primary', '--primary-foreground', '--secondary',
  '--secondary-foreground', '--destructive', '--border-token', '--input', '--ring'];

/** Declared and read by nothing, kept on purpose: a value the design contract names. */
const CONTRACT_UNREAD: Record<string, string> = {
  '--radius': 'the contract’s corner radius (18 px, the card radius); DESIGN.md derives it from :root',
};

describe('token use, both ways', () => {
  it('the scanner reads var(), quoted names, tint(), families and a component\'s own properties', () => {
    const s = scan(`<i style:--dot={x}></i><script>const a = 'var(--text)'; tint('--series-2', 0.1); readToken(el, '--chart-text');
      const b = \`var(--st-\${s})\`; el.style.setProperty('--set-here', v);</script>
      <style>.x { --local: 1px; color: var(--dot); border: var(--local); } .y { background: var(--surface) }</style>`);
    expect([...s.reads].sort()).toEqual(['--chart-text', '--series-2', '--surface', '--text']);
    expect([...s.families]).toEqual(['--st-']);
    expect([...s.own].sort()).toEqual(['--dot', '--local', '--set-here']);
  });

  it('reads every source: the views, the shared pieces, the libs and app.css (not the companion, not the tests)', () => {
    const files = FILES.map(([f]) => f);
    expect(files.length).toBeGreaterThan(80);
    for (const f of ['App.svelte', 'views/GraphView.svelte', 'lib/chartColors.ts', 'lib/ui/Nav.svelte']) expect(files).toContain(f);
    expect(files.some((f) => f.startsWith('companion/') || f.endsWith('.test.ts'))).toBe(false);
    expect(READS.has('--bg')).toBe(true); // body reads it in app.css
  });

  it('(a) catches a read of a token declared nowhere: the var(--fg) of Retrieval, unnoticed from #596 to PR 7', () => {
    const old = scan('<style>.xsummary strong { color: var(--fg); font-variant-numeric: tabular-nums; }</style>');
    expect(undeclared(old)).toEqual(['--fg']);
  });

  // Code review M3: a component's own properties count for that file only. DayTimeline and Trace set --fc; a new view
  // that reads var(--fc) without setting it inherits whatever an ancestor left, the --fg failure under another name.
  it('(a) catches a read of a property another component sets but this one does not', () => {
    expect(OWN.has('--fc')).toBe(true);
    expect(undeclared(scan('<style>.x { color: var(--fc); }</style>'))).toEqual(['--fc']);
    expect(undeclared(scan('<i style:--fc={c}></i><style>.x { color: var(--fc); }</style>'))).toEqual([]);
  });

  it('(a) every token a source reads is declared (at :root, .rail, @theme) or set by that component itself', () => {
    const missing = SCANS.flatMap(([f, s]) => undeclared(s).map((t) => `${f}: ${t}`));
    missing.push(...[...CSS_SCAN.reads].filter((t) => !DECLARED.has(t)).map((t) => `app.css: ${t}`));
    expect(missing).toEqual([]);
  });

  it('(a) every family a source builds is listed, and every member its build can reach is declared at :root', () => {
    expect([...FAMILY_READS].sort()).toEqual(Object.keys(FAMILIES).sort());
    for (const [prefix, members] of Object.entries(FAMILIES)) {
      expect(members.length, prefix).toBeGreaterThan(2);
      for (const m of members) expect(ROOT[`${prefix}${m}`], `${prefix}${m}`).toBeDefined();
    }
  });

  it('(b) every token :root or .rail declares is read somewhere, or is on an allowlist with its reason', () => {
    // A family member counts as read only if the family's build can reach it (FAMILIES): an unreachable member is dead.
    const reached = (t: string) => Object.entries(FAMILIES).some(([p, ms]) => ms.some((m) => t === `${p}${m}`));
    const credited = (t: string) => READS.has(t) || reached(t) || MAPPED_SHADCN.includes(t) || t in CONTRACT_UNREAD;
    const unread = [...names(ROOT), ...names(RAIL)].filter((t) => !credited(t));
    expect([...new Set(unread)].sort()).toEqual([]);
  });

  // From varCoverage.test.ts (PR 4 review M5, re-review N2), now over every file that draws a Chart, not seven: a canvas
  // cannot read var(), so in jsdom a token a chart file names resolves through TOKEN_FALLBACK, or to '' if it is missing.
  it('a file that draws a Chart: every token its script or markup names is in TOKEN_FALLBACK', () => {
    const chartFiles = FILES.filter(([f, text]) => f.endsWith('.svelte') && /import Chart from/.test(text));
    expect(chartFiles.length).toBeGreaterThan(10);
    const missing = chartFiles.flatMap(([f]) => {
      const raw = readFileSync(join(SRC, f), 'utf-8');
      const js = scriptBlocks(raw) + '\n' + markupOf(raw);
      const named = scan(js, false);
      return [...named.reads].filter((t) => !OWN.has(t) && !(t in TOKEN_FALLBACK)).map((t) => `${f}: ${t}`);
    });
    expect(missing).toEqual([]);
  });

  it('the contract allowlist is live: each entry is declared and still read by nothing', () => {
    for (const t of Object.keys(CONTRACT_UNREAD)) {
      expect(ROOT[t], t).toBeDefined();
      expect(READS.has(t), `${t} is read now: take it off the allowlist`).toBe(false);
    }
  });

  it('the allowlist is live: each mapped shadcn variable is declared at :root and mapped by @theme inline, and read by nothing else', () => {
    const inline = BLOCKS.get('@theme inline') ?? {};
    const mapped = new Set(Object.values(inline).flatMap((v) => [...v.matchAll(/var\((--[\w-]+)\)/g)].map((m) => m[1])));
    for (const t of MAPPED_SHADCN) {
      expect(ROOT[t], `${t} declared`).toBeDefined();
      expect(mapped.has(t), `${t} mapped`).toBe(true);
    }
    expect([...mapped].filter((t) => !MAPPED_SHADCN.includes(t))).toEqual([]);
  });
});
