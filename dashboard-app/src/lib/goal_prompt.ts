/**
 * The default goal prompt (GET, PUT and DELETE /goals/prompt): the text goal sessions read in their first task and
 * their follow-up turns, which the owner may replace or reset from the Identity screen. The requests, the rules the
 * server counts and checks a text by, and every sentence the card shows. No DOM.
 */
import { apiRequest, type ApiAnswerError } from './api';
import { timed } from './goals';
import { fmtClockUtc, fmtDayMonth } from './harness';

export const PROMPT_PATH = '/goals/prompt';

/** The read with goals on: the text the builders start from (the owner's, or the built-in `default`). `reload` is
 * set when a change was made and reading it back failed; the answer is then what was written. */
export interface PromptState {
  enabled: true;
  text: string;
  is_default: boolean;
  default: string;
  max_chars: number;
  updated_at: string | null;
  updated_by: string | null;
  reload?: boolean;
}
export type PromptAnswer = PromptState | { enabled: false };

export const readPrompt = (signal?: AbortSignal) =>
  timed((sig) => apiRequest<PromptAnswer>(PROMPT_PATH, 'GET', undefined, sig), signal);
/** The body is the text alone: the server keeps the proof it accepted as who saved it. */
export const savePrompt = (text: string) =>
  timed((signal) => apiRequest<PromptState>(PROMPT_PATH, 'PUT', { text }, signal));
export const resetPrompt = () => timed((signal) => apiRequest<PromptState>(PROMPT_PATH, 'DELETE', {}, signal));

/** The characters of a text as the server counts them: code points, so an emoji is one and an accent mark one more. */
export function codePoints(text: string): number {
  return [...text].length;
}

/** The draft as the server stores it (`normalize_goal_prompt`): `\r\n` and `\r` turned into `\n`, then spaces,
 * tabs and newlines removed from both ends, and nothing else. */
export function normalized(draft: string): string {
  return draft.replace(/\r\n?/g, '\n').replace(/^[ \t\n]+|[ \t\n]+$/g, '');
}

/** Whether a text shows anything once rendered (`goal_prompt_has_text`): a character that is not white space, a
 * control character or an invisible format character. */
export function hasText(text: string): boolean {
  return /[^\s\p{Cc}\p{Cf}]/u.test(text);
}

// ── Words ───────────────────────────────────────────────────────────────────────────────────────────────────────────

export const TITLE = 'Default goal prompt';
export const INTRO =
  'Goal sessions get this text under the heading "How a goal\'s work runs": in the first task of each session and ' +
  'in each follow-up turn, except as said below. A change applies from the next first task and the next follow-up ' +
  'turn. Nothing needs a restart.';
export const SHOWING_DEFAULT = 'Showing the built-in text. Nothing of yours is saved.';
export const NONE_WHEN =
  'A goal gets no text when its own text, allowed tools, plan and journal leave too little room, or when a censor ' +
  'refuses the text.';
export const BUILT_IN_WHEN =
  'A goal gets the built-in text instead, or no text, when its own text, allowed tools, plan and journal leave too ' +
  'little room, or when a censor refuses the text.';
export const LABEL = 'Prompt text';
export const UNSAVED = 'Unsaved changes.';
export const BLANK = 'The text is blank. To go back to the built-in text, use "Reset to the default".';
export const SAVE = 'Save';
export const RESET = 'Reset to the default';
export const CONFIRM_SAVE =
  'Save this text in place of the built-in text? It applies from the next first task and the next follow-up turn.';
export const CONFIRM_RESET = 'Go back to the built-in text? Your saved text will no longer be used.';
export const SAVING = 'Saving…';
export const SAVED = 'Saved. It applies from the next first task and the next follow-up turn.';
export const RESET_DONE = 'Back to the built-in text.';
export const LOAD_FAILED = 'The default goal prompt could not be loaded.';

/** The line under the title when the owner's text is saved, with when it was (when the answer says). */
export function savedLine(updatedAt: string | null): string {
  const said = 'Your saved text replaces the built-in text.';
  return updatedAt ? `${said} Saved ${fmtDayMonth(updatedAt)}, ${fmtClockUtc(updatedAt)}.` : said;
}

/** The count under the field: of the draft as the server counts it, and by how much it is over the limit. */
export function countText(n: number, max: number): string {
  return n > max ? `${n} of ${max} characters: ${n - max} too many. Shorten it to save.` : `${n} of ${max} characters`;
}

/** A refused save or reset in words: the server's own sentence, else what failed. */
export function refusalText(err: unknown, verb: 'save' | 'reset'): string {
  const body = (err as ApiAnswerError)?.body as { error?: unknown } | undefined;
  if (typeof body?.error === 'string' && body.error) return body.error;
  return `Could not ${verb}: ${err instanceof Error ? err.message : String(err)}`;
}

/** Whether a failed read means there is no card to show (goals off, or a server without the route) rather than a
 * card that could not be loaded. */
export function noCard(err: unknown): boolean {
  return (err as ApiAnswerError)?.status === 404;
}
