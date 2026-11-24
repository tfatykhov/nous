import { derived, writable } from 'svelte/store';
import { apiGet } from '../api';
import { makePollStore } from './registry';
import type { AttentionData } from '../types/api';

/**
 * Harness dashboard §3.4 / §4.6: the counts behind the nav badges, the
 * mobile menu dot and the Overview strip. One poll (60 s) for the whole app;
 * a tab that has just loaded fresher numbers pushes them here, so a badge can
 * never disagree with the tab it links to for longer than one poll.
 */
export const attentionPoll = makePollStore<AttentionData>(
  (signal) => apiGet<AttentionData>('/dashboard/attention', { signal }),
  60_000,
);

type Stamped = { value: number; at: number };
type Overrides = { questions?: Stamped; sends?: Stamped; goals?: Stamped };
export const attentionOverride = writable<Overrides>({});

/** `goals`: the approval requests and questions the goals' sessions wait on the owner for (the Goals badge). */
export function pushCounts(counts: { questions?: number; sends?: number; goals?: number }): void {
  const at = Date.now();
  attentionOverride.update((o) => ({
    ...o,
    ...(counts.questions !== undefined ? { questions: { value: counts.questions, at } } : {}),
    ...(counts.sends !== undefined ? { sends: { value: counts.sends, at } } : {}),
    ...(counts.goals !== undefined ? { goals: { value: counts.goals, at } } : {}),
  }));
}

/** A count the server could not read (`goal_asks_waiting: null`) is 0 here: no badge. */
export function mergeCounts(
  data: Pick<AttentionData, 'questions_waiting' | 'sends_in_doubt' | 'goal_asks_waiting'> | null,
  polledAt: number | null,
  override: Overrides,
): { questions: number; sends: number; goals: number } {
  const pick = (polled: number, o: Stamped | undefined) =>
    o && (polledAt === null || o.at >= polledAt) ? o.value : polled;
  return {
    questions: pick(data?.questions_waiting ?? 0, override.questions),
    sends: pick(data?.sends_in_doubt ?? 0, override.sends),
    goals: pick(data?.goal_asks_waiting ?? 0, override.goals),
  };
}

export const attentionCounts = derived([attentionPoll, attentionOverride], ([$poll, $o]) =>
  mergeCounts($poll.data, $poll.lastUpdated, $o),
);
