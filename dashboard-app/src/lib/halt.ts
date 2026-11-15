/**
 * The background halt (goals PR 0c): every word the Halt and Resume control says, and the state-to-view logic.
 * Pure: no DOM, no fetch (the store in stores/halt.ts calls the routes). Unit-tested in halt.test.ts.
 */
import { fmtUtc } from './harness';
import type { BackgroundHaltAnswer, BackgroundState } from './types/api';

export const WORDS = {
  running: 'Background work running',
  stopping: 'Stopping',
  halted: 'Background work halted',
  host: 'Halted by the host',
  halt: 'Halt background work',
  resume: 'Resume background work',
  caption: 'Resuming does not restart work that was cancelled. One-time schedules that came due run now.',
  notSaved: 'Halted, but the halt is not saved yet. Nous keeps trying to save it.',
  hostText: 'The server is set to hold background work (NOUS_BACKGROUND_HALT). Clear that setting on the host and restart to resume.',
  needsSignIn: 'Resume needs your sign-in. Open the dashboard through the login page.',
  notConfigured: 'Resume is not configured on this server.',
  raced: 'A halt arrived while resuming, so it stays halted.',
  resumeNotSaved: 'Resume could not be saved. Background work stays halted; try again.',
  unreachable: 'Cannot reach the server.',
} as const;

const MEANING = 'Nothing new starts, and every tool call from background work is refused.';

/** "Halted by you on Nov 15 09:31 UTC. Nothing new starts, ...". "By you" only for a halt that carried the owner's or the
 * proxy's secret (verified): the halt route takes no secret, so the surface a caller names proves nothing. */
export function haltedText(s: Pick<BackgroundState, 'since' | 'surface' | 'verified'>): string {
  const who = s.verified ? ' by you' : s.surface === 'agent' ? ' by Nous' : '';
  const when = s.since && !Number.isNaN(Date.parse(s.since)) ? ` on ${fmtUtc(s.since)}` : '';
  return `Halted${who}${when}. ${MEANING}`;
}

/** What the last Halt or Resume press left to say: shown while the state is the one it was said about. */
export interface HaltOutcome {
  message: string;
  /** Shown while halted (true) or while running (false). */
  whileHalted: boolean;
  /** A partial sweep: keep a Halt button beside Resume to press again. */
  retry: boolean;
}

export function haltAnswered(a: BackgroundHaltAnswer): HaltOutcome {
  const failed = a.report?.failed ?? [];
  if (failed.length > 0) {
    return {
      message: `Could not stop everything: ${failed.join(', ')}. Background tool calls are refused, so nothing can act. Press Halt again to retry.`,
      whileHalted: true,
      retry: true,
    };
  }
  return { message: a.text, whileHalted: true, retry: false };
}

/** A halt that got no answer (or not a usable one). */
export function haltFailed(): HaltOutcome {
  return { message: WORDS.unreachable, whileHalted: false, retry: false };
}

/** A refused Resume, from the error apiRequest threw: its status (none when no answer came) and its JSON body. */
export function resumeRefused(err: { status?: number; body?: unknown }): HaltOutcome {
  const body = (err.body ?? {}) as { reason?: unknown; host_hold?: unknown; raced?: unknown; error?: unknown };
  let message: string = WORDS.unreachable;
  if (err.status === 401 || err.status === 403) message = body.reason === 'not_configured' ? WORDS.notConfigured : WORDS.needsSignIn;
  else if (err.status === 409) message = body.host_hold ? WORDS.hostText : WORDS.raced;
  else if (err.status === 503 && body.error === 'not_saved') message = WORDS.resumeNotSaved;
  return { message, whileHalted: true, retry: false };
}

export interface HaltInput {
  /** The last state the poll read (null before the first answer). */
  state: BackgroundState | null;
  /** The last poll failed. */
  unreachable: boolean;
  /** A press in flight. */
  pending: 'halt' | 'resume' | null;
  outcome: HaltOutcome | null;
}

export interface HaltButton {
  kind: 'halt' | 'resume';
  label: string;
  disabled: boolean;
}

export interface HaltView {
  /** The state as a word in a status pill (null before the first answer: nothing is known yet). */
  pill: { text: string; tone: 'ok' | 'warn' } | null;
  /** The most urgent first: a Halt to press again, then Resume. */
  buttons: HaltButton[];
  /** The banner's sentences: what the halt means, then what a press or the poll left to say. */
  lines: string[];
  /** Under Resume: what resuming does not do. */
  caption: string | null;
  halted: boolean;
}

export function haltView({ state, unreachable, pending, outcome }: HaltInput): HaltView {
  const host = state?.source === 'host';
  const halted = !!state && (state.halted || host || state.source === 'unreadable');
  const said = outcome && !pending && (state === null || outcome.whileHalted === halted) ? outcome : null;

  const lines: string[] = [];
  if (state && host) lines.push(WORDS.hostText);
  else if (state && halted) {
    lines.push(haltedText(state));
    if (!state.persisted) lines.push(WORDS.notSaved);
  }
  if (said?.message) lines.push(said.message);
  if (unreachable) lines.push(WORDS.unreachable);

  const halt = (disabled: boolean): HaltButton => ({ kind: 'halt', label: WORDS.halt, disabled });
  let pill: HaltView['pill'] = null;
  let buttons: HaltButton[];
  if (pending === 'halt') {
    pill = { text: WORDS.stopping, tone: 'warn' };
    buttons = [halt(true)];
  } else if (!halted) {
    if (state) pill = { text: WORDS.running, tone: 'ok' };
    buttons = [halt(pending !== null)];
  } else {
    pill = { text: host ? WORDS.host : WORDS.halted, tone: 'warn' };
    buttons = [
      ...(said?.retry ? [halt(pending !== null)] : []),
      ...(host ? [] : [{ kind: 'resume' as const, label: WORDS.resume, disabled: pending !== null }]),
    ];
  }

  return { pill, buttons, lines: [...new Set(lines)], caption: halted && !host ? WORDS.caption : null, halted };
}
