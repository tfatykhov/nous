import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import type { Component } from 'svelte';
import Work from './Work.svelte';
import Ledger from './Ledger.svelte';
import Overview from './Overview.svelte';
import Harness from './Harness.svelte';
import Subtasks from './Subtasks.svelte';
import Consolidation from './Consolidation.svelte';
import Observability from './Observability.svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { initRouter } from '../lib/router';
import { styleBlocks } from '../lib/testing/css';
import { attentionOverride, attentionPoll } from '../lib/stores/attention';

// F108 Phase 2 T13: the execution ledger is Work's Changes tab, and the old Activity screen's sections live on the
// screens they belong to (Event Timeline on Observability, Upcoming Schedules on Subtasks, Sleep Activity on
// Consolidation, Most Active Censors on Guards). Each destination fetches /dashboard/activity once for its section.

const ID = '8f2c41d0-5b7e-4a3c-9d21-6e0f3a1b7c44';
const MODES = {
  persist: true, retention_days: 90, detail: true, detail_retention_days: 30, offered_set: 'warn', context_policy: 'warn',
  claim_verification: 'enforce', action_gating: 'off', events_persisted: true,
};
const row = (over: Record<string, unknown> = {}) => ({
  id: crypto.randomUUID(), created_at: '2026-11-08T12:40:19+00:00', completed_at: null, dispatched_at: null,
  tool_name: 'bash', context_kind: 'subtask', side_effect_type: 'write', status: 'success',
  refusal_code: null, result_summary: null, key_args: { command_sha256: '3f9a1234', command_len: 412 },
  input_preview: null, has_input: false, has_output: false, idempotency_key: null, external_ref: null, session_id: 's-1',
  parent_session_id: null, subtask_id: null, dag_id: null, dag_name: null, dag_node_id: null, node_name: null, turn: 3,
  tombstone: false, held_by: null, ...over,
});
const EXECUTION = {
  modes: MODES,
  stats: { calls: 3, sends: 1, external: 1, repeat_sends_refused: 0, blocked: 0, unknown: 1, unknown_keyed: 1, errors: 0, pending: 0 },
  attention: [row({
    id: ID, tool_name: 'send_email', side_effect_type: 'external', status: 'unknown',
    key_args: { to: ['anna@northwind.example'] }, idempotency_key: 'dag:9f1c:send-report:9b41c0de2a7f53e1',
  })],
  attention_total: 1,
  rows: [row({ tool_name: 'write_file' })],
  next_before: null,
};

const ACTIVITY = {
  events: [
    { type: 'censor_triggered', created_at: '2026-11-08T10:00:00+00:00', data: { censor_id: 'c-9', matched_text: 'rm -rf' } },
    { type: 'sleep_completed', created_at: '2026-11-08T03:00:00+00:00', data: { facts_created: 4, procedures_created: 1 } },
  ],
  censor_stats: {
    total: 3, active: 2, auto_created: 2, manual_created: 1, total_activations_7d: 17, false_positives_7d: 6,
    top_censors: [{ id: 'c-1', trigger_pattern: 'never email the board', activations: 11 }],
  },
  schedule_stats: {
    total: 2, active: 4, fires_7d: 9,
    next_fires: [{ id: 'sch-1', task: 'weekly report digest', next_fire_at: '2099-01-05T09:00:00+00:00' }],
  },
  sleep_stats: { total_sleeps: 23, last_sleep: '2026-11-08T03:00:00+00:00', facts_created: 41, procedures_created: 5, censors_retired: 8 },
};

const HARNESS = {
  window: '7d', events_persisted: true,
  rules: {
    offered_set: { mode: 'warn', first_event_at: null, by_mode: {}, by_context: [], by_tool: [] },
    context_policy: { mode: 'warn', first_event_at: null, by_mode: {}, by_violation: [], by_context: [] },
    claims: { mode: 'enforce', first_event_at: null, evidence_since: null,
      by_evidence: { exact: 0, plausible: 0, none: 0 }, by_mode: {}, none_by_mode: {},
      turns_with_claims: 0, legacy: { events: 0, violations: 0 } },
  },
  daily: [], patterns: [],
};
const SUBTASKS = {
  window_hours: 24, totals: { total_terminal: 0, by_outcome: {}, empty_rate: 0, retry_rate: 0 },
  tokens_by_outcome: {}, top_failing_tasks: [], dag_correlation: {}, recent_outcomes: [], daily_trend: [],
};
const CONSOLIDATION = { cycles: [] };
const OBSERVABILITY = {
  event_bus: { total_processed: 12, total_dropped: 0, queue_depth: 0, uptime_seconds: 3700, event_counts: {}, handlers: {} },
  recent_traces: [], recent_modifications: [], drift: null, drift_trends: {}, context_log: [],
};
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
  questions_waiting: 0, next: null, sends_in_doubt: 1, ledger_persisted: true,
  latest_in_doubt: { id: ID, tool_name: 'send_email', recipients: ['anna@northwind.example'], dag_name: null,
    created_at: '2026-11-08T12:40:19+00:00', tombstone: false },
  harness: { events_persisted: true, offered_set: { mode: 'warn', warn_7d: 0, refused_7d: 0 },
    context_policy: { mode: 'warn', warn_7d: 0, refused_7d: 0 } },
};
const SESSIONS = {
  enabled: true, window_days: 7,
  counts: { conversation: { sessions: 1, turns: 1 }, task: { sessions: 0, turns: 0 }, background: { sessions: 0, turns: 0 } },
  tool_names: [], next_before: null,
  sessions: [{
    session_id: 's-1', parent_session_id: null, kind: 'conversation', work_kind: 'interactive', context_kind: null,
    title: 'Gap Scout status check', first_message: null, started_at: '2026-11-08T00:07:00+00:00', ended_at: null,
    turns: 1, tool_calls: 0, errors: 0, tools: [], flags: [],
  }],
};

// The Overview's day timeline and Needs-you findings (redesign PR 1): recording off, no open finding.
const TODAY_OFF = {
  enabled: false, day: '2026-11-08', now: '2026-11-08T12:00:00+00:00', turns: [], truncated: false,
  totals: { owner_turns: 0, background_turns: 0, tool_calls: 0, errors: 0, gated: 0, median_seconds: null,
    model_calls: null, input_tokens: null, cache_read_tokens: null },
};
const FINDINGS = { findings: [], stats: {} };

// Routed by URL: /dashboard/activity serves the four moved sections; each host answers from its own endpoint.
let urls: string[] = [];
let failing: string[] = [];
let execution: unknown = EXECUTION;
let activityBody: unknown = ACTIVITY;
let activityGate: Promise<void> | null = null;
function install() {
  urls = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(url);
    // A 4xx is not retried by apiGet, so a failure surfaces at once.
    if (failing.some((p) => url.startsWith(p))) return { ok: false, status: 400, json: async () => ({}) } as Response;
    if (url.startsWith('/dashboard/activity') && activityGate) await activityGate;
    const body = url.startsWith('/dashboard/activity') ? activityBody
      : url.startsWith('/dashboard/execution') ? execution
      : url.startsWith('/dashboard/harness') ? HARNESS
      : url.startsWith('/dashboard/subtasks') ? SUBTASKS
      : url.startsWith('/dashboard/consolidation') ? CONSOLIDATION
      : url.startsWith('/dashboard/observability') ? OBSERVABILITY
      : url.startsWith('/dashboard/attention') ? ATTENTION
      : url.startsWith('/dashboard/work/sessions') ? SESSIONS
      : url.startsWith('/dashboard/work/today') ? TODAY_OFF
      : url.startsWith('/heartbeat/findings') ? FINDINGS
      : url.startsWith('/status') ? STATUS
      : null;
    if (!body) throw new Error(`unexpected ${url}`);
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
  vi.stubGlobal('Chart', class { destroy() {} update() {} data = {}; options = {}; });
}
function go(hash: string) {
  location.hash = hash;
  window.dispatchEvent(new HashChangeEvent('hashchange'));
}
const h2 = (name: string) => screen.queryByRole('heading', { level: 2, name });
const sectionOf = async (name: string) =>
  (await screen.findByRole('heading', { level: 2, name })).closest('section') as HTMLElement;
const activityUrls = () => urls.filter((u) => u.startsWith('/dashboard/activity'));

const SECTIONS = ['Event Timeline', 'Most Active Censors', 'Upcoming Schedules', 'Sleep Activity'];
const CENSOR_STATS = ['Censor Activations (7d)', 'Auto-created Censors', 'Manual Censors', 'False Positives (7d)'];
const SCHEDULE_STATS = ['Active Schedules', 'Schedule Fires (7d)'];
const SLEEP_STATS = ['Last Sleep', 'Sleep Facts Created'];

beforeEach(() => {
  failing = [];
  execution = EXECUTION;
  activityBody = ACTIVITY;
  activityGate = null;
  attentionOverride.set({});
  install();
  location.hash = '';
  initRouter();
});
// globals: false (vite.config.ts): testing-library cannot register its own cleanup.
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Work Changes tab', () => {
  it('shows the ledger under Work with Changes current in the sub navigation', async () => {
    go('#/work/changes');
    const { container } = render(Work);
    const nav = screen.getByRole('navigation', { name: 'Work views' });
    const links = within(nav).getAllByRole('link');
    expect(links.map((a) => a.textContent)).toEqual(['Sessions', 'Patterns', 'Changes']);
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['#/work', '#/work/patterns', '#/work/changes']);
    expect(links.map((a) => a.getAttribute('aria-current'))).toEqual([null, null, 'page']);

    // Holds, with the guarded undo statements of HoldUndo.
    expect(await screen.findByText('1 send ended without confirming delivery')).toBeTruthy();
    const sql = Array.from(container.querySelectorAll('code.sql')).map((c) => c.textContent ?? '');
    expect(sql).toHaveLength(2);
    expect(sql[0]).toContain(`WHERE id = '${ID}' AND status = 'unknown'`);
    // Stats and the calls table.
    const stats = container.querySelector('.stat-grid') as HTMLElement;
    expect(within(stats).getByText('Side-effecting calls')).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Calls' })).toBeTruthy();
    expect(screen.getAllByText('write_file').length).toBeGreaterThan(0);
    // The screen's title, not the ledger's; the ledger keeps its subtitle and its staleness badge.
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Work');
    expect(screen.queryByText('Execution Ledger')).toBeNull();
    expect(container.textContent).toContain('Every side-effecting tool call, kept durably');
    expect(container.querySelector('.stale-badge')).not.toBeNull();
    expect(urls.some((u) => u.startsWith('/dashboard/execution'))).toBe(true);
    expect(urls.some((u) => u.startsWith('/dashboard/work/sessions'))).toBe(false);
  });

  it('Changes renders the Ledger straight into the paper page: no dark island, one level-1 heading', async () => {
    go('#/work/changes');
    const main = document.createElement('main');
    main.classList.add('page-ground');
    document.body.appendChild(main);
    const { container } = render(Work, { target: main });
    await screen.findByText('1 send ended without confirming delivery');
    // Review Focus 5: nothing inside the paper route keeps a dark ground.
    expect(main.querySelector('.page-ground')).toBeNull();
    expect(container.querySelector('.island')).toBeNull();
    const subtitle = screen.getByText(/Every side-effecting tool call, kept durably/);
    expect(subtitle.closest('.page-ground')).toBe(main);
    expect(main.querySelector('code.sql')).not.toBeNull();
    // The Work title only: the embedded ledger drops its own, and the Changes frame's h2 is gone.
    expect(screen.getAllByRole('heading', { level: 1 }).map((h) => h.textContent)).toEqual(['Work']);
    expect(h2('Changes')).toBeNull();
    main.remove();
  });

  it('Work no longer imports the dark island', () => {
    const src = readFileSync(join(__dirname, 'Work.svelte'), 'utf-8');
    expect(src).not.toMatch(/import Ground|<Ground/);
  });

  it('the sessions list has no ledger in it', async () => {
    go('#/work');
    const { container } = render(Work);
    await screen.findByText('Gap Scout status check');
    expect(urls.some((u) => u.startsWith('/dashboard/execution'))).toBe(false);
    expect(container.textContent).not.toContain('Side-effecting calls');
    expect(container.textContent).not.toContain('Execution Ledger');
    expect(container.querySelector('code.sql')).toBeNull();
    const nav = screen.getByRole('navigation', { name: 'Work views' });
    expect(within(nav).getByRole('link', { name: 'Sessions' }).getAttribute('aria-current')).toBe('page');
    expect(within(nav).getByRole('link', { name: 'Changes' }).getAttribute('aria-current')).toBeNull();
  });

  it('leaving the Changes tab for the sessions list removes the ledger', async () => {
    go('#/work/changes');
    const { container } = render(Work);
    await screen.findByText('1 send ended without confirming delivery');
    go('#/work');
    await screen.findByText('Gap Scout status check');
    expect(container.textContent).not.toContain('Side-effecting calls');
    expect(container.querySelector('code.sql')).toBeNull();
  });
});

describe('Ledger embedded prop', () => {
  it('standalone, the ledger keeps its own title', async () => {
    render(Ledger);
    expect((await screen.findByRole('heading', { level: 1 })).textContent).toBe('Execution Ledger');
  });

  it('embedded, it drops only its title: subtitle and staleness badge stay', async () => {
    const { container } = render(Ledger, { props: { embedded: true } });
    await screen.findByText('1 send ended without confirming delivery');
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull();
    expect(container.querySelector('.view-head .subtitle')?.textContent).toContain('Every side-effecting tool call');
    expect(container.querySelector('.view-head .stale-badge')).not.toBeNull();
  });
});

// ── Redesign PR 3: the Ledger on paper ───────────────────────────────────────────────────────────────────────────────

describe('Ledger: status and effect by token', () => {
  const css = () => styleBlocks(readFileSync(join(__dirname, 'Ledger.svelte'), 'utf-8'));
  const WANT: Record<string, string> = {
    pending: 'var(--pending)', success: 'var(--st-success)', error: 'var(--st-failed)',
    blocked: 'var(--st-ledger-blocked)', unknown: 'var(--unknown)', // the unknown colour itself (PR 7 T4)
  };

  it('every ledger status renders a badge from its token with a color-mix tint, never a hex', async () => {
    execution = { ...EXECUTION, attention: [], attention_total: 0,
      rows: Object.keys(WANT).map((status) => row({ status, tool_name: `tool_${status}` })) };
    const { container } = render(Ledger);
    await screen.findByText('tool_pending');
    for (const [status, v] of Object.entries(WANT)) {
      const pill = Array.from(container.querySelectorAll<HTMLElement>('.pill')).find((p) => p.textContent === status);
      expect(pill, status).toBeTruthy();
      expect(pill!.style.color, status).toBe(v);
      expect(pill!.style.background, status).toBe(`color-mix(in srgb, ${v} 12.5%, transparent)`);
      expect(pill!.getAttribute('style'), status).not.toContain('#');
    }
  });

  it('every effect renders its pill', async () => {
    execution = { ...EXECUTION, attention: [], attention_total: 0,
      rows: ['write', 'external', 'irreversible'].map((e) => row({ side_effect_type: e, tool_name: `tool_${e}` })) };
    const { container } = render(Ledger);
    await screen.findByText('tool_write');
    expect(Array.from(container.querySelectorAll('.pill[class*="effect-"]')).map((p) => p.className.match(/effect-\w+/)?.[0]))
      .toEqual(['effect-write', 'effect-external', 'effect-irreversible']);
  });

  it('source contract: the effect pills\' colours are tokens', () => {
    expect(css()).toMatch(/\.effect-write \{ background: color-mix\(in srgb, var\(--effect-write\) 15%, transparent\); color: var\(--effect-write\); \}/);
    expect(css()).toMatch(/\.effect-external \{ background: var\(--badge-warn-bg\); color: var\(--st-ledger-blocked\); \}/);
    expect(css()).toMatch(/\.effect-irreversible \{ background: var\(--badge-bad-bg\); color: var\(--err-text\); \}/);
  });

  it('source contract: the banner, the modes and the sends-in-doubt card read status tokens', () => {
    const c = css();
    expect(c).toMatch(/\.banner-dot \{[^}]*background: var\(--st-success\);/);
    expect(c).toMatch(/\.banner-off \.banner-dot \{ background: var\(--st-ledger-blocked\); \}/);
    expect(c).toMatch(/\.mode-enforce \{ color: var\(--st-success\); \}/);
    expect(c).toMatch(/\.mode-warn \{ color: var\(--st-ledger-blocked\); \}/);
    expect(c).toMatch(/\.attention \{[^}]*background: color-mix\(in srgb, var\(--unknown\) 6%, transparent\); border: 1px solid color-mix\(in srgb, var\(--unknown\) 35%, transparent\);/);
    expect(c).toMatch(/border-top: 1px solid color-mix\(in srgb, var\(--unknown\) 20%, transparent\);/);
  });
});

describe('Overview ledger links', () => {
  it('the sends-in-doubt card and "Open ledger" point at the Changes tab', async () => {
    await attentionPoll.refresh();
    render(Overview);
    const card = await screen.findByRole('link', { name: '1 send ended without confirming delivery' });
    expect(card.getAttribute('href')).toBe('#/work/changes');
    const open = await screen.findByRole('link', { name: /Open ledger/ });
    expect(open.getAttribute('href')).toBe('#/work/changes');
    expect(document.querySelector('a[href="#/execution"]')).toBeNull();
  });

  it('the questions card opens Workflows and the harness line says Open Guards, as the nav names them', async () => {
    const base = globalThis.fetch;
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) =>
      url.startsWith('/dashboard/attention')
        ? ({ ok: true, status: 200, json: async () => ({ ...ATTENTION, questions_waiting: 2 }) } as Response)
        : base(url, init)));
    await attentionPoll.refresh();
    render(Overview);
    const questions = await screen.findByRole('link', { name: '2 questions waiting on you' });
    expect(questions.getAttribute('href')).toBe('#/dag');
    const guards = screen.getByRole('link', { name: 'Open Guards' });
    expect(guards.getAttribute('href')).toBe('#/harness');
    expect(screen.queryByText('Open Harness')).toBeNull();
  });
});

type Dest = {
  name: string; view: Component; section: string; content: string; host: string; hostMarker: string;
  stats: string[]; absentStats: string[];
};
const DESTS: Dest[] = [
  { name: 'Observability', view: Observability as unknown as Component, section: 'Event Timeline', content: 'Censor c-9 fired — matched: rm -rf',
    host: '/dashboard/observability', hostMarker: 'Handler Health', stats: [], absentStats: [...CENSOR_STATS, ...SCHEDULE_STATS, ...SLEEP_STATS] },
  { name: 'Guards (Harness)', view: Harness as unknown as Component, section: 'Most Active Censors', content: 'never email the board',
    host: '/dashboard/harness', hostMarker: 'Flags per day', stats: CENSOR_STATS, absentStats: [...SCHEDULE_STATS, ...SLEEP_STATS] },
  { name: 'Subtasks', view: Subtasks as unknown as Component, section: 'Upcoming Schedules', content: 'weekly report digest',
    host: '/dashboard/subtasks', hostMarker: 'Outcome distribution', stats: SCHEDULE_STATS, absentStats: [...CENSOR_STATS, ...SLEEP_STATS] },
  { name: 'Consolidation', view: Consolidation as unknown as Component, section: 'Sleep Activity', content: 'Censors Retired',
    host: '/dashboard/consolidation', hostMarker: 'Recent Cycles', stats: [], absentStats: [...CENSOR_STATS, ...SCHEDULE_STATS, 'Sleep Facts Created'] },
];

describe('Activity split', () => {
  it('Observability shows the Event Timeline, from /dashboard/activity', async () => {
    render(Observability);
    const s = await sectionOf('Event Timeline');
    await waitFor(() => expect(within(s).getByText('censor triggered')).toBeTruthy());
    expect(within(s).getByText('Censor c-9 fired — matched: rm -rf')).toBeTruthy();
    expect(within(s).getByText('sleep completed')).toBeTruthy();
    expect(within(s).getByText('Sleep cycle completed. 4 facts, 1 procedures.')).toBeTruthy();
    expect(within(s).getByText(new Date('2026-11-08T10:00:00+00:00').toLocaleString())).toBeTruthy();
  });

  it('Guards shows Most Active Censors with the censor numbers', async () => {
    render(Harness);
    const s = await sectionOf('Most Active Censors');
    await waitFor(() => expect(within(s).getByText('never email the board')).toBeTruthy());
    const item = within(s).getByText('never email the board').closest('li') as HTMLElement;
    expect(within(item).getByText('11')).toBeTruthy();
    const card = (label: string) => within(s).getByText(label).closest('.stat-card') as HTMLElement;
    expect(within(card('Censor Activations (7d)')).getByText('17')).toBeTruthy();
    expect(within(card('Auto-created Censors')).getByText('2')).toBeTruthy();
    expect(within(card('Manual Censors')).getByText('1')).toBeTruthy();
    expect(within(card('False Positives (7d)')).getByText('6')).toBeTruthy();
  });

  it('Subtasks shows Upcoming Schedules with the schedule numbers', async () => {
    render(Subtasks);
    const s = await sectionOf('Upcoming Schedules');
    await waitFor(() => expect(within(s).getByText('weekly report digest')).toBeTruthy());
    expect(within(s).getByText(`Next: ${new Date('2099-01-05T09:00:00+00:00').toLocaleString()}`)).toBeTruthy();
    const card = (label: string) => within(s).getByText(label).closest('.stat-card') as HTMLElement;
    expect(within(card('Active Schedules')).getByText('4')).toBeTruthy();
    expect(within(card('Schedule Fires (7d)')).getByText('9')).toBeTruthy();
  });

  // The moved numbers keep Activity's fixed windows, under screens that have a window selector of their own.
  it('Guards says its censor numbers do not follow the window selector', async () => {
    render(Harness);
    const s = await sectionOf('Most Active Censors');
    await waitFor(() => expect(within(s).getByText('never email the board')).toBeTruthy());
    expect(within(s).getByText(
      'Numbers marked (7d) are the last 7 days, whatever the window above. The count beside each censor is its all-time activations.',
    )).toBeTruthy();
    expect(screen.getByRole('group', { name: 'Window' })).toBeTruthy(); // the selector the caption speaks of
  });

  it('Subtasks says its schedule numbers do not follow the window selector', async () => {
    render(Subtasks);
    const s = await sectionOf('Upcoming Schedules');
    await waitFor(() => expect(within(s).getByText('weekly report digest')).toBeTruthy());
    expect(within(s).getByText('Numbers marked (7d) are the last 7 days, whatever the window above.')).toBeTruthy();
    expect(screen.getByRole('combobox', { name: 'Window' })).toBeTruthy();
  });

  it('Consolidation shows Sleep Activity', async () => {
    render(Consolidation);
    const s = await sectionOf('Sleep Activity');
    await waitFor(() => expect(within(s).getByText('Total Sleeps')).toBeTruthy());
    const dd = (term: string) => within(s).getByText(term).nextElementSibling?.textContent;
    expect(dd('Last Sleep')).toBe(new Date('2026-11-08T03:00:00+00:00').toLocaleString());
    expect(dd('Total Sleeps')).toBe('23');
    expect(dd('Facts Created')).toBe('41');
    expect(dd('Procedures')).toBe('5');
    expect(dd('Censors Retired')).toBe('8');
  });

  it('Consolidation says Never when no sleep has run', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      const body = url.startsWith('/dashboard/activity')
        ? { ...ACTIVITY, sleep_stats: { ...ACTIVITY.sleep_stats, last_sleep: null } } : CONSOLIDATION;
      return { ok: true, status: 200, json: async () => body } as Response;
    }));
    render(Consolidation);
    const s = await sectionOf('Sleep Activity');
    await waitFor(() => expect(within(s).getByText('Last Sleep').nextElementSibling?.textContent).toBe('Never'));
  });

  it('Consolidation Refresh reloads the sleep numbers too', async () => {
    render(Consolidation);
    const s = await sectionOf('Sleep Activity');
    await waitFor(() => expect(within(s).getByText('Total Sleeps')).toBeTruthy());
    await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(false));
    expect(activityUrls()).toHaveLength(1);
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(activityUrls()).toHaveLength(2));
  });

  it('the empty states read as they did on Activity', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      const empty = { ...ACTIVITY, events: [],
        censor_stats: { ...ACTIVITY.censor_stats, top_censors: [] },
        schedule_stats: { ...ACTIVITY.schedule_stats, next_fires: [] } };
      const body = url.startsWith('/dashboard/activity') ? empty
        : url.startsWith('/dashboard/harness') ? HARNESS
        : url.startsWith('/dashboard/subtasks') ? SUBTASKS
        : OBSERVABILITY;
      return { ok: true, status: 200, json: async () => body } as Response;
    }));
    render(Observability);
    expect(await screen.findByText('No events in the last 7 days.')).toBeTruthy();
    cleanup();
    render(Harness);
    expect(await screen.findByText('No censor activations.')).toBeTruthy();
    cleanup();
    render(Subtasks);
    expect(await screen.findByText('No upcoming schedules.')).toBeTruthy();
  });

  // Fetch-once, so a section that loaded keeps a manual Refresh (Consolidation's header Refresh covers Sleep Activity).
  const REFRESHED = {
    ...ACTIVITY,
    events: [{ type: 'censor_triggered', created_at: '2026-11-09T06:00:00+00:00', data: { censor_id: 'c-77', matched_text: 'DROP TABLE' } }],
    censor_stats: { ...ACTIVITY.censor_stats, top_censors: [{ id: 'c-2', trigger_pattern: 'never post secrets', activations: 3 }] },
    schedule_stats: { ...ACTIVITY.schedule_stats, next_fires: [{ id: 'sch-2', task: 'monthly invoice sweep', next_fire_at: '2099-02-01T09:00:00+00:00' }] },
  };
  const REFRESH_CASES = [
    { name: 'Observability', view: Observability as unknown as Component, section: 'Event Timeline',
      before: 'Censor c-9 fired — matched: rm -rf', after: 'Censor c-77 fired — matched: DROP TABLE' },
    { name: 'Guards', view: Harness as unknown as Component, section: 'Most Active Censors',
      before: 'never email the board', after: 'never post secrets' },
    { name: 'Subtasks', view: Subtasks as unknown as Component, section: 'Upcoming Schedules',
      before: 'weekly report digest', after: 'monthly invoice sweep' },
  ];
  for (const c of REFRESH_CASES) {
    it(`${c.name}: after a good load, the section's Refresh fetches the activity again and shows the new data`, async () => {
      render(c.view);
      const s = await sectionOf(c.section);
      await waitFor(() => expect(within(s).getByText(c.before)).toBeTruthy());
      expect(activityUrls()).toHaveLength(1);
      const button = within(s).getByRole('button', { name: 'Refresh' }) as HTMLButtonElement;
      expect(button.disabled).toBe(false);

      let release: () => void = () => {};
      activityGate = new Promise<void>((r) => { release = r; });
      activityBody = REFRESHED;
      await fireEvent.click(button);
      // In flight: the button says so and cannot be pressed again; the loaded rows stay on screen.
      await waitFor(() => expect(button.textContent?.trim()).toBe('Loading…'));
      expect(button.disabled).toBe(true);
      expect(within(s).getByText(c.before)).toBeTruthy();
      release();

      await waitFor(() => expect(within(s).getByText(c.after)).toBeTruthy());
      expect(within(s).queryByText(c.before)).toBeNull();
      expect(activityUrls()).toEqual(['/dashboard/activity?hours=168', '/dashboard/activity?hours=168']);
      await waitFor(() => expect(button.textContent?.trim()).toBe('Refresh'));
      expect(button.disabled).toBe(false);
    });
  }

  for (const d of DESTS) {
    it(`${d.name}: only its own section, its own numbers, and one fetch of /dashboard/activity?hours=168`, async () => {
      const { container } = render(d.view);
      const s = await sectionOf(d.section);
      await screen.findByRole('heading', { level: 2, name: d.hostMarker });
      await waitFor(() => expect(within(s).getByText(d.content)).toBeTruthy());
      for (const other of SECTIONS.filter((x) => x !== d.section)) expect(h2(other)).toBeNull();
      for (const label of d.stats) expect(within(s).getByText(label)).toBeTruthy();
      for (const label of d.absentStats) expect(within(container).queryByText(label)).toBeNull();
      expect(activityUrls()).toEqual(['/dashboard/activity?hours=168']);
    });

    it(`${d.name}: fetches the activity once, as Activity did, and never polls it`, async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        render(d.view);
        const s = await sectionOf(d.section);
        await waitFor(() => expect(within(s).getByText(d.content)).toBeTruthy());
        await vi.advanceTimersByTimeAsync(10 * 60_000);
        expect(activityUrls()).toEqual(['/dashboard/activity?hours=168']);
      } finally {
        vi.useRealTimers();
      }
    });

    it(`${d.name}: a failed activity load says so in the section, offers a retry, and the rest of the screen stays`, async () => {
      failing = ['/dashboard/activity'];
      render(d.view);
      const s = await sectionOf(d.section);
      await waitFor(() => expect(within(s).getByText(/Failed to load activity data/)).toBeTruthy());
      expect(await screen.findByRole('heading', { level: 2, name: d.hostMarker })).toBeTruthy();
      failing = [];
      await fireEvent.click(within(s).getByRole('button', { name: 'retry' }));
      await waitFor(() => expect(activityUrls()).toHaveLength(2));
      await waitFor(() => expect(within(s).queryByText(/Failed to load activity data/)).toBeNull());
    });

    it(`${d.name}: the section still shows when the screen's own data fails`, async () => {
      failing = [d.host];
      render(d.view);
      const s = await sectionOf(d.section);
      await waitFor(() => expect(within(s).getByText(d.content)).toBeTruthy());
      expect(h2(d.hostMarker)).toBeNull();
      expect(s.textContent).not.toMatch(/Failed to load activity data/);
    });
  }
});
