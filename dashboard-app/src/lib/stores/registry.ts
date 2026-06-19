import { writable, type Readable } from 'svelte/store';

export interface PollState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  lastUpdated: number | null;
}

export interface PollStore<T> extends Readable<PollState<T>> {
  start(): void;
  stop(): void;
  refresh(): Promise<void>;
}

export function makePollStore<T>(
  fetcher: (signal?: AbortSignal) => Promise<T>,
  intervalMs: number,
): PollStore<T> {
  const { subscribe, update } = writable<PollState<T>>({
    data: null,
    error: null,
    loading: false,
    lastUpdated: null,
  });

  let timer: ReturnType<typeof setTimeout> | null = null;
  let inFlight = false;
  let stopped = true;
  let ac: AbortController | null = null;

  function schedule() {
    if (!stopped) timer = setTimeout(() => void tick(), intervalMs);
  }

  async function tick() {
    if (inFlight) { schedule(); return; }
    inFlight = true;
    ac = new AbortController();
    update((s) => ({ ...s, loading: true }));
    try {
      const data = await fetcher(ac.signal);
      if (!ac.signal.aborted) {
        update((s) => ({ ...s, data, error: null, loading: false, lastUpdated: Date.now() }));
      }
    } catch (err) {
      if (!ac.signal.aborted) {
        update((s) => ({ ...s, error: err as Error, loading: false }));
      }
    } finally {
      inFlight = false;
      schedule();
    }
  }

  return {
    subscribe,
    start() {
      if (!stopped) return;
      stopped = false;
      void tick();
    },
    stop() {
      stopped = true;
      if (timer) { clearTimeout(timer); timer = null; }
      ac?.abort();
    },
    refresh: tick,
  };
}
