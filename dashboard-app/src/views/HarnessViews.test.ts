import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import { get } from 'svelte/store';
import Harness from './Harness.svelte';
import Ledger from './Ledger.svelte';
import DagView from './DagView.svelte';
import Overview from './Overview.svelte';
import { attentionOverride, attentionPoll } from '../lib/stores/attention';

// Render smoke for the harness dashboard views (spec 2026-09-25 v2 §5).
// Fixtures carry ONLY shapes the backend stores (§4.5): hashed free text,
// `scope:hash16` keys, no invented causes.

const MODES = {
  persist: true, retention_days: 90, offered_set: 'warn', context_policy: 'warn',
  claim_verification: 'enforce', action_gating: 'off', events_persisted: true,
};
const ID = '8f2c41d0-5b7e-4a3c-9d21-6e0f3a1b7c44';
const row = (over: Record<string, unknown> = {}) => ({
  id: crypto.randomUUID(), created_at: '2026-09-25T12:40:19+00:00', completed_at: null, dispatched_at: null,
  tool_name: 'bash', context_kind: 'subtask', side_effect_type: 'write', status: 'success',
  refusal_code: null, result_summary: null, key_args: { command_sha256: '3f9a1234', command_len: 412 },
  idempotency_key: null, external_ref: null, session_id: 's-1', parent_session_id: null, subtask_id: null,
  dag_id: null, dag_name: null, dag_node_id: null, node_name: null, turn: 3, tombstone: false, held_by: null,
  ...over,
});

const EXECUTION = {
  modes: MODES,
  stats: { calls: 3, sends: 1, external: 1, repeat_sends_refused: 0, blocked: 0, unknown: 1, unknown_keyed: 1, errors: 0, pending: 0 },
  attention: [row({
    id: ID, tool_name: 'send_email', side_effect_type: 'external', status: 'unknown',
    key_args: { to: ['anna@northwind.example'] }, idempotency_key: 'dag:9f1c:send-report:9b41c0de2a7f53e1',
    dag_name: 'mail-weekly-report', node_name: 'send-report', result_summary: 'cancelled mid-call — outcome unknown',
  })],
  attention_total: 1,
  rows: [row(), row({ tool_name: 'send_email', status: 'success', key_args: {}, idempotency_key: 'subtask:x:1a2b', tombstone: true })],
  next_before: null,
};

let execution: Record<string, unknown> = EXECUTION;
let olderFails = false;
let harnessPatterns: unknown[] | null = null;
let attention: Record<string, unknown> = {};

const STATUS = {
  memory: { total_facts: 1, total_episodes: 1, total_chunks: 0, total_decisions: 1, total_procedures: 0,
    active_censors: 0, active_conversations: 0 },
  dashboard: {
    deltas: { facts: { last_7_days: 0 }, episodes: { last_7_days: 0 }, decisions: { last_7_days: 0 },
      procedures: { last_7_days: 0 } },
    distributions: { fact_categories: {}, decision_outcomes: {}, edge_relations: {} },
    timeseries: { facts: [], episodes: [], decisions: [] },
    graph_density: null,
  },
  calibration: { brier_score: null },
};
const ATTENTION = {
  questions_waiting: 0, next: null, sends_in_doubt: 0, latest_in_doubt: null, ledger_persisted: true,
  harness: { events_persisted: true, offered_set: { mode: 'warn', warn_7d: 0, refused_7d: 0 },
    context_policy: { mode: 'warn', warn_7d: 0, refused_7d: 0 } },
};

const HARNESS = (persisted = true) => ({
  window: '7d', events_persisted: persisted,
  rules: {
    offered_set: { mode: 'warn', first_event_at: '2026-09-24T08:10:00+00:00', by_mode: persisted ? { warn: 12 } : {},
      by_context: persisted ? [{ key: 'heartbeat_callback', count: 9 }] : [], by_tool: [] },
    context_policy: { mode: 'warn', first_event_at: null, by_mode: {}, by_violation: [], by_context: [] },
    claims: { mode: 'enforce', first_event_at: null, evidence_since: null,
      by_evidence: { exact: 0, plausible: 0, none: 0 }, by_mode: persisted ? { enforce: 40 } : {}, none_by_mode: {},
      turns_with_claims: 0, legacy: { events: 0, violations: 0 } },
  },
  daily: [{ date: '2026-09-25', offered_set: persisted ? 12 : 0, context_policy: 0, claims_none: 0 }],
  patterns: persisted ? [{ rule: 'offered_set', mode: 'warn', context: 'heartbeat_callback', tool: 'send_file',
    violation: 'not offered', count: 9, last_seen: '2026-09-25T13:02:00+00:00', latest_session: 'hb-1', snippet: null }] : [],
});

const DAG = {
  active_dags: [{
    id: 'd1', name: 'mail-weekly-report', description: '', status: 'running', source: 'agent',
    created_at: '2026-09-25T10:00:00+00:00', started_at: null, token_budget: 0, tokens_consumed: 0,
    waiting: 1, held_reason: null, edges: [],
    nodes: [{ id: 'n1', name: 'approve-send', description: '', node_type: 'approval', wave: 1, status: 'awaiting_input',
      result: '', error: '', tokens_used: 0, started_at: null, completed_at: null }],
  }],
  recent_dags: [{ id: 'r1', name: 'invoice-followup', status: 'failed', source: 'agent', created_at: null,
    completed_at: '2026-09-24T09:00:00+00:00', token_budget: 0, tokens_consumed: 0,
    result_summary: "Stopped at approval 'approve-send': 'Don't send'; 1 step not run", postmortem: null,
    node_count: 3, completed_count: 1, stopped_by: 'deadline',
    stops: [{ node_name: 'approve-send', answer_source: 'deadline', answer_label: "Don't send" }] }],
  waiting_on_you: [{ dag_id: 'd1', dag_name: 'mail-weekly-report', node_id: 'n1', node_name: 'approve-send',
    question: 'Send the drafted weekly report?', deadline: '2099-01-01T12:00:00+00:00', default_label: "Don't send",
    card_url: null, card_error: 'approval card not delivered yet: companion down', reviewing: ['draft-report'] }],
  stats: { active_count: 1, nodes_completed_24h: 3, success_rate: 0.9, avg_completion_seconds: 60, waiting_count: 1 },
  phase2_signals: {},
};

const approval = (over: Record<string, unknown> = {}) => ({
  question: 'Send the drafted weekly report?',
  options: [{ id: 'send', label: 'Send it', outcome: 'proceed' }, { id: 'hold', label: "Don't send", outcome: 'stop' }],
  default_option: 'hold', default_label: "Don't send", asked_at: null, deadline: null, answer: null,
  answer_label: null, answer_source: null, answered_by: null, answered_at: null, card_url: null, card_error: null,
  card_summary: 'Send the drafted weekly report?', reviewing: [], attempts: [], ...over,
});
const node = (id: string, name: string, status: string, over: Record<string, unknown> = {}) => ({
  id, name, description: '', node_type: 'approval', wave: 1, status, result: '', error: '', tokens_used: 0,
  started_at: null, completed_at: null, ...over,
});
const DAG_APPROVALS = {
  ...DAG,
  active_dags: [{
    ...DAG.active_dags[0],
    nodes: [
      node('n1', 'approve-send', 'awaiting_input',
        { approval: approval({ asked_at: '2026-09-25T10:00:00+00:00', deadline: '2099-01-01T12:00:00+00:00' }) }),
      node('n2', 'approve-later', 'pending', { wave: 2, approval: approval() }),
    ],
  }],
};

let dag: Record<string, unknown> = DAG;

let harnessPersisted = true;

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    // A 4xx is not retried by apiGet, so the failure surfaces at once.
    if (olderFails && url.includes('before=')) return { ok: false, status: 400, json: async () => ({}) } as Response;
    const harness = HARNESS(harnessPersisted);
    const body = url.startsWith('/dashboard/execution') ? execution
      : url.startsWith('/dashboard/harness') ? (harnessPatterns ? { ...harness, patterns: harnessPatterns } : harness)
      : url.startsWith('/dashboard/dag') ? dag
      : url.startsWith('/dashboard/attention') ? { ...ATTENTION, ...attention }
      : url.startsWith('/status') ? STATUS
      : null;
    if (!body) throw new Error(`unexpected ${url}`);
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
  vi.stubGlobal('Chart', class { destroy() {} update() {} data = {}; options = {}; });
  // d3 is a CDN global in the app: a chainable no-op stands in for it.
  const d3: any = new Proxy(function () {}, {
    get: (_t, k) => (k === Symbol.toPrimitive ? () => '' : k === 'then' ? undefined : d3),
    apply: () => d3,
  });
  vi.stubGlobal('d3', d3);
}

describe('harness dashboard views', () => {
  beforeEach(() => {
    harnessPersisted = true; execution = EXECUTION; dag = DAG; olderFails = false; harnessPatterns = null; attention = {};
    attentionOverride.set({});
    install();
  });
  // globals: false (vite.config.ts) — testing-library cannot register its
  // own cleanup, so every render would otherwise stay mounted in <body>.
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('Harness says what enforce would refuse, from the numbers', async () => {
    render(Harness);
    expect(await screen.findByText('Enforce would refuse calls like these')).toBeTruthy();
    expect(screen.getByText('12 in 7 d. Most: heartbeat_callback · send_file · not offered.')).toBeTruthy();
    expect(screen.getByText('No flags recorded yet')).toBeTruthy(); // context policy: never "would refuse nothing"
  });

  it('Harness says "not measured" when nothing is recorded', async () => {
    harnessPersisted = false;
    render(Harness);
    expect((await screen.findAllByText('Not measured')).length).toBe(3);
    expect(screen.getByText(/nothing below was measured/)).toBeTruthy();
  });

  it('Ledger shows a keyed send in doubt with two guarded statements and no invented cause', async () => {
    const { container } = render(Ledger);
    expect(await screen.findByText('1 send ended without confirming delivery')).toBeTruthy();
    const sql = Array.from(container.querySelectorAll('code.sql')).map((c) => c.textContent ?? '');
    expect(sql).toHaveLength(2);
    expect(sql[0]).toContain(`WHERE id = '${ID}' AND status = 'unknown'`);
    expect(sql[0]).toContain("status = 'success'");
    expect(sql[1]).toContain("status = 'error'");
    expect(container.textContent).toContain('cancelled mid-call — outcome unknown');
    expect(container.textContent).not.toContain('connection dropped');
    expect(container.textContent).toContain('command sha256:3f9a1234… (412 chars)');
    expect(container.textContent).toContain('details removed by retention');
  });

  it('Ledger counts every held send, not the page it shows, and tells the badge the same number', async () => {
    execution = { ...EXECUTION, attention_total: 25 };
    const { container } = render(Ledger);
    expect(await screen.findByText('25 sends ended without confirming delivery')).toBeTruthy();
    expect(container.textContent).toContain('Showing the newest 1 of 25');
    expect(container.textContent).toContain('25 hold a send');
    expect(get(attentionOverride).sends?.value).toBe(25);
  });

  it('Ledger says when older rows fail to load and does not stay paused', async () => {
    execution = { ...EXECUTION, next_before: '2026-09-25T12:40:19+00:00,abc' };
    olderFails = true;
    render(Ledger);
    await fireEvent.click(await screen.findByRole('button', { name: 'Load older' }));
    expect(await screen.findByText(/Could not load older rows/)).toBeTruthy();
    expect(screen.queryByText(/Paused while you view older rows/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Load older' })).toBeTruthy(); // retry stays available
  });

  it('Harness explains a gap without inventing why the rule was silent', async () => {
    const { container } = render(Harness);
    expect(await screen.findByText(/^A gap is a day the record cannot vouch for/)).toBeTruthy();
    expect(container.textContent).not.toContain('not recording yet');
  });

  it('Ledger never says an unknown row holds a send while persistence is off', async () => {
    execution = { ...EXECUTION, modes: { ...MODES, persist: false }, attention: [], attention_total: 0,
      rows: [row({ tool_name: 'send_email', status: 'unknown', idempotency_key: 'dag:9f1c:send:9b41c0de2a7f53e1' })] };
    const { container } = render(Ledger);
    await screen.findByText('Ledger persistence is off');
    await waitFor(() => expect(container.textContent).toContain('holds nothing while persistence is off'));
    expect(container.textContent).not.toContain('holds a send');
  });

  it('Harness shows no pattern when nothing is being recorded, whatever the payload holds', async () => {
    harnessPersisted = false;
    harnessPatterns = [{ rule: 'offered_set', mode: 'warn', context: 'subtask', tool: 'send_file',
      violation: 'not offered', count: 9, last_seen: '2026-09-25T13:02:00+00:00', latest_session: 's', snippet: null }];
    const { container } = render(Harness);
    // One in the chart panel, one where the patterns table would be.
    expect(await screen.findAllByText('Not measured — event persistence is off.', { selector: '.empty' })).toHaveLength(2);
    expect(container.textContent).not.toContain('send_file');
  });

  it('Harness gives no evidence start date it cannot know', async () => {
    // Post-2c events only, none older in view: the switch predates the window.
    const { container } = render(Harness);
    expect(await screen.findByText('Every check in this window recorded evidence levels')).toBeTruthy();
    expect(container.textContent).not.toContain('Evidence levels recorded since');
  });

  it('Harness names the top pattern from the mode its verdict describes', async () => {
    const p = { rule: 'offered_set', context: 'subtask', violation: 'not offered',
      last_seen: '2026-09-25T13:02:00+00:00', latest_session: 's', snippet: null };
    // Ranked first overall but recorded under enforce: not what warn "would refuse".
    harnessPatterns = [{ ...p, mode: 'enforce', tool: 'send_email', count: 30 },
      { ...p, mode: 'warn', tool: 'send_file', count: 9 }];
    render(Harness);
    expect(await screen.findByText(/^12 in 7 d\. Most: subtask · send_file · not offered\.$/)).toBeTruthy();
  });

  it('Harness lists one pattern per mode — warn and enforce rows of one kind never collide', async () => {
    const p = { rule: 'offered_set', context: 'subtask', tool: 'send_file', violation: 'not offered',
      count: 1, last_seen: '2026-09-25T13:02:00+00:00', latest_session: 's', snippet: null };
    harnessPatterns = [{ ...p, mode: 'enforce' }, { ...p, mode: 'warn' }];
    render(Harness);
    expect((await screen.findAllByText('send_file')).length).toBeGreaterThanOrEqual(2);
  });

  it('Overview never reports "no sends in doubt" or zero calls when the ledger is not recording', async () => {
    attention = { ledger_persisted: false };
    execution = { ...EXECUTION, modes: { ...MODES, persist: false } };
    await attentionPoll.refresh();
    const { container } = render(Overview);
    await waitFor(() => expect(container.textContent).toContain('Ledger persistence is off'));
    expect(container.textContent).not.toContain('no sends in doubt');
    expect(container.textContent).not.toContain('Calls (24 h)');
  });

  it('DAG node sheet follows the poll: an answered step updates, a finished DAG closes it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      dag = { ...DAG_APPROVALS, waiting_on_you: DAG.waiting_on_you };
      render(DagView);
      await fireEvent.click(await screen.findByRole('button', { name: 'Details' }));
      const badge = () => document.querySelector('.node-detail .status-badge')?.textContent;
      await waitFor(() => expect(badge()).toBe('awaiting_input'));

      const answered = approval({ asked_at: '2026-09-25T10:00:00+00:00', deadline: '2099-01-01T12:00:00+00:00',
        answer: 'send', answer_label: 'Send it', answer_source: 'companion', answered_at: '2026-09-25T11:00:00+00:00' });
      dag = { ...DAG_APPROVALS, waiting_on_you: [], active_dags: [{ ...DAG_APPROVALS.active_dags[0],
        nodes: [node('n1', 'approve-send', 'completed', { approval: answered })] }] };
      await vi.advanceTimersByTimeAsync(15_000);
      await waitFor(() => expect(badge()).toBe('completed'));

      dag = { ...DAG, active_dags: [], waiting_on_you: [] };
      await vi.advanceTimersByTimeAsync(15_000);
      await waitFor(() => expect(document.querySelector('.node-detail')).toBeNull());
    } finally {
      vi.useRealTimers();
    }
  });

  it('DAG node sheet says a card is on its way only for a question actually waiting', async () => {
    dag = DAG_APPROVALS;
    const { container } = render(DagView);
    await fireEvent.click(await screen.findByRole('button', { name: 'View Graph' }));
    await fireEvent.click(await screen.findByRole('button', { name: /approve-send/ }));
    expect(await screen.findByText('Card being delivered…')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: /approve-later/ }));
    await waitFor(() => expect(container.ownerDocument.body.textContent).toContain('not asked yet'));
    const sheet = container.ownerDocument.body.textContent ?? '';
    expect(sheet).not.toContain('Card being delivered');
    expect(sheet).not.toContain('()');
  });

  it('DAG view lists the question, explains an undelivered card, and names a deadline stop neutrally', async () => {
    const { container } = render(DagView);
    expect(await screen.findByText('Send the drafted weekly report?')).toBeTruthy();
    expect(screen.getByText('approval card not delivered yet: companion down')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Details' })).toBeTruthy();
    expect(screen.queryByText('Answer in companion')).toBeNull(); // no card to answer
    expect(container.textContent).toContain('stopped at approval');
    expect(container.textContent).not.toContain('stopped by you');
    expect(container.textContent).not.toMatch(/unattributed|human/);
  });
});
