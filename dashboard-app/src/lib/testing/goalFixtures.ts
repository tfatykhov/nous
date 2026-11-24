// Test-only: answers of the goal routes (nous/api/goal_routes.py) in their real shape, with made-up values. The
// detail's keys and order are those of a real GET /goals/{ref} answer; refresh them when the route's answer changes.
import type {
  AwaitingProposal, AwaitingQuestion, CommandsOptions, GoalActivity, GoalCommandsNow, GoalDetail, GoalOptions, GoalRow,
  GoalToolGrant, GoalToolOption, GoalTools, GoalToolWords, NewGoalTools, PacingOptions,
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
    awaiting_count: 0,
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
    mode_hours: null,
    commands: { state: 'off', says: 'Off', usable: false, not_usable_why: null, approve: null },
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
    awaiting: { proposals: [], questions: [] },
    ...over,
  };
}

/** An approval request in a goal's `awaiting`, as the server sends it (made-up values; the call is the stored
 * arguments as two-space-indented JSON). */
export function awaitingProposal(over: Partial<AwaitingProposal> = {}): AwaitingProposal {
  return {
    id: 'cfd2afea-0000-4000-8000-000000000001',
    short_id: 'cfd2afea',
    tool: 'send_email',
    arguments: '{\n  "to": "friend@example.com",\n  "subject": "Snow",\n  "body": "40 cm overnight."\n}',
    why: 'The owner asked me to share the snow report.',
    expires_at: '2026-11-16T09:00:00+00:00',
    ...over,
  };
}

/** A question in a goal's `awaiting`, as the server sends it (made-up values). */
export function awaitingQuestion(over: Partial<AwaitingQuestion> = {}): AwaitingQuestion {
  return {
    id: '5b1e0c2d-0000-4000-8000-000000000002',
    short_id: '5b1e0c2d',
    text: 'Which folder holds the newest notes?',
    expires_at: '2026-11-16T10:30:00+00:00',
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

// A goal that may run any command: GET /goals/options `commands` in its real shape, with the server's own words
// (nous/goals/commands.py WORDS). Refresh it when the route's answer changes.
export const DESCRIPTION =
  "This goal's tasks can run any command on the server Nous runs on, as the same user Nous runs as, and can start coding jobs. Nous does not pass its database login, model keys or chat tokens to those commands, but a command can read them from the running server, and can read and change Nous's files, settings and data. The code-host token is passed to them. Nous checks the end time before each command. A command already running at that time, anything it left running, and anything it changed stay as they are.";
export const COMMANDS = (over: Partial<CommandsOptions> = {}): CommandsOptions => ({
  max_hours: 24,
  sentence: 'allow this goal to run any command for {hours}',
  hour_one: '{n} hour',
  hours_many: '{n} hours',
  words: {
    checkbox: "Let this goal's tasks run any command until a time I set.",
    hours: 'Hours from approval',
    description: DESCRIPTION,
    heading: 'May run any command',
    off: 'Off',
    set: 'Set: {hours} from approval.',
    on: 'On until {until} UTC.',
    ended: 'Ended: {reason}',
    return: 'Return to contained',
    return_note:
      'Takes effect at the next tool call. A command already running, anything it left running, and anything it changed stay as they are.',
    return_stop: 'Use Stop now for the session.',
    stops: 'A lock or Not allowed on bash under Limits and tools stops its commands.',
    approve: "Approving starts its time: this goal's tasks may then run any command for {hours}.",
    banner: '{ref} may run any command until {until} UTC.',
  },
  refusals: { hours_range: 'Hours must be a whole number from 1 to {n}.', confirm: 'Type this sentence to allow it.' },
  ...over,
});
/** A goal's `commands` while it may run any command until 2026-11-15 20:00 UTC, its tasks running any command now. */
export const COMMANDS_ON = {
  state: 'on' as const,
  says: 'On until 2026-11-15 20:00 UTC.',
  usable: true,
  not_usable_why: null,
  approve: null,
};
/** GET /goals/commands with one goal in the mode. */
export const COMMANDS_NOW = (over: Partial<GoalCommandsNow> = {}): GoalCommandsNow => ({
  enabled: true,
  allowed: true,
  active: [
    { ref: 'G-0003', title: 'Ship the fix', until: '2026-11-15T20:00:00+00:00', says: 'G-0003 may run any command until 2026-11-15 20:00 UTC.' },
  ],
  ...over,
});

/** Goal pacing, with its flag on: GET /goals/options `pacing` in its real shape. A goal's detail then also has
 * `daily_at` (after `stall_limit`); pass it in `goalDetail`'s `over`. */
export const PACING = (over: Partial<PacingOptions> = {}): PacingOptions => ({
  zone: 'America/New_York',
  stall_limit: { low: 1, high: 10 },
  default_at: '09:00',
  ...over,
});

/** The keys of a real detail answer, in order. */
export const DETAIL_KEYS = [
  'enabled', 'id', 'ref', 'title', 'state', 'pause_reason', 'preset', 'proposed_by', 'total_budget', 'day_budget',
  'spent_total', 'spent_today', 'deadline_at', 'next_wake_at', 'open_session', 'pending_request', 'awaiting_count', 'mode',
  'updated_at', 'objective', 'done_when', 'origin_channel', 'deadline_days', 'sessions_per_day', 'min_interval_seconds',
  'jobs_per_day', 'quiet_hours_ok', 'stall_limit', 'mode_until', 'mode_hours', 'commands', 'approved_at', 'finished_at',
  'created_at', 'plan', 'sessions', 'sessions_total', 'journal', 'pause_sentence', 'actions', 'resume_blocked', 'spend_note',
  'awaiting',
];

/** The keys of a real GET /goals/{ref}/activity answer with the panel on, in order. */
export const ACTIVITY_KEYS = [
  'enabled', 'activity_enabled', 'log_enabled', 'session', 'phase', 'current', 'running_count', 'last_call_at', 'calls',
  'jobs', 'recent', 'work_sessions',
];

/** GET /goals/{ref}/activity in its real shape (nous/goals/activity.py): session 4 of G-0001, running since 11:48, its
 * chain working on a task; times relative to `NOW`. */
export function activityAnswer(over: Partial<GoalActivity> = {}): GoalActivity {
  return {
    enabled: true,
    activity_enabled: true,
    log_enabled: true,
    session: { n: 4, open: true, started_at: '2026-11-15T11:48:00+00:00', ended_at: null },
    phase: 'task',
    current: {
      kind: 'task', name: 'Merge the duplicate pairs', first_task: false, status: 'running',
      started_at: '2026-11-15T11:50:00+00:00', waiting_for: null,
    },
    running_count: 1,
    last_call_at: '2026-11-15T11:58:00+00:00',
    calls: { total: 31, errors: 2, capped: false },
    jobs: { launched: 0, last_launch_at: null, capped: false },
    recent: [
      { tool: 'bash', at: '2026-11-15T11:58:00+00:00', outcome: 'ran', in: 'task' },
      { tool: 'read_file', at: '2026-11-15T11:57:00+00:00', outcome: 'error', in: 'task' },
      { tool: 'spawn_task', at: '2026-11-15T11:49:00+00:00', outcome: 'ran', in: 'decisions' },
    ],
    work_sessions: [
      { id: 'subtask-1a2b3c4d', kind: 'task' },
      { id: 'intent-00000000-0000-4000-8000-0000000000bb', kind: 'decisions' },
    ],
    ...over,
  };
}

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
