/// <reference types="node" />
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, relative } from 'node:path';
import { parseThemes } from './testing/themes';
import { contrast, over } from './testing/color';

// The companion's theme tokens, read from companion.css as the browser resolves them (testing/themes.ts), against
// the tables of origin/main (9e4953f3). The five named themes and the old default look must not move.

const CSS = readFileSync(join(__dirname, 'companion.css'), 'utf-8');
const SHEET = parseThemes(CSS);

// The resolved token table of each theme at 9e4953f3 (generated from that commit's companion.css by
// testing/themes.ts, aliases substituted where the browser substitutes them). 'nous-default' is the old :root.
const ORIGIN_THEMES: Record<string, Record<string, string>> = {
  'nous-default': {
    '--bg': '#0a0a0f',
    '--surface': '#12121a',
    '--surface-hover': '#181824',
    '--border': '#1e1e2e',
    '--accent': '#7c6af7',
    '--accent-dim': '#4f46a8',
    '--accent-glow': 'rgba(124, 106, 247, 0.15)',
    '--text': '#e2e2f0',
    '--muted': '#6b6b8a',
    '--green': '#34d399',
    '--red': '#f87171',
    '--yellow': '#fbbf24',
    '--radius': '12px',
    '--radius-sm': '8px',
    '--transition': '0.2s ease',
    '--font-ui': "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
    '--font-mono': "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
    '--ok': '#34d399',
    '--warn': '#fbbf24',
    '--crit': '#f87171',
    '--locked': '#5fb3a1',
    '--soft': '#8a8fa3',
    '--font-display': "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
    '--font-numeric': "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
    '--on-accent': '#ffffff',
    '--scrim': 'rgba(10, 10, 15, 0.72)',
    '--node-decision': '#a78bfa',
    '--series-1': '#7c6af7',
    '--series-2': '#34d399',
    '--series-3': '#fbbf24',
    '--series-4': '#60a5fa',
    '--chart-axis': '#6b6b8a',
    '--chart-grid': '#1e1e2e',
  },
  'alpine-dusk': {
    '--bg': '#0e1014',
    '--surface': '#161a20',
    '--surface-hover': '#1e232b',
    '--border': '#232a33',
    '--accent': '#e8833a',
    '--accent-dim': '#a85a25',
    '--accent-glow': 'rgba(232, 131, 58, 0.15)',
    '--text': '#ece6dd',
    '--muted': '#8a8072',
    '--green': '#34d399',
    '--red': '#f87171',
    '--yellow': '#fbbf24',
    '--radius': '12px',
    '--radius-sm': '8px',
    '--transition': '0.2s ease',
    '--font-ui': "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
    '--font-mono': "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
    '--ok': '#5fb3a1',
    '--warn': '#e8a33a',
    '--crit': '#d9553f',
    '--locked': '#5fb3a1',
    '--soft': '#8a8072',
    '--font-display': "Fraunces, 'Georgia', 'Times New Roman', serif",
    '--font-numeric': "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
    '--on-accent': '#1a1206',
    '--scrim': 'rgba(14, 16, 20, 0.74)',
    '--node-decision': '#c79bde',
    '--series-1': '#e8833a',
    '--series-2': '#5fb3a1',
    '--series-3': '#e8a33a',
    '--series-4': '#8fb8d9',
    '--chart-axis': '#8a8072',
    '--chart-grid': '#232a33',
  },
  'harbor': {
    '--bg': '#0b1016',
    '--surface': '#121a24',
    '--surface-hover': '#182230',
    '--border': '#1f2c3a',
    '--accent': '#4a90d9',
    '--accent-dim': '#2f5f92',
    '--accent-glow': 'rgba(74, 144, 217, 0.15)',
    '--text': '#dbe6f0',
    '--muted': '#7089a0',
    '--green': '#34d399',
    '--red': '#f87171',
    '--yellow': '#fbbf24',
    '--radius': '12px',
    '--radius-sm': '8px',
    '--transition': '0.2s ease',
    '--font-ui': "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
    '--font-mono': "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
    '--ok': '#3fb6a0',
    '--warn': '#e0a94a',
    '--crit': '#e0685f',
    '--locked': '#4a90d9',
    '--soft': '#7089a0',
    '--font-display': "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
    '--font-numeric': "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
    '--on-accent': '#06101a',
    '--scrim': 'rgba(11, 16, 22, 0.74)',
    '--node-decision': '#8fb0e0',
    '--series-1': '#4a90d9',
    '--series-2': '#3fb6a0',
    '--series-3': '#e0a94a',
    '--series-4': '#a98fd9',
    '--chart-axis': '#7089a0',
    '--chart-grid': '#1f2c3a',
  },
  'paper': {
    '--bg': '#f4f1ea',
    '--surface': '#fbf9f4',
    '--surface-hover': '#f0ece1',
    '--border': '#ddd6c8',
    '--accent': '#b5651d',
    '--accent-dim': '#8a4d16',
    '--accent-glow': 'rgba(181, 101, 29, 0.12)',
    '--text': '#2a2622',
    '--muted': '#6e675b',
    '--green': '#34d399',
    '--red': '#f87171',
    '--yellow': '#fbbf24',
    '--radius': '12px',
    '--radius-sm': '8px',
    '--transition': '0.2s ease',
    '--font-ui': "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
    '--font-mono': "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
    '--ok': '#2f8f6b',
    '--warn': '#b5811d',
    '--crit': '#c0432f',
    '--locked': '#2f8f6b',
    '--soft': '#6e675b',
    '--font-display': "Fraunces, 'Georgia', 'Times New Roman', serif",
    '--font-numeric': "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
    '--on-accent': '#fbf9f4',
    '--scrim': 'rgba(42, 38, 34, 0.42)',
    '--node-decision': '#7a5aa8',
    '--series-1': '#b5651d',
    '--series-2': '#2f8f6b',
    '--series-3': '#b5811d',
    '--series-4': '#3a6ea8',
    '--chart-axis': '#6e675b',
    '--chart-grid': '#ddd6c8',
  },
  'signal': {
    '--bg': '#08080a',
    '--surface': '#101014',
    '--surface-hover': '#17171d',
    '--border': '#24242e',
    '--accent': '#00d9a3',
    '--accent-dim': '#009873',
    '--accent-glow': 'rgba(0, 217, 163, 0.16)',
    '--text': '#f4f4fa',
    '--muted': '#7a7a92',
    '--green': '#34d399',
    '--red': '#f87171',
    '--yellow': '#fbbf24',
    '--radius': '12px',
    '--radius-sm': '8px',
    '--transition': '0.2s ease',
    '--font-ui': "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
    '--font-mono': "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
    '--ok': '#00d9a3',
    '--warn': '#ffcc33',
    '--crit': '#ff5c5c',
    '--locked': '#00d9a3',
    '--soft': '#7a7a92',
    '--font-display': "'JetBrains Mono', 'Cascadia Code', monospace",
    '--font-numeric': "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
    '--on-accent': '#04120d',
    '--scrim': 'rgba(8, 8, 10, 0.8)',
    '--node-decision': '#b18cff',
    '--series-1': '#00d9a3',
    '--series-2': '#60a5fa',
    '--series-3': '#ffcc33',
    '--series-4': '#ff5c5c',
    '--chart-axis': '#7a7a92',
    '--chart-grid': '#24242e',
  },
  'report': {
    '--bg': '#0a0c11',
    '--surface': '#131822',
    '--surface-hover': '#182030',
    '--border': '#212b3b',
    '--accent': '#7c9cd0',
    '--accent-dim': '#4f6fa0',
    '--accent-glow': 'rgba(124, 156, 208, 0.15)',
    '--text': '#e9edf5',
    '--muted': '#8d99ae',
    '--green': '#34d399',
    '--red': '#f87171',
    '--yellow': '#fbbf24',
    '--radius': '12px',
    '--radius-sm': '8px',
    '--transition': '0.2s ease',
    '--font-ui': "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
    '--font-mono': "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
    '--ok': '#4ade80',
    '--warn': '#fbbf24',
    '--crit': '#fb7185',
    '--locked': '#94a3b8',
    '--soft': '#94a3b8',
    '--font-display': "'Iowan Old Style', 'Palatino Linotype', Palatino, Georgia, serif",
    '--font-numeric': "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
    '--on-accent': '#0a0c11',
    '--scrim': 'rgba(10, 12, 17, 0.74)',
    '--node-decision': '#c4b5fd',
    '--series-1': '#f0abfc',
    '--series-2': '#4ade80',
    '--series-3': '#fbbf24',
    '--series-4': '#60a5fa',
    '--chart-axis': '#8d99ae',
    '--chart-grid': '#212b3b',
  },
};

/** Pairs below their bar on origin/main, with the ratio measured there: recorded, not fixed (this PR does not change
 * the five themes' colours). A recorded pair may not get worse, and one that now passes must be removed. */
const KNOWN_BELOW: Record<string, number> = {
  'nous-default|--muted|--bg': 3.85,
  'nous-default|--muted|--surface': 3.63,
  'nous-default|--muted|--surface-hover': 3.42,
  'nous-default|--on-accent|--accent': 3.99,
  'nous-default|--accent|--accent-glow': 3.96,
  'nous-default|--chart-axis|--surface': 3.63,
  'alpine-dusk|--muted|--surface': 4.49,
  'alpine-dusk|--soft|--surface': 4.49,
  'alpine-dusk|--muted|--surface-hover': 4.06,
  'alpine-dusk|--soft|--surface-hover': 4.06,
  'alpine-dusk|--crit|--surface': 4.42,
  'alpine-dusk|--crit|--surface-hover': 3.99,
  'alpine-dusk|--chart-axis|--surface': 4.49,
  'harbor|--muted|--surface-hover': 4.4,
  'harbor|--soft|--surface-hover': 4.4,
  'harbor|--accent|--accent-glow': 4.25,
  'paper|--ok|--surface': 3.79,
  'paper|--warn|--surface': 3.25,
  'paper|--locked|--surface': 3.79,
  'paper|--ok|--surface-hover': 3.38,
  'paper|--warn|--surface-hover': 2.9,
  'paper|--crit|--surface-hover': 4.35,
  'paper|--locked|--surface-hover': 3.38,
  'paper|--on-accent|--accent': 4.12,
  'paper|--accent|--accent-glow': 3.56,
  'signal|--muted|--surface-hover': 4.27,
  'signal|--soft|--surface-hover': 4.27,
};

const THEMES = ['nous-default', 'alpine-dusk', 'harbor', 'paper', 'signal', 'report'];

/** A token value as an opaque colour: a translucent one (the selected-row glow) painted over the theme's surface. */
function opaque(t: Record<string, string>, token: string): string {
  const v = t[token];
  return /^rgba|^#[0-9a-f]{8}$/i.test(v) ? over(v, t['--surface']) : v;
}

/** The contrast matrix: [foreground, background, bar]. Text 4.5, marks 3. */
function matrix(t: Record<string, string>): [string, string, number][] {
  const pairs: [string, string, number][] = [];
  for (const bg of ['--bg', '--surface', '--surface-hover']) {
    for (const fg of ['--text', '--muted', '--soft']) pairs.push([fg, bg, 4.5]);
  }
  for (const bg of ['--surface', '--surface-hover']) {
    for (const fg of ['--ok', '--warn', '--crit', '--locked']) pairs.push([fg, bg, 4.5]); // tone text: tile values, pills
  }
  pairs.push(['--on-accent', '--accent', 4.5], ['--accent', '--accent-glow', 4.5], ['--chart-axis', '--surface', 4.5]);
  for (const k of Object.keys(t)) if (/^--(series-\d+|node-[\w-]+)$/.test(k)) pairs.push([k, '--surface', 3]);
  return pairs;
}

describe('the theme reader follows the cascade the browser does', () => {
  it('substitutes a :root alias at :root, and a block alias at the themed element', () => {
    const s = parseThemes(`
      @import url('https://x/css2?family=A:wght@400;500&display=swap');
      :root { --a: 1px; --b: var(--a); --c: var(--a); }
      [data-theme='x'], [data-theme='y'] { --c: var(--a); --g: 9; }
      [data-theme='x'] { --a: 2px; }
      [data-theme='y'] { --g: 10; }
      [data-theme] { color: var(--a); }
    `);
    expect(s.resolved('root-only')).toEqual({ '--a': '1px', '--b': '1px', '--c': '1px' });
    // --b was substituted at :root, so x inherits 1px; --c is re-declared on x, so it reads x's own --a.
    expect(s.resolved('x')).toEqual({ '--a': '2px', '--b': '1px', '--c': '2px', '--g': '9' });
    // A block after its group wins (same specificity, later in source).
    expect(s.resolved('y')['--g']).toBe('10');
    expect([...s.groups.keys()]).toEqual(["[data-theme='x'], [data-theme='y']"]);
    expect(s.blocks.get('x')).toEqual({ '--a': '2px' });
    expect(s.declared('x').color).toBe('var(--a)');
  });

  it('reads the six themes of companion.css', () => {
    expect(SHEET.root['--bg']).toBeDefined();
    for (const id of THEMES.slice(1)) expect(SHEET.blocks.has(id), id).toBe(true);
  });
});

describe('the five named themes and the default render exactly as on origin/main', () => {
  it.each(THEMES)('%s resolves to its origin table', (id) => {
    expect(SHEET.resolved(id)).toEqual(ORIGIN_THEMES[id]);
  });
});

describe('every token a companion component reads exists', () => {
  const COMPANION = __dirname;
  function sources(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
      const p = join(dir, e.name);
      if (e.isDirectory()) return e.name === 'testing' || e.name === '__fixtures__' ? [] : sources(p);
      return /\.(svelte|css)$/.test(e.name) || (/\.ts$/.test(e.name) && !e.name.endsWith('.test.ts')) ? [p] : [];
    });
  }

  it('every var(--x) without a fallback is defined in :root or declared in its own file (style:--x)', () => {
    const missing: string[] = [];
    for (const file of sources(COMPANION)) {
      const src = readFileSync(file, 'utf-8');
      const local = new Set([...src.matchAll(/(?:style:)?(--[\w-]+)\s*[:=]/g)].map((m) => m[1]));
      for (const m of src.matchAll(/var\((--[\w-]+)\)/g)) {
        if (!(m[1] in SHEET.root) && !local.has(m[1])) missing.push(`${relative(COMPANION, file)}: ${m[1]}`);
      }
    }
    expect(missing).toEqual([]);
  });
});

describe('contrast per theme (WCAG: text 4.5, marks 3)', () => {
  for (const id of THEMES) {
    it(`${id}: every pair meets its bar, or is a recorded origin/main shortfall that got no worse`, () => {
      const t = SHEET.resolved(id);
      const fails: string[] = [];
      for (const [fg, bg, bar] of matrix(t)) {
        if (!(fg in t) || !(bg in t)) continue;
        const key = `${id}|${fg}|${bg}`;
        const r = contrast(opaque(t, fg), opaque(t, bg));
        if (key in KNOWN_BELOW) {
          if (r >= bar) fails.push(`${key} now passes (${r.toFixed(2)}): remove it from KNOWN_BELOW`);
          else if (r < KNOWN_BELOW[key]) fails.push(`${key} got worse: ${r.toFixed(2)} < ${KNOWN_BELOW[key]}`);
        } else if (r < bar) fails.push(`${key} ${r.toFixed(2)} < ${bar}`);
      }
      expect(fails).toEqual([]);
    });
  }

  it('every recorded shortfall names a pair the matrix measures', () => {
    const measured = new Set(THEMES.flatMap((id) => matrix(SHEET.resolved(id)).map(([fg, bg]) => `${id}|${fg}|${bg}`)));
    expect(Object.keys(KNOWN_BELOW).filter((k) => !measured.has(k))).toEqual([]);
  });
});
