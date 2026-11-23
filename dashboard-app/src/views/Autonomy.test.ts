import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Autonomy from './Autonomy.svelte';
import { markupOf, parseBlocks, styleBlocks } from '../lib/testing/css';

// Render smoke for the Autonomy view. The fixture has the shape
// nous/api/autonomy_dashboard.py returns; every value is made up.

const stats = (over: Record<string, unknown> = {}) => ({
  count: 40, mean: 0.31, p10: -0.5, p50: 0.5, p90: 1, overrides: 2, ...over,
});
const none = { count: 0, mean: null, p10: null, p50: null, p90: null, overrides: 0 };
const outcomes = (over: Record<string, number> = {}) => ({
  done_self: 0, done_verified: 0, done_checked: 0, partial: 0, failed: 0, not_scored: 0, ...over,
});
const penalties = { harness_errors: 1, refusals: 2, unverified_claims: 0, owner_overrides: 1 };

const FULL = () => ({
  window_days: 7,
  taps: { total: 6, by_action: [{ action_name: 'heartbeat.ack', count: 4 }, { action_name: 'decision.grade', count: 2 }] },
  triage_taps: { heartbeat: 4, decision: 2 },
  approval_cards_asked: 3,
  decision_grades: [{ reviewer: 'a2ui', outcome: 'success', count: 2 }],
  task_scores: {
    scorer_version: 1,
    first_scored_at: new Date(Date.now() - 60 * 60000).toISOString(),
    last_scored_at: new Date(Date.now() - 5 * 60000).toISOString(),
    audits_without_action_row: 0,
    settled_window: { from: '2026-10-02T21:00:00+00:00', to: '2026-10-09T21:00:00+00:00', held: 0 },
    scorer: {
      scorer_version: 1, last_run_at: new Date(Date.now() - 5 * 60000).toISOString(), last_status: 'ok',
      last_error: null, last_ok_at: new Date(Date.now() - 5 * 60000).toISOString(), rows_submitted: 1614,
    },
    headline: {
      verified_share: { provisional: { scored: 40, verified: 18, share: 0.45 }, settled: { scored: 0, verified: 0, share: null } },
      owner_overrides: { total: 5, subtasks: 2, rollups: 3 },
      harness_penalties: { harness_errors: 7, refusals: 4, unverified_claims: 1 },
    },
    subtasks: {
      finished: 50, covered: 44, coverage: 0.88,
      by_outcome: outcomes({ done_verified: 18, done_self: 15, failed: 7, not_scored: 4 }),
      provisional: stats(), settled: none,
    },
    rollups: {
      dag: { count: 9, by_outcome: outcomes({ done_verified: 6, failed: 3 }), own_penalties: penalties },
      intention: { count: 12, by_outcome: outcomes({ done_self: 12 }), own_penalties: penalties },
    },
  },
  inventory: {
    days: 30,
    tools: [
      { tool_name: 'bash', total: 120, by_status: { success: 110, error: 8, blocked: 2, other: 0 },
        by_context: { owner_present: 60, approved: 0, autonomous: 50 }, side_effects: { write: 100, external: 10 },
        compensable: false, de_facto_level: 'acts_without_asking' },
      { tool_name: 'write_file', total: 10, by_status: { success: 10, error: 0, blocked: 0, other: 0 },
        by_context: { owner_present: 10, approved: 0, autonomous: 0 }, side_effects: { write: 10 },
        compensable: true, de_facto_level: 'L0' },
      { tool_name: 'send_email', total: 3, by_status: { success: 0, error: 3, blocked: 0, other: 0 },
        by_context: { owner_present: 0, approved: 0, autonomous: 0 }, side_effects: {},
        compensable: false, de_facto_level: 'L0' },
    ],
    censors: { days: 30, by_provenance: { auto: { created: 2, deactivated: 1 }, agent: { created: 0, deactivated: 0 },
      human: { created: 1, deactivated: 0 } } },
  },
  events_persisted: true,
  warn_classes: [{ mode: 'warn', context_kind: 'heartbeat_triage', violation: 'level:external', tool_name: 'bash', count: 138 }],
  job_fixes: { enabled: true, jobs: [JUDGING(), STOPPED()] },
  guards: GUARDS(),
});

const guardRecord = (over: Record<string, unknown>) => ({
  at: new Date(Date.now() - 3600_000).toISOString(), kind: 'would_skip', schedule_id: 'ef'.repeat(16),
  subtask_id: '12'.repeat(16), check_id: null, name: 'ledger_sweep', mode: 'shadow', reason: null, exit_code: 0, timed_out: false,
  duration_ms: 41, stdout_bytes: 0, stderr_bytes: 0, stderr_head: '', command: 'bash /x/sweep.sh', acted: false,
  acted_by: [], observed: true, ...over,
});
const guardSchedule = (over: Record<string, unknown>) => ({
  schedule_id: 'ef'.repeat(16), job: 'Run <i>the</i> ledger sweep', name: 'ledger_sweep', mode: 'shadow', allowlisted: true,
  runs: 168, guard_runs: 160, skipped: 0, would_skip: 150, would_skip_unrecorded: 0, would_skip_unobserved: 0,
  fell_through: { output: 6, stderr: 1, exit: 0, timeout: 0, error: 3, not_allowlisted: 0, invalid: 0 },
  shadow_misses: 0, shadow_misses_by_signal: { report: 0, finding: 0, inbox: 0, push: 0, ledger: 0 }, last_runs: [],
  ...over,
});
const GUARDS = (over: Record<string, unknown> = {}) => ({
  enabled: true, bus_enabled: true, window_days: 7,
  schedules: [guardSchedule({})],
  runs: 168, guard_runs: 160, skipped: 0, would_skip: 150, would_skip_unrecorded: 0, would_skip_unobserved: 0,
  fell_through: { output: 6, stderr: 1, exit: 0, timeout: 0, error: 3, not_allowlisted: 0, invalid: 0 },
  shadow_misses: 0, shadow_misses_by_signal: { report: 0, finding: 0, inbox: 0, push: 0, ledger: 0 }, other: null, checks: [],
  recent: [
    guardRecord({ acted: true, acted_by: ['report', 'ledger'] }),
    guardRecord({ kind: 'fell_through', reason: 'stderr', stderr_bytes: 20, stderr_head: '<script>alert(1)</script>', subtask_id: '34'.repeat(16) }),
  ],
  ...over,
});

const fixRow = (over: Record<string, unknown>) => ({
  schedule_id: 'ab'.repeat(16), job: 'Run the grid tick', active: true, state: 'running', trigger: 'fails_often',
  verdict: null, stop_reason: null, started_at: new Date(Date.now() - 3 * 86_400_000).toISOString(),
  applied_at: null, settled_at: null, stopped_at: null, fix_run: '1f'.repeat(16), reported: false, baseline: null, since_change: null,
  detail: {}, old_task: null, task: null, ...over,
});
const JUDGING = () => fixRow({
  state: 'judging', verdict: 'changed_job', applied_at: new Date(Date.now() - 86_400_000).toISOString(),
  baseline: { bad: 4, scored: 5 }, since_change: { scored: 2, bad: 1, needed: 5 },
  detail: { why: 'the report path moved', new_instructions: 'Check /new/path' },
  job: 'Check /old/path', old_task: 'Check /old/path', task: 'Check /new/path',
});
const STOPPED = () => fixRow({
  schedule_id: 'cd'.repeat(16), job: 'Post the digest', active: false, state: 'stopped', verdict: 'needs_owner',
  stop_reason: 'needs_owner', reported: true, stopped_at: new Date(Date.now() - 3600_000).toISOString(),
  detail: { diagnosis: 'the token <b>expired</b>', owner_action: 'renew the token', check: { status: 'completed', outcome: 'completed' } },
});

// GET /dashboard/loop-numbers (nous/api/loop_numbers_dashboard.py): off unless a test turns it on.
const loopUnit = (over: Record<string, unknown> = {}) => ({
  done: 40, not_done: 6, tokens_in: { done: 400_000, not_done: 90_000 }, tokens_out: { done: 40_000, not_done: 6_000 },
  tokens_per_finished: 13_400, output_per_finished: 1_150, done_tokens: { mean: 11_000, p50: 9_500 },
  by_final_outcome: { completed: 40, timed_out: 6 },
  outside_check: { confirmed: 2, unconfirmed: 30, taken_back: 3, unscored: 5, unconfirmed_share: 0.9375, provisional: 30, flagged: 1 },
  ...over,
});
const loopWindow = (over: Record<string, unknown> = {}) => ({
  from: '2026-11-15T00:00:00+00:00', to: '2026-11-22T00:00:00+00:00', truncated: false,
  tasks: loopUnit(),
  workflow_steps: loopUnit({ done: 0, not_done: 2, tokens_per_finished: null, output_per_finished: null,
    tokens_in: { done: 0, not_done: 7_000 }, tokens_out: { done: 0, not_done: 500 },
    outside_check: { confirmed: 0, unconfirmed: 0, taken_back: 0, unscored: 0, unconfirmed_share: null, provisional: 0, flagged: 0 } }),
  goal_sessions: { sessions: 5, sessions_without_step: 2, steps_closed: 4, spent: 200_000, spent_without_step: 30_000,
    tokens_per_finished: 50_000, outside_check: null },
  ...over,
});
const LOOP_ON = () => ({
  enabled: true, window_days: 7, counts_cached_input: false, scorer_ran: true, claims_recorded: true,
  notes: ['tokens_leave_out_cached_input', 'goal_session_tokens_include_tasks_and_workflows'],
  current: loopWindow(),
  previous: loopWindow({ tasks: loopUnit({ done: 30, tokens_per_finished: 15_000, output_per_finished: 1_300,
    tokens_in: { done: 300_000, not_done: 120_000 }, tokens_out: { done: 30_000, not_done: 5_000 },
    outside_check: { confirmed: 3, unconfirmed: 27, taken_back: 0, unscored: 0, unconfirmed_share: 0.9, provisional: 0, flagged: 0 } }) }),
});

let body: Record<string, unknown> = FULL();
let status = 200;
let urls: string[] = [];
let loopBody: Record<string, unknown> = { enabled: false };
let loopStatus = 200;
let loopUrls: string[] = [];
// F118 PR 3: the second judge's settings card asks once; off ({enabled: false}) unless a test says otherwise.
let judgeBody: Record<string, unknown> = { enabled: false };
let judgeUrls: string[] = [];

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.startsWith('/dashboard/loop-numbers')) {
      loopUrls.push(url);
      return { ok: loopStatus < 400, status: loopStatus, json: async () => loopBody } as Response;
    }
    if (url.startsWith('/dashboard/outside-judge/second')) {
      judgeUrls.push(url);
      return { ok: true, status: 200, text: async () => JSON.stringify(judgeBody) } as Response;
    }
    if (!url.startsWith('/dashboard/autonomy')) throw new Error(`unexpected ${url}`);
    urls.push(url);
    // A 4xx is not retried by apiGet, so the failure surfaces at once.
    return { ok: status < 400, status, json: async () => body } as Response;
  }));
}

describe('Autonomy view', () => {
  beforeEach(() => { body = FULL(); status = 200; urls = []; loopBody = { enabled: false }; loopStatus = 200; loopUrls = []; install(); });
  // globals: false (vite.config.ts): testing-library cannot register its own cleanup.
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('renders every section from the endpoint', async () => {
    const { container } = render(Autonomy);
    expect(await screen.findByText('Scorer running')).toBeTruthy();
    expect(urls[0]).toBe('/dashboard/autonomy?days=7');
    const text = container.textContent ?? '';
    expect(text).toContain('1614 rows');
    expect(screen.getByText('45%')).toBeTruthy(); // provisional verified share
    expect(text).toContain('Provisional: 18 of 40 scored subtasks');
    expect(text).toContain('2 on subtasks · 3 on DAG runs and intentions');
    expect(text).toMatch(/Since the scorer's first run \([^)]*\): 44 of 50 finished subtasks have a score row \(88%\)/);
    // values sit beside their own labels
    const valueOf = (label: string) => screen.getByText(label, { selector: 'dt' }).nextElementSibling?.textContent;
    expect(valueOf('Approval cards asked')).toBe('3');
    expect(valueOf('Taps in the companion')).toBe('6');
    expect(valueOf('Harness errors')).toBe('7');
    expect(valueOf('Refusals')).toBe('4');
    expect(valueOf('Unverified claims')).toBe('1');
    // DAG runs row: count, outcomes in display order (verified, checked, self-reported, partial, failed, not scored),
    // then its own harness errors, refusals, unverified claims and overrides
    const dagRow = screen.getByText('DAG runs').closest('tr')!;
    expect(Array.from(dagRow.querySelectorAll('td')).map((td) => td.textContent)).toEqual(
      ['9', '6', '0', '0', '0', '3', '0', '1', '2', '0', '1']);
    expect(screen.getByText('no successful run')).toBeTruthy(); // L0 with zero successes is not "always with you"
    expect(screen.getByText('DAG runs')).toBeTruthy();
    expect(screen.getByText('Intention roots')).toBeTruthy();
    expect(text).toContain('Approval cards asked');
    expect(text).toContain('heartbeat.ack');
    expect(text).toContain('1 of 3 tools ran at least once without you in the loop.');
    expect(screen.getAllByText('Acts without asking').length).toBeGreaterThan(0);
    expect(text).toContain('Last 30 d, whatever the window');
    expect(text).toContain('level:external');
    expect(text).toContain('138');
    expect(text).not.toMatch(/NaN|undefined/);
  });

  it('names the settled window, which ends 14 days ago, and the subtasks still held in it', async () => {
    const { container } = render(Autonomy);
    await screen.findByText('Scorer running');
    const text = container.textContent ?? '';
    expect(text).toContain('Settled reads subtasks that finished Oct 2 – Oct 9, a window as long as this one that ends 14 days ago.');
    expect(text).toContain('Scores (−1 to 1) · settled: Oct 2 – Oct 9');
    expect(text).not.toContain('not settled');
    for (const [held, line] of [
      [1, '1 of them is not settled, so not counted as settled.'],
      [2, '2 of them are not settled, so not counted as settled.'],
    ] as const) {
      cleanup();
      const full = FULL();
      body = { ...full, task_scores: { ...full.task_scores, settled_window: { ...full.task_scores.settled_window, held } } };
      const again = render(Autonomy);
      await screen.findByText('Scorer running');
      expect(again.container.textContent).toContain(line);
    }
  });

  it('fetches once on mount', async () => {
    render(Autonomy);
    await screen.findByText('Scorer running');
    await new Promise((r) => setTimeout(r, 50));
    expect(urls).toEqual(['/dashboard/autonomy?days=7']);
  });

  it('names the window when the scorer is older than it', async () => {
    const full = FULL();
    body = { ...full, task_scores: { ...full.task_scores, first_scored_at: new Date(Date.now() - 20 * 86_400_000).toISOString() } };
    const { container } = render(Autonomy);
    await screen.findByText('Scorer running');
    expect(container.textContent).toContain('In the last 7 d: 44 of 50 finished subtasks have a score row (88%)');
  });

  it('fetches the chosen window', async () => {
    render(Autonomy);
    await screen.findByText('Scorer running');
    await fireEvent.click(screen.getByRole('button', { name: '30 d' }));
    await waitFor(() => expect(urls).toContain('/dashboard/autonomy?days=30'));
  });

  it('says "not measured" when events are not persisted, not "nothing flagged"', async () => {
    body = { ...FULL(), events_persisted: false, warn_classes: [] };
    const { container } = render(Autonomy);
    expect(await screen.findByText(/Not measured — event persistence is off/)).toBeTruthy();
    expect(container.textContent).not.toContain('Nothing flagged in this window.');
  });

  it('shows dashes, not NaN or null, before the scorer has run', async () => {
    const full = FULL();
    body = {
      ...full,
      taps: { total: 0, by_action: [] },
      decision_grades: [],
      task_scores: {
        ...full.task_scores, first_scored_at: null, last_scored_at: null, scorer: null,
        headline: {
          verified_share: { provisional: { scored: 0, verified: 0, share: null }, settled: { scored: 0, verified: 0, share: null } },
          owner_overrides: { total: 0, subtasks: 0, rollups: 0 },
          harness_penalties: { harness_errors: 0, refusals: 0, unverified_claims: 0 },
        },
        subtasks: { finished: null, covered: null, coverage: null, by_outcome: outcomes(), provisional: none, settled: none },
      },
      inventory: { ...full.inventory, tools: [] },
      warn_classes: [],
    };
    const { container } = render(Autonomy);
    expect(await screen.findByText('Scorer has not run yet')).toBeTruthy();
    expect(screen.getByText("Coverage is counted from the scorer's first run.")).toBeTruthy();
    expect(screen.getByText('No side-effecting tool ran in the last 30 days.')).toBeTruthy();
    expect(screen.getByText('No taps in this window.')).toBeTruthy();
    expect(container.textContent).not.toMatch(/NaN|undefined|null/);
  });

  it('flags a scorer that failed its last pass', async () => {
    const full = FULL();
    body = { ...full, task_scores: { ...full.task_scores,
      scorer: { ...full.task_scores.scorer, last_status: 'error', last_error: 'InsufficientPrivilege' } } };
    render(Autonomy);
    expect(await screen.findByText('Last pass failed')).toBeTruthy();
    expect(screen.getByText(/InsufficientPrivilege/)).toBeTruthy();
  });

  it('says nothing about spot-check answers when none lack their tap, and one plain line when some do', async () => {
    const full = FULL();
    render(Autonomy);
    expect(await screen.findByText('Done, verified', { selector: 'h2' })).toBeTruthy();
    expect(screen.queryByText(/spot-check answer/)).toBeNull();
    cleanup();
    body = { ...full, task_scores: { ...full.task_scores, audits_without_action_row: 2 } };
    render(Autonomy);
    expect(await screen.findByText(/2 spot-check answers on file with no matching tap/)).toBeTruthy();
  });

  it('flags a scorer on another version than the endpoint reads', async () => {
    const full = FULL();
    body = { ...full, task_scores: { ...full.task_scores, scorer_version: 2 } };
    render(Autonomy);
    expect(await screen.findByText('Scorer version differs')).toBeTruthy();
  });

  it('lists every check-and-fix record with its state and where it stands', async () => {
    const { container } = render(Autonomy);
    expect(await screen.findByText('Check and fix')).toBeTruthy();
    const text = container.textContent ?? '';
    expect(screen.getByText('Judging the change')).toBeTruthy();
    expect(text).toContain('2 of 5 runs since the change, 1 went wrong');
    expect(text).toContain('4 of 5 runs went wrong before');
    expect(screen.getByText('Stopped, you were told')).toBeTruthy();
    expect(text).toContain('Stopped: the check needs you');
    expect(text).not.toContain('Off (');
  });

  it("shows the check's own words as plain text, and the instructions before and after a change", async () => {
    const { container } = render(Autonomy);
    await screen.findByText('Check and fix');
    await fireEvent.click(screen.getByRole('button', { name: 'Show details for Post the digest' }));
    expect(screen.getByText('the token <b>expired</b>')).toBeTruthy(); // escaped, not markup
    expect(container.querySelector('.fix-text b')).toBeNull();
    expect(screen.getByText('renew the token')).toBeTruthy();
    expect(screen.getByText('completed / completed')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Show details for Check /old/path' }));
    expect(screen.getByText('Instructions now')).toBeTruthy();
    expect(screen.getByText('the report path moved')).toBeTruthy();
    expect(screen.queryByText('Proposed instructions')).toBeNull(); // the applied text shows once, as "now"
  });

  it('calls a change that was applied and then undone by the stop "tried, then restored"', async () => {
    const undone = fixRow({
      schedule_id: 'ef'.repeat(16), job: 'Sync the files', state: 'stopped', verdict: 'changed_job',
      stop_reason: 'did_not_help', reported: true, applied_at: new Date(Date.now() - 86_400_000).toISOString(),
      detail: { new_instructions: 'Sync the files twice' },
    });
    body = { ...FULL(), job_fixes: { enabled: true, jobs: [undone] } };
    render(Autonomy);
    await screen.findByText('Check and fix');
    await fireEvent.click(screen.getByRole('button', { name: 'Show details for Sync the files' }));
    expect(screen.getByText('Instructions tried, then restored')).toBeTruthy();
    expect(screen.getByText('Sync the files twice')).toBeTruthy();
    expect(screen.queryByText('Proposed instructions')).toBeNull();
  });

  it('says check-and-fix is off rather than "no job needed a check"', async () => {
    body = { ...FULL(), job_fixes: { enabled: false, jobs: [] } };
    const { container } = render(Autonomy);
    expect(await screen.findByText(/no check starts and no record below moves/)).toBeTruthy();
    expect(container.textContent).not.toContain('No job has needed a check.');
    cleanup();
    body = { ...FULL(), job_fixes: { enabled: true, jobs: [] } };
    render(Autonomy);
    expect(await screen.findByText(/No job has needed a check\./)).toBeTruthy();
  });

  it('shows each guarded job, the counts for the window and the latest records as plain text', async () => {
    const { container } = render(Autonomy);
    expect(await screen.findByRole('heading', { name: 'Run guards' })).toBeTruthy();
    const section = screen.getByRole('heading', { name: 'Run guards' }).closest('section')!;
    const text = section.textContent ?? '';
    expect(text).toContain('Run <i>the</i> ledger sweep'); // job text and command output are never markup
    expect(text).toContain('<script>alert(1)</script>');
    expect(section.querySelector('i, script')).toBeNull();
    expect(text).toContain('ledger_sweep');
    expect(text).toContain('Shadow');
    const valueOf = (label: string) => Array.from(section.querySelectorAll('dt')).find((dt) => dt.textContent === label)
      ?.nextElementSibling?.textContent;
    expect(valueOf('Runs')).toBe('168');
    expect(valueOf('Guard ran')).toBe('160');
    expect(valueOf('Would have skipped')).toBe('150');
    expect(valueOf('Skipped')).toBe('0');
    expect(text).toContain('Output 6');
    expect(text).toContain('stderr 1');
    expect(text).toContain('Error 3');
    expect(text).toContain('Model acted: report, ledger');
    expect(section.querySelector('[role="alert"]')).toBeNull();
    expect(container.textContent).not.toMatch(/NaN|undefined/);
  });

  it('makes shadow misses stand out, with the acting kinds behind them', async () => {
    body = { ...FULL(), guards: GUARDS({ shadow_misses: 3, shadow_misses_by_signal: { report: 3, finding: 1, inbox: 3, push: 0, ledger: 2 } }) };
    render(Autonomy);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('3 shadow misses');
    expect(alert.textContent).toContain('finding 1');
    expect(alert.textContent).toContain('ledger 2');
    // report and inbox come with every completed run: context in the records, not why a run is a miss
    expect(alert.textContent).not.toContain('report');
    expect(alert.textContent).not.toContain('inbox');
  });

  it('shows each guarded job with its own counts and shadow misses', async () => {
    body = { ...FULL(), guards: GUARDS({ shadow_misses: 3, schedules: [
      guardSchedule({ runs: 160, would_skip: 150, shadow_misses: 3 }),
      guardSchedule({ schedule_id: 'cd'.repeat(16), job: 'Check the grid bot', mode: 'skip', runs: 8, skipped: 7, would_skip: 0 }),
    ] }) };
    render(Autonomy);
    const row = async (job: string) => (await screen.findByText(job)).closest('tr')!;
    const cells = (tr: HTMLElement) => Array.from(tr.querySelectorAll('td')).slice(2).map((td) => td.textContent);
    const sweep = await row('Run <i>the</i> ledger sweep');
    expect(cells(sweep)).toEqual(['160', '0', '150', '3']);
    expect(sweep.querySelector('[data-testid="schedule-misses"]')!.classList.contains('acted')).toBe(true);
    const grid = await row('Check the grid bot');
    expect(cells(grid)).toEqual(['8', '7', '0', '0']);
    expect(grid.querySelector('[data-testid="schedule-misses"]')!.classList.contains('acted')).toBe(false);
  });

  it("shows each job's own last runs as plain text, even when another job fills the latest records", async () => {
    const hourly = Array.from({ length: 10 }, (_, i) => guardRecord({ subtask_id: `${i}`.padStart(32, 'a') }));
    const daily = guardRecord({
      at: new Date(Date.now() - 3 * 86_400_000).toISOString(), schedule_id: 'cd'.repeat(16), subtask_id: '9d'.repeat(16),
      kind: 'fell_through', reason: 'stderr', stderr_bytes: 30, stderr_head: '<script>daily()</script>',
      acted: true, acted_by: ['finding'],
    });
    body = { ...FULL(), guards: GUARDS({ recent: hourly, schedules: [
      guardSchedule({ last_runs: hourly }),
      guardSchedule({ schedule_id: 'cd'.repeat(16), job: 'Check the grid bot', mode: 'skip', last_runs: [daily] }),
    ] }) };
    const { container } = render(Autonomy);
    const caption = await screen.findByText(/Latest 10 records/);
    expect(caption.closest('table')!.textContent).not.toContain('<script>daily()</script>');
    const grid = (await screen.findByText('Check the grid bot')).closest('tr')!;
    const details = grid.nextElementSibling!.querySelector('details')!;
    expect(details.querySelector('summary')!.textContent).toBe('Last 1 run of this job');
    const [record] = Array.from(details.querySelectorAll(':scope tbody tr'));
    expect(record.textContent).toContain('Fell through: stderr');
    expect(record.textContent).toContain('Model acted: finding');
    expect(record.querySelector('pre')!.textContent).toBe('<script>daily()</script>');
    expect(container.querySelector('script')).toBeNull();
    const sweep = (await screen.findByText('Run <i>the</i> ledger sweep')).closest('tr')!;
    expect(sweep.nextElementSibling!.querySelector('summary')!.textContent).toBe('Last 10 runs of this job');
  });

  it('shows an unresolved guard with no mode, and a null mode of a guard that ran as unknown', async () => {
    body = { ...FULL(), guards: GUARDS({ recent: [
      guardRecord({ kind: 'fell_through', reason: 'not_allowlisted', name: 'nope', mode: null, exit_code: null, duration_ms: null, subtask_id: '56'.repeat(16) }),
      guardRecord({ kind: 'fell_through', reason: 'error', mode: null, subtask_id: '78'.repeat(16) }),
    ] }) };
    render(Autonomy);
    const caption = await screen.findByText(/Latest 2 records/);
    const [unresolved, odd] = Array.from(caption.closest('table')!.querySelectorAll('tbody tr'));
    expect(unresolved.querySelectorAll('td')[1].textContent).toBe('nope —');
    expect(unresolved.textContent).toContain('Fell through: Not allowlisted');
    expect(unresolved.textContent).not.toContain('Unknown mode');
    expect(odd.querySelectorAll('td')[1].textContent).toContain('Unknown mode: —');
  });

  it('says how many would-skip runs have no record of what the model did', async () => {
    body = { ...FULL(), guards: GUARDS({ would_skip_unrecorded: 2 }) };
    render(Autonomy);
    expect(await screen.findByText(/2 would-skip runs have no record of what the model did/)).toBeTruthy();
  });

  it('labels a run with no structured report "not observed", and counts those apart', async () => {
    body = { ...FULL(), guards: GUARDS({ would_skip_unobserved: 1, recent: [
      guardRecord({ acted: null, acted_by: ['inbox', 'push'], observed: false }),
      guardRecord({ acted: null, acted_by: null, observed: null, subtask_id: '9a'.repeat(16) }),
    ] }) };
    render(Autonomy);
    const caption = await screen.findByText(/Latest 2 records/);
    const [legacy, unrecorded] = Array.from(caption.closest('table')!.querySelectorAll('tbody tr'));
    expect(legacy.querySelectorAll('td')[4].textContent).toBe('not observed');
    expect(unrecorded.querySelectorAll('td')[4].textContent).toBe('—');
    expect(await screen.findByText(/1 would-skip run was not observed/)).toBeTruthy();
  });

  it('shows the records no guarded job holds in one Other row, so the rows add up to the totals', async () => {
    const orphan = guardRecord({ schedule_id: null, subtask_id: '0f'.repeat(16), stderr_head: '<b>gone</b>' });
    body = { ...FULL(), guards: GUARDS({ schedules: [], other: guardSchedule({
      runs: 3, skipped: 2, would_skip: 1, shadow_misses: 1, last_runs: [orphan],
    }) }) };
    render(Autonomy);
    const other = (await screen.findByText('Other (stopped, de-guarded or unresolved)')).closest('tr')!;
    expect(Array.from(other.querySelectorAll('td')).map((td) => td.textContent)).toEqual(['—', '—', '3', '2', '1', '1']);
    expect(other.querySelector('[data-testid="schedule-misses"]')!.classList.contains('acted')).toBe(true);
    const details = other.nextElementSibling!.querySelector('details')!;
    expect(details.querySelector('summary')!.textContent).toBe('Last 1 run in no row above');
    expect(details.querySelector('pre')!.textContent).toBe('<b>gone</b>');
    expect(details.querySelector('b')).toBeNull();
    expect(screen.queryByText(/No schedule has a guard\./)).toBeNull();
  });

  it('says guards are off, or that nothing is guarded yet', async () => {
    body = { ...FULL(), guards: GUARDS({ enabled: false }) };
    render(Autonomy);
    expect(await screen.findByText(/NOUS_SCHEDULE_GUARDS_ENABLED=false/)).toBeTruthy();
    cleanup();
    // the guards' own switch, not the response's events_persisted (F026 persistence)
    body = { ...FULL(), events_persisted: true, guards: GUARDS({ bus_enabled: false }) };
    render(Autonomy);
    expect(await screen.findByText(/NOUS_EVENT_BUS_ENABLED=false/)).toBeTruthy();
    cleanup();
    body = { ...FULL(), events_persisted: false, warn_classes: [] };
    render(Autonomy);
    await screen.findByRole('heading', { name: 'Run guards' });
    expect(screen.queryByText(/NOUS_EVENT_BUS_ENABLED=false/)).toBeNull();
    cleanup();
    body = { ...FULL(), guards: GUARDS({ schedules: [], recent: [], runs: 0, guard_runs: 0, would_skip: 0 }) };
    render(Autonomy);
    expect(await screen.findByText(/No schedule or heartbeat check has a guard\./)).toBeTruthy();
  });

  it('shows each guarded heartbeat check with its counts, misses and last runs', async () => {
    const tick = guardSchedule({
      runs: 288, would_skip: 280, shadow_misses: 1, schedule_id: undefined, job: undefined,
      fell_through: { output: 6, stderr: 0, exit: 1, timeout: 0, error: 0, not_allowlisted: 0, invalid: 1 },
      last_runs: [guardRecord({ kind: 'fell_through', reason: 'invalid', mode: null, name: 'gridbot_tick', schedule_id: null, subtask_id: null, check_id: 'c1'.repeat(16) })],
    });
    body = { ...FULL(), guards: GUARDS({ shadow_misses: 1, schedules: [], checks: [
      { ...tick, check_id: 'c1'.repeat(16), check: 'gridbot-paper-tick', enabled: true, timeout_seconds: 900, name: 'gridbot_tick' },
      { ...guardSchedule({ runs: 2, would_skip: 0, last_runs: [] }), check_id: 'c2'.repeat(16), check: 'old-check', enabled: true,
        timeout_seconds: 60, name: null, mode: null, allowlisted: null },
    ] }) };
    render(Autonomy);
    expect(await screen.findByText(/No schedule has a guard\./)).toBeTruthy();
    const tr = (await screen.findByText('gridbot-paper-tick')).closest('tr')!;
    expect(Array.from(tr.querySelectorAll('td')).slice(2).map((td) => td.textContent)).toEqual(['288', '0', '280', '1']);
    expect(tr.querySelector('[data-testid="check-misses"]')!.classList.contains('acted')).toBe(true);
    expect(screen.getByText('Last 1 run of this check')).toBeTruthy();
    expect(screen.getByText('Fell through: Refused (invalid)')).toBeTruthy();
    const old = (await screen.findByText('old-check')).closest('tr')!;
    expect(old.textContent).toContain('guard removed');
  });

  it('says so when the endpoint fails', async () => {
    status = 400;
    render(Autonomy);
    expect(await screen.findByText('Failed to load autonomy data — retrying…')).toBeTruthy();
  });
});

describe('Autonomy: what a finished job costs', () => {
  beforeEach(() => { body = FULL(); status = 200; urls = []; loopBody = LOOP_ON(); loopStatus = 200; loopUrls = []; install(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  const heading = async () => (await screen.findByRole('heading', { name: 'What a finished job costs' })).closest('section')!;
  const panel = async () => { render(Autonomy); return heading(); };
  const row = (section: HTMLElement, label: string) =>
    Array.from(section.querySelectorAll('tbody tr')).find((tr) => tr.querySelector('th')?.textContent?.startsWith(label)) as HTMLElement;
  const cells = (tr: HTMLElement) => Array.from(tr.querySelectorAll('td'));

  it('is not shown when the numbers are off', async () => {
    loopBody = { enabled: false };
    render(Autonomy);
    await screen.findByText('Scorer running');
    await waitFor(() => expect(loopUrls).toEqual(['/dashboard/loop-numbers?days=7']));
    expect(screen.queryByRole('heading', { name: 'What a finished job costs' })).toBeNull();
  });

  it('fetches the same window as the page, and again when it changes', async () => {
    await panel();
    expect(loopUrls).toEqual(['/dashboard/loop-numbers?days=7']);
    await fireEvent.click(screen.getByRole('button', { name: '30 d' }));
    await waitFor(() => expect(loopUrls).toContain('/dashboard/loop-numbers?days=30'));
  });

  it('shows three rows, each with the window before beside it', async () => {
    const section = await panel();
    const tasks = cells(row(section, 'Tasks'));
    expect(tasks[0].textContent).toContain('40');
    expect(tasks[0].textContent).toContain('before: 30');
    expect(tasks[1].textContent).toContain('13,400');
    expect(tasks[1].textContent).toContain('before: 15,000');
    expect(tasks[1].textContent).toContain('output only: 1,150');
    expect(tasks[2].textContent).toContain('96,000'); // 90,000 in and 6,000 out on work that did not finish
    expect(tasks[2].textContent).toContain('before: 125,000');
    expect(tasks[3].textContent).toContain('94%');
    expect(tasks[3].textContent).toContain('before: 90%');
    expect(row(section, 'Workflow steps')).toBeTruthy();
    const goals = cells(row(section, 'Goal sessions'));
    expect(goals[0].textContent).toContain('4');
    expect(goals[1].textContent).toContain('50,000');
    expect(goals[2].textContent).toContain('30,000');
    expect(section.querySelectorAll('tbody tr').length).toBe(3);
    expect(section.textContent).not.toMatch(/NaN|undefined|null/);
  });

  it('shows a dash for a number with nothing to divide, and says a goal session is not measured', async () => {
    const section = await panel();
    const steps = cells(row(section, 'Workflow steps'));
    expect(steps[1].textContent?.trim().startsWith('—')).toBe(true); // no finished step: no cost, not 0
    expect(steps[3].textContent?.trim().startsWith('—')).toBe(true); // nothing scored: no share, not 0%
    expect(steps[3].textContent).not.toContain('0%');
    expect(cells(row(section, 'Goal sessions'))[3].textContent).toContain('Not measured');
  });

  it('says in plain words what the numbers leave out', async () => {
    const section = await panel();
    const text = section.textContent ?? '';
    expect(text).toContain('Tokens leave out input that was read from or saved to the cache, the same way budgets count.');
    expect(text).toContain('A job counts as checked only when you graded it, a pull request it opened was merged by someone else, '
      + 'or your spot checks trust its kind of check. No outside check does not mean the job was wrong.');
    expect(text).toContain("A goal session's tokens include its tasks and workflows, so the rows overlap. Do not add them.");
    expect(text).not.toContain('The scorer has not run');
  });

  it('says when the scorer has not run, first among the notes: it is why the last column is dashes', async () => {
    loopBody = { ...LOOP_ON(), scorer_ran: false };
    const section = await panel();
    const notes = Array.from(section.querySelectorAll('.loop-notes p')).map((p) => p.textContent);
    expect(notes[0]).toBe('The scorer has not run, so nothing can be counted as checked yet.');
    expect(notes[1]).toMatch(/^A dash means/);
  });

  it('names the unit of every column, and each number cell carries its column\'s name for the stacked phone layout', async () => {
    const section = await panel();
    const heads = Array.from(section.querySelectorAll('thead th')).map((th) => th.textContent?.trim());
    expect(heads).toEqual(['', 'Finished', 'Tokens for each finished one', 'Tokens spent on work that did not finish', 'Finished with no outside check']);
    for (const tr of Array.from(section.querySelectorAll('tbody tr'))) {
      expect(cells(tr as HTMLElement).map((td) => td.dataset.label)).toEqual(heads.slice(1));
    }
  });

  it('under 640px each row is a small stacked block: the row name, then each number with its name, nothing to scroll', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'Autonomy.svelte'), 'utf-8'));
    const at = css.indexOf('@media (max-width: 640px)');
    expect(at).toBeGreaterThan(-1);
    const phone = parseBlocks(css.slice(at));
    expect(phone.get('.loop thead')).toMatchObject({ display: 'none' });
    expect(phone.get('.loop tbody tr')).toMatchObject({ display: 'block' });
    expect(phone.get('.loop tbody th')).toMatchObject({ display: 'block', position: 'static' });
    expect(phone.get('.loop tbody td')).toMatchObject({ display: 'grid', 'white-space': 'normal' });
    expect(phone.get('.loop tbody td::before')).toMatchObject({ content: 'attr(data-label)', 'grid-column': '1' });
  });

  it('with the numbers off it asks once on load, then never again: not on the timer, Refresh or a new window', async () => {
    loopBody = { enabled: false };
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(Autonomy);
      await screen.findByText('Scorer running');
      await waitFor(() => expect(loopUrls).toHaveLength(1));
      await vi.advanceTimersByTimeAsync(3 * 60_000);
      await waitFor(() => expect(urls.length).toBeGreaterThan(1)); // the scores kept their own poll
      await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(false));
      await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
      await fireEvent.click(screen.getByRole('button', { name: '30 d' }));
      await waitFor(() => expect(urls).toContain('/dashboard/autonomy?days=30'));
      expect(loopUrls).toEqual(['/dashboard/loop-numbers?days=7']);
    } finally {
      vi.useRealTimers();
    }
  });

  it('with the numbers on it keeps asking, with the page', async () => {
    render(Autonomy);
    await heading();
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(loopUrls.length).toBeGreaterThan(1));
  });

  it('when the panel is not shown the badge reads as the scores alone', async () => {
    const badge = (c: HTMLElement) => c.querySelector('.stale-badge') as HTMLElement;
    // the numbers failed: the badge does not name them
    loopStatus = 400;
    let view = render(Autonomy);
    await screen.findByText('Scorer running');
    await waitFor(() => expect(loopUrls.length).toBeGreaterThan(0));
    await waitFor(() => expect(badge(view.container).textContent).toMatch(/^\s*updated /));
    expect(badge(view.container).classList.contains('error')).toBe(false);
    cleanup();
    // the numbers are off and the scores fail: the badge is the scores' own, as before the panel existed
    loopStatus = 200;
    loopBody = { enabled: false };
    status = 400;
    view = render(Autonomy);
    await screen.findByText('Failed to load autonomy data — retrying…');
    await waitFor(() => expect(badge(view.container).textContent?.trim()).toBe('error — retrying'));
  });

  it('when the panel is shown and its numbers fail, the badge names them', async () => {
    const { container } = render(Autonomy);
    await heading();
    loopStatus = 400;
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(container.querySelector('.stale-badge')?.textContent).toContain('job costs failed'));
  });

  it('keeps the rest of the page when the numbers fail to load', async () => {
    loopStatus = 400;
    render(Autonomy);
    await screen.findByText('Scorer running');
    await waitFor(() => expect(loopUrls.length).toBeGreaterThan(0));
    expect(screen.queryByRole('heading', { name: 'What a finished job costs' })).toBeNull();
    expect(screen.queryByText('Failed to load autonomy data — retrying…')).toBeNull();
  });
});

// F118 PR 3: the second judge's settings card, under "What a finished job costs".
describe('Autonomy: a second opinion on decisions', () => {
  beforeEach(() => {
    body = FULL(); status = 200; urls = []; loopBody = LOOP_ON(); loopStatus = 200; loopUrls = [];
    judgeBody = { enabled: false }; judgeUrls = [];
    install();
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); judgeBody = { enabled: false }; });

  const JUDGE_ON = () => ({
    enabled: true, locked: null, loaded: true, running: true,
    on: { value: true, source: 'stored' }, model: { value: 'jev-1.14.0', source: 'server' },
    daily_calls: { value: 100, most: 100, source: 'server' },
    key: { source: 'stored', set_at: '2026-05-03T12:00:00Z', problem: null, server_has_key: false },
    can_store_key: true, sends_to: 'api.typesafe.ai', confirm_text: 'Send a copy?', updated_at: null, updated_by: null,
  });

  it('with the settings off it asks once on load, draws nothing, and never asks again', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(Autonomy);
      await screen.findByRole('heading', { name: 'What a finished job costs' });
      await waitFor(() => expect(judgeUrls).toEqual(['/dashboard/outside-judge/second']));
      await vi.advanceTimersByTimeAsync(3 * 60_000);
      await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(false));
      await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
      await fireEvent.click(screen.getByRole('button', { name: '30 d' }));
      await waitFor(() => expect(urls).toContain('/dashboard/autonomy?days=30'));
      expect(judgeUrls).toEqual(['/dashboard/outside-judge/second']);
      expect(screen.queryByRole('heading', { name: 'A second opinion on decisions' })).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('with the settings on the card is drawn under what a finished job costs', async () => {
    judgeBody = JUDGE_ON();
    render(Autonomy);
    const card = (await screen.findByRole('heading', { name: 'A second opinion on decisions' })).closest('section')!;
    const costs = (await screen.findByRole('heading', { name: 'What a finished job costs' })).closest('section')!;
    expect(costs.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(card.querySelector('[data-testid="jev-status"]')?.textContent).toBe('On');
    expect(card.textContent).toContain('A key is saved (3 May)');
  });

  it('a card that cannot be read leaves the rest of the page', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.startsWith('/dashboard/outside-judge/second')) throw new TypeError('network');
      if (url.startsWith('/dashboard/loop-numbers')) return { ok: true, status: 200, json: async () => loopBody } as Response;
      return { ok: true, status: 200, json: async () => body } as Response;
    }));
    render(Autonomy);
    expect(await screen.findByText('These settings could not be read. Reload the page to try again.')).toBeTruthy();
    expect(screen.getByText('Scorer running')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'What a finished job costs' })).toBeTruthy();
  });
});

// Redesign PR 4: Autonomy on paper. A bar, a legend swatch or a stacked share carries its token as a custom property
// (`--fill`), which the stylesheet paints: no hex in the script, and the same markup under both looks.
describe('Autonomy: colours by token', () => {
  beforeEach(() => { body = FULL(); status = 200; urls = []; loopBody = { enabled: false }; loopStatus = 200; loopUrls = []; install(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  const fillOf = (el: Element | null | undefined) => (el as HTMLElement | null)?.style.getPropertyValue('--fill');

  it('each outcome bar carries its outcome\'s token, beside its label', async () => {
    body = FULL();
    (body.task_scores as any).subtasks.by_outcome = outcomes({ done_verified: 4, done_checked: 3, done_self: 2, partial: 2, failed: 1, not_scored: 1 });
    render(Autonomy);
    await screen.findByText('Scorer running');
    const bar = (label: string) => screen.getByText(label, { selector: '.bar-key' }).closest('.bar-row')!.querySelector('.bar-fill');
    expect(fillOf(bar('Done, verified'))).toBe('var(--green)');
    expect(fillOf(bar('Done, checked'))).toBe('var(--series-2)');
    expect(fillOf(bar('Done, self-reported'))).toBe('var(--series-1)');
    expect(fillOf(bar('Partial'))).toBe('var(--warn-text)');
    expect(fillOf(bar('Failed'))).toBe('var(--red)');
    expect(fillOf(bar('Not scored'))).toBe('var(--muted)');
  });

  it('the taps bars are the violet series mark, not the accent (the question frame\'s blue on paper)', async () => {
    render(Autonomy);
    const key = await screen.findByText('heartbeat.ack', { selector: '.bar-key' });
    expect(fillOf(key.closest('.bar-row')!.querySelector('.bar-fill'))).toBe('var(--series-0)');
  });

  it('the "who was there" shares and their legend carry the context tokens', async () => {
    const { container } = render(Autonomy);
    await screen.findByText('Scorer running');
    expect([...container.querySelectorAll('.legend i')].map(fillOf)).toEqual(['var(--series-1)', 'var(--series-2)', 'var(--series-4)']);
    const stack = container.querySelector('.stack[aria-label^="You were there 60"]')!;
    expect([...stack.querySelectorAll('div')].map(fillOf)).toEqual(['var(--series-1)', 'var(--series-4)']); // bash: present and on its own
  });

  it('a word after an inline condition keeps its space (Svelte trims the space that opens an {#if})', async () => {
    const full = FULL();
    const g = full.guards as any;
    body = { ...full, task_scores: { ...full.task_scores, settled_window: { ...full.task_scores.settled_window, held: 2 } },
      guards: { ...g, schedules: [{ ...g.schedules[0], allowlisted: false }],
        checks: [{ ...guardSchedule({ last_runs: [] }), check_id: 'c1'.repeat(16), check: 'disk_usage', enabled: false,
          timeout_seconds: 60, name: 'df_guard', mode: 'shadow', allowlisted: false }] } };
    const { container } = render(Autonomy);
    await screen.findByText('Scorer running');
    const text = container.textContent ?? '';
    expect(text).toContain('that ends 14 days ago. 2 of them are not settled');
    expect(text).toContain('0 41 ms'); // exit code 0, then the duration: not "041 ms"
    expect(text).toContain('ledger_sweep not allowlisted');
    expect(text).toContain('disk_usage disabled');
    expect(text).toContain('df_guard not allowlisted');
  });

  it('only the all-number tables are set in the narrow width; the run-guard and grade tables, with words and pills, are not (review M6)', async () => {
    const { container } = render(Autonomy);
    await screen.findByText('Scorer running');
    const nums = [...container.querySelectorAll('table.nums')];
    expect(nums.map((t) => t.querySelector('caption, th')?.textContent?.trim())).toEqual(['Scores (−1 to 1) · settled: Oct 2 – Oct 9', '', 'Censors, last 30 d']);
    for (const t of container.querySelectorAll('table.guards')) expect(t.classList.contains('nums')).toBe(false);
    const grades = screen.getByText('Reviewer').closest('table')!;
    expect(grades.classList.contains('nums')).toBe(false);
  });

  it('the scorer strip\'s class follows the verdict\'s tone', async () => {
    const { container } = render(Autonomy);
    await screen.findByText('Scorer running');
    expect(container.querySelector('section.strip')?.classList.contains('verdict-ok')).toBe(true);
  });

  it('source contract: no inline background is left; the stylesheet paints --fill', () => {
    const src = readFileSync(join(__dirname, 'Autonomy.svelte'), 'utf-8');
    expect(markupOf(src)).not.toMatch(/style:background=/);
    const rules = parseBlocks(styleBlocks(src));
    for (const sel of ['.bar-fill', '.legend i', '.stack > div']) expect(rules.get(sel)?.background, sel).toBe('var(--fill)');
  });

  it('source contract: status text reads the text tokens, cards the card tokens, labels the label tokens', () => {
    const rules = parseBlocks(styleBlocks(readFileSync(join(__dirname, 'Autonomy.svelte'), 'utf-8')));
    expect(rules.get('.verdict-warn')).toEqual({
      color: 'var(--warn-text)', 'border-color': 'color-mix(in srgb, var(--warn-text) 35%, transparent)',
      background: 'color-mix(in srgb, var(--warn-text) 8%, transparent)',
    });
    expect(rules.get('.verdict-bad')).toMatchObject({
      color: 'var(--err-text)', 'border-color': 'color-mix(in srgb, var(--tone-bad) 50%, transparent)',
      background: 'color-mix(in srgb, var(--tone-bad) 10%, transparent)',
    });
    expect(rules.get('.card, .panel')).toMatchObject({ border: 'var(--card-border)', 'border-radius': 'var(--card-radius)', 'box-shadow': 'var(--card-shadow)' });
    expect(rules.get('.strip')).toMatchObject({ 'border-radius': 'var(--card-radius)' });
    expect(rules.get('.strip-meta dt, .tiles dt, .triple dt, .detail dt, .label')).toMatchObject({
      'text-transform': 'none', 'letter-spacing': '0',
    });
    expect(rules.get('.nums td:not(:first-child)')).toMatchObject({ 'font-stretch': '85%' });
    expect(rules.get('.stats td:not(:first-child)')).toBeUndefined();
    expect(rules.get('h1')).toMatchObject({ 'font-size': '30px' });
  });
});
