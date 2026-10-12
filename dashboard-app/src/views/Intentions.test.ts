import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import Intentions from './Intentions.svelte';

const HOSTILE = '<img src=x onerror=alert(1)>';
const limits = { depth: 1, spawns: 1, turns: 2, tokens: 12000, stalls: 0, spawn_blocked: false, escalate: null };
const child = (id: string, intent: string, children: unknown[] = []) => ({
  id, parent_id: 'r1', depth: 1, source_kind: 'subtask', source_id: 's', state: 'closed', close_reason: 'resolved',
  intent, created_at: '2026-10-12T10:00:00+00:00', result_at: null, closed_at: null, attempts: 0, failed_tokens: 0, children,
});
const DATA = {
  continuation: true, window: '7d',
  roots: [{
    id: 'r1', short_id: 'aaaaaaaa', intent: HOSTILE, state: 'deciding', wake_policy: 'continue', authority: 'owner',
    origin_kind: 'interactive', origin_channel: 'telegram:1', created_at: '2026-10-12T09:00:00+00:00', deadline: null,
    root_cancelled_at: null, root_expired_at: null, closed_at: null, limits,
    children: [child('c1', 'check the forecast', [child('g1', 'read the radar')])],
    arrivals: [{ id: 'a1', n: 1, intention_ids: ['r1'], decision: 'continue', note: '**not bold**', progress_claimed: true,
      progress: true, confidence: 0.8, gate_reason: null, outcome: 'resolved', tokens_in: 10, tokens_out: 5,
      decided_at: '2026-10-12T09:05:00+00:00', decision_record_id: 'd1', decision_outcome: 'noise',
      decision_reviewer: 'agent-background', result_to_decision_s: 38 }],
    proposals: [{ id: 'p1', short_id: 'bbbbbbbb', root_id: 'r1', intention_id: 'r1', arrival_id: 'a1', tool: 'send_email',
      arguments: { to: 'a@example.test' }, rationale: 'because', state: 'executed', deadline: null, decided_at: null,
      decided_by: 'owner', result: 'sent' }],
    owner_rows: [{ id: 'o1', msg_type: 'REPORT', title: 'All done', created_at: '2026-10-12T09:10:00+00:00',
      push_after: null, pushed_at: null, delivered_at: null }],
  }],
  metrics: {
    arrivals_by_decision_outcome: [{ decision: 'continue', outcome: 'resolved', gated: false, count: 2 }],
    result_to_decision_latency_s: { p50: 38, p90: 66, max: 123, n: 10 },
    results_lost: 0, results_waiting: 1, results_lost_grace_s: 1080, owner_rows_unpushed: 0,
    proposals_by_state: { counts: { executed: 2 }, approved_total: 2 },
    escalations_by_cause: { by_reason: { budget_turns: 0, budget_stall: 2 }, expired_roots: 0 },
    stalls: { verified_no_progress: 15, claimed_not_credited: 12, stall_escalations: 2, progress_claimed: 40, progress_credited: 28 },
    arrivals_ungradable: 5,
    token_spend: {
      basis: 'lifetime_of_active_roots', roots_counted: 1,
      budget_counted: { arrivals_in: 150, arrivals_out: 70, failed_tokens: 25, subtask_tokens: 0, dag_tokens: 0, total: 245 },
      context_log: { input_uncached: 270, output: 130, cache_write: 15, cache_read: 600, calls: 3 },
      unattributed_output: 60, failed_tokens: 25,
      top_roots: [{ root_id: 'r1', short_id: 'aaaaaaaa', intent: HOSTILE,
        budget_counted: { arrivals_in: 150, arrivals_out: 70, failed_tokens: 25, subtask_tokens: 0, dag_tokens: 0, total: 245 },
        context_log: { input_uncached: 270, output: 130, cache_write: 15, cache_read: 600, calls: 3 }, unattributed_output: 60 }],
    },
  },
};

let body: unknown = DATA;
let urls: string[] = [];
function install() {
  urls = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(url);
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
}

describe('Intentions view', () => {
  beforeEach(() => { body = DATA; install(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('shows the metrics the Phase 2 numbers come from', async () => {
    render(Intentions);
    const tile = async (label: string) => (await screen.findByText(label)).nextElementSibling?.textContent;
    expect(await tile('Result to decision, median')).toBe('38 s');       // p50 (the arrivals table also says 38 s)
    expect(await tile('90th percentile')).toBe('1 min 6 s');             // p90 66
    expect(await tile('Slowest')).toBe('2 min 3 s');                      // max 123
    expect(screen.getByText(/40 claimed/)).toBeTruthy();
    expect(screen.getByText(/28 credited/)).toBeTruthy();
    expect(screen.getByText(/5 arrivals have no decision record/)).toBeTruthy();
    expect(screen.getByText(/continuation turns only/)).toBeTruthy();     // S4
  });

  it('nests the tree and renders model text as text, never as HTML', async () => {
    const { container } = render(Intentions);
    expect((await screen.findAllByText(HOSTILE)).length).toBeGreaterThan(0);
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByText('check the forecast')).toBeTruthy();
    expect(screen.getByText('read the radar')).toBeTruthy();
    expect(container.querySelector('ul ul')).not.toBeNull();              // the grandchild is nested
    expect(screen.getByText('**not bold**')).toBeTruthy();                // markdown is not interpreted
    expect(container.querySelector('strong, b')).toBeNull();
  });

  it('says so when continuation is off', async () => {
    body = { continuation: false, roots: [], metrics: null };
    render(Intentions);
    expect(await screen.findByText(/Continuation is off/)).toBeTruthy();
  });

  it('refetches with the new window', async () => {
    render(Intentions);
    await screen.findByText('Result to decision, median');
    await fireEvent.click(screen.getByRole('button', { name: '30 d' }));
    await waitFor(() => expect(urls.some((u) => u.includes('window=30d'))).toBe(true));
  });

  it('PIN: no view file uses the raw-HTML directive', () => {
    // Mutation: add the directive to any of these files and this fails.
    // vitest runs from the dashboard-app root; import.meta.url is not a file URL under jsdom.
    for (const f of ['src/views/Intentions.svelte', 'src/lib/intentions.ts'].map((p) => resolve(process.cwd(), p))) {
      expect(readFileSync(f, 'utf-8')).not.toContain('{@html');
    }
  });
});
