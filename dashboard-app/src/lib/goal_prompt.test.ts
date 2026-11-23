import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  BLANK, BUILT_IN_WHEN, CONFIRM_RESET, CONFIRM_SAVE, INTRO, LABEL, LOAD_FAILED, NONE_WHEN, RESET, RESET_DONE, SAVE,
  SAVED, SAVING, SHOWING_DEFAULT, TITLE, UNSAVED,
  codePoints, countText, hasText, noCard, normalized, readPrompt, refusalText, resetPrompt, savePrompt, savedLine,
} from './goal_prompt';

// The default goal prompt's requests, the rules a text is counted and checked by, and the words.

describe('the words', () => {
  it('say what the card shows, verbatim', () => {
    expect(TITLE).toBe('Default goal prompt');
    expect(INTRO).toBe('Goal sessions get this text under the heading "How a goal\'s work runs": in the first task of '
      + 'each session and in each follow-up turn, except as said below. A change applies from the next first task and '
      + 'the next follow-up turn. Nothing needs a restart.');
    expect(SHOWING_DEFAULT).toBe('Showing the built-in text. Nothing of yours is saved.');
    expect(NONE_WHEN).toBe('A goal gets no text when its own text, allowed tools, plan and journal leave too little '
      + 'room, or when a censor refuses the text.');
    expect(BUILT_IN_WHEN).toBe('A goal gets the built-in text instead, or no text, when its own text, allowed tools, '
      + 'plan and journal leave too little room, or when a censor refuses the text.');
    expect(LABEL).toBe('Prompt text');
    expect(UNSAVED).toBe('Unsaved changes.');
    expect(BLANK).toBe('The text is blank. To go back to the built-in text, use "Reset to the default".');
    expect([SAVE, RESET]).toEqual(['Save', 'Reset to the default']);
    expect(CONFIRM_SAVE).toBe('Save this text in place of the built-in text? It applies from the next first task and '
      + 'the next follow-up turn.');
    expect(CONFIRM_RESET).toBe('Go back to the built-in text? Your saved text will no longer be used.');
    expect(SAVING).toBe('Saving…');
    expect(SAVED).toBe('Saved. It applies from the next first task and the next follow-up turn.');
    expect(RESET_DONE).toBe('Back to the built-in text.');
    expect(LOAD_FAILED).toBe('The default goal prompt could not be loaded.');
  });

  it('the saved line names the day and the UTC time it was saved, when the answer says', () => {
    expect(savedLine('2026-11-20T09:05:00Z')).toBe('Your saved text replaces the built-in text. Saved 20 November, '
      + '09:05 UTC.');
    expect(savedLine(null)).toBe('Your saved text replaces the built-in text.');
  });

  it('the count says how many characters there are, and how many too many', () => {
    expect(countText(12, 1500)).toBe('12 of 1500 characters');
    expect(countText(1500, 1500)).toBe('1500 of 1500 characters');
    expect(countText(1503, 1500)).toBe('1503 of 1500 characters: 3 too many. Shorten it to save.');
  });
});

describe('the rules a text is counted and checked by (the server has the same)', () => {
  it('counts code points', () => {
    expect(codePoints('\u{1F600}')).toBe(1); // one emoji, two UTF-16 units
    expect(codePoints('e\u0301')).toBe(2); // a letter and its combining accent
    expect(codePoints('abc')).toBe(3);
  });

  it('turns \\r\\n and \\r into \\n and removes spaces, tabs and newlines from both ends only', () => {
    expect(normalized('  Work small.\n')).toBe('Work small.');
    expect(normalized('one\r\ntwo\rthree')).toBe('one\ntwo\nthree');
    expect(normalized(' \t\u00a0Work small.\ufeff\n')).toBe('\u00a0Work small.\ufeff');
    expect(codePoints(normalized('a'.repeat(1500) + '\n'))).toBe(1500); // not over the limit
    expect(codePoints(normalized('a'.repeat(700) + '\r\n' + 'b'.repeat(799)))).toBe(1500);
  });

  it('a text shows something only with a character that is not white space, a control or a format character', () => {
    for (const blank of ['', ' \n\t', '\u200b', '\u00ad\u200d\ufeff', '\u0001\u0002', '\u00a0\u3000']) {
      expect(hasText(blank), JSON.stringify(blank)).toBe(false);
    }
    for (const shown of ['a', '\u200ba', '\u00e9', '\u{1F600}']) expect(hasText(shown), shown).toBe(true);
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

  it('only a 404 means there is no card; other failures are a card that could not be loaded', async () => {
    reply = { status: 404, body: { error: 'no goal prompt' } };
    expect(noCard(await readPrompt().catch((e) => e))).toBe(true);
    reply = { status: 500, body: {} };
    expect(noCard(await readPrompt().catch((e) => e))).toBe(false);
    expect(noCard(new TypeError('network'))).toBe(false);
  });
});
