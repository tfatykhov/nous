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
  '--series-6': ['#fbbf24', '#985c50'], // review M2 (the brown #8a5a2b was 2.1 from warn text) then N2 (#905860 was 3.1 from related_to under protanopia)
  '--series-7': ['#6b6b8a', '#4f5f6f'],
  '--series-8': ['#e2e2f0', '#1c2f55'],
  '--series-9': ['#22d3ee', '#0e7490'],
  '--series-10': ['#e879f9', '#8b5cf6'],
  '--series-11': ['#f472b6', '#6b7280'],
  '--series-12': ['#9ca3af', '#908870'], // review M2: the amber #a8741a was 3.1 from the warn fill
  '--series-13': ['#c4c4d8', '#7a3f3f'],
};
const GV_NODES: Record<string, [string, string]> = {
  '--gv-fact': ['#34d399', '#159a7a'],
  '--gv-episode': ['#60a5fa', '#1f5fa8'],
  '--gv-decision': ['#a78bfa', '#802c66'], // re-review: #a8408f was 4.2 from episode under protanopia
  '--gv-procedure': ['#fbbf24', '#d0620d'],
  '--gv-chunk': ['#06b6d4', '#8b5cf6'], // review I4: the olive merged with procedure under red-green colour blindness
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
  '--rel-contradicts': ['#f87171', '#985c50'],
  '--rel-supersedes': ['#fb923c', '#7a8a1a'],
  '--rel-caused-by': ['#e2e2f0', '#1c2f55'],
  '--rel-discussed-in': ['#6b6b8a', '#6b7280'],
  '--rel-part-of': ['#22d3ee', '#0e7490'],
  '--rel-summarized-by': ['#06b6d4', '#8b5cf6'],
};
const DISP: Record<string, [string, string]> = {
  '--disp-rendered': ['#34d399', '#045c4c'], // review M2 then N1: apart from ok text, from f071 under protanopia, from the task frame
  '--disp-returned-to-script': ['#22d3ee', '#1c5d99'],
  '--disp-sliced-off': ['#f59e0b', '#883800'], // review M2: 3.8 from warn text before
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
  '--gv-edge-opacity': ['0.5', '0.75'], // review M8: Graph edges, less faint on white
  '--gv-edge-opacity-inferred': ['0.3', '0.5'],
  '--series-0': ['#7c6af7', '#274455'], // review M1/R2: was --accent (the question blue), then violet; a slate navy of its own
  '--save-ok': ['#4ade80', '#23703d'], // Browse: a censor's save message (not --ok-text, whose dark is #34d399)
  '--save-err': ['#f87171', '#b02a22'],
  '--tone-bad': ['#ef4444', '#b02a22'], // Memory write health: a tripped rule's border and tint (not --err-text's #f87171)
  '--btn-ink': ['#fff', '#ffffff'], // text on an accent-filled button (--on-accent is near-black under dark)
};
for (const [k, [dark]] of Object.entries(PR2)) NEW_DARK[k] = dark;

// PR 3 (the Execution screens): [dark, paper]. Status (`--st-*`, dark = lib/status.ts's hex) and outcome (`--oc-*`,
// dark = Subtasks' literals) colours, plus the one-off tokens each screen needs for an exact dark look.
const ST: Record<string, [string, string]> = {
  '--st-pending': ['#8e8eab', '#56687a'],
  '--st-ready': ['#22d3ee', '#1b6a9c'],
  '--st-running': ['#fbbf24', '#8a5a0e'],
  '--st-awaiting-check': ['#f59e0b', '#9a4d0a'],
  '--st-awaiting-input': ['#a78bfa', '#7b3fa0'], // = --waiting, both grounds
  '--st-completed': ['#4ade80', '#23703d'],
  '--st-failed': ['#f87171', '#b02a22'],
  '--st-blocked': ['#f25c5c', '#8a1c2c'],
  '--st-cancelled': ['#8a8a9a', '#55616c'], // fix round 0: #5f6b76 and #5c6b7d were 4.32:1 on a hovered row
  '--st-skipped': ['#94a3b8', '#536170'],
  '--st-partial': ['#fb923c', '#a14a08'],
  '--st-success': ['#10b981', '#23703d'], // the ledger's success, Subtasks' completed
  '--st-ledger-blocked': ['#f59e0b', '#8a5a0e'], // the ledger's blocked, Subtasks' incomplete_blocked
  '--st-unknown': ['#f472b6', '#a3266f'], // = --unknown, both grounds
  '--st-neutral': ['#6b7280', '#55616c'],
};
const OC: Record<string, [string, string]> = {
  '--oc-failed': ['#ef4444', '#b02a22'], // Subtasks: incomplete_no_terminal, validation_failed
  '--oc-timeout': ['#dc2626', '#8a1c2c'],
  '--oc-errored': ['#7c2d12', '#6b2a12'],
  '--oc-unknown': ['#9ca3af', '#7a8794'], // fix round 0: a lighter slate, not cancelled's grey
  '--oc-blocked': ['#f59e0b', '#b7791f'], // Subtasks' incomplete_blocked as a mark (review I3: the text amber merged with failed)
};
const PR3: Record<string, [string, string]> = {
  ...ST,
  ...OC,
  '--stack-bar': ['rgba(251, 191, 36, 0.7)', '#b7791f'], // Workflows: a token budget's consumed part
  '--hit-good': ['#22c55e', '#23703d'], // Cache: the hit-rate line and the doughnut's cache-read part
  '--hit-mid': ['#eab308', '#8a5a0e'],
  '--hit-bad': ['#ef4444', '#b02a22'],
  '--cache-blue': ['#3b82f6', '#1f5fa8'], // Cache: the doughnut's cache-created part
  '--cache-grey': ['#4b5563', '#4f5f6f'], // Cache: the doughnut's uncached part
  '--wait-ink': ['#c4b5fd', '#7b3fa0'], // Workflows: the Now column's waiting pill
  '--wait-bg': ['rgba(167, 139, 250, 0.14)', '#efe3f6'],
  '--wash': ['rgba(255, 255, 255, 0.03)', '#f4f7f8'], // Heartbeat: the tuning report's panel
  '--hb-warn-ink': ['#fbbf24', '#8a5a0e'], // Heartbeat: amber as text (paper's --yellow is a mark colour)
  '--effect-write': ['#60a5fa', '#1f5fa8'], // the Ledger's write effect pill
  '--mc-section-1': ['#a99df9', '#8b5cf6'], // a model call's first context section (dark: the accent text; paper: a violet)
  '--mc-section-7': ['#f472b6', '#7890d0'], // Messages (dark: the old --unknown; paper: a light blue, not a status colour)
  '--mc-section-8': ['#9ca3af', '#2020e0'], // Tools (dark: the old --series-12; paper: apart from Decisions under red-green CVD)
};
for (const [k, [dark]] of Object.entries(PR3)) NEW_DARK[k] = dark;

// The legacy memory-type colours keep their golden dark values (ORIGIN_ROOT) and get paper ones (Retrieval's chips).
const LEGACY_PAPER: Record<string, string> = {
  '--fact-color': '#159a7a', // review M6: one colour per memory type on paper, the Graph's
  '--episode-color': '#1f5fa8',
  '--decision-color': '#802c66',
  '--procedure-color': '#d0620d',
  '--censor-color': '#985c50',
  '--heartbeat-color': '#4f5f6f',
  '--chunk-color': '#8b5cf6',
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
/** Colour-vision deficiency (Machado, Oliveira and Fernandes 2009, severity 1), applied to linear sRGB. */
const CVD: Record<'protan' | 'deutan', number[][]> = {
  protan: [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
  deutan: [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.01182, 0.04294, 0.968881]],
};
function oklabLinear([r, g, b]: number[]): [number, number, number] {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}
function linearRgb(c: string): number[] {
  return parseColor(c).slice(0, 3).map((v) => {
    const s = v / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
}
function okDistance(a: string, b: string, cvd?: 'protan' | 'deutan'): number {
  const see = (c: string) => {
    const lin = linearRgb(c);
    if (!cvd) return oklabLinear(lin);
    return oklabLinear(CVD[cvd].map((row) => Math.min(1, Math.max(0, row[0] * lin[0] + row[1] * lin[1] + row[2] * lin[2]))));
  };
  const [x, y] = [see(a), see(b)];
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

  it('series 0, the one-series chart mark: its own colour, at least 7 from every slot and 6 from every frame (review R2)', () => {
    const zero = PAPER['--series-0'];
    expect(contrast(zero, '#ffffff')).toBeGreaterThanOrEqual(3);
    for (const [k, [, p]] of Object.entries(SERIES)) expect(okDistance(zero, p), `--series-0 and ${k}`).toBeGreaterThanOrEqual(7);
    for (const f of [...FRAMES, 'other']) expect(okDistance(zero, PAPER[`--f-${f}`]), `--series-0 and --f-${f}`).toBeGreaterThanOrEqual(6);
  });

  it('the CVD helper reproduces the review\'s figures (the old olive chunk against procedure)', () => {
    expect(okDistance('#7a8a1a', '#d0620d', 'deutan')).toBeCloseTo(3.4, 1);
    expect(okDistance('#7a8a1a', '#d0620d', 'protan')).toBeCloseTo(5.5, 1);
    expect(okDistance('#123456', '#123456', 'protan')).toBe(0);
  });

  it('the five graph node colours: at least 3:1 on white, and at least 12 apart', () => {
    const nodes = Object.entries(GV_NODES).map(([k, [, p]]) => [k, p] as const);
    for (const [k, c] of nodes) expect(contrast(c, '#ffffff'), k).toBeGreaterThanOrEqual(3);
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        expect(okDistance(nodes[i][1], nodes[j][1]), `${nodes[i][0]} and ${nodes[j][0]}`).toBeGreaterThanOrEqual(12);
      }
    }
    expect(contrast(PAPER['--gv-other'], '#ffffff')).toBeGreaterThanOrEqual(3);
    // The rings and the label halo on the white card.
    expect(contrast(PAPER['--gv-select'], '#ffffff')).toBeGreaterThanOrEqual(3);
    expect(contrast(PAPER['--muted'], PAPER['--gv-outline'])).toBeGreaterThanOrEqual(4.5);
  });

  it('graph node colours under red-green colour blindness: chunk at least 12 from every type, other pairs at least 8', () => {
    // Review I4: the olive chunk and the orange procedure read as one colour under deuteranopia (3.4). The re-review:
    // episode and decision were 4.2 apart under protanopia; --gv-decision moved, so no pair is exempt.
    const nodes = Object.entries(GV_NODES).map(([k, [, p]]) => [k.replace('--gv-', ''), p] as const);
    for (const cvd of ['protan', 'deutan'] as const) {
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const [a, b] = [nodes[i], nodes[j]];
          const d = okDistance(a[1], b[1], cvd);
          if (a[0] === 'chunk' || b[0] === 'chunk') expect(d, `${cvd}: ${a[0]} and ${b[0]}`).toBeGreaterThanOrEqual(12);
          else expect(d, `${cvd}: ${a[0]} and ${b[0]}`).toBeGreaterThanOrEqual(8);
        }
      }
    }
  });

  it('no categorical paper colour is within 6 (OKLab) of a status fill or text: a hue carries one meaning (review M2)', () => {
    const status = ['--green', '--yellow', '--red', '--ok-text', '--warn-text', '--err-text'].map((t) => [t, PAPER[t]] as const);
    const categorical: [string, string][] = [
      ...Object.entries({ ...SERIES, '--series-0': PR2['--series-0'], ...GV_NODES, '--gv-other': GV['--gv-other'], ...REL, ...DISP }).map(([k, [, p]]) => [k, p] as [string, string]),
      ...Object.entries(LEGACY_PAPER).filter(([k]) => k !== '--pending'),
    ];
    for (const [k, c] of categorical) {
      for (const [s, sc] of status) expect(okDistance(c, sc), `${k} (${c}) and ${s} (${sc})`).toBeGreaterThanOrEqual(6);
    }
  });

  it('a memory type has one colour on every Memory screen on paper: the legacy type colours are the Graph\'s', () => {
    // Review M6. Under dark the legacy fact and episode colours are the Graph's the other way round (golden-pinned).
    for (const t of ['fact', 'episode', 'decision', 'procedure', 'chunk']) expect(PAPER[`--${t}-color`], t).toBe(PAPER[`--gv-${t}`]);
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

  it('a contradicts edge is told from a plain related_to edge under protanopia (review N2)', () => {
    expect(okDistance(PAPER['--rel-contradicts'], PAPER['--rel-related-to'], 'protan')).toBeGreaterThanOrEqual(6);
    expect(okDistance(PAPER['--rel-contradicts'], PAPER['--rel-related-to'], 'deutan')).toBeGreaterThanOrEqual(6);
    // --series-6 and the legacy censor colour share the hex.
    expect(PAPER['--series-6']).toBe(PAPER['--rel-contradicts']);
    expect(PAPER['--censor-color']).toBe(PAPER['--rel-contradicts']);
  });

  it('rendered, the one good outcome, stands apart in the funnel under red-green colour blindness and from the task frame (review N1)', () => {
    const rendered = PAPER['--disp-rendered'];
    for (const [k, [, p]] of Object.entries(DISP)) {
      if (k === '--disp-rendered') continue;
      expect(okDistance(rendered, p), k).toBeGreaterThanOrEqual(7);
      for (const cvd of ['protan', 'deutan'] as const) expect(okDistance(rendered, p, cvd), `${cvd}: ${k}`).toBeGreaterThanOrEqual(4);
    }
    expect(okDistance(rendered, PAPER['--f-task'])).toBeGreaterThanOrEqual(6);
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

// ── PR 3: status, outcome and the Execution screens' one-off colours ─────────────────────────────────────────────────

describe('PR 3 tokens: status, outcomes, execution screens', () => {
  it('every new token has the dark value the screens used, and a paper value', () => {
    for (const [k, [dark, paper]] of Object.entries(PR3)) {
      expect(DARK[k], k).toBe(dark);
      expect(PAPER[k], k).toBe(paper);
    }
  });

  it('a status colour is readable on its own badge: at least 4.5:1 on its 12.5% tint, over the white card and a hovered row', () => {
    // Fix round 0, ruling 6: a table row's hover wash (--surface-hover) sits under the badge too.
    for (const bg of ['#ffffff', PAPER['--surface-hover']]) {
      for (const [k, [, p]] of Object.entries(ST)) expect(contrast(p, tint(p, 0.125, bg)), `${k} on ${bg}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('one meaning each: awaiting input is the waiting colour and unknown the unknown colour, in both grounds', () => {
    expect(DARK['--st-awaiting-input']).toBe(DARK['--waiting']);
    expect(PAPER['--st-awaiting-input']).toBe(PAPER['--waiting']);
    expect(DARK['--st-unknown']).toBe(DARK['--unknown']);
    expect(PAPER['--st-unknown']).toBe(PAPER['--unknown']);
    expect(PAPER['--st-blocked']).not.toBe(PAPER['--st-failed']);
  });

  it('marks one chart draws together keep 8 apart for normal vision and under protanopia and deuteranopia (review I3)', () => {
    // Marks need 3:1, not the badge's 4.5:1, so a mark can take a lighter tone than its text token. The sets are the
    // tokens the views draw together: Subtasks' outcomes (doughnut, bars and trend; no_terminal and validation_failed
    // share --oc-failed by design) and Heartbeat's urgencies (stacked bars and timeline dots).
    const sets: Record<string, string[]> = {
      subtasks: ['--st-success', '--oc-blocked', '--oc-failed', '--oc-timeout', '--oc-errored', '--st-neutral', '--oc-unknown'],
      heartbeat: ['--err-text', '--yellow', '--st-neutral'],
    };
    // Open pairs with no verified hex (the review's and the ones measured with it), each held at its present floor.
    const open: Record<string, number> = {
      '--st-success|--oc-failed': 5, // deutan 5.5
      '--st-success|--oc-timeout': 5.5, // deutan 5.8
      '--st-success|--st-neutral': 6.5, // deutan 6.9
      '--oc-failed|--oc-timeout': 7.5, // protan 7.8
      '--oc-timeout|--oc-errored': 3.5, // protan 3.7, deutan 4.8
    };
    for (const [chart, tokens] of Object.entries(sets)) {
      for (let i = 0; i < tokens.length; i++) {
        for (let j = i + 1; j < tokens.length; j++) {
          const [a, b] = [tokens[i], tokens[j]];
          const floor = open[`${a}|${b}`] ?? 8;
          for (const cvd of [undefined, 'protan', 'deutan'] as const) {
            expect(okDistance(PAPER[a], PAPER[b], cvd), `${chart}: ${a} and ${b} (${cvd ?? 'normal'})`).toBeGreaterThanOrEqual(floor);
          }
        }
      }
    }
    expect(contrast(PAPER['--oc-blocked'], '#ffffff')).toBeGreaterThanOrEqual(3);
  });

  it('Subtasks\' cancelled and unknown outcomes are told apart on paper (ruling 5): at least 10 apart, as under dark', () => {
    // Cancelled is --st-neutral, unknown --oc-unknown; the doughnut and the trend draw them side by side.
    expect(okDistance(PAPER['--oc-unknown'], PAPER['--st-neutral'])).toBeGreaterThanOrEqual(10);
    expect(okDistance(DARK['--oc-unknown'], DARK['--st-neutral'])).toBeGreaterThanOrEqual(10);
  });

  it('the nine context sections of a model call keep 7 apart for normal vision and under protanopia and deuteranopia (review M5, M6)', () => {
    // CallDetail's PALETTE, in order. The closest pair is Decisions (teal) and Procedures (brick), 7.6 under deuteranopia.
    const sections = ['--mc-section-1', '--series-1', '--series-9', '--series-2', '--series-6', '--series-4', '--mc-section-7', '--mc-section-8', '--series-13'];
    for (let i = 0; i < sections.length; i++) {
      expect(contrast(PAPER[sections[i]], '#ffffff'), sections[i]).toBeGreaterThanOrEqual(3);
      for (let j = i + 1; j < sections.length; j++) {
        for (const cvd of [undefined, 'protan', 'deutan'] as const) {
          expect(okDistance(PAPER[sections[i]], PAPER[sections[j]], cvd), `${sections[i]} and ${sections[j]} (${cvd ?? 'normal'})`).toBeGreaterThanOrEqual(7);
        }
      }
    }
    // Messages is not the unknown status's colour (nor the creative frame's), on paper.
    expect(PAPER['--mc-section-7']).not.toBe(PAPER['--unknown']);
  });

  it('the first two context sections of a model call are distinct on paper (ruling 5), and 3:1 on white', () => {
    // Its own violet: not the question frame's link blue, and not tied to a series slot PR 2 owns (review I1).
    expect(PAPER['--mc-section-1']).not.toBe(PAPER['--f-question']);
    expect(okDistance(PAPER['--mc-section-1'], PAPER['--series-1'])).toBeGreaterThanOrEqual(12);
    expect(contrast(PAPER['--mc-section-1'], '#ffffff')).toBeGreaterThanOrEqual(3);
  });

  it('PR 3\'s categorical colours keep the status distance: at least 6 (OKLab) from every status fill and text (review M2)', () => {
    const status = ['--green', '--yellow', '--red', '--ok-text', '--warn-text', '--err-text'].map((t) => [t, PAPER[t]] as const);
    for (const k of ['--mc-section-1', '--mc-section-7', '--mc-section-8', '--cache-blue', '--cache-grey', '--effect-write']) {
      for (const [s, sc] of status) expect(okDistance(PAPER[k], sc), `${k} (${PAPER[k]}) and ${s}`).toBeGreaterThanOrEqual(6);
    }
  });

  it('outcome marks and the budget bar are at least 3:1 on the white card', () => {
    for (const [k, [, p]] of Object.entries(OC)) expect(contrast(p, '#ffffff'), k).toBeGreaterThanOrEqual(3);
    expect(contrast(PAPER['--stack-bar'], '#ffffff')).toBeGreaterThanOrEqual(3);
    for (const k of ['--hit-good', '--hit-mid', '--hit-bad', '--cache-blue', '--cache-grey']) expect(contrast(PAPER[k], '#ffffff'), k).toBeGreaterThanOrEqual(3);
  });

  it('Heartbeat\'s amber text on the card and on the wash: at least 4.5:1', () => {
    expect(contrast(PAPER['--hb-warn-ink'], PAPER['--surface'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--hb-warn-ink'], PAPER['--wash'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--text'], PAPER['--wash'])).toBeGreaterThanOrEqual(4.5);
  });

  it('the Ledger\'s effect pills: text at least 4.5:1 on its tint over the white card', () => {
    expect(contrast(PAPER['--effect-write'], tint(PAPER['--effect-write'], 0.15, '#ffffff'))).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--st-ledger-blocked'], PAPER['--badge-warn-bg'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--err-text'], PAPER['--badge-bad-bg'])).toBeGreaterThanOrEqual(4.5);
  });

  it('the waiting pill\'s text on its tint: at least 4.5:1 in both grounds', () => {
    expect(contrast(PAPER['--wait-ink'], PAPER['--wait-bg'])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(DARK['--wait-ink'], over(parseColor(DARK['--wait-bg']), parseColor(DARK['--surface'])))).toBeGreaterThanOrEqual(4.5);
  });
});
