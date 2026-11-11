/// <reference types="node" />
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { RAIL_SEL, ROOT_SEL, block, parseBlocks } from '../lib/testing/css';
import { parseThemes } from './testing/themes';

// The companion keeps its own stylesheet (it never imports app.css), so the paper values it shares with the
// dashboard are copies. This test is the sync: it reads the dashboard's paper ground from app.css and fails when a
// shared value drifts, and it fails on any companion :root token that is neither shared nor listed as its own.

type Blocks = Map<string, Record<string, string>>;

/** The dashboard's paper values: since PR 7 its one look, declared at `:root`. `block` throws when the block is
 * missing or thin, so a renamed block fails loudly instead of comparing against `{}`. */
function paperBlock(blocks: Blocks): Record<string, string> {
  return block(blocks, ROOT_SEL, 50);
}
/** The dashboard's rail ground: the Atlantic-blue sidebar's own block, `.rail`. */
function railBlock(blocks: Blocks): Record<string, string> {
  return block(blocks, RAIL_SEL, 10);
}

const BLOCKS = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8'));
const DASH = paperBlock(BLOCKS);
const RAIL = railBlock(BLOCKS);
const ROOT = parseThemes(readFileSync(join(__dirname, 'companion.css'), 'utf-8')).resolved('nous-default');

/** companion :root token -> the dashboard paper token it must equal. */
const PARITY: Record<string, string> = {
  '--bg': '--bg',
  '--surface': '--surface',
  '--surface-hover': '--surface-hover',
  '--border': '--border',
  '--text': '--text',
  '--muted': '--muted',
  '--green': '--green',
  '--red': '--red',
  '--yellow': '--yellow',
  '--transition': '--transition',
  '--font-ui': '--font-ui',
  '--font-mono': '--font-mono',
  '--on-accent': '--on-accent',
  // status words are text, so the companion's --ok/--warn/--crit are the dashboard's text tones
  '--ok': '--ok-text',
  '--warn': '--warn-text',
  '--crit': '--err-text',
  // MemoryGraph's node types are the dashboard Graph's
  '--node-fact': '--gv-fact',
  '--node-episode': '--gv-episode',
  '--node-procedure': '--gv-procedure',
  '--node-chunk': '--gv-chunk',
  '--node-decision': '--gv-decision',
  // The series ramp, shifted one slot: the companion's first-drawn series is the dashboard's slot 0 (the slate navy
  // of single marks). The fourth is the dashboard's violet slot 10 (review I2): slot 3 (magenta) is 4.2 from slot 1
  // under protanopia, slot 4 (orange) sits next to warn, and the olive slot 5 merges with the amber status mark.
  '--series-1': '--series-0',
  '--series-2': '--series-1',
  '--series-3': '--series-2',
  '--series-4': '--series-10',
  // status tones drawn as marks (a chart line, a meter band): the dashboard's outcome marks
  '--crit-mark': '--err-text',
  '--chart-axis': '--chart-text',
  '--chart-grid': '--chart-grid',
  // shape
  '--radius': '--card-radius',
  '--radius-md': '--card-radius-sm',
  '--radius-sm': '--radius-sm',
  '--ctl-radius': '--ctl-radius',
  '--card-shadow': '--card-shadow',
  '--card-border': '--card-border',
  '--field-border': '--field-border',
  '--tab-active-bg': '--tab-active-bg',
  '--tab-active-shadow': '--tab-active-shadow',
  // the seven frames
  '--f-conversation': '--f-conversation',
  '--f-question': '--f-question',
  '--f-task': '--f-task',
  '--f-decision': '--f-decision',
  '--f-debug': '--f-debug',
  '--f-creative': '--f-creative',
  '--f-initiation': '--f-initiation',
};

/** The companion's bar (chrome tokens) -> the dashboard's rail ground: the same Atlantic blue. */
const RAIL_PARITY: Record<string, string> = {
  '--rail': '--surface',
  '--rail-ink': '--text',
  '--rail-mute': '--muted',
  '--rail-focus': '--focus-ring',
};

/** companion :root tokens with no dashboard counterpart, each with the reason. */
const COMPANION_OWN: Record<string, string> = {
  '--accent': 'the rail navy is the companion accent (primary button, selected row, active tab); the dashboard accent is its link blue',
  '--accent-dim': 'the hover of a filled navy button (the prototype rail-2)',
  '--accent-glow': "the selected option row's wash (the prototype's checked .opt)",
  '--select-border': "the selected option row's outline",
  '--locked': 'a companion meaning; the task-frame teal',
  '--soft': 'the neutral pill and delta ink (#4f5f6f, a step darker than --muted so the pill reads 4.5 on its tint over a nested tile); the dashboard has no such token',
  '--scrim': 'the modal backdrop and the BarChart negative hatching',
  '--font-display': 'an alias of the UI face that themes may swap for a serif',
  '--font-numeric': 'the figure face (F096): the UI face on paper, set condensed with tabular figures; the mono face in a legacy theme',
  '--ok-mark': "the ok tone as a mark: #21694b, not the dashboard's #3f8772 (4.2 from the companion's teal series under deuteranopia); 3.5 from the ok text tone, so not a second visible green",
  '--chip-selected-bg': "a selected ChoicePicker chip's fill: navy on paper (as the shell's active switcher chip), the accent wash in a legacy theme",
  '--chip-selected-ink': "a selected chip's word on that fill",
  '--chip-focus': "a focused chip's own ring on paper (a navy border cannot show on a navy chip); none in a legacy theme",
  '--side-rule': 'a coloured side rule on a card or list item: none on paper (the craft floor), 3px in a legacy theme',
  '--side-rule-pad': "a model-supplied section's inset beside that rule",
  '--caps': 'tracked capitals on a badge: off on paper (a sentence-case pill), on in a legacy theme',
  '--callout-bg': "the ApprovalPanel's fill: none on paper (it sits on the card, as the prototype's approval), the old tile in a legacy theme",
  '--callout-pad': "the ApprovalPanel's padding",
  '--tl-flag-rule': "a flagged timeline entry's rail: the plain rail on paper (its ringed dot and label carry the flag), warn in a legacy theme",
  '--tap-min': "a control's minimum height: 44px on paper (a phone's tap target), auto (the initial value) in a legacy theme",
  '--tap-pad': "a CheckBox row's padding above and below: 11px on paper, so a one-line row is a 44px target with its label " +
    'centred in it; 0 (as before) in a legacy theme',
  '--settled-opacity': "a settled DecisionCard's opacity: 1 on paper (dimmed content fell to 2.6:1), 0.65 in a legacy theme",
  '--dim-opacity': "a Section's opacity while its data refreshes: 1 on paper (the header says it is working), 0.72 in a legacy theme",
  '--settled-ink': "a settled decision's title: muted on paper, the text colour in a legacy theme",
  '--field-focus': "a focused text field's ring: 2px navy on paper (the border change alone was 3.7:1 on a 1px line), none in a legacy theme",
  '--video-bg': "a video frame's fill before its first frame: the prototype's navy on paper (WebKit paints no frame without a poster), the soft tile in a legacy theme",
  '--stat-min': "a StatRow tile's minimum width: 96px on paper, so three evidence tiles fit a phone as in the prototype; 120px in a legacy theme",
  '--section-ink': "a section heading's colour: ink on paper (the prototype's headings), muted in a legacy theme",
  '--warn-mark': "the amber mark #b4771e: the dashboard's #b7791f is 2.97:1 on the meter and bar track; this is 0.8 from it",
  '--figure-stretch': "the figures' width: 82% on paper (Archivo's width axis, as the dashboard's figures), 100% in a legacy theme",
  '--nested-bg': 'a card inside a surface card: a soft tile instead of a shadow on a shadow',
  '--ctl-bg': "a default button's fill (the prototype .btn: white)",
  '--ctl-border': "a default button's outline (the prototype rule-2)",
  '--field-bg': "a text field's fill",
  '--label-transform': 'sentence-case labels on paper; a legacy theme keeps capitals (the dashboard has had no such token since PR 7)',
  '--label-tracking': 'no tracking on paper; a legacy theme keeps 0.05em (the dashboard has had no such token since PR 7)',
  '--option-pad': 'a ChoicePicker option card: its padding',
  '--option-border': 'a ChoicePicker option card: its outline',
  '--option-selected-bg': 'a ChoicePicker option card: the chosen wash',
  '--picker-pad': 'a ChoicePicker frame: none on paper (the options are the cards)',
  '--tablist-bg': 'the tab track (the prototype .tabs)',
  '--tablist-rule': 'the legacy underline rule under the tabs; none on paper',
  '--tablist-pad': 'the tab track inset',
  '--tablist-radius': 'the tab track and segment rounding; square in a legacy theme',
  '--tab-underline': "the legacy active tab's underline; none on paper",
  '--tile-rule-width': "a stat tile's tone rule: none on paper",
  '--badge-tint': "a coloured badge's tint on paper (the dashboard's badges are tinted pills too)",
  '--badge-edge': "a coloured badge's outline: none on paper",
  '--tl-dot': "a timeline entry's ringed dot (the prototype .tl)",
  '--tl-dot-ring': "the dot's ring: the navy accent",
  '--meter-track': "the confidence meter's track",
  '--graph-edge': 'graph edges: the axis grey, as the dashboard Graph draws them',
  '--graph-edge-opacity': "graph edges' opacity",
  '--line-width': 'chart line width',
  '--line-cap': 'chart line ends',
  '--line-join': 'chart line corners',
  '--spark-ring': "the sparkline end dot's ring",
};

describe('the parity reader (review M7)', () => {
  // PR 7 left the dashboard one look: its tokens at `:root`, the rail's at `.rail`. The reader reads exactly those two
  // blocks and fails loudly when one is missing or thin, never comparing against an empty block.
  const decls = (n: number) => Array.from({ length: n }, (_, i) => `--t${i}: ${i};`).join(' ');
  it('reads :root and .rail', () => {
    const blocks = parseBlocks(`:root { --bg: #eef2f3; ${decls(60)} } .rail { --surface: #12304f; ${decls(12)} }`);
    expect(paperBlock(blocks)['--bg']).toBe('#eef2f3');
    expect(railBlock(blocks)['--surface']).toBe('#12304f');
  });

  it('a missing or tiny block throws, and a region class is not read as the paper block', () => {
    expect(() => paperBlock(parseBlocks(':root { --bg: #000; }'))).toThrow();
    expect(() => railBlock(parseBlocks(''))).toThrow();
    expect(() => paperBlock(parseBlocks(`.page-ground { --bg: #eef2f3; ${decls(60)} }`))).toThrow();
  });
});

describe('the companion paper default and the dashboard paper ground share their values', () => {
  it('reads the dashboard paper ground', () => {
    expect(DASH['--bg']).toBe('#eef2f3');
  });

  it.each(Object.entries(PARITY))('%s equals the dashboard %s', (mine, theirs) => {
    expect(DASH[theirs], theirs).toBeDefined();
    expect(ROOT[mine], mine).toBe(DASH[theirs]);
  });

  it.each(Object.entries(RAIL_PARITY))('the bar: %s equals the dashboard rail %s', (mine, theirs) => {
    expect(RAIL[theirs], theirs).toBeDefined();
    expect(ROOT[mine], mine).toBe(RAIL[theirs]);
  });

  it('every companion :root token is shared or listed as the companion own, with a reason', () => {
    const unlisted = Object.keys(ROOT).filter((k) => !(k in PARITY) && !(k in RAIL_PARITY) && !(k in COMPANION_OWN));
    expect(unlisted).toEqual([]);
    for (const [k, why] of Object.entries(COMPANION_OWN)) {
      expect(ROOT[k], k).toBeDefined();
      expect(why.length).toBeGreaterThan(10);
    }
  });
});
