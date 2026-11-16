import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  END_TEXT, FORBIDDEN_TEXT, NO_ANSWER_TEXT, UNREACHABLE_TEXT, UNSUPPORTED_TEXT, codeEnd, deviceLine, lastSeenWords, listDevices,
  listGate, openPairing, pairingLine, problemText, revokeDevice, revokedLine, triesText, validForText, type CodeEnd, type Pairing,
} from './devices';
import * as devices from './devices';
import { OPEN, deviceRow } from './testing/deviceFixtures';

// The Devices section's words, rules and requests (no DOM). The answers are those of the /a2ui/devices routes.

const GLYPH = /[\u2190-\u21FF\u25A0-\u25FF\u2600-\u27BF]|[\u{1F300}-\u{1FAFF}]/u;
type Sent = { url: string; method: string; headers: Record<string, string> | undefined; body: string | undefined };

/** fetch answering `status` and `payload`, recording what was sent: the client's own headers, not a stand-in's. */
function stub(status = 200, payload: unknown = {}): Sent[] {
  const sent: Sent[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    sent.push({ url, method: init.method ?? 'GET', headers: init.headers as Record<string, string> | undefined,
      body: typeof init.body === 'string' ? init.body : undefined });
    return new Response(JSON.stringify(payload), { status });
  }));
  return sent;
}

const err = (status?: number, body?: unknown) => Object.assign(new Error('x'), { status, body });

afterEach(() => vi.unstubAllGlobals());

describe('the requests', () => {
  it('the list is a GET with no header and no body', async () => {
    const sent = stub(200, { devices: [], pairing: null, redeem: { limited: false } });
    await listDevices();
    expect(sent).toEqual([{ url: '/a2ui/devices', method: 'GET', headers: undefined, body: undefined }]);
  });

  it('a new code is a POST of JSON, with the name only when one was typed', async () => {
    const sent = stub(200, { code: 'K7QM-2XRD', expires_in: 300 });
    expect(await openPairing('')).toEqual({ code: 'K7QM-2XRD', expires_in: 300 });
    await openPairing('Pixel 7');
    expect(sent).toEqual([
      { url: '/a2ui/devices/pairings', method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
      { url: '/a2ui/devices/pairings', method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"name":"Pixel 7"}' },
    ]);
  });

  it('a revoke is a DELETE that says it is JSON with no body, and its id is one path segment', async () => {
    const sent = stub(200, { revoked: true });
    expect(await revokeDevice('00000000-0000-4000-8000-000000000001')).toEqual({ revoked: true });
    await revokeDevice('a/b?c');
    expect(sent).toEqual([
      { url: '/a2ui/devices/00000000-0000-4000-8000-000000000001', method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: undefined },
      { url: '/a2ui/devices/a%2Fb%3Fc', method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: undefined },
    ]);
  });

  it('a refused change throws its status and the server\'s body', async () => {
    stub(409, { error: 'revoke a device first' });
    await expect(openPairing('')).rejects.toMatchObject({ status: 409, body: { error: 'revoke a device first' } });
  });

  it('a change with no answer is given up after 15 s, and says the answer was lost', async () => {
    vi.useFakeTimers();
    try {
      vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit = {}) => new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('The operation was aborted.', 'AbortError')));
      })));
      const pending = openPairing('').catch((e: unknown) => e);
      await vi.advanceTimersByTimeAsync(14_999);
      let settled = false;
      void pending.then(() => (settled = true));
      await Promise.resolve();
      expect(settled).toBe(false);
      await vi.advanceTimersByTimeAsync(2);
      expect(problemText(await pending, 'pair')).toBe(NO_ANSWER_TEXT);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('the code and its countdown', () => {
  it('counts down in whole minutes, rounded up, then the last minute in words, then nothing', () => {
    expect(validForText(300_000)).toBe('Valid for 5 minutes');
    expect(validForText(240_001)).toBe('Valid for 5 minutes');
    expect(validForText(240_000)).toBe('Valid for 4 minutes');
    expect(validForText(60_001)).toBe('Valid for 2 minutes');
    expect(validForText(60_000)).toBe('Valid for less than a minute');
    expect(validForText(1)).toBe('Valid for less than a minute');
    expect(validForText(0)).toBe('');
    expect(validForText(-5)).toBe('');
  });

  it('says the tries left only once a wrong code was tried', () => {
    expect(triesText(OPEN(5))).toBe('');
    expect(triesText(OPEN(4))).toBe('4 tries left before this code closes.');
    expect(triesText(OPEN(1))).toBe('1 try left before this code closes.');
    for (const p of [null, { state: 'used' }, { state: 'closed_tries' }, { state: 'expired' }] as Pairing[]) expect(triesText(p)).toBe('');
  });

  it('a read ends the code on screen unless it says the code is open; no pairing at all means the server restarted', () => {
    const ends: [Pairing, CodeEnd | null][] = [
      [OPEN(), null], [OPEN(2), null], [{ state: 'used' }, 'used'], [{ state: 'closed_tries' }, 'closed_tries'],
      [{ state: 'expired' }, 'expired'], [null, 'gone'],
    ];
    for (const [pairing, end] of ends) expect(codeEnd(pairing), JSON.stringify(pairing)).toBe(end);
  });

  it('pins the words for a code that ended', () => {
    expect(END_TEXT).toEqual({
      used: { text: 'This code was used. The device is paired.', tone: 'ok' },
      closed_tries: { text: 'This code was closed after too many wrong tries. Make a new one.', tone: 'warn' },
      expired: { text: 'This code has expired. Make a new one.', tone: 'neutral' },
      gone: { text: 'This code no longer works. Make a new one.', tone: 'warn' },
    });
  });

  it('with no code on screen, says what the server holds', () => {
    expect(pairingLine(null)).toBeNull();
    expect(pairingLine(OPEN())).toEqual({ text: 'A code made earlier is still open. It is shown only once: make a new one to pair a phone.', tone: 'neutral' });
    expect(pairingLine(OPEN(3))?.text).toBe(
      'A code made earlier is still open. It is shown only once: make a new one to pair a phone. 3 tries left before this code closes.');
    expect(pairingLine(OPEN(3))?.tone).toBe('warn');
    expect(pairingLine({ state: 'used' })).toEqual({ text: 'The last code was used.', tone: 'neutral' });
    expect(pairingLine({ state: 'closed_tries' })?.text).toBe(
      'The last code was closed after too many wrong tries. If this was not you, check who can reach this host.');
    expect(pairingLine({ state: 'expired' })).toEqual({ text: 'The last code expired.', tone: 'neutral' });
  });
});

describe('a device in words, by the browser\'s local day', () => {
  /** The browser's offset from UTC in minutes, as getTimezoneOffset gives it (300: UTC-5, -540: UTC+9). */
  const offset = (minutes: number) => vi.spyOn(Date.prototype, 'getTimezoneOffset').mockReturnValue(minutes);
  afterEach(() => vi.restoreAllMocks());

  it('in the evening west of UTC, a phone seen an hour ago was seen today, one seen 26 hours ago yesterday', () => {
    offset(300);
    const now = Date.parse('2026-11-16T00:30:00Z'); // 19:30 on Nov 15 in UTC-5
    expect(lastSeenWords('2026-11-15T23:30:00Z', now)).toBe('today'); // an hour ago: the UTC day before, the same local day
    expect(lastSeenWords('2026-11-14T22:30:00Z', now)).toBe('yesterday'); // 26 hours ago
    expect(lastSeenWords('2026-11-15T05:00:00Z', now)).toBe('today'); // local midnight
    expect(lastSeenWords('2026-11-15T04:59:59Z', now)).toBe('yesterday'); // a second before it
    expect(deviceLine(deviceRow({ created_at: '2026-11-13T02:00:00Z', last_seen_at: '2026-11-15T23:30:00Z' }), now))
      .toBe('Paired on Nov 12. Last seen today.'); // 21:00 on Nov 12 local
    expect(revokedLine(deviceRow({ revoked_at: '2026-11-16T00:30:00Z' }))).toBe('Revoked on Nov 15');
  });

  it('in the morning east of UTC, the local day starts before the UTC one', () => {
    offset(-540);
    const now = Date.parse('2026-11-15T01:00:00Z'); // 10:00 on Nov 15 in UTC+9
    expect(lastSeenWords('2026-11-14T15:00:00Z', now)).toBe('today'); // local midnight, the UTC day before
    expect(lastSeenWords('2026-11-14T14:59:59Z', now)).toBe('yesterday');
    expect(deviceLine(deviceRow({ created_at: '2026-11-11T20:00:00Z', last_seen_at: null }), now)).toBe('Paired on Nov 12. Not seen yet.');
  });

  it('last seen in words: today, yesterday, N days ago; a time ahead of the clock is today; nothing for no time', () => {
    offset(0);
    const NOW = Date.parse('2026-11-15T12:00:00Z');
    expect(lastSeenWords('2026-11-15T00:00:00Z', NOW)).toBe('today');
    expect(lastSeenWords('2026-11-15T23:00:00Z', NOW)).toBe('today');
    expect(lastSeenWords('2026-11-14T23:59:59Z', NOW)).toBe('yesterday');
    expect(lastSeenWords('2026-11-14T00:00:00Z', NOW)).toBe('yesterday');
    expect(lastSeenWords('2026-11-12T13:00:00Z', NOW)).toBe('3 days ago');
    expect(lastSeenWords('2025-11-15T12:00:00Z', NOW)).toBe('365 days ago');
    expect(lastSeenWords(null, NOW)).toBe('');
    expect(lastSeenWords('not a date', NOW)).toBe('');
  });

  it('a live device: when it was paired and when it was last seen; a revoked one: when it was revoked', () => {
    offset(0);
    const NOW = Date.parse('2026-11-15T12:00:00Z');
    expect(deviceLine(deviceRow({ created_at: '2026-11-12T09:00:00Z', last_seen_at: '2026-11-15T08:00:00Z' }), NOW))
      .toBe('Paired on Nov 12. Last seen today.');
    expect(deviceLine(deviceRow({ created_at: '2026-11-12T09:00:00Z', last_seen_at: '2026-11-14T08:00:00Z' }), NOW))
      .toBe('Paired on Nov 12. Last seen yesterday.');
    expect(deviceLine(deviceRow({ created_at: '2026-11-01T09:00:00Z', last_seen_at: '2026-11-05T08:00:00Z' }), NOW))
      .toBe('Paired on Nov 1. Last seen 10 days ago.');
    expect(deviceLine(deviceRow({ created_at: '2026-11-12T09:00:00Z', last_seen_at: null }), NOW)).toBe('Paired on Nov 12. Not seen yet.');
    expect(revokedLine(deviceRow({ revoked_at: '2026-11-14T10:00:00Z' }))).toBe('Revoked on Nov 14');
    expect(deviceLine(deviceRow({ created_at: null, last_seen_at: null }), NOW)).toBe('Paired on —. Not seen yet.');
  });
});

describe('a refused or failed request in words', () => {
  it('no answer: a read could not reach the server, a change may or may not have been made', () => {
    expect(problemText(new DOMException('aborted', 'AbortError'), 'see')).toBe(UNREACHABLE_TEXT);
    expect(problemText(new TypeError('Failed to fetch'), 'pair')).toBe(NO_ANSWER_TEXT);
    expect(problemText(new TypeError('Failed to fetch'), 'revoke')).toBe('No answer came back. Check the list before trying again.');
  });

  it('403 asks for the login page, whatever the request', () => {
    for (const doing of ['see', 'pair', 'revoke'] as const) expect(problemText(err(403, { error: 'forbidden' }), doing)).toBe(FORBIDDEN_TEXT);
    expect(FORBIDDEN_TEXT).toBe('Sign in through the login page to manage devices.');
  });

  it('a 404 of the list or of a new code means no pairing on this server; a revoke\'s 404 is the server\'s sentence', () => {
    expect(problemText(err(404, undefined), 'see')).toBe(UNSUPPORTED_TEXT);
    expect(problemText(err(404, { error: 'not found' }), 'pair')).toBe(UNSUPPORTED_TEXT);
    expect(UNSUPPORTED_TEXT).toBe('This server does not support device pairing yet.');
    expect(problemText(err(404, { error: 'no such device' }), 'revoke')).toBe('No such device.');
  });

  it('the server\'s own sentence, capitalised and ended; never an object; else the status', () => {
    expect(problemText(err(409, { error: 'revoke a device first' }), 'pair')).toBe('Revoke a device first.');
    expect(problemText(err(400, { error: 'the name has no letter or digit that can be shown' }), 'pair'))
      .toBe('The name has no letter or digit that can be shown.');
    expect(problemText(err(415, { error: 'Content-Type must be application/json' }), 'pair')).toBe('The request was not sent as JSON, so it was refused.');
    expect(problemText(err(500, { error: { code: 'X', message: '<b>no</b>' } }), 'revoke')).toBe('The request failed (HTTP 500).');
    expect(problemText(err(502, undefined), 'see')).toBe('The request failed (HTTP 502).');
  });

  it('only 403 and 404 of the list replace the section', () => {
    expect(listGate(err(403))).toBe('forbidden');
    expect(listGate(err(404))).toBe('unsupported');
    for (const e of [err(500), err(undefined), new TypeError('Failed to fetch'), null]) expect(listGate(e)).toBeNull();
  });
});

describe('the words follow the owner\'s rules', () => {
  it('no glyph in any sentence, and every sentence ends', () => {
    const texts = (Object.values(devices) as unknown[]).filter((v): v is string => typeof v === 'string');
    texts.push(...Object.values(END_TEXT).map((e) => e.text));
    expect(texts.length).toBeGreaterThan(12);
    for (const t of texts) expect(t, t).not.toMatch(GLYPH);
    for (const t of Object.values(END_TEXT)) expect(t.text).toMatch(/\.$/);
  });
});
