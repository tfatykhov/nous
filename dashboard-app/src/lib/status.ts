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
