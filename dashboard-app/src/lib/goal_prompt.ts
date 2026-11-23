/**
 * The default goal prompt (GET, PUT and DELETE /goals/prompt): the text the first task of each goal session and every
 * follow-up turn read, which the owner may replace or reset from the Identity screen. The requests, the count the
 * server makes, and every sentence the card shows. No DOM.
 */
import { apiRequest, type ApiAnswerError } from './api';
import { timed } from './goals';
import { fmtClockUtc, fmtDayMonth } from './harness';

export const PROMPT_PATH = '/goals/prompt';

/** The read with goals on: the text in use (the owner's, or the built-in `default` when `is_default`). */
export interface PromptState {
  enabled: true;
  text: string;
  is_default: boolean;
  default: string;
  max_chars: number;
  updated_at: string | null;
  updated_by: string | null;
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

/** The draft as the server stores it: `\r\n` turned into `\n`, then trimmed. */
export function normalized(draft: string): string {
  return draft.replace(/\r\n/g, '\n').trim();
}

// ── Words ───────────────────────────────────────────────────────────────────────────────────────────────────────────

export const TITLE = 'Default goal prompt';
export const INTRO =
  'The first task of each goal session and every follow-up turn read this text, under the heading "How a goal\'s work ' +
  'runs". A change applies from the next session\'s first task and the next follow-up turn. Nothing needs a restart.';
export const SHOWING_DEFAULT = 'Showing the built-in default. Nothing of yours is saved.';
export const MAY_BE_SET_ASIDE = 'On a goal whose own text is very long, the built-in text or none may be used instead.';
export const LABEL = 'Prompt text';
export const UNSAVED = 'Unsaved changes.';
export const BLANK = 'The text cannot be empty. To go back to the default, use "Reset to the default".';
export const SAVE = 'Save';
export const RESET = 'Reset to the default';
export const CONFIRM_SAVE =
  'Replace the default goal prompt? The first task of each goal session and every follow-up turn will read the new text.';
export const CONFIRM_RESET = 'Go back to the built-in default? Your saved text will no longer be used.';
export const SAVING = 'Saving…';
export const SAVED = "Saved. It applies from the next session's first task and the next follow-up turn.";
export const RESET_DONE = 'Back to the built-in default.';

/** The line under the title when the owner's text is in use. */
export function savedLine(updatedAt: string | null): string {
  return `Your saved text is in use. Saved ${fmtDayMonth(updatedAt)}, ${fmtClockUtc(updatedAt)}. ${MAY_BE_SET_ASIDE}`;
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
