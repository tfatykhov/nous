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
