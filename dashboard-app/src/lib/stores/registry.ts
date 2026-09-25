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
  // A refresh() that arrived while a fetch was in flight: one re-run with
  // the latest inputs, resolved when it lands.
  let again: { promise: Promise<void>; resolve: () => void } | null = null;

  function schedule() {
    // Clear any existing timer before arming a new one so that calling
    // refresh() while a poll timer is already armed doesn't leak the old handle
    // and cause duplicate recurring polls.
    if (timer) { clearTimeout(timer); timer = null; }
    // intervalMs <= 0 => fetch-once: do not reschedule after the initial tick.
    if (!stopped && intervalMs > 0) timer = setTimeout(() => void tick(), intervalMs);
  }

  async function tick() {
    // A tick fired (or refresh() was called) while a request is still in
    // flight. Do NOT schedule here — the in-flight tick's `finally` will
    // reschedule. Scheduling here too would stack duplicate timers.
    if (inFlight) return;
    inFlight = true;
    const mine = new AbortController();
    ac = mine;
    update((s) => ({ ...s, loading: true }));
    try {
      const data = await fetcher(mine.signal);
      if (!mine.signal.aborted) {
        update((s) => ({ ...s, data, error: null, loading: false, lastUpdated: Date.now() }));
      }
    } catch (err) {
      if (!mine.signal.aborted) {
        update((s) => ({ ...s, error: err as Error, loading: false }));
      }
    } finally {
      inFlight = false;
      if (again) {
        const queued = again;
        again = null;
        void tick().then(queued.resolve);
      } else {
        schedule();
      }
    }
  }

  /**
   * Fetch now. A timer tick that finds a fetch in flight just lets it finish,
   * but a refresh means the inputs changed (a filter, a window) or the user
   * asked: the answer in flight is for the OLD inputs, so it is aborted —
   * never committed — and one more fetch runs as soon as it settles. Several
   * refreshes during one fetch share that single re-run.
   */
  function refresh(): Promise<void> {
    if (!inFlight) return tick();
    ac?.abort();
    if (!again) {
      let resolve!: () => void;
      const promise = new Promise<void>((r) => { resolve = r; });
      again = { promise, resolve };
    }
    return again.promise;
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
      // A stopped store fetches nothing more, queued refresh included (the
      // Ledger stops to hold its rows still while older ones are shown).
      if (again) { again.resolve(); again = null; }
    },
    refresh,
  };
}
