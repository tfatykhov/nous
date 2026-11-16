// The Devices section of the Integrations screen: the companion devices paired with this Nous, and the one pairing
// code the owner can open for a new one. The answers are those of the /a2ui/devices routes: every route needs the
// owner's or the proxy's secret (the login proxy adds it, the dashboard sends none). A device's name is shown as text
// only. A pairing code is never stored here: the view keeps it in its own state.

import { apiRequest, type ApiAnswerError } from './api';
import { MONTHS } from './harness';
import { timed } from './goals';

export interface Device {
  id: string;
  name: string;
  created_at: string | null;
  last_seen_at: string | null;
  revoked_at: string | null;
}

/** The pairing the server holds: none since it started (null), the open code, or how the last one ended. */
export type Pairing =
  | null
  | { state: 'open'; expires_in: number; tries_left: number }
  | { state: 'used' }
  | { state: 'closed_tries' }
  | { state: 'expired' };

export interface DeviceList {
  /** Live devices oldest first, then the most recently revoked. */
  devices: Device[];
  pairing: Pairing;
  /** Whether redeeming a code is held back; an answer without it is read as not held back. */
  redeem?: { limited: boolean };
}

/** A code just made: shown once, valid for `expires_in` seconds. */
export interface OpenedPairing { code: string; expires_in: number }

// ── Requests ────────────────────────────────────────────────────────────────

const BASE = '/a2ui/devices';
/** A device's route. An id is encoded as a path segment whatever it holds. */
export const devicePath = (id: string) => `${BASE}/${encodeURIComponent(id)}`;

/** The list and the pairing state, given up after the request timeout. */
export const listDevices = (signal?: AbortSignal) =>
  timed((sig) => apiRequest<DeviceList>(BASE, 'GET', undefined, sig), signal);
/** Open a pairing code. The name is sent only when one was typed; the body is JSON either way. */
export const openPairing = (name: string) =>
  timed((signal) => apiRequest<OpenedPairing>(`${BASE}/pairings`, 'POST', name ? { name } : {}, signal));
export const revokeDevice = (id: string) =>
  timed((signal) => apiRequest<{ revoked: boolean }>(devicePath(id), 'DELETE', undefined, signal));

/** A read of the list with what was true when it STARTED: how many codes had been made (a read that started before the
 * code on screen was made says nothing about that code, even when it answers after it) and the time it was asked (the
 * server's time left is as of then, at the latest, so a slow answer never makes a code look newer). */
export interface StampedList { list: DeviceList; made: number; askedAt: number }
export function readStamped(made: () => number, signal?: AbortSignal): Promise<StampedList> {
  const asked = made();
  const askedAt = Date.now();
  return listDevices(signal).then((list) => ({ list, made: asked, askedAt }));
}

// ── Words ───────────────────────────────────────────────────────────────────

export const INTRO_TEXT =
  'Phones paired with this Nous can approve, answer and read the companion on their own key. Pair a phone here, then type the code in the app.';
export const NAME_HINT = 'Leave it empty to use the name the phone reports.';
/** The longest name the server keeps. */
export const NAME_MAX = 60;
export const CODE_HELP = 'Type this code in the app under Settings, Pair this device. It works once.';
export const REPLACED_TEXT = 'The earlier code no longer works.';
export const FORBIDDEN_TEXT = 'Sign in through the login page to manage devices.';
export const UNSUPPORTED_TEXT = 'This server does not support device pairing yet.';
export const UNREACHABLE_TEXT = 'Could not reach the server.';
/** A change whose answer was lost: it may or may not have been made. */
export const NO_ANSWER_TEXT = 'No answer came back. Check the list before trying again.';
export const LIMITED_TEXT = 'Pairing is being held back for a minute because of too many tries.';
export const EMPTY_TEXT = 'No device is paired.';
export const REVOKE_TEXT = 'This device will stop working with Nous until it is paired again.';
/** How many wrong tries close a code. */
export const PAIRING_TRIES = 5;

export type Tone = 'ok' | 'warn' | 'neutral';
export type CodeEnd = 'used' | 'closed_tries' | 'expired' | 'gone' | 'replaced';

/** How a code that was on screen ended. The server's state names no code: "used" says a code was used, not which. */
export const END_TEXT: Record<CodeEnd, { text: string; tone: Tone }> = {
  used: { text: 'A code was used. The new device is in the list below.', tone: 'ok' },
  closed_tries: { text: 'This code was closed after too many wrong tries. Make a new one.', tone: 'warn' },
  expired: { text: 'This code has expired. Make a new one.', tone: 'neutral' },
  gone: { text: 'This code no longer works. Make a new one.', tone: 'warn' },
  replaced: { text: 'A newer code was made. This one no longer works.', tone: 'warn' },
};

/** How much longer than the code on screen the server's open code must have left before it is taken for a newer one
 * (made in another tab or by someone else): room for the server's rounding and the two clocks. */
export const REPLACED_MARGIN_MS = 10_000;

/** What a read of the list says about the code on screen: null while it is open, else how it ended. A server
 * restarted since the code was made holds no pairing at all. `leftMs` is the time the code on screen had left when the
 * read was asked: an open code with clearly more time left than that is a newer one, made elsewhere. */
export function codeEnd(pairing: Pairing, leftMs?: number): CodeEnd | null {
  if (pairing === null) return 'gone';
  if (pairing.state !== 'open') return pairing.state;
  return leftMs !== undefined && pairing.expires_in * 1000 > leftMs + REPLACED_MARGIN_MS ? 'replaced' : null;
}

/** "Valid for 5 minutes", counting down: whole minutes rounded up, and the last minute in words. '' once it is over. */
export function validForText(msLeft: number): string {
  if (msLeft <= 0) return '';
  if (msLeft <= 60_000) return 'Valid for less than a minute';
  const minutes = Math.ceil(msLeft / 60_000);
  return `Valid for ${minutes} minutes`;
}

/** The tries left on an open code, in words, once a wrong one was tried; '' before. */
export function triesText(pairing: Pairing): string {
  if (pairing?.state !== 'open' || pairing.tries_left >= PAIRING_TRIES) return '';
  const n = pairing.tries_left;
  return `${n} ${n === 1 ? 'try' : 'tries'} left before this code closes.`;
}

/** The pairing state worth a line when no code is on screen (after a reload, or a code whose answer was lost): an open
 * code, which a new one replaces, and a code closed after wrong tries. A used or expired code needs nothing: the list
 * shows the device, and a code that ended on screen was said there. Null for none. */
export function pairingLine(pairing: Pairing): { text: string; tone: Tone } | null {
  if (pairing === null) return null;
  switch (pairing.state) {
    case 'open': {
      const tries = triesText(pairing);
      return {
        text: `A code made earlier is still open. It is shown only once: make a new one to pair a phone.${tries ? ` ${tries}` : ''}`,
        tone: tries ? 'warn' : 'neutral',
      };
    }
    case 'closed_tries':
      return { text: 'The last code was closed after too many wrong tries. If this was not you, check who can reach this host.', tone: 'warn' };
    default: return null;
  }
}

// A device's days are the owner's: "today" and "yesterday" are the browser's local days, so a phone seen an hour ago
// never reads "yesterday" in the evening west of UTC. A moment is moved by the browser's offset at that moment, and
// then read with the UTC getters.
const DAY_MS = 86_400_000;
const local = (ms: number) => ms - new Date(ms).getTimezoneOffset() * 60_000;
const localDay = (ms: number) => Math.floor(local(ms) / DAY_MS);

/** A day in the browser's local time, in fmtDay's words: "Nov 12". */
export function localDayText(iso: string | null): string {
  if (!iso) return '—';
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return '—';
  const d = new Date(local(at));
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
}

/** "today", "yesterday", "3 days ago", by the browser's local day; a time ahead of the clock is today. */
export function lastSeenWords(iso: string | null, now: number = Date.now()): string {
  if (!iso) return '';
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return '';
  const days = Math.max(0, localDay(now) - localDay(at));
  if (days === 0) return 'today';
  if (days === 1) return 'yesterday';
  return `${days} days ago`;
}

/** A device's line: "Paired on Nov 12. Last seen today." or "Paired on Nov 12. Not seen yet." */
export function deviceLine(d: Device, now: number = Date.now()): string {
  const seen = lastSeenWords(d.last_seen_at, now);
  return `Paired on ${localDayText(d.created_at)}. ${seen ? `Last seen ${seen}.` : 'Not seen yet.'}`;
}

export const revokedLine = (d: Device) => `Revoked on ${localDayText(d.revoked_at)}`;

/** Which request failed: the list, a new code, or a revoke. */
export type Doing = 'see' | 'pair' | 'revoke';

/** A refused or failed request in words: the server's own sentence when it sent one, never an object or a stack. A
 * 404 of the list or of a new code means the routes are not there; a revoke's 404 is the server's "no such device". */
export function problemText(err: unknown, doing: Doing): string {
  const e = err as ApiAnswerError | null;
  const status = e?.status;
  if (status === undefined) return doing === 'see' ? UNREACHABLE_TEXT : NO_ANSWER_TEXT;
  if (status === 403) return FORBIDDEN_TEXT;
  if (status === 404 && doing !== 'revoke') return UNSUPPORTED_TEXT;
  if (status === 415) return 'The request was not sent as JSON, so it was refused.';
  const api = (e?.body as { error?: unknown } | undefined)?.error;
  if (typeof api === 'string' && api.trim()) {
    const text = api.trim();
    return `${text[0].toUpperCase()}${text.slice(1)}${/[.!?]$/.test(text) ? '' : '.'}`;
  }
  return `The request failed (HTTP ${status}).`;
}

/** What a failed read of the list means for the whole section: 403 and 404 replace it; anything else keeps the last
 * list. */
export function listGate(err: unknown): 'forbidden' | 'unsupported' | null {
  const status = (err as ApiAnswerError | null)?.status;
  if (status === 403) return 'forbidden';
  if (status === 404) return 'unsupported';
  return null;
}
