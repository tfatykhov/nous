/// <reference types="node" />
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { PAPER_SEL, RAIL_SEL, parseBlocks } from '../lib/testing/css';
import { parseThemes } from './testing/themes';

// The companion keeps its own stylesheet (it never imports app.css), so the paper values it shares with the
// dashboard are copies. This test is the sync: it reads the dashboard's paper ground from app.css and fails when a
// shared value drifts, and it fails on any companion :root token that is neither shared nor listed as its own.

const BLOCKS = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8'));
const DASH = BLOCKS.get(PAPER_SEL) ?? {};
const RAIL = BLOCKS.get(RAIL_SEL) ?? {};
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
  // of single marks). Dashboard slot 4 (orange) is not used: it sits next to warn.
  '--series-1': '--series-0',
  '--series-2': '--series-1',
  '--series-3': '--series-2',
  '--series-4': '--series-3',
  '--chart-axis': '--chart-text',
  '--chart-grid': '--chart-grid',
  // shape
  '--radius': '--card-radius',
  '--radius-md': '--card-radius-sm',
  '--radius-sm': '--radius-sm',
  '--ctl-radius': '--ctl-radius',
  '--card-shadow': '--card-shadow',
  '--card-border': '--card-border',
  '--label-transform': '--label-transform',
  '--label-tracking': '--label-tracking',
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
  '--font-numeric': 'an alias of the mono face for figures (F096)',
  '--nested-bg': 'a card inside a surface card: a soft tile instead of a shadow on a shadow',
  '--ctl-bg': "a default button's fill (the prototype .btn: white)",
  '--ctl-border': "a default button's outline (the prototype rule-2)",
  '--field-bg': "a text field's fill",
  '--field-border': "a text field's boundary; the dashboard's --field-border (#7a8896) is in PR 4, not on main yet",
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
};

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
