// F115 autonomy settings: the Limits and tools screen's types, requests and words. The answers are those of
// nous/api/autonomy_routes.py; every route needs the owner's or the proxy's secret (the login proxy adds it, the
// dashboard sends none). Every sentence the owner reads is here (pinned by autonomy_settings.test.ts). A label,
// sentence or tool summary the server sends is shown as a text node only.

import { apiRequest, type ApiAnswerError } from './api';
import { fmtClockUtc, fmtUtc } from './harness';
import { timed } from './goals';

// ── Answers ─────────────────────────────────────────────────────────────────

/** Where a limit's value comes from: the owner, the host's environment, or the host's value lowered to its ceiling. */
export type Source = 'owner' | 'host' | 'clamped';

/** One change of a limit or of a tool's setting. `old` and `new` null: the host's value (a limit) or the default (a
 * tool). `tool` is set for a tool's setting, `label` for a limit's. `actor` is the proof the server accepted. */
export interface Change {
  key: string;
  label: string | null;
  tool: string | null;
  old: unknown;
  new: unknown;
  actor: string;
  at: string | null;
}

export interface LimitRow {
  key: string;
  label: string;
  unit: string;
  low: number;
  high: number;
  /** What the readers use now. */
  value: number;
  /** The value the host's environment set. */
  host_value: number;
  /** What "Use the server's value" restores: the host's value, lowered to its ceiling. */
  baseline: number;
  source: Source;
  ceiling: number | null;
  /** One plain sentence of what the limit limits. */
  description: string;
  /** When a change takes effect. */
  applies: string;
  last_change: Change | null;
}

/** `loaded` false: the saved settings could not be read; `message` says so and no row is sent. */
export interface LimitsAnswer {
  enabled: boolean;
  loaded?: boolean;
  message?: string;
  tool_guard?: boolean;
  ceilings_set?: boolean;
  limits?: LimitRow[];
}
export interface LogAnswer { enabled: boolean; changes?: Change[] }

export type ToolGroup = 'reading' | 'writing' | 'starting_work' | 'reaching_outside' | 'other_servers';
export type ToolSetting = 'allowed' | 'denied';
/** A kind of background work, in the server's words. */
export interface Place { key: string; words: string }

export interface ToolRow {
  name: string;
  summary: string;
  registered: boolean;
  group: ToolGroup;
  classes: string[];
  setting: ToolSetting;
  /** `default` (nobody set it), `owner`, or `server` for a tool the host locked. */
  source: 'default' | 'owner' | 'server';
  locked: boolean;
  /** Each kind of background work the code does not offer the tool to, and why. */
  not_offered: { place: string; why: string }[];
  /** Background calls in the last 30 days; null when the tool-call log is off. */
  calls_30d: number | null;
  last_call: string | null;
}

export interface ToolsAnswer {
  enabled: boolean;
  loaded?: boolean;
  message?: string;
  recorded?: boolean;
  /** Where Not allowed applies, in words, from the guard's own table. */
  held_in?: string[];
  locks_set?: boolean;
  places?: Place[];
  tools?: ToolRow[];
}

export interface LimitChanged { enabled: true; changed: boolean; limit: LimitRow }
export interface ToolChanged { enabled: true; changed: boolean; tool: ToolRow }

// ── Requests ────────────────────────────────────────────────────────────────

const BASE = '/autonomy';
/** A limit's route. A key is encoded as a path segment whatever it holds. */
export const limitPath = (key: string) => `${BASE}/limits/${encodeURIComponent(key)}`;
/** A tool's route. */
export const toolPath = (tool: string) => `${BASE}/tools/${encodeURIComponent(tool)}`;
/** Which changes a log read lists: the limits' or the tools'. */
export type LogKind = 'limits' | 'tools';
export const logPath = (kind: LogKind) => `${BASE}/log?kind=${kind}`;

export const readLimits = (signal?: AbortSignal) =>
  timed((sig) => apiRequest<LimitsAnswer>(`${BASE}/limits`, 'GET', undefined, sig), signal);
export const readLog = (kind: LogKind, signal?: AbortSignal) =>
  timed((sig) => apiRequest<LogAnswer>(logPath(kind), 'GET', undefined, sig), signal);
export const readTools = (signal?: AbortSignal) =>
  timed((sig) => apiRequest<ToolsAnswer>(`${BASE}/tools`, 'GET', undefined, sig), signal);
/** The owner's value of a limit. A value that is not a whole number is sent as typed: the server says why not. */
export const saveLimit = (key: string, value: number | string) =>
  timed((signal) => apiRequest<LimitChanged>(limitPath(key), 'PUT', { value }, signal));
/** Back to the server's value. */
export const resetLimit = (key: string) =>
  timed((signal) => apiRequest<LimitChanged>(limitPath(key), 'DELETE', undefined, signal));
/** Not allowed for background work. */
export const denyTool = (tool: string) =>
  timed((signal) => apiRequest<ToolChanged>(toolPath(tool), 'PUT', { setting: 'denied' }, signal));
/** Back to the default, which is Allowed. */
export const resetTool = (tool: string) =>
  timed((signal) => apiRequest<ToolChanged>(toolPath(tool), 'DELETE', undefined, signal));

/** What the input holds, as the request sends it: a whole number as a number, anything else as typed. */
export function typedValue(text: string): number | string {
  const t = text.trim();
  return /^\d{1,9}$/.test(t) ? Number(t) : t;
}

// ── Words ───────────────────────────────────────────────────────────────────

export const TITLE = 'Limits and tools';
export const PREFERENCES_TEXT =
  "You can set each limit within the range shown, and each change is recorded. Where the server sets a ceiling, the ceiling wins, and a tool the server locks stays off. Your provider's spend limit is the one that holds whatever is set here.";
export const LIMITS_SUBTITLE = 'How much background work may do, and for how long. A change applies without a restart.';
export const TOOLS_SUBTITLE =
  'Which tools background work may use. Not allowed takes a tool away from background work: it is not offered there and a call to it is refused.';
export const TOOLS_DEFAULT_TEXT = 'Allowed is the default. It does not add a tool where Nous does not offer it.';
export const NOT_LISTED_TEXT =
  "A tool of a server that is not connected now is not listed, so it cannot be switched off ahead of time, and a newly added server's tools start Allowed.";
export const NOT_HELD_TEXT = 'Chat itself, a tap on a companion card and an action you approved are not held.';
export const OFF_TITLE = 'Autonomy settings are off on this server.';
export const OFF_TEXT =
  "Set NOUS_AUTONOMY_SETTINGS_ENABLED=true on the server to change the limits of background work here. Until then every limit is the server's own value.";
export const GUARD_OFF_TITLE = 'The tool guard is off on this server.';
export const GUARD_OFF_TEXT =
  'Set NOUS_TOOL_GUARD_ENABLED=true, with NOUS_AUTONOMY_SETTINGS_ENABLED=true, on the server to choose the tools background work may use. Until then background work is offered the tools it was before.';
export const FORBIDDEN_TEXT = 'Sign in through the login page to see and change these settings.';
export const UNSUPPORTED_TEXT = 'Limits and tools are not available on this server.';
export const UNREACHABLE_TEXT = 'Could not reach the server.';
/** A change whose answer was lost: it may or may not have been made. */
export const NO_ANSWER_TEXT = 'No answer came back. Refresh the page before trying again.';
export const NOT_JSON_TEXT = 'The request was not sent as JSON, so it was refused.';
export const NO_CEILING_TEXT = 'The server sets no ceiling.';
export const SAME_TEXT = 'Saved. The value is the same as before.';
/** A save of the value already saved: the server wrote nothing. */
export const ALREADY_TEXT = 'That was already the saved value.';
export const ALREADY_SETTING_TEXT = 'That was already the saved setting.';
export const NO_CHANGES_TEXT = 'No change has been made yet.';
export const NO_TOOL_MATCH_TEXT = 'No tool matches.';
export const LOCKED_TEXT = 'Locked by the server';
export const NOT_REGISTERED_TEXT = 'Not registered now.';
export const NOT_RECORDED_TEXT = 'Background calls are not recorded on this server.';
export const LOG_TITLE = 'The last 20 changes';
export const READING_TEXT = 'Reading…';
export const RETRY_LABEL = 'Retry';
export const REFRESH_LABEL = 'Refresh';

/** Who made a change, in words: the proof the server accepted. */
export const ACTOR_WORDS: Record<string, string> = {
  proxy: 'from the dashboard',
  owner: "with the owner's secret",
};

export const SETTING_WORDS: Record<ToolSetting, string> = { allowed: 'Allowed', denied: 'Not allowed' };

/** The tool groups, in the order the screen shows them. */
export const GROUPS: { group: ToolGroup; words: string }[] = [
  { group: 'reading', words: 'Reading' },
  { group: 'writing', words: 'Writing' },
  { group: 'starting_work', words: 'Starting work' },
  { group: 'reaching_outside', words: 'Reaching outside' },
  { group: 'other_servers', words: 'Tools from other servers' },
];

export type ToolFilter = 'all' | 'changed' | 'denied';
export const TOOL_FILTERS: { value: ToolFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'changed', label: 'Changed' },
  { value: 'denied', label: 'Not allowed' },
];

/** A number as the screen writes it: "1,000". */
export const n = (value: number) => value.toLocaleString('en-US');

/** "a, b, and c": a list in plain English. */
export function listWords(items: string[]): string {
  if (items.length <= 1) return items[0] ?? '';
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return `${items.slice(0, -1).join(', ')}, and ${items[items.length - 1]}`;
}

/** Where a limit's value comes from, in one statement: the server's value, lowered to its ceiling when it was above
 * it, or the owner's value beside the server's, and a ceiling that lowered nothing. A host value equal to its ceiling
 * is not lowered. */
export function factsText(row: LimitRow): string {
  const lowered = row.ceiling !== null && row.host_value > row.ceiling;
  const ceiling = row.ceiling !== null && !lowered ? ` Ceiling: ${n(row.ceiling)}.` : '';
  const server = lowered
    ? `${n(row.host_value)}, lowered to its ceiling of ${n(row.ceiling as number)}`
    : n(row.host_value);
  if (row.source === 'owner') return `Your value. The server's value is ${server}.${ceiling}`;
  if (lowered) return `Server's value ${server}.`;
  return `Server's value: ${server}.${ceiling}`;
}
/** "From 1 to 200 tool calls". */
export const rangeText = (row: LimitRow) => `From ${n(row.low)} to ${n(row.high)} ${row.unit}`;

// What a change does, by limit: raising and lowering. A limit the screen does not know gets the plain sentence.
const RAISE: Record<string, (v: string) => string> = {
  max_turns: (v) => `This lets turns take more rounds of tool use: up to ${v} per turn, chat included.`,
  subtask_tool_call_limit: (v) => `This lets background tasks do more: up to ${v} tool calls per run.`,
  continuation_max_turns_per_root: (v) => `This lets a chain decide more often: up to ${v} decisions.`,
  continuation_max_spawns_per_root: (v) => `This lets a chain start more follow-up tasks: up to ${v}.`,
  continuation_max_depth: (v) => `This lets a chain go deeper: up to ${v} levels.`,
  continuation_max_tokens_per_root: (v) => `This lets a chain spend more: up to ${v} tokens.`,
  continuation_stall_limit: (v) => `This lets a chain go on longer without progress: up to ${v} decisions in a row.`,
  intention_root_ttl_hours: (v) => `Chains that start from now on may run longer: up to ${v} hours.`,
  intention_proposal_ttl_hours: (v) => `Questions and proposals wait longer: up to ${v} hours.`,
  subtask_default_timeout: (v) => `Tasks that name no time limit may run longer: up to ${v} seconds.`,
  subtask_max_timeout: (v) => `Tasks may ask for longer: up to ${v} seconds.`,
};
const CHAIN_LOWER = 'This stops chains sooner. A chain already above it stops at its next step.';
const LOWER: Record<string, string> = {
  max_turns: 'This makes turns answer sooner, chat included.',
  subtask_tool_call_limit: 'This stops background tasks sooner.',
  continuation_max_turns_per_root: CHAIN_LOWER,
  continuation_max_spawns_per_root: CHAIN_LOWER,
  continuation_max_depth: CHAIN_LOWER,
  continuation_max_tokens_per_root: CHAIN_LOWER,
  continuation_stall_limit: CHAIN_LOWER,
  intention_root_ttl_hours: 'Chains that start from now on end sooner.',
  intention_proposal_ttl_hours: 'Questions and proposals lapse sooner.',
  subtask_default_timeout: 'Tasks that name no time limit stop sooner.',
  subtask_max_timeout: 'Tasks that start from now on stop sooner.',
};

/** The one sentence after a saved change of `row` from `before` to its new value. */
export function changeText(row: LimitRow, before: number): string {
  if (row.value === before) return SAME_TEXT;
  if (row.value > before) return RAISE[row.key]?.(n(row.value)) ?? `Saved: ${n(row.value)} ${row.unit}.`;
  return LOWER[row.key] ?? `Saved: ${n(row.value)} ${row.unit}.`;
}

/** The sentence after "Use the server's value". */
export const resetText = (row: LimitRow, changed: boolean) =>
  changed ? `Back to the server's value: ${n(row.value)} ${row.unit}.` : `This already uses the server's value: ${n(row.value)} ${row.unit}.`;

/** A tool's sentence after its setting changed. */
export const toolChangeText = (tool: string, setting: ToolSetting) =>
  setting === 'denied' ? `${tool} is not allowed for background work now.` : `${tool} is allowed for background work again.`;

/** A tool's classes in words: "Reads only, and brings in outside text." */
export function classesText(classes: string[]): string {
  if (!classes.length) return '';
  const [first, ...rest] = classes;
  const lowered = rest.map((c) => c.charAt(0).toLowerCase() + c.slice(1));
  const all = [first, ...lowered];
  const text = all.length === 2 ? `${all[0]}, and ${all[1]}` : listWords(all);
  return `${text}.`;
}

/** Where Not allowed applies, from the server's own list. */
export const heldText = (heldIn: string[]) => (heldIn.length ? `Not allowed applies to ${listWords(heldIn)}.` : '');

/** A value of a change in words: null is the host's value (a limit) or the default (a tool). */
function valueWords(change: Change, value: unknown): string {
  if (value === null || value === undefined) return change.tool !== null ? 'default' : "the server's value";
  if (value === 'allowed' || value === 'denied') return SETTING_WORDS[value];
  return typeof value === 'number' ? n(value) : String(value);
}

/** A change as the list says it: "Tool calls per background task: the server's value to 80, from the dashboard, Nov 16 14:31
 * UTC". */
export function changeLine(change: Change): string {
  const what = change.label ?? change.tool ?? change.key;
  const who = ACTOR_WORDS[change.actor] ?? change.actor;
  return `${what}: ${valueWords(change, change.old)} to ${valueWords(change, change.new)}, ${who}, ${fmtUtc(change.at)}`;
}

/** The "not offered" lines of a tool: the places with one reason together, in the server's order of places. */
export function notOfferedLines(row: ToolRow, places: Place[]): string[] {
  const words = new Map(places.map((p) => [p.key, p.words]));
  const byWhy = new Map<string, string[]>();
  for (const item of row.not_offered) {
    const list = byWhy.get(item.why) ?? [];
    list.push(words.get(item.place) ?? item.place);
    byWhy.set(item.why, list);
  }
  return [...byWhy].map(([why, where]) => `Not offered here: ${where.join(', ')}: ${why}`);
}

/** A tool's calls in words. */
export function callsText(row: ToolRow, recorded: boolean): string {
  if (!recorded || row.calls_30d === null) return NOT_RECORDED_TEXT;
  if (row.calls_30d === 0) return 'No background calls in 30 days.';
  const calls = `${n(row.calls_30d)} background ${row.calls_30d === 1 ? 'call' : 'calls'} in 30 days`;
  return row.last_call ? `${calls}, the last ${fmtUtc(row.last_call)}.` : `${calls}.`;
}

/** Whether a tool is shown under a filter and a search (its name or summary, any case). */
export function toolShown(row: ToolRow, filter: ToolFilter, search: string): boolean {
  if (filter === 'changed' && row.source !== 'owner') return false;
  if (filter === 'denied' && row.setting !== 'denied') return false;
  const q = search.trim().toLowerCase();
  return !q || row.name.toLowerCase().includes(q) || row.summary.toLowerCase().includes(q);
}

/** Which request failed: a read, or a change. */
export type Doing = 'see' | 'change';

/** A refused or failed request in words: the server's own sentence as it sent it (a full stop added when it has none),
 * never an object or a stack. */
export function problemText(err: unknown, doing: Doing): string {
  const e = err as ApiAnswerError | null;
  const status = e?.status;
  if (status === undefined) return doing === 'see' ? UNREACHABLE_TEXT : NO_ANSWER_TEXT;
  if (status === 403) return FORBIDDEN_TEXT;
  if (status === 415) return NOT_JSON_TEXT;
  const api = (e?.body as { error?: unknown } | undefined)?.error;
  if (typeof api === 'string' && api.trim()) {
    const text = api.trim();
    return /[.!?]$/.test(text) ? text : `${text}.`;
  }
  if (status === 404) return UNSUPPORTED_TEXT;
  return `The request failed (HTTP ${status}).`;
}

/** What a failed read means for the whole tab: 403 and 404 replace it; anything else keeps the last answer. */
export function readGate(err: unknown): 'forbidden' | 'unsupported' | null {
  const status = (err as ApiAnswerError | null)?.status;
  if (status === 403) return 'forbidden';
  if (status === 404) return 'unsupported';
  return null;
}

/** "Read at 14:31 UTC.": the time of the last good read, in the one clock format of the screens. */
export const readAtText = (ms: number) => `Read at ${fmtClockUtc(new Date(ms).toISOString())}.`;

/** The header's line about the last read of a tab: reading, the failure in words, or when the last good read was. */
export function readStateText(
  state: { loading: boolean; error: unknown; lastUpdated: number | null; data: unknown },
  what: string,
): string {
  if (state.error) {
    const kept = state.data !== null && state.data !== undefined ? ' What is shown is from the last read.' : '';
    return `Could not read the ${what}: ${problemText(state.error, 'see')}${kept}`;
  }
  if (state.loading && (state.data === null || state.data === undefined)) return READING_TEXT;
  if (state.lastUpdated !== null) return readAtText(state.lastUpdated);
  return READING_TEXT;
}
