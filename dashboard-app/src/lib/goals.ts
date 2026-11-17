// F114 goals PR 2: the Goals screen's types, requests and words. The answers are those of nous/api/goal_routes.py;
// every sentence the owner reads before an action, and every word for a state, is here (one table, pinned by
// goals.test.ts). Text a goal's sessions wrote (plan, journal, summaries) is shown as text nodes only.

import { apiRequest, type ApiAnswerError } from './api';
import { fmtUtc } from './harness';

export type GoalState = 'draft' | 'active' | 'paused' | 'done' | 'failed' | 'cancelled';
export type PauseReason = 'owner' | 'budget' | 'stalled' | 'waiting_for_owner' | 'error' | 'deadline';
/** The owner's actions on a goal, in the order the server lists them: each is the last segment of its route. */
export const ACTIONS = ['approve', 'run-now', 'resume', 'confirm-done', 'pause', 'stop', 'budget', 'deadline', 'note', 'cancel', 'contained'] as const;
export type Action = typeof ACTIONS[number];
export type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'neutral';

export interface GoalRow {
  id: string;
  ref: string;
  title: string;
  state: GoalState;
  pause_reason: PauseReason | null;
  preset: string;
  proposed_by: 'owner' | 'agent' | 'host';
  total_budget: number;
  day_budget: number;
  spent_total: number;
  spent_today: number;
  deadline_at: string | null;
  next_wake_at: string | null;
  open_session: number | null;
  pending_request: 'run_now' | 'stop' | 'cancel' | null;
  mode: string;
  updated_at: string | null;
}

export interface GoalSessionRow {
  n: number;
  trigger: 'timer' | 'run_now';
  started_at: string | null;
  ended_at: string | null;
  outcome: 'step_closed' | 'no_step' | 'asked' | 'error' | 'interrupted' | null;
  spent: number;
  steps_closed: number;
  summary: string | null;
}

export interface JournalRow {
  seq: number;
  session_n: number | null;
  kind: string;
  actor: string;
  text: string;
  evidence: string[];
  created_at: string | null;
}

/** A plan step: only the keys the server found with the right type. */
export interface PlanStep { text?: string; status?: string; evidence?: string[]; closed_session?: number }

export interface GoalDetail extends GoalRow {
  enabled: true;
  objective: string;
  done_when: string;
  origin_channel: string | null;
  deadline_days: number;
  sessions_per_day: number;
  min_interval_seconds: number;
  jobs_per_day: number;
  quiet_hours_ok: boolean;
  stall_limit: number;
  mode_until: string | null;
  /** A goal that may run any command: the hours chosen at creation (null for a contained goal). */
  mode_hours: number | null;
  commands: GoalCommands;
  approved_at: string | null;
  finished_at: string | null;
  created_at: string | null;
  plan: PlanStep[];
  /** The newest sessions (at most 50), the oldest of them first; `sessions_total` counts them all. */
  sessions: GoalSessionRow[];
  sessions_total: number;
  journal: JournalRow[];
  /** For a goal the harness paused, the harness's own sentence of why; null otherwise. */
  pause_sentence: string | null;
  actions: Action[];
  resume_blocked: 'budget' | 'deadline' | null;
  spend_note: string;
}

/** Whether a goal may run any command, as its page shows it (the server's words): `off`, `set` (chosen at creation,
 * its time starts at approval), `on` (until `mode_until`) or `ended`; whether its tasks run any command now and why
 * not; and for such a draft what approving it starts. */
export interface GoalCommands {
  state: 'off' | 'set' | 'on' | 'ended';
  says: string;
  usable: boolean;
  not_usable_why: string | null;
  approve: string | null;
}

/** The create form's part for a goal that may run any command (GET /goals/options `commands`, with that flag on):
 * the host's ceiling, the sentence to type with `{hours}` in it (filled with `hour_one` or `hours_many`, each with
 * `{n}`), and the words of the form and of the goal's page. */
export interface CommandsOptions {
  max_hours: number;
  sentence: string;
  hour_one: string;
  hours_many: string;
  words: Record<string, string>;
}

/** GET /goals/commands: the goals whose tasks may run any command now, for the banner. */
export interface GoalCommandsNow {
  enabled: boolean;
  allowed: boolean;
  active: { ref: string; title: string; until: string; says: string }[];
}

/** `total`: how many goals match the filter (the list holds the newest of them). */
export interface GoalList { enabled: boolean; goals: GoalRow[]; total?: number }

/** A change's answer when the change was made and the goal could not be read back in full: read it again. */
export interface GoalMade extends Pick<GoalRow, 'id' | 'ref' | 'title' | 'state' | 'pause_reason' | 'pending_request'> {
  enabled: true;
  reload: true;
}

export interface Limits {
  total_budget: number;
  day_budget: number;
  deadline_days: number;
  sessions_per_day: number;
  min_interval_seconds: number;
  jobs_per_day: number;
  quiet_hours_ok: boolean;
}
export type NumberLimit = Exclude<keyof Limits, 'quiet_hours_ok'>;
export interface Range { field: NumberLimit; low: number; high: number; words: string }

export interface GoalOptions {
  enabled: boolean;
  presets?: Record<string, Limits>;
  ranges?: Range[];
  has_default_channel?: boolean;
  channel_kinds?: string[];
  bounds?: { title: number; objective: number; done_when: number; channel: number; note: number; deadline_days: number; journal: number };
  driver_running?: boolean;
  spend_note?: string;
  /** F115 per-goal tools, with that flag on: what a new goal may be allowed when it is created. */
  tools?: NewGoalTools;
  /** With goals that may run any command: the create form's part. */
  commands?: CommandsOptions;
}

/** What a new goal may be allowed (GET /goals/options `tools`): the same rows as a goal's own, and the one question
 * asked before the goal is created with any. */
export interface NewGoalTools {
  applies_in: string;
  never: string;
  limit: number;
  confirm: string;
  available: GoalToolOption[];
}

/** What the screen reads of the background halt (GET /background/state). */
export interface HaltState { halted: boolean }

export interface CreateBody {
  title: string;
  objective: string;
  done_when: string;
  preset: string;
  limits?: Limits;
  origin_channel?: string;
  /** F115: the tools the new goal may use without asking, each as PUT /goals/{ref}/tools/{tool} takes it. */
  tools?: NewGoalTool[];
  /** Its tasks may run any command for `hours` from its approval; `confirm` is the sentence the owner typed. */
  commands?: { hours: number; confirm: string };
}

export interface NewGoalTool { tool: string; confirm: true; identity?: string }

/** The tools chosen for a new goal, in the body POST /goals takes; an MCP tool carries the identity the form showed. */
export function newGoalTools(chosen: string[], available: GoalToolOption[]): NewGoalTool[] {
  return chosen.flatMap((tool) => {
    const option = available.find((o) => o.tool === tool);
    if (!option) return [];
    return [option.identity ? { tool, confirm: true as const, identity: option.identity } : { tool, confirm: true as const }];
  });
}

// ── Requests ────────────────────────────────────────────────────────────────
// Every goal route needs the owner's or the proxy's secret: the login proxy adds it, the dashboard sends none.

const BASE = '/goals';
/** A goal's route. A ref is encoded as a path segment whatever it holds. */
export const goalPath = (ref: string) => `${BASE}/${encodeURIComponent(ref)}`;

/** Which goals the list asks for: the open ones (the server's default), every one, or the finished ones. */
export type GoalFilter = 'open' | 'all' | 'finished';
export const listGoals = (filter: GoalFilter = 'open', signal?: AbortSignal) =>
  apiRequest<GoalList>(`${BASE}?state=${filter}`, 'GET', undefined, signal);
export const goalOptions = (signal?: AbortSignal) => apiRequest<GoalOptions>(`${BASE}/options`, 'GET', undefined, signal);
export const getGoal = (ref: string, journal?: number, signal?: AbortSignal) =>
  apiRequest<GoalDetail | { enabled: false }>(`${goalPath(ref)}${journal ? `?journal=${journal}` : ''}`, 'GET', undefined, signal);
// A change answers with the goal in full, or with `reload` when it was made and the goal could not be read back; the
// screen reads the goal again after every change either way.
export const createGoal = (body: CreateBody) =>
  timed((signal) => apiRequest<GoalDetail | GoalMade>(BASE, 'POST', body, signal));
export const goalAction = (ref: string, action: Action, body?: object) =>
  timed((signal) => apiRequest<GoalDetail | GoalMade>(`${goalPath(ref)}/${action}`, 'POST', body, signal));
export const goalCommandsNow = (signal?: AbortSignal) =>
  apiRequest<GoalCommandsNow>(`${BASE}/commands`, 'GET', undefined, signal);

// ── A goal that may run any command ─────────────────────────────────────────────
// Every sentence comes from the server (GET /goals/options `commands.words`, a goal's `commands.says`).

/** The sentence the owner types to allow `hours` hours, built from the server's template. */
export function commandsSentence(words: CommandsOptions, hours: number): string {
  const unit = (hours === 1 ? words.hour_one : words.hours_many).replace('{n}', String(hours));
  return words.sentence.replace('{hours}', unit);
}

/** The most hours a new goal may run any command: the host's ceiling, and its deadline. */
export const commandsMostHours = (words: CommandsOptions, deadlineDays: number) =>
  Math.max(0, Math.min(words.max_hours, deadlineDays * 24));

/** What is wrong with the hours and the typed sentence, in words; null when they can be sent. */
export function commandsProblem(words: CommandsOptions, hours: number, typed: string, deadlineDays: number): string | null {
  const most = commandsMostHours(words, deadlineDays);
  if (!Number.isInteger(hours) || hours < 1 || hours > most) return `Hours must be a whole number from 1 to ${most}.`;
  if (typed.trim() !== commandsSentence(words, hours)) return 'Type the sentence exactly to allow it.';
  return null;
}

// ── The tools a goal may use without asking (F115) ─────────────────────────────
// GET /goals/{ref}/tools, PUT and DELETE /goals/{ref}/tools/{tool}. Every sentence of this section comes from the
// server's answer (nous/goals/grants.py); the page lays them out.

/** What a tool does when a goal may use it, as the server words it. An MCP client tool names its server. */
export interface GoalToolWords {
  tool: string;
  label: string;
  group: string;
  classes: string[];
  does: string;
  note: string;
  says: string;
  confirm: string;
  server_name: string | null;
  server_host: string | null;
  server_id: string | null;
  /** An MCP client tool's identity (its server row, URL digest and remote tool name) as one opaque token; sent back
   * when it is allowed, so a server changed since the page was read is refused. */
  identity: string | null;
}
export interface GoalToolGrant extends GoalToolWords {
  granted_at: string | null;
  usable: boolean;
  not_usable_why: string | null;
}
export interface GoalToolOption extends GoalToolWords {
  /** Why it cannot be allowed now; null when it can. */
  blocked: { code: string; sentence: string } | null;
}
export interface GoalTools {
  enabled: boolean;
  grants_enabled?: boolean;
  can_edit?: boolean;
  limit?: number;
  applies_in?: string;
  never?: string;
  grants?: GoalToolGrant[];
  available?: GoalToolOption[];
  /** A change's answer: whether it changed anything, and whether the server could not read it back yet. */
  changed?: boolean;
  reload?: boolean;
}

export const goalTools = (ref: string, signal?: AbortSignal) =>
  apiRequest<GoalTools>(`${goalPath(ref)}/tools`, 'GET', undefined, signal);
/** Allow: the owner's confirmation, and an MCP client tool's identity as the screen showed it. */
export const allowTool = (ref: string, tool: GoalToolOption) =>
  timed((signal) =>
    apiRequest<GoalTools>(
      `${goalPath(ref)}/tools/${encodeURIComponent(tool.tool)}`,
      'PUT',
      tool.identity ? { confirm: true, identity: tool.identity } : { confirm: true },
      signal,
    ),
  );
export const removeTool = (ref: string, tool: string) =>
  timed((signal) => apiRequest<GoalTools>(`${goalPath(ref)}/tools/${encodeURIComponent(tool)}`, 'DELETE', {}, signal));

export const TOOLS_TITLE = 'Tools this goal may use';
/** On the create form, beside a tool that can be allowed. */
export const TOOL_CHOOSE_LABEL = 'Allow without asking';
export const TOOLS_NONE_TEXT = 'None yet.';
export const TOOLS_ADD_TITLE = 'Tools you can allow';
export const TOOLS_NOT_USED_TEXT = 'Not used now.';
export const TOOL_ALLOWED_TEXT = 'Allowed.';
export const TOOL_ALREADY_ALLOWED_TEXT = 'It was allowed already.';
export const TOOL_REMOVED_TEXT = 'Removed.';
export const TOOL_ALREADY_REMOVED_TEXT = 'It was not allowed.';
/** A change the server made and could not read back into the running work yet (`reload`). */
export const TOOL_RELOAD_TEXT = 'Saved. The server could not read it back yet.';
export const toolsLimitText = (limit: number) => `A goal can have at most ${limit} tools.`;
export const toolAllowedOnText = (at: string | null) => (at ? `Allowed ${fmtUtc(at)}.` : 'Allowed.');
export const toolsReadFailedText = (err: unknown) => `Could not read this goal's tools. ${refusalText(err, 'see')}`;
/** A refused tool change: the server's sentence exactly as sent (it may start with a tool's name), else as for any
 * refused change. */
/** A refused create: a refused tool's sentence exactly as sent (the answer names the tool), else as for any change. */
export function createRefusalText(err: unknown): string {
  const body = (err as ApiAnswerError | null)?.body as { tool?: unknown } | undefined;
  return typeof body?.tool === 'string' ? toolRefusalText(err) : refusalText(err);
}

export function toolRefusalText(err: unknown): string {
  const api = ((err as ApiAnswerError | null)?.body as { error?: unknown } | undefined)?.error;
  return typeof api === 'string' && api.trim() ? api.trim() : refusalText(err);
}

/** The groups of the tools that can be allowed, in the order shown; a server's tools are grouped by server. */
export const TOOL_GROUP_WORDS: Record<string, string> = {
  reaching_outside: 'Reaching outside',
  other_servers: 'Tools from other servers',
};

/** The tools that can be allowed, grouped: the built-in ones, then each MCP server's (`server_name`) in name order. */
export function toolGroups(options: GoalToolOption[]): { key: string; words: string; rows: GoalToolOption[] }[] {
  const groups: { key: string; words: string; rows: GoalToolOption[] }[] = [];
  const builtIn = options.filter((o) => o.server_name === null);
  if (builtIn.length) groups.push({ key: 'built-in', words: TOOL_GROUP_WORDS.reaching_outside, rows: builtIn });
  const servers = [...new Set(options.filter((o) => o.server_name !== null).map((o) => o.server_name as string))].sort();
  for (const server of servers) {
    const rows = options.filter((o) => o.server_name === server);
    groups.push({ key: `server-${server}`, words: `${TOOL_GROUP_WORDS.other_servers}: ${server} (${rows[0].server_host})`, rows });
  }
  return groups;
}

/** A request with no answer after this long is given up: a read says the server could not be reached, a change that
 * its answer was lost. */
export const REQUEST_TIMEOUT_MS = 15_000;

/** Run `request` with a signal that aborts after `REQUEST_TIMEOUT_MS`, or when `outer` aborts. */
export async function timed<T>(request: (signal: AbortSignal) => Promise<T>, outer?: AbortSignal): Promise<T> {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), REQUEST_TIMEOUT_MS);
  const stop = () => ac.abort();
  if (outer?.aborted) ac.abort();
  outer?.addEventListener('abort', stop);
  try {
    return await request(ac.signal);
  } finally {
    clearTimeout(timer);
    outer?.removeEventListener('abort', stop);
  }
}

/** The background halt, when this server has the route. A 404 (no halt on this server) or any failure is null:
 * nothing to show, and never an error of the goals list. */
export async function haltState(signal?: AbortSignal): Promise<HaltState | null> {
  try {
    const answer = await apiRequest<{ halted?: unknown }>('/background/state', 'GET', undefined, signal);
    return { halted: answer?.halted === true };
  } catch {
    return null;
  }
}

// ── Words ───────────────────────────────────────────────────────────────────

export const GOALS_OFF_TEXT = 'Goals are off on this server.';
export const DRIVER_OFF_TEXT = 'Sessions are not being started on this server yet.';
export const HALT_TEXT = 'Background work is halted. No goal starts a session until you resume it.';
export const HELD_TEXT = 'Held by the halt';
export const READ_FORBIDDEN_TEXT = 'Your sign-in is needed to see goals. Open the dashboard through the login page.';
export const WRITE_FORBIDDEN_TEXT = 'Your sign-in is needed to change goals. Open the dashboard through the login page.';
/** A read that got no answer. */
export const UNREACHABLE_TEXT = 'Could not reach the server.';
/** A change whose answer was lost: it may or may not have been made. */
export const NO_ANSWER_TEXT = 'No answer came back. Check the goal before trying again.';
export const NO_CHANNEL_TEXT =
  'This goal has nowhere to ask you questions, so it cannot be approved. Set a default chat on the server, or cancel it and create it again with a channel.';
export const DRAFT_NOTE = 'Nothing runs until you approve it.';
/** A goal's mode in words. */
export const MODE_WORDS: Record<string, string> = { contained: 'Contained', commands: 'May run any command' };
export const CHANNEL_HINT = 'Questions and reports go here, as telegram:<chat id>.';

const PAUSED: Record<PauseReason, { text: string; tone: Tone }> = {
  owner: { text: 'Paused by you', tone: 'warn' },
  budget: { text: 'Paused for its budget', tone: 'warn' },
  stalled: { text: 'Paused: stalled', tone: 'warn' },
  waiting_for_owner: { text: 'Waiting for you to confirm it is done', tone: 'info' },
  error: { text: 'Paused after an error', tone: 'bad' },
  deadline: { text: 'Paused for its deadline', tone: 'warn' },
};

/** A goal's state as a word and a colour. While no session is started on the server (`driverRunning` false), an
 * approved goal is only "Approved": nothing about it is about to run. */
export function stateWord(
  goal: Pick<GoalRow, 'state' | 'pause_reason' | 'open_session'>,
  driverRunning = true,
): { text: string; tone: Tone } {
  switch (goal.state) {
    case 'draft': return { text: 'Draft', tone: 'neutral' };
    case 'active':
      if (!driverRunning) return { text: 'Approved', tone: 'neutral' };
      return goal.open_session !== null ? { text: 'Working', tone: 'info' } : { text: 'Waiting for its next session', tone: 'ok' };
    case 'paused': return (goal.pause_reason && PAUSED[goal.pause_reason]) || { text: 'Paused', tone: 'warn' };
    case 'done': return { text: 'Done', tone: 'ok' };
    case 'failed': return { text: 'Failed', tone: 'bad' };
    case 'cancelled': return { text: 'Cancelled', tone: 'neutral' };
    default: return { text: String(goal.state), tone: 'neutral' };
  }
}

export const PILL: Record<Tone, string> = {
  ok: 'status-pill--ok', warn: 'status-pill--warn', bad: 'status-pill--bad', info: 'status-pill--info', neutral: '',
};

/** Each action's button and what it costs. `spends`: it lets the goal spend tokens; `ends`: it ends a session or the
 * goal. Pause and Note ask nothing first: neither spends nor ends anything. */
export const ACTION_TEXT: Record<Action, { label: string; spends: boolean; ends: boolean; done: string }> = {
  approve: { label: 'Approve', spends: true, ends: false, done: 'Approved.' },
  'run-now': { label: 'Run a session now', spends: true, ends: false, done: 'Asked for a session now.' },
  resume: { label: 'Resume', spends: true, ends: false, done: 'Resumed.' },
  'confirm-done': { label: 'Confirm done', spends: false, ends: true, done: 'Closed as done.' },
  pause: { label: 'Pause', spends: false, ends: false, done: 'Paused: no new session starts. A session already running finishes.' },
  stop: { label: 'Stop now', spends: false, ends: true, done: 'Stopped.' },
  budget: { label: 'Change budget', spends: true, ends: false, done: 'Budget changed.' },
  deadline: { label: 'Extend deadline', spends: true, ends: false, done: 'Deadline extended.' },
  note: { label: 'Add note', spends: false, ends: false, done: 'Added. The next session reads it.' },
  cancel: { label: 'Cancel goal', spends: false, ends: true, done: 'Cancelled.' },
  contained: { label: 'Return to contained', spends: false, ends: false, done: 'Returned to contained.' },
};

/** Whether an action asks for a sentence and a Yes before it is sent. */
export const asksFirst = (action: Action) => ACTION_TEXT[action].spends || ACTION_TEXT[action].ends;

/** The Resume button's words: for a goal waiting for the owner, resuming means its done claim is turned down. */
export const resumeLabel = (goal: Pick<GoalRow, 'pause_reason'>) =>
  goal.pause_reason === 'waiting_for_owner' ? 'Not done yet, keep working' : ACTION_TEXT.resume.label;

/** "2,000,000 tokens". */
export const formatTokens = (n: number) => `${formatNumber(n)} tokens`;
export const formatNumber = (n: number) => n.toLocaleString('en-US');

/** What the owner is told before an action, with the goal's own numbers. `input` holds a budget or deadline change. */
export function confirmText(
  action: Action,
  goal: Pick<GoalDetail, 'total_budget' | 'day_budget' | 'deadline_days' | 'deadline_at' | 'open_session' | 'state'> & {
    commands?: Pick<GoalCommands, 'approve'>;
  },
  opts: { driverRunning: boolean; total?: number; day?: number; days?: number; now?: number } = { driverRunning: false },
): string {
  const ready = opts.driverRunning ? 'This starts the goal.' : 'This marks the goal ready to start.';
  switch (action) {
    case 'approve': {
      const commands = goal.commands?.approve ? ` ${goal.commands.approve}` : '';
      return `${ready} It can work in the background for up to ${goal.deadline_days} days and spend up to ${formatTokens(goal.total_budget)} (${formatNumber(goal.day_budget)} a day).${commands} You can pause or stop it at any time.`;
    }
    case 'run-now':
      return opts.driverRunning
        ? "This starts a session now. It spends tokens from this goal's budget."
        : "This asks for a session as soon as sessions run on this server. It spends tokens from this goal's budget.";
    case 'resume':
      return 'This lets the goal start sessions again. They spend tokens from its budget.';
    case 'budget':
      return budgetText(goal, opts.total, opts.day);
    case 'deadline':
      return deadlineChangeText(goal, opts.days ?? 0, opts.now ?? Date.now());
    case 'stop':
      return goal.open_session !== null
        ? `This cancels session ${goal.open_session}, which is running${goal.state === 'active' ? ', and pauses the goal' : ''}. Work already done stays.`
        : 'This pauses the goal. No session is running, so none is cancelled. Work already done stays.';
    case 'cancel':
      return `This ends the goal for good. It cannot be reopened.${goal.open_session !== null ? ` Session ${goal.open_session}, which is running, is cancelled too.` : ''}`;
    case 'confirm-done':
      return 'This closes the goal as done. Nothing more runs for it.';
    default:
      return '';
  }
}

function budgetText(goal: Pick<GoalDetail, 'total_budget' | 'day_budget'>, total?: number, day?: number): string {
  const parts: string[] = [];
  const change = (what: string, from: number, to: number | undefined) => {
    if (to === undefined || to === from) return;
    parts.push(`${to > from ? 'raises' : 'lowers'} ${what} from ${formatNumber(from)} to ${formatTokens(to)}`);
  };
  change('the total budget', goal.total_budget, total);
  change('the day budget', goal.day_budget, day);
  if (!parts.length) return 'This keeps the budget as it is.';
  const more = (total ?? goal.total_budget) > goal.total_budget || (day ?? goal.day_budget) > goal.day_budget;
  return `This ${parts.join(' and ')}.${more ? ' The goal can spend more.' : ''}`;
}

function deadlineChangeText(goal: Pick<GoalDetail, 'deadline_days' | 'deadline_at'>, days: number, now: number): string {
  const unit = `${days} more day${days === 1 ? '' : 's'}`;
  if (!goal.deadline_at) {
    return `This lets the goal work, and spend tokens, for ${unit} once it is approved: ${goal.deadline_days + days} days in all.`;
  }
  const from = Math.max(new Date(goal.deadline_at).getTime(), now);
  const until = fmtUtc(new Date(from + days * 86_400_000).toISOString());
  return `This lets the goal keep working, and spending tokens, ${unit}, until ${until}.`;
}

const rangeOf = (options: GoalOptions, field: NumberLimit) => options.ranges?.find((r) => r.field === field);
const inRange = (value: number, low: number, high: number) => Number.isInteger(value) && value >= low && value <= high;

/** What is wrong with a new budget, in words; null when it can be sent. The ranges are the server's
 * (`options.ranges`), the same as the create form's. `total` and `day` are what was typed, as numbers (NaN when the
 * text is not a whole number). */
export function budgetProblem(total: number, day: number, goal: Pick<GoalDetail, 'total_budget' | 'day_budget'>, options: GoalOptions): string | null {
  for (const [value, field] of [[total, 'total_budget'], [day, 'day_budget']] as const) {
    const range = rangeOf(options, field) ?? { low: 1, high: Number.MAX_SAFE_INTEGER, words: field };
    if (!Number.isInteger(value)) return capital(`${range.words} must be a whole number`);
    if (!inRange(value, range.low, range.high)) return capital(`${range.words} must be at least ${formatNumber(range.low)} and at most ${formatNumber(range.high)}`);
  }
  if (day > total) return capital('the day budget cannot be larger than the total budget');
  if (total === goal.total_budget && day === goal.day_budget) return 'Type a new total or day budget.';
  return null;
}

/** What is wrong with a number of days to add, in words; null when it can be sent. */
export function deadlineProblem(days: number, options: GoalOptions): string | null {
  const most = options.bounds?.deadline_days ?? 90;
  return inRange(days, 1, most) ? null : `Type a whole number of days, 1 to ${most}.`;
}

/** What is typed in a number field, as a number: NaN unless it is a whole number. */
export const typedNumber = (raw: string) => (/^-?\d+$/.test(raw.trim()) ? Number(raw.trim()) : Number.NaN);

/** A change whose answer was lost: the goal as it was just before it was sent, and what was sent. */
export interface LostChange {
  action: Action;
  /** When it was sent (this browser's clock). */
  at: number;
  before: Pick<GoalDetail, 'state' | 'pause_reason' | 'pending_request' | 'total_budget' | 'day_budget' | 'deadline_days' | 'deadline_at' | 'mode'> & {
    /** The newest journal row's seq, and the newest session's number, before the change. */
    top: number;
    lastSession: number;
  };
  /** A note's text, a budget's new values, a deadline's added days, as sent. */
  text?: string;
  total?: number;
  day?: number;
  days?: number;
}

const DAY_MS = 86_400_000;
/** How far a moved deadline may be from the one this browser expects (the server's clock decides it). */
const DEADLINE_SLACK_MS = 10 * 60_000;

/** The goal as it is before a change is sent, for `changeLanded`. */
export function beforeChange(goal: GoalDetail): LostChange['before'] {
  return {
    state: goal.state,
    pause_reason: goal.pause_reason,
    pending_request: goal.pending_request,
    total_budget: goal.total_budget,
    day_budget: goal.day_budget,
    deadline_days: goal.deadline_days,
    deadline_at: goal.deadline_at,
    mode: goal.mode,
    top: goal.journal[0]?.seq ?? 0,
    lastSession: goal.sessions.reduce((n, s) => Math.max(n, s.n), 0),
  };
}

/** Whether `goal`, read after a change whose answer was lost, shows that very change was made: what that action
 * produces, and not what it was before. Anything else (a session's own journal rows, the harness pausing the goal for
 * another reason, a spend) is no evidence, and "No answer came back" stays. */
export function changeLanded(lost: LostChange, goal: GoalDetail): boolean {
  const b = lost.before;
  switch (lost.action) {
    case 'note': {
      const sent = (lost.text ?? '').trim();
      return goal.journal.some((row) => row.seq > b.top && row.kind === 'owner_note' && row.actor === 'owner' && row.text === sent);
    }
    case 'pause':
      return b.state === 'active' && goal.state === 'paused' && goal.pause_reason === 'owner';
    case 'stop':
      return (b.state === 'active' && goal.state === 'paused' && goal.pause_reason === 'owner')
        || (b.pending_request !== 'stop' && goal.pending_request === 'stop');
    case 'resume':
      return b.state === 'paused' && goal.state === 'active';
    case 'approve':
      return b.state === 'draft' && goal.state === 'active';
    case 'cancel':
      return b.state !== 'cancelled' && goal.state === 'cancelled';
    case 'confirm-done':
      return b.state !== 'done' && goal.state === 'done';
    case 'contained':
      return b.mode !== 'contained' && goal.mode === 'contained';
    case 'run-now':
      return (b.pending_request !== 'run_now' && goal.pending_request === 'run_now')
        || goal.sessions.some((s) => s.n > b.lastSession && s.trigger === 'run_now');
    case 'budget':
      return (lost.total !== undefined || lost.day !== undefined)
        && (lost.total === undefined || goal.total_budget === lost.total)
        && (lost.day === undefined || goal.day_budget === lost.day)
        && (goal.total_budget !== b.total_budget || goal.day_budget !== b.day_budget);
    case 'deadline': {
      const days = lost.days ?? 0;
      if (days < 1) return false;
      if (b.deadline_at === null) return goal.deadline_at === null && goal.deadline_days === b.deadline_days + days;
      if (!goal.deadline_at) return false;
      const expected = Math.max(Date.parse(b.deadline_at), lost.at) + days * DAY_MS;
      return Math.abs(Date.parse(goal.deadline_at) - expected) <= DEADLINE_SLACK_MS;
    }
    default:
      return false;
  }
}

/** The hint beside a Resume that the store would refuse. */
export const RESUME_BLOCKED_TEXT: Record<'budget' | 'deadline', string> = {
  budget: 'Raise the budget first.',
  deadline: 'Extend the deadline first.',
};

export const PENDING_TEXT: Record<'run_now' | 'stop' | 'cancel', string> = {
  run_now: 'A session was asked for. It has not started yet.',
  stop: 'A stop was asked for. It has not happened yet.',
  cancel: 'A cancel was asked for. It has not happened yet.',
};

const OUTCOMES: Record<string, string> = {
  step_closed: 'Closed a step', no_step: 'No step closed', asked: 'Asked you a question', error: 'Error', interrupted: 'Interrupted',
};
export const sessionOutcomeWord = (outcome: string | null) => (outcome === null ? 'Running' : OUTCOMES[outcome] ?? outcome);

const KINDS: Record<string, string> = {
  plan: 'Plan', progress: 'Progress', report: 'Report', question: 'Question', answer: 'Your answer', owner_note: 'Your note',
  state: 'State', done_claim: 'Says it is done',
};
export const journalKindWord = (kind: string) => KINDS[kind] ?? kind;
/** The owner's own rows are shown first-class. */
export const fromOwner = (row: Pick<JournalRow, 'kind'>) => row.kind === 'owner_note' || row.kind === 'answer';

const ACTORS: Record<string, string> = { owner: 'You', host: 'The host', chain: 'The chain', harness: 'The harness' };
export const actorWord = (actor: string) => ACTORS[actor] ?? actor;

const PROPOSERS: Record<string, string> = { owner: 'You', agent: 'Nous', host: 'The host command' };
export const proposerWord = (who: string) => PROPOSERS[who] ?? who;

/** "3 days", "2 hours", "15 minutes", "under a minute". */
export function spanText(ms: number): string {
  const minutes = Math.floor(Math.abs(ms) / 60_000);
  if (minutes < 1) return 'under a minute';
  const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? '' : 's'}`;
  if (minutes < 60) return plural(minutes, 'minute');
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return plural(hours, 'hour');
  return plural(Math.floor(hours / 24), 'day');
}

/** The interval between sessions in words: "1 hour", "30 minutes". */
export function intervalText(seconds: number): string {
  return spanText(seconds * 1000);
}

/** When the next session is, in words; '' when the state word already says it, and for an approved goal while no
 * session is started on the server. */
export function nextSessionText(
  goal: Pick<GoalRow, 'state' | 'pause_reason' | 'open_session' | 'next_wake_at' | 'pending_request'>,
  now: number = Date.now(),
  driverRunning = true,
): string {
  if (goal.state === 'active' && !driverRunning) return '';
  if (goal.open_session !== null) return `Session ${goal.open_session} running`;
  if (goal.state === 'draft') return 'Not approved yet';
  if (goal.state !== 'active') return '';
  if (goal.pending_request === 'run_now') return 'A session was asked for';
  if (!goal.next_wake_at) return 'No session planned';
  const wait = new Date(goal.next_wake_at).getTime() - now;
  return wait <= 0 ? 'Next session due now' : `Next session in ${spanText(wait)}`;
}

/** The deadline in words; '' for a finished goal. */
export function deadlineText(goal: Pick<GoalRow, 'state' | 'deadline_at'> & { deadline_days?: number }, now: number = Date.now()): string {
  if (goal.state === 'done' || goal.state === 'failed' || goal.state === 'cancelled') return '';
  if (!goal.deadline_at) return goal.deadline_days ? `Runs for ${goal.deadline_days} days once approved` : '';
  const left = new Date(goal.deadline_at).getTime() - now;
  return left <= 0 ? 'Deadline passed' : `Deadline in ${spanText(left)}`;
}

/** Part of a budget spent, 0 to 100, for a bar. */
export const spentPercent = (spent: number, budget: number) => (budget > 0 ? Math.min(100, Math.round((spent / budget) * 100)) : 0);

/** A refused or failed request in words: the server's own sentence when it sent one, never an object or a stack. */
export function refusalText(err: unknown, doing: 'see' | 'change' = 'change'): string {
  const e = err as ApiAnswerError | null;
  const status = e?.status;
  if (status === undefined) return doing === 'see' ? UNREACHABLE_TEXT : NO_ANSWER_TEXT;
  if (status === 403) return doing === 'see' ? READ_FORBIDDEN_TEXT : WRITE_FORBIDDEN_TEXT;
  if (status === 415) return 'The request was not sent as JSON, so it was refused.';
  const api = (e?.body as { error?: unknown } | undefined)?.error;
  if (typeof api === 'string' && api.trim()) {
    const text = api.trim();
    return `${text[0].toUpperCase()}${text.slice(1)}${/[.!?]$/.test(text) ? '' : '.'}`;
  }
  if (status === 503) return GOALS_OFF_TEXT;
  return `The request failed (HTTP ${status}).`;
}

// ── The create form ─────────────────────────────────────────────────────────

export const PRESET_NAMES = ['small', 'medium', 'large'] as const;
export const PRESET_LABEL: Record<string, string> = { small: 'Small', medium: 'Medium', large: 'Large' };
export const LIMIT_LABEL: Record<NumberLimit, string> = {
  total_budget: 'Total budget (tokens)',
  day_budget: 'Day budget (tokens)',
  deadline_days: 'Deadline (days from approval)',
  sessions_per_day: 'Sessions per day',
  min_interval_seconds: 'Shortest gap between sessions (seconds)',
  jobs_per_day: 'Job launches per day',
};

export interface CreateForm {
  title: string;
  objective: string;
  done_when: string;
  preset: string;
  custom: boolean;
  limits: Record<NumberLimit, string>;
  quiet_hours_ok: boolean;
  origin_channel: string;
}

const capital = (text: string) => `${text[0].toUpperCase()}${text.slice(1)}.`;

/** What is wrong with the form, in words, before anything is sent. The limits follow the server's ranges (the same
 * rules and words as nous/goals/envelope.py `problems`). */
export function createProblems(form: CreateForm, options: GoalOptions): string[] {
  const found: string[] = [];
  const bounds = options.bounds;
  const text = (value: string, missing: string, label: string, longest: number | undefined) => {
    if (!value.trim()) found.push(missing);
    else if (longest !== undefined && value.length > longest) found.push(`${label} is at most ${formatNumber(longest)} characters.`);
  };
  text(form.title, 'Give it a title.', 'The title', bounds?.title);
  text(form.objective, 'Give it an objective.', 'The objective', bounds?.objective);
  text(form.done_when, 'Say what done looks like.', 'What done looks like', bounds?.done_when);
  if (form.custom) {
    const limits: string[] = [];
    const values: Partial<Record<NumberLimit, number>> = {};
    for (const range of options.ranges ?? []) {
      const raw = (form.limits[range.field] ?? '').trim();
      if (!/^-?\d+$/.test(raw)) {
        limits.push(`${range.words} must be a whole number`);
        continue;
      }
      const value = Number(raw);
      values[range.field] = value;
      if (value < range.low || value > range.high) limits.push(`${range.words} must be at least ${formatNumber(range.low)} and at most ${formatNumber(range.high)}`);
    }
    if (!limits.length && (values.day_budget ?? 0) > (values.total_budget ?? 0)) {
      limits.push('the day budget cannot be larger than the total budget');
    }
    found.push(...limits.map(capital));
  } else if (!options.presets?.[form.preset]) {
    found.push('Choose a size.');
  }
  const channel = form.origin_channel.trim();
  if (!channel) {
    if (options.has_default_channel === false) found.push('Give it a channel: this server has no default chat to ask you.');
  } else {
    const [kind, ...rest] = channel.split(':');
    const chat = rest.join(':');
    if (!(options.channel_kinds ?? ['telegram']).includes(kind) || !chat || /\s/.test(chat)) {
      found.push('The channel must be telegram:<chat id>.');
    }
  }
  return found;
}

/** The body of POST /goals for a form with no problems. */
export function createBody(form: CreateForm): CreateBody {
  const body: CreateBody = {
    title: form.title.trim(),
    objective: form.objective.trim(),
    done_when: form.done_when.trim(),
    preset: form.custom ? 'custom' : form.preset,
  };
  if (form.custom) {
    const n = (field: NumberLimit) => Number(form.limits[field].trim());
    body.limits = {
      total_budget: n('total_budget'),
      day_budget: n('day_budget'),
      deadline_days: n('deadline_days'),
      sessions_per_day: n('sessions_per_day'),
      min_interval_seconds: n('min_interval_seconds'),
      jobs_per_day: n('jobs_per_day'),
      quiet_hours_ok: form.quiet_hours_ok,
    };
  }
  if (form.origin_channel.trim()) body.origin_channel = form.origin_channel.trim();
  return body;
}

/** A preset's numbers as the form's text fields. */
export function limitFields(limits: Limits): Record<NumberLimit, string> {
  return {
    total_budget: String(limits.total_budget),
    day_budget: String(limits.day_budget),
    deadline_days: String(limits.deadline_days),
    sessions_per_day: String(limits.sessions_per_day),
    min_interval_seconds: String(limits.min_interval_seconds),
    jobs_per_day: String(limits.jobs_per_day),
  };
}

/** A preset in one line: "2,000,000 tokens, 500,000 a day, 3 days". */
export const presetLine = (limits: Limits) =>
  `${formatTokens(limits.total_budget)}, ${formatNumber(limits.day_budget)} a day, ${limits.deadline_days} days`;
