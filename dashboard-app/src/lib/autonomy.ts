/**
 * Pure helpers for the Autonomy view (GET /dashboard/autonomy).
 * No DOM, no fetch — unit-tested in autonomy.test.ts.
 */
import { fmtWhen, type Verdict } from './harness';
import type { AutonomyData } from './types/api';

/** The nous-scorer service starts a pass every 15 minutes (docker-compose.yml). */
export const SCORER_INTERVAL_MIN = 15;
/** Three missed passes: the scorer stopped, or cannot write its heartbeat. */
export const SCORER_STALE_MIN = 3 * SCORER_INTERVAL_MIN;

type Scorer = AutonomyData['task_scores']['scorer'];

/** Derived from the scorer's heartbeat row, never stored. */
export function scorerHealth(scorer: Scorer, now: number = Date.now()): Verdict {
  if (!scorer) {
    return {
      title: 'Scorer has not run yet',
      detail: 'No pass has written its heartbeat. Scores appear after the first pass of the nous-scorer service.',
      tone: 'warn',
    };
  }
  const ageMin = (now - new Date(scorer.last_run_at).getTime()) / 60000;
  const failed = scorer.last_status !== 'ok';
  const lastOk = scorer.last_ok_at ? ` Last successful pass ${fmtWhen(scorer.last_ok_at, now)}.` : ' No pass has succeeded yet.';
  if (ageMin > SCORER_STALE_MIN) {
    return {
      title: 'Scorer looks stopped',
      detail: `Last pass ${fmtWhen(scorer.last_run_at, now)}${failed ? ` (${scorer.last_error ?? 'failed'})` : ''}. `
        + `It runs every ${SCORER_INTERVAL_MIN} min, so no pass for ${SCORER_STALE_MIN} min means it stopped or cannot write.`,
      tone: 'warn',
    };
  }
  if (failed) {
    return {
      title: 'Last pass failed',
      detail: `${scorer.last_error ?? 'Unknown error'} at ${fmtWhen(scorer.last_run_at, now)}.${lastOk}`,
      tone: 'warn',
    };
  }
  const rows = scorer.rows_submitted == null ? '' : `, ${scorer.rows_submitted} rows`;
  return { title: 'Scorer running', detail: `Last pass ${fmtWhen(scorer.last_run_at, now)}${rows}.`, tone: 'ok' };
}

/** A 0–1 share as a whole percent; "—" when there was nothing to divide. */
export function pct(share: number | null | undefined): string {
  return share == null ? '—' : `${Math.round(share * 100)}%`;
}

/** A count with thousands separators: 18,310. */
export function count(n: number): string {
  return n.toLocaleString('en-US');
}

/** A score (−1 to 1) to two decimals; "—" when absent. */
export function score(value: number | null | undefined): string {
  return value == null ? '—' : value.toFixed(2);
}

/** Rows for a bar list: each count against the largest, in the given order. */
export function bars(entries: [string, number][]): { key: string; count: number; pct: number }[] {
  const max = Math.max(1, ...entries.map(([, n]) => n));
  return entries.map(([key, count]) => ({ key, count, pct: Math.round((count / max) * 100) }));
}

/**
 * Where subtask coverage is counted from: the endpoint counts from the later of the window's start and the
 * scorer's first run (``floor = max(since, first_scored_at)``), so name whichever one it is.
 */
export function coverageFrom(firstScoredAt: string | null, windowDays: number, now: number = Date.now()): string {
  const windowStart = now - windowDays * 86_400_000;
  if (firstScoredAt && new Date(firstScoredAt).getTime() > windowStart) {
    return `Since the scorer's first run (${fmtWhen(firstScoredAt, now)})`;
  }
  return `In the last ${windowDays} d`;
}

/** task_scoring.OUTCOMES, in display order (verified first); every lookup is by key. */
export const OUTCOMES = ['done_verified', 'done_self', 'partial', 'failed', 'not_scored'] as const;

export const OUTCOME_LABEL: Record<string, string> = {
  done_verified: 'Done, verified',
  done_self: 'Done, self-reported',
  partial: 'Partial',
  failed: 'Failed',
  not_scored: 'Not scored',
};

export const ROLLUP_LABEL: Record<string, string> = { dag: 'DAG runs', intention: 'Intention roots' };

/** autonomy_inventory.context_bucket: who was in the loop when a tool ran. */
export const CONTEXT_LABEL: Record<string, string> = {
  owner_present: 'You were there',
  approved: 'You approved',
  autonomous: 'On its own',
};

export const LEVEL_LABEL: Record<string, string> = {
  L0: 'Always with you',
  acts_without_asking: 'Acts without asking',
};
