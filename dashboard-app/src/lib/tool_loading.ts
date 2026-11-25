// F120 tool loading: the Limits and tools screen's card "Tool loading". The answers are those of
// nous/api/tool_loading_routes.py; both routes need the owner's or the proxy's secret (the login proxy adds it, the
// dashboard sends none). Every sentence the owner reads is here (pinned by tool_loading.test.ts). A tool name, a kind's
// words or a reason the server sends is shown as a text node only. Watch only records what a call would have left out
// and changes nothing that is sent; On leaves the tools outside the core set out of each call, for the model to load.

import { apiRequest } from './api';
import { timed } from './goals';
import { ACTOR_WORDS, n } from './autonomy_settings';
import { fmtUtc } from './harness';

// ── Answers ─────────────────────────────────────────────────────────────────

export type Mode = 'off' | 'watch' | 'on';
/** `pinned` is always sent in full; `core` is in the core set; `left_out` would be left out. */
export type ToolState = 'pinned' | 'core' | 'left_out';

export interface KindSwitch { key: string; words: string; on: boolean }
export interface Pinned { name: string; why: string }

/** One kind of call over the window: the calls recorded in Watch only and what they would have saved. */
export interface KindNumbers extends KindSwitch {
  calls: number;
  read_calls: number;
  written_calls: number;
  deferred_avg: number;
  deferred_tokens_avg: number;
  index_tokens_avg: number;
  /** Per weighed call, in uncached input tokens' worth; null when no call reported its cache state. */
  saved_per_call: number | null;
  turns: number;
  /** Null while tool calls are not recorded. */
  turns_used_left_out: number | null;
  calls_used_left_out: number | null;
  /** The calls recorded in On; null when there was none in the window. */
  on_mode: OnNumbers | null;
}

/** Why calls of a kind in On were sent with every tool in full, and how many (nous/api/tool_loading.py why_not). */
export interface NotApplied { why: string; calls: number }

/** One kind of call's calls in On over the window. */
export interface OnNumbers {
  calls: number;
  /** The calls sent with tools left out. */
  applied: number;
  /** Per call sent with tools left out: the tools sent in full (the loader not counted) and left out. */
  full_avg: number;
  deferred_avg: number;
  read_calls: number;
  written_calls: number;
  /** Per weighed call, in uncached input tokens' worth; null when no call reported its cache state. */
  saved_per_call: number | null;
  /** The turns that left tools out. */
  turns: number;
  /** Per such turn; null while tool calls are not recorded. */
  loads_per_turn: number | null;
  refusals_per_turn: number | null;
  /** The calls sent with every tool in full after a fault. */
  fallbacks: number;
  not_applied: NotApplied[];
}

export interface ToolRow {
  name: string;
  server: string | null;
  registered: boolean;
  tokens_est: number | null;
  /** Calls and sessions in the last 30 days, every kind of call; null while tool calls are not recorded. */
  calls_30d: number | null;
  sessions_30d: number | null;
  state: ToolState;
  why: string | null;
}

export interface Totals {
  tools: number;
  kept: number;
  left_out: number;
  tokens_est: number;
  left_out_tokens_est: number;
  index_tokens_est: number;
}

export interface Setting {
  mode: Mode;
  kinds: KindSwitch[];
  core_default: boolean;
  /** The core set, without the pinned tools. */
  core: string[];
  pinned: Pinned[];
  updated_at: string | null;
  updated_by: string | null;
  /** Why the stored setting could not be read at start (it is Off then). */
  problem: string | null;
}

export interface LoadingAnswer extends Partial<Setting> {
  enabled: boolean;
  recorded?: { model_calls: boolean; tool_calls: boolean };
  /** Whether the model client in use can leave tools out (On does nothing without it). */
  deferring_client?: boolean;
  days?: number;
  /** Null while model calls are not recorded. */
  numbers?: KindNumbers[] | null;
  /** The numbers could not be read now (`numbers` is null); the setting can still be changed. */
  numbers_failed?: boolean;
  /** The tools' use could not be read now (their counts are null). */
  usage_failed?: boolean;
  totals?: Totals;
  tools?: ToolRow[];
}

export interface Changed extends Setting { enabled: true; changed: boolean }

/** A change: each field optional, merged into the stored setting. A null core is the default core set. */
export interface Change { mode?: Mode; kinds?: Record<string, boolean>; core?: string[] | null }

// ── Requests ────────────────────────────────────────────────────────────────

export const PATH = '/dashboard/tool-loading';
export const WINDOWS = [1, 7, 30] as const;
export type Window = (typeof WINDOWS)[number];
export const readPath = (days: Window) => `${PATH}?days=${days}`;

export const readLoading = (days: Window, signal?: AbortSignal) =>
  timed((sig) => apiRequest<LoadingAnswer>(readPath(days), 'GET', undefined, sig), signal);
export const changeLoading = (change: Change) =>
  timed((signal) => apiRequest<Changed>(PATH, 'PUT', change, signal));

// ── Words ───────────────────────────────────────────────────────────────────

export const SUBTITLE =
  'Which tools each model call sends in full, and what is saved by sending the rest as a list of names the model loads from. Watch only changes nothing that is sent: it records what each call would have left out. On leaves them out.';
export const OFF_TITLE = 'Tool loading is off on this server.';
export const OFF_TEXT =
  'Set NOUS_TOOL_LOADING_ENABLED=true on the server to watch what tool loading would save here. Until then nothing is recorded.';
export const MODE_TITLE = 'Mode';
export const MODE_WORDS: Record<Mode, string> = { off: 'Off', watch: 'Watch only', on: 'On' };
export const MODE_HELP: Record<Mode, string> = {
  off: 'Every tool is sent in full, as now, and nothing is recorded.',
  watch:
    'Every tool is still sent in full. Each model call of a kind switched on below records what it would have left out.',
  on:
    'In a kind switched on below, the tools outside the core set are left out of each model call and listed by name; the model loads the ones it needs, and each turn starts with the list again. What a turn may use is as before. Each call records what it sent.',
};
export const KINDS_TITLE = 'Kinds of call';
export const KINDS_HELP =
  'Chain decisions and every other turn that works for a chain are never watched, nor a turn with its own list of tools, and On never leaves their tools out.';
export const SWITCH_WORDS = { on: 'On', off: 'Off' } as const;
export const SAVES_AT_ONCE_TEXT =
  'The mode and each kind of call are saved when you choose them, and apply from the next model call (in On, from the next turn).';
export const NO_DEFERRING_TEXT =
  'On does nothing on this server: the model client in use cannot leave tools out (only the SDK client, NOUS_API_BACKEND=sdk, was tested), so every call is sent in full.';
export const NUMBERS_TITLE = 'What it saved, or would have';
/** Why calls in On were sent with every tool in full (the server's codes). */
export const WHY_WORDS: Record<string, string> = {
  client: 'the model client in use cannot leave tools out',
  model: 'their model is not one tool loading was tested on',
  nothing: 'every tool they could use is in the core set or pinned',
  turn: 'their turn began before On was chosen',
};
export const WINDOW_OPTIONS = WINDOWS.map((d) => ({ value: String(d), label: d === 1 ? 'Last day' : `Last ${d} days` }));
export const WEIGHED_TEXT =
  "Only successful calls are counted. Each call is weighed by its own cache state: a call that read from the cache read its tools too (they come first), and a token read counts as about a tenth of an uncached input token; a call that read nothing and wrote to the cache wrote them, at about 1.25. A call that did neither is not weighed. These are approximations, not prices.";
export const NUMBERS_FAILED_TEXT =
  'The numbers could not be read just now. The mode, the kinds of call and the core set can still be changed; Refresh reads them again.';
export const USAGE_FAILED_TEXT = "The tools' calls could not be read just now; Refresh reads them again.";
export const MODEL_CALLS_OFF_TEXT =
  'Model calls are not recorded on this server (NOUS_MODEL_CALL_LOG_ENABLED), so Watch only records nothing.';
export const TOOL_CALLS_OFF_TEXT =
  'Tool calls are not recorded on this server (NOUS_TOOL_CALL_LOG_ENABLED), so the card cannot say whether a tool that would have been left out was then used, and the tool list has no calls.';
export const CORE_TITLE = 'The core set';
export const CORE_HELP =
  'Tools in the core set would be sent in full; the rest would be listed by name. Pinned tools are always sent in full. Changes to the core set are saved together with Save, and apply from the next model call.';
export const HARNESS_TEXT = "The harness's own tools for a turn, such as a task's final report, are always sent in full.";
export const CORE_WORDS = { in: 'In full', out: 'Left out' } as const;
export const SAVE_LABEL = 'Save the core set';
export const DEFAULT_LABEL = 'Use the default core set';
export const DISCARD_LABEL = 'Discard changes';
export const UNSAVED_TEXT = 'The core set has changes that are not saved.';
export const SAVED_TEXT = 'Saved. It applies from the next model call.';
export const ALREADY_TEXT = 'That was already the saved setting.';
export const DEFAULT_TEXT = 'This is the default core set.';
export const NOT_REGISTERED_TEXT =
  'Not registered now. It stays in the core set and does nothing until a tool of that name is registered again.';
export const NO_TOOLS_TEXT = 'No tool is registered now.';

/** "Changed from the dashboard, Nov 24 10:00 UTC." or that nothing was changed yet. */
export function lastChangeText(setting: Pick<Setting, 'updated_at' | 'updated_by'>): string {
  if (!setting.updated_at) return 'Not changed yet.';
  const who = setting.updated_by ? ACTOR_WORDS[setting.updated_by] ?? setting.updated_by : '';
  return `Last changed${who ? ` ${who}` : ''}, ${fmtUtc(setting.updated_at)}.`;
}

/** The stored setting could not be read: it is off until it is saved again. */
export const problemLine = (problem: string) =>
  `The saved setting could not be read (${problem}), so tool loading is off. Saving any change replaces it.`;

/** What the core set means now, over every registered tool. */
export function totalsText(t: Totals): string {
  if (t.tools === 0) return NO_TOOLS_TEXT;
  const all = `Every registered tool together: ${n(t.tools)} tools, about ${n(t.tokens_est)} tokens.`;
  if (t.left_out === 0) return `${all} With this core set all of them are sent in full.`;
  return `${all} With this core set ${n(t.kept)} would be sent in full and ${n(t.left_out)} left out (about ${n(t.left_out_tokens_est)} tokens), listed by name in about ${n(t.index_tokens_est)} tokens.`;
}

/** A kind of call's numbers over the window, as lines: its calls in Watch only, then in On. */
export function kindLines(k: KindNumbers): string[] {
  if (k.calls === 0) {
    if (k.on_mode) return onLines(k.on_mode);
    return [k.on ? 'No successful call recorded in this window.' : 'Off, and no successful call recorded in this window.'];
  }
  return [...watchLines(k), ...(k.on_mode ? onLines(k.on_mode) : [])];
}

function watchLines(k: KindNumbers): string[] {
  const lines = [
    `${n(k.calls)} successful ${k.calls === 1 ? 'call' : 'calls'} recorded. Each would have left out ${n(k.deferred_avg)} tools on average, about ${n(k.deferred_tokens_avg)} tokens, listed by name in about ${n(k.index_tokens_avg)}.`,
  ];
  if (k.saved_per_call === null) {
    lines.push('No call reported its cache state, so the saving is not weighed.');
  } else {
    const cache = `${n(k.read_calls)} read the tools from the cache and ${n(k.written_calls)} wrote them`;
    lines.push(`Weighed, that is worth about ${n(k.saved_per_call)} uncached input tokens a call (${cache}).`);
  }
  if (k.turns_used_left_out !== null) {
    const turns = `${n(k.turns_used_left_out)} of ${n(k.turns)} ${k.turns === 1 ? 'turn' : 'turns'}`;
    lines.push(`${turns} then used a tool that would have been left out.`);
  }
  return lines;
}

const calls = (count: number) => `${n(count)} ${count === 1 ? 'call' : 'calls'}`;

/** A kind of call's calls in On, as lines. */
export function onLines(o: OnNumbers): string[] {
  const lines = [`On: ${calls(o.calls)} recorded, ${n(o.applied)} sent with tools left out.`];
  if (o.applied > 0) {
    lines.push(`Each of those sent ${n(o.full_avg)} tools in full and left out ${n(o.deferred_avg)} on average.`);
  }
  if (o.saved_per_call === null) {
    lines.push('No call reported its cache state, so the saving is not weighed.');
  } else {
    const cache = `${n(o.read_calls)} read the tools from the cache and ${n(o.written_calls)} wrote them`;
    lines.push(`Weighed, that saved about ${n(o.saved_per_call)} uncached input tokens a call (${cache}).`);
  }
  if (o.turns > 0 && o.loads_per_turn !== null && o.refusals_per_turn !== null) {
    lines.push(
      `In the ${n(o.turns)} ${o.turns === 1 ? 'turn' : 'turns'} that left tools out: ${n(o.loads_per_turn)} loads a turn, and ${n(o.refusals_per_turn)} calls a turn of a tool not loaded yet, which did not run.`,
    );
  }
  if (o.fallbacks > 0) lines.push(`${calls(o.fallbacks)} sent with every tool in full after a fault.`);
  for (const w of o.not_applied) {
    lines.push(`${calls(w.calls)} sent with every tool in full: ${WHY_WORDS[w.why] ?? 'for a reason this page does not know'}.`);
  }
  return lines;
}

/** A tool's numbers: its size and its use. */
export function toolNumbersText(row: ToolRow): string {
  const size = row.tokens_est === null ? '' : `About ${n(row.tokens_est)} tokens. `;
  if (row.calls_30d === null || row.sessions_30d === null) return size.trim();
  if (row.calls_30d === 0) return `${size}No calls in 30 days.`;
  const calls = `${n(row.calls_30d)} ${row.calls_30d === 1 ? 'call' : 'calls'}`;
  const sessions = `${n(row.sessions_30d)} ${row.sessions_30d === 1 ? 'session' : 'sessions'}`;
  return `${size}${calls} in ${sessions} in 30 days.`;
}

/** Where a tool comes from: Nous, or the server named in its name. */
export const sourceText = (row: ToolRow) => (row.server ? `From the server ${row.server}` : 'From Nous');

/** A pinned tool's words. */
export const pinnedText = (row: ToolRow) => `Always sent in full: ${row.why ?? 'pinned.'}`;

/** The core set the owner is editing, sorted, as the request sends it. */
export const draftList = (draft: Set<string>) => [...draft].sort();

/** Whether the draft differs from the saved core set. */
export function draftChanged(draft: Set<string>, saved: string[]): boolean {
  return draft.size !== saved.length || saved.some((name) => !draft.has(name));
}
