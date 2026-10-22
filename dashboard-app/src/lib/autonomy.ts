/**
 * Pure helpers for the Autonomy view (GET /dashboard/autonomy).
 * No DOM, no fetch — unit-tested in autonomy.test.ts.
 */
import { fmtWhen, type Verdict } from './harness';
import type { AutonomyData, JobFixRow } from './types/api';

/** The nous-scorer service starts a pass every 15 minutes (docker-compose.yml). */
export const SCORER_INTERVAL_MIN = 15;
/** Three missed passes: the scorer stopped, or cannot write its heartbeat. */
export const SCORER_STALE_MIN = 3 * SCORER_INTERVAL_MIN;

type Scorer = AutonomyData['task_scores']['scorer'];

/**
 * Derived from the scorer's heartbeat row, never stored. ``expectedVersion`` is the version the endpoint reads
 * (``task_scores.scorer_version``): a scorer on another version writes rows the endpoint filters out.
 */
export function scorerHealth(scorer: Scorer, expectedVersion: number, now: number = Date.now()): Verdict {
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
  if (scorer.scorer_version !== expectedVersion) {
    return {
      title: 'Scorer version differs',
      detail: `The scorer writes version ${scorer.scorer_version}; this deployment reads version ${expectedVersion}, `
        + 'so its scores do not appear here. Rebuild and restart the nous-scorer service with the same release.',
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
export const OUTCOMES = ['done_verified', 'done_checked', 'done_self', 'partial', 'failed', 'not_scored'] as const;

export const OUTCOME_LABEL: Record<string, string> = {
  done_verified: 'Done, verified',
  done_checked: 'Done, checked',
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

// ── Check and fix (job_fixes): nous/handlers/job_fixes.py's states, triggers, verdicts and stop reasons ──

/** The fixer still has a step to take: it is checking, judging a change, or finishing a stop. */
export const FIX_LIVE = ['running', 'judging', 'stopping'];

export const FIX_TRIGGER_LABEL: Record<string, string> = {
  fails_often: 'Failed often',
  lesson_retired: 'A lesson did not help',
};

export const FIX_VERDICT_LABEL: Record<string, string> = {
  changed_job: 'Changed the instructions',
  fixed_elsewhere: 'Fixed outside the job',
  needs_owner: 'Needs you',
  no_fault_found: 'Found nothing wrong',
};

export const FIX_STOP_LABEL: Record<string, string> = {
  needs_owner: 'the check needs you',
  change_refused: 'the review refused the change',
  did_not_help: 'the change did not help',
  no_fault_found: 'nothing found and it still fails',
  unreadable_report: "the check's report could not be read",
  fails_again: 'it started failing again',
  inactive: 'you stopped the job',
};

export type FixTone = 'live' | 'ok' | 'muted' | 'warn';

/** The state as the owner reads it; a stop says who stopped the job and whether the owner was told. */
export function fixState(row: JobFixRow): { label: string; tone: FixTone } {
  switch (row.state) {
    case 'running': return { label: 'Checking', tone: 'live' };
    case 'judging': return { label: 'Judging the change', tone: 'live' };
    case 'stopping': return { label: 'Stopping', tone: 'live' };
    case 'kept': return { label: 'Fix kept', tone: 'ok' };
    case 'closed': return { label: 'Closed', tone: 'muted' };
    case 'stopped':
      if (row.stop_reason === 'inactive') return { label: 'Stopped by you', tone: 'muted' };
      return { label: row.reported ? 'Stopped, you were told' : 'Stopped', tone: 'warn' };
    default: return { label: 'Unreadable record', tone: 'muted' };
  }
}

/** One line on where the attempt stands or how it ended. */
export function fixOutcome(row: JobFixRow): string {
  const verdict = row.verdict ? FIX_VERDICT_LABEL[row.verdict] ?? row.verdict : null;
  if (row.state === 'running') return 'Check run in progress';
  if (row.state === 'judging') {
    const s = row.since_change;
    if (!s) return verdict ?? 'Judging';
    const runs = `${s.scored} of ${s.needed} runs since the change, ${s.bad} went wrong`;
    return s.scored >= s.needed ? `${runs}; judged at the next sleep` : runs;
  }
  if (row.state === 'stopping' || row.state === 'stopped') {
    const why = row.stop_reason ? FIX_STOP_LABEL[row.stop_reason] ?? row.stop_reason : null;
    const head = row.state === 'stopping' ? 'Stopping' : 'Stopped';
    return why ? `${head}: ${why}` : head;
  }
  if (row.state === 'kept') return verdict ? `${verdict}; later runs fail less` : 'Later runs fail less';
  if (row.state === 'closed') return 'Found nothing wrong; the job no longer failed often';
  return '—';
}

/** The check's own words, in reading order. ``new_instructions`` shows only when no changed text stands. */
export const FIX_TEXTS: [keyof Omit<JobFixRow['detail'], 'check'>, string][] = [
  ['why', 'What was wrong'],
  ['what_changed', 'What it fixed outside the job'],
  ['diagnosis', 'Diagnosis'],
  ['owner_action', 'What it asks of you'],
  ['checked', 'What it checked'],
  ['reason', 'Why the review refused the change'],
  ['new_instructions', 'Proposed instructions'],
];

/** "4 of 5 runs went wrong" before the check; null without a usable baseline. */
export function fixBaseline(row: JobFixRow): string | null {
  const b = row.baseline;
  return b ? `${b.bad} of ${b.scored} runs went wrong before` : null;
}

/** The record's newest time: settled, else applied, else started. */
export function fixLatest(row: JobFixRow): string | null {
  const times = [row.settled_at, row.applied_at, row.started_at].filter((t): t is string => !!t);
  if (times.length === 0) return null;
  return times.reduce((a, b) => (new Date(b).getTime() > new Date(a).getTime() ? b : a));
}
