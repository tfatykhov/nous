/**
 * One status → colour map (harness dashboard spec §4.1), shared by the DAG
 * view and the graph renderer so the two can never disagree. Values are hex
 * because callers build tints by appending an alpha suffix ("20", "40").
 * Colour is never the only signal: every status also renders as text.
 */

export const DAG_NODE_STATUSES = [
  'pending', 'ready', 'running', 'awaiting_check', 'awaiting_input',
  'completed', 'failed', 'blocked', 'cancelled', 'skipped',
] as const;

export const LEDGER_STATUSES = ['pending', 'success', 'error', 'blocked', 'unknown'] as const;

/** Matches the --muted token (app.css). */
export const MUTED = '#8e8eab';
/** --waiting: an approval, a step awaiting an answer, a DAG stopped at one. */
export const WAITING = '#a78bfa';
/** --unknown: a call whose outcome was never confirmed. */
export const UNKNOWN = '#f472b6';
/** --pending (ledger) / ready (DAG). */
export const PENDING = '#22d3ee';

const DAG: Record<string, string> = {
  pending: MUTED,
  ready: PENDING,
  running: '#fbbf24',
  awaiting_check: '#f59e0b',
  awaiting_input: WAITING,
  completed: '#4ade80',
  failed: '#f87171',
  // #dc2626 was 3.55:1 on its own badge tint (fails AA at 11px); this stays
  // a deeper red than failed and clears 4.5:1 (status.test.ts checks all).
  blocked: '#f25c5c',
  cancelled: '#8a8a9a',
  skipped: '#94a3b8',
  partial: '#fb923c',
};

const LEDGER: Record<string, string> = {
  pending: PENDING,
  success: '#10b981',
  error: '#f87171',
  blocked: '#f59e0b',
  unknown: UNKNOWN,
};

export function statusColor(status: string): string {
  return DAG[status] ?? MUTED;
}

export function ledgerStatusColor(status: string): string {
  return LEDGER[status] ?? MUTED;
}

/** Inline style for a status badge (the DagView badge shape). */
export function badgeStyle(color: string): string {
  return `background:${color}20;color:${color};border-color:${color}40`;
}

// ── Redesign: the var() siblings ─────────────────────────────────────────────────────────────────────────────────────
// The views paint status with tokens (app.css `--st-*`), so a ground can restyle them; each token's dark value is the
// hex above for the same status (status.test.ts proves it). The hex functions stay for anything that parses hex.

/** `var(--st-<status>)` for a DAG node status (or `partial`); an unknown status takes the pending (muted) token. */
export function statusVar(status: string): string {
  return Object.hasOwn(DAG, status) ? `var(--st-${status.replaceAll('_', '-')})` : 'var(--st-pending)';
}

const LEDGER_VAR: Record<string, string> = {
  pending: 'var(--pending)',
  success: 'var(--st-success)',
  error: 'var(--st-failed)',
  blocked: 'var(--st-ledger-blocked)',
  unknown: 'var(--st-unknown)',
};

/** The token of a ledger status; an unknown status takes the pending (muted) token. */
export function ledgerStatusVar(status: string): string {
  return Object.hasOwn(LEDGER_VAR, status) ? LEDGER_VAR[status] : 'var(--st-pending)';
}

/** Inline style for a status badge from a colour token: its 12.5% tint, the colour as text, a 25% border (the old
 * `20` and `40` hex alphas, so a dark badge composites as before). */
export function badgeStyleVar(cssColor: string): string {
  return `background:color-mix(in srgb, ${cssColor} 12.5%, transparent);color:${cssColor};border-color:color-mix(in srgb, ${cssColor} 25%, transparent)`;
}

/** The waiting colour (an approval, a step awaiting an answer, a DAG stopped at one) as a token. */
export const WAITING_VAR = 'var(--waiting)';
