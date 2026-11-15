import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fmtUtc } from './harness';
import { WORDS, haltAnswered, haltFailed, haltView, haltedText, resumeRefused, type HaltInput } from './halt';
import type { BackgroundHaltAnswer, BackgroundState } from './types/api';

// Goals PR 0c, the background halt: every word the Halt and Resume control says, from the state to the view (pure),
// and the one-tap halt through the store.

const SINCE = '2026-11-15T09:31:45Z';
const WHEN = 'Nov 15 09:31 UTC';
const REST = 'Nothing new starts, and every tool call from background work is refused. It does not stop a script that is already running.';
const CAPTION = 'Resuming does not restart work that was cancelled. One-time schedules that came due run now.';
const HOST_TEXT = 'The server is set to hold background work (NOUS_BACKGROUND_HALT). Clear that setting on the host and restart to resume.';
const NOT_SAVED = 'Halted, but the halt is not saved yet. A restart now would lose it.';
const UNREACHABLE = 'Cannot reach the server.';

const running = (over: Partial<BackgroundState> = {}): BackgroundState => ({
  halted: false, source: 'none', since: null, by: null, surface: null, verified: false, reason: null, persisted: true, ...over,
});
const halted = (over: Partial<BackgroundState> = {}): BackgroundState =>
  running({ halted: true, source: 'stored', since: SINCE, by: 'dashboard', surface: 'dashboard', verified: true, ...over });
const view = (over: Partial<HaltInput>) => haltView({ state: null, unreachable: false, pending: null, outcome: null, ...over });
const labels = (v: ReturnType<typeof haltView>) => v.buttons.map((b) => b.label);

const ANSWER: BackgroundHaltAnswer = {
  halted: true, already: false, verified: false, persisted: true, text: 'Halted. Cancelled 2 subtasks and 1 command.',
  report: { cancelled: { chains: 0, subtasks: 2, subtasks_running: 1, dags: 0, commands: 1, jobs: 0 }, failed: [], jobs_checked: true },
};

describe('the words per state', () => {
  it('running: the state pill and the Halt button', () => {
    const v = view({ state: running() });
    expect(v.pill).toEqual({ text: 'Background work running', tone: 'ok' });
    expect(v.buttons).toEqual([{ kind: 'halt', label: 'Halt background work', disabled: false }]);
    expect(v.lines).toEqual([]);
    expect(v.caption).toBeNull();
    expect(v.halted).toBe(false);
  });

  it('stopping (the halt call in flight): the pill says Stopping and the button is disabled', () => {
    const v = view({ state: running(), pending: 'halt' });
    expect(v.pill?.text).toBe('Stopping');
    expect(v.buttons).toEqual([{ kind: 'halt', label: 'Halt background work', disabled: true }]);
  });

  it.each(['dashboard', 'telegram'] as const)('halted by you (verified, surface %s): the pill, the sentence, Resume and its caption', (surface) => {
    const v = view({ state: halted({ surface }) });
    expect(v.pill).toEqual({ text: 'Background work halted', tone: 'warn' });
    expect(v.lines).toEqual([`Halted by you on ${WHEN}. ${REST}`]);
    expect(v.buttons).toEqual([{ kind: 'resume', label: 'Resume background work', disabled: false }]);
    expect(v.caption).toBe(CAPTION);
    expect(v.halted).toBe(true);
  });

  it('a halt that carried no secret is not "by you", whatever surface it names', () => {
    expect(view({ state: halted({ surface: 'dashboard', verified: false }) }).lines).toEqual([`Halted on ${WHEN}. ${REST}`]);
  });

  it('halted by the agent: "Halted by Nous on"', () => {
    expect(view({ state: halted({ surface: 'agent', verified: false }) }).lines).toEqual([`Halted by Nous on ${WHEN}. ${REST}`]);
  });

  it.each(['host', 'unknown', null] as const)('halted from surface %s: "Halted on"', (surface) => {
    expect(view({ state: halted({ surface, verified: false }) }).lines).toEqual([`Halted on ${WHEN}. ${REST}`]);
  });

  it('with no time recorded the sentence drops " on <time>"', () => {
    expect(haltedText({ since: null, surface: 'dashboard', verified: true })).toBe(`Halted by you. ${REST}`);
    expect(haltedText({ since: null, surface: 'agent', verified: false })).toBe(`Halted by Nous. ${REST}`);
    expect(haltedText({ since: 'not a time', surface: null, verified: false })).toBe(`Halted. ${REST}`);
  });

  it('not saved: a halt that is not stored yet says a restart would lose it', () => {
    const v = view({ state: halted({ persisted: false }) });
    expect(v.lines).toEqual([`Halted by you on ${WHEN}. ${REST}`, NOT_SAVED]);
    expect(labels(v)).toEqual(['Resume background work']);
  });

  it('an unreadable halt is treated as halted, with Resume', () => {
    for (const s of [halted({ source: 'unreadable', surface: null, since: null, verified: false }), running({ source: 'unreadable' })]) {
      const v = view({ state: s });
      expect(v.halted).toBe(true);
      expect(v.pill?.text).toBe('Background work halted');
      expect(v.lines[0]).toBe(`Halted. ${REST}`);
      expect(labels(v)).toEqual(['Resume background work']);
    }
  });

  it('host: its pill and sentence, no Resume, no caption, and never "not saved"', () => {
    const v = view({ state: halted({ source: 'host', surface: 'host', persisted: false }) });
    expect(v.pill).toEqual({ text: 'Halted by the host', tone: 'warn' });
    expect(v.lines).toEqual([HOST_TEXT]);
    expect(v.buttons).toEqual([]);
    expect(v.caption).toBeNull();
    expect(v.halted).toBe(true);
  });

  it('before the first answer: no state word, the Halt button enabled, nothing to say', () => {
    const v = view({});
    expect(v.pill).toBeNull();
    expect(v.buttons).toEqual([{ kind: 'halt', label: 'Halt background work', disabled: false }]);
    expect(v.lines).toEqual([]);
  });

  it('unreachable: "Cannot reach the server." and the Halt button stays enabled', () => {
    for (const state of [null, running()]) {
      const v = view({ state, unreachable: true });
      expect(v.lines).toEqual([UNREACHABLE]);
      expect(v.buttons).toEqual([{ kind: 'halt', label: 'Halt background work', disabled: false }]);
    }
    // A halt that could not be sent says the same, while the state is still running.
    expect(view({ state: running(), outcome: haltFailed() }).lines).toEqual([UNREACHABLE]);
    expect(view({ state: halted(), outcome: haltFailed() }).lines).toEqual([`Halted by you on ${WHEN}. ${REST}`]);
    // Said once, not twice, when the poll fails too.
    expect(view({ state: running(), unreachable: true, outcome: haltFailed() }).lines).toEqual([UNREACHABLE]);
  });
});

describe('what a press leaves to say', () => {
  it('a halt that stopped everything shows the answer’s text once, under the halted sentence', () => {
    const outcome = haltAnswered(ANSWER);
    const v = view({ state: halted(), outcome });
    expect(v.lines).toEqual([`Halted by you on ${WHEN}. ${REST}`, ANSWER.text]);
    expect(labels(v)).toEqual(['Resume background work']);
    // Resumed from elsewhere since: the text no longer applies.
    expect(view({ state: running(), outcome }).lines).toEqual([]);
  });

  it('a partial sweep names the steps that failed and keeps a Halt button beside Resume', () => {
    const outcome = haltAnswered({ ...ANSWER, report: { ...ANSWER.report, failed: ['subtasks', 'jobs'] } });
    const v = view({ state: halted(), outcome });
    expect(v.lines).toContain(
      'Could not stop everything: subtasks, jobs. Background tool calls are refused, so nothing can act. Press Halt again to retry.',
    );
    expect(v.buttons).toEqual([
      { kind: 'halt', label: 'Halt background work', disabled: false },
      { kind: 'resume', label: 'Resume background work', disabled: false },
    ]);
  });

  it.each([
    [403, { error: 'forbidden', reason: 'no_secret' }, 'Resume needs your sign-in. Open the dashboard through the login page.'],
    [403, undefined, 'Resume needs your sign-in. Open the dashboard through the login page.'], // the login proxy's own page
    [403, { error: 'forbidden', reason: 'not_configured' }, 'Resume is not configured on this server.'],
    [409, { raced: true }, 'A halt arrived while resuming, so it stays halted.'],
    [409, { host_hold: true }, HOST_TEXT],
    [503, { error: 'not_saved' }, 'Resume could not be saved. Background work stays halted; try again.'],
    [undefined, undefined, UNREACHABLE], // no answer at all
  ])('a refused resume (%s %j) says: %s', (status, body, said) => {
    const outcome = resumeRefused({ status, body });
    expect(outcome.message).toBe(said);
    const v = view({ state: halted(), outcome });
    expect(v.lines).toEqual([`Halted by you on ${WHEN}. ${REST}`, said]);
    expect(labels(v)).toEqual(['Resume background work']);
  });

  it('a resume in flight disables Resume and keeps the halted words', () => {
    const v = view({ state: halted(), pending: 'resume' });
    expect(v.pill?.text).toBe('Background work halted');
    expect(v.buttons).toEqual([{ kind: 'resume', label: 'Resume background work', disabled: true }]);
  });

  it('the fixed words are the ones exported', () => {
    expect([WORDS.caption, WORDS.hostText, WORDS.notSaved, WORDS.unreachable]).toEqual([CAPTION, HOST_TEXT, NOT_SAVED, UNREACHABLE]);
  });
});

describe('timestamps through the shared formatter', () => {
  it('the halted sentence prints the time as fmtUtc does', () => {
    const at = '2026-01-02T03:04:59Z';
    expect(haltedText({ since: at, surface: 'telegram', verified: true })).toBe(`Halted by you on ${fmtUtc(at)}. ${REST}`);
    expect(fmtUtc(at)).toBe('Jan 2 03:04 UTC');
  });

  it('source contract: lib/halt.ts formats no date of its own', () => {
    const src = readFileSync(join(__dirname, 'halt.ts'), 'utf-8');
    expect(src).toMatch(/import \{ fmtUtc \} from '\.\/harness';/);
    expect(src).not.toMatch(/getUTC|toLocale|toISOString|\.slice\(/);
  });
});

describe('one tap halts', () => {
  type Call = { url: string; method: string; headers: unknown; body: unknown };
  let calls: Call[] = [];
  let stateAnswer: BackgroundState = running();
  let answers: Record<string, [number, unknown]> = {};

  beforeEach(() => {
    vi.resetModules();
    calls = [];
    stateAnswer = running();
    answers = {};
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? 'GET';
      calls.push({ url, method, headers: init.headers, body: typeof init.body === 'string' ? JSON.parse(init.body) : undefined });
      const [status, payload] = method === 'GET' ? [200, stateAnswer] : answers[`${method} ${url}`] ?? [500, { error: 'unexpected' }];
      if (status === 0) throw new TypeError('Failed to fetch');
      return new Response(payload === undefined ? '' : JSON.stringify(payload), { status });
    }));
  });
  afterEach(() => vi.unstubAllGlobals());

  it('a single press posts once, with the exact type and body and no secret, then shows the halted state', async () => {
    answers['POST /background/halt'] = [200, ANSWER];
    const { haltNow, haltState } = await import('./stores/halt');
    const first = haltNow();
    const second = haltNow(); // a second tap while the first is in flight sends nothing
    expect(get(haltState).pill?.text).toBe('Stopping');
    stateAnswer = halted();
    await Promise.all([first, second]);
    const posts = calls.filter((c) => c.method === 'POST');
    expect(posts).toEqual([
      { url: '/background/halt', method: 'POST', headers: { 'Content-Type': 'application/json' }, body: { actor: 'dashboard', surface: 'dashboard' } },
    ]);
    const v = get(haltState);
    expect(v.pill?.text).toBe('Background work halted');
    expect(v.lines).toEqual([`Halted by you on ${WHEN}. ${REST}`, ANSWER.text]);
    // The press refreshed the state at once.
    expect(calls.filter((c) => c.url === '/background/state')).toHaveLength(1);
  });

  it('a halt that gets no answer says so and leaves the Halt button enabled', async () => {
    answers['POST /background/halt'] = [0, undefined];
    const { haltNow, haltState } = await import('./stores/halt');
    await haltNow();
    const v = get(haltState);
    expect(v.lines).toEqual([UNREACHABLE]);
    expect(v.buttons).toEqual([{ kind: 'halt', label: 'Halt background work', disabled: false }]);
  });

  it('Resume posts once to its route with the actor only; a refusal is said', async () => {
    stateAnswer = halted();
    answers['POST /background/resume'] = [403, { error: 'forbidden', reason: 'no_secret' }];
    const { resumeNow, haltPoll, haltState } = await import('./stores/halt');
    await haltPoll.refresh();
    await resumeNow();
    expect(calls.filter((c) => c.method === 'POST')).toEqual([
      { url: '/background/resume', method: 'POST', headers: { 'Content-Type': 'application/json' }, body: { actor: 'dashboard' } },
    ]);
    expect(get(haltState).lines).toContain('Resume needs your sign-in. Open the dashboard through the login page.');
  });

  it('a Resume that succeeds leaves nothing to say once the state is running', async () => {
    stateAnswer = halted();
    answers['POST /background/resume'] = [200, { halted: false, changed: true, text: 'Background work is running again.' }];
    const { resumeNow, haltPoll, haltState } = await import('./stores/halt');
    await haltPoll.refresh();
    stateAnswer = running();
    await resumeNow();
    const v = get(haltState);
    expect(v.pill?.text).toBe('Background work running');
    expect(v.lines).toEqual([]);
  });
});
