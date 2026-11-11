/**
 * One status → colour token map (harness dashboard spec §4.1), shared by the DAG view and the graph renderer so the
 * two can never disagree. The colours are app.css's tokens (`--st-*`, `--pending`, `--waiting`, `--unknown`).
 * Colour is never the only signal: every status also renders as text.
 */

export const DAG_NODE_STATUSES = [
  'pending', 'ready', 'running', 'awaiting_check', 'awaiting_input',
  'completed', 'failed', 'blocked', 'cancelled', 'skipped',
] as const;

export const LEDGER_STATUSES = ['pending', 'success', 'error', 'blocked', 'unknown'] as const;

/** The DAG statuses with a colour of their own, plus `partial` (a run that finished with some steps not done). */
const DAG_STATUS_SET = new Set<string>([...DAG_NODE_STATUSES, 'partial']);

/** `var(--st-<status>)` for a DAG node status (or `partial`); awaiting input is the waiting colour itself; an unknown
 * status takes the pending (muted) token. */
export function statusVar(status: string): string {
  if (status === 'awaiting_input') return 'var(--waiting)';
  return DAG_STATUS_SET.has(status) ? `var(--st-${status.replaceAll('_', '-')})` : 'var(--st-pending)';
}

const LEDGER_VAR: Record<string, string> = {
  pending: 'var(--pending)',
  success: 'var(--st-success)',
  error: 'var(--st-failed)',
  blocked: 'var(--st-ledger-blocked)',
  unknown: 'var(--unknown)',
};

/** The token of a ledger status; an unknown status takes the pending (muted) token. */
export function ledgerStatusVar(status: string): string {
  return Object.hasOwn(LEDGER_VAR, status) ? LEDGER_VAR[status] : 'var(--st-pending)';
}

/** Inline style for a status badge from a colour token: its 12.5% tint, the colour as text, a 25% border. */
export function badgeStyleVar(cssColor: string): string {
  return `background:color-mix(in srgb, ${cssColor} 12.5%, transparent);color:${cssColor};border-color:color-mix(in srgb, ${cssColor} 25%, transparent)`;
}

/** The waiting colour (an approval, a step awaiting an answer, a DAG stopped at one) as a token. */
export const WAITING_VAR = 'var(--waiting)';
