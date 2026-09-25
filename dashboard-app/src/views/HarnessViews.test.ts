import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import Harness from './Harness.svelte';
import Ledger from './Ledger.svelte';
import DagView from './DagView.svelte';

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
  rows: [row(), row({ tool_name: 'send_email', status: 'success', key_args: {}, idempotency_key: 'subtask:x:1a2b', tombstone: true })],
  next_before: null,
};

const HARNESS = (persisted = true) => ({
  window: '7d', events_persisted: persisted,
  rules: {
    offered_set: { mode: 'warn', first_event_at: '2026-09-24T08:10:00+00:00', by_mode: persisted ? { warn: 12 } : {},
      by_context: persisted ? [{ key: 'heartbeat_callback', count: 9 }] : [], by_tool: [] },
    context_policy: { mode: 'warn', first_event_at: null, by_mode: {}, by_violation: [], by_context: [] },
    claims: { mode: 'enforce', first_event_at: null, evidence_since: null,
      by_evidence: { exact: 0, plausible: 0, none: 0 }, turns_with_claims: 0, legacy: { events: 0, violations: 0 } },
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

let harnessPersisted = true;

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.startsWith('/dashboard/execution') ? EXECUTION
      : url.startsWith('/dashboard/harness') ? HARNESS(harnessPersisted)
      : url.startsWith('/dashboard/dag') ? DAG
      : url.startsWith('/dashboard/attention') ? { questions_waiting: 1, sends_in_doubt: 1 }
      : null;
    if (!body) throw new Error(`unexpected ${url}`);
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
  vi.stubGlobal('Chart', class { destroy() {} update() {} data = {}; options = {}; });
}

describe('harness dashboard views', () => {
  beforeEach(() => { harnessPersisted = true; install(); });
  afterEach(() => { vi.unstubAllGlobals(); });

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
