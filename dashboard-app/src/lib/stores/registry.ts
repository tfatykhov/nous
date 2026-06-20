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

/**
 * Create a polling store.
 *
 * @param intervalMs  Poll cadence in ms. Pass `0` (or any value <= 0) for
 *   **fetch-once** mode: `start()` fetches a single time and never reschedules
 *   (use the returned `refresh()` for a manual reload). Do NOT pass a huge
 *   sentinel like `Number.MAX_SAFE_INTEGER` to fake "load once" — JS timer
 *   delays are clamped to a 32-bit range (~24.8 days max), so oversized delays
 *   overflow and fire almost immediately, turning a "load-once" view into a
 *   rapid poll.
 */
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
    // intervalMs <= 0 => fetch-once: do not reschedule after the initial tick.
    if (!stopped && intervalMs > 0) timer = setTimeout(() => void tick(), intervalMs);
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
