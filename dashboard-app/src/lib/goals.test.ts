import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  ACTION_TEXT, ACTIONS, PENDING_TEXT, RESUME_BLOCKED_TEXT, actorWord, asksFirst, beforeChange, budgetProblem, changeLanded,
  confirmText, createBody,
  createProblems, deadlineProblem, typedNumber,
  deadlineText, formatTokens, goalAction, haltState, journalKindWord, limitFields, listGoals, getGoal, nextSessionText,
  refusalText, resumeLabel, sessionOutcomeWord, spanText, stateWord, type Action, type CreateForm, type GoalDetail, type GoalState, type LostChange, type PauseReason,
} from './goals';
import * as goals from './goals';
import { DETAIL_KEYS, NOW, OPTIONS, SPEND_NOTE, goalDetail, goalRow } from './testing/goalFixtures';

// F114 goals PR 2: the Goals screen's words, rules and requests (no DOM).

const STATES: GoalState[] = ['draft', 'active', 'paused', 'done', 'failed', 'cancelled'];
const REASONS: PauseReason[] = ['owner', 'budget', 'stalled', 'waiting_for_owner', 'error', 'deadline'];
const GLYPH = /[\u2190-\u21FF\u25A0-\u25FF\u2600-\u27BF]|[\u{1F300}-\u{1FAFF}]/u;

function form(over: Partial<CreateForm> = {}): CreateForm {
  return {
    title: 'Sort the photos', objective: 'Put them in folders', done_when: 'Every photo is in a folder', preset: 'small',
    custom: false, limits: limitFields(OPTIONS().presets!.small), quiet_hours_ok: false, origin_channel: 'telegram:4242', ...over,
  };
}

afterEach(() => vi.unstubAllGlobals());

describe('state words', () => {
  it('every state and pause reason has a word and a tone', () => {
    for (const state of STATES) {
      for (const reason of state === 'paused' ? REASONS : [null]) {
        for (const open of [null, 2]) {
          const { text, tone } = stateWord({ state, pause_reason: reason, open_session: open });
          expect(text.trim(), `${state} ${reason}`).not.toBe('');
          expect(['ok', 'warn', 'bad', 'info', 'neutral']).toContain(tone);
        }
      }
    }
  });

  it('pins the table', () => {
    const word = (state: GoalState, reason: PauseReason | null = null, open: number | null = null) =>
      stateWord({ state, pause_reason: reason, open_session: open });
    expect(word('draft')).toEqual({ text: 'Draft', tone: 'neutral' });
    expect(word('active', null, 3)).toEqual({ text: 'Working', tone: 'info' });
    expect(word('active')).toEqual({ text: 'Waiting for its next session', tone: 'ok' });
    // While no session is started on the server, an approved goal is not about to run.
    expect(stateWord({ state: 'active', pause_reason: null, open_session: null }, false)).toEqual({ text: 'Approved', tone: 'neutral' });
    expect(stateWord({ state: 'active', pause_reason: null, open_session: 3 }, false)).toEqual({ text: 'Approved', tone: 'neutral' });
    expect(stateWord({ state: 'paused', pause_reason: 'owner', open_session: null }, false)).toEqual({ text: 'Paused by you', tone: 'warn' });
    expect(word('paused', 'owner')).toEqual({ text: 'Paused by you', tone: 'warn' });
    expect(word('paused', 'waiting_for_owner')).toEqual({ text: 'Waiting for you to confirm it is done', tone: 'info' });
    expect(word('paused', 'error')).toEqual({ text: 'Paused after an error', tone: 'bad' });
    expect(word('done')).toEqual({ text: 'Done', tone: 'ok' });
    expect(word('failed')).toEqual({ text: 'Failed', tone: 'bad' });
    expect(word('cancelled')).toEqual({ text: 'Cancelled', tone: 'neutral' });
  });

  it('states no number or rule the code does not hold (the stall word names no count)', () => {
    for (const reason of REASONS) expect(stateWord({ state: 'paused', pause_reason: reason, open_session: null }).text).not.toMatch(/\d/);
  });
});

describe('actions', () => {
  it('the list of actions is the server order', () => {
    expect([...ACTIONS]).toEqual(['approve', 'run-now', 'resume', 'confirm-done', 'pause', 'stop', 'budget', 'deadline', 'note', 'cancel']);
  });

  it('spending and ending actions say so before they act; pause and note ask nothing', () => {
    const goal = goalDetail({ open_session: 2 });
    for (const action of ['approve', 'run-now', 'resume', 'budget', 'deadline'] as Action[]) {
      expect(ACTION_TEXT[action].spends, action).toBe(true);
      const text = confirmText(action, goal, { driverRunning: true, total: 20_000_000, days: 3, now: NOW });
      expect(text, action).toMatch(/spend|tokens/);
    }
    for (const action of ['stop', 'cancel', 'confirm-done'] as Action[]) {
      expect(ACTION_TEXT[action].ends, action).toBe(true);
      expect(confirmText(action, goal, { driverRunning: true }), action).toMatch(/ends|cancels|closes/);
    }
    for (const action of ['pause', 'note'] as Action[]) expect(asksFirst(action), action).toBe(false);
  });

  it('the confirm sentences use the goal\'s numbers', () => {
    const goal = goalDetail({ total_budget: 10_000_000, day_budget: 2_000_000, deadline_days: 10 });
    const text = confirmText('approve', goal, { driverRunning: true });
    expect(text).toContain('10,000,000 tokens (2,000,000 a day)');
    expect(text).toContain('10 days');
    expect(text.startsWith('This starts the goal.')).toBe(true);
  });

  it('with no driver running, approve and run-now say they mark, not start', () => {
    const goal = goalDetail();
    expect(confirmText('approve', goal, { driverRunning: false })).toMatch(/^This marks the goal ready to start\./);
    expect(confirmText('run-now', goal, { driverRunning: false })).not.toMatch(/starts/);
  });

  it('the budget sentence says raise or lower, and the deadline sentence names the new date', () => {
    const goal = goalDetail({ total_budget: 10_000_000, day_budget: 2_000_000 });
    expect(confirmText('budget', goal, { driverRunning: true, total: 20_000_000 }))
      .toBe('This raises the total budget from 10,000,000 to 20,000,000 tokens. The goal can spend more.');
    expect(confirmText('budget', goal, { driverRunning: true, total: 5_000_000, day: 1_000_000 }))
      .toBe('This lowers the total budget from 10,000,000 to 5,000,000 tokens and lowers the day budget from 2,000,000 to 1,000,000 tokens.');
    const later = confirmText('deadline', goal, { driverRunning: true, days: 3, now: NOW });
    expect(later).toBe('This lets the goal keep working, and spending tokens, 3 more days, until Nov 24 12:00 UTC.');
    const a_draft = goalDetail({ state: 'draft', deadline_at: null, deadline_days: 10 });
    expect(confirmText('deadline', a_draft, { driverRunning: true, days: 1, now: NOW })).toContain('11 days in all');
  });

  it('stop and cancel name the running session', () => {
    expect(confirmText('stop', goalDetail({ open_session: 4 }), { driverRunning: true })).toContain('session 4');
    expect(confirmText('cancel', goalDetail({ open_session: 4 }), { driverRunning: true })).toContain('Session 4');
    expect(confirmText('stop', goalDetail({ open_session: null }), { driverRunning: true })).toMatch(/pauses the goal/);
  });

  it('resume says "Not done yet, keep working" for a goal waiting for the owner', () => {
    expect(resumeLabel({ pause_reason: 'waiting_for_owner' })).toBe('Not done yet, keep working');
    expect(resumeLabel({ pause_reason: 'owner' })).toBe('Resume');
    expect(RESUME_BLOCKED_TEXT).toEqual({ budget: 'Raise the budget first.', deadline: 'Extend the deadline first.' });
    expect(PENDING_TEXT.stop).toBe('A stop was asked for. It has not happened yet.');
    for (const text of Object.values(PENDING_TEXT)) expect(text).not.toMatch(/driver/);
  });
});

describe('the create form', () => {
  const options = OPTIONS();

  it('a valid preset has no problem, and the body names the preset only', () => {
    expect(createProblems(form(), options)).toEqual([]);
    expect(createBody(form())).toEqual({
      title: 'Sort the photos', objective: 'Put them in folders', done_when: 'Every photo is in a folder', preset: 'small',
      origin_channel: 'telegram:4242',
    });
  });

  it('the limits follow the server\'s ranges in its words', () => {
    const limits = { ...limitFields(options.presets!.small) };
    const custom = (over: Partial<Record<keyof typeof limits, string>>) =>
      createProblems(form({ custom: true, limits: { ...limits, ...over } }), options);
    expect(custom({ day_budget: '3000000' })).toEqual(['The day budget cannot be larger than the total budget.']);
    expect(custom({ sessions_per_day: '0' })).toEqual(['Sessions per day must be at least 1 and at most 24.']);
    expect(custom({ deadline_days: '91' })).toEqual(['The deadline in days must be at least 1 and at most 90.']);
    expect(custom({ min_interval_seconds: '59' })).toEqual(['The interval between sessions in seconds must be at least 60 and at most 604,800.']);
    expect(custom({ jobs_per_day: '1.5' })).toEqual(['Jobs per day must be a whole number.']);
    expect(custom({})).toEqual([]);
    // Every range the server sends is checked: a range with a new field is read, not ignored.
    const narrower = OPTIONS({ ranges: [{ field: 'total_budget', low: 1, high: 10, words: 'the total budget' }] });
    expect(createProblems(form({ custom: true }), narrower)).toEqual(['The total budget must be at least 1 and at most 10.']);
  });

  it('a custom body carries every limit', () => {
    const body = createBody(form({ custom: true, quiet_hours_ok: true }));
    expect(body.preset).toBe('custom');
    expect(body.limits).toEqual({ ...options.presets!.small, quiet_hours_ok: true });
  });

  it('the channel is required only without a default chat, and must be telegram:<chat id>', () => {
    expect(createProblems(form({ origin_channel: '' }), options)).toEqual(['Give it a channel: this server has no default chat to ask you.']);
    expect(createProblems(form({ origin_channel: '' }), OPTIONS({ has_default_channel: true }))).toEqual([]);
    expect(createProblems(form({ origin_channel: 'email:me' }), options)).toEqual(['The channel must be telegram:<chat id>.']);
    expect(createProblems(form({ origin_channel: 'telegram:12 34' }), options)).toEqual(['The channel must be telegram:<chat id>.']);
  });

  it('texts are required and bounded', () => {
    expect(createProblems(form({ title: ' ', objective: '', done_when: '' }), options))
      .toEqual(['Give it a title.', 'Give it an objective.', 'Say what done looks like.']);
    expect(createProblems(form({ title: 't'.repeat(201) }), options)).toEqual(['The title is at most 200 characters.']);
  });
});

describe('time words', () => {
  it('next session and deadline', () => {
    const hours = (h: number) => new Date(NOW + h * 3_600_000).toISOString();
    expect(nextSessionText(goalRow({ next_wake_at: hours(2) }), NOW)).toBe('Next session in 2 hours');
    expect(nextSessionText(goalRow({ next_wake_at: hours(-1) }), NOW)).toBe('Next session due now');
    expect(nextSessionText(goalRow({ open_session: 3 }), NOW)).toBe('Session 3 running');
    expect(nextSessionText(goalRow({ state: 'paused', pause_reason: 'waiting_for_owner' }), NOW)).toBe(''); // the state word says it
    expect(nextSessionText(goalRow({ state: 'draft' }), NOW)).toBe('Not approved yet');
    expect(nextSessionText(goalRow({ next_wake_at: hours(2) }), NOW, false)).toBe(''); // no session is started
    expect(nextSessionText(goalRow({ open_session: 3 }), NOW, false)).toBe('');
    expect(deadlineText(goalRow({ deadline_at: hours(24 * 6) }), NOW)).toBe('Deadline in 6 days');
    expect(deadlineText(goalRow({ deadline_at: hours(-1) }), NOW)).toBe('Deadline passed');
    expect(deadlineText({ state: 'draft', deadline_at: null, deadline_days: 3 }, NOW)).toBe('Runs for 3 days once approved');
    expect(deadlineText(goalRow({ state: 'done' }), NOW)).toBe('');
    expect([spanText(30_000), spanText(60_000), spanText(3 * 86_400_000)]).toEqual(['under a minute', '1 minute', '3 days']);
  });

  it('other words', () => {
    expect(formatTokens(2_000_000)).toBe('2,000,000 tokens');
    expect(sessionOutcomeWord(null)).toBe('Running');
    expect(sessionOutcomeWord('step_closed')).toBe('Closed a step');
    expect(journalKindWord('owner_note')).toBe('Your note');
    expect([actorWord('owner'), actorWord('host'), actorWord('chain'), actorWord('harness')])
      .toEqual(['You', 'The host', 'The chain', 'The harness']);
  });
});

describe('requests', () => {
  type Seen = { url: string; method: string; headers?: Record<string, string>; body?: string };
  function stub(status: number, payload: unknown): Seen[] {
    const seen: Seen[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
      seen.push({ url, method: init.method ?? 'GET', headers: init.headers as Record<string, string>, body: init.body as string });
      if (status === 0) throw new TypeError('Failed to fetch');
      return new Response(JSON.stringify(payload), { status });
    }));
    return seen;
  }

  it('use the right verbs and paths, JSON on a change, and encode the ref', async () => {
    const seen = stub(200, goalDetail());
    await goalAction('G-0007', 'confirm-done');
    await goalAction('a/b', 'budget', { total: 5 });
    await listGoals();
    await getGoal('G-0007', 200);
    expect(seen.map((s) => `${s.method} ${s.url}`)).toEqual([
      'POST /goals/G-0007/confirm-done', 'POST /goals/a%2Fb/budget', 'GET /goals?state=open', 'GET /goals/G-0007?journal=200',
    ]);
    expect(seen[0].headers).toEqual({ 'Content-Type': 'application/json' });
    expect(seen[1].body).toBe('{"total":5}');
    expect(seen[2].headers).toBeUndefined(); // a read sends no header of its own: the proxy adds the secret
  });

  it('the halt state is null when the route is missing or fails, never an error', async () => {
    stub(404, { error: 'not found' });
    expect(await haltState()).toBeNull();
    stub(0, null);
    expect(await haltState()).toBeNull();
    stub(200, { halted: true, source: 'owner' });
    expect(await haltState()).toEqual({ halted: true });
    stub(200, { halted: 'yes' });
    expect(await haltState()).toEqual({ halted: false });
  });

  it('a refusal prefers the server\'s sentence and never prints an object', () => {
    const err = (status: number | undefined, body?: unknown) => Object.assign(new Error('x'), { status, body });
    expect(refusalText(err(409, { error: 'G-0001 draft: only an active goal can be paused' })))
      .toBe('G-0001 draft: only an active goal can be paused.');
    expect(refusalText(err(403, { error: 'forbidden' }))).toBe('Your sign-in is needed to change goals. Open the dashboard through the login page.');
    expect(refusalText(err(403), 'see')).toBe('Your sign-in is needed to see goals. Open the dashboard through the login page.');
    expect(refusalText(err(undefined))).toBe('No answer came back. Check the goal before trying again.');
    expect(refusalText(err(undefined), 'see')).toBe('Could not reach the server.');
    expect(refusalText(err(500, { error: { nested: true } }))).toBe('The request failed (HTTP 500).');
    expect(refusalText(err(503))).toBe('Goals are off on this server.');
    expect(refusalText(err(500, '<html>stack</html>'))).toBe('The request failed (HTTP 500).');
  });
});

describe('fixtures and words', () => {
  it('the detail fixture has the real answer\'s keys, in order, and the server\'s spend sentence', () => {
    expect(Object.keys(goalDetail())).toEqual(DETAIL_KEYS);
    expect(goalDetail().spend_note).toBe(SPEND_NOTE);
    expect(OPTIONS().spend_note).toBe(SPEND_NOTE);
  });

  it('no exported string holds a symbol or an icon', () => {
    const strings: string[] = [];
    const walk = (v: unknown) => {
      if (typeof v === 'string') strings.push(v);
      else if (v && typeof v === 'object') Object.values(v).forEach(walk);
    };
    walk(Object.fromEntries(Object.entries(goals).filter(([, v]) => typeof v !== 'function')));
    const goal = goalDetail({ open_session: 1 });
    for (const action of ACTIONS) strings.push(confirmText(action, goal, { driverRunning: false, total: 1, day: 1, days: 1, now: NOW }));
    expect(strings.length).toBeGreaterThan(40);
    for (const s of strings) expect(s, s).not.toMatch(GLYPH);
  });
});

describe('the budget and deadline forms', () => {
  const options = OPTIONS();
  const goal = goalDetail({ total_budget: 10_000_000, day_budget: 2_000_000 });
  it('follow the server\'s ranges in its words, and an unchanged budget is not sent', () => {
    expect(budgetProblem(20_000_000, 2_000_000, goal, options)).toBeNull();
    expect(budgetProblem(0, 2_000_000, goal, options)).toBe('The total budget must be at least 1 and at most 1,000,000,000.');
    expect(budgetProblem(2_000_000_000, 2_000_000, goal, options)).toBe('The total budget must be at least 1 and at most 1,000,000,000.');
    expect(budgetProblem(10_000_000, Number.NaN, goal, options)).toBe('The day budget must be a whole number.');
    expect(budgetProblem(1_000_000, 2_000_000, goal, options)).toBe('The day budget cannot be larger than the total budget.');
    expect(budgetProblem(10_000_000, 2_000_000, goal, options)).toBe('Type a new total or day budget.');
    const narrower = OPTIONS({ ranges: [{ field: 'total_budget', low: 1, high: 15_000_000, words: 'the total budget' }] });
    expect(budgetProblem(20_000_000, 2_000_000, goal, narrower)).toBe('The total budget must be at least 1 and at most 15,000,000.');
  });
  it('a deadline change is 1 to the server\'s bound of whole days', () => {
    expect(deadlineProblem(5, options)).toBeNull();
    expect(deadlineProblem(90, options)).toBeNull();
    for (const bad of [0, 91, Number.NaN, 1.5]) expect(deadlineProblem(bad, options), String(bad)).toBe('Type a whole number of days, 1 to 90.');
    expect(deadlineProblem(30, OPTIONS({ bounds: { ...options.bounds!, deadline_days: 20 } }))).toBe('Type a whole number of days, 1 to 20.');
  });
  it('typed numbers are whole numbers or not a number', () => {
    expect([typedNumber(' 42 '), typedNumber('-3')]).toEqual([42, -3]);
    for (const bad of ['', '1.5', '1e3', '12a']) expect(Number.isNaN(typedNumber(bad)), bad).toBe(true);
  });
});

describe('a lost answer is replaced only on evidence of that change', () => {
  const at = NOW;
  const lostOf = (action: Action, goal: GoalDetail, sent: Partial<LostChange> = {}): LostChange => ({ action, at, before: beforeChange(goal), ...sent });
  const row = (seq: number, kind: string, actor: string, text: string) => ({ seq, session_n: null, kind, actor, text, evidence: [], created_at: null });

  it('a note: an owner_note row newer than the change, with the text as sent (trimmed)', () => {
    const goal = goalDetail();
    const lost = lostOf('note', goal, { text: '  Use the 2024 folder first.  ' });
    const top = goal.journal[0].seq;
    expect(changeLanded(lost, { ...goal, journal: [row(top + 1, 'owner_note', 'owner', 'Use the 2024 folder first.'), ...goal.journal] })).toBe(true);
    expect(changeLanded(lost, { ...goal, journal: [row(top + 1, 'report', 'chain', 'Use the 2024 folder first.'), ...goal.journal] })).toBe(false);
    expect(changeLanded(lost, { ...goal, journal: [row(top + 1, 'owner_note', 'owner', 'Something else'), ...goal.journal] })).toBe(false);
    expect(changeLanded(lost, goal)).toBe(false);
  });

  it('state changes: only what the action produces, from what it was', () => {
    const active = goalDetail({ state: 'active' });
    expect(changeLanded(lostOf('pause', active), { ...active, state: 'paused', pause_reason: 'owner' })).toBe(true);
    expect(changeLanded(lostOf('pause', active), { ...active, state: 'paused', pause_reason: 'error' })).toBe(false);
    expect(changeLanded(lostOf('pause', active), { ...active, spent_today: 5 })).toBe(false);
    expect(changeLanded(lostOf('stop', active), { ...active, state: 'paused', pause_reason: 'owner' })).toBe(true);
    const pausedWithSession = goalDetail({ state: 'paused', pause_reason: 'owner', open_session: 2 });
    expect(changeLanded(lostOf('stop', pausedWithSession), { ...pausedWithSession, pending_request: 'stop' })).toBe(true);
    expect(changeLanded(lostOf('stop', pausedWithSession), pausedWithSession)).toBe(false);
    expect(changeLanded(lostOf('resume', pausedWithSession), { ...pausedWithSession, state: 'active', pause_reason: null })).toBe(true);
    const draft = goalDetail({ state: 'draft' });
    expect(changeLanded(lostOf('approve', draft), { ...draft, state: 'active' })).toBe(true);
    expect(changeLanded(lostOf('approve', draft), { ...draft, state: 'cancelled' })).toBe(false);
    expect(changeLanded(lostOf('cancel', draft), { ...draft, state: 'cancelled' })).toBe(true);
    const waiting = goalDetail({ state: 'paused', pause_reason: 'waiting_for_owner' });
    expect(changeLanded(lostOf('confirm-done', waiting), { ...waiting, state: 'done', pause_reason: null })).toBe(true);
    expect(changeLanded(lostOf('confirm-done', waiting), { ...waiting, state: 'active', pause_reason: null })).toBe(false);
  });

  it('run now: the pending request, or a newer session with the owner\'s trigger', () => {
    const goal = goalDetail();
    const lost = lostOf('run-now', goal);
    expect(changeLanded(lost, { ...goal, pending_request: 'run_now' })).toBe(true);
    expect(changeLanded(lost, { ...goal, sessions: [...goal.sessions, { ...goal.sessions[0], n: 2, trigger: 'run_now' }] })).toBe(true);
    expect(changeLanded(lost, { ...goal, sessions: [...goal.sessions, { ...goal.sessions[0], n: 2, trigger: 'timer' }] })).toBe(false);
  });

  it('budget and deadline: the limits equal what was sent', () => {
    const goal = goalDetail({ total_budget: 10_000_000, day_budget: 2_000_000, deadline_at: '2026-11-21T12:00:00+00:00' });
    const budget = lostOf('budget', goal, { total: 20_000_000 });
    expect(changeLanded(budget, { ...goal, total_budget: 20_000_000 })).toBe(true);
    expect(changeLanded(budget, { ...goal, total_budget: 15_000_000 })).toBe(false);
    expect(changeLanded(budget, goal)).toBe(false);
    const deadline = lostOf('deadline', goal, { days: 3 });
    expect(changeLanded(deadline, { ...goal, deadline_at: '2026-11-24T12:00:00+00:00' })).toBe(true);
    expect(changeLanded(deadline, { ...goal, deadline_at: '2026-11-22T12:00:00+00:00' })).toBe(false);
    const draft = goalDetail({ state: 'draft', deadline_at: null, deadline_days: 10 });
    expect(changeLanded(lostOf('deadline', draft, { days: 4 }), { ...draft, deadline_days: 14 })).toBe(true);
    expect(changeLanded(lostOf('deadline', draft, { days: 4 }), { ...draft, deadline_days: 11 })).toBe(false);
  });
});
