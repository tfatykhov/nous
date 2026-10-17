import { describe, it, expect } from 'vitest';
import { bars, count, pct, score, scorerHealth, SCORER_STALE_MIN } from './autonomy';

const NOW = Date.parse('2026-10-17T21:00:00Z');
const minsAgo = (m: number) => new Date(NOW - m * 60000).toISOString();
const beat = (over: Record<string, unknown> = {}) => ({
  scorer_version: 1, last_run_at: minsAgo(5), last_status: 'ok', last_error: null,
  last_ok_at: minsAgo(5), rows_submitted: 1614, ...over,
});

describe('scorerHealth', () => {
  it('says the scorer has not run when there is no heartbeat row', () => {
    const v = scorerHealth(null, NOW);
    expect(v.title).toBe('Scorer has not run yet');
    expect(v.tone).toBe('warn');
  });

  it('reports a fresh ok pass with its row count', () => {
    const v = scorerHealth(beat(), NOW);
    expect(v).toMatchObject({ title: 'Scorer running', tone: 'ok' });
    expect(v.detail).toContain('1614 rows');
  });

  it('names the error of a failed pass and the last successful one', () => {
    const v = scorerHealth(beat({ last_status: 'error', last_error: 'InsufficientPrivilege', last_ok_at: minsAgo(20) }), NOW);
    expect(v).toMatchObject({ title: 'Last pass failed', tone: 'warn' });
    expect(v.detail).toContain('InsufficientPrivilege');
    expect(v.detail).toContain('Last successful pass');
  });

  it('says so when no pass ever succeeded', () => {
    const v = scorerHealth(beat({ last_status: 'error', last_error: 'X', last_ok_at: null }), NOW);
    expect(v.detail).toContain('No pass has succeeded yet.');
  });

  it('calls a heartbeat older than three intervals stopped, even when its last pass was ok', () => {
    expect(scorerHealth(beat({ last_run_at: minsAgo(SCORER_STALE_MIN - 1) }), NOW).title).toBe('Scorer running');
    const v = scorerHealth(beat({ last_run_at: minsAgo(SCORER_STALE_MIN + 1) }), NOW);
    expect(v).toMatchObject({ title: 'Scorer looks stopped', tone: 'warn' });
  });

  it('leaves the row count out when the pass committed none', () => {
    expect(scorerHealth(beat({ rows_submitted: null }), NOW).detail).not.toContain('rows');
  });
});

describe('formatters', () => {
  it('show a dash for an absent value, never NaN', () => {
    expect(pct(null)).toBe('—');
    expect(pct(undefined)).toBe('—');
    expect(score(null)).toBe('—');
  });

  it('round shares to whole percents and scores to two decimals', () => {
    expect(pct(0.4567)).toBe('46%');
    expect(pct(0)).toBe('0%');
    expect(score(-0.5)).toBe('-0.50');
    expect(score(0)).toBe('0.00');
    expect(count(18310)).toBe('18,310');
  });

  it('scale bars against the largest count, and keep zero counts at zero', () => {
    expect(bars([['a', 10], ['b', 5], ['c', 0]])).toEqual([
      { key: 'a', count: 10, pct: 100 }, { key: 'b', count: 5, pct: 50 }, { key: 'c', count: 0, pct: 0 },
    ]);
    expect(bars([['a', 0]])).toEqual([{ key: 'a', count: 0, pct: 0 }]);
  });
});
