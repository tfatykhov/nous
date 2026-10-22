import { describe, it, expect } from 'vitest';
import { bars, count, coverageFrom, fixBaseline, fixLatest, fixOutcome, fixState, pct, score, scorerHealth, SCORER_STALE_MIN } from './autonomy';
import type { JobFixRow } from './types/api';

const NOW = Date.parse('2026-10-17T21:00:00Z');
const minsAgo = (m: number) => new Date(NOW - m * 60000).toISOString();
const beat = (over: Record<string, unknown> = {}) => ({
  scorer_version: 1, last_run_at: minsAgo(5), last_status: 'ok', last_error: null,
  last_ok_at: minsAgo(5), rows_submitted: 1614, ...over,
});

describe('scorerHealth', () => {
  it('says the scorer has not run when there is no heartbeat row', () => {
    const v = scorerHealth(null, 1, NOW);
    expect(v.title).toBe('Scorer has not run yet');
    expect(v.tone).toBe('warn');
  });

  it('reports a fresh ok pass with its row count', () => {
    const v = scorerHealth(beat(), 1, NOW);
    expect(v).toMatchObject({ title: 'Scorer running', tone: 'ok' });
    expect(v.detail).toContain('1614 rows');
  });

  it('names the error of a failed pass and the last successful one', () => {
    const v = scorerHealth(beat({ last_status: 'error', last_error: 'InsufficientPrivilege', last_ok_at: minsAgo(20) }), 1, NOW);
    expect(v).toMatchObject({ title: 'Last pass failed', tone: 'warn' });
    expect(v.detail).toContain('InsufficientPrivilege');
    expect(v.detail).toContain('Last successful pass');
  });

  it('says so when no pass ever succeeded', () => {
    const v = scorerHealth(beat({ last_status: 'error', last_error: 'X', last_ok_at: null }), 1, NOW);
    expect(v.detail).toContain('No pass has succeeded yet.');
  });

  it('calls a heartbeat older than three intervals stopped, even when its last pass was ok', () => {
    expect(scorerHealth(beat({ last_run_at: minsAgo(SCORER_STALE_MIN - 1) }), 1, NOW).title).toBe('Scorer running');
    const v = scorerHealth(beat({ last_run_at: minsAgo(SCORER_STALE_MIN + 1) }), 1, NOW);
    expect(v).toMatchObject({ title: 'Scorer looks stopped', tone: 'warn' });
  });

  it('warns when the scorer writes another version than the endpoint reads', () => {
    const v = scorerHealth(beat({ scorer_version: 1 }), 2, NOW);
    expect(v).toMatchObject({ title: 'Scorer version differs', tone: 'warn' });
    expect(v.detail).toContain('writes version 1; this deployment reads version 2');
  });

  it('leaves the row count out when the pass committed none', () => {
    expect(scorerHealth(beat({ rows_submitted: null }), 1, NOW).detail).not.toContain('rows');
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

describe('coverageFrom', () => {
  // The endpoint counts coverage from max(window start, first_scored_at).
  it("names the scorer's first run while it falls inside the window", () => {
    expect(coverageFrom(minsAgo(60), 7, NOW)).toMatch(/^Since the scorer's first run \(Oct 17 20:00 UTC/);
  });

  it('names the window once the first run is older than it', () => {
    expect(coverageFrom(minsAgo(20 * 24 * 60), 7, NOW)).toBe('In the last 7 d');
    expect(coverageFrom(minsAgo(20 * 24 * 60), 30, NOW)).toMatch(/^Since the scorer's first run/);
  });
});

const fix = (over: Partial<JobFixRow> = {}): JobFixRow => ({
  schedule_id: 'ab'.repeat(16), job: 'Run the grid tick', active: true, state: 'running', trigger: 'fails_often',
  verdict: null, stop_reason: null, started_at: minsAgo(60), applied_at: null, settled_at: null, fix_run: null,
  reported: false, baseline: null, since_change: null, detail: {}, old_task: null, task: null, ...over,
});

describe('check and fix', () => {
  it('labels each state, and says who stopped a job and whether you were told', () => {
    expect(fixState(fix())).toEqual({ label: 'Checking', tone: 'live' });
    expect(fixState(fix({ state: 'judging' })).label).toBe('Judging the change');
    expect(fixState(fix({ state: 'kept' }))).toEqual({ label: 'Fix kept', tone: 'ok' });
    expect(fixState(fix({ state: 'closed' })).tone).toBe('muted');
    expect(fixState(fix({ state: 'stopped', stop_reason: 'did_not_help', reported: true })))
      .toEqual({ label: 'Stopped, you were told', tone: 'warn' });
    expect(fixState(fix({ state: 'stopped', stop_reason: 'inactive' })).label).toBe('Stopped by you');
    expect(fixState(fix({ state: null })).label).toBe('Unreadable record');
  });

  it('counts the judged runs and says when the judgment is due', () => {
    const judging = (scored: number) => fix({ state: 'judging', since_change: { scored, bad: 1, needed: 5 } });
    expect(fixOutcome(judging(2))).toBe('2 of 5 runs since the change, 1 went wrong');
    expect(fixOutcome(judging(5))).toBe('5 of 5 runs since the change, 1 went wrong; judged at the next sleep');
  });

  it('names the stop reason, in the right tense', () => {
    expect(fixOutcome(fix({ state: 'stopped', stop_reason: 'needs_owner' }))).toBe('Stopped: the check needs you');
    expect(fixOutcome(fix({ state: 'stopping', stop_reason: 'fails_again' }))).toBe('Stopping: it started failing again');
    expect(fixOutcome(fix({ state: 'kept', verdict: 'changed_job' }))).toBe('Changed the instructions; later runs fail less');
    expect(fixOutcome(fix())).toBe('Check run in progress');
  });

  it('shows the baseline only when there is one, and the newest time', () => {
    expect(fixBaseline(fix())).toBeNull();
    expect(fixBaseline(fix({ baseline: { bad: 4, scored: 5 } }))).toBe('4 of 5 runs went wrong before');
    expect(fixLatest(fix({ applied_at: minsAgo(30), settled_at: null }))).toBe(minsAgo(30));
    expect(fixLatest(fix({ started_at: null }))).toBeNull();
  });
});
