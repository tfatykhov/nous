import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import { makePollStore } from './stores/registry';

describe('makePollStore', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('fetches once on start and updates data', async () => {
    const fetcher = vi.fn(async () => ({ n: 1 }));
    const s = makePollStore(fetcher, 30000);
    s.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(get(s).data).toEqual({ n: 1 });
    expect(get(s).error).toBeNull();
    s.stop();
  });

  it('re-fetches on interval', async () => {
    let i = 0; const fetcher = vi.fn(async () => ({ n: ++i }));
    const s = makePollStore(fetcher, 1000);
    s.start();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1000);
    expect(get(s).data).toEqual({ n: 2 });
    s.stop();
  });

  it('captures errors without clobbering last good data', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce({ n: 1 })
      .mockRejectedValueOnce(new Error('boom'));
    const s = makePollStore(fetcher as any, 1000);
    s.start();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1000);
    const v = get(s);
    expect(v.data).toEqual({ n: 1 });
    expect(v.error).toBeInstanceOf(Error);
    s.stop();
  });

  it('fetch-once mode (interval <= 0) does not reschedule', async () => {
    const fetcher = vi.fn(async () => ({ n: 1 }));
    const s = makePollStore(fetcher, 0);
    s.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetcher).toHaveBeenCalledTimes(1);
    // advance well past any 32-bit-clamped delay window — must NOT re-fetch
    await vi.advanceTimersByTimeAsync(60_000);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetcher).toHaveBeenCalledTimes(1);
    // manual refresh still works
    await s.refresh();
    expect(fetcher).toHaveBeenCalledTimes(2);
    s.stop();
  });

  it('does not stack duplicate timers when refresh races an in-flight poll', async () => {
    let resolve: ((v: unknown) => void) | null = null;
    let calls = 0;
    const fetcher = vi.fn(() => { calls++; return new Promise((r) => { resolve = r; }); });
    const s = makePollStore(fetcher as any, 1000);
    s.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(1);                 // tick1 in flight
    void s.refresh();                      // races in-flight -> early return, no extra fetch/timer
    expect(calls).toBe(1);
    resolve!({ n: 1 });                    // tick1 settles -> finally schedules exactly ONE timer
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1000);
    expect(calls).toBe(2);                 // one timer fired, not two+ (no duplicate)
    s.stop();
  });

  it('aborts the in-flight fetch on stop()', async () => {
    let seenSignal: AbortSignal | undefined;
    const fetcher = vi.fn((signal?: AbortSignal) => {
      seenSignal = signal;
      return new Promise((resolve) => { /* never resolves on its own */ });
    });
    const s = makePollStore(fetcher as any, 1000);
    s.start();
    await vi.advanceTimersByTimeAsync(0);
    s.stop();
    expect(seenSignal?.aborted).toBe(true);
  });
});
