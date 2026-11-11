import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { LANE_COLORS } from './lib/work';
import { ROOT_SEL, RAIL_SEL, block, parseBlocks } from './lib/testing/css';

// The dashboard has one look (paper, PR 7). Its tokens are at `:root`, the Atlantic-blue rail's at `.rail`. These tests
// pin the values (the flip moved the paper block to :root verbatim: PAPER_GOLDEN) and check their contrast and their
// distances under colour blindness.

const CSS = readFileSync(join(__dirname, 'app.css'), 'utf-8');
const BLOCKS = parseBlocks(CSS);
/** The one look's tokens (the paper look's, named so the assertions read as before the flip). */
const PAPER = block(BLOCKS, ROOT_SEL, 50);
const RAIL = block(BLOCKS, RAIL_SEL, 10);
const tokens = (block: Record<string, string>) => Object.keys(block).filter((k) => k.startsWith('--'));

// Every token of the paper block as parsed at the commit before the flip (PR 7 T2, e2a91aa1: the page ground's block
// under the look switch), byte for byte. The flip moved the block to :root unchanged; its `font-variant-numeric:
// tabular-nums` went to body.
const PAPER_GOLDEN: Record<string, string> = {
  '--bg': '#eef2f3',
  '--surface': '#ffffff',
  '--surface-hover': '#f4f7f8',
  '--border': '#e3e9ec',
  '--accent': '#2346c8',
  '--accent-dim': '#12304f',
  '--accent-glow': '#e6ecfb',
  '--accent-text': '#2346c8',
  '--text': '#14202b',
  '--muted': '#56687a',
  '--green': '#2f8a4e',
  '--red': '#d2352b',
  '--yellow': '#b7791f',
  '--waiting': '#7b3fa0',
  '--unknown': '#a3266f',
  '--radius': '18px',
  '--radius-sm': '8px',
  '--transition': '0.2s cubic-bezier(0.16, 1, 0.3, 1)',
  '--font-ui': "'Archivo', system-ui, sans-serif",
  '--font-mono': "'Red Hat Mono', ui-monospace, monospace",
  '--ok-text': '#23703d',
  '--ok-bg': '#e3f2e8',
  '--warn-text': '#8a5a0e',
  '--warn-bg': '#f8efdc',
  '--warn-border': 'transparent',
  '--err-text': '#b02a22',
  '--err-bg': '#fbe9e8',
  '--stat-warn': '#8a5a0e',
  '--stat-ok': '#23703d',
  '--stale-err-bg': '#fbe9e8',
  '--notice-bg': '#f8efdc',
  '--notice-border': 'transparent',
  '--kind-task': '#0b6e7a',
  '--seg-bg': 'transparent',
  '--series-7': '#4f5f6f',
  '--f-conversation': '#55697a',
  '--f-question': '#2346c8',
  '--f-task': '#0b6e7a',
  '--f-decision': '#7b3fa0',
  '--f-debug': '#b4531c',
  '--f-creative': '#a3266f',
  '--f-initiation': '#8f5c0f',
  '--f-other': '#14202b',
  '--lane-memory': '#3a6ea5',
  '--lane-live': '#946213',
  '--lane-workspace': '#5f6b76',
  '--lane-acts': '#96406b',
  '--lane-other': '#56687a',
  '--card-radius': '18px',
  '--card-radius-sm': '12px',
  '--card-shadow': '0 1px 2px rgba(18, 48, 79, 0.06), 0 6px 20px rgba(18, 48, 79, 0.07)',
  '--card-border': '1px solid transparent',
  '--ctl-radius': '999px',
  '--label-transform': 'none',
  '--label-tracking': '0',
  '--on-accent': '#ffffff',
  '--tab-active-bg': '#ffffff',
  '--tab-active-shadow': '0 1px 3px rgba(18, 48, 79, 0.12)',
  '--focus-ring': '#2346c8',
  '--btn-focus-width': '2px',
  '--pending': '#1b6a9c',
  '--fact-color': '#159a7a',
  '--episode-color': '#1f5fa8',
  '--decision-color': '#802c66',
  '--procedure-color': '#d0620d',
  '--censor-color': '#985c50',
  '--heartbeat-color': '#4f5f6f',
  '--chunk-color': '#8b5cf6',
  '--gv-edge-opacity': '0.75',
  '--gv-edge-opacity-inferred': '0.5',
  '--series-0': '#274455',
  '--series-1': '#1f5fa8',
  '--series-2': '#159a7a',
  '--series-3': '#a8408f',
  '--series-4': '#d0620d',
  '--series-5': '#7a8a1a',
  '--series-6': '#985c50',
  '--series-8': '#1c2f55',
  '--series-9': '#0e7490',
  '--series-10': '#8b5cf6',
  '--series-11': '#6b7280',
  '--series-12': '#908870',
  '--series-13': '#7a3f3f',
  '--gv-fact': '#159a7a',
  '--gv-episode': '#1f5fa8',
  '--gv-decision': '#802c66',
  '--gv-procedure': '#d0620d',
  '--gv-chunk': '#8b5cf6',
  '--gv-other': '#4f5f6f',
  '--gv-outline': '#ffffff',
  '--gv-node-border': 'rgba(20, 32, 43, 0.35)',
  '--gv-select': '#14202b',
  '--rel-related-to': '#4f5f6f',
  '--rel-extracted-from': '#1f5fa8',
  '--rel-supports': '#159a7a',
  '--rel-informed-by': '#a8408f',
  '--rel-evidence-for': '#d0620d',
  '--rel-contradicts': '#985c50',
  '--rel-supersedes': '#7a8a1a',
  '--rel-caused-by': '#1c2f55',
  '--rel-discussed-in': '#6b7280',
  '--rel-part-of': '#0e7490',
  '--rel-summarized-by': '#8b5cf6',
  '--disp-rendered': '#045c4c',
  '--disp-returned-to-script': '#1c5d99',
  '--disp-sliced-off': '#883800',
  '--disp-budget-truncated': '#b05a09',
  '--disp-below-floor': '#5b2fb3',
  '--disp-filter-dropped': '#7c4dd0',
  '--disp-deduped': '#4d5a6b',
  '--disp-superseded': '#6b7787',
  '--disp-replaced-at-merge': '#2a3646',
  '--disp-f071-excluded': '#5d6a7b',
  '--disp-type-excluded': '#3b4859',
  '--disp-unaccounted': '#a3185c',
  '--seg-ink': '#ffffff',
  '--badge-good-bg': '#e3f2e8',
  '--badge-warn-bg': '#f8efdc',
  '--badge-bad-bg': '#fbe9e8',
  '--track': '#e3e9ec',
  '--track-strong': '#e3e9ec',
  '--bar-bg': '#f4f7f8',
  '--glow-blur': '0px',
  '--chart-text': '#56687a',
  '--chart-grid': '#e3e9ec',
  '--save-ok': '#23703d',
  '--save-err': '#b02a22',
  '--tone-bad': '#b02a22',
  '--btn-ink': '#ffffff',
  '--st-pending': '#56687a',
  '--st-ready': '#1b6a9c',
  '--st-running': '#8a5a0e',
  '--st-awaiting-check': '#9a4d0a',
  '--st-awaiting-input': '#7b3fa0',
  '--st-completed': '#23703d',
  '--st-failed': '#b02a22',
  '--st-blocked': '#8a1c2c',
  '--st-cancelled': '#55616c',
  '--st-skipped': '#536170',
  '--st-partial': '#a14a08',
  '--st-success': '#23703d',
  '--st-ledger-blocked': '#8a5a0e',
  '--st-unknown': '#a3266f',
  '--st-neutral': '#55616c',
  '--oc-failed': '#b02a22',
  '--oc-timeout': '#881c34',
  '--oc-errored': '#641400',
  '--oc-unknown': '#7a8794',
  '--oc-blocked': '#b7791f',
  '--stack-bar': '#b7791f',
  '--hit-good': '#23703d',
  '--hit-mid': '#8a5a0e',
  '--hit-bad': '#b02a22',
  '--cache-blue': '#1f5fa8',
  '--cache-grey': '#4f5f6f',
  '--wait-ink': '#7b3fa0',
  '--wait-bg': '#efe3f6',
  '--wash': '#f4f7f8',
  '--hb-warn-ink': '#8a5a0e',
  '--effect-write': '#1f5fa8',
  '--mc-section-1': '#8b5cf6',
  '--mc-section-7': '#7890d0',
  '--mc-section-8': '#382880',
  '--field-border': '#7a8896',
  '--signal-self-corrected': '#b87ab3',
  '--oc-success': '#3f8772',
  '--background': '192 17% 94%',
  '--foreground': '209 37% 12%',
  '--card': '0 0% 100%',
  '--card-foreground': '209 37% 12%',
  '--popover': '0 0% 100%',
  '--primary': '210 63% 19%',
  '--primary-foreground': '0 0% 100%',
  '--secondary': '195 22% 96%',
  '--secondary-foreground': '209 37% 12%',
  '--muted-foreground': '210 17% 41%',
  '--accent-foreground': '0 0% 100%',
  '--destructive': '4 66% 50%',
  '--border-token': '200 19% 91%',
  '--input': '200 19% 91%',
  '--ring': '227 70% 46%',
};

// The old :root's tokens that have no paper value and stay: structure, the skip link's text, and the shadcn variables
// no utility maps (Task 5 deletes the last two kinds where nothing reads them).
const KEPT: Record<string, string> = {
  '--sidebar-width': '220px',
  '--sidebar-collapsed': '56px',
  '--overlay-bg': 'rgba(0, 0, 0, 0.6)',
  '--skip-link-text': '#fff',
  '--popover-foreground': '240 33% 92%',
  '--muted-token': '240 17% 61%',
  '--accent-token': '249 89% 63%',
  '--destructive-foreground': '0 0% 100%',
  '--radius-tw': '0.75rem',
};

// PR 2 (the Memory screens): the paper values.
const SERIES: Record<string, string> = {
  '--series-1': '#1f5fa8',
  '--series-2': '#159a7a',
  '--series-3': '#a8408f',
  '--series-4': '#d0620d',
  '--series-5': '#7a8a1a',
  '--series-6': '#985c50', // review M2 (the brown #8a5a2b was 2.1 from warn text) then N2 (#905860 was 3.1 from related_to under protanopia)
  '--series-7': '#4f5f6f',
  '--series-8': '#1c2f55',
  '--series-9': '#0e7490',
  '--series-10': '#8b5cf6',
  '--series-11': '#6b7280',
  '--series-12': '#908870', // review M2: the amber #a8741a was 3.1 from the warn fill
  '--series-13': '#7a3f3f',
};
const GV_NODES: Record<string, string> = {
  '--gv-fact': '#159a7a',
  '--gv-episode': '#1f5fa8',
  '--gv-decision': '#802c66', // re-review: #a8408f was 4.2 from episode under protanopia
  '--gv-procedure': '#d0620d',
  '--gv-chunk': '#8b5cf6', // review I4: the olive merged with procedure under red-green colour blindness
};
const GV: Record<string, string> = {
  ...GV_NODES,
  '--gv-other': '#4f5f6f',
  '--gv-outline': '#ffffff', // the label halo
  '--gv-node-border': 'rgba(20, 32, 43, 0.35)',
  '--gv-select': '#14202b', // the selected and search-hit rings
};
const REL: Record<string, string> = {
  '--rel-related-to': '#4f5f6f',
  '--rel-extracted-from': '#1f5fa8',
  '--rel-supports': '#159a7a',
  '--rel-informed-by': '#a8408f',
  '--rel-evidence-for': '#d0620d',
  '--rel-contradicts': '#985c50',
  '--rel-supersedes': '#7a8a1a',
  '--rel-caused-by': '#1c2f55',
  '--rel-discussed-in': '#6b7280',
  '--rel-part-of': '#0e7490',
  '--rel-summarized-by': '#8b5cf6',
};
const DISP: Record<string, string> = {
  '--disp-rendered': '#045c4c', // review M2 then N1: apart from ok text, from f071 under protanopia, from the task frame
  '--disp-returned-to-script': '#1c5d99',
  '--disp-sliced-off': '#883800', // review M2: 3.8 from warn text before
  '--disp-budget-truncated': '#b05a09',
  '--disp-below-floor': '#5b2fb3',
  '--disp-filter-dropped': '#7c4dd0',
  '--disp-deduped': '#4d5a6b',
  '--disp-superseded': '#6b7787',
  '--disp-replaced-at-merge': '#2a3646',
  '--disp-f071-excluded': '#5d6a7b',
  '--disp-type-excluded': '#3b4859',
  '--disp-unaccounted': '#a3185c',
};
const PR2: Record<string, string> = {
  ...SERIES,
  ...GV,
  ...REL,
  ...DISP,
  '--seg-ink': '#ffffff', // the count label on a Retrieval funnel segment
  '--badge-good-bg': '#e3f2e8',
  '--badge-warn-bg': '#f8efdc',
  '--badge-bad-bg': '#fbe9e8',
  '--track': '#e3e9ec', // a doughnut's remainder, a panel wash
  '--track-strong': '#e3e9ec',
  '--bar-bg': '#f4f7f8', // the Graph controls bar
  '--glow-blur': '0px',
  '--chart-text': '#56687a', // a chart's ticks and legend on a paper ground
  '--chart-grid': '#e3e9ec',
  '--gv-edge-opacity': '0.75', // review M8: Graph edges, less faint on white
  '--gv-edge-opacity-inferred': '0.5',
  '--series-0': '#274455', // review M1/R2: was --accent (the question blue), then violet; a slate navy of its own
  '--save-ok': '#23703d', // Browse: a censor's save message
  '--save-err': '#b02a22',
  '--tone-bad': '#b02a22', // Memory write health: a tripped rule's border and tint
  '--btn-ink': '#ffffff', // text on an accent-filled button
};

// PR 3 (the Execution screens): status (`--st-*`) and outcome (`--oc-*`) colours, and the screens' own tokens.
const ST: Record<string, string> = {
  '--st-pending': '#56687a',
  '--st-ready': '#1b6a9c',
  '--st-running': '#8a5a0e',
  '--st-awaiting-check': '#9a4d0a',
  '--st-awaiting-input': '#7b3fa0', // = --waiting
  '--st-completed': '#23703d',
  '--st-failed': '#b02a22',
  '--st-blocked': '#8a1c2c',
  '--st-cancelled': '#55616c', // fix round 0: #5f6b76 and #5c6b7d were 4.32:1 on a hovered row
  '--st-skipped': '#536170',
  '--st-partial': '#a14a08',
  '--st-success': '#23703d', // the ledger's success, Subtasks' completed
  '--st-ledger-blocked': '#8a5a0e', // the ledger's blocked, Subtasks' incomplete_blocked
  '--st-unknown': '#a3266f', // = --unknown
  '--st-neutral': '#55616c',
};
const OC: Record<string, string> = {
  '--oc-failed': '#b02a22', // Subtasks: incomplete_no_terminal, validation_failed
  '--oc-timeout': '#881c34', // re-review N1: #8a1c2c was 3.7 from errored under protanopia
  '--oc-errored': '#641400',
  '--oc-unknown': '#7a8794', // fix round 0: a lighter slate, not cancelled's grey
  '--oc-blocked': '#b7791f', // Subtasks' incomplete_blocked as a mark (review I3: the text amber merged with failed)
};
const PR3: Record<string, string> = {
  ...ST,
  ...OC,
  '--stack-bar': '#b7791f', // Workflows: a token budget's consumed part
  '--hit-good': '#23703d', // Cache: the hit-rate line and the doughnut's cache-read part
  '--hit-mid': '#8a5a0e',
  '--hit-bad': '#b02a22',
  '--cache-blue': '#1f5fa8', // Cache: the doughnut's cache-created part
  '--cache-grey': '#4f5f6f', // Cache: the doughnut's uncached part
  '--wait-ink': '#7b3fa0', // Workflows: the Now column's waiting pill
  '--wait-bg': '#efe3f6',
  '--wash': '#f4f7f8', // Heartbeat: the tuning report's panel
  '--hb-warn-ink': '#8a5a0e', // Heartbeat: amber as text (paper's --yellow is a mark colour)
  '--effect-write': '#1f5fa8', // the Ledger's write effect pill
  '--mc-section-1': '#8b5cf6', // a model call's first context section: a violet
  '--mc-section-7': '#7890d0', // Messages: a light blue, not a status colour
  '--mc-section-8': '#382880', // Tools: an indigo, apart from Decisions and the link blue
};

// PR 4 and PR 5 (the Decisions, Autonomy and System groups).
const PR4: Record<string, string> = {
  // A text field's boundary: 3:1 on the card and the page (WCAG 1.4.11); the prototype's rule-2 #c9d4da is about 1.5:1.
  '--field-border': '#7a8896',
  // Scoring's self-corrected signal mark: a soft mauve, 13.4 (OKLab) from every other signal colour under all three
  // visions; --series-3 (#a8408f) was 4.2 from praised's blue under protanopia.
  '--signal-self-corrected': '#b87ab3',
  // Review I2: the success mark of Decisions' outcomes, the Overview's doughnut and Scoring's completed signal: a green
  // 9.8 or more from partial, failure (--err-text) and the other signal marks under all three visions, where --green was
  // 4.6 from partial under protanopia and 5.9 from failure under deuteranopia. It sits close to --green on purpose (the
  // same meaning), so it is kept out of the categorical-against-status checks.
  '--oc-success': '#3f8772',
};

// The legacy memory-type colours (Retrieval's chips): one colour per memory type, the Graph's.
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

describe('the tokens of the one look', () => {
  it(':root and .rail are the only token blocks; nothing is scoped to a look or a ground', () => {
    expect(BLOCKS.has(ROOT_SEL)).toBe(true);
    expect(BLOCKS.has(RAIL_SEL)).toBe(true);
    expect(ROOT_SEL).toBe(':root');
    expect(RAIL_SEL).toBe('.rail');
    expect([...BLOCKS.keys()].filter((k) => k.startsWith('html[') || k.includes('[data-'))).toEqual([]);
  });

  it(':root is the paper block moved verbatim (PAPER_GOLDEN), plus the kept tokens that had no paper value', () => {
    const got = Object.fromEntries(tokens(PAPER).map((k) => [k, PAPER[k]]));
    expect(got).toEqual({ ...PAPER_GOLDEN, ...KEPT });
    expect(Object.keys(PAPER_GOLDEN)).toHaveLength(181);
  });

  it('every lane has its dot colour', () => {
    for (const lane of Object.keys(LANE_COLORS)) expect(PAPER[`--lane-${lane}`], lane).toMatch(/^#[0-9a-f]{6}$/);
  });

  it('every rail token is a :root token too (the rail re-declares a value, it adds no name) except its own nav ones', () => {
    const own = ['--nav-label-tracking', '--nav-icon-display', '--logo-display', '--nav-radius', '--nav-ink', '--nav-active-weight', '--brand-size'];
    expect(tokens(RAIL).filter((t) => !(t in PAPER)).sort()).toEqual([...own].sort());
    for (const t of own) expect(PAPER[t], `${t} has no :root default`).toBeUndefined();
  });

  it('no token is built from var() (a var() resolves where it is declared, so an inherited one would not follow .rail)', () => {
    expect(tokens(PAPER).filter((t) => PAPER[t].includes('var('))).toEqual([]);
    expect(tokens(RAIL).filter((t) => RAIL[t].includes('var('))).toEqual([]);
  });

  it(':root carries the contract values', () => {
    expect(PAPER).toMatchObject({
      '--bg': '#eef2f3',
      '--surface': '#ffffff',
      '--surface-hover': '#f4f7f8',
      '--border': '#e3e9ec',
      '--text': '#14202b',
      '--muted': '#56687a',
      '--accent': '#2346c8',
      '--accent-text': '#2346c8',
      '--focus-ring': '#2346c8',
      '--ok-text': '#23703d',
      '--ok-bg': '#e3f2e8',
      '--warn-text': '#8a5a0e',
      '--warn-bg': '#f8efdc',
      '--err-text': '#b02a22',
      '--err-bg': '#fbe9e8',
      '--green': '#2f8a4e',
      '--yellow': '#b7791f',
      '--red': '#d2352b',
      '--oc-success': '#3f8772',
      '--field-border': '#7a8896',
      '--radius': '18px',
      '--card-radius': '18px',
      '--card-shadow': '0 1px 2px rgba(18, 48, 79, 0.06), 0 6px 20px rgba(18, 48, 79, 0.07)',
      '--ctl-radius': '999px',
      '--font-ui': "'Archivo', system-ui, sans-serif",
      '--font-mono': "'Red Hat Mono', ui-monospace, monospace",
      '--series-0': '#274455',
      '--decision-color': '#802c66',
      '--gv-decision': '#802c66',
    });
    for (const f of FRAMES) expect(PAPER[`--f-${f}`], f).toBe(PAPER_FRAMES[f]);
    expect(PAPER['--f-other']).toBe('#14202b');
  });

  it('.rail carries the rail values and paints its own ground in the dark scheme', () => {
    expect(RAIL).toEqual({
      '--bg': '#12304f',
      '--surface': '#12304f',
      '--surface-hover': '#1a3d62',
      '--border': 'rgba(255, 255, 255, 0.12)',
      '--text': '#dbe6ef',
      '--muted': '#9fb6c9',
      '--accent': '#9fb6c9',
      '--accent-glow': '#eef2f3',
      '--accent-text': '#14202b',
      '--focus-ring': '#ffffff',
      '--waiting': '#c4b5fd',
      '--unknown': '#f9a8d4',
      '--nav-label-tracking': '0',
      '--nav-icon-display': 'none',
      '--logo-display': 'none',
      '--nav-radius': '10px',
      '--nav-ink': '#dbe6ef',
      '--nav-active-weight': '600',
      '--brand-size': '21px',
      'color-scheme': 'dark',
      background: 'var(--bg)',
      color: 'var(--text)',
      'font-family': 'var(--font-ui)',
      'font-variant-numeric': 'normal', // the rail's badges keep the proportional figures they had (pixel oracle)
    });
  });
});

describe('the document level', () => {
  it('html is the light scheme; body paints the ground and sets the text, the font and tabular figures, no gradient', () => {
    expect(BLOCKS.get('html')).toMatchObject({ 'color-scheme': 'light' });
    const body = block(BLOCKS, 'body', 5);
    expect(body).toMatchObject({
      background: 'var(--bg)',
      color: 'var(--text)',
      'font-family': 'var(--font-ui)',
      'font-variant-numeric': 'tabular-nums',
    });
    expect(body['background-image']).toBeUndefined();
    expect(CSS).not.toMatch(/radial-gradient/);
  });

  it('the page region needs no rule of its own (body carries the ground); only the page title is anchored to it', () => {
    expect(BLOCKS.has('.page-ground')).toBe(false);
    expect(BLOCKS.get('html:root .page-ground .page-h1')).toEqual({ 'font-size': '30px', 'font-weight': '650', 'letter-spacing': '-0.022em' });
    expect([...BLOCKS.keys()].filter((k) => k.includes('.page-ground'))).toEqual(['html:root .page-ground .page-h1']);
  });

  it('the fonts: Archivo and Red Hat Mono only, loaded and named in @theme (a bare <code> follows --font-mono)', () => {
    expect(CSS).toContain("@import url('https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900&family=Red+Hat+Mono:wght@400;500&display=swap');");
    expect(CSS).not.toMatch(/DM\+?\s?Sans|JetBrains/);
    expect(BLOCKS.get('@theme')).toMatchObject({
      '--font-ui': "'Archivo', system-ui, sans-serif",
      '--font-mono': "'Red Hat Mono', ui-monospace, monospace",
    });
    expect(CSS).not.toContain('--default-mono-font-family');
  });

  it('reduced motion turns every transition off', () => {
    expect(CSS).toMatch(/@media \(prefers-reduced-motion: reduce\) \{\s*\* \{\s*transition: none !important;/);
  });

  it('the shadcn utilities resolve through @theme inline, live where they are used', () => {
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
    // Not mapped: the drawer (Graph and Workflows' bottom sheet) carries these classes and paints itself.
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
    expect(contrast('#767676', '#ffffff')).toBeCloseTo(4.54, 1); // the lightest grey that passes AA on white
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

  it('the neutral for an unknown frame: readable on its 7% tint, and not conversation\'s grey', () => {
    expect(contrast(PAPER['--f-other'], tint(PAPER['--f-other'], 0.07, '#ffffff'))).toBeGreaterThanOrEqual(4.5);
    expect(PAPER['--f-other']).not.toBe(PAPER['--f-conversation']);
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

  it('the skip link: white on the link blue, at least 4.5 (it reads :root; no override of its own is left)', () => {
    expect([...BLOCKS.keys()].filter((k) => k.includes('skip-link'))).toEqual([]);
    const app = readFileSync(join(__dirname, 'App.svelte'), 'utf-8');
    expect(app).toMatch(/\.skip-link \{[^}]*background: var\(--accent\);[^}]*color: var\(--skip-link-text\);/);
    expect(contrast(PAPER['--skip-link-text'], PAPER['--accent'])).toBeGreaterThanOrEqual(4.5);
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

  it('every token has its value', () => {
    for (const [k, paper] of Object.entries(PR2)) expect(PAPER[k], k).toBe(paper);
    for (const [k, paper] of Object.entries(LEGACY_PAPER)) expect(PAPER[k], k).toBe(paper);
  });

  it('series 1 to 9 are at least 3:1 on the white card; any two of 1 to 8 are at least 7 apart', () => {
    const paper = Object.values(SERIES);
    for (const [i, c] of paper.slice(0, 9).entries()) expect(contrast(c, '#ffffff'), `series-${i + 1}`).toBeGreaterThanOrEqual(3);
    for (let i = 0; i < 8; i++) {
      for (let j = i + 1; j < 8; j++) {
        expect(okDistance(paper[i], paper[j]), `series-${i + 1} and series-${j + 1}`).toBeGreaterThanOrEqual(7);
      }
    }
  });

  it('series 10 to 13 are at least 3:1 and at least 7 apart from every other slot', () => {
    const paper = Object.values(SERIES);
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
    for (const [k, p] of Object.entries(SERIES)) expect(okDistance(zero, p), `--series-0 and ${k}`).toBeGreaterThanOrEqual(7);
    for (const f of [...FRAMES, 'other']) expect(okDistance(zero, PAPER[`--f-${f}`]), `--series-0 and --f-${f}`).toBeGreaterThanOrEqual(6);
  });

  it('the CVD helper reproduces the review\'s figures (the old olive chunk against procedure)', () => {
    expect(okDistance('#7a8a1a', '#d0620d', 'deutan')).toBeCloseTo(3.4, 1);
    expect(okDistance('#7a8a1a', '#d0620d', 'protan')).toBeCloseTo(5.5, 1);
    expect(okDistance('#123456', '#123456', 'protan')).toBe(0);
  });

  it('the five graph node colours: at least 3:1 on white, and at least 12 apart', () => {
    const nodes = Object.entries(GV_NODES);
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
    const nodes = Object.entries(GV_NODES).map(([k, p]) => [k.replace('--gv-', ''), p] as const);
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
      ...Object.entries({ ...SERIES, '--series-0': PR2['--series-0'], ...GV_NODES, '--gv-other': GV['--gv-other'], ...REL, ...DISP }).map(([k, p]) => [k, p] as [string, string]),
      ...Object.entries(LEGACY_PAPER).filter(([k]) => k !== '--pending'),
    ];
    for (const [k, c] of categorical) {
      for (const [s, sc] of status) expect(okDistance(c, sc), `${k} (${c}) and ${s} (${sc})`).toBeGreaterThanOrEqual(6);
    }
  });

  it('a memory type has one colour on every Memory screen on paper: the legacy type colours are the Graph\'s', () => {
    // Review M6.
    for (const t of ['fact', 'episode', 'decision', 'procedure', 'chunk']) expect(PAPER[`--${t}-color`], t).toBe(PAPER[`--gv-${t}`]);
  });

  it('every relation colour and legacy type colour is at least 3:1 on white', () => {
    for (const [k, p] of Object.entries(REL)) expect(contrast(p, '#ffffff'), k).toBeGreaterThanOrEqual(3);
    for (const [k, p] of Object.entries(LEGACY_PAPER)) expect(contrast(p, '#ffffff'), k).toBeGreaterThanOrEqual(3);
  });

  it('no categorical paper colour is a frame colour or a status fill or text (a hue carries one meaning)', () => {
    const taken = new Set([
      ...Object.keys(PAPER).filter((k) => k.startsWith('--f-')).map((k) => PAPER[k].toLowerCase()),
      '#2f8a4e', '#b7791f', '#d2352b', '#23703d', '#8a5a0e', '#b02a22',
    ]);
    expect(taken.size).toBeGreaterThanOrEqual(14);
    const categorical = { ...SERIES, ...GV_NODES, '--gv-other': GV['--gv-other'], ...REL, ...DISP };
    for (const [k, p] of Object.entries(categorical)) expect(taken.has(p.toLowerCase()), k).toBe(false);
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
    for (const [k, p] of Object.entries(DISP)) {
      if (k === '--disp-rendered') continue;
      expect(okDistance(rendered, p), k).toBeGreaterThanOrEqual(7);
      for (const cvd of ['protan', 'deutan'] as const) expect(okDistance(rendered, p, cvd), `${cvd}: ${k}`).toBeGreaterThanOrEqual(4);
    }
    expect(okDistance(rendered, PAPER['--f-task'])).toBeGreaterThanOrEqual(6);
  });

  it('every disposition fill takes the white count label at 4.5:1 or more', () => {
    for (const [k, p] of Object.entries(DISP)) expect(contrast(PAPER['--seg-ink'], p), k).toBeGreaterThanOrEqual(4.5);
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
  it('every token has its value', () => {
    for (const [k, paper] of Object.entries(PR3)) expect(PAPER[k], k).toBe(paper);
  });

  it('a status colour is readable on its own badge: at least 4.5:1 on its 12.5% tint, over the white card and a hovered row', () => {
    // Fix round 0, ruling 6: a table row's hover wash (--surface-hover) sits under the badge too.
    for (const bg of ['#ffffff', PAPER['--surface-hover']]) {
      for (const [k, p] of Object.entries(ST)) expect(contrast(p, tint(p, 0.125, bg)), `${k} on ${bg}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('one meaning each: awaiting input is the waiting colour and unknown the unknown colour', () => {
    expect(PAPER['--st-awaiting-input']).toBe(PAPER['--waiting']);
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
      '--st-success|--oc-timeout': 5.5, // deutan 5.95
      '--st-success|--st-neutral': 6.5, // deutan 6.9
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

  it('Subtasks\' cancelled and unknown outcomes are told apart (ruling 5): at least 10 apart', () => {
    // Cancelled is --st-neutral, unknown --oc-unknown; the doughnut and the trend draw them side by side.
    expect(okDistance(PAPER['--oc-unknown'], PAPER['--st-neutral'])).toBeGreaterThanOrEqual(10);
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
    // Re-review N2: no section reads as the link blue on the same card (the question frame's colour), under any vision.
    for (const s of sections) {
      for (const cvd of [undefined, 'protan', 'deutan'] as const) {
        expect(okDistance(PAPER[s], PAPER['--f-question'], cvd), `${s} and the link blue (${cvd ?? 'normal'})`).toBeGreaterThanOrEqual(6);
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
    for (const [k, p] of Object.entries(OC)) expect(contrast(p, '#ffffff'), k).toBeGreaterThanOrEqual(3);
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

  it('the waiting pill\'s text on its tint: at least 4.5:1', () => {
    expect(contrast(PAPER['--wait-ink'], PAPER['--wait-bg'])).toBeGreaterThanOrEqual(4.5);
  });
});

// ── PR 4 and PR 5: the Decisions, Autonomy and System groups ──────────────────────────────────────────────────────

describe('PR 4/5 tokens and the pairs the dense screens draw', () => {
  const page = PAPER['--bg'];
  const white = '#ffffff';
  const hover = PAPER['--surface-hover'];

  it('every token has its value', () => {
    for (const [k, paper] of Object.entries(PR4)) expect(PAPER[k], k).toBe(paper);
  });

  it('a text field\'s boundary is at least 3:1 on the white card and on the page (WCAG 1.4.11)', () => {
    expect(contrast(PAPER['--field-border'], white)).toBeGreaterThanOrEqual(3); // 3.63
    expect(contrast(PAPER['--field-border'], page)).toBeGreaterThanOrEqual(3); // 3.22
  });

  it('Identity\'s form: the focus ring 3:1 on the card and on the field\'s fill; the typed text and the hovered Deactivate 4.5:1', () => {
    expect(contrast(PAPER['--focus-ring'], white)).toBeGreaterThanOrEqual(3); // 7.55
    expect(contrast(PAPER['--focus-ring'], page)).toBeGreaterThanOrEqual(3); // 6.70
    expect(contrast(PAPER['--text'], page)).toBeGreaterThanOrEqual(4.5); // what is typed, on the field
    expect(contrast(PAPER['--err-text'], white)).toBeGreaterThanOrEqual(4.5); // Deactivate, hovered
  });

  it('Scoring\'s heatmap: ink on the strongest cell (65% of the fill over the card) is at least 4.5:1 on paper', () => {
    expect(contrast(PAPER['--text'], tint(PAPER['--green'], 0.65, white))).toBeGreaterThanOrEqual(4.5); // 6.78
    expect(contrast(PAPER['--text'], tint(PAPER['--red'], 0.65, white))).toBeGreaterThanOrEqual(4.5); // 5.78
  });

  it('Guards\' censor count: the accent on its 12% tint over the card is at least 4.5:1 on paper', () => {
    expect(contrast(PAPER['--accent'], tint(PAPER['--accent'], 0.12, white))).toBeGreaterThanOrEqual(4.5); // 6.22
  });

  it('status strips and pills: text on a tint of itself, over the card, a hovered row and the page, at least 4.5:1', () => {
    // Autonomy's scorer strip sits on the page; Guards' verdicts in a card; badges and pills in hoverable rows.
    for (const bg of [white, hover, page]) {
      expect(contrast(PAPER['--stat-ok'], tint(PAPER['--stat-ok'], 0.08, bg)), `ok on ${bg}`).toBeGreaterThanOrEqual(4.5);
      expect(contrast(PAPER['--stat-warn'], tint(PAPER['--stat-warn'], 0.08, bg)), `warn on ${bg}`).toBeGreaterThanOrEqual(4.5);
      expect(contrast(PAPER['--err-text'], tint(PAPER['--tone-bad'], 0.1, bg)), `bad on ${bg}`).toBeGreaterThanOrEqual(4.5);
    }
    // The partial badge takes PR 3's 12.5% badge tint: at 15% it is 4.49:1 on a hovered row.
    for (const bg of [white, hover]) {
      expect(contrast(PAPER['--st-partial'], tint(PAPER['--st-partial'], 0.125, bg)), `partial on ${bg}`).toBeGreaterThanOrEqual(4.5);
    }
    expect(contrast(PAPER['--st-partial'], tint(PAPER['--st-partial'], 0.15, hover))).toBeLessThan(4.5);
    // Border-only pills and coloured counts, on a hovered row.
    for (const t of ['--st-partial', '--stat-ok', '--stat-warn', '--err-text', '--muted', '--accent']) {
      expect(contrast(PAPER[t], hover), t).toBeGreaterThanOrEqual(4.5);
    }
    // The neutral badge (a pending or unknown status): muted text on the wash.
    expect(contrast(PAPER['--muted'], PAPER['--wash'])).toBeGreaterThanOrEqual(4.5);
  });

  // Marks one chart draws together keep 8 apart for normal vision and under protanopia and deuteranopia (PR 3 review I3).
  // Each screen task adds the sets its charts draw. An open pair is held at its present floor, named, with the cue that
  // tells its marks apart besides colour; it is listed in the PR 4 report for the owner.
  const MARK_SETS: Record<string, string[]> = {
    // Decisions' outcome by category and by stakes (stacked bars), and the Overview's outcome doughnut: one palette, so a
    // hue means the same outcome on both screens. Review I2: success --oc-success, failure --err-text.
    'decisions outcomes': ['--oc-success', '--yellow', '--err-text', '--series-7'],
    // Scoring's signal doughnut and stacked trend. Reworked is drawn in --err-text, which keeps it apart from corrected's
    // orange under deuteranopia (5.2 with --red); --series-7 is an unknown signal type's slice.
    'scoring signals': ['--oc-success', '--series-1', '--series-4', '--err-text', '--signal-self-corrected', '--series-7'],
    // Scoring's weight evolution: one line per dimension. Each line also has a dash of its own (Rubric.test.ts).
    'scoring dimensions': ['--series-0', '--series-2', '--series-1', '--series-4', '--series-5', '--series-3', '--series-6', '--series-8', '--series-9'],
    // Autonomy's "who was there" shares and their legend. (Its outcome bars each sit beside their label, so they are
    // never matched by colour.)
    'autonomy contexts': ['--series-1', '--series-2', '--series-4'],
    // Guards' flags per day: one line per rule, each with its own dash as well.
    'guards rules': ['--series-1', '--series-4', '--series-9'],
    // Observability's handler dots: no errors, some, more than 10%. The success mark and --err-text keep every pair 9.8
    // or more apart; --red was 5.1 from the amber, --st-completed 5.5 from red.
    'observability handler dots': ['--oc-success', '--yellow', '--err-text'],
  };
  const OPEN_PAIRS: Record<string, number> = {
    '--series-1|--series-3': 4, // protan 4.2 (Scoring's dimensions 3 and 6)
    // Scoring's dimensions: told apart by the dash as well as the hue.
    '--series-0|--series-8': 7, // 7.2 normal
    '--series-2|--series-6': 7.5, // deutan 7.6
    '--series-4|--series-5': 3, // deutan 3.4: orange and olive (the accepted paper series 5)
    '--series-3|--series-9': 3.5, // deutan 3.7
    '--series-1|--series-9': 7.5, // protan 7.95 (8.0 normal)
  };
  const openFloor = (a: string, b: string) => OPEN_PAIRS[`${a}|${b}`] ?? OPEN_PAIRS[`${b}|${a}`] ?? 8;

  it('marks one chart draws together keep 8 apart under normal vision, protanopia and deuteranopia, or a named floor', () => {
    for (const [chart, set] of Object.entries(MARK_SETS)) {
      for (let i = 0; i < set.length; i++) {
        expect(contrast(PAPER[set[i]], white), `${chart}: ${set[i]} on white`).toBeGreaterThanOrEqual(3);
        for (let j = i + 1; j < set.length; j++) {
          const floor = openFloor(set[i], set[j]);
          for (const cvd of [undefined, 'protan', 'deutan'] as const) {
            expect(okDistance(PAPER[set[i]], PAPER[set[j]], cvd), `${chart}: ${set[i]} and ${set[j]} (${cvd ?? 'normal'})`)
              .toBeGreaterThanOrEqual(floor);
          }
        }
      }
    }
  });

  it('the self-corrected mark is a colour of its own: 3:1 on white, 6 or more from every status and frame colour', () => {
    const c = PAPER['--signal-self-corrected'];
    expect(contrast(c, white)).toBeGreaterThanOrEqual(3);
    for (const t of ['--green', '--yellow', '--red', '--ok-text', '--warn-text', '--err-text', ...FRAMES.map((f) => `--f-${f}`)]) {
      expect(okDistance(c, PAPER[t]), t).toBeGreaterThanOrEqual(6);
    }
  });

  it('Observability\'s anomaly lines: the severity word and the line on their tint over the card, at least 4.5:1', () => {
    const yellowOn = (b: Record<string, string>, bg: string) => tint(b['--yellow'], 0.1, bg);
    expect(contrast(PAPER['--warn-text'], yellowOn(PAPER, white))).toBeGreaterThanOrEqual(4.5); // 5.30
    expect(contrast(PAPER['--text'], yellowOn(PAPER, white))).toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--err-text'], PAPER['--err-bg'])).toBeGreaterThanOrEqual(4.5); // 5.60
    expect(contrast(PAPER['--text'], PAPER['--err-bg'])).toBeGreaterThanOrEqual(4.5);
  });

  it('Scoring\'s radar bounds are full-strength lines: 3:1 or more on the card and over the weight polygon\'s 20% fill (review I1)', () => {
    // At 40% they were 1.87:1 and 1.68:1 on white. Min and Max also differ by dash kind (Rubric.test.ts).
    const polygon = tint(PAPER['--series-0'], 0.2, white);
    for (const t of ['--red', '--green']) {
      expect(contrast(PAPER[t], white), `${t} on white`).toBeGreaterThanOrEqual(3);
      expect(contrast(PAPER[t], polygon), `${t} over the polygon`).toBeGreaterThanOrEqual(3);
    }
  });

  it('every named open pair is drawn together by some chart and is really below 8 (no stale exemption)', () => {
    for (const key of Object.keys(OPEN_PAIRS)) {
      const [a, b] = key.split('|');
      expect(Object.values(MARK_SETS).some((s) => s.includes(a) && s.includes(b)), key).toBe(true);
      const worst = Math.min(...([undefined, 'protan', 'deutan'] as const).map((cvd) => okDistance(PAPER[a], PAPER[b], cvd)));
      expect(worst, key).toBeLessThan(8);
    }
  });

  it('Observability\'s event count and the perfect-calibration line', () => {
    // The event count (review M2): --muted on its own 10% tint, over the card and over a hovered or open row (5.02 /
    // 4.68). The wash it had equals the hovered row's fill.
    expect(contrast(PAPER['--muted'], tint(PAPER['--muted'], 0.1, white)), 'count badge on the card').toBeGreaterThanOrEqual(4.5);
    expect(contrast(PAPER['--muted'], tint(PAPER['--muted'], 0.1, hover)), 'count badge on a hovered row').toBeGreaterThanOrEqual(4.5);
    expect(PAPER['--wash']).toBe(PAPER['--surface-hover']); // why the wash could not be the badge's fill
    // D3, the perfect-calibration line: --muted at 40%. A reference line, not data: recorded.
    const at40 = (c: string, bg: string) => over([...parseColor(c).slice(0, 3), 0.4] as RGBA, parseColor(bg));
    expect(contrast(at40(PAPER['--muted'], white), white)).toBeCloseTo(1.79, 1);
  });
});
