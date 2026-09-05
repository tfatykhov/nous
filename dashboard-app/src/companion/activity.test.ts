import { describe, it, expect } from 'vitest';
import {
  formatElapsed,
  pendingActionOf,
  pendingActivity,
  pendingIsFresh,
  recomposedAfter,
  PENDING_STALE_FALLBACK_MS,
} from './activity';

describe('formatElapsed', () => {
  it('reads as a wait, not a lap time', () => {
    expect(formatElapsed(0)).toBe('0s');
    expect(formatElapsed(4_200)).toBe('4s');
    expect(formatElapsed(72_000)).toBe('1m 12s');
    expect(formatElapsed(65_000)).toBe('1m 05s');
    expect(formatElapsed(3_780_000)).toBe('1h 03m');
    expect(formatElapsed(-5)).toBe('0s');
  });
});

describe('pendingActionOf', () => {
  it('parses a server stamp and derives the stale window from timeout_s', () => {
    const p = pendingActionOf({ pendingAction: { id: 'a', label: 'Act', at: '2026-09-04T10:00:00Z', timeout_s: 60 } });
    expect(p).toEqual({ id: 'a', label: 'Act', at: '2026-09-04T10:00:00Z', staleMs: 60_000, key: 'a@2026-09-04T10:00:00Z' });
  });

  it('identifies a stamp by its subtask_id — two stamps can share a second-precision at', () => {
    const at = '2026-09-04T10:00:00Z';
    const a = pendingActionOf({ pendingAction: { id: 'x', at, subtask_id: 'sub-1' } });
    const b = pendingActionOf({ pendingAction: { id: 'x', at, subtask_id: 'sub-2' } });
    expect(a?.key).toBe('sub-1');
    expect(b?.key).toBe('sub-2');
    expect(a?.key).not.toBe(b?.key);
  });

  it('falls back to the client window when the stamp predates timeout_s, and to id when label is missing', () => {
    const p = pendingActionOf({ pendingAction: { id: 'a', at: '2026-09-04T10:00:00Z' } });
    expect(p?.label).toBe('a');
    expect(p?.staleMs).toBe(PENDING_STALE_FALLBACK_MS);
  });

  it('treats anything malformed as no pending action', () => {
    expect(pendingActionOf(undefined)).toBeNull();
    expect(pendingActionOf({})).toBeNull();
    expect(pendingActionOf({ pendingAction: 'x' })).toBeNull();
    expect(pendingActionOf({ pendingAction: { id: 'a' } })).toBeNull();
    expect(pendingActionOf({ pendingAction: { at: 'now' } })).toBeNull();
  });
});

describe('pendingIsFresh / pendingActivity', () => {
  const at = Date.parse('2026-09-04T10:00:00Z');
  const meta = { pendingAction: { id: 'rebalance', label: 'Rebalance', at: '2026-09-04T10:00:00Z', timeout_s: 300 } };

  it('is fresh inside the window and stale past it', () => {
    const p = pendingActionOf(meta);
    expect(pendingIsFresh(p, at + 10_000)).toBe(true);
    expect(pendingIsFresh(p, at + 300_000)).toBe(false);
    expect(pendingIsFresh(null, at)).toBe(false);
  });

  it('an unparsable stamp is never fresh', () => {
    expect(pendingIsFresh(pendingActionOf({ pendingAction: { id: 'a', at: 'not a date' } }), at)).toBe(false);
  });

  it('a fresh stamp is an act activity that started at the stamp', () => {
    expect(pendingActivity(meta, at + 5_000)).toEqual({ kind: 'act', id: 'rebalance', startedAt: at, token: 0 });
    expect(pendingActivity(meta, at + 600_000)).toBeNull();
  });
});

describe('recomposedAfter', () => {
  const tap = Date.parse('2026-09-04T10:00:00Z');

  it('is true only for a composedAt strictly after the tap', () => {
    expect(recomposedAfter('2026-09-04T10:00:41Z', tap)).toBe(true);
    // Same second: server stamps are second-precision, so this is "not
    // after" — a missed flash beats claiming an update that did not happen.
    expect(recomposedAfter('2026-09-04T10:00:00Z', tap)).toBe(false);
    // The pre-tap compose the failure watcher leaves untouched.
    expect(recomposedAfter('2026-09-04T09:00:00Z', tap)).toBe(false);
  });

  it('never fires on an unparsable stamp', () => {
    expect(recomposedAfter('', tap)).toBe(false);
    expect(recomposedAfter('soon', tap)).toBe(false);
  });
});
