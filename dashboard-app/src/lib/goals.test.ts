import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  ACTION_TEXT, ACTIONS, PENDING_TEXT, RESUME_BLOCKED_TEXT, actorWord, asksFirst, beforeChange, budgetProblem, changeLanded,
  confirmText, createBody,
  createProblems, deadlineProblem, typedNumber,
  deadlineText, formatTokens, goalAction, haltState, journalKindWord, limitFields, listGoals, getGoal, nextSessionText,
  refusalText, resumeLabel, sessionOutcomeWord, spanText, stateWord, type Action, type CreateForm, type GoalDetail, type GoalState, type LostChange, type PauseReason,
} from './goals';
import * as goals from './goals';
import { ACTIVITY_KEYS, DETAIL_KEYS, NOW, OPTIONS, PACING, SPEND_NOTE, activityAnswer, goalDetail, goalRow } from './testing/goalFixtures';

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
    expect([...ACTIONS]).toEqual(['approve', 'run-now', 'resume', 'confirm-done', 'pause', 'stop', 'budget', 'sessions', 'deadline', 'note', 'cancel', 'contained']);
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
    for (const action of ['pause', 'note', 'contained'] as Action[]) expect(asksFirst(action), action).toBe(false);
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

describe('the tools a goal may use without asking (F115)', () => {
  it('groups the tools that can be allowed: the built-in ones, then each server by name', () => {
    const built = { tool: 'send_email', server_name: null, server_host: null } as unknown as goals.GoalToolOption;
    const notes = { tool: 'mcp__notes__a', server_name: 'notes', server_host: 'notes.example.com' } as unknown as goals.GoalToolOption;
    const alpha = { tool: 'mcp__alpha__b', server_name: 'alpha', server_host: 'alpha.example.com' } as unknown as goals.GoalToolOption;
    const groups = goals.toolGroups([notes, built, alpha]);
    expect(groups.map((g) => [g.words, g.rows.map((r) => r.tool)])).toEqual([
      ['Reaching outside', ['send_email']],
      ['Tools from other servers: alpha (alpha.example.com)', ['mcp__alpha__b']],
      ['Tools from other servers: notes (notes.example.com)', ['mcp__notes__a']],
    ]);
    expect(goals.toolGroups([])).toEqual([]);
  });

  it('sends the confirmation, an MCP tool its server id, and a removal an empty object', async () => {
    const sent: [string, string, unknown][] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
      sent.push([init.method ?? 'GET', url, init.body === undefined ? undefined : JSON.parse(String(init.body))]);
      return new Response(JSON.stringify({ enabled: true, grants_enabled: true, changed: true }), { status: 200 });
    }));
    await goals.allowTool('G-0001', { tool: 'send_email', server_id: null, identity: null } as unknown as goals.GoalToolOption);
    await goals.allowTool('G-0001', { tool: 'mcp__notes__a', server_id: 'abc', identity: 'f00d' } as unknown as goals.GoalToolOption);
    await goals.removeTool('G-0001', 'send_email');
    await goals.goalTools('G 1');
    expect(sent).toEqual([
      ['PUT', '/goals/G-0001/tools/send_email', { confirm: true }],
      ['PUT', '/goals/G-0001/tools/mcp__notes__a', { confirm: true, identity: 'f00d' }],
      ['DELETE', '/goals/G-0001/tools/send_email', {}],
      ['GET', '/goals/G%201/tools', undefined],
    ]);
  });

  it("a refused tool change is the server's sentence as sent", () => {
    const err = Object.assign(new Error('x'), { status: 409, body: { error: 'send_email is locked by the server and cannot be given to a goal.' } });
    expect(goals.toolRefusalText(err)).toBe('send_email is locked by the server and cannot be given to a goal.');
    expect(goals.toolRefusalText(Object.assign(new Error('x'), { status: undefined }))).toBe(goals.NO_ANSWER_TEXT);
  });

  it('the words have no glyph standing for a word', () => {
    for (const text of [
      goals.TOOLS_TITLE, goals.TOOLS_NONE_TEXT, goals.TOOLS_ADD_TITLE, goals.TOOLS_NOT_USED_TEXT, goals.TOOL_ALLOWED_TEXT,
      goals.TOOL_ALREADY_ALLOWED_TEXT, goals.TOOL_REMOVED_TEXT, goals.TOOL_ALREADY_REMOVED_TEXT, goals.TOOL_RELOAD_TEXT,
      goals.toolsLimitText(20), goals.toolAllowedOnText('2026-11-15T10:00:00+00:00'),
    ]) {
      expect(text).not.toMatch(GLYPH);
      expect(text).toMatch(/[.]$|^[A-Z][a-z]/);
    }
  });
});

describe('goal pacing: how often a goal works, and changing its session settings', () => {
  const options = OPTIONS({ pacing: PACING() });
  const goal = goalDetail({ sessions_per_day: 4, min_interval_seconds: 3600, jobs_per_day: 2, quiet_hours_ok: false, stall_limit: 3, daily_at: null });
  const start = () => goals.sessionsForm(goal, options);

  it('the action is "Change how it works", it asks first, and its sentence says when it applies', () => {
    expect(ACTION_TEXT.sessions.label).toBe('Change how it works');
    expect(asksFirst('sessions')).toBe(true);
    expect(confirmText('sessions', goal, { driverRunning: true })).toBe(goals.SESSIONS_NOTE);
    expect(goals.SESSIONS_NOTE).toBe('This applies from the next session. A session that is running now is not changed. To start a session sooner, use Run now.');
  });

  it('how often, in words', () => {
    expect(goals.howOftenText({ daily_at: '09:00' }, options)).toBe('Once a day, from 09:00 (America/New_York)');
    expect(goals.howOftenText({ daily_at: null }, options)).toBe('As often as its limits allow');
  });

  it('the form starts from the goal, with the gap in minutes and the default time', () => {
    expect(start()).toEqual({
      sessions_per_day: '4', gap_minutes: '60', jobs_per_day: '2', quiet_hours_ok: false, stall_limit: '3', daily: false, daily_at: '09:00',
    });
    expect(goals.sessionsForm({ ...goal, daily_at: '07:30' }, options)).toMatchObject({ daily: true, daily_at: '07:30' });
  });

  it('only what changed is sent', () => {
    expect(goals.sessionsBody({ ...start(), sessions_per_day: '1' }, start())).toEqual({ sessions_per_day: 1 });
    expect(goals.sessionsBody({ ...start(), gap_minutes: '90', stall_limit: '5' }, start())).toEqual({ min_interval_seconds: 5400, stall_limit: 5 });
    expect(goals.sessionsBody({ ...start(), quiet_hours_ok: true, jobs_per_day: '0' }, start())).toEqual({ jobs_per_day: 0, quiet_hours_ok: true });
    expect(goals.sessionsBody({ ...start(), daily: true }, start())).toEqual({ daily: true, daily_at: '09:00' });
    expect(goals.sessionsBody({ ...start(), sessions_per_day: ' 04 ' }, start())).toEqual({});
    const daily = goals.sessionsForm({ ...goal, daily_at: '07:30' }, options);
    expect(goals.sessionsBody({ ...daily, daily_at: '08:15' }, daily)).toEqual({ daily: true, daily_at: '08:15' });
    expect(goals.sessionsBody({ ...daily, daily: false }, daily)).toEqual({ daily: false });
    expect(goals.sessionsBody({ ...daily }, daily)).toEqual({});
  });

  it('a gap that is not whole minutes is kept unless it is changed', () => {
    const odd = goals.sessionsForm({ ...goal, min_interval_seconds: 90 }, options);
    expect(odd.gap_minutes).toBe('1.5');
    expect(goals.sessionsProblem({ ...odd, stall_limit: '4' }, odd, options)).toBeNull();
    expect(goals.sessionsBody({ ...odd, stall_limit: '4' }, odd)).toEqual({ stall_limit: 4 });
  });

  it('a value out of its range is said in words, and nothing changed asks for a change', () => {
    const problem = (over: Partial<goals.SessionsForm>) => goals.sessionsProblem({ ...start(), ...over }, start(), options);
    expect(problem({})).toBe('Change a setting first.');
    expect(problem({ sessions_per_day: '1' })).toBeNull();
    expect(problem({ sessions_per_day: '25' })).toBe('Sessions per day must be at least 1 and at most 24.');
    expect(problem({ sessions_per_day: 'x' })).toBe('Sessions per day must be a whole number.');
    expect(problem({ gap_minutes: '0' })).toBe('The shortest gap between sessions must be at least 1 and at most 10,080 minutes.');
    expect(problem({ gap_minutes: '10080' })).toBeNull();
    expect(problem({ jobs_per_day: '21' })).toBe('Jobs per day must be at least 0 and at most 20.');
    expect(problem({ stall_limit: '11' })).toBe('The stall limit must be at least 1 and at most 10.');
    expect(problem({ stall_limit: '0' })).toBe('The stall limit must be at least 1 and at most 10.');
    expect(problem({ daily: true, daily_at: '9:00' })).toBe('Type the time of day as HH:MM, from 00:00 to 23:59.');
    expect(problem({ daily: true, daily_at: '24:00' })).toBe('Type the time of day as HH:MM, from 00:00 to 23:59.');
    expect(problem({ daily: true, daily_at: '23:59' })).toBeNull();
    const narrower = OPTIONS({ pacing: PACING({ stall_limit: { low: 2, high: 5 } }) });
    expect(goals.sessionsProblem({ ...start(), stall_limit: '6' }, start(), narrower)).toBe('The stall limit must be at least 2 and at most 5.');
  });

  it('a lost answer is replaced on the owner\'s own row of the change', () => {
    const lost: LostChange = { action: 'sessions', at: NOW, before: beforeChange(goal), sessions: { stall_limit: 3 } };
    const top = goal.journal[0].seq;
    const row = (seq: number, actor: string, text: string) => ({ seq, session_n: null, kind: 'state', actor, text, evidence: [], created_at: null });
    expect(changeLanded(lost, { ...goal, journal: [row(top + 1, 'owner', 'Session settings: stall limit 3 -> 4.'), ...goal.journal] })).toBe(true);
    expect(changeLanded(lost, { ...goal, journal: [row(top + 1, 'chain', 'Session settings: stall limit 3 -> 4.'), ...goal.journal] })).toBe(false);
    expect(changeLanded(lost, { ...goal, journal: [row(top + 1, 'owner', 'Budget: total 1 -> 2 tokens.'), ...goal.journal] })).toBe(false);
    expect(changeLanded(lost, goal)).toBe(false);
  });

  it('the create form sends a time of day only for once a day, and checks it', () => {
    const daily = { ...form(), daily: true, daily_at: '07:45' };
    expect(createProblems(daily, options)).toEqual([]);
    expect(createBody(daily)).toMatchObject({ daily_at: '07:45' });
    expect('daily_at' in createBody({ ...daily, daily: false })).toBe(false);
    expect('daily_at' in createBody(form())).toBe(false);
    expect(createProblems({ ...daily, daily_at: '7:45' }, options)).toEqual(['Type the time of day as HH:MM, from 00:00 to 23:59.']);
    expect(createProblems({ ...daily, daily: false, daily_at: '7:45' }, options)).toEqual([]);
  });
});

describe('goal pacing: the review follow-ups', () => {
  const options = OPTIONS({ pacing: PACING() });
  const goal = goalDetail({ sessions_per_day: 4, min_interval_seconds: 3600, jobs_per_day: 2, quiet_hours_ok: false, stall_limit: 3, daily_at: null });
  const start = () => goals.sessionsForm(goal, options);

  it('the hint says a goal approved or changed after its time starts at once', () => {
    expect(goals.dailyHint('America/New_York', 'create')).toBe('Time of day in America/New_York. It starts one session at that time each day. Approved after that time, it starts its first session at once.');
    expect(goals.dailyHint('America/New_York', 'change')).toBe('Time of day in America/New_York. It starts one session at that time each day. If that time has passed today and no session ran today, one starts at once.');
  });

  it('the pause sentence is said only when the stall limit is lowered', () => {
    expect(goals.sessionsNote(start(), start())).toBe(goals.SESSIONS_NOTE);
    expect(goals.sessionsNote({ ...start(), sessions_per_day: '1' }, start())).toBe(goals.SESSIONS_NOTE);
    expect(goals.sessionsNote({ ...start(), stall_limit: '5' }, start())).toBe(goals.SESSIONS_NOTE);
    expect(goals.sessionsNote({ ...start(), stall_limit: '2' }, start())).toBe(
      `${goals.SESSIONS_NOTE} If its last 2 sessions in a row finished nothing, Nous pauses it at its next check.`,
    );
    expect(goals.sessionsNote({ ...start(), stall_limit: '1' }, start())).toBe(
      `${goals.SESSIONS_NOTE} If its last session finished nothing, Nous pauses it at its next check.`,
    );
  });

  it('once a day, sessions per day is not sent or checked, and the facts say why', () => {
    expect(goals.sessionsBody({ ...start(), daily: true, sessions_per_day: '7' }, start())).toEqual({ daily: true, daily_at: '09:00' });
    expect(goals.sessionsProblem({ ...start(), daily: true, sessions_per_day: 'x' }, start(), options)).toBeNull();
    expect(goals.sessionsPerDayText({ sessions_per_day: 4, daily_at: '09:00' })).toBe('One, as it works once a day');
    expect(goals.sessionsPerDayText({ sessions_per_day: 4, daily_at: null })).toBe('4');
    expect(goals.sessionsPerDayText({ sessions_per_day: 4 })).toBe('4');
    expect(goals.ONCE_A_DAY_SESSIONS_NOTE).toBe('Sessions per day does not apply: a goal that works once a day starts one session a day.');
  });
});

describe('a lost change of session settings is confirmed only by the values asked for', () => {
  const goal = goalDetail({ sessions_per_day: 4, min_interval_seconds: 3600, jobs_per_day: 2, quiet_hours_ok: false, stall_limit: 3, daily_at: null });
  const daily = goalDetail({ ...goal, daily_at: '07:30' });
  const top = goal.journal[0].seq;
  const row = (actor = 'owner') =>
    ({ seq: top + 1, session_n: null, kind: 'state', actor, text: 'Session settings: stall limit 3 -> 4.', evidence: [], created_at: null });
  const lostOf = (from: GoalDetail, sessions?: goals.SessionsBody): LostChange =>
    ({ action: 'sessions', at: NOW, before: beforeChange(from), ...(sessions ? { sessions } : {}) });
  const after = (from: GoalDetail, over: Partial<GoalDetail>) => ({ ...from, ...over, journal: [row(), ...from.journal] });

  it('a newer owner row with the values asked for is proof', () => {
    expect(changeLanded(lostOf(goal, { stall_limit: 4 }), after(goal, { stall_limit: 4 }))).toBe(true);
    const all = { sessions_per_day: 1, min_interval_seconds: 600, jobs_per_day: 0, quiet_hours_ok: true, stall_limit: 4 };
    expect(changeLanded(lostOf(goal, all), after(goal, all))).toBe(true);
    expect(changeLanded(lostOf(goal, { daily: true, daily_at: '07:30' }), after(goal, { daily_at: '07:30' }))).toBe(true);
    expect(changeLanded(lostOf(daily, { daily: false }), after(daily, { daily_at: null }))).toBe(true);
  });

  it('a newer owner row made with other values, from another tab, is not proof', () => {
    expect(changeLanded(lostOf(goal, { stall_limit: 4 }), after(goal, { stall_limit: 6 }))).toBe(false);
    expect(changeLanded(lostOf(goal, { daily: true, daily_at: '07:30' }), after(goal, { daily_at: '08:00' }))).toBe(false);
    expect(changeLanded(lostOf(goal, { daily: true, daily_at: '07:30' }), after(goal, { daily_at: null }))).toBe(false);
    expect(changeLanded(lostOf(daily, { daily: false }), after(daily, { daily_at: '08:00' }))).toBe(false);
  });

  it('a partial match is not proof', () => {
    const asked = { stall_limit: 4, jobs_per_day: 0, daily: true, daily_at: '07:30' };
    expect(changeLanded(lostOf(goal, asked), after(goal, { stall_limit: 4, jobs_per_day: 2, daily_at: '07:30' }))).toBe(false);
    expect(changeLanded(lostOf(goal, asked), after(goal, { stall_limit: 4, jobs_per_day: 0, daily_at: null }))).toBe(false);
    expect(changeLanded(lostOf(goal, asked), after(goal, { stall_limit: 4, jobs_per_day: 0, daily_at: '07:30' }))).toBe(true);
  });

  it('the values alone, a row by another actor, or nothing asked is not proof', () => {
    expect(changeLanded(lostOf(goal, { stall_limit: 4 }), { ...goal, stall_limit: 4 })).toBe(false);
    expect(changeLanded(lostOf(goal, { stall_limit: 4 }), { ...goal, stall_limit: 4, journal: [row('chain'), ...goal.journal] })).toBe(false);
    expect(changeLanded(lostOf(goal, {}), after(goal, {}))).toBe(false);
    expect(changeLanded(lostOf(goal), after(goal, { stall_limit: 4 }))).toBe(false);
describe('what a goal is doing now, and the folding page', () => {
  const MIN = 60_000;
  const at = (minutesBefore: number) => new Date(NOW - minutesBefore * MIN).toISOString();
  const current = (over: Partial<goals.ActivityCurrent> = {}): goals.ActivityCurrent => ({
    kind: 'task', name: 'Sort the photos', first_task: false, status: 'running', started_at: at(12), waiting_for: null, ...over,
  });
  const PHASES: goals.ActivityPhase[] = ['none', 'task', 'workflow', 'waiting', 'approval', 'deciding', 'asking', 'between'];

  it('reads GET /goals/<ref>/activity, the ref encoded', async () => {
    const fetch = vi.fn(async (_url: string) => new Response(JSON.stringify(activityAnswer()), { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    await goals.getGoalActivity('G 1/2');
    expect(fetch.mock.calls[0][0]).toBe('/goals/G%201%2F2/activity');
  });

  it('the fixture has every key of a real answer, in order', () => {
    expect(Object.keys(activityAnswer())).toEqual(ACTIVITY_KEYS);
  });

  it('every phase, and every kind of wait, has words', () => {
    for (const phase of PHASES) {
      const text = goals.phaseText(activityAnswer({ phase, current: phase === 'none' ? null : current() }));
      expect(text, phase).toMatch(/\S/);
      expect(text, phase).not.toMatch(GLYPH);
    }
    const said = (over: Partial<goals.GoalActivity>) => goals.phaseText(activityAnswer(over));
    expect(said({ phase: 'task', current: current() })).toBe('Working on a task: Sort the photos');
    expect(said({ phase: 'task', current: current({ name: null, first_task: true }) })).toBe('Working on its first task of this session.');
    expect(said({ phase: 'task', current: current({ status: 'pending', started_at: null }) })).toBe('Waiting to start a task: Sort the photos');
    expect(said({ phase: 'workflow', current: current({ kind: 'workflow', name: 'Build the export' }) })).toBe('Running a workflow: Build the export');
    expect(said({ phase: 'workflow', current: current({ kind: 'workflow', name: 'Build the export', status: 'pending' }) })).toBe(
      'Waiting to start a workflow: Build the export',
    );
    const waiting = (waiting_for: goals.WaitingFor) => said({ phase: 'waiting', current: current({ kind: 'workflow', waiting_for }) });
    expect(waiting({ kind: 'job', job_id: 'job-20261122-102031-ab12cd34' })).toBe('Waiting for a coding job (job-20261122-102031-ab12cd34) to finish.');
    expect(waiting({ kind: 'pr', number: 951 })).toBe('Waiting for pull request 951.');
    expect(waiting({ kind: 'file' })).toBe('Waiting for a file to appear.');
    expect(waiting({ kind: 'other' })).toBe('Waiting for a condition.');
    expect(said({ phase: 'approval', current: current({ kind: 'workflow' }) })).toBe('Waiting for your approval of a workflow step.');
    expect(said({ phase: 'deciding', current: null })).toBe('Deciding what to do next.');
    expect(said({ phase: 'asking', current: null })).toBe('Waiting for your answer to a question.');
    expect(said({ phase: 'between', current: null })).toBe('Between steps.');
    expect(said({ session: null, phase: 'none', current: null })).toBe('No session has run yet.');
  });

  it('more than one item running says how many more', () => {
    expect(goals.phaseText(activityAnswer({ phase: 'task', current: current(), running_count: 3 }))).toBe('Working on a task: Sort the photos and 2 more');
    expect(goals.phaseText(activityAnswer({ phase: 'task', current: current({ name: null, first_task: true }), running_count: 2 }))).toBe(
      'Working on its first task of this session and 1 more.',
    );
  });

  it('every stored outcome has a word, and an unknown one reads "other"', () => {
    const words = { ran: 'done', error: 'failed', refused: 'refused', gated: 'held for approval', suppressed: 'skipped', retired: 'retired tool', bad_input: 'bad input', other: 'other' };
    for (const [outcome, word] of Object.entries(words)) expect(goals.outcomeWord(outcome)).toBe(word);
    expect(goals.outcomeWord('something-new')).toBe('other');
    expect(goals.workKindWord('decisions')).toBe('decisions');
    expect(goals.workLinkLabel('workflow')).toBe('Workflow step');
    expect(goals.workLinkLabel('something-new')).toBe('Other');
  });

  it('counts in words, singular and plural, and "more than" when the read was capped', () => {
    expect(goals.callsText({ total: 31, errors: 2, capped: false })).toBe('31 tool calls this session, 2 failed.');
    expect(goals.callsText({ total: 1, errors: 1, capped: false })).toBe('1 tool call this session, 1 failed.');
    expect(goals.callsText({ total: 4, errors: 0, capped: false })).toBe('4 tool calls this session.');
    expect(goals.callsText({ total: 2000, errors: 3, capped: true })).toBe('More than 2,000 tool calls this session, at least 3 failed.');
    expect(goals.jobsText({ launched: 1, last_launch_at: at(40), capped: false }, NOW)).toBe('Coding jobs started this session: 1. The last one 40 minutes ago.');
    expect(goals.jobsText({ launched: 3, last_launch_at: at(40), capped: true }, NOW)).toBe(
      'Coding jobs started this session: at least 3. The last one 40 minutes ago.',
    );
    expect(goals.jobsText({ launched: 0, last_launch_at: null, capped: false }, NOW)).toBe('');
  });

  it('the times say how long ago, from the page clock', () => {
    expect(goals.startedText(at(12), NOW)).toBe('Started 12 minutes ago.');
    expect(goals.lastCallText(at(4), 'bash', NOW)).toBe('Last tool call 4 minutes ago (bash).');
    expect(goals.lastCallText(at(4), null, NOW)).toBe('Last tool call 4 minutes ago.');
    expect(goals.noCallYetText(at(3), NOW)).toBe('No tool call yet in this session. It started 3 minutes ago.');
  });

  it('the quiet note shows from ten minutes without a call, not at nine', () => {
    expect(goals.quietText(at(9), NOW)).toBe('');
    expect(goals.quietText(at(10), NOW)).toBe(
      'No tool call for 10 minutes. A long command or a coding job can take this long; a call shows here when it ends.',
    );
    expect(goals.quietText(at(42), NOW)).toMatch(/^No tool call for 42 minutes\./);
  });

  it('an ended session is one line, with its calls when the log has them', () => {
    const session = { n: 4, open: false, started_at: at(240), ended_at: at(180) };
    expect(goals.endedText(session, { total: 31, errors: 0, capped: false }, NOW)).toBe('Last session (4) ended 3 hours ago after 31 tool calls.');
    expect(goals.endedText(session, { total: 1, errors: 0, capped: false }, NOW)).toBe('Last session (4) ended 3 hours ago after 1 tool call.');
    expect(goals.endedText(session, { total: 2000, errors: 0, capped: true }, NOW)).toBe(
      'Last session (4) ended 3 hours ago after more than 2,000 tool calls.',
    );
    expect(goals.endedText(session, null, NOW)).toBe('Last session (4) ended 3 hours ago.');
  });

  it('the fixed sentences', () => {
    expect(goals.ACTIVITY_TITLE).toBe('What it is doing now');
    expect(goals.ACTIVITY_ENDED_TITLE).toBe('Last session');
    expect(goals.ACTIVITY_LOADING_TEXT).toBe('Reading what it is doing.');
    expect(goals.ACTIVITY_FAILED_TEXT).toBe('Could not read what the goal is doing.');
    expect(goals.ACTIVITY_STALE_TEXT).toBe('Could not read what the goal is doing. What is shown is from the last read.');
    expect(goals.ACTIVITY_LOG_OFF_TEXT).toBe('The tool-call log is off, so individual calls are not listed.');
    expect(goals.WORK_LINKS_TEXT).toBe('See this session on the Work screen:');
  });

  it('the two summaries', () => {
    expect([0, 1, 5].map(goals.toolsSummary)).toEqual(['None allowed', '1 allowed', '5 allowed']);
    expect([0, 1, 5].map(goals.sessionsSummary)).toEqual(['None yet', '1 so far', '5 so far']);
  });

  describe('the folds the viewer chose', () => {
    afterEach(() => {
      try {
        localStorage.clear();
      } catch {
        // a test stubbed the storage away
      }
    });

    it('a stored choice of a known section is read back, and a write keeps the others', () => {
      localStorage.clear();
      goals.writeFold('journal', false);
      goals.writeFold('tools', true);
      expect(goals.readFolds()).toEqual({ journal: false, tools: true });
      expect(JSON.parse(localStorage.getItem(goals.FOLD_KEY)!)).toEqual({ journal: false, tools: true });
    });

    it('no storage, a throwing read, a value that is not JSON or not an object, an unknown section and a value that is not a boolean give no choice', () => {
      localStorage.clear();
      expect(goals.readFolds()).toEqual({});
      for (const raw of ['{not json', '[true]', 'true', 'null', '{"journal": "yes", "nope": true, "plan": 1}']) {
        localStorage.setItem(goals.FOLD_KEY, raw);
        expect(goals.readFolds(), raw).toEqual({});
      }
      localStorage.setItem(goals.FOLD_KEY, '{"journal": "yes", "spend": false}');
      expect(goals.readFolds()).toEqual({ spend: false });
      vi.stubGlobal('localStorage', { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); } });
      expect(goals.readFolds()).toEqual({});
      vi.stubGlobal('localStorage', undefined);
      expect(goals.readFolds()).toEqual({});
    });

    it('a write that the storage refuses is dropped and nothing is thrown', () => {
      vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => { throw new Error('full'); } });
      expect(() => goals.writeFold('plan', false)).not.toThrow();
      vi.stubGlobal('localStorage', undefined);
      expect(() => goals.writeFold('plan', false)).not.toThrow();
    });

    it('the defaults: journal, plan and spend open; the rest closed', () => {
      expect(goals.FOLD_DEFAULTS).toEqual({ journal: true, plan: true, spend: true, sessions: false, charter: false, limits: false, tools: false });
    });
  });
});
