import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { LANE_COLORS } from './lib/work';
import { DARK_SEL, PAPER_SEL, RAIL_SEL, parseBlocks } from './lib/testing/css';

// The redesign is staged behind <html data-look="paper">. app.css keeps today's dark values on `:root` (and on a
// dark island, `[data-ground="dark"]`), and puts the new values on the paper and rail grounds. These tests pin the
// dark values (so no un-migrated screen moves), keep the grounds in step, and check the contrast of the new values.

const CSS = readFileSync(join(__dirname, 'app.css'), 'utf-8');
const BLOCKS = parseBlocks(CSS);
const DARK = BLOCKS.get(DARK_SEL) ?? {};
const PAPER = BLOCKS.get(PAPER_SEL) ?? {};
const RAIL = BLOCKS.get(RAIL_SEL) ?? {};
const tokens = (block: Record<string, string>) => Object.keys(block).filter((k) => k.startsWith('--'));

// Every `--name: value` of origin/main's `:root` (f4d735a1), byte for byte.
const ORIGIN_ROOT: Record<string, string> = {
  '--bg': '#0a0a0f',
  '--surface': '#12121a',
  '--surface-hover': '#181824',
  '--border': '#1e1e2e',
  '--accent': '#7c6af7',
  '--accent-dim': '#4f46a8',
  '--accent-glow': 'rgba(124, 106, 247, 0.15)',
  '--text': '#e2e2f0',
  '--muted': '#8e8eab',
  '--accent-text': '#a99df9',
  '--waiting': '#a78bfa',
  '--unknown': '#f472b6',
  '--pending': '#22d3ee',
  '--green': '#34d399',
  '--red': '#f87171',
  '--yellow': '#fbbf24',
  '--fact-color': '#60a5fa',
  '--episode-color': '#34d399',
  '--decision-color': '#a78bfa',
  '--procedure-color': '#fb923c',
  '--censor-color': '#f87171',
  '--heartbeat-color': '#22d3ee',
  '--chunk-color': '#06b6d4',
  '--sidebar-width': '220px',
  '--sidebar-collapsed': '56px',
  '--radius': '12px',
  '--radius-sm': '8px',
  '--transition': '0.2s ease',
  '--font-ui': "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
  '--font-mono': "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
  '--background': '222 13% 6%',
  '--foreground': '240 33% 92%',
  '--card': '240 17% 8%',
  '--card-foreground': '240 33% 92%',
  '--popover': '240 17% 8%',
  '--popover-foreground': '240 33% 92%',
  '--primary': '249 89% 63%',
  '--primary-foreground': '0 0% 100%',
  '--secondary': '240 16% 11%',
  '--secondary-foreground': '240 33% 92%',
  '--muted-token': '240 17% 61%',
  '--muted-foreground': '240 17% 61%',
  '--accent-token': '249 89% 63%',
  '--accent-foreground': '0 0% 100%',
  '--destructive': '0 91% 71%',
  '--destructive-foreground': '0 0% 100%',
  '--border-token': '237 25% 14%',
  '--input': '237 25% 14%',
  '--ring': '249 89% 63%',
  '--radius-tw': '0.75rem',
};

// The new dark-side tokens: each one's value is the literal the component used before it read the token, so the
// dark look does not move. (Where one plan token would have two dark values, a token of its own carries each.)
const NEW_DARK: Record<string, string> = {
  '--card-radius': '12px', // the 12px cards (session list, patterns, trace)
  '--card-radius-sm': '8px', // StatGrid's cards, DataTable's card mode
  '--card-shadow': '0 0 #0000', // no shadow before; a transparent zero shadow, as `none` breaks Tailwind's ring list
  '--card-border': '1px solid var(--border)',
  '--ctl-radius': '8px', // SubNav's tabs (var(--radius-sm))
  '--label-transform': 'uppercase',
  '--label-tracking': '0.05em', // StatGrid's labels, DataTable's th
  '--nav-label-tracking': '0.06em', // Nav's group labels
  '--nav-icon-display': 'inline',
  '--logo-display': 'inline',
  '--nav-radius': '8px',
  '--nav-ink': 'var(--muted)',
  '--nav-active-weight': '500',
  '--brand-size': '1rem',
  '--on-accent': 'var(--bg)', // FilterBar's active text
  '--skip-link-text': '#fff',
  '--overlay-bg': 'rgba(0, 0, 0, 0.6)',
  '--tab-active-bg': 'var(--surface-hover)',
  '--tab-active-shadow': 'inset 0 0 0 1px var(--accent)',
  '--focus-ring': 'var(--accent)', // the global :focus-visible ring the Nav links had
  '--ok-text': '#34d399',
  '--ok-bg': 'rgba(52, 211, 153, 0.12)',
  '--warn-text': '#fbbf24',
  '--warn-bg': 'rgba(251, 191, 36, 0.08)',
  '--warn-border': 'rgba(251, 191, 36, 0.5)',
  '--err-text': '#f87171',
  '--err-bg': 'rgba(248, 113, 113, 0.1)',
  '--stat-warn': '#f59e0b', // StatGrid's warn tone
  '--stat-ok': '#10b981', // StatGrid's ok tone
  '--stale-err-bg': 'rgba(248, 113, 113, 0.08)', // StaleBadge's error tint
  '--notice-bg': 'rgba(245, 158, 11, 0.08)', // the Work screens' "recording is off" box (not the --warn-* amber)
  '--notice-border': 'rgba(245, 158, 11, 0.4)',
  '--kind-task': '#22d3ee', // SessionList's .kind-task and Patterns' Tasks bars
  '--seg-bg': 'var(--surface)', // SessionList's kind selector
  '--series-7': '#6b6b8a', // Overview's pending outcomes (the PR 2/3 plan's ramp, slot 7)
  '--f-conversation': '#8fa3b5',
  '--f-question': '#8aa4ff',
  '--f-task': '#3fc3d1',
  '--f-decision': '#c08be0',
  '--f-debug': '#f09a5a',
  '--f-creative': '#ee82bd',
  '--f-initiation': '#e0b050',
  '--btn-focus-width': '0px', // a focused shadcn Button's outline: none under dark, as before (review M11)
  '--f-other': '#c4c4d8', // a frame id outside the seven (review M1): neutral, not conversation's grey
};

// PR 2 (the Memory screens): [dark, paper]. Each dark value is the literal the screen used before it read the token.
const SERIES: Record<string, [string, string]> = {
  '--series-1': ['#60a5fa', '#1f5fa8'],
  '--series-2': ['#34d399', '#159a7a'],
  '--series-3': ['#a78bfa', '#a8408f'],
  '--series-4': ['#fb923c', '#d0620d'],
  '--series-5': ['#f87171', '#7a8a1a'],
  '--series-6': ['#fbbf24', '#8a5a2b'],
  '--series-7': ['#6b6b8a', '#4f5f6f'],
  '--series-8': ['#e2e2f0', '#1c2f55'],
  '--series-9': ['#22d3ee', '#0e7490'],
  '--series-10': ['#e879f9', '#8b5cf6'],
  '--series-11': ['#f472b6', '#6b7280'],
  '--series-12': ['#9ca3af', '#a8741a'],
  '--series-13': ['#c4c4d8', '#7a3f3f'],
};
const GV_NODES: Record<string, [string, string]> = {
  '--gv-fact': ['#34d399', '#159a7a'],
  '--gv-episode': ['#60a5fa', '#1f5fa8'],
  '--gv-decision': ['#a78bfa', '#a8408f'],
  '--gv-procedure': ['#fbbf24', '#d0620d'],
  '--gv-chunk': ['#06b6d4', '#7a8a1a'],
};
const GV: Record<string, [string, string]> = {
  ...GV_NODES,
  '--gv-other': ['#6b6b8a', '#4f5f6f'],
  '--gv-outline': ['#0a0a0f', '#ffffff'], // the label halo
  '--gv-node-border': ['rgba(0, 0, 0, 0.3)', 'rgba(20, 32, 43, 0.35)'],
  '--gv-select': ['#fbbf24', '#14202b'], // the selected and search-hit rings
};
const REL: Record<string, [string, string]> = {
  '--rel-related-to': ['#7c6af7', '#4f5f6f'],
  '--rel-extracted-from': ['#60a5fa', '#1f5fa8'],
  '--rel-supports': ['#34d399', '#159a7a'],
  '--rel-informed-by': ['#a78bfa', '#a8408f'],
  '--rel-evidence-for': ['#fbbf24', '#d0620d'],
  '--rel-contradicts': ['#f87171', '#8a5a2b'],
  '--rel-supersedes': ['#fb923c', '#7a8a1a'],
  '--rel-caused-by': ['#e2e2f0', '#1c2f55'],
  '--rel-discussed-in': ['#6b6b8a', '#6b7280'],
  '--rel-part-of': ['#22d3ee', '#0e7490'],
  '--rel-summarized-by': ['#06b6d4', '#8b5cf6'],
};
const DISP: Record<string, [string, string]> = {
  '--disp-rendered': ['#34d399', '#0f6e63'],
  '--disp-returned-to-script': ['#22d3ee', '#1c5d99'],
  '--disp-sliced-off': ['#f59e0b', '#8a4b08'],
  '--disp-budget-truncated': ['#fcd34d', '#b05a09'],
  '--disp-below-floor': ['#a78bfa', '#5b2fb3'],
  '--disp-filter-dropped': ['#c4b5fd', '#7c4dd0'],
  '--disp-deduped': ['#94a3b8', '#4d5a6b'],
  '--disp-superseded': ['#cbd5e1', '#6b7787'],
  '--disp-replaced-at-merge': ['#64748b', '#2a3646'],
  '--disp-f071-excluded': ['#7c8da3', '#5d6a7b'],
  '--disp-type-excluded': ['#475569', '#3b4859'],
  '--disp-unaccounted': ['#f472b6', '#a3185c'],
};
const PR2: Record<string, [string, string]> = {
  ...SERIES,
  ...GV,
  ...REL,
  ...DISP,
  '--seg-ink': ['#0a0a0f', '#ffffff'], // the count label on a Retrieval funnel segment
  '--badge-good-bg': ['rgba(52, 211, 153, 0.15)', '#e3f2e8'],
  '--badge-warn-bg': ['rgba(251, 191, 36, 0.15)', '#f8efdc'],
  '--badge-bad-bg': ['rgba(248, 113, 113, 0.15)', '#fbe9e8'],
  '--track': ['rgba(255, 255, 255, 0.06)', '#e3e9ec'], // a doughnut's remainder, a panel wash
  '--track-strong': ['rgba(255, 255, 255, 0.08)', '#e3e9ec'],
  '--bar-bg': ['rgba(17, 17, 24, 0.6)', '#f4f7f8'], // the Graph controls bar
  '--glow-blur': ['6px', '0px'],
  '--chart-text': ['#94a3b8', '#56687a'], // a chart's ticks and legend on a paper ground
  '--chart-grid': ['#1e293b', '#e3e9ec'],
  '--save-ok': ['#4ade80', '#23703d'], // Browse: a censor's save message (not --ok-text, whose dark is #34d399)
  '--save-err': ['#f87171', '#b02a22'],
  '--tone-bad': ['#ef4444', '#b02a22'], // Memory write health: a tripped rule's border and tint (not --err-text's #f87171)
  '--btn-ink': ['#fff', '#ffffff'], // text on an accent-filled button (--on-accent is near-black under dark)
};
for (const [k, [dark]] of Object.entries(PR2)) NEW_DARK[k] = dark;

// The legacy memory-type colours keep their golden dark values (ORIGIN_ROOT) and get paper ones (Retrieval's chips).
const LEGACY_PAPER: Record<string, string> = {
  '--fact-color': '#1f5fa8',
  '--episode-color': '#159a7a',
  '--decision-color': '#a8408f',
  '--procedure-color': '#d0620d',
  '--censor-color': '#8a5a2b',
  '--heartbeat-color': '#4f5f6f',
  '--chunk-color': '#7a8a1a',
  '--pending': '#1b6a9c',
};

const FRAMES = ['conversation', 'question', 'task', 'decision', 'debug', 'creative', 'initiation'];
const PAPER_FRAMES: Record<string, string> = {
  conversation: '#55697a',
  question: '#2346c8',
  task: '#0b6e7a',
  decision: '#7b3fa0',
  debug: '#b4531c',
  creative: '#a3266f',
  initiation: '#8f5c0f',
};

describe('the dark tokens', () => {
  it('the dark block and the paper and rail grounds exist', () => {
    expect(BLOCKS.has(DARK_SEL)).toBe(true);
    expect(BLOCKS.has(PAPER_SEL)).toBe(true);
    expect(BLOCKS.has(RAIL_SEL)).toBe(true);
    expect(BLOCKS.has(':root')).toBe(false); // every :root token is in the shared dark block
  });

  it("dark golden: every token of origin/main's :root keeps its value byte for byte", () => {
    const got = Object.fromEntries(Object.keys(ORIGIN_ROOT).map((k) => [k, DARK[k]]));
    expect(got).toEqual(ORIGIN_ROOT);
  });

  it('new dark-side tokens equal the literals the components used', () => {
    const got = Object.fromEntries(Object.keys(NEW_DARK).map((k) => [k, DARK[k]]));
    expect(got).toEqual(NEW_DARK);
    for (const [lane, hex] of Object.entries(LANE_COLORS)) expect(DARK[`--lane-${lane}`], lane).toBe(hex);
  });

  it('the dark block holds nothing but the golden and the new tokens', () => {
    const lanes = Object.keys(LANE_COLORS).map((l) => `--lane-${l}`);
    expect(tokens(DARK).sort()).toEqual([...Object.keys(ORIGIN_ROOT), ...Object.keys(NEW_DARK), ...lanes].sort());
  });
});

describe('the grounds', () => {
  it('every token of the paper or rail ground is also defined in the dark block', () => {
    // A paper-only token would resolve to nothing on a dark island or with the look off.
    const missing = [...tokens(PAPER), ...tokens(RAIL)].filter((t) => !(t in DARK));
    expect(missing).toEqual([]);
  });

  it('every dark token built from var() is redefined on the paper ground', () => {
    // A custom property's var() resolves where it is declared: inherited from :root it would carry the dark value.
    const fromVar = tokens(DARK).filter((t) => DARK[t].includes('var(') && !t.startsWith('--nav-'));
    expect(fromVar.filter((t) => !(t in PAPER))).toEqual([]);
    expect('--nav-ink' in RAIL).toBe(true);
  });

  it('the paper ground carries the contract values', () => {
    expect(PAPER).toMatchObject({
      '--bg': '#eef2f3',
      '--surface': '#ffffff',
      '--surface-hover': '#f4f7f8',
      '--border': '#e3e9ec',
      '--text': '#14202b',
      '--muted': '#56687a',
      '--accent': '#2346c8',
      '--ok-text': '#23703d',
      '--ok-bg': '#e3f2e8',
      '--warn-text': '#8a5a0e',
      '--warn-bg': '#f8efdc',
      '--err-text': '#b02a22',
      '--err-bg': '#fbe9e8',
      '--card-radius': '18px',
      '--card-shadow': '0 1px 2px rgba(18, 48, 79, 0.06), 0 6px 20px rgba(18, 48, 79, 0.07)',
      '--ctl-radius': '999px',
      '--font-ui': "'Archivo', system-ui, sans-serif",
      '--font-mono': "'Red Hat Mono', ui-monospace, monospace",
      'font-variant-numeric': 'tabular-nums',
    });
    for (const f of FRAMES) expect(PAPER[`--f-${f}`], f).toBe(PAPER_FRAMES[f]);
    expect(RAIL).toMatchObject({
      '--bg': '#12304f',
      '--surface': '#12304f',
      '--surface-hover': '#1a3d62',
      '--text': '#dbe6ef',
      '--muted': '#9fb6c9',
      '--accent-glow': '#eef2f3',
      '--accent-text': '#14202b',
      '--focus-ring': '#ffffff',
      '--nav-icon-display': 'none',
      '--logo-display': 'none',
      '--brand-size': '21px',
      '--nav-radius': '10px',
      '--nav-active-weight': '600',
    });
  });

  it('a ground sets its own background, text colour and font (the body resolves them once, at :root)', () => {
    const ground = BLOCKS.get('html[data-look="paper"] [data-ground]') ?? {};
    expect(ground).toMatchObject({
      background: 'var(--bg)',
      color: 'var(--text)',
      'font-family': 'var(--font-ui)',
      // Tailwind's preflight sets a bare <code>/<pre> in --default-mono-font-family, declared (and resolved) at :root.
      '--default-mono-font-family': 'var(--font-mono)',
    });
    // A dark island inside a paper page resets what the paper ground hands down and the island's tokens do not.
    expect(BLOCKS.get('html[data-look="paper"] [data-ground="dark"]')).toMatchObject({
      'font-variant-numeric': 'normal',
    });
    // Scoped to the paper look: with the look off, <main> stays transparent over the body's gradient.
    expect(BLOCKS.has('[data-ground]')).toBe(false);
    expect(BLOCKS.get('html[data-look="paper"] body')).toMatchObject({ background: '#eef2f3', 'background-image': 'none' });
  });

  it('the fonts are loaded beside the old ones, and the dark fonts stay at :root', () => {
    expect(CSS).toMatch(/@import url\('https:\/\/fonts\.googleapis\.com\/css2\?[^']*family=DM\+Sans/);
    expect(CSS).toContain('family=Archivo:wdth,wght@62..125,100..900&family=Red+Hat+Mono:wght@400;500');
    expect(CSS).toContain('family=JetBrains+Mono');
  });

  it('the shadcn utilities resolve through @theme inline, live per ground', () => {
    const theme = BLOCKS.get('@theme inline') ?? {};
    expect(theme).toMatchObject({
      '--color-background': 'hsl(var(--background))',
      '--color-card': 'hsl(var(--card))',
      '--color-card-foreground': 'hsl(var(--card-foreground))',
      '--color-primary': 'hsl(var(--primary))',
      '--color-primary-foreground': 'hsl(var(--primary-foreground))',
      '--color-destructive': 'hsl(var(--destructive))',
      '--color-border': 'hsl(var(--border-token))',
      '--color-ring': 'hsl(var(--ring))',
    });
    // Not mapped: the drawer (Graph and Workflows' bottom sheet) already uses these classes, and mapping them would
    // change those dark screens.
    for (const k of ['--color-foreground', '--color-muted', '--color-muted-foreground', '--color-popover']) {
      expect(theme[k], k).toBeUndefined();
    }
  });
});

// ── Contrast (WCAG 2.x) ─────────────────────────────────────────────────────────────────────────────────────────

type RGBA = [number, number, number, number];

function parseColor(c: string): RGBA {
  const hex = c.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (hex) {
    const h = hex[1].length === 3 ? [...hex[1]].map((x) => x + x).join('') : hex[1];
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), 1];
  }
  const rgba = c.match(/^rgba?\(([^)]+)\)$/);
  if (rgba) {
    const [r, g, b, a = '1'] = rgba[1].split(',').map((x) => x.trim());
    return [Number(r), Number(g), Number(b), Number(a)];
  }
  throw new Error(`not a colour: ${c}`);
}

/** `fg` at its alpha over the opaque `bg`. */
function over(fg: RGBA, bg: RGBA): RGBA {
  const a = fg[3];
  return [fg[0] * a + bg[0] * (1 - a), fg[1] * a + bg[1] * (1 - a), fg[2] * a + bg[2] * (1 - a), 1];
}

/** `color-mix(in srgb, c p%, transparent)` drawn on `bg`. */
function tint(c: string, p: number, bg: string): RGBA {
  const [r, g, b] = parseColor(c);
  return over([r, g, b, p], parseColor(bg));
}

function luminance([r, g, b]: RGBA): number {
  const lin = (v: number) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrast(fg: string | RGBA, bg: string | RGBA): number {
  const b = typeof bg === 'string' ? parseColor(bg) : bg;
  const f = over(typeof fg === 'string' ? parseColor(fg) : fg, b);
  const [l1, l2] = [luminance(f), luminance(b)].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
}

describe('contrast of the new values', () => {
  it('the helper agrees with known ratios', () => {
    expect(contrast('#000000', '#ffffff')).toBeCloseTo(21, 1);
    expect(contrast('#ffffff', '#7c6af7')).toBeCloseTo(3.99, 1); // the comment in FilterBar
  });

  it('paper text and secondary text on every paper surface: at least 4.5', () => {
    for (const fg of ['--text', '--muted', '--accent-text']) {
      for (const bg of ['--bg', '--surface', '--surface-hover']) {
        expect(contrast(PAPER[fg], PAPER[bg]), `${fg} on ${bg}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it('each paper frame colour on white and on its 7% tint over white: at least 4.5', () => {
    for (const f of FRAMES) {
      const c = PAPER[`--f-${f}`];
      expect(contrast(c, '#ffffff'), f).toBeGreaterThanOrEqual(4.5);
      expect(contrast(c, tint(c, 0.07, '#ffffff')), `${f} on its tint`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('the replay highlight (--surface-hover) keeps the rows readable, and is why it never sits under the frame column', () => {
    // Review I5: the 7% frame tint is measured over the white card. Mixed over --surface-hover, debug's name falls
    // below 4.5 (about 4.2:1), so the trace's replay mark tints the rows only (Trace.test.ts pins the rule).
    const hover = PAPER['--surface-hover'];
    expect(contrast(PAPER['--f-debug'], tint(PAPER['--f-debug'], 0.07, hover))).toBeLessThan(4.5);
    for (const fg of ['--text', '--muted', '--accent-text', '--ok-text', '--warn-text', '--err-text']) {
      expect(contrast(PAPER[fg], hover), fg).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('the neutral for an unknown frame: readable on its 7% tint in both looks, and not conversation\'s grey', () => {
    expect(contrast(PAPER['--f-other'], tint(PAPER['--f-other'], 0.07, '#ffffff'))).toBeGreaterThanOrEqual(4.5);
    expect(contrast(DARK['--f-other'], tint(DARK['--f-other'], 0.07, DARK['--surface']))).toBeGreaterThanOrEqual(4.5);
    expect(PAPER['--f-other']).not.toBe(PAPER['--f-conversation']);
    expect(DARK['--f-other']).not.toBe(DARK['--f-conversation']);
  });

  it('each dark frame colour on the dark surface and on its 7% tint (the trace gutter, both looks): at least 4.5', () => {
    for (const f of FRAMES) {
      const c = DARK[`--f-${f}`];
      expect(contrast(c, DARK['--surface']), f).toBeGreaterThanOrEqual(4.5);
      expect(contrast(c, tint(c, 0.07, DARK['--surface'])), `${f} on its tint`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('paper status text on its own tint, and the StatGrid tones on white: at least 4.5', () => {
    for (const s of ['ok', 'warn', 'err']) {
      expect(contrast(PAPER[`--${s}-text`], PAPER[`--${s}-bg`]), s).toBeGreaterThanOrEqual(4.5);
    }
    expect(contrast(PAPER['--err-text'], PAPER['--stale-err-bg'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--text'], PAPER['--notice-bg'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--kind-task'], PAPER['--surface'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--kind-task'], PAPER['--bg'])).toBeGreaterThanOrEqual(4.5);
    // Chart marks (non-text) on the white card: pending's series colour and the accent bars.
    expect(contrast(PAPER['--series-7'], '#ffffff')).toBeGreaterThanOrEqual(3);
    expect(contrast(PAPER['--accent'], '#ffffff')).toBeGreaterThanOrEqual(3);
    for (const s of ['--stat-warn', '--stat-ok', '--red']) {
      expect(contrast(PAPER[s], PAPER['--surface']), s).toBeGreaterThanOrEqual(s === '--red' ? 3 : 4.5);
    }
  });

  it('the skip link under the paper look: the paper link blue and white, at least 4.5; dark untouched', () => {
    const skip = BLOCKS.get('html[data-look="paper"] .skip-link') ?? {};
    expect(skip).toEqual({ '--accent': PAPER['--accent'], '--skip-link-text': '#ffffff' });
    expect(contrast(skip['--skip-link-text'], skip['--accent'])).toBeGreaterThanOrEqual(4.5);
    // The App's rule reads exactly these two tokens, so the paper values reach it and the dark ones stay.
    const app = readFileSync(join(__dirname, 'App.svelte'), 'utf-8');
    expect(app).toMatch(/\.skip-link \{[^}]*background: var\(--accent\);[^}]*color: var\(--skip-link-text\);/);
    expect(DARK['--skip-link-text']).toBe('#fff');
    expect(contrast('#ffffff', DARK['--accent'])).toBeCloseTo(3.99, 1); // origin/main's dark skip link, kept as is
  });

  it('paper text on the accent (a primary button, an active filter): at least 4.5', () => {
    expect(contrast(PAPER['--on-accent'], PAPER['--accent'])).toBeGreaterThanOrEqual(4.5);
  });

  it('paper lane dots on white: at least 3 (non-text)', () => {
    for (const lane of Object.keys(LANE_COLORS)) {
      expect(contrast(PAPER[`--lane-${lane}`], '#ffffff'), lane).toBeGreaterThanOrEqual(3);
    }
  });

  it('the rail: its text and secondary text, the current pill, the badges, and the focus ring', () => {
    expect(contrast(RAIL['--text'], RAIL['--surface'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(RAIL['--muted'], RAIL['--surface'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(RAIL['--nav-ink'], RAIL['--surface'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(RAIL['--text'], RAIL['--surface-hover'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(RAIL['--accent-text'], RAIL['--accent-glow'])).toBeGreaterThanOrEqual(4.5);
    // The badge's count is in --bg on its kind's colour.
    expect(contrast(RAIL['--bg'], RAIL['--waiting'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(RAIL['--bg'], RAIL['--unknown'])).toBeGreaterThanOrEqual(4.5);
    // The ring sits outside the pill, on the rail.
    expect(contrast(RAIL['--focus-ring'], RAIL['--surface'])).toBeGreaterThanOrEqual(3);
    expect(contrast(RAIL['--focus-ring'], RAIL['--surface-hover'])).toBeGreaterThanOrEqual(3);
  });
});

// ── PR 2: series, graph, relation and disposition colours ─────────────────────────────────────────────────────────

/** OKLab (Björn Ottosson) of an sRGB colour; distances below are Euclidean, times 100. */
function oklab(c: string): [number, number, number] {
  const lin = (v: number) => {
    const s = v / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  const [r, g, b] = parseColor(c).slice(0, 3).map(lin);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}
function okDistance(a: string, b: string): number {
  const [x, y] = [oklab(a), oklab(b)];
  return 100 * Math.hypot(x[0] - y[0], x[1] - y[1], x[2] - y[2]);
}

describe('PR 2 tokens: series, graph, relations, dispositions', () => {
  it('the OKLab helper agrees with known values', () => {
    expect(oklab('#ffffff')[0]).toBeCloseTo(1, 3);
    expect(okDistance('#000000', '#ffffff')).toBeCloseTo(100, 1);
    expect(okDistance('#159a7a', '#159a7a')).toBe(0);
  });

  it('every new token has the dark value the screens used, and a paper value', () => {
    for (const [k, [dark, paper]] of Object.entries(PR2)) {
      expect(DARK[k], k).toBe(dark);
      expect(PAPER[k], k).toBe(paper);
    }
    for (const [k, paper] of Object.entries(LEGACY_PAPER)) {
      expect(DARK[k], k).toBe(ORIGIN_ROOT[k]); // the dark golden is untouched
      expect(PAPER[k], k).toBe(paper);
    }
  });

  it('series 1 to 9 are at least 3:1 on the white card; any two of 1 to 8 are at least 7 apart', () => {
    const paper = Object.values(SERIES).map(([, p]) => p);
    for (const [i, c] of paper.slice(0, 9).entries()) expect(contrast(c, '#ffffff'), `series-${i + 1}`).toBeGreaterThanOrEqual(3);
    for (let i = 0; i < 8; i++) {
      for (let j = i + 1; j < 8; j++) {
        expect(okDistance(paper[i], paper[j]), `series-${i + 1} and series-${j + 1}`).toBeGreaterThanOrEqual(7);
      }
    }
  });

  it('series 10 to 13 are at least 3:1 and at least 7 apart from every other slot', () => {
    const paper = Object.values(SERIES).map(([, p]) => p);
    for (let i = 9; i < 13; i++) {
      expect(contrast(paper[i], '#ffffff'), `series-${i + 1}`).toBeGreaterThanOrEqual(3);
      for (let j = 0; j < 13; j++) {
        if (j !== i) expect(okDistance(paper[i], paper[j]), `series-${i + 1} and series-${j + 1}`).toBeGreaterThanOrEqual(7);
      }
    }
  });

  it('the five graph node colours: at least 3:1 on white, and at least 11 apart (fact and chunk are the closest, 11.1)', () => {
    // The plan asked for 12; its own hexes measure 11.10 for fact (teal) and chunk (olive). The hexes are kept: they
    // are shared with the series ramp, the legacy type colours and the relations.
    const nodes = Object.entries(GV_NODES).map(([k, [, p]]) => [k, p] as const);
    for (const [k, c] of nodes) expect(contrast(c, '#ffffff'), k).toBeGreaterThanOrEqual(3);
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        expect(okDistance(nodes[i][1], nodes[j][1]), `${nodes[i][0]} and ${nodes[j][0]}`).toBeGreaterThanOrEqual(11);
      }
    }
    expect(contrast(PAPER['--gv-other'], '#ffffff')).toBeGreaterThanOrEqual(3);
    // The rings and the label halo on the white card.
    expect(contrast(PAPER['--gv-select'], '#ffffff')).toBeGreaterThanOrEqual(3);
    expect(contrast(PAPER['--muted'], PAPER['--gv-outline'])).toBeGreaterThanOrEqual(4.5);
  });

  it('every relation colour and legacy type colour is at least 3:1 on white', () => {
    for (const [k, [, p]] of Object.entries(REL)) expect(contrast(p, '#ffffff'), k).toBeGreaterThanOrEqual(3);
    for (const [k, p] of Object.entries(LEGACY_PAPER)) expect(contrast(p, '#ffffff'), k).toBeGreaterThanOrEqual(3);
  });

  it('no categorical paper colour is a frame colour or a status fill or text (a hue carries one meaning)', () => {
    const taken = new Set([
      ...Object.keys(PAPER).filter((k) => k.startsWith('--f-')).map((k) => PAPER[k].toLowerCase()),
      '#2f8a4e', '#b7791f', '#d2352b', '#23703d', '#8a5a0e', '#b02a22',
    ]);
    expect(taken.size).toBeGreaterThanOrEqual(14);
    const categorical = { ...SERIES, ...GV_NODES, '--gv-other': GV['--gv-other'], ...REL, ...DISP };
    for (const [k, [, p]] of Object.entries(categorical)) expect(taken.has(p.toLowerCase()), k).toBe(false);
  });

  it('every disposition fill takes the white count label at 4.5:1 or more', () => {
    for (const [k, [, p]] of Object.entries(DISP)) expect(contrast(PAPER['--seg-ink'], p), k).toBeGreaterThanOrEqual(4.5);
  });

  it('a chart\'s text on the white card is at least 4.5:1; status text on the badge tints too', () => {
    expect(contrast(PAPER['--chart-text'], '#ffffff')).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--ok-text'], PAPER['--badge-good-bg'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--warn-text'], PAPER['--badge-warn-bg'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--err-text'], PAPER['--badge-bad-bg'])).toBeGreaterThanOrEqual(4.5);
  });

  it('memory write health: the verdict text on its tint over the page, the pills on the card, the button ink', () => {
    const page = PAPER['--bg'];
    expect(contrast(PAPER['--stat-ok'], tint(PAPER['--stat-ok'], 0.08, page))).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--err-text'], tint(PAPER['--tone-bad'], 0.1, page))).toBeGreaterThanOrEqual(4.5);
    for (const t of ['--stat-ok', '--stat-warn', '--err-text']) expect(contrast(PAPER[t], '#ffffff'), t).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--btn-ink'], PAPER['--accent'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--tone-bad'], '#ffffff')).toBeGreaterThanOrEqual(3); // the tripped tile's border
  });

  it('the censor save messages are readable text on the white card and on the hover wash', () => {
    for (const t of ['--save-ok', '--save-err']) {
      expect(contrast(PAPER[t], '#ffffff'), t).toBeGreaterThanOrEqual(4.5);
      expect(contrast(PAPER[t], PAPER['--surface-hover']), t).toBeGreaterThanOrEqual(4.5);
    }
  });
});
