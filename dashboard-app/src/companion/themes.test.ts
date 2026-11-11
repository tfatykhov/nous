/// <reference types="node" />
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, relative } from 'node:path';
import { parseThemes } from './testing/themes';
import { contrast, hexLuminance, mix, over } from './testing/color';

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
  'nous-dark|--muted|--bg': 3.85,
  'nous-dark|--muted|--surface': 3.63,
  'nous-dark|--muted|--surface-hover': 3.42,
  'nous-dark|--on-accent|--accent': 3.99,
  'nous-dark|--accent|--accent-glow': 3.96,
  'nous-dark|--chart-axis|--surface': 3.63,
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

const THEMES = ['nous-default', 'nous-dark', 'alpine-dusk', 'harbor', 'paper', 'signal', 'report'];

/** Tone pills below 4.5 on origin/main (the legacy themes' own palettes, recorded, not fixed; a legacy theme's
 * nested tile is its --surface, so both grounds are listed). */
const PILL_BELOW: Record<string, number> = {
  'alpine-dusk|--soft|13%|--nested-bg': 3.85,
  'alpine-dusk|--soft|13%|--surface': 3.85,
  'alpine-dusk|--soft|14%|--nested-bg': 3.8,
  'alpine-dusk|--soft|14%|--surface': 3.8,
  'alpine-dusk|--crit|13%|--nested-bg': 3.86,
  'alpine-dusk|--crit|13%|--surface': 3.86,
  'alpine-dusk|--crit|14%|--nested-bg': 3.84,
  'alpine-dusk|--crit|14%|--surface': 3.84,
  'harbor|--soft|13%|--nested-bg': 4.1,
  'harbor|--soft|13%|--surface': 4.1,
  'harbor|--soft|14%|--nested-bg': 4,
  'harbor|--soft|14%|--surface': 4,
  'harbor|--crit|14%|--nested-bg': 4.44,
  'harbor|--crit|14%|--surface': 4.44,
  'paper|--soft|13%|--nested-bg': 4.49,
  'paper|--soft|13%|--surface': 4.49,
  'paper|--soft|14%|--nested-bg': 4.44,
  'paper|--soft|14%|--surface': 4.44,
  'paper|--ok|13%|--nested-bg': 3.25,
  'paper|--ok|13%|--surface': 3.25,
  'paper|--ok|14%|--nested-bg': 3.22,
  'paper|--ok|14%|--surface': 3.22,
  'paper|--warn|13%|--nested-bg': 2.84,
  'paper|--warn|13%|--surface': 2.84,
  'paper|--warn|14%|--nested-bg': 2.81,
  'paper|--warn|14%|--surface': 2.81,
  'paper|--crit|13%|--nested-bg': 4.06,
  'paper|--crit|13%|--surface': 4.06,
  'paper|--crit|14%|--nested-bg': 4.03,
  'paper|--crit|14%|--surface': 4.03,
  'signal|--soft|13%|--nested-bg': 3.96,
  'signal|--soft|13%|--surface': 3.96,
  'signal|--soft|14%|--nested-bg': 3.91,
  'signal|--soft|14%|--surface': 3.91,
};
/** The old default under its new name, and the five named themes: the looks that must not move. */
const LEGACY = ['nous-dark', 'alpine-dusk', 'harbor', 'paper', 'signal', 'report'];
/** Each legacy look's origin table: nous-dark is the old :root. */
const ORIGIN_OF = (id: string) => ORIGIN_THEMES[id === 'nous-dark' ? 'nous-default' : id];

/** Tokens added by this PR. The structural ones keep the old shape in a legacy theme; the colour ones are what a
 * legacy theme's components painted before (MemoryGraph: fact = accent, episode = ok, procedure = warn, chunk = muted)
 * and, for the frames, the dashboard's dark-legible values (the paper values on `paper`). */
const DARK_FRAMES = {
  '--f-conversation': '#8fa3b5',
  '--f-question': '#8aa4ff',
  '--f-task': '#3fc3d1',
  '--f-decision': '#c08be0',
  '--f-debug': '#f09a5a',
  '--f-creative': '#ee82bd',
  '--f-initiation': '#e0b050',
};
const PAPER_FRAMES = {
  '--f-conversation': '#55697a',
  '--f-question': '#2346c8',
  '--f-task': '#0b6e7a',
  '--f-decision': '#7b3fa0',
  '--f-debug': '#b4531c',
  '--f-creative': '#a3266f',
  '--f-initiation': '#8f5c0f',
};
const NEW_COLOUR = ['--node-fact', '--node-episode', '--node-procedure', '--node-chunk', ...Object.keys(DARK_FRAMES)];
function newLegacy(id: string): Record<string, string> {
  const o = ORIGIN_OF(id);
  return {
    '--radius-md': '8px',
    '--ctl-radius': '8px',
    '--card-shadow': '0 0 #0000',
    '--card-border': `1px solid ${o['--border']}`,
    '--select-border': o['--accent'],
    '--node-fact': o['--accent'],
    '--node-episode': o['--ok'],
    '--node-procedure': o['--warn'],
    '--node-chunk': o['--muted'],
    ...(id === 'paper' ? PAPER_FRAMES : DARK_FRAMES),
    // component shape switches (T5): the old look's values
    '--nested-bg': o['--surface'],
    '--ctl-bg': o['--surface-hover'],
    '--ctl-border': o['--border'],
    '--field-bg': o['--bg'],
    '--field-border': o['--border'],
    '--option-pad': '0',
    '--option-border': '0 solid transparent',
    '--option-selected-bg': 'transparent',
    '--picker-pad': '0.6rem 0.75rem 0.75rem',
    '--label-transform': 'uppercase',
    '--label-tracking': '0.05em',
    '--tablist-bg': 'transparent',
    '--tablist-rule': `1px solid ${o['--border']}`,
    '--tablist-pad': '0',
    '--tablist-radius': '0',
    '--tab-active-bg': 'transparent',
    '--tab-active-shadow': '0 0 #0000',
    '--tab-underline': o['--accent'],
    '--tile-rule-width': '3px',
    '--badge-tint': '0%',
    '--badge-edge': '100%',
    '--tl-dot': '0px',
    '--tl-dot-ring': '0 solid transparent',
    '--meter-track': o['--surface-hover'],
  };
}
const JETBRAINS = "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace";
/** :root-only chrome tokens (the shell's bar): no theme needs them. */
const CHROME_VALUES = { '--rail': '#12304f', '--rail-ink': '#dbe6ef', '--rail-mute': '#9fb6c9', '--rail-focus': '#ffffff' };
const CHROME = Object.keys(CHROME_VALUES);

/** The companion's source files (components, scripts, the stylesheet), without tests and test helpers. */
function sourcesOf(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return e.name === 'testing' || e.name === '__fixtures__' ? [] : sourcesOf(p);
    return /\.(svelte|css)$/.test(e.name) || (/\.ts$/.test(e.name) && !e.name.endsWith('.test.ts')) ? [p] : [];
  });
}

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

  it('reads the seven themes of companion.css', () => {
    expect(SHEET.root['--bg']).toBeDefined();
    for (const id of LEGACY) expect(SHEET.blocks.has(id), id).toBe(true);
    expect(SHEET.blocks.has('nous-default')).toBe(false); // the default is :root
  });
});

describe('the old default (now nous-dark) and the five named themes render exactly as on origin/main', () => {
  it.each(LEGACY)('%s resolves to its origin table plus the new tokens, nothing else', (id) => {
    // (plus the chrome tokens it inherits from :root and never reads)
    expect(SHEET.resolved(id)).toEqual({ ...ORIGIN_OF(id), ...newLegacy(id), ...CHROME_VALUES });
  });

});

/** The default (`nous-default`: no data-theme, or data-theme="nous-default") is the paper world of the dashboard
 * redesign. The values it shares with the dashboard are pinned again in companionParity.test.ts. */
const PAPER_ROOT: Record<string, string> = {
  '--bg': '#eef2f3',
  '--surface': '#ffffff',
  '--surface-hover': '#f4f7f8',
  '--border': '#e3e9ec',
  '--accent': '#12304f',
  '--accent-dim': '#1a3d62',
  '--accent-glow': '#f3f6fa',
  '--text': '#14202b',
  '--muted': '#56687a',
  '--green': '#2f8a4e',
  '--red': '#d2352b',
  '--yellow': '#b7791f',
  '--radius': '18px',
  '--radius-md': '12px',
  '--radius-sm': '8px',
  '--ctl-radius': '999px',
  '--card-shadow': '0 1px 2px rgba(18, 48, 79, 0.06), 0 6px 20px rgba(18, 48, 79, 0.07)',
  '--card-border': '1px solid transparent',
  '--select-border': '#12304f',
  '--transition': '0.2s cubic-bezier(0.16, 1, 0.3, 1)',
  '--font-ui': "'Archivo', system-ui, sans-serif",
  '--font-mono': "'Red Hat Mono', ui-monospace, monospace",
  '--font-display': "'Archivo', system-ui, sans-serif",
  '--font-numeric': "'Red Hat Mono', ui-monospace, monospace",
  '--ok': '#23703d',
  '--warn': '#8a5a0e',
  '--crit': '#b02a22',
  '--locked': '#0b6e7a',
  '--soft': '#4f5f6f',
  '--on-accent': '#ffffff',
  '--scrim': 'rgba(20, 32, 43, 0.45)',
  '--node-decision': '#802c66',
  '--node-fact': '#159a7a',
  '--node-episode': '#1f5fa8',
  '--node-procedure': '#d0620d',
  '--node-chunk': '#8b5cf6',
  '--series-1': '#274455',
  '--series-2': '#1f5fa8',
  '--series-3': '#159a7a',
  '--series-4': '#a8408f',
  '--chart-axis': '#56687a',
  '--chart-grid': '#e3e9ec',
  ...PAPER_FRAMES,
  ...CHROME_VALUES,
  '--nested-bg': '#f4f7f8',
  '--ctl-bg': '#ffffff',
  '--ctl-border': '#c9d4da',
  '--field-bg': '#ffffff',
  '--field-border': '#7a8896',
  '--option-pad': '0.7rem 0.8rem',
  '--option-border': '1.5px solid #e3e9ec',
  '--option-selected-bg': '#f3f6fa',
  '--picker-pad': '0',
  '--label-transform': 'none',
  '--label-tracking': '0',
  '--tablist-bg': '#f4f7f8',
  '--tablist-rule': '0 solid transparent',
  '--tablist-pad': '3px',
  '--tablist-radius': '999px',
  '--tab-active-bg': '#ffffff',
  '--tab-active-shadow': '0 1px 3px rgba(18, 48, 79, 0.12)',
  '--tab-underline': 'transparent',
  '--tile-rule-width': '0',
  '--badge-tint': '13%',
  '--badge-edge': '0%',
  '--tl-dot': '10px',
  '--tl-dot-ring': '2px solid #12304f',
  '--meter-track': '#e3e9ec',
};

describe('the default is the paper world', () => {
  it('nous-default resolves to the paper table, nothing else', () => {
    expect(SHEET.resolved('nous-default')).toEqual(PAPER_ROOT);
  });

  it('figures are Red Hat Mono in the default and JetBrains Mono in nous-dark', () => {
    expect(SHEET.resolved('nous-default')['--font-numeric']).toBe("'Red Hat Mono', ui-monospace, monospace");
    expect(SHEET.resolved('nous-dark')['--font-numeric']).toBe(JETBRAINS);
  });

  it('the page is light: html and :root say so, body sets tabular figures, and a legacy theme turns them off', () => {
    expect(SHEET.rules.get('html')?.['color-scheme']).toBe('light');
    expect(SHEET.root['color-scheme']).toBe('light');
    expect(SHEET.rules.get('body')?.['font-variant-numeric']).toBe('tabular-nums');
    for (const id of LEGACY) expect(SHEET.declared(id)['font-variant-numeric'], id).toBe('normal');
  });

  it('loads Archivo and Red Hat Mono beside the legacy faces', () => {
    const url = /@import url\('([^']+)'\)/.exec(CSS)?.[1] ?? '';
    for (const family of ['DM+Sans', 'JetBrains+Mono', 'Archivo:wdth,wght@62..125,100..900', 'Red+Hat+Mono:wght@400;500']) {
      expect(url, family).toContain(`family=${family}`);
    }
  });
});

describe('no theme inherits a token from :root by accident', () => {
  it.each(LEGACY)('%s declares every :root token itself (its own block or a group naming it)', (id) => {
    const own = SHEET.declared(id);
    expect(Object.keys(SHEET.root).filter((k) => k.startsWith('--') && !(k in own) && !CHROME.includes(k))).toEqual([]);
  });

  it('the chrome tokens are read only by the shell, outside every surface', () => {
    // The bar and its pill: Companion.svelte's header, which is never under a [data-theme].
    const users = CHROME.flatMap((k) =>
      sourcesOf(__dirname).filter((f) => readFileSync(f, 'utf-8').includes(`var(${k})`)).map((f) => relative(__dirname, f)),
    );
    expect([...new Set(users)]).toEqual(['Companion.svelte']);
  });

  it.each(LEGACY)('%s declares each new colour token in its OWN block, as a literal hex', (id) => {
    const block = SHEET.blocks.get(id) ?? {};
    for (const k of NEW_COLOUR) expect(block[k], `${id} ${k}`).toMatch(/^#[0-9a-f]{6}$/);
  });

  it.each(THEMES)('%s: --font-numeric is its own --font-mono', (id) => {
    const t = SHEET.resolved(id);
    expect(t['--font-numeric']).toBe(t['--font-mono']);
  });

  it('nous-dark and the five named themes keep JetBrains Mono for figures', () => {
    for (const id of LEGACY) expect(SHEET.resolved(id)['--font-numeric'], id).toBe(JETBRAINS);
  });
});

describe('color-scheme follows the ground', () => {
  it.each(THEMES)('%s declares a color-scheme that matches the darkness of its --bg', (id) => {
    const scheme = id === 'nous-default' ? SHEET.root['color-scheme'] ?? SHEET.rules.get('html')?.['color-scheme'] : SHEET.declared(id)['color-scheme'];
    const dark = hexLuminance(SHEET.resolved(id)['--bg']) < 0.18;
    expect(scheme).toBe(dark ? 'dark' : 'light');
  });
});

describe('every token a companion component reads exists', () => {
  const COMPANION = __dirname;
  const sources = sourcesOf;

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

  it('the bar: the brand on navy, and each connection state word on its light pill, at least 4.5; the focus ring 3', () => {
    const r = SHEET.resolved('nous-default');
    expect(contrast(r['--rail-ink'], r['--rail'])).toBeGreaterThanOrEqual(4.5); // "Nous"
    expect(contrast(r['--rail-mute'], r['--rail'])).toBeGreaterThanOrEqual(4.5); // "Companion"
    for (const word of ['--rail', '--ok', '--warn']) {
      expect(contrast(r[word], r['--rail-ink']), word).toBeGreaterThanOrEqual(4.5); // connecting, live, error
    }
    expect(contrast(r['--rail-focus'], r['--rail'])).toBeGreaterThanOrEqual(3);
  });

  it('paper: a text field boundary is at least 3:1 on the card and on a nested tile (it identifies the field)', () => {
    const r = SHEET.resolved('nous-default');
    expect(contrast(r['--field-border'], r['--surface'])).toBeGreaterThanOrEqual(3);
    expect(contrast(r['--field-border'], r['--nested-bg'])).toBeGreaterThanOrEqual(3);
    // the chosen option's navy outline against the plain rows' hairline
    expect(contrast(r['--select-border'], r['--surface'])).toBeGreaterThanOrEqual(3);
  });

  it('the neutral ink is never weaker than the muted caption beside it, in every theme', () => {
    for (const id of THEMES) {
      const t = SHEET.resolved(id);
      const [soft, muted] = [contrast(t['--soft'], t['--surface']), contrast(t['--muted'], t['--surface'])];
      expect(soft, id).toBeGreaterThanOrEqual(muted - 0.005);
    }
  });

  it('a tone pill (MetricCard 13%, ScoreCard 14% tint of its ink) reads at 4.5 on its tint, on a nested tile and on the card', () => {
    const fails: string[] = [];
    for (const id of THEMES) {
      const t = SHEET.resolved(id);
      for (const ink of ['--soft', '--ok', '--warn', '--crit']) {
        for (const pct of [13, 14]) {
          for (const ground of ['--nested-bg', '--surface']) {
            const key = `${id}|${ink}|${pct}%|${ground}`;
            const r = contrast(t[ink], mix(t[ink], pct, 'transparent', t[ground]));
            if (key in PILL_BELOW) {
              if (r >= 4.5) fails.push(`${key} now passes (${r.toFixed(2)}): remove it from PILL_BELOW`);
              else if (r < PILL_BELOW[key]) fails.push(`${key} got worse: ${r.toFixed(2)}`);
            } else if (r < 4.5) fails.push(`${key} ${r.toFixed(2)}`);
          }
        }
      }
    }
    expect(fails).toEqual([]);
  });

  it('paper: a coloured badge reads at 4.5 on its --badge-tint pill over a nested tile', () => {
    const t = SHEET.resolved('nous-default');
    const pct = parseFloat(t['--badge-tint']);
    expect(pct).toBeGreaterThan(0);
    for (const ink of ['--warn', '--crit', '--ok', '--accent']) {
      expect(contrast(t[ink], mix(t[ink], pct, 'transparent', t['--nested-bg'])), ink).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('every recorded shortfall names a pair the matrix measures', () => {
    const measured = new Set(THEMES.flatMap((id) => matrix(SHEET.resolved(id)).map(([fg, bg]) => `${id}|${fg}|${bg}`)));
    expect(Object.keys(KNOWN_BELOW).filter((k) => !measured.has(k))).toEqual([]);
  });
});
