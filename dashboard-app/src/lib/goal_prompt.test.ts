import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  BLANK, CONFIRM_RESET, CONFIRM_SAVE, INTRO, LABEL, MAY_BE_SET_ASIDE, RESET, RESET_DONE, SAVE, SAVED, SAVING,
  SHOWING_DEFAULT, TITLE, UNSAVED,
  codePoints, countText, normalized, readPrompt, refusalText, resetPrompt, savePrompt, savedLine,
} from './goal_prompt';

// The default goal prompt's requests, count and words.

describe('the words', () => {
  it('say what the card shows, verbatim', () => {
    expect(TITLE).toBe('Default goal prompt');
    expect(INTRO).toBe('The first task of each goal session and every follow-up turn read this text, under the heading '
      + '"How a goal\'s work runs". A change applies from the next session\'s first task and the next follow-up turn. '
      + 'Nothing needs a restart.');
    expect(SHOWING_DEFAULT).toBe('Showing the built-in default. Nothing of yours is saved.');
    expect(MAY_BE_SET_ASIDE).toBe('On a goal whose own text is very long, the built-in text or none may be used instead.');
    expect(LABEL).toBe('Prompt text');
    expect(UNSAVED).toBe('Unsaved changes.');
    expect(BLANK).toBe('The text cannot be empty. To go back to the default, use "Reset to the default".');
    expect([SAVE, RESET]).toEqual(['Save', 'Reset to the default']);
    expect(CONFIRM_SAVE).toBe('Replace the default goal prompt? The first task of each goal session and every '
      + 'follow-up turn will read the new text.');
    expect(CONFIRM_RESET).toBe('Go back to the built-in default? Your saved text will no longer be used.');
    expect(SAVING).toBe('Saving…');
    expect(SAVED).toBe("Saved. It applies from the next session's first task and the next follow-up turn.");
    expect(RESET_DONE).toBe('Back to the built-in default.');
  });

  it('the saved line names the day and the UTC time it was saved', () => {
    expect(savedLine('2026-11-20T09:05:00Z')).toBe('Your saved text is in use. Saved 20 November, 09:05 UTC. '
      + 'On a goal whose own text is very long, the built-in text or none may be used instead.');
  });

  it('the count says how many characters there are, and how many too many', () => {
    expect(countText(12, 1500)).toBe('12 of 1500 characters');
    expect(countText(1500, 1500)).toBe('1500 of 1500 characters');
    expect(countText(1503, 1500)).toBe('1503 of 1500 characters: 3 too many. Shorten it to save.');
  });
});

describe('the count', () => {
  it('counts code points, as the server does', () => {
    expect(codePoints('\u{1F600}')).toBe(1); // one emoji, two UTF-16 units
    expect(codePoints('é')).toBe(2); // a letter and its combining accent
    expect(codePoints('abc')).toBe(3);
  });

  it('counts the draft trimmed, with \\r\\n as one character', () => {
    expect(normalized('  Work small.\n')).toBe('Work small.');
    expect(codePoints(normalized('a'.repeat(1500) + '\n'))).toBe(1500); // not over the limit
    expect(codePoints(normalized('a'.repeat(700) + '\r\n' + 'b'.repeat(799)))).toBe(1500);
  });
});

describe('the requests', () => {
  let sent: { url: string; method: string; headers: unknown; body: unknown }[];
  let reply: { status: number; body: unknown };

  beforeEach(() => {
    sent = [];
    reply = { status: 200, body: { enabled: false } };
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
      sent.push({ url, method: init.method ?? 'GET', headers: init.headers,
        body: init.body === undefined ? undefined : JSON.parse(String(init.body)) });
      return { ok: reply.status < 400, status: reply.status, text: async () => JSON.stringify(reply.body) } as Response;
    }));
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it('read is a GET with no body; save sends the text alone; reset sends an empty object', async () => {
    await readPrompt();
    await savePrompt('Work small.');
    await resetPrompt();
    expect(sent.map(({ url, method, body }) => ({ url, method, body }))).toEqual([
      { url: '/goals/prompt', method: 'GET', body: undefined },
      { url: '/goals/prompt', method: 'PUT', body: { text: 'Work small.' } },
      { url: '/goals/prompt', method: 'DELETE', body: {} },
    ]);
    expect(sent[1].headers).toEqual({ 'Content-Type': 'application/json' });
    expect(sent[2].headers).toEqual({ 'Content-Type': 'application/json' });
    expect(Object.keys(sent[1].body as object)).toEqual(['text']); // never who saved it
  });

  it('a refusal shows the server\'s sentence; any other failure says what failed', async () => {
    reply = { status: 400, body: { error: 'The text is 1501 characters; at most 1500 are allowed.' } };
    const refused = await savePrompt('x').catch((e) => e);
    expect(refusalText(refused, 'save')).toBe('The text is 1501 characters; at most 1500 are allowed.');
    reply = { status: 500, body: {} };
    const failed = await resetPrompt().catch((e) => e);
    expect(refusalText(failed, 'reset')).toBe('Could not reset: HTTP 500 for /goals/prompt');
    expect(refusalText(new TypeError('network'), 'save')).toBe('Could not save: network');
  });
});
