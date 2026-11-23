// F118 PR 3: the card "A second opinion on decisions" on the Autonomy screen: its types, requests and words. The
// answers are those of nous/api/judge_second_routes.py; every route needs the owner's or the proxy's secret (the login
// proxy adds it, the dashboard sends none). The API key is sent in a request body only and is kept nowhere here: not
// in a store, browser storage, an error or rendered text. Every sentence the owner reads is here (pinned by
// outside_judge.test.ts), except the confirmation, which the server sends.

import { apiRequest, type ApiAnswerError } from './api';
import { timed } from './goals';
import { fmtDayMonth } from './harness';

// ── Answers ─────────────────────────────────────────────────────────────────

export type Source = 'stored' | 'server';
export type KeySource = 'stored' | 'server' | 'none';

export interface SecondState {
  enabled: true;
  locked: null | 'off';
  loaded: boolean;
  running: boolean;
  on: { value: boolean; source: Source };
  model: { value: string; source: Source };
  daily_calls: { value: number; most: number; source: Source | 'lowered' };
  key: { source: KeySource; set_at: string | null; problem: string | null; server_has_key: boolean };
  can_store_key: boolean;
  sends_to: string;
  confirm_text: string;
  updated_at: string | null;
  updated_by: string | null;
  changed?: boolean;
}
export type SecondAnswer = { enabled: false } | SecondState;
export type CheckResult = 'ok' | 'refused' | 'unreachable';
export interface CheckAnswer { result: CheckResult }
export interface SecondChange { on?: boolean; model?: string | null; daily_calls?: number | null }

// ── Requests ────────────────────────────────────────────────────────────────

const SECOND = '/dashboard/outside-judge/second';
const SECOND_KEY = '/dashboard/outside-judge/second/key';
const SECOND_CHECK = '/dashboard/outside-judge/second/check';

/** The owner's yes, sent back with the daily limit its sentence named (null: no yes). */
const yes = (confirmed: number | null) =>
  confirmed === null ? {} : { confirm_send: true as const, confirm_daily_calls: confirmed };

export const readSecond = (signal?: AbortSignal) =>
  timed((sig) => apiRequest<SecondAnswer>(SECOND, 'GET', undefined, sig), signal);
export const changeSecond = (change: SecondChange, confirmed: number | null) =>
  timed((signal) => apiRequest<SecondState>(SECOND, 'PATCH', { ...change, ...yes(confirmed) }, signal));
export const saveKey = (key: string, confirmed: number | null) =>
  timed((signal) => apiRequest<SecondState>(SECOND_KEY, 'PUT', { key, ...yes(confirmed) }, signal));
export const removeKey = (confirmed: number | null) =>
  timed((signal) => apiRequest<SecondState>(SECOND_KEY, 'DELETE', yes(confirmed), signal));
export const checkSecond = () => timed((signal) => apiRequest<CheckAnswer>(SECOND_CHECK, 'POST', {}, signal));

// ── Words ───────────────────────────────────────────────────────────────────

export const TITLE = 'A second opinion on decisions';
export const INTRO = "Jev, a model of the company TypeSafe, grades Nous's decisions once they have a grade, beside Nous's own judge. Its grades are kept for comparison and change nothing.";
export const HELP = 'A change applies at once. Decisions waiting in line for Jev when you save are dropped from the line.';
export const SWITCH_LABEL = 'Ask Jev as well';
export const MODEL_LABEL = 'Model';
export const MODEL_HINT = 'A version like jev-1.14.0';
export const LIMIT_LABEL = 'Most copies a day';
export const KEY_LABEL = 'API key';
export const SERVER_VALUE = "Use the server's value";
export const NO_KEY_FILE = 'This server has no key file, so a key cannot be saved here.';
export const SAVED = 'Saved.';
export const NOT_SAVED = 'The change was not saved.';
export const LOAD_FAILED = 'These settings could not be read. Reload the page to try again.';
export const YES_SEND = 'Yes, send them';
export const YES_REMOVE = 'Yes, remove the key';

export const mostText = (most: number) => `Most the server allows: ${most.toLocaleString('en-US')}`;
export const limitRule = (most: number) => `Enter a whole number from 0 to ${most.toLocaleString('en-US')}.`;

/** Where a value comes from, in words. */
export function sourceText(source: Source | 'lowered'): string {
  if (source === 'stored') return 'Set here';
  if (source === 'lowered') return "Set here, lowered to the server's most";
  return "The server's value";
}

/** The card's status, in words. */
export function statusText(s: SecondState): string {
  if (!s.loaded) return 'Waiting for the saved settings';
  if (s.locked === 'off') return 'Switched off by the server';
  if (!s.on.value) return 'Off';
  if (s.key.source === 'none' || s.key.problem) return 'On, but no key';
  if (s.daily_calls.value === 0) return 'On, but the daily limit is 0';
  if (!s.running) return 'On, but not running. Save again or restart Nous.';
  return 'On';
}

/** The status pill's tone. */
export function statusTone(s: SecondState): 'ok' | 'warn' | 'neutral' {
  const text = statusText(s);
  if (text === 'On') return 'ok';
  return text.startsWith('On, but') || text === 'Waiting for the saved settings' ? 'warn' : 'neutral';
}

/** A day as "3 May" (UTC, the screens' one day format); an empty string for a value that is not a date. */
export function dayText(iso: string | null): string {
  const day = fmtDayMonth(iso);
  return day === '—' ? '' : day;
}

/** What the card says of the key. Never the key, its length or any part of it: the server sends none. */
export function keyLine(s: SecondState): string {
  if (s.key.source === 'stored' && s.key.problem) {
    return 'The saved key cannot be read on this server. Enter it again or remove it.';
  }
  if (s.key.source === 'stored') {
    const day = dayText(s.key.set_at);
    return day ? `A key is saved (${day})` : 'A key is saved';
  }
  if (s.key.source === 'server') return "Using the server's key";
  return 'No key';
}

/** The question before the saved key is removed. */
export const removeQuestion = (s: SecondState) =>
  s.key.server_has_key
    ? "Remove the saved key? Jev will use the server's own key."
    : 'Remove the saved key? Jev stops until a key is set again.';

export const CHECK_WORDS: Record<CheckResult, string> = {
  ok: 'Jev answered',
  refused: 'Jev refused the key or the model',
  unreachable: 'Jev could not be reached',
};

/** When a refused change needs the owner's yes: the server's sentence and the daily limit it names (the number a
 * yes sends back), else null. */
export function confirmation(err: unknown): { text: string; calls: number } | null {
  const e = err as ApiAnswerError;
  const body = e?.body as { error?: unknown; needs_confirm?: unknown; confirm_daily_calls?: unknown } | undefined;
  const calls = body?.confirm_daily_calls;
  if (e?.status !== 409 || body?.needs_confirm !== true || typeof body.error !== 'string') return null;
  return Number.isInteger(calls) ? { text: body.error, calls: calls as number } : null;
}

/** A refused change in words: the server's own sentence (it never repeats a value sent), else a fixed one. */
export function refusalText(err: unknown): string {
  const body = (err as ApiAnswerError)?.body as { error?: unknown } | undefined;
  return typeof body?.error === 'string' && body.error ? body.error : NOT_SAVED;
}

/** A typed daily limit: a whole number from 0 to `most`, else null. */
export function parseLimit(text: string, most: number): number | null {
  const trimmed = text.trim();
  if (!/^\d{1,5}$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return value <= most ? value : null;
}
