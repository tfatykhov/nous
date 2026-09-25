import { describe, it, expect } from 'vitest';
import { DAG_NODE_STATUSES, LEDGER_STATUSES, statusColor, ledgerStatusColor } from './status';

// WCAG relative luminance / contrast ratio.
const rgb = (hex: string) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
const channel = (c: number) => { const v = c / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
const lum = (c: number[]) => 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2]);
const contrast = (a: number[], b: number[]) => {
  const [x, y] = [lum(a), lum(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
};
/** A badge (badgeStyle): the colour as text over its own 0x20 tint on --surface. */
const badgeContrast = (hex: string) => {
  const fg = rgb(hex);
  const surface = rgb('#12121a');
  const a = 0x20 / 255;
  return contrast(fg, fg.map((v, i) => v * a + surface[i] * (1 - a)));
};

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

  it('every status badge is readable: 4.5:1 (AA, 11px text) against its own tint', () => {
    const statuses = [...DAG_NODE_STATUSES, 'partial'].map((s) => [`dag ${s}`, statusColor(s)] as const)
      .concat(LEDGER_STATUSES.map((s) => [`ledger ${s}`, ledgerStatusColor(s)] as const));
    const failing = statuses.filter(([, hex]) => badgeContrast(hex) < 4.5)
      .map(([name, hex]) => `${name} ${hex} ${badgeContrast(hex).toFixed(2)}`);
    expect(failing).toEqual([]);
  });

  it('keeps blocked distinct from failed', () => {
    expect(statusColor('blocked')).not.toBe(statusColor('failed'));
  });

  it('falls back to the muted colour for an unknown status', () => {
    expect(statusColor('nonsense')).toBe('#8e8eab');
  });
});
