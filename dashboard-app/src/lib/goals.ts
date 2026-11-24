// F114 goals PR 2: the Goals screen's types, requests and words. The answers are those of nous/api/goal_routes.py;
// every sentence the owner reads before an action, and every word for a state, is here (one table, pinned by
// goals.test.ts). Text a goal's sessions wrote (plan, journal, summaries) is shown as text nodes only.

import { apiRequest, type ApiAnswerError } from './api';
import { fmtUtc } from './harness';

export type GoalState = 'draft' | 'active' | 'paused' | 'done' | 'failed' | 'cancelled';
export type PauseReason = 'owner' | 'budget' | 'stalled' | 'waiting_for_owner' | 'error' | 'deadline';
/** The owner's actions on a goal, in the order the server lists them: each is the last segment of its route. */
export const ACTIONS = ['approve', 'run-now', 'resume', 'confirm-done', 'pause', 'stop', 'budget', 'sessions', 'deadline', 'note', 'cancel', 'contained'] as const;
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
  /** How many approval requests and questions its sessions wait on you for; null when the server could not read it. */
  awaiting_count: number | null;
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
  /** Goal pacing, sent only with its flag on: "HH:MM" for a goal that works once a day, else null. */
  daily_at?: string | null;
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
  /** What its sessions wait on you for, oldest first; null when the server could not read it. */
  awaiting: Awaiting | null;
}

/** An approval request a goal's session waits on you for. `tool`, `arguments` (the call, exactly as it will run) and
 * `why` are the approval message's own text; `id` is what POST /intentions/proposals/{id}/decide takes. */
export interface AwaitingProposal {
  id: string;
  short_id: string;
  tool: string;
  arguments: string;
  why: string;
  expires_at: string | null;
}
/** A question a goal's session waits on you for; `id` is what POST /intentions/questions/{id}/answer takes. */
export interface AwaitingQuestion {
  id: string;
  short_id: string;
  text: string;
  expires_at: string;
}
export interface Awaiting { proposals: AwaitingProposal[]; questions: AwaitingQuestion[] }

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
 * `{n}`), the words of the form and of the goal's page, and the store's refusals of the hours (with `{n}`) and of
 * the sentence. */
export interface CommandsOptions {
  max_hours: number;
  sentence: string;
  hour_one: string;
  hours_many: string;
  words: Record<string, string>;
  refusals: { hours_range: string; confirm: string };
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
  /** Goal pacing, with its flag on: the zone of once-a-day goals, the stall limit's range, the form's default time. */
  pacing?: PacingOptions;
  /** With the goal activity panel on: a goal's page reads what it is doing now and its sections fold. */
  activity?: boolean;
}

/** GET /goals/options `pacing`. */
export interface PacingOptions {
  zone: string;
  stall_limit: { low: number; high: number };
  default_at: string;
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
  /** Goal pacing: the goal works once a day from this time ("HH:MM"). */
  daily_at?: string;
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

/** What is wrong with the hours and the typed sentence, in the server's words; null when they can be sent. */
export function commandsProblem(words: CommandsOptions, hours: number, typed: string, deadlineDays: number): string | null {
  const most = commandsMostHours(words, deadlineDays);
  if (!Number.isInteger(hours) || hours < 1 || hours > most) return words.refusals.hours_range.replace('{n}', String(most));
  if (typed.trim() !== commandsSentence(words, hours)) return words.refusals.confirm;
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
  sessions: { label: 'Change how it works', spends: true, ends: false, done: 'Changed. It applies from the next session.' },
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
    case 'sessions':
      return SESSIONS_NOTE;
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
  if (total === goal.total_budget && day === goal.day_budget) return BUDGET_UNCHANGED_TEXT;
  return null;
}
/** Said in place of a problem while the budget form still holds the goal's own budget: a hint, not a warning. */
export const BUDGET_UNCHANGED_TEXT = 'Type a new total or day budget.';

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
  /** A note's text, a budget's new values, a deadline's added days, the session settings, as sent. */
  text?: string;
  total?: number;
  day?: number;
  days?: number;
  sessions?: SessionsBody;
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
    case 'sessions':
      // The owner's row of a change, and every value this change asked for: a change made elsewhere is not this one.
      return sessionsHeld(lost.sessions, goal)
        && goal.journal.some((row) => row.seq > b.top && row.kind === 'state' && row.actor === 'owner' && row.text.startsWith(SESSIONS_ROW));
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
  min_interval_seconds: 'Shortest gap between sessions (minutes)',
  jobs_per_day: 'Job launches per day',
};

/** A limit's range as the forms show it: the gap between sessions in whole minutes (the server's range, like the value
 * it takes, is in seconds), every other limit as the server sends it. */
export function formRange(range: Range): { low: number; high: number; words: string; unit: string } {
  if (range.field !== 'min_interval_seconds') return { low: range.low, high: range.high, words: range.words, unit: '' };
  return { low: Math.ceil(range.low / 60), high: Math.floor(range.high / 60), words: 'the shortest gap between sessions', unit: ' minutes' };
}

export interface CreateForm {
  title: string;
  objective: string;
  done_when: string;
  preset: string;
  custom: boolean;
  /** As typed; `min_interval_seconds` is typed in minutes (`createBody` sends seconds). */
  limits: Record<NumberLimit, string>;
  quiet_hours_ok: boolean;
  origin_channel: string;
  /** Goal pacing: once a day, from `daily_at` ("HH:MM"). */
  daily?: boolean;
  daily_at?: string;
}

const capital = (text: string) => `${text[0].toUpperCase()}${text.slice(1)}.`;

/** What is wrong with the form, in words, before anything is sent. The limits follow the server's ranges (the same
 * rules and words as nous/goals/envelope.py `problems`, but the gap between sessions in minutes: `formRange`). */
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
    for (const sent of options.ranges ?? []) {
      const range = formRange(sent);
      const raw = (form.limits[sent.field] ?? '').trim();
      if (!/^-?\d+$/.test(raw)) {
        limits.push(`${range.words} must be a whole number`);
        continue;
      }
      const value = Number(raw);
      values[sent.field] = value;
      if (value < range.low || value > range.high) {
        limits.push(`${range.words} must be at least ${formatNumber(range.low)} and at most ${formatNumber(range.high)}${range.unit}`);
      }
    }
    if (!limits.length && (values.day_budget ?? 0) > (values.total_budget ?? 0)) {
      limits.push('the day budget cannot be larger than the total budget');
    }
    found.push(...limits.map(capital));
  } else if (!options.presets?.[form.preset]) {
    found.push('Choose a size.');
  }
  if (form.daily && !TIME_OF_DAY.test((form.daily_at ?? '').trim())) found.push(TIME_PROBLEM);
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
      min_interval_seconds: n('min_interval_seconds') * 60,
      jobs_per_day: n('jobs_per_day'),
      quiet_hours_ok: form.quiet_hours_ok,
    };
  }
  if (form.origin_channel.trim()) body.origin_channel = form.origin_channel.trim();
  if (form.daily) body.daily_at = (form.daily_at ?? '').trim();
  return body;
}

/** A preset's numbers as the form's text fields, the gap between sessions in minutes. */
export function limitFields(limits: Limits): Record<NumberLimit, string> {
  return {
    total_budget: String(limits.total_budget),
    day_budget: String(limits.day_budget),
    deadline_days: String(limits.deadline_days),
    sessions_per_day: String(limits.sessions_per_day),
    min_interval_seconds: String(limits.min_interval_seconds / 60),
    jobs_per_day: String(limits.jobs_per_day),
  };
}

/** A preset in one line: "2,000,000 tokens, 500,000 a day, 3 days". */
export const presetLine = (limits: Limits) =>
  `${formatTokens(limits.total_budget)}, ${formatNumber(limits.day_budget)} a day, ${limits.deadline_days} days`;

// ── Goal pacing: how often a goal works, and its session settings ─────────────────────
// With the server's pacing on (GET /goals/options `pacing`): the create form's "How often should it work?", a goal's
// "How often" row, and "Change how it works" (POST /goals/{ref}/sessions), which sends only what changed.

/** "HH:MM", 00:00 to 23:59: a time of day as the server takes it. */
export const TIME_OF_DAY = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;
export const TIME_PROBLEM = 'Type the time of day as HH:MM, from 00:00 to 23:59.';
/** What the owner reads before a change of session settings is sent (`sessionsNote` adds Run a session now's, where
 * it is offered, and the stall limit's). */
export const SESSIONS_NOTE = 'This applies from the next session. A session that is running now is not changed.';
export const RUN_NOW_NOTE = `To start a session sooner, press ${ACTION_TEXT['run-now'].label}.`;
/** Said in place of a problem while nothing in the form was changed: a hint, not a warning. */
export const SESSIONS_UNCHANGED_TEXT = 'Change a setting first.';
/** The stall limit's label is short; this says what it means, under the number fields. */
export const STALL_HINT = 'Stall limit: the goal pauses after this many sessions in a row that finish nothing.';
/** Said beside a once-a-day goal's sessions per day, which the server does not read for it. */
export const ONCE_A_DAY_SESSIONS_NOTE = 'Sessions per day does not apply: a goal that works once a day starts one session a day.';
/** How the server's journal row of such a change begins. */
const SESSIONS_ROW = 'Session settings: ';
export const HOW_OFTEN_TEXT = { often: 'As often as its size allows', daily: 'Once a day' } as const;

/** Whether `goal` holds every session setting `asked` sets; false when it asks for nothing. */
function sessionsHeld(asked: SessionsBody | undefined, goal: GoalDetail): boolean {
  if (!asked) return false;
  const held: boolean[] = [];
  for (const field of ['sessions_per_day', 'min_interval_seconds', 'jobs_per_day', 'quiet_hours_ok', 'stall_limit'] as const) {
    if (asked[field] !== undefined) held.push(goal[field] === asked[field]);
  }
  if (asked.daily !== undefined) held.push(asked.daily ? goal.daily_at === asked.daily_at : !goal.daily_at);
  return held.length > 0 && held.every(Boolean);
}

/** The hint under a once-a-day time: on the create form (`create`), or on a goal's page (`change`). The zone is said
 * beside the time itself (`zoneWords`). */
export function dailyHint(where: 'create' | 'change'): string {
  const then = where === 'create'
    ? 'If you approve it after that time of day, its first session starts right away.'
    : 'If that time has passed today and no session ran today, one starts at once.';
  return `It starts one session at that time each day. ${then}`;
}

/** The server's time zone in plain words, beside a time of day: "New York time" for America/New_York; a zone that does
 * not name a place (UTC, Etc/GMT+5, US/Eastern) as the server names it. */
export function zoneWords(zone: string): string {
  const place = /^(?:Africa|America|Antarctica|Asia|Atlantic|Australia|Europe|Indian|Pacific)\/(?:[A-Za-z_-]+\/)?([A-Za-z_-]+)$/.exec(zone)?.[1];
  return place ? `${place.replace(/_/g, ' ')} time` : zone;
}

/** A goal's sessions per day as its facts show it: for a once-a-day goal, why the number does not count. */
export const sessionsPerDayText = (goal: Pick<GoalDetail, 'sessions_per_day' | 'daily_at'>) =>
  goal.daily_at ? 'One, as it works once a day' : String(goal.sessions_per_day);

/** A goal's "How often" in words. */
export function howOftenText(goal: Pick<GoalDetail, 'daily_at'>, options: GoalOptions): string {
  if (!goal.daily_at) return 'As often as its limits allow';
  const zone = options.pacing?.zone;
  return `Once a day, from ${goal.daily_at}${zone ? ` (${zone})` : ''}`;
}

/** The session settings form, as typed. The gap is in minutes. */
export interface SessionsForm {
  sessions_per_day: string;
  gap_minutes: string;
  jobs_per_day: string;
  quiet_hours_ok: boolean;
  stall_limit: string;
  daily: boolean;
  daily_at: string;
}

/** The body of POST /goals/{ref}/sessions. */
export interface SessionsBody {
  sessions_per_day?: number;
  min_interval_seconds?: number;
  jobs_per_day?: number;
  quiet_hours_ok?: boolean;
  stall_limit?: number;
  daily?: boolean;
  daily_at?: string;
}

/** The form as it starts: the goal's own settings, and the server's default time for a goal that is not once a day. */
export function sessionsForm(
  goal: Pick<GoalDetail, 'sessions_per_day' | 'min_interval_seconds' | 'jobs_per_day' | 'quiet_hours_ok' | 'stall_limit' | 'daily_at'>,
  options: GoalOptions,
): SessionsForm {
  return {
    sessions_per_day: String(goal.sessions_per_day),
    gap_minutes: String(goal.min_interval_seconds / 60),
    jobs_per_day: String(goal.jobs_per_day),
    quiet_hours_ok: goal.quiet_hours_ok,
    stall_limit: String(goal.stall_limit),
    daily: Boolean(goal.daily_at),
    daily_at: goal.daily_at || options.pacing?.default_at || '09:00',
  };
}

const NUMBER_FIELDS = ['sessions_per_day', 'gap_minutes', 'jobs_per_day', 'stall_limit'] as const;
type SessionsNumber = typeof NUMBER_FIELDS[number];

/** Whether a number field was changed: its text, and then its value (" 04 " is still 4). */
function numberChanged(form: SessionsForm, start: SessionsForm, field: SessionsNumber): boolean {
  if (form[field].trim() === start[field].trim()) return false;
  return typedNumber(form[field]) !== typedNumber(start[field]) || Number.isNaN(typedNumber(form[field]));
}

/** Only what changed, as the route takes it. */
export function sessionsBody(form: SessionsForm, start: SessionsForm): SessionsBody {
  const body: SessionsBody = {};
  // A once-a-day goal's sessions per day is not read by the server: it is neither sent nor checked.
  if (!form.daily && numberChanged(form, start, 'sessions_per_day')) body.sessions_per_day = typedNumber(form.sessions_per_day);
  if (numberChanged(form, start, 'gap_minutes')) body.min_interval_seconds = typedNumber(form.gap_minutes) * 60;
  if (numberChanged(form, start, 'jobs_per_day')) body.jobs_per_day = typedNumber(form.jobs_per_day);
  if (form.quiet_hours_ok !== start.quiet_hours_ok) body.quiet_hours_ok = form.quiet_hours_ok;
  if (numberChanged(form, start, 'stall_limit')) body.stall_limit = typedNumber(form.stall_limit);
  if (form.daily !== start.daily || (form.daily && form.daily_at.trim() !== start.daily_at.trim())) {
    body.daily = form.daily;
    if (form.daily) body.daily_at = form.daily_at.trim();
  }
  return body;
}

/** What is wrong with the form, in words; null when it can be sent. Only a changed setting is checked, against the
 * server's ranges: the create form's, the gap in whole minutes, and the stall limit's from `options.pacing`. */
export function sessionsProblem(form: SessionsForm, start: SessionsForm, options: GoalOptions): string | null {
  const range = (field: NumberLimit) => rangeOf(options, field);
  const gap = formRange(range('min_interval_seconds') ?? { field: 'min_interval_seconds', low: 60, high: 604_800, words: '' });
  const stall = options.pacing?.stall_limit ?? { low: 1, high: 10 };
  const checks: [SessionsNumber, string, number, number, string][] = [
    ['sessions_per_day', range('sessions_per_day')?.words ?? 'sessions per day', range('sessions_per_day')?.low ?? 1, range('sessions_per_day')?.high ?? 24, ''],
    ['gap_minutes', gap.words, gap.low, gap.high, gap.unit],
    ['jobs_per_day', range('jobs_per_day')?.words ?? 'jobs per day', range('jobs_per_day')?.low ?? 0, range('jobs_per_day')?.high ?? 20, ''],
    ['stall_limit', 'the stall limit', stall.low, stall.high, ''],
  ];
  for (const [field, words, low, high, unit] of checks) {
    if (!numberChanged(form, start, field) || (field === 'sessions_per_day' && form.daily)) continue;
    const value = typedNumber(form[field]);
    if (!Number.isInteger(value)) return capital(`${words} must be a whole number`);
    if (!inRange(value, low, high)) return capital(`${words} must be at least ${formatNumber(low)} and at most ${formatNumber(high)}${unit}`);
  }
  if (form.daily && !TIME_OF_DAY.test(form.daily_at.trim())) return TIME_PROBLEM;
  if (!Object.keys(sessionsBody(form, start)).length) return SESSIONS_UNCHANGED_TEXT;
  return null;
}

/** The sentence before a change of session settings is sent: `SESSIONS_NOTE`; `RUN_NOW_NOTE` when the goal offers Run
 * a session now (`runNow`); and when the stall limit is lowered, that a goal whose newest sessions already reach it is
 * paused (the server's next check pauses it; the change never does). */
export function sessionsNote(form: SessionsForm, start: SessionsForm, runNow: boolean): string {
  const note = runNow ? `${SESSIONS_NOTE} ${RUN_NOW_NOTE}` : SESSIONS_NOTE;
  const stall = typedNumber(form.stall_limit);
  if (!numberChanged(form, start, 'stall_limit') || !(stall < typedNumber(start.stall_limit))) return note;
  const newest = stall === 1 ? 'its last session' : `its last ${stall} sessions in a row`;
  return `${note} If ${newest} finished nothing, Nous pauses it at its next check.`;
}

// ── What a goal is doing now, and the folding page (GET /goals/options `activity`) ─────────────────────────────
// GET /goals/{ref}/activity (nous/goals/activity.py). The answer holds numbers, times, ids and words from closed sets;
// the only text the agent wrote in it is a task's, workflow's or step's name, shown as a text node.

export type ActivityPhase = 'none' | 'task' | 'workflow' | 'waiting' | 'approval' | 'deciding' | 'asking' | 'between';
export type WaitingFor = { kind: 'job'; job_id: string } | { kind: 'pr'; number: number } | { kind: 'file' } | { kind: 'other' };
/** Where a call was made: the chain's decision turns, a task, a workflow step, or other work. */
export type WorkKind = 'decisions' | 'task' | 'workflow' | 'other';
/** A call, at the time it ended; `tool` is `other` for a name that is not shaped like a tool's. */
export interface ActivityCall { tool: string; at: string; outcome: string; in: WorkKind }
export interface ActivitySession { n: number; open: boolean; started_at: string; ended_at: string | null }
export interface ActivityCurrent {
  kind: 'task' | 'workflow';
  /** Null for the session's first task (its purpose line only repeats the goal's title). */
  name: string | null;
  first_task: boolean;
  status: 'running' | 'pending';
  started_at: string | null;
  waiting_for: WaitingFor | null;
}
export interface ActivityCalls { total: number; errors: number; capped: boolean }
/** Coding jobs the session started; `capped` when more shell calls ran than were read, so `launched` is a least count. */
export interface ActivityJobs { launched: number; last_launch_at: string | null; capped: boolean }
export interface GoalActivity {
  enabled: true;
  activity_enabled: true;
  log_enabled: boolean;
  session: ActivitySession | null;
  phase: ActivityPhase;
  current: ActivityCurrent | null;
  running_count: number;
  last_call_at: string | null;
  calls: ActivityCalls | null;
  jobs: ActivityJobs | null;
  recent: ActivityCall[];
  work_sessions: { id: string; kind: WorkKind }[];
}
/** The route's answer with goals off, or with the panel off on the server. */
export type GoalActivityOff = { enabled: false } | { enabled: true; activity_enabled: false };

export const getGoalActivity = (ref: string, signal?: AbortSignal) =>
  apiRequest<GoalActivity | GoalActivityOff>(`${goalPath(ref)}/activity`, 'GET', undefined, signal);
/** Whether an answer carries the panel (the server's flag may have changed since the options were read). */
export const activityOn = (answer: GoalActivity | GoalActivityOff): answer is GoalActivity =>
  answer.enabled === true && answer.activity_enabled === true;

export const ACTIVITY_TITLE = 'What it is doing now';
export const ACTIVITY_ENDED_TITLE = 'Last session';
export const ACTIVITY_LOADING_TEXT = 'Reading what it is doing.';
export const ACTIVITY_FAILED_TEXT = 'Could not read what the goal is doing.';
export const ACTIVITY_STALE_TEXT = `${ACTIVITY_FAILED_TEXT} What is shown is from the last read.`;
export const ACTIVITY_LOG_OFF_TEXT = 'The tool-call log is off, so individual calls are not listed.';
export const NO_SESSION_TEXT = 'No session has run yet.';
export const WORK_LINKS_TEXT = 'See this session on the Work screen:';
/** From this many minutes without a tool call, the panel says a long command or a job can take that long. */
export const QUIET_MINUTES = 10;

const ago = (at: string, now: number) => `${spanText(now - Date.parse(at))} ago`;

function waitingText(wait: WaitingFor | null, more: string): string {
  if (wait?.kind === 'job') return `Waiting for a coding job (${wait.job_id}) to finish${more}.`;
  if (wait?.kind === 'pr') return `Waiting for pull request ${wait.number}${more}.`;
  if (wait?.kind === 'file') return `Waiting for a file to appear${more}.`;
  return `Waiting for a condition${more}.`;
}

/** What the open session is doing, in one line: the phase, with the current item's name where it has one. */
export function phaseText(a: Pick<GoalActivity, 'session' | 'phase' | 'current' | 'running_count'>): string {
  if (a.session === null) return NO_SESSION_TEXT;
  const c = a.current;
  const more = a.running_count > 1 ? ` and ${a.running_count - 1} more` : '';
  const waiting = c?.status === 'pending';
  switch (a.phase) {
    case 'task':
      if (c?.first_task) return `${waiting ? 'Waiting to start' : 'Working on'} its first task of this session${more}.`;
      return `${waiting ? 'Waiting to start a task' : 'Working on a task'}: ${c?.name ?? ''}${more}`;
    case 'workflow':
      return `${waiting ? 'Waiting to start a workflow' : 'Running a workflow'}: ${c?.name ?? ''}${more}`;
    case 'waiting':
      return waitingText(c?.waiting_for ?? null, more);
    case 'approval':
      return `Waiting for your approval of a workflow step${more}.`;
    case 'deciding':
      return 'Deciding what to do next.';
    case 'asking':
      return 'Waiting for your answer to a question.';
    default:
      return 'Between steps.';
  }
}

const OUTCOME_WORDS: Record<string, string> = {
  ran: 'done', error: 'failed', refused: 'refused', gated: 'held for approval', suppressed: 'skipped', retired: 'retired tool',
  bad_input: 'bad input', other: 'other',
};
export const outcomeWord = (outcome: string) => OUTCOME_WORDS[outcome] ?? 'other';
const WORK_KIND_WORDS: Record<string, string> = { decisions: 'decisions', task: 'task', workflow: 'workflow', other: 'other' };
export const workKindWord = (kind: string) => WORK_KIND_WORDS[kind] ?? 'other';
/** A Work-screen link's name in a sentence: "the task"; numbered ("task 1", "task 2") when a kind repeats. */
const WORK_LINK_WORDS: Record<string, [string, string]> = {
  decisions: ['the decisions', 'decisions'], task: ['the task', 'task'], workflow: ['the workflow step', 'workflow step'],
  other: ['other work', 'other work'],
};
export function workLinks(sessions: { id: string; kind: WorkKind }[]): { id: string; label: string }[] {
  const words = (kind: string) => WORK_LINK_WORDS[kind] ?? WORK_LINK_WORDS.other;
  const total = new Map<string, number>();
  for (const { kind } of sessions) total.set(words(kind)[1], (total.get(words(kind)[1]) ?? 0) + 1);
  const seen = new Map<string, number>();
  return sessions.map(({ id, kind }) => {
    const [one, many] = words(kind);
    const n = (seen.get(many) ?? 0) + 1;
    seen.set(many, n);
    return { id, label: (total.get(many) ?? 0) > 1 ? `${many} ${n}` : one };
  });
}
/** What goes before link `i` of `n` in a sentence: nothing, a comma, or " and " before the last. */
export const linkSeparator = (i: number, n: number) => (i === 0 ? '' : i === n - 1 ? ' and ' : ', ');

const callCount = (calls: ActivityCalls) =>
  calls.capped ? `more than ${formatNumber(calls.total)} tool calls` : `${formatNumber(calls.total)} tool call${calls.total === 1 ? '' : 's'}`;

/** "31 tool calls this session, 2 failed." */
export function callsText(calls: ActivityCalls): string {
  const count = callCount(calls);
  const failed = calls.errors ? `, ${calls.capped ? 'at least ' : ''}${formatNumber(calls.errors)} failed` : '';
  return `${count[0].toUpperCase()}${count.slice(1)} this session${failed}.`;
}

/** When the current task or workflow started, naming which (a wait step's job has no start time in the answer). */
export const startedText = (kind: ActivityCurrent['kind'], at: string, now: number) =>
  `The ${kind === 'workflow' ? 'workflow' : 'task'} started ${ago(at, now)}.`;
export const lastCallText = (at: string, tool: string | null, now: number) =>
  `Last tool call ${ago(at, now)}${tool ? ` (${tool})` : ''}.`;
export const noCallYetText = (startedAt: string, now: number) => `No tool call yet in this session. It started ${ago(startedAt, now)}.`;

/** Said in place of the last-call line once the open session has gone `QUIET_MINUTES` without a tool call; '' before. */
export function quietText(lastCallAt: string, tool: string | null, now: number): string {
  const quiet = now - Date.parse(lastCallAt);
  if (quiet < QUIET_MINUTES * 60_000) return '';
  const last = tool ? `; the last one was ${tool}` : '';
  return `No tool call for ${spanText(quiet)}${last}. A long command or a coding job can take this long; a call shows here when it ends.`;
}

/** Coding jobs the session started ("at least" when the server read only its newest shell calls); '' when none. It
 * never says whether a job still runs. */
export function jobsText(jobs: ActivityJobs, now: number): string {
  if (jobs.launched <= 0) return '';
  const last = jobs.last_launch_at ? ` The last one ${ago(jobs.last_launch_at, now)}.` : '';
  return `Coding jobs started this session: ${jobs.capped ? 'at least ' : ''}${formatNumber(jobs.launched)}.${last}`;
}

/** The newest session once it has ended, in one line under the "Last session" heading. */
export function endedText(session: ActivitySession, calls: ActivityCalls | null, now: number): string {
  const when = session.ended_at ? ` ended ${ago(session.ended_at, now)}` : ' ended';
  return `Session ${session.n}${when}${calls ? ` after ${callCount(calls)}` : ''}.`;
}

// ── What a goal's sessions wait on you for ───────────────────────────────────
// A goal's `awaiting` (its approval requests and questions), answered through the routes the Telegram bot calls. The
// 200 sentences are the bot's (nous/telegram_bot.py describe_decision, describe_answer); a 409 shows the server's own
// fixed sentence (nous/owner_actions.py). Workflow approval steps are not here: they are on the Workflows screen.

/** The answer of POST /intentions/proposals/{id}/decide. The call's own output (`result`, `error`) is never shown. */
export interface DecisionAnswer { proposal_id: string; short_id: string; state: string; changed: boolean }

/** Approve or reject. An approve answers once the approved call has run (the server bounds it by the tool's time
 * limit), so it is not given up after `REQUEST_TIMEOUT_MS` as the goal changes are. */
export const decideProposal = (id: string, approve: boolean) =>
  apiRequest<DecisionAnswer>(`/intentions/proposals/${encodeURIComponent(id)}/decide`, 'POST', {
    decision: approve ? 'approve' : 'reject',
    actor: 'dashboard',
  });
export const answerQuestion = (id: string, text: string) =>
  timed((signal) =>
    apiRequest<unknown>(`/intentions/questions/${encodeURIComponent(id)}/answer`, 'POST', { text, actor: 'dashboard' }, signal),
  );

/** The longest answer the server takes (continuation.ANSWER_MAX_CHARS). */
export const ANSWER_MAX = 8000;
export const ASKS_TITLE = 'Waiting for you';
export const ASKS_READ_FAILED_TEXT = 'Could not read what is waiting for you.';
export const ASKS_GONE_TEXT = 'This is no longer waiting for you. It was answered somewhere else, expired, or its work ended.';
export const ASKS_LOST_GONE_TEXT = "No answer came back, and this is no longer waiting. Check the goal's journal for what happened.";
export const ANSWER_RECORDED_TEXT = 'Answer recorded.';
export const ANSWER_EMPTY_TEXT = 'Write an answer first.';
export const ANSWER_LIMIT_TEXT = 'At most 8,000 characters.';
/** A 400 for an answer the owner typed: the server could not take the text (a pasted NUL or broken character). */
export const ANSWER_UNREADABLE_TEXT = 'The answer could not be read as text. Remove any unusual characters and send it again.';
export const NOT_RUNNING_TEXT = 'Nous is not running its follow-up work.';
export const proposalLead = (tool: string) => `Nous asks to run ${tool}`;
export const proposalExpiryText = (at: string) => `Expires ${fmtUtc(at)}. If you do nothing, it is rejected.`;
export const questionExpiryText = (at: string) => `You can answer until ${fmtUtc(at)}.`;

/** The list's pill for a goal that waits on you. */
export const waitingPill = (n: number) => (n === 1 ? 'Waiting for your answer' : `${formatNumber(n)} waiting for your answer`);

/** What waits on you, in words: "1 approval request and 2 questions" ('' for nothing). */
export function waitingWords(proposals: number, questions: number): string {
  return [
    proposals ? `${formatNumber(proposals)} approval request${proposals === 1 ? '' : 's'}` : '',
    questions ? `${formatNumber(questions)} question${questions === 1 ? '' : 's'}` : '',
  ].filter(Boolean).join(' and ');
}

/** The same in one sentence: "Waiting for your answer: 1 approval request and 2 questions." ('' for nothing). */
export function waitingSentence(proposals: number, questions: number): string {
  const words = waitingWords(proposals, questions);
  return words ? `Waiting for your answer: ${words}.` : '';
}

/** What a press led to. `final`: the item can no longer be answered from here, so its buttons go; `lost`: no answer
 * came back, so the press may or may not have landed. */
export interface AskOutcome { text: string; ok: boolean; final: boolean; lost: boolean }

/** The sentence of a decision the server made, by the proposal's state after it (the bot's words). */
export function decisionText(state: unknown, shortId: string): string {
  switch (state) {
    case 'executed': return `Approved and executed (${shortId}).`;
    case 'failed': return `Approved, but the call failed (${shortId}). Nothing was retried.`;
    case 'executing': return `Already running (${shortId}).`;
    case 'approved': return `Approved (${shortId}).`;
    case 'rejected': return `Rejected (${shortId}).`;
    default: return `Done (${shortId}).`;
  }
}

/** A press of Approve, Reject or Send that did not get a 200, in words. A gateway's 502 or 504 is a lost answer too:
 * the routes never send one, and an approved call may have run behind it. */
export function askRefusal(kind: 'proposal' | 'question', err: unknown): AskOutcome {
  const status = (err as ApiAnswerError | null)?.status;
  const said = (text: string, final: boolean) => ({ text, ok: false, final, lost: false });
  if (status === undefined || status === 502 || status === 504) return { text: NO_ANSWER_TEXT, ok: false, final: false, lost: true };
  if (status === 404) return said(`That ${kind} is no longer available.`, true);
  if (status === 409) {
    const api = ((err as ApiAnswerError).body as { error?: unknown } | undefined)?.error;
    const fallback = kind === 'proposal' ? 'That proposal can no longer be decided.' : 'That question can no longer be answered.';
    return said(typeof api === 'string' && api.trim() ? refusalText(err) : fallback, true);
  }
  if (status === 503) return said(NOT_RUNNING_TEXT, false);
  if (status === 400 && kind === 'question') return said(ANSWER_UNREADABLE_TEXT, false);
  return said(refusalText(err), false);
}

// The page's sections fold. The viewer's choice is kept in this browser (one choice for every goal); the page works
// without the storage (a private window, blocked site data), with the defaults.
export const FOLD_KEY = 'nous.goals.folds';
export const FOLD_IDS = ['journal', 'plan', 'spend', 'sessions', 'charter', 'limits', 'tools'] as const;
export type FoldId = typeof FOLD_IDS[number];
/** Open at first: the journal, the plan and the spend; the rest closed. */
export const FOLD_DEFAULTS: Record<FoldId, boolean> = {
  journal: true, plan: true, spend: true, sessions: false, charter: false, limits: false, tools: false,
};

/** The sections the viewer chose to open or close: only known sections with a true or false; nothing on any problem. */
export function readFolds(): Partial<Record<FoldId, boolean>> {
  try {
    const raw = localStorage.getItem(FOLD_KEY);
    const stored: unknown = raw ? JSON.parse(raw) : null;
    if (!stored || typeof stored !== 'object' || Array.isArray(stored)) return {};
    const kept: Partial<Record<FoldId, boolean>> = {};
    for (const id of FOLD_IDS) {
      const value = (stored as Record<string, unknown>)[id];
      if (typeof value === 'boolean') kept[id] = value;
    }
    return kept;
  } catch {
    return {};
  }
}

/** Keep the viewer's choice for one section; when the storage refuses, nothing is kept. */
export function writeFold(id: FoldId, open: boolean): void {
  try {
    localStorage.setItem(FOLD_KEY, JSON.stringify({ ...readFolds(), [id]: open }));
  } catch {
    // not kept: the next page shows the defaults
  }
}

export const toolsSummary = (n: number) => (n === 0 ? 'None allowed' : `${formatNumber(n)} allowed`);
export const sessionsSummary = (n: number) => (n === 0 ? 'None yet' : `${formatNumber(n)} so far`);
