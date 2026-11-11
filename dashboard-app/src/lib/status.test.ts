import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import * as status from './status';
import { DAG_NODE_STATUSES, LEDGER_STATUSES, WAITING_VAR, badgeStyleVar, ledgerStatusVar, statusVar } from './status';
import { ROOT_SEL, block, parseBlocks } from './testing/css';

// WCAG relative luminance / contrast ratio.
const rgb = (hex: string) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
const channel = (c: number) => { const v = c / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
const lum = (c: number[]) => 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2]);
const contrast = (a: number[], b: number[]) => {
  const [x, y] = [lum(a), lum(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
};

// The views paint status with tokens (`--st-*`, `--pending`, `--waiting`, `--unknown`), declared at :root. One map for
// the DAG view and the graph renderer, so the two can never disagree; colour is never the only signal (every status
// also renders as text).
const PAPER = block(parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')), ROOT_SEL, 50);
const token = (v: string) => v.match(/^var\((--[\w-]+)\)$/)?.[1] ?? '';
const over = (fg: number[], a: number, bg: number[]) => fg.map((v, i) => v * a + bg[i] * (1 - a));

describe('status tokens', () => {
  it('the module exports the token helpers and the status lists only: no hex map or hex helper is left (PR 7 T4)', () => {
    expect(Object.keys(status).sort()).toEqual(
      ['DAG_NODE_STATUSES', 'LEDGER_STATUSES', 'WAITING_VAR', 'badgeStyleVar', 'ledgerStatusVar', 'statusVar'].sort());
    expect(readFileSync(join(__dirname, 'status.ts'), 'utf-8')).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });

  it('covers every DAGNodeStatus the backend can send', () => {
    // nous/dag/schemas.py DAGNodeStatus
    for (const s of ['pending', 'ready', 'running', 'awaiting_check', 'awaiting_input',
      'completed', 'failed', 'blocked', 'cancelled', 'skipped']) {
      expect(DAG_NODE_STATUSES).toContain(s);
    }
  });

  it('every DAG status (and partial) has its token, declared; awaiting input is the waiting colour itself', () => {
    for (const s of [...DAG_NODE_STATUSES, 'partial']) {
      const want = s === 'awaiting_input' ? 'var(--waiting)' : `var(--st-${s.replaceAll('_', '-')})`;
      expect(statusVar(s), s).toBe(want);
      expect(PAPER[token(statusVar(s))], s).toMatch(/^#[0-9a-f]{6}$/);
    }
    expect(statusVar('awaiting_input')).not.toBe(statusVar('awaiting_check'));
  });

  it('every ledger status maps to a declared token; an unknown send is the unknown colour itself', () => {
    const want: Record<string, string> = {
      pending: 'var(--pending)', success: 'var(--st-success)', error: 'var(--st-failed)',
      blocked: 'var(--st-ledger-blocked)', unknown: 'var(--unknown)',
    };
    for (const s of LEDGER_STATUSES) {
      expect(ledgerStatusVar(s), s).toBe(want[s]);
      expect(PAPER[token(ledgerStatusVar(s))], s).toMatch(/^#[0-9a-f]{6}$/);
    }
  });

  it('the waiting token is a name for the waiting colour', () => {
    expect(WAITING_VAR).toBe('var(--waiting)');
  });

  it('every token a badge can take is readable on the paper card and on a hovered row: 4.5:1 on its own 12.5% tint', () => {
    const vars = new Set([...[...DAG_NODE_STATUSES, 'partial'].map(statusVar), ...LEDGER_STATUSES.map(ledgerStatusVar), statusVar('x')]);
    const failing = ['#ffffff', PAPER['--surface-hover']].flatMap((bg) => [...vars].map(token).map((t): [string, string, string] => [t, PAPER[t], bg])
      .filter(([, hex]) => !hex || contrast(rgb(hex), over(rgb(hex), 0.125, rgb(bg))) < 4.5));
    expect(failing).toEqual([]);
  });

  it('keeps blocked distinct from failed', () => {
    expect(statusVar('blocked')).not.toBe(statusVar('failed'));
    expect(PAPER['--st-blocked']).not.toBe(PAPER['--st-failed']);
  });

  it('a badge style is built from the token with color-mix: no hex, no alpha suffix', () => {
    const s = badgeStyleVar('var(--st-failed)');
    expect(s).toBe('background:color-mix(in srgb, var(--st-failed) 12.5%, transparent);color:var(--st-failed);'
      + 'border-color:color-mix(in srgb, var(--st-failed) 25%, transparent)');
    expect(s).not.toMatch(/#/);
    // 12.5% and 25% are the old "20" and "40" hex alphas (32/255, 64/255).
    expect(Math.abs(0.125 - 0x20 / 255)).toBeLessThan(0.001);
    expect(Math.abs(0.25 - 0x40 / 255)).toBeLessThan(0.001);
  });

  it('an unknown status falls back to the pending (muted) token', () => {
    expect(statusVar('nonsense')).toBe('var(--st-pending)');
    expect(ledgerStatusVar('nonsense')).toBe('var(--st-pending)');
    expect(statusVar('constructor')).toBe('var(--st-pending)');
    expect(ledgerStatusVar('toString')).toBe('var(--st-pending)');
  });
});
