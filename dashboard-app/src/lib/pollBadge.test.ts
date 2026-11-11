import { describe, it, expect } from 'vitest';
import { combinePolls, nameList } from './pollBadge';
import type { PollState } from './stores/registry';

// Codex round 1 (PR 7): a screen that reads more than one poll store has one "updated" badge in its header, so
// that badge must speak for every store: the oldest update, loading while any loads, and an error, naming what failed,
// when any store's last refresh failed (the poll store keeps its earlier data, so a failure is otherwise silent).

const ok = (t: number): PollState<unknown> => ({ data: {}, error: null, loading: false, lastUpdated: t });
const failed = (t: number | null): PollState<unknown> => ({ data: t === null ? null : {}, error: new Error('500'), loading: false, lastUpdated: t });
const first = (): PollState<unknown> => ({ data: null, error: null, loading: true, lastUpdated: null });

describe('combinePolls', () => {
  it('all fresh: no error, not loading, the oldest update', () => {
    expect(combinePolls({ health: ok(2000), density: ok(1000) })).toEqual(
      { data: true, error: null, loading: false, lastUpdated: 1000, failed: [] });
  });

  it('one store failed after a good load: an error naming it, and the age of the oldest data still shown', () => {
    const c = combinePolls({ health: ok(5000), density: failed(1000) });
    expect(c.failed).toEqual(['density']);
    expect(c.error?.message).toBe('density failed');
    expect(c.lastUpdated).toBe(1000);
    expect(c.data).toBe(true);
  });

  it('every store that failed is named, in order', () => {
    expect(combinePolls({ a: failed(1), b: ok(2), c: failed(3) }).failed).toEqual(['a', 'c']);
  });

  it('loading while any store loads; no data and no time until every store has loaded once', () => {
    const c = combinePolls({ health: ok(5000), density: first() });
    expect(c.loading).toBe(true);
    expect(c.data).toBeNull();
    expect(c.lastUpdated).toBeNull();
  });

  it('a store that failed before it ever loaded has no time', () => {
    expect(combinePolls({ health: ok(5000), density: failed(null) })).toMatchObject({ data: null, lastUpdated: null, failed: ['density'] });
  });
});

describe('nameList', () => {
  it('says one, two or more names as a sentence', () => {
    expect([nameList(['a']), nameList(['a', 'b']), nameList(['a', 'b', 'c'])]).toEqual(['a', 'a and b', 'a, b and c']);
  });
});
