import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  CHECK_WORDS, HELP, INTRO, NOT_SAVED, changeSecond, checkSecond, confirmation, dayText, keyLine, limitRule, mostText, parseLimit,
  readSecond, refusalText, removeKey, removeQuestion, saveKey, sourceText, statusText, statusTone,
  type SecondState,
} from './outside_judge';

// F118 PR 3: the second judge's card: its words and its requests. Dates are fixed UTC strings, so no test depends on
// the clock or the time zone.

const STATE = (over: Partial<SecondState> = {}): SecondState => ({
  enabled: true,
  locked: null,
  loaded: true,
  running: true,
  on: { value: true, source: 'stored' },
  model: { value: 'jev-1.14.0', source: 'server' },
  daily_calls: { value: 100, most: 100, source: 'server' },
  key: { source: 'stored', set_at: '2026-05-03T12:00:00Z', problem: null, server_has_key: false },
  can_store_key: true,
  sends_to: 'api.typesafe.ai',
  confirm_text: 'Send a copy of each decision Nous resolves to TypeSafe (api.typesafe.ai), an outside company?',
  updated_at: null,
  updated_by: null,
  ...over,
});

describe('the status in words', () => {
  it.each([
    [{ loaded: false }, 'Waiting for the saved settings', 'warn'],
    [{ locked: 'off' as const }, 'Switched off by the server', 'neutral'],
    [{ on: { value: false, source: 'stored' as const }, running: false }, 'Off', 'neutral'],
    [{ key: { source: 'none' as const, set_at: null, problem: null, server_has_key: false }, running: false }, 'On, but no key', 'warn'],
    [{ key: { source: 'stored' as const, set_at: null, problem: 'stored with another key', server_has_key: true }, running: false },
      'On, but no key', 'warn'],
    [{ daily_calls: { value: 0, most: 100, source: 'stored' as const }, running: false }, 'On, but the daily limit is 0', 'warn'],
    [{ running: false }, 'On, but not running. Save again or restart Nous.', 'warn'],
    [{}, 'On', 'ok'],
  ])('%j reads "%s"', (over, text, tone) => {
    expect(statusText(STATE(over))).toBe(text);
    expect(statusTone(STATE(over))).toBe(tone);
  });
});

describe('the key in words', () => {
  it('says that a key is saved and on which day, never more', () => {
    expect(keyLine(STATE())).toBe('A key is saved (3 May)');
    expect(keyLine(STATE({ key: { source: 'stored', set_at: null, problem: null, server_has_key: false } }))).toBe('A key is saved');
  });
  it('says when the saved key cannot be read', () => {
    const s = STATE({ key: { source: 'stored', set_at: '2026-01-09T12:00:00Z', problem: 'no key file', server_has_key: true } });
    expect(keyLine(s)).toBe('The saved key cannot be read on this server. Enter it again or remove it.');
  });
  it("says when the server's key is used, and when there is none", () => {
    expect(keyLine(STATE({ key: { source: 'server', set_at: null, problem: null, server_has_key: true } }))).toBe("Using the server's key");
    expect(keyLine(STATE({ key: { source: 'none', set_at: null, problem: null, server_has_key: false } }))).toBe('No key');
  });
  it('asks the right question before the saved key is removed', () => {
    expect(removeQuestion(STATE())).toBe('Remove the saved key? Jev stops until a key is set again.');
    expect(removeQuestion(STATE({ key: { source: 'stored', set_at: null, problem: null, server_has_key: true } })))
      .toBe("Remove the saved key? Jev will use the server's own key.");
  });
  it('names a day in UTC, and nothing for a value that is not a date', () => {
    expect(dayText('2026-11-23T23:59:00Z')).toBe('23 November');
    expect(dayText('2026-01-01T00:01:00+00:00')).toBe('1 January');
    expect(dayText('not a date')).toBe('');
    expect(dayText(null)).toBe('');
  });
});

describe('the other words', () => {
  it('names where a value comes from', () => {
    expect(sourceText('stored')).toBe('Set here');
    expect(sourceText('server')).toBe("The server's value");
    expect(sourceText('lowered')).toBe("Set here, lowered to the server's most");
  });
  it('says the most the server allows and the rule of the limit', () => {
    expect(mostText(5000)).toBe('Most the server allows: 5,000');
    expect(limitRule(40)).toBe('Enter a whole number from 0 to 40.');
  });
  it('says what a check found', () => {
    expect(CHECK_WORDS).toEqual({
      ok: 'Jev answered', refused: 'Jev refused the key or the model', unreachable: 'Jev could not be reached',
    });
  });
  it('says that a change applies at once and what waits in line is dropped from it', () => {
    expect(HELP).toBe('A change applies at once. Decisions waiting in line for Jev when you save are dropped from the line.');
  });
  it('says what Jev is without naming which decisions are sent (the confirmation says that)', () => {
    expect(INTRO).toBe(
      "Jev, a model of the company TypeSafe, gives a second opinion on Nous's decisions, beside Nous's own judge. " +
        'Its grades are kept for comparison and change nothing.',
    );
  });
  it.each([
    ['40', 100, 40], ['0', 100, 0], [' 7 ', 100, 7], ['100', 100, 100], ['101', 100, null], ['-1', 100, null],
    ['4.5', 100, null], ['1e2', 100, null], ['', 100, null], ['abc', 100, null], ['123456', 5000, null],
  ])('reads the limit %j (most %d) as %j', (text, most, value) => {
    expect(parseLimit(text, most)).toBe(value);
  });
});

describe('a refused change', () => {
  const refused = (status: number, body: unknown) => Object.assign(new Error(`HTTP ${status}`), { status, body });
  it("asks for the owner's yes only for a 409 that says so, with the server's sentence and the number it names", () => {
    expect(confirmation(refused(409, { error: 'Send a copy?', needs_confirm: true, confirm_daily_calls: 40 })))
      .toEqual({ text: 'Send a copy?', calls: 40 });
    expect(confirmation(refused(409, { error: 'Send a copy?', needs_confirm: true }))).toBeNull();
    expect(confirmation(refused(409, { error: 'Send a copy?', needs_confirm: true, confirm_daily_calls: '40' }))).toBeNull();
    expect(confirmation(refused(409, { error: 'The server has the outside judge switched off.' }))).toBeNull();
    expect(confirmation(refused(400, { error: 'x', needs_confirm: true, confirm_daily_calls: 1 }))).toBeNull();
    expect(confirmation(new Error('network'))).toBeNull();
  });
  it("shows the server's sentence, else a fixed one", () => {
    expect(refusalText(refused(400, { error: 'Not accepted: key.' }))).toBe('Not accepted: key.');
    expect(refusalText(refused(500, undefined))).toBe(NOT_SAVED);
    expect(refusalText(new Error('HTTP 502 for /dashboard/outside-judge/second'))).toBe(NOT_SAVED);
  });
});

describe('the requests', () => {
  const sent: { url: string; method: string; body: unknown; headers: Record<string, string> }[] = [];
  const install = () => vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    sent.push({
      url, method: init.method ?? 'GET', body: init.body === undefined ? undefined : JSON.parse(String(init.body)),
      headers: (init.headers ?? {}) as Record<string, string>,
    });
    return { ok: true, status: 200, text: async () => JSON.stringify({ result: 'ok' }) } as Response;
  }));
  afterEach(() => { sent.length = 0; vi.unstubAllGlobals(); });

  it('sends each change to its route, with the yes and the number it was given for only when there is one', async () => {
    install();
    const KEY = 'ts-request-' + 'Q'.repeat(20);
    await readSecond();
    await changeSecond({ on: true }, null);
    await changeSecond({ daily_calls: null }, 100);
    await saveKey(KEY, null);
    await saveKey(KEY, 7);
    await removeKey(null);
    await removeKey(0);
    await checkSecond();
    expect(sent.map(({ url, method, body }) => [method, url, body])).toEqual([
      ['GET', '/dashboard/outside-judge/second', undefined],
      ['PATCH', '/dashboard/outside-judge/second', { on: true }],
      ['PATCH', '/dashboard/outside-judge/second', { daily_calls: null, confirm_send: true, confirm_daily_calls: 100 }],
      ['PUT', '/dashboard/outside-judge/second/key', { key: KEY }],
      ['PUT', '/dashboard/outside-judge/second/key', { key: KEY, confirm_send: true, confirm_daily_calls: 7 }],
      ['DELETE', '/dashboard/outside-judge/second/key', {}],
      ['DELETE', '/dashboard/outside-judge/second/key', { confirm_send: true, confirm_daily_calls: 0 }],
      ['POST', '/dashboard/outside-judge/second/check', {}],
    ]);
    // The key is in a body only, never in an address; every change is sent as JSON.
    expect(sent.every(({ url }) => !url.includes(KEY))).toBe(true);
    expect(sent.filter(({ method }) => method !== 'GET').every(({ headers }) => headers['Content-Type'] === 'application/json')).toBe(true);
  });
});
