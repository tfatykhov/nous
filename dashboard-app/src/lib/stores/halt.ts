import { derived, get, writable } from 'svelte/store';
import { apiRequest, type ApiAnswerError } from '../api';
import { makePollStore } from './registry';
import { haltAnswered, haltFailed, haltView, resumeRefused, type HaltOutcome } from '../halt';
import type { BackgroundHaltAnswer, BackgroundResumeAnswer, BackgroundState } from '../types/api';

/**
 * Goals PR 0c: the background halt, read every 15 s for the whole app (started in App.svelte beside the attention
 * poll). No retry: an outage is said at the next poll, not after a backoff.
 */
export const haltPoll = makePollStore<BackgroundState>(
  (signal) => apiRequest<BackgroundState>('/background/state', 'GET', undefined, signal),
  15_000,
);

/** The press in flight and what the last one left to say, shared by both placements of the control and the banner. */
export const haltAction = writable<{ pending: 'halt' | 'resume' | null; outcome: HaltOutcome | null }>({
  pending: null,
  outcome: null,
});

export const haltState = derived([haltPoll, haltAction], ([$poll, $action]) =>
  haltView({ state: $poll.data, unreachable: $poll.error !== null, ...$action }),
);

/** One tap halts: no confirm step. A tap while a press is in flight sends nothing. The state is read again at once,
 * and "Stopping" holds until it lands. */
export async function haltNow(): Promise<void> {
  if (get(haltAction).pending) return;
  haltAction.set({ pending: 'halt', outcome: null });
  let outcome: HaltOutcome;
  try {
    outcome = haltAnswered(
      await apiRequest<BackgroundHaltAnswer>('/background/halt', 'POST', { actor: 'dashboard', surface: 'dashboard' }),
    );
  } catch {
    outcome = haltFailed();
  }
  await haltPoll.refresh();
  haltAction.set({ pending: null, outcome });
}

/** Resume. The login proxy adds the secret; the dashboard sends none. */
export async function resumeNow(): Promise<void> {
  if (get(haltAction).pending) return;
  haltAction.set({ pending: 'resume', outcome: null });
  let outcome: HaltOutcome | null = null;
  try {
    await apiRequest<BackgroundResumeAnswer>('/background/resume', 'POST', { actor: 'dashboard' });
  } catch (err) {
    outcome = resumeRefused(err as ApiAnswerError);
  }
  await haltPoll.refresh();
  haltAction.set({ pending: null, outcome });
}
