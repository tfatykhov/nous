import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import Autonomy from './Autonomy.svelte';

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

let body: Record<string, unknown> = FULL();
let status = 200;
let urls: string[] = [];

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (!url.startsWith('/dashboard/autonomy')) throw new Error(`unexpected ${url}`);
    urls.push(url);
    // A 4xx is not retried by apiGet, so the failure surfaces at once.
    return { ok: status < 400, status, json: async () => body } as Response;
  }));
}

describe('Autonomy view', () => {
  beforeEach(() => { body = FULL(); status = 200; urls = []; install(); });
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

  it('says so when the endpoint fails', async () => {
    status = 400;
    render(Autonomy);
    expect(await screen.findByText('Failed to load autonomy data — retrying…')).toBeTruthy();
  });
});
