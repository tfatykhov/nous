import type { PollState } from './stores/registry';

/** What a screen's one "updated" badge shows when the screen reads more than one poll store. */
export type CombinedPoll = PollState<true> & { failed: string[] };

/** "a", "a and b", "a, b and c". */
export function nameList(names: string[]): string {
  return names.length < 2 ? (names[0] ?? '') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/**
 * The poll states a screen reads, named in the owner's words, as one badge state (Codex round 1): data once
 * every store has loaded, loading while any loads, the oldest update (the age of the oldest figures on the page), and
 * an error naming every store whose last refresh failed. A poll store keeps its earlier data when a refresh fails, so
 * without this the page shows old figures beside a fresh "updated".
 */
export function combinePolls(parts: Record<string, PollState<unknown>>): CombinedPoll {
  const states = Object.entries(parts);
  const failed = states.filter(([, s]) => s.error).map(([name]) => name);
  const times = states.map(([, s]) => s.lastUpdated);
  return {
    data: states.every(([, s]) => s.data !== null) ? true : null,
    error: failed.length ? new Error(`${nameList(failed)} failed`) : null,
    loading: states.some(([, s]) => s.loading),
    lastUpdated: times.some((t) => t === null) ? null : Math.min(...(times as number[])),
    failed,
  };
}
