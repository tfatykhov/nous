import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { PAPER_ROUTES } from './lib/look';
import { DARK_SEL, parseBlocks, scriptBlocks, styleBlocks } from './lib/testing/css';

// Redesign PR 4 and PR 5: the dark look of the Decisions, Autonomy and System screens does not move. Every declaration a
// task converts is a row here: the file, the rule, the property, the value it now reads, and the literal it had. The
// value, resolved through app.css's dark block (var() chains, and color-mix() with transparent, which is the colour at
// that alpha), must equal the old literal. The deliberate changes (the plan's D rows) are listed apart, each with the
// value it moves to; their contrast is measured in tokens.test.ts.

const SRC = __dirname;
const DARK = parseBlocks(readFileSync(join(SRC, 'app.css'), 'utf-8')).get(DARK_SEL) ?? {};

/** A rule's value with every var() replaced by its dark value (a fallback only when the token is not defined). */
function resolveDark(value: string, depth = 0): string {
  if (depth > 10) throw new Error(`var() cycle in ${value}`);
  const out = value.replace(/var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)/g, (_m, name: string, fallback?: string) => {
    const v = DARK[name] ?? fallback?.trim();
    if (v == null) throw new Error(`${name} is not in the dark block`);
    return v;
  });
  return out.includes('var(') ? resolveDark(out, depth + 1) : out;
}

type RGBA = [number, number, number, number];
function parseColour(c: string): RGBA | null {
  const hex = c.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (hex) {
    const h = hex[1].length === 3 ? [...hex[1]].map((x) => x + x).join('') : hex[1];
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), 1];
  }
  const rgb = c.match(/^rgba?\(([^)]+)\)$/);
  if (rgb) {
    const [r, g, b, a = '1'] = rgb[1].split(',').map((x) => x.trim());
    return [Number(r), Number(g), Number(b), Number(a)];
  }
  return null;
}
const rgba = ([r, g, b, a]: RGBA) => `rgba(${r}, ${g}, ${b}, ${Math.round(a * 1000) / 1000})`;

/** Every colour in a value as rgba(); `color-mix(in srgb, C P%, transparent)` is C at P% of its alpha. */
function canonical(value: string): string {
  const mixed = value.replace(/color-mix\(in srgb,\s*(#[0-9a-f]{3,6}|rgba?\([^)]*\))\s+([\d.]+)%,\s*transparent\)/gi, (_m, c: string, p: string) => {
    const col = parseColour(c);
    if (!col) throw new Error(`not a colour: ${c}`);
    return rgba([col[0], col[1], col[2], col[3] * (Number(p) / 100)]);
  });
  if (mixed.includes('color-mix(')) throw new Error(`a color-mix() this test cannot resolve: ${value}`);
  return mixed
    .replace(/#[0-9a-f]{6}\b|#[0-9a-f]{3}\b|rgba?\([^)]*\)/gi, (c) => rgba(parseColour(c)!))
    .replace(/\s+/g, ' ')
    .trim();
}

/** [file, selector, property, the value it reads now, the literal it had]. Each screen task appends its rows. */
type Row = [file: string, selector: string, property: string, value: string, old: string];
const ROWS: Row[] = [
  // Decisions
  ['views/Decisions.svelte', '.stat-card', 'border', 'var(--card-border)', '1px solid #1e1e2e'],
  ['views/Decisions.svelte', '.stat-card', 'border-radius', 'var(--card-radius-sm)', '8px'],
  ['views/Decisions.svelte', '.stat-label', 'text-transform', 'var(--label-transform)', 'uppercase'],
  ['views/Decisions.svelte', '.stat-label', 'letter-spacing', 'var(--label-tracking)', '0.05em'],
  ['views/Decisions.svelte', '.indicator.good', 'background', 'var(--green)', '#34d399'],
  ['views/Decisions.svelte', '.indicator.warn', 'background', 'var(--yellow)', '#fbbf24'],
  ['views/Decisions.svelte', '.indicator.bad', 'background', 'var(--red)', '#f87171'],
  ['views/Decisions.svelte', '.chart-card', 'border', 'var(--card-border)', '1px solid #1e1e2e'],
  ['views/Decisions.svelte', '.status-msg.error', 'color', 'var(--err-text)', '#f87171'],
  // Scoring
  ['views/Rubric.svelte', '.status-msg.error', 'color', 'var(--err-text)', '#f87171'],
  ['views/Rubric.svelte', '.retry-link', 'color', 'var(--accent)', '#7c6af7'],
  ['views/Rubric.svelte', '.empty-state', 'border', 'var(--card-border)', '1px solid #1e1e2e'],
  ['views/Rubric.svelte', '.empty-state code', 'background', 'var(--track-strong)', 'rgba(255, 255, 255, 0.08)'],
  ['views/Rubric.svelte', '.config-banner--active', 'background', 'color-mix(in srgb, var(--green) 6%, transparent)', 'rgba(52, 211, 153, 0.06)'],
  ['views/Rubric.svelte', '.config-banner--active', 'border-color', 'color-mix(in srgb, var(--green) 30%, transparent)', 'rgba(52, 211, 153, 0.3)'],
  ['views/Rubric.svelte', '.config-banner--off', 'background', 'var(--wash)', 'rgba(255, 255, 255, 0.03)'],
  ['views/Rubric.svelte', '.dot-active', 'background', 'var(--green)', '#34d399'],
  ['views/Rubric.svelte', '.dot-off', 'background', 'var(--st-neutral)', '#6b7280'],
  ['views/Rubric.svelte', '.chart-card', 'border', 'var(--card-border)', '1px solid #1e1e2e'],
  ['views/Rubric.svelte', '.dim-card', 'background', 'var(--bg)', '#0a0a0f'],
  ['views/Rubric.svelte', '.dim-card', 'border', 'var(--card-border)', '1px solid #1e1e2e'],
  ['views/Rubric.svelte', '.dim-card', 'border-radius', 'var(--card-radius-sm)', '8px'],
  ['views/Rubric.svelte', '.corr-table th', 'background', 'var(--bg)', '#0a0a0f'],
  ['views/Rubric.svelte', '.data-table th', 'text-transform', 'var(--label-transform)', 'uppercase'],
  ['views/Rubric.svelte', '.badge-success', 'background', 'var(--badge-good-bg)', 'rgba(52, 211, 153, 0.15)'],
  ['views/Rubric.svelte', '.badge-success', 'color', 'var(--ok-text)', '#34d399'],
  ['views/Rubric.svelte', '.badge-failure', 'background', 'var(--badge-bad-bg)', 'rgba(248, 113, 113, 0.15)'],
  ['views/Rubric.svelte', '.badge-failure', 'color', 'var(--err-text)', '#f87171'],
  ['views/Rubric.svelte', '.badge-partial', 'color', 'var(--st-partial)', '#fb923c'],
  // Autonomy
  ['views/Autonomy.svelte', '.verdict-warn', 'color', 'var(--stat-warn)', '#f59e0b'],
  ['views/Autonomy.svelte', '.verdict-warn', 'border-color', 'color-mix(in srgb, var(--stat-warn) 35%, transparent)', 'rgba(245, 158, 11, 0.35)'],
  ['views/Autonomy.svelte', '.verdict-warn', 'background', 'color-mix(in srgb, var(--stat-warn) 8%, transparent)', 'rgba(245, 158, 11, 0.08)'],
  ['views/Autonomy.svelte', '.verdict-ok', 'color', 'var(--stat-ok)', '#10b981'],
  ['views/Autonomy.svelte', '.verdict-ok', 'border-color', 'color-mix(in srgb, var(--stat-ok) 35%, transparent)', 'rgba(16, 185, 129, 0.35)'],
  ['views/Autonomy.svelte', '.verdict-ok', 'background', 'color-mix(in srgb, var(--stat-ok) 8%, transparent)', 'rgba(16, 185, 129, 0.08)'],
  ['views/Autonomy.svelte', '.verdict-bad', 'color', 'var(--err-text)', '#f87171'],
  ['views/Autonomy.svelte', '.verdict-bad', 'border-color', 'color-mix(in srgb, var(--tone-bad) 50%, transparent)', 'rgba(239, 68, 68, 0.5)'],
  ['views/Autonomy.svelte', '.verdict-bad', 'background', 'color-mix(in srgb, var(--tone-bad) 10%, transparent)', 'rgba(239, 68, 68, 0.1)'],
  ['views/Autonomy.svelte', '.strip-meta dt, .tiles dt, .triple dt, .detail dt, .label', 'text-transform', 'var(--label-transform)', 'uppercase'],
  ['views/Autonomy.svelte', '.card, .panel', 'border', 'var(--card-border)', '1px solid #1e1e2e'],
  ['views/Autonomy.svelte', '.guards td.acted', 'color', 'var(--st-partial)', '#fb923c'],
  ['views/Autonomy.svelte', '.pill-warn', 'color', 'var(--st-partial)', '#fb923c'],
  ['views/Autonomy.svelte', '.pill-warn', 'border-color', 'color-mix(in srgb, var(--st-partial) 50%, transparent)', 'rgba(251, 146, 60, 0.5)'],
  ['views/Autonomy.svelte', '.fix-ok', 'color', 'var(--stat-ok)', '#10b981'],
  ['views/Autonomy.svelte', '.fix-ok', 'border-color', 'color-mix(in srgb, var(--stat-ok) 50%, transparent)', 'rgba(16, 185, 129, 0.5)'],
  ['views/Autonomy.svelte', '.fix-warn', 'color', 'var(--st-partial)', '#fb923c'],
  ['views/Autonomy.svelte', '.fix-warn', 'border-color', 'color-mix(in srgb, var(--st-partial) 50%, transparent)', 'rgba(251, 146, 60, 0.5)'],
  ['views/Autonomy.svelte', '.state-msg.error', 'color', 'var(--err-text)', '#f87171'],
  // Intentions
  ['views/Intentions.svelte', '.notice', 'background', 'var(--notice-bg)', 'rgba(245, 158, 11, 0.08)'],
  ['views/Intentions.svelte', '.notice', 'border', '1px solid var(--notice-border)', '1px solid rgba(245, 158, 11, 0.4)'],
  ['views/Intentions.svelte', '.notice', 'border-radius', 'var(--card-radius-sm)', '8px'],
  ['views/Intentions.svelte', '.panel', 'border', 'var(--card-border)', '1px solid #1e1e2e'],
  ['views/Intentions.svelte', '.tiles div', 'border', 'var(--card-border)', '1px solid #1e1e2e'],
  ['views/Intentions.svelte', '.tiles div', 'border-radius', 'var(--card-radius-sm)', '8px'],
  ['views/Intentions.svelte', '.tiles dt', 'text-transform', 'var(--label-transform)', 'uppercase'],
  // Guards
  ['views/Harness.svelte', '.notice', 'background', 'var(--notice-bg)', 'rgba(245, 158, 11, 0.08)'],
  ['views/Harness.svelte', '.notice', 'border', '1px solid var(--notice-border)', '1px solid rgba(245, 158, 11, 0.4)'],
  ['views/Harness.svelte', '.notice', 'border-radius', 'var(--card-radius-sm)', '8px'],
  ['views/Harness.svelte', '.card, .panel', 'border', 'var(--card-border)', '1px solid #1e1e2e'],
  ['views/Harness.svelte', '.mode-enforce', 'color', 'var(--stat-ok)', '#10b981'],
  ['views/Harness.svelte', '.mode-warn, .mode-shadow', 'color', 'var(--stat-warn)', '#f59e0b'],
  ['views/Harness.svelte', '.label', 'text-transform', 'var(--label-transform)', 'uppercase'],
  ['views/Harness.svelte', '.detail dt', 'text-transform', 'var(--label-transform)', 'uppercase'],
  ['views/Harness.svelte', '.verdict', 'border-radius', 'var(--card-radius-sm)', '8px'],
  ['views/Harness.svelte', '.verdict-warn', 'color', 'var(--stat-warn)', '#f59e0b'],
  ['views/Harness.svelte', '.verdict-warn', 'border-color', 'color-mix(in srgb, var(--stat-warn) 35%, transparent)', 'rgba(245, 158, 11, 0.35)'],
  ['views/Harness.svelte', '.verdict-warn', 'background', 'color-mix(in srgb, var(--stat-warn) 8%, transparent)', 'rgba(245, 158, 11, 0.08)'],
  ['views/Harness.svelte', '.verdict-ok', 'color', 'var(--stat-ok)', '#10b981'],
  ['views/Harness.svelte', '.verdict-ok', 'border-color', 'color-mix(in srgb, var(--stat-ok) 35%, transparent)', 'rgba(16, 185, 129, 0.35)'],
  ['views/Harness.svelte', '.verdict-ok', 'background', 'color-mix(in srgb, var(--stat-ok) 8%, transparent)', 'rgba(16, 185, 129, 0.08)'],
  ['views/Harness.svelte', '.empty.error', 'color', 'var(--err-text)', '#f87171'],
  ['views/Harness.svelte', '.state-msg.error', 'color', 'var(--err-text)', '#f87171'],
  ['views/Harness.svelte', '.badge', 'color', 'var(--accent)', '#7c6af7'],
  ['views/Harness.svelte', '.badge', 'background', 'color-mix(in srgb, var(--accent) 12%, transparent)', 'rgba(124, 106, 247, 0.12)'],
  // Observability (the handler dots and the trend chart: Observability.look.test.ts)
  ['views/Observability.svelte', '.chart-card', 'border', 'var(--card-border)', '1px solid #1e1e2e'],
  ['views/Observability.svelte', '.handler-rate--warn', 'color', 'var(--err-text)', '#f87171'],
  ['views/Observability.svelte', '.trace-row:hover, .trace-row--expanded', 'background', 'var(--surface-hover)', '#181824'],
  // The global :focus-visible ring the row had (2px solid var(--accent)), now its own rule on --focus-ring.
  ['views/Observability.svelte', '.trace-row:focus-visible', 'outline', '2px solid var(--focus-ring)', '2px solid #7c6af7'],
  ['views/Observability.svelte', '.trace-badge--mod', 'color', 'var(--st-partial)', '#fb923c'],
  ['views/Observability.svelte', '.mod-type', 'background', 'var(--badge-good-bg)', 'rgba(52, 211, 153, 0.15)'],
  ['views/Observability.svelte', '.mod-type', 'color', 'var(--ok-text)', '#34d399'],
  ['views/Observability.svelte', '.anomaly', 'background', 'color-mix(in srgb, var(--yellow) 10%, transparent)', 'rgba(251, 191, 36, 0.1)'],
  ['views/Observability.svelte', '.anomaly', 'border-left', '3px solid var(--yellow)', '3px solid #fbbf24'],
  ['views/Observability.svelte', '.anomaly--alert', 'background', 'var(--err-bg)', 'rgba(248, 113, 113, 0.1)'],
  ['views/Observability.svelte', '.anomaly--alert', 'border-left-color', 'var(--red)', '#f87171'],
  ['views/Observability.svelte', '.event-badge', 'color', 'var(--accent)', '#7c6af7'],
  ['views/Observability.svelte', '.event-badge', 'background', 'color-mix(in srgb, var(--accent) 12%, transparent)', 'rgba(124, 106, 247, 0.12)'],
  ['views/Observability.svelte', '.event-badge', 'text-transform', 'var(--label-transform)', 'uppercase'],
  ['views/Observability.svelte', '.event-badge', 'letter-spacing', 'var(--label-tracking)', '0.05em'],
  ['views/Observability.svelte', '.empty.error', 'color', 'var(--err-text)', '#f87171'], // was var(--red, #ef4444): --red is #f87171
  ['views/Observability.svelte', '.status-msg.error', 'color', 'var(--err-text)', '#f87171'],
  // Identity (the buttons are the shared outline pill; Identity.test.ts)
  ['views/Identity.svelte', '.section-card', 'border', 'var(--card-border)', '1px solid #1e1e2e'],
  ['views/Identity.svelte', '.section-card', 'border-radius', 'var(--card-radius-sm)', '8px'], // was var(--radius-sm, 6px): 8px
  ['views/Identity.svelte', '.section-label', 'text-transform', 'var(--label-transform)', 'uppercase'],
  ['views/Identity.svelte', '.section-label', 'letter-spacing', 'var(--label-tracking)', '0.05em'],
  ['views/Identity.svelte', 'textarea', 'background', 'var(--bg)', '#0a0a0f'], // was var(--bg, var(--surface))
  ['views/Identity.svelte', 'textarea', 'border', '1px solid var(--field-border)', '1px solid #1e1e2e'],
  ['views/Identity.svelte', 'textarea', 'border-radius', 'var(--radius-sm)', '8px'],
  ['views/Identity.svelte', '.save-status.save-ok', 'color', 'var(--save-ok)', '#4ade80'],
  ['views/Identity.svelte', '.save-status.save-err', 'color', 'var(--save-err)', '#f87171'],
  ['views/Identity.svelte', '.status-msg.error', 'color', 'var(--err-text)', '#f87171'], // was var(--red, #ef4444): --red is #f87171
  ['views/Identity.svelte', '.info-highlight', 'border', 'var(--card-border)', '1px solid #1e1e2e'],
  ['views/Identity.svelte', '.info-highlight', 'border-radius', 'var(--card-radius-sm)', '8px'],
  ['views/Identity.svelte', '.fact-edit', 'border-top', '1px solid var(--border)', '1px solid #1e1e2e'],
];

/** The plan's listed dark changes: [file, selector, property, value now, old literal, the dark value it resolves to]. */
type Change = [file: string, selector: string, property: string, value: string, old: string, now: string, why: string];
const CHANGES: Change[] = [
  ['views/Decisions.svelte', '.chart-card', 'border-radius', 'var(--card-radius)', '10px', '12px', 'D2: a chart card takes the card radius'],
  ['views/Rubric.svelte', '.chart-card', 'border-radius', 'var(--card-radius)', '8px', '12px', 'D2: a chart card takes the card radius'],
  ['views/Rubric.svelte', '.empty-state', 'border-radius', 'var(--card-radius)', '8px', '12px', 'D2: the empty state is a card'],
  ['views/Rubric.svelte', '.config-banner', 'border-radius', 'var(--card-radius)', '8px', '12px', 'D2: the banner is a card'],
  ['views/Rubric.svelte', '.data-table th', 'letter-spacing', 'var(--label-tracking)', '0.04em', '0.05em', 'D6: labels take the label tracking'],
  ['views/Rubric.svelte', '.data-table tbody tr:hover', 'background', 'var(--wash)', 'rgba(255, 255, 255, 0.02)', 'rgba(255, 255, 255, 0.03)', 'D4: the hover is the wash'],
  ['views/Rubric.svelte', '.badge-partial', 'background', 'color-mix(in srgb, var(--st-partial) 12.5%, transparent)', 'rgba(251, 146, 60, 0.15)', 'rgba(251, 146, 60, 0.125)', "PR 3's badge tint: at 15% the paper text is 4.49:1 on a hovered row"],
  ['views/Rubric.svelte', '.badge-pending', 'color', 'var(--muted)', '#a78bfa', '#8e8eab', 'D4: a pending or unknown status is neutral, not violet'],
  ['views/Rubric.svelte', '.badge-pending', 'background', 'var(--wash)', 'rgba(167, 139, 250, 0.15)', 'rgba(255, 255, 255, 0.03)', 'D4: the neutral badge sits on the wash'],
  ['views/Autonomy.svelte', '.strip', 'border-radius', 'var(--card-radius)', '8px', '12px', 'D2: the scorer strip takes the card radius'],
  ['views/Autonomy.svelte', '.card, .panel', 'border-radius', 'var(--card-radius)', '8px', '12px', 'D2: cards and panels take the card radius'],
  ['views/Autonomy.svelte', '.strip-meta dt, .tiles dt, .triple dt, .detail dt, .label', 'letter-spacing', 'var(--label-tracking)', '0.04em', '0.05em', 'D6: labels take the label tracking'],
  ['views/Intentions.svelte', '.panel', 'border-radius', 'var(--card-radius)', '8px', '12px', 'D2: panels take the card radius'],
  ['views/Intentions.svelte', '.tiles dt', 'letter-spacing', 'var(--label-tracking)', '0.04em', '0.05em', 'D6: labels take the label tracking'],
  ['views/Harness.svelte', '.card, .panel', 'border-radius', 'var(--card-radius)', '8px', '12px', 'D2: cards and panels take the card radius'],
  ['views/Harness.svelte', '.label', 'letter-spacing', 'var(--label-tracking)', '0.04em', '0.05em', 'D6: labels take the label tracking'],
  ['views/Harness.svelte', '.detail dt', 'letter-spacing', 'var(--label-tracking)', '0.04em', '0.05em', 'D6: labels take the label tracking'],
  // The old pill text was the rule's own colour (offered_set's shown); series colours are marks, never text.
  ['views/Harness.svelte', '.rule-pill', 'color', 'var(--text)', '#60a5fa', '#e2e2f0', 'the pill text is the ink; the rule colour is its dot and border'],
  ['views/Harness.svelte', '.rule-pill', 'border-radius', '999px', '4px', '999px', 'a rule pill is a pill, as the badges of PR 2 and PR 3'],
  ['views/Harness.svelte', '.badge', 'border-radius', '999px', '4px', '999px', 'the censor count is a pill, as the badges of PR 2 and PR 3'],
  ['views/Observability.svelte', '.chart-card', 'border-radius', 'var(--card-radius)', '10px', '12px', 'D2: a chart card takes the card radius'],
  ['views/Observability.svelte', '.trace-type', 'color', 'var(--accent-text)', '#38bdf8', '#a99df9', 'D4: the trace type is the accent text, not a one-off sky blue'],
  ['views/Observability.svelte', '.trace-badge', 'background', 'color-mix(in srgb, var(--muted) 10%, transparent)', 'rgba(96, 165, 250, 0.15)', 'rgba(142, 142, 171, 0.1)', 'D4 and review M2: the event count is a neutral badge, a tint that keeps its shape on a hovered row'],
  ['views/Observability.svelte', '.trace-badge', 'color', 'var(--muted)', '#60a5fa', '#8e8eab', 'D4: the event count is a neutral badge, not blue'],
  ['views/Observability.svelte', '.trace-badge', 'border-radius', '999px', '4px', '999px', 'a badge is a pill, as the badges of PR 2 to PR 4'],
  ['views/Observability.svelte', '.trace-badge--mod', 'background', 'color-mix(in srgb, var(--st-partial) 12.5%, transparent)', 'rgba(251, 146, 60, 0.15)', 'rgba(251, 146, 60, 0.125)', "PR 3's badge tint: at 15% the paper text is 4.49:1 on a hovered row"],
  ['views/Observability.svelte', '.trace-detail', 'background', 'var(--wash)', 'rgba(255, 255, 255, 0.025)', 'rgba(255, 255, 255, 0.03)', 'D4: the open trace sits on the wash'],
  ['views/Observability.svelte', '.mod-type', 'border-radius', '999px', '4px', '999px', 'a badge is a pill, as the badges of PR 2 to PR 4'],
  ['views/Observability.svelte', '.event-badge', 'border-radius', '999px', '4px', '999px', 'a badge is a pill, as the badges of PR 2 to PR 4'],
  ['views/Observability.svelte', '.mono', 'font-family', 'var(--font-mono)', 'monospace', "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace", "the trace id in the look's mono font (Red Hat Mono on paper); scoped, so no other screen moves"],
  ['views/Identity.svelte', 'textarea:focus-visible', 'outline', '2px solid var(--focus-ring)', 'none', '2px solid #7c6af7', 'the plan\'s form rule: the focus ring replaces outline: none and the accent border'],
  ['views/Identity.svelte', '.detail-grid .mono', 'font-family', 'var(--font-mono)', 'monospace', "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace", "the fact id in the look's mono font; a scoped rule, so the global .mono other screens use is unchanged"],
];

/** PR 4 and PR 5's routes and their views. */
const SCREENS: Record<string, string> = {
  decisions: 'views/Decisions.svelte',
  rubric: 'views/Rubric.svelte',
  autonomy: 'views/Autonomy.svelte',
  intentions: 'views/Intentions.svelte',
  harness: 'views/Harness.svelte',
  observability: 'views/Observability.svelte',
  identity: 'views/Identity.svelte',
};

const styleOf = (file: string) => parseBlocks(styleBlocks(readFileSync(join(SRC, file), 'utf-8')));

describe('the dark look of the converted dense screens equals the old literals', () => {
  it('the resolver follows var() chains and fallbacks, and reads a tint as the colour at that alpha', () => {
    expect(resolveDark('var(--err-text)')).toBe('#f87171');
    expect(resolveDark('var(--card-border)')).toBe('1px solid #1e1e2e');
    expect(resolveDark('var(--field-border)')).toBe('#1e1e2e');
    expect(resolveDark('var(--no-such, #123456)')).toBe('#123456');
    expect(() => resolveDark('var(--no-such)')).toThrow();
    expect(canonical(resolveDark('color-mix(in srgb, var(--stat-warn) 35%, transparent)'))).toBe('rgba(245, 158, 11, 0.35)');
    expect(canonical(resolveDark('color-mix(in srgb, var(--wash) 50%, transparent)'))).toBe('rgba(255, 255, 255, 0.015)');
    expect(canonical('rgba(245,158,11,0.35)')).toBe('rgba(245, 158, 11, 0.35)');
    expect(canonical('1px solid #1E1E2E')).toBe('1px solid rgba(30, 30, 46, 1)');
  });

  it('each row: the rule reads the value, and the value resolves under dark to the old literal', () => {
    for (const [file, selector, property, value, old] of ROWS) {
      const rule = styleOf(file).get(selector);
      expect(rule, `${file}: ${selector}`).toBeDefined();
      expect(rule![property], `${file}: ${selector} { ${property} }`).toBe(value);
      expect(canonical(resolveDark(value)), `${file}: ${selector} { ${property}: ${value} }`).toBe(canonical(old));
    }
  });

  it('each listed dark change: the rule reads the value, which moves from the old literal to the listed one', () => {
    for (const [file, selector, property, value, old, now, why] of CHANGES) {
      const rule = styleOf(file).get(selector);
      expect(rule?.[property], `${file}: ${selector} { ${property} } (${why})`).toBe(value);
      expect(canonical(resolveDark(value)), why).toBe(canonical(now));
      expect(canonical(now), why).not.toBe(canonical(old));
    }
  });

  it('every converted screen of the seven has its rows (a screen joins when its route turns paper)', () => {
    for (const [route, file] of Object.entries(SCREENS)) {
      if (!(PAPER_ROUTES as readonly string[]).includes(route)) continue;
      expect(ROWS.some((r) => r[0] === file), `${route}: no parity row for ${file}`).toBe(true);
    }
  });

  it('none of the seven imports lib/status.ts (its hex helpers are for the screens that are still dark)', () => {
    for (const file of Object.values(SCREENS)) {
      const js = scriptBlocks(readFileSync(join(SRC, file), 'utf-8'));
      expect(js, file).not.toMatch(/from\s*['"][^'"]*\/status(?:\.(?:ts|js))?['"]/);
    }
  });
});
