import { describe, it, expect } from 'vitest';
import { get } from 'svelte/store';
import { makePollStore } from './registry';

type Call = { q: string; resolve: (v: string) => void; signal?: AbortSignal };

/** A fetcher whose answers the test releases by hand, recording the query each call saw. */
function manualFetcher(query: () => string) {
  const calls: Call[] = [];
  const fetcher = (signal?: AbortSignal) => new Promise<string>((resolve, reject) => {
    calls.push({ q: query(), resolve, signal });
    signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
  });
  return { calls, fetcher };
}

describe('makePollStore.refresh', () => {
  it('a refresh while a fetch is in flight aborts it and fetches again with the latest inputs', async () => {
    let q = 'a';
    const { calls, fetcher } = manualFetcher(() => q);
    const store = makePollStore<string>(fetcher, 0);

    const first = store.refresh();
    q = 'ab'; // the user typed another character before the answer came back
    const second = store.refresh();

    expect(calls[0].signal?.aborted).toBe(true);
    calls[0].resolve('a'); // a late answer for the old query is never committed
    await first;
    expect(calls.map((c) => c.q)).toEqual(['a', 'ab']);
    expect(get(store).data).toBeNull();

    calls[1].resolve('ab');
    await second;
    expect(get(store).data).toBe('ab');
  });

  it('several refreshes during one fetch cost one more fetch, not one each', async () => {
    let q = 'a';
    const { calls, fetcher } = manualFetcher(() => q);
    const store = makePollStore<string>(fetcher, 0);

    void store.refresh();
    q = 'ab';
    void store.refresh();
    q = 'abc';
    const last = store.refresh();
    await new Promise((r) => setTimeout(r, 0)); // let the aborted fetch settle

    expect(calls.map((c) => c.q)).toEqual(['a', 'abc']);
    calls[1].resolve('abc');
    await last;
    expect(get(store).data).toBe('abc');
  });

  it('stop() drops a refresh queued behind an aborted fetch', async () => {
    const { calls, fetcher } = manualFetcher(() => 'x');
    const store = makePollStore<string>(fetcher, 0);

    void store.refresh();
    const queued = store.refresh();
    store.stop(); // e.g. the Ledger pausing to show older rows
    await queued;

    expect(calls).toHaveLength(1);
    expect(get(store).data).toBeNull();
  });
});
