// Test-only: answers of the goal routes (nous/api/goal_routes.py) in their real shape, with made-up values. The
// detail's keys and order are those of a real GET /goals/{ref} answer; refresh them when the route's answer changes.
import type {
  GoalDetail, GoalOptions, GoalRow, GoalToolGrant, GoalToolOption, GoalTools, GoalToolWords, NewGoalTools,
} from '../goals';

/** The server's sentence of what spend does not count (nous/goals/seed.py SPEND_NOTE). */
export const SPEND_NOTE =
  "Spend counts the uncached input and output tokens of each session's chain. Cache reads and writes, jobs launched from the chain, embeddings and helper calls are not counted.";

export const NOW = Date.parse('2026-11-15T12:00:00Z');

export function goalRow(over: Partial<GoalRow> = {}): GoalRow {
  return {
    id: '00000000-0000-4000-8000-000000000001',
    ref: 'G-0001',
    title: 'Tidy the notes',
    state: 'active',
    pause_reason: null,
    preset: 'medium',
    proposed_by: 'owner',
    total_budget: 10_000_000,
    day_budget: 2_000_000,
    spent_total: 123_000,
    spent_today: 23_000,
    deadline_at: '2026-11-21T12:00:00+00:00',
    next_wake_at: '2026-11-15T14:00:00+00:00',
    open_session: null,
    pending_request: null,
    mode: 'contained',
    updated_at: '2026-11-15T11:00:00+00:00',
    ...over,
  };
}

export function goalDetail(over: Partial<GoalDetail> = {}): GoalDetail {
  const row = goalRow(over);
  return {
    enabled: true,
    ...row,
    objective: 'Merge the duplicate notes in the notes folder',
    done_when: 'No two notes say the same thing',
    origin_channel: 'telegram:****4242',
    deadline_days: 10,
    sessions_per_day: 4,
    min_interval_seconds: 3600,
    jobs_per_day: 2,
    quiet_hours_ok: false,
    stall_limit: 3,
    mode_until: null,
    approved_at: '2026-11-11T12:00:00+00:00',
    finished_at: null,
    created_at: '2026-11-11T11:00:00+00:00',
    plan: [
      { text: 'List the duplicate notes', status: 'done', evidence: ['notes/index.md'], closed_session: 1 },
      { text: 'Merge each pair', status: 'open' },
    ],
    sessions: [
      { n: 1, trigger: 'timer', started_at: '2026-11-14T09:00:00+00:00', ended_at: '2026-11-14T09:20:00+00:00', outcome: 'step_closed', spent: 120_000, steps_closed: 1, summary: 'Listed 14 duplicate pairs.' },
    ],
    sessions_total: 1,
    journal: [
      { seq: 12, session_n: null, kind: 'owner_note', actor: 'owner', text: 'Use the 2024 folder.', evidence: [], created_at: '2026-11-15T10:00:00+00:00' },
      { seq: 11, session_n: 1, kind: 'progress', actor: 'chain', text: 'Listed the duplicates.', evidence: ['notes/index.md'], created_at: '2026-11-14T09:19:00+00:00' },
      { seq: 10, session_n: null, kind: 'state', actor: 'owner', text: 'Approved: active until 2026-11-21 12:00 UTC.', evidence: [], created_at: '2026-11-11T12:00:00+00:00' },
    ],
    pause_sentence: null,
    actions: ['run-now', 'pause', 'stop', 'budget', 'deadline', 'note', 'cancel'],
    resume_blocked: null,
    spend_note: SPEND_NOTE,
    ...over,
  };
}

export const OPTIONS = (over: Partial<GoalOptions> = {}): GoalOptions => ({
  enabled: true,
  presets: {
    small: { total_budget: 2_000_000, day_budget: 500_000, deadline_days: 3, sessions_per_day: 2, min_interval_seconds: 7200, jobs_per_day: 1, quiet_hours_ok: false },
    medium: { total_budget: 10_000_000, day_budget: 2_000_000, deadline_days: 10, sessions_per_day: 4, min_interval_seconds: 3600, jobs_per_day: 2, quiet_hours_ok: false },
    large: { total_budget: 40_000_000, day_budget: 6_000_000, deadline_days: 30, sessions_per_day: 8, min_interval_seconds: 1800, jobs_per_day: 4, quiet_hours_ok: true },
  },
  ranges: [
    { field: 'total_budget', low: 1, high: 1_000_000_000, words: 'the total budget' },
    { field: 'day_budget', low: 1, high: 1_000_000_000, words: 'the day budget' },
    { field: 'deadline_days', low: 1, high: 90, words: 'the deadline in days' },
    { field: 'sessions_per_day', low: 1, high: 24, words: 'sessions per day' },
    { field: 'min_interval_seconds', low: 60, high: 604_800, words: 'the interval between sessions in seconds' },
    { field: 'jobs_per_day', low: 0, high: 20, words: 'jobs per day' },
  ],
  has_default_channel: false,
  channel_kinds: ['telegram'],
  bounds: { title: 200, objective: 4000, done_when: 2000, channel: 200, note: 4000, deadline_days: 90, journal: 200 },
  driver_running: false,
  spend_note: SPEND_NOTE,
  ...over,
});

/** The keys of a real detail answer, in order. */
export const DETAIL_KEYS = [
  'enabled', 'id', 'ref', 'title', 'state', 'pause_reason', 'preset', 'proposed_by', 'total_budget', 'day_budget',
  'spent_total', 'spent_today', 'deadline_at', 'next_wake_at', 'open_session', 'pending_request', 'mode', 'updated_at',
  'objective', 'done_when', 'origin_channel', 'deadline_days', 'sessions_per_day', 'min_interval_seconds', 'jobs_per_day',
  'quiet_hours_ok', 'stall_limit', 'mode_until', 'mode_hours', 'commands', 'approved_at', 'finished_at', 'created_at',
  'plan', 'sessions', 'sessions_total', 'journal', 'pause_sentence', 'actions', 'resume_blocked', 'spend_note',
];

// F115 per-goal tools: GET /goals/{ref}/tools in its real shape (nous/api/goal_routes.py `tools_payload`), with the
// server's own sentences (nous/goals/grants.py) and made-up ids. Refresh it when the route's answer changes.
export const LEAD =
  "These tools can be used without asking you, by this goal's own sessions and the work they start (chain decisions, background tasks, and workflow steps that run as tasks). A workflow's check steps are not included. Nothing else gets them.";
export const NEVER =
  'The shell and Python cannot be allowed here. Neither can scheduling, starting other work, or changing settings.';
export const NOTES_SERVER_ID = '00000000-0000-4000-8000-0000000000aa';
/** A made-up identity token, in the shape the routes send (64 hex characters). */
export const NOTES_IDENTITY = 'a'.repeat(64);

export function toolWords(tool: string, ref = 'G-0001'): GoalToolWords {
  const words: Record<string, [string, string]> = {
    send_email: [
      "send email, with files this goal's work wrote, to the addresses on your email allowlist",
      'A sent email cannot be taken back.',
    ],
    send_file: ["send files this goal's work wrote to your Telegram chat", 'A sent file cannot be taken back.'],
    browser_task: [
      'open web pages on the sites each call names, click and type',
      'It has no logins, and it stops before a step that looks like signing in, paying, accepting terms, sending a message or deleting something.',
    ],
    mcp__notes__search: [
      'use search on notes (notes.example.com)',
      'It can do whatever that tool does there, which Nous cannot check: read, change or delete things.',
    ],
  };
  const [does, note] = words[tool];
  const mcp = tool.startsWith('mcp__');
  return {
    label: mcp ? 'search' : tool,
    group: mcp ? 'other_servers' : 'reaching_outside',
    classes: mcp ? ['Reaches outside Nous', 'Brings in outside text'] : ['Reaches outside Nous'],
    does,
    note,
    says: `${does[0].toUpperCase()}${does.slice(1)}. ${note}`,
    confirm: `Allow ${ref} to ${does} without asking you each time? ${note}`,
    server_name: mcp ? 'notes' : null,
    server_host: mcp ? 'notes.example.com' : null,
    server_id: mcp ? NOTES_SERVER_ID : null,
    identity: mcp ? NOTES_IDENTITY : null,
    tool,
  };
}

export function toolGrant(tool: string, over: Partial<GoalToolGrant> = {}): GoalToolGrant {
  return { ...toolWords(tool), granted_at: '2026-11-15T10:00:00+00:00', usable: true, not_usable_why: null, ...over };
}

export function toolOption(tool: string, over: Partial<GoalToolOption> = {}): GoalToolOption {
  return { ...toolWords(tool), blocked: null, ...over };
}

export function goalToolsAnswer(over: Partial<GoalTools> = {}): GoalTools {
  return {
    enabled: true,
    grants_enabled: true,
    can_edit: true,
    limit: 20,
    applies_in: LEAD,
    never: NEVER,
    grants: [toolGrant('send_email')],
    available: [
      toolOption('browser_task'),
      toolOption('send_file', {
        blocked: {
          code: 'not_ready',
          sentence: 'send_file has no Telegram chat set, so it could send to any chat a call names. Set the chat first.',
        },
      }),
      toolOption('mcp__notes__search'),
    ],
    ...over,
  };
}

/** GET /goals/options `tools` with the grants on: the rows for "this goal" and the one question before Create. */
export function newGoalToolsAnswer(): NewGoalTools {
  const forNew = (tool: string) => {
    const words = toolWords(tool, 'this goal');
    return { ...words, blocked: null };
  };
  return {
    applies_in: LEAD,
    never: NEVER,
    limit: 20,
    confirm: 'Allow this goal to use these tools without asking you each time, from its first session after you approve it?',
    available: [
      forNew('browser_task'),
      forNew('send_email'),
      {
        ...forNew('send_file'),
        blocked: {
          code: 'not_ready',
          sentence: 'send_file has no Telegram chat set, so it could send to any chat a call names. Set the chat first.',
        },
      },
      forNew('mcp__notes__search'),
    ],
  };
}
