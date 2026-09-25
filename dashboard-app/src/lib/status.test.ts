import { describe, it, expect } from 'vitest';
import { DAG_NODE_STATUSES, LEDGER_STATUSES, statusColor, ledgerStatusColor } from './status';

describe('status colours — one map for the DAG view and the graph', () => {
  it('covers every DAGNodeStatus the backend can send', () => {
    // nous/dag/schemas.py DAGNodeStatus
    for (const s of ['pending', 'ready', 'running', 'awaiting_check', 'awaiting_input',
      'completed', 'failed', 'blocked', 'cancelled', 'skipped']) {
      expect(DAG_NODE_STATUSES).toContain(s);
      expect(statusColor(s)).toMatch(/^#[0-9a-f]{6}$/);
    }
  });

  it('gives awaiting_input the waiting colour, distinct from awaiting_check', () => {
    expect(statusColor('awaiting_input')).toBe('#a78bfa');
    expect(statusColor('awaiting_input')).not.toBe(statusColor('awaiting_check'));
  });

  it('covers every ledger status', () => {
    for (const s of ['pending', 'success', 'error', 'blocked', 'unknown']) {
      expect(LEDGER_STATUSES).toContain(s);
      expect(ledgerStatusColor(s)).toMatch(/^#[0-9a-f]{6}$/);
    }
    expect(ledgerStatusColor('unknown')).toBe('#f472b6');
  });

  it('falls back to the muted colour for an unknown status', () => {
    expect(statusColor('nonsense')).toBe('#8e8eab');
  });
});
