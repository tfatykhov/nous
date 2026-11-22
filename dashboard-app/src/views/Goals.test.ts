import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Goals from './Goals.svelte';
import { initRouter } from '../lib/router';
import { markupOf, parseBlocks, styleBlocks } from '../lib/testing/css';
import { COMMANDS, COMMANDS_ON, OPTIONS, PACING, activityAnswer, goalDetail, goalRow, goalToolsAnswer } from '../lib/testing/goalFixtures';
import type { Action, GoalDetail } from '../lib/goals';

// F114 goals PR 2: the Goals screen over the /goals routes. The fixtures have the shapes nous/api/goal_routes.py
// answers with; every value is made up.

type Answer = [number, unknown];
/** A status for a request that never answers: it ends only when its signal aborts. */
const HANG = -1;
type Call = { url: string; method: string; body: unknown };

let calls: Call[] = [];
let handlers: Record<string, (body: unknown) => Answer | Promise<Answer>> = {};
let details: Record<string, GoalDetail> = {};

const A = goalDetail({ id: 'a', ref: 'G-0001', title: 'Tidy the notes', state: 'active' });
const B = goalDetail({
  id: 'b', ref: 'G-0002', title: 'Sort the photos', state: 'paused', pause_reason: 'budget', spent_total: 10_000_000,
  resume_blocked: 'budget', actions: ['budget', 'deadline', 'note', 'cancel'],
});
const C = goalDetail({ id: 'c', ref: 'G-0003', title: 'Old report', state: 'done', actions: [], next_wake_at: null });

function rowOf(d: GoalDetail) {
  const keys = Object.keys(goalRow()) as (keyof ReturnType<typeof goalRow>)[];
  return Object.fromEntries(keys.map((k) => [k, d[k]]));
}

function install(goals: GoalDetail[] = [A, B, C]) {
  details = Object.fromEntries(goals.map((g) => [g.ref, g]));
  handlers['GET /goals'] = () => [200, { enabled: true, goals: Object.values(details).map(rowOf), total: Object.keys(details).length }];
  // The list as the server filters it: open by default, finished or all on request.
  for (const [filter, keep] of [['open', ['draft', 'active', 'paused']], ['finished', ['done', 'failed', 'cancelled']]] as const) {
    handlers[`GET /goals?state=${filter}`] = () => {
      const rows = Object.values(details).filter((g) => (keep as readonly string[]).includes(g.state)).map(rowOf);
      return [200, { enabled: true, goals: rows, total: rows.length }];
    };
  }
  handlers['GET /goals?state=all'] = handlers['GET /goals'];
  handlers['GET /goals/options'] = () => [200, OPTIONS()];
  handlers['GET /background/state'] = () => [404, { error: 'not found' }];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    const raw = typeof init.body === 'string' ? init.body : undefined;
    const body = raw === undefined ? undefined : JSON.parse(raw);
    calls.push({ url, method, body });
    const path = url.split('?')[0];
    const ref = decodeURIComponent(path.split('/')[2] ?? '');
    let answer: Answer | Promise<Answer>;
    if (handlers[`${method} ${url}`]) answer = handlers[`${method} ${url}`](body);
    else if (handlers[`${method} ${path}`]) answer = handlers[`${method} ${path}`](body);
    else if (method === 'GET' && path.split('/').length === 3 && ref in details) answer = [200, details[ref]];
    else if (method === 'GET' && path.split('/').length === 3) answer = [404, { error: `no goal ${ref}` }];
    // F115: a server with per-goal tools off, as the route answers then.
    else if (method === 'GET' && path.split('/').length === 4 && path.endsWith('/tools')) answer = [200, { enabled: true, grants_enabled: false }];
    else answer = [500, { error: `unexpected ${method} ${url}` }];
    const [status, payload] = await answer;
    if (status === 0) throw new TypeError('Failed to fetch');
    if (status === HANG) {
      return new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('The operation was aborted.', 'AbortError')));
      });
    }
    return new Response(payload === undefined ? '' : JSON.stringify(payload), { status });
  }));
}

/** A POST to a goal action that answers with the goal changed by `change`, and stores it for the next read. */
function acting(ref: string, action: Action, change: Partial<GoalDetail>) {
  handlers[`POST /goals/${ref}/${action}`] = () => {
    details[ref] = { ...details[ref], ...change };
    return [200, details[ref]];
  };
}

const go = (hash: string) => { location.hash = hash; initRouter(); };
const posts = () => calls.filter((c) => c.method === 'POST');
const reads = (path: string) => calls.filter((c) => c.method === 'GET' && c.url.split('?')[0] === path).length;
const pane = (title: string) => screen.findByRole('region', { name: title });
const actionButtons = (region: HTMLElement) =>
  Array.from(region.querySelectorAll<HTMLButtonElement>('button[data-action]')).map((b) => b.dataset.action);
function held(): { promise: Promise<Answer>; resolve: (a: Answer) => void } {
  let resolve!: (a: Answer) => void;
  const promise = new Promise<Answer>((r) => { resolve = r; });
  return { promise, resolve };
}

beforeEach(() => {
  sessionStorage.clear(); // the goal last shown is remembered per browser tab
  calls = [];
  handlers = {};
  install();
  go('#/goals');
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Goals: the states', () => {
  it('goals off shows the card and no form', async () => {
    handlers['GET /goals?state=open'] = () => [200, { enabled: false, goals: [] }];
    handlers['GET /goals/options'] = () => [200, { enabled: false }];
    render(Goals);
    expect(await screen.findByRole('heading', { name: 'Goals are off on this server.' })).toBeTruthy();
    expect(screen.queryByRole('navigation', { name: 'Goals' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Create draft' })).toBeNull();
  });

  it('a 403 asks for the sign-in, in words', async () => {
    handlers['GET /goals?state=open'] = () => [403, { error: 'forbidden' }];
    render(Goals);
    expect(await screen.findByText('Your sign-in is needed to see goals. Open the dashboard through the login page.')).toBeTruthy();
    expect(screen.queryByText('forbidden')).toBeNull();
  });

  it('a failed list says so', async () => {
    handlers['GET /goals?state=open'] = () => [500, { error: 'the request could not be processed' }];
    render(Goals);
    expect(await screen.findByText('Could not load the goals: The request could not be processed.')).toBeTruthy();
  });

  it('no goal yet: the list says so and the pane is the form', async () => {
    install([]);
    render(Goals);
    expect(await screen.findByText('No open goals.')).toBeTruthy();
    expect(await screen.findByRole('region', { name: 'New goal' })).toBeTruthy();
  });

  it('the list shows state in words, spend, deadline and next wake', async () => {
    render(Goals);
    const nav = await screen.findByRole('navigation', { name: 'Goals' });
    await within(nav).findByText('Tidy the notes');
    expect(within(nav).getAllByText('Approved').length).toBeGreaterThan(0); // no session is started on this server
    expect(within(nav).getByText('Paused for its budget')).toBeTruthy();
    expect(within(nav).queryByText('Old report')).toBeNull(); // finished: not in Open
    expect(within(nav).getAllByText('Today 23,000 of 2,000,000').length).toBe(2);
    expect(within(nav).getByText('In all 10,000,000 of 10,000,000 tokens')).toBeTruthy();
    expect(within(nav).getAllByText(/Deadline in \d+ days?|Deadline passed/).length).toBeGreaterThan(0);
    await fireEvent.click(within(nav).getByRole('button', { name: 'Finished' }));
    expect(await within(nav).findByText('Old report')).toBeTruthy(); // the server is asked for the finished goals
    expect(calls.some((c) => c.url === '/goals?state=finished')).toBe(true);
    expect(within(nav).getByText('Done')).toBeTruthy();
    expect(within(nav).queryByText('Tidy the notes')).toBeNull();
  });

  it('the first open goal is open, and a reload keeps the one that was open', async () => {
    render(Goals);
    await pane('Tidy the notes');
    go('#/goals/g/G-0002');
    await pane('Sort the photos');
    go('#/goals');
    await pane('Sort the photos'); // the bare address keeps the goal shown last
    expect(screen.queryByRole('region', { name: 'Tidy the notes' })).toBeNull();
  });

  it('a goal address opens it, and an unknown one says so', async () => {
    go('#/goals/g/G-0003');
    render(Goals);
    expect(await pane('Old report')).toBeTruthy();
    go('#/goals/g/G-0099');
    expect(await screen.findByText('There is no goal G-0099 here. Choose one from the list.')).toBeTruthy();
  });

  it('the pane shows charter, limits, spend, plan with evidence, sessions and journal, all as text', async () => {
    details['G-0001'] = {
      ...A,
      journal: [{ seq: 9, session_n: 2, kind: 'report', actor: 'chain', text: '<b>x</b> & more', evidence: ['<i>ref</i>'], created_at: A.updated_at }, ...A.journal],
    };
    render(Goals);
    const region = await pane('Tidy the notes');
    for (const text of [
      'Merge the duplicate notes in the notes folder', 'No two notes say the same thing', 'telegram:****4242', '10,000,000 tokens',
      '2,000,000 tokens', '1 hour', 'Contained', 'List the duplicate notes', 'Closed in session 1.', 'Merge each pair', 'Session 1',
      'Closed a step', 'Listed 14 duplicate pairs.', '<b>x</b> & more', '<i>ref</i>', 'Your note', 'Use the 2024 folder.', A.spend_note,
    ]) {
      expect(within(region).getAllByText(text, { exact: true }).length, text).toBeGreaterThan(0);
    }
    expect(region.querySelector('.journal b, .journal i')).toBeNull();
    expect(within(region).getByText('Use the 2024 folder.').closest('li')?.classList.contains('own')).toBe(true);
  });
});

describe('Goals: the actions', () => {
  it.each([
    ['draft', { state: 'draft', actions: ['approve', 'budget', 'deadline', 'note', 'cancel'] }],
    ['active', { state: 'active', actions: ['run-now', 'pause', 'stop', 'budget', 'deadline', 'note', 'cancel'] }],
    ['paused waiting', { state: 'paused', pause_reason: 'waiting_for_owner', actions: ['resume', 'confirm-done', 'budget', 'deadline', 'note', 'cancel'] }],
    ['done', { state: 'done', actions: [] }],
  ] as [string, Partial<GoalDetail>][])('the buttons are exactly the server\'s actions: %s', async (_name, over) => {
    details['G-0001'] = { ...A, ...over };
    go('#/goals/g/G-0001');
    render(Goals);
    const region = await pane('Tidy the notes');
    expect(actionButtons(region)).toEqual((over.actions ?? []).filter((a) => a !== 'note'));
    expect(region.querySelector('textarea') !== null).toBe((over.actions ?? []).includes('note'));
  });

  it('a spending action asks first, with the goal\'s numbers, and sends nothing until Yes', async () => {
    details['G-0001'] = { ...A, state: 'draft', deadline_at: null, actions: ['approve', 'budget', 'deadline', 'note', 'cancel'] };
    acting('G-0001', 'approve', { state: 'active', actions: ['run-now', 'pause', 'stop', 'budget', 'deadline', 'note', 'cancel'] });
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Approve' }));
    expect(within(region).getByText(/This marks the goal ready to start\. It can work in the background for up to 10 days and spend up to 10,000,000 tokens \(2,000,000 a day\)\./)).toBeTruthy();
    expect(posts()).toEqual([]);
    await fireEvent.click(within(region).getByRole('button', { name: 'No' }));
    expect(posts()).toEqual([]);
    expect(within(region).queryByText(/This marks the goal ready/)).toBeNull();
    await fireEvent.click(within(region).getByRole('button', { name: 'Approve' }));
    const listsBefore = reads('/goals');
    await fireEvent.click(within(region).getByRole('button', { name: 'Yes, approve' }));
    expect(await within(region).findByText('Approved.')).toBeTruthy();
    expect(posts().map((c) => c.url)).toEqual(['/goals/G-0001/approve']);
    expect(reads('/goals')).toBeGreaterThan(listsBefore);
    expect(await within(region).findByRole('button', { name: 'Pause' })).toBeTruthy();
  });

  it('with the driver running, approve says it starts the goal, and the not-started line is gone', async () => {
    handlers['GET /goals/options'] = () => [200, OPTIONS({ driver_running: true })];
    details['G-0001'] = { ...A, state: 'draft', deadline_at: null, actions: ['approve', 'cancel'] };
    render(Goals);
    const region = await pane('Tidy the notes');
    expect(screen.queryByText('Sessions are not being started on this server yet.')).toBeNull();
    await fireEvent.click(within(region).getByRole('button', { name: 'Approve' }));
    expect(within(region).getByText(/^This starts the goal\./)).toBeTruthy();
  });

  it('without the driver, the list and the pane say sessions are not started', async () => {
    render(Goals);
    const region = await pane('Tidy the notes');
    expect(screen.getAllByText('Sessions are not being started on this server yet.').length).toBe(2);
    expect(within(region).getByText('Sessions are not being started on this server yet.')).toBeTruthy();
  });

  it('a change answered with reload shows its result and reads the goal again', async () => {
    handlers['POST /goals/G-0001/pause'] = () => {
      details['G-0001'] = { ...details['G-0001'], state: 'paused', pause_reason: 'owner', actions: ['resume', 'cancel'] };
      return [200, { enabled: true, reload: true, id: 'a', ref: 'G-0001', title: A.title, state: 'paused', pause_reason: 'owner', pending_request: null }];
    };
    render(Goals);
    const region = await pane('Tidy the notes');
    const before = calls.filter((c) => c.url.startsWith('/goals/G-0001') && c.method === 'GET').length;
    await fireEvent.click(within(region).getByRole('button', { name: 'Pause' }));
    expect(await within(region).findByText('Paused: no new session starts. A session already running finishes.')).toBeTruthy();
    expect(calls.filter((c) => c.url.startsWith('/goals/G-0001') && c.method === 'GET').length).toBeGreaterThan(before);
    expect(within(region).getByText('Paused by you')).toBeTruthy();
  });

  it('pause acts at once', async () => {
    acting('G-0001', 'pause', { state: 'paused', pause_reason: 'owner', actions: ['resume', 'budget', 'deadline', 'note', 'cancel'] });
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Pause' }));
    expect(await within(region).findByText('Paused: no new session starts. A session already running finishes.')).toBeTruthy();
    expect(posts().map((c) => [c.url, c.body])).toEqual([['/goals/G-0001/pause', undefined]]);
  });

  it('stop, cancel and confirm done name what they end', async () => {
    details['G-0001'] = { ...A, open_session: 4 };
    render(Goals);
    let region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Stop now' }));
    expect(within(region).getByText('This cancels session 4, which is running, and pauses the goal. Work already done stays.')).toBeTruthy();
    await fireEvent.click(within(region).getByRole('button', { name: 'No' }));
    await fireEvent.click(within(region).getByRole('button', { name: 'Cancel goal' }));
    expect(within(region).getByText('This ends the goal for good. It cannot be reopened. Session 4, which is running, is cancelled too.')).toBeTruthy();
    cleanup();
    details['G-0001'] = { ...A, state: 'paused', pause_reason: 'waiting_for_owner', actions: ['resume', 'confirm-done', 'cancel'] };
    render(Goals);
    region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Confirm done' }));
    expect(within(region).getByText('This closes the goal as done. Nothing more runs for it.')).toBeTruthy();
    expect(posts()).toEqual([]);
  });

  it('a refusal shows the server\'s sentence in the warn colour and leaves the state', async () => {
    handlers['POST /goals/G-0001/pause'] = () => [409, { error: 'G-0001 active: changed meanwhile; nothing was done' }];
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Pause' }));
    const line = await within(region).findByText('G-0001 active: changed meanwhile; nothing was done.');
    expect(line.classList.contains('result-err')).toBe(true);
    expect(line.getAttribute('role')).toBe('alert');
    expect(within(region).getByText('Approved')).toBeTruthy();
    expect((within(region).getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('a 403 on a change asks for the sign-in, and a lost answer says to check the goal', async () => {
    handlers['POST /goals/G-0001/pause'] = () => [403, { error: 'forbidden' }];
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Pause' }));
    expect(await within(region).findByText('Your sign-in is needed to change goals. Open the dashboard through the login page.')).toBeTruthy();
    handlers['POST /goals/G-0001/pause'] = () => [0, null];
    await fireEvent.click(within(region).getByRole('button', { name: 'Pause' }));
    expect(await within(region).findByText('No answer came back. Check the goal before trying again.')).toBeTruthy();
  });

  it('a budget change sends only the field that changed', async () => {
    acting('G-0001', 'budget', { day_budget: 1_000_000 });
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Change budget' }));
    await fireEvent.input(within(region).getByLabelText('Day budget (tokens)'), { target: { value: '1000000' } });
    expect(within(region).getByText('This lowers the day budget from 2,000,000 to 1,000,000 tokens.')).toBeTruthy();
    await fireEvent.click(within(region).getByRole('button', { name: 'Yes, change the budget' }));
    await within(region).findByText('Budget changed.');
    expect(posts().map((c) => [c.url, c.body])).toEqual([['/goals/G-0001/budget', { day: 1_000_000 }]]);
  });

  it('raise budget and extend deadline send their bodies, and the sentence says raise or lower', async () => {
    acting('G-0001', 'budget', { total_budget: 20_000_000 });
    acting('G-0001', 'deadline', {});
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Change budget' }));
    await fireEvent.input(within(region).getByLabelText('Total budget (tokens)'), { target: { value: '20000000' } });
    expect(within(region).getByText('This raises the total budget from 10,000,000 to 20,000,000 tokens. The goal can spend more.')).toBeTruthy();
    await fireEvent.input(within(region).getByLabelText('Day budget (tokens)'), { target: { value: '1000000' } });
    expect(within(region).getByText(/and lowers the day budget from 2,000,000 to 1,000,000 tokens/)).toBeTruthy();
    await fireEvent.input(within(region).getByLabelText('Day budget (tokens)'), { target: { value: '2000000' } });
    await fireEvent.click(within(region).getByRole('button', { name: 'Yes, change the budget' }));
    await within(region).findByText('Budget changed.');
    await fireEvent.click(within(region).getByRole('button', { name: 'Extend deadline' }));
    const yes = within(region).getByRole('button', { name: 'Yes, extend the deadline' }) as HTMLButtonElement;
    expect(yes.disabled).toBe(true);
    await fireEvent.input(within(region).getByLabelText('More days'), { target: { value: '5' } });
    expect(within(region).getByText(/^This lets the goal keep working, and spending tokens, 5 more days, until /)).toBeTruthy();
    await fireEvent.click(yes);
    await within(region).findByText('Deadline extended.');
    expect(posts().map((c) => [c.url, c.body])).toEqual([
      ['/goals/G-0001/budget', { total: 20_000_000 }],
      ['/goals/G-0001/deadline', { days: 5 }],
    ]);
  });

  it('a note is sent, and the box is cleared only on success', async () => {
    handlers['POST /goals/G-0001/note'] = () => [409, { error: 'a note needs some text' }];
    render(Goals);
    const region = await pane('Tidy the notes');
    const box = within(region).getByLabelText('A note for the next session') as HTMLTextAreaElement;
    await fireEvent.input(box, { target: { value: 'Use the 2024 folder.' } });
    await fireEvent.click(within(region).getByRole('button', { name: 'Add note' }));
    expect(await within(region).findByText('A note needs some text.')).toBeTruthy();
    expect(box.value).toBe('Use the 2024 folder.');
    acting('G-0001', 'note', {});
    await fireEvent.click(within(region).getByRole('button', { name: 'Add note' }));
    expect(await within(region).findByText('Added. The next session reads it.')).toBeTruthy();
    expect(box.value).toBe('');
    expect(posts().map((c) => c.body)).toEqual([{ text: 'Use the 2024 folder.' }, { text: 'Use the 2024 folder.' }]);
  });

  it('a goal paused for its budget says raise the budget first, beside a Resume that cannot be pressed', async () => {
    go('#/goals/g/G-0002');
    render(Goals);
    const region = await pane('Sort the photos');
    const resume = within(region).getByRole('button', { name: 'Resume' }) as HTMLButtonElement;
    expect(resume.disabled).toBe(true);
    expect(within(region).getByText('Raise the budget first.')).toBeTruthy();
    expect(actionButtons(region)).toEqual(['budget', 'deadline', 'cancel', 'resume-blocked']);
    details['G-0002'] = { ...B, total_budget: 20_000_000, resume_blocked: null, actions: ['resume', 'budget', 'deadline', 'note', 'cancel'] };
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect((within(region).getByRole('button', { name: 'Resume' }) as HTMLButtonElement).disabled).toBe(false));
    expect(within(region).queryByText('Raise the budget first.')).toBeNull();
  });

  it('a goal waiting for the owner offers "Not done yet, keep working"; a paused goal shows its newest state sentence', async () => {
    details['G-0001'] = {
      ...A, state: 'paused', pause_reason: 'error', actions: ['resume', 'budget', 'deadline', 'note', 'cancel'],
      pause_sentence: 'Session 3 could not start: the queue was full.',
      journal: [{ seq: 30, session_n: 3, kind: 'state', actor: 'harness', text: 'Session 3 could not start: the queue was full.', evidence: [], created_at: A.updated_at }],
    };
    details['G-0002'] = { ...B, pause_reason: 'waiting_for_owner', resume_blocked: null, actions: ['resume', 'confirm-done', 'cancel'] };
    render(Goals);
    const region = await pane('Tidy the notes');
    expect(within(region).getByText('Paused after an error')).toBeTruthy();
    expect(within(region).getAllByText('Session 3 could not start: the queue was full.').length).toBe(2); // reason and journal
    expect(region.querySelector('.reason')?.textContent).toBe('Session 3 could not start: the queue was full.'); // the server's pause_sentence
    go('#/goals/g/G-0002');
    const waiting = await pane('Sort the photos');
    await fireEvent.click(within(waiting).getByRole('button', { name: 'Not done yet, keep working' }));
    expect(within(waiting).getByRole('button', { name: 'Yes, keep working' })).toBeTruthy();
  });

  it('a pending stop and an agent\'s draft are said in words', async () => {
    details['G-0001'] = { ...A, open_session: 2, pending_request: 'stop' };
    details['G-0002'] = { ...B, state: 'draft', pause_reason: null, proposed_by: 'agent', resume_blocked: null, actions: ['approve', 'cancel'] };
    render(Goals);
    const region = await pane('Tidy the notes');
    expect(within(region).getByText('A stop was asked for. It has not happened yet.')).toBeTruthy();
    expect(within(region).getByText('Approved')).toBeTruthy();
    go('#/goals/g/G-0002');
    const draft = await pane('Sort the photos');
    expect(within(draft).getByText('Nous')).toBeTruthy();
  });

  it('a draft with nowhere to ask offers no Approve, and says why', async () => {
    details['G-0001'] = { ...A, state: 'draft', origin_channel: null, deadline_at: null, actions: ['budget', 'deadline', 'note', 'cancel'] };
    render(Goals);
    const region = await pane('Tidy the notes');
    expect(within(region).queryByRole('button', { name: 'Approve' })).toBeNull();
    expect(within(region).getByText(/This goal has nowhere to ask you questions, so it cannot be approved\./)).toBeTruthy();
  });

  it('busy buttons cannot double submit', async () => {
    const hold = held();
    handlers['POST /goals/G-0001/pause'] = () => hold.promise;
    render(Goals);
    const region = await pane('Tidy the notes');
    const pause = within(region).getByRole('button', { name: 'Pause' }) as HTMLButtonElement;
    await fireEvent.click(pause);
    await waitFor(() => expect(within(region).getByRole('button', { name: 'Working…' })).toBeTruthy());
    for (const b of actionButtons(region)) expect((region.querySelector(`button[data-action="${b}"]`) as HTMLButtonElement).disabled, b).toBe(true);
    await fireEvent.click(within(region).getByRole('button', { name: 'Working…' }));
    await fireEvent.click(within(region).getByRole('button', { name: 'Stop now' }));
    expect(posts().length).toBe(1);
    hold.resolve([200, { ...A, state: 'paused', pause_reason: 'owner' }]);
    await within(region).findByText('Paused: no new session starts. A session already running finishes.');
    expect(posts().length).toBe(1);
  });

  it('the Show more button asks for the longer journal', async () => {
    const many = Array.from({ length: 50 }, (_, i) => ({ seq: 100 - i, session_n: null, kind: 'progress', actor: 'chain', text: `row ${i}`, evidence: [], created_at: A.updated_at }));
    details['G-0001'] = { ...A, journal: many };
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Show more' }));
    await waitFor(() => expect(calls.some((c) => c.url === '/goals/G-0001?journal=200')).toBe(true));
  });
});

describe('Goals: the halt', () => {
  it('the halt card shows while halted, active goals say held, and nothing shows when the state route 404s', async () => {
    handlers['GET /background/state'] = () => [200, { halted: true, source: 'owner', since: A.updated_at }];
    render(Goals);
    expect(await screen.findByText('Background work is halted. No goal starts a session until you resume it.')).toBeTruthy();
    const region = await pane('Tidy the notes');
    expect(within(region).getByText('Held by the halt')).toBeTruthy();
    expect(within(screen.getByRole('navigation', { name: 'Goals' })).getAllByText('Held by the halt').length).toBe(1);
    cleanup();
    handlers['GET /background/state'] = () => [404, { error: 'not found' }];
    render(Goals);
    await pane('Tidy the notes');
    expect(screen.queryByText(/Background work is halted/)).toBeNull();
    expect(screen.queryByText('Held by the halt')).toBeNull();
  });
});

describe('Goals: one column', () => {
  it('the list, then the pane alone with a link back', async () => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: query === '(max-width: 1040px)', media: query, addEventListener: () => {}, removeEventListener: () => {},
    })));
    render(Goals);
    await screen.findByRole('navigation', { name: 'Goals' });
    expect(screen.queryByRole('region', { name: 'Tidy the notes' })).toBeNull();
    go('#/goals/g/G-0001');
    await pane('Tidy the notes');
    expect(screen.getByRole('link', { name: 'All goals' }).getAttribute('href')).toBe('#/goals');
    await waitFor(() => expect(document.activeElement?.id).toBe('goal-pane')); // the focus follows what came into view
  });
});

describe('Goals: the source', () => {
  const read = (f: string) => markupOf(readFileSync(join(__dirname, f), 'utf-8'));
  it.each(['Goals.svelte', 'goals/GoalPane.svelte', 'goals/NewGoal.svelte', 'goals/GoalActivity.svelte', 'goals/Fold.svelte'])('%s renders text only and builds its links', (f) => {
    const src = read(f);
    expect(src).not.toMatch(/\{@html/);
    expect(src).not.toMatch(/href="#\//);
  });
});

describe('Goals: the review fixes', () => {
  it('with no session started on the server, an active goal reads "Approved" with no next-session line', async () => {
    details['G-0001'] = { ...A, next_wake_at: new Date(Date.now() + 2 * 3_600_000).toISOString() };
    render(Goals);
    const region = await pane('Tidy the notes');
    expect(within(region).getByText('Approved')).toBeTruthy();
    expect(screen.queryByText(/Next session/)).toBeNull();
    expect(screen.queryByText('Waiting for its next session')).toBeNull();
  });

  it('a confirmation puts the focus on its sentence, never on Yes, and the group is described by the sentence', async () => {
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Cancel goal' }));
    const group = within(region).getByRole('group', { name: 'Confirm: Cancel goal' });
    const sentence = document.getElementById(group.getAttribute('aria-describedby')!)!;
    expect(sentence.textContent).toMatch(/^This ends the goal for good\./);
    await waitFor(() => expect(document.activeElement).toBe(sentence));
    expect(document.activeElement?.textContent).not.toMatch(/^Yes/);
    await fireEvent.click(within(region).getByRole('button', { name: 'No' }));
    await fireEvent.click(within(region).getByRole('button', { name: 'Change budget' }));
    await waitFor(() => expect(document.activeElement).toBe(within(region).getByLabelText('Total budget (tokens)')));
  });

  it('Run now says what it spends before anything is sent', async () => {
    acting('G-0001', 'run-now', { pending_request: 'run_now' });
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Run a session now' }));
    expect(within(region).getByText("This asks for a session as soon as sessions run on this server. It spends tokens from this goal's budget.")).toBeTruthy();
    expect(posts()).toEqual([]);
    await fireEvent.click(within(region).getByRole('button', { name: 'Yes, run a session now' }));
    await within(region).findByText('Asked for a session now.');
    expect(posts().map((c) => c.url)).toEqual(['/goals/G-0001/run-now']);
  });

  it('an invalid budget or deadline is said, and Yes cannot be pressed', async () => {
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Change budget' }));
    await fireEvent.input(within(region).getByLabelText('Total budget (tokens)'), { target: { value: '2000000000' } });
    expect(within(region).getByText('The total budget must be at least 1 and at most 1,000,000,000.')).toBeTruthy();
    expect((within(region).getByRole('button', { name: 'Yes, change the budget' }) as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.input(within(region).getByLabelText('Total budget (tokens)'), { target: { value: '1000000' } });
    expect(within(region).getByText('The day budget cannot be larger than the total budget.')).toBeTruthy();
    expect((within(region).getByRole('button', { name: 'Yes, change the budget' }) as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.click(within(region).getByRole('button', { name: 'No' }));
    await fireEvent.click(within(region).getByRole('button', { name: 'Extend deadline' }));
    await fireEvent.input(within(region).getByLabelText('More days'), { target: { value: '91' } });
    expect(within(region).getByText('Type a whole number of days, 1 to 90.')).toBeTruthy();
    expect((within(region).getByRole('button', { name: 'Yes, extend the deadline' }) as HTMLButtonElement).disabled).toBe(true);
    expect(posts()).toEqual([]);
  });

  it('a failed read of the open goal keeps the last good detail and what was typed, and says so above it', async () => {
    render(Goals);
    const region = await pane('Tidy the notes');
    const box = within(region).getByLabelText('A note for the next session') as HTMLTextAreaElement;
    await fireEvent.input(box, { target: { value: 'Half a thought' } });
    handlers['GET /goals/G-0001'] = () => [500, { error: 'the request could not be processed' }];
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByText(/^Could not read G-0001 again: The request could not be processed\./)).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Tidy the notes' })).toBe(region);
    expect((within(region).getByLabelText('A note for the next session') as HTMLTextAreaElement).value).toBe('Half a thought');
    delete handlers['GET /goals/G-0001'];
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(screen.queryByText(/^Could not read G-0001 again/)).toBeNull());
  });

  it('the pane never shows a goal fetched for another address', async () => {
    const hold = held();
    render(Goals);
    await pane('Tidy the notes');
    handlers['GET /goals/G-0002'] = () => hold.promise;
    go('#/goals/g/G-0002');
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Tidy the notes' })).toBeNull());
    expect(screen.getAllByText('Loading…').length).toBeGreaterThan(0);
    hold.resolve([200, details['G-0002']]);
    expect(await pane('Sort the photos')).toBeTruthy();
  });

  it('cut lists say so in words, and the pane is named by the goal\'s title', async () => {
    const sessions = Array.from({ length: 50 }, (_, i) => ({ ...A.sessions[0], n: i + 11 }));
    const journal = Array.from({ length: 200 }, (_, i) => ({ seq: 1000 - i, session_n: null, kind: 'progress', actor: 'chain', text: `row ${i}`, evidence: [], created_at: A.updated_at }));
    details['G-0001'] = { ...A, sessions, sessions_total: 60, journal };
    render(Goals);
    const region = await pane('Tidy the notes');
    expect(within(region).getByText('Showing the newest 50 of 60.')).toBeTruthy();
    await fireEvent.click(within(region).getByRole('button', { name: 'Show more' }));
    expect(await within(region).findByText('Showing the newest 200 entries.')).toBeTruthy();
    expect(within(region).queryByRole('button', { name: 'Show more' })).toBeNull();
    expect(screen.getByRole('group', { name: 'Tidy the notes' }).id).toBe('goal-pane');
  });

  it('the options are read once, and again on the page Refresh', async () => {
    render(Goals);
    await pane('Tidy the notes');
    const reads = () => calls.filter((c) => c.url === '/goals/options').length;
    expect(reads()).toBe(1);
    acting('G-0001', 'pause', { state: 'paused', pause_reason: 'owner', actions: ['resume', 'cancel'] });
    await fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    await screen.findByText('Paused: no new session starts. A session already running finishes.');
    expect(reads()).toBe(1);
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(reads()).toBe(2));
  });

  it('the create form holds no more text than the server takes', async () => {
    go('#/goals/new');
    render(Goals);
    const form = await screen.findByRole('region', { name: 'New goal' });
    const bounds = OPTIONS().bounds!;
    expect(within(form).getByLabelText('Title').getAttribute('maxlength')).toBe(String(bounds.title));
    expect(within(form).getByLabelText('Objective').getAttribute('maxlength')).toBe(String(bounds.objective));
    expect(within(form).getByLabelText('What done looks like').getAttribute('maxlength')).toBe(String(bounds.done_when));
    expect(within(form).getByLabelText('Channel').getAttribute('maxlength')).toBe(String(bounds.channel));
  });
});

describe('Goals: the bare address keeps its goal', () => {
  it('a cancel on the bare address stays on the goal and says so', async () => {
    acting('G-0001', 'cancel', { state: 'cancelled', actions: [] });
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Cancel goal' }));
    await fireEvent.click(within(region).getByRole('button', { name: 'Yes, cancel the goal' }));
    expect(await within(region).findByText('Cancelled.')).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Tidy the notes' })).toBe(region);
    expect(within(region).getByText('Cancelled')).toBeTruthy();
  });

  it('confirm done on the bare address stays on the goal', async () => {
    install([goalDetail({ id: 'w', ref: 'G-0004', title: 'Check the backups', state: 'paused', pause_reason: 'waiting_for_owner', actions: ['resume', 'confirm-done', 'cancel'] })]);
    acting('G-0004', 'confirm-done', { state: 'done', pause_reason: null, actions: [] });
    render(Goals);
    const region = await pane('Check the backups');
    await fireEvent.click(within(region).getByRole('button', { name: 'Confirm done' }));
    await fireEvent.click(within(region).getByRole('button', { name: 'Yes, it is done' }));
    expect(await within(region).findByText('Closed as done.')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'New goal' })).toBeNull();
  });

  it('with one open goal, a cancel never turns the pane into the New goal form', async () => {
    install([A]);
    acting('G-0001', 'cancel', { state: 'cancelled', actions: [] });
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Cancel goal' }));
    await fireEvent.click(within(region).getByRole('button', { name: 'Yes, cancel the goal' }));
    await within(region).findByText('Cancelled.');
    expect(await screen.findByText('No open goals.')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'New goal' })).toBeNull();
    expect(screen.getByRole('region', { name: 'Tidy the notes' })).toBe(region);
  });

  it('a bare address that came to the form stays on it, with what was typed, when a filter lists goals', async () => {
    install([C]); // no open goal: the bare address shows the form
    render(Goals);
    const form = await screen.findByRole('region', { name: 'New goal' });
    await fireEvent.input(within(form).getByLabelText('Title'), { target: { value: 'Half a title' } });
    const nav = screen.getByRole('navigation', { name: 'Goals' });
    await fireEvent.click(within(nav).getByRole('button', { name: 'All' }));
    await within(nav).findByText('Old report');
    expect(screen.getByRole('region', { name: 'New goal' })).toBe(form);
    expect((within(form).getByLabelText('Title') as HTMLInputElement).value).toBe('Half a title');
    expect(screen.queryByRole('region', { name: 'Old report' })).toBeNull();
  });

  it('a filter switch never moves the pane or drops a typed note', async () => {
    render(Goals);
    const region = await pane('Tidy the notes');
    const box = within(region).getByLabelText('A note for the next session') as HTMLTextAreaElement;
    await fireEvent.input(box, { target: { value: 'Half a thought' } });
    const nav = screen.getByRole('navigation', { name: 'Goals' });
    await fireEvent.click(within(nav).getByRole('button', { name: 'Finished' }));
    await within(nav).findByText('Old report');
    expect(screen.getByRole('region', { name: 'Tidy the notes' })).toBe(region);
    expect(box.value).toBe('Half a thought');
    await fireEvent.click(within(nav).getByRole('button', { name: 'Open' }));
    await within(nav).findByText('Sort the photos');
    expect(box.value).toBe('Half a thought');
  });
});

describe('Goals: a filter switch', () => {
  it('says "Loading" until that filter\'s answer lands, never another filter\'s emptiness', async () => {
    const hold = held();
    render(Goals);
    const nav = await screen.findByRole('navigation', { name: 'Goals' });
    await within(nav).findByText('Tidy the notes');
    handlers['GET /goals?state=finished'] = () => hold.promise;
    await fireEvent.click(within(nav).getByRole('button', { name: 'Finished' }));
    expect(await within(nav).findByText('Loading…')).toBeTruthy();
    expect(within(nav).queryByText('No finished goals.')).toBeNull();
    expect(within(nav).queryByText('Tidy the notes')).toBeNull(); // the open list is not shown under Finished
    hold.resolve([200, { enabled: true, goals: [rowOf(C)], total: 1 }]);
    expect(await within(nav).findByText('Old report')).toBeTruthy();
  });

  it('says so when that filter\'s list could not be read', async () => {
    render(Goals);
    await pane('Tidy the notes');
    const nav = screen.getByRole('navigation', { name: 'Goals' });
    handlers['GET /goals?state=finished'] = () => [500, { error: 'the request could not be processed' }];
    await fireEvent.click(within(nav).getByRole('button', { name: 'Finished' }));
    expect(await within(nav).findByText('Could not load the list.')).toBeTruthy();
    expect(within(nav).queryByText('No finished goals.')).toBeNull();
    // the goal shown stays, and says its data is from the last read
    expect(await screen.findByText(/What is shown is from the last read\./)).toBeTruthy();
  });

  it('says how many it shows when the list is cut', async () => {
    handlers['GET /goals?state=open'] = () => [200, { enabled: true, goals: [rowOf(A), rowOf(B)], total: 7 }];
    render(Goals);
    const nav = await screen.findByRole('navigation', { name: 'Goals' });
    expect(await within(nav).findByText('Showing the newest 2 of 7.')).toBeTruthy();
  });
});

describe('Goals: confirmations and focus', () => {
  it('Escape closes a confirmation, sends nothing, and gives the focus back', async () => {
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Cancel goal' }));
    expect(within(region).getByRole('group', { name: 'Confirm: Cancel goal' })).toBeTruthy();
    await fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(within(region).queryByRole('group', { name: 'Confirm: Cancel goal' })).toBeNull());
    expect(posts()).toEqual([]);
    expect(document.activeElement).toBe(within(region).getByRole('button', { name: 'Cancel goal' }));
  });

  it('the focus goes back to the control that started a change once it has landed', async () => {
    acting('G-0001', 'budget', { total_budget: 20_000_000 });
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Change budget' }));
    await fireEvent.input(within(region).getByLabelText('Total budget (tokens)'), { target: { value: '20000000' } });
    await fireEvent.click(within(region).getByRole('button', { name: 'Yes, change the budget' }));
    await within(region).findByText('Budget changed.');
    await waitFor(() => expect(document.activeElement).toBe(within(region).getByRole('button', { name: 'Change budget' })));
  });

  it('the focused sentence of a confirmation shows a focus outline', () => {
    const css = parseBlocks(styleBlocks(readFileSync(join(__dirname, 'goals/GoalPane.svelte'), 'utf-8')));
    const rule = css.get('.confirm p:focus');
    expect(rule?.outline).toMatch(/^2px solid var\(--focus-ring\)$/);
  });
});

describe('Goals: the poll', () => {
  afterEach(() => {
    vi.useRealTimers();
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
  });

  it('pauses while the tab is hidden and reads at once when it is shown again', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(Goals);
    await pane('Tidy the notes');
    const lists = () => calls.filter((c) => c.url.startsWith('/goals?')).length;
    const before = lists();
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(lists()).toBe(before);
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.waitFor(() => expect(lists()).toBe(before + 1));
  });

  it('a read with no answer is given up, and the screen says the server could not be reached', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    handlers['GET /goals?state=open'] = () => [HANG, null];
    render(Goals);
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(screen.getByText('Could not load the goals: Could not reach the server.')).toBeTruthy());
  });
});

describe('Goals: the follow-ups', () => {
  it('a kept goal that answers 404 is dropped, said once, and the bare address shows the first open goal', async () => {
    render(Goals);
    const region = await pane('Tidy the notes');
    expect(region).toBeTruthy();
    delete details['G-0001']; // gone: its read answers 404 and the list no longer has it
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByText('No goal G-0001 here any more.')).toBeTruthy();
    expect(await pane('Sort the photos')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Tidy the notes' })).toBeNull();
    go('#/goals/g/G-0002');
    await waitFor(() => expect(screen.queryByText('No goal G-0001 here any more.')).toBeNull()); // said once
  });

  it('a goal opened by its address that answers 404 says so, without its old buttons', async () => {
    go('#/goals/g/G-0001');
    render(Goals);
    await pane('Tidy the notes');
    delete details['G-0001'];
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByText('There is no goal G-0001 here. Choose one from the list.')).toBeTruthy();
    expect(document.querySelector('button[data-action]')).toBeNull();
  });

  it('the bare address shows the goal last shown in this tab, and works when the storage refuses', async () => {
    sessionStorage.setItem('nous.goals.shown', 'G-0002');
    render(Goals);
    expect(await pane('Sort the photos')).toBeTruthy();
    cleanup();
    go('#/goals/g/G-0003');
    render(Goals);
    await pane('Old report');
    await waitFor(() => expect(sessionStorage.getItem('nous.goals.shown')).toBe('G-0003'));
    cleanup();
    const refused = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    go('#/goals');
    render(Goals);
    expect(await pane('Tidy the notes')).toBeTruthy(); // the first open goal
    expect(refused).toHaveBeenCalled();
    vi.restoreAllMocks();
  });
});

describe('Goals: a lost answer', () => {
  afterEach(() => vi.useRealTimers());

  it('of a note stays when only an unrelated row or another note appears, and the typed note stays', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    handlers['POST /goals/G-0001/note'] = () => {
      // never reached the server: what appears instead is a session's report and an older note's twin
      details['G-0001'] = { ...details['G-0001'], journal: [
        { seq: 98, session_n: 2, kind: 'report', actor: 'chain', text: 'Landed after all', evidence: [], created_at: A.updated_at },
        { seq: 97, session_n: null, kind: 'owner_note', actor: 'owner', text: 'Something else', evidence: [], created_at: A.updated_at },
        ...A.journal,
      ] };
      return [HANG, null];
    };
    render(Goals);
    const region = await pane('Tidy the notes');
    const box = within(region).getByLabelText('A note for the next session') as HTMLTextAreaElement;
    await fireEvent.input(box, { target: { value: 'Landed after all' } });
    await fireEvent.click(within(region).getByRole('button', { name: 'Add note' }));
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(within(region).getByText('No answer came back. Check the goal before trying again.')).toBeTruthy());
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await vi.advanceTimersByTimeAsync(1_000);
    expect(within(region).getByText('No answer came back. Check the goal before trying again.')).toBeTruthy();
    expect(within(region).queryByText('Added. The next session reads it.')).toBeNull();
    expect(box.value).toBe('Landed after all');
  });

  it('of a pause stays when the goal changes in another way, or is paused for another reason', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    handlers['POST /goals/G-0001/pause'] = () => {
      // never reached the server: the goal spends and journals on its own
      details['G-0001'] = { ...details['G-0001'], spent_today: 99_000, journal: [
        { seq: 98, session_n: 2, kind: 'progress', actor: 'chain', text: 'Merged two notes.', evidence: [], created_at: A.updated_at },
        ...A.journal,
      ] };
      return [HANG, null];
    };
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Pause' }));
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(within(region).getByText('No answer came back. Check the goal before trying again.')).toBeTruthy());
    expect(within(region).queryByText('Paused: no new session starts. A session already running finishes.')).toBeNull();
    // the harness pauses it for an error: a paused goal, but not the owner's pause
    details['G-0001'] = { ...details['G-0001'], state: 'paused', pause_reason: 'error', actions: ['resume', 'cancel'] };
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await vi.waitFor(() => expect(within(region).getByText('Paused after an error')).toBeTruthy());
    expect(within(region).getByText('No answer came back. Check the goal before trying again.')).toBeTruthy();
    expect(within(region).queryByText('Paused: no new session starts. A session already running finishes.')).toBeNull();
  });

  it('is replaced by the usual result once a read shows the change landed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    handlers['POST /goals/G-0001/pause'] = () => {
      details['G-0001'] = { ...details['G-0001'], state: 'paused', pause_reason: 'owner', actions: ['resume', 'cancel'] };
      return [HANG, null]; // the change is made, and its answer never comes
    };
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Pause' }));
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(within(region).getByText('Paused: no new session starts. A session already running finishes.')).toBeTruthy());
    expect(within(region).queryByText('No answer came back. Check the goal before trying again.')).toBeNull();
  });

  it('of a note is replaced once the journal shows the note, and the box is cleared', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    handlers['POST /goals/G-0001/note'] = (body) => {
      const text = (body as { text: string }).text;
      details['G-0001'] = { ...details['G-0001'], journal: [{ seq: 99, session_n: null, kind: 'owner_note', actor: 'owner', text, evidence: [], created_at: A.updated_at }, ...A.journal] };
      return [HANG, null];
    };
    render(Goals);
    const region = await pane('Tidy the notes');
    const box = within(region).getByLabelText('A note for the next session') as HTMLTextAreaElement;
    await fireEvent.input(box, { target: { value: 'Landed after all' } });
    await fireEvent.click(within(region).getByRole('button', { name: 'Add note' }));
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(within(region).getByText('Added. The next session reads it.')).toBeTruthy());
    expect(box.value).toBe('');
  });
});

describe('Goals: timeouts and the clock', () => {
  afterEach(() => vi.useRealTimers());

  it('a change with no answer is given up: it says to check the goal, and the controls work again', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    handlers['POST /goals/G-0001/pause'] = () => [HANG, null];
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Pause' }));
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(within(region).getByText('No answer came back. Check the goal before trying again.')).toBeTruthy());
    expect((within(region).getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('after the goal\'s read times out, the goal shown says it could not be read again', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(Goals);
    await pane('Tidy the notes');
    handlers['GET /goals?state=open'] = () => [HANG, null];
    handlers['GET /goals/G-0001'] = () => [HANG, null];
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(screen.getByText(/^Could not read G-0001 again: Could not reach the server\. What is shown is from the last read\./)).toBeTruthy());
  });

  it('when only the list read fails, the pane says the list could not be refreshed, and not after a good read', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(Goals);
    await pane('Tidy the notes');
    handlers['GET /goals?state=open'] = () => [HANG, null];
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(screen.getByText('Could not refresh the list. What is shown is from the last read.')).toBeTruthy());
    expect(screen.queryByText(/^Could not read G-0001 again/)).toBeNull();
    delete handlers['GET /goals?state=open'];
    install([A, B, C]);
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await vi.waitFor(() => expect(screen.queryByText('Could not refresh the list. What is shown is from the last read.')).toBeNull());
  });

  it('the "updated" badge moves on between polls', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(Goals);
    await pane('Tidy the notes');
    await vi.waitFor(() => expect(screen.getByText(/^updated [0-2]s ago$/)).toBeTruthy());
    await vi.advanceTimersByTimeAsync(7_000);
    await vi.waitFor(() => expect(screen.getByText(/^updated [7-9]s ago$/)).toBeTruthy());
  });
});

describe('Goals: how often a goal works (goal pacing)', () => {
  const NOTE = 'This applies from the next session. A session that is running now is not changed. To start a session sooner, use Run now.';
  const limits = (region: HTMLElement) => within(region).getByRole('heading', { name: 'Limits' }).parentElement as HTMLElement;

  it('the Limits card says how often only when the answer has it', async () => {
    handlers['GET /goals/options'] = () => [200, OPTIONS({ pacing: PACING() })];
    render(Goals);
    let region = await pane('Tidy the notes');
    expect(within(limits(region)).queryByText('How often')).toBeNull();
    cleanup();
    details['G-0001'] = { ...A, daily_at: '09:00' };
    render(Goals);
    region = await pane('Tidy the notes');
    expect(within(limits(region)).getByText('How often')).toBeTruthy();
    expect(within(limits(region)).getByText('Once a day, from 09:00 (America/New_York)')).toBeTruthy();
    cleanup();
    details['G-0001'] = { ...A, daily_at: null };
    render(Goals);
    region = await pane('Tidy the notes');
    expect(within(limits(region)).getByText('As often as its limits allow')).toBeTruthy();
  });

  it('Change how it works shows only when the server offers it', async () => {
    render(Goals);
    const region = await pane('Tidy the notes');
    expect(within(region).queryByRole('button', { name: 'Change how it works' })).toBeNull();
    expect(actionButtons(region)).not.toContain('sessions');
  });

  it('the form starts from the goal, says when it applies, and sends only what changed', async () => {
    handlers['GET /goals/options'] = () => [200, OPTIONS({ pacing: PACING() })];
    details['G-0001'] = { ...A, daily_at: null, actions: ['run-now', 'pause', 'stop', 'budget', 'sessions', 'deadline', 'note', 'cancel'] };
    acting('G-0001', 'sessions', { jobs_per_day: 1, daily_at: '07:30' });
    render(Goals);
    const region = await pane('Tidy the notes');
    expect(actionButtons(region)).toEqual(['run-now', 'pause', 'stop', 'budget', 'sessions', 'deadline', 'cancel']);
    await fireEvent.click(within(region).getByRole('button', { name: 'Change how it works' }));
    const box = within(region).getByRole('group', { name: 'Confirm: Change how it works' });
    expect((within(box).getByLabelText('Sessions per day') as HTMLInputElement).value).toBe('4');
    expect((within(box).getByLabelText('Shortest gap between sessions (minutes)') as HTMLInputElement).value).toBe('60');
    expect((within(box).getByLabelText('Job launches per day') as HTMLInputElement).value).toBe('2');
    expect((within(box).getByLabelText('Sessions may start in quiet hours') as HTMLInputElement).checked).toBe(false);
    expect((within(box).getByLabelText('Pause after this many sessions in a row that finish nothing') as HTMLInputElement).value).toBe('3');
    expect((within(box).getByLabelText('As often as its limits allow') as HTMLInputElement).checked).toBe(true);
    expect(within(box).queryByLabelText('Time of day')).toBeNull();
    const yes = within(box).getByRole('button', { name: 'Yes, change how it works' }) as HTMLButtonElement;
    expect(within(box).getByText('Change a setting first.')).toBeTruthy();
    expect(yes.disabled).toBe(true);
    expect(document.activeElement).toBe(within(box).getByLabelText('Sessions per day'));
    await fireEvent.input(within(box).getByLabelText('Job launches per day'), { target: { value: '1' } });
    await fireEvent.click(within(box).getByLabelText('Once a day'));
    const time = within(box).getByLabelText('Time of day') as HTMLInputElement;
    expect(time.value).toBe('09:00');
    expect(within(box).getByText('Time of day in America/New_York. It starts one session at that time each day. If that time has passed today and no session ran today, one starts at once.')).toBeTruthy();
    await fireEvent.input(time, { target: { value: '07:30' } });
    expect(within(box).getByText(NOTE)).toBeTruthy();
    expect(yes.disabled).toBe(false);
    expect(posts()).toEqual([]);
    await fireEvent.click(yes);
    expect(await within(region).findByText('Changed. It applies from the next session.')).toBeTruthy();
    expect(posts().map((c) => [c.url, c.body])).toEqual([['/goals/G-0001/sessions', { jobs_per_day: 1, daily: true, daily_at: '07:30' }]]);
  });

  it('a setting out of its range is said, and Yes cannot be pressed', async () => {
    handlers['GET /goals/options'] = () => [200, OPTIONS({ pacing: PACING() })];
    details['G-0001'] = { ...A, daily_at: '09:00', actions: ['run-now', 'pause', 'stop', 'budget', 'sessions', 'deadline', 'note', 'cancel'] };
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Change how it works' }));
    const box = within(region).getByRole('group', { name: 'Confirm: Change how it works' });
    const yes = within(box).getByRole('button', { name: 'Yes, change how it works' }) as HTMLButtonElement;
    expect((within(box).getByLabelText('Once a day') as HTMLInputElement).checked).toBe(true);
    await fireEvent.input(within(box).getByLabelText('Pause after this many sessions in a row that finish nothing'), { target: { value: '11' } });
    expect(within(box).getByText('The stall limit must be at least 1 and at most 10.')).toBeTruthy();
    expect(yes.disabled).toBe(true);
    await fireEvent.input(within(box).getByLabelText('Pause after this many sessions in a row that finish nothing'), { target: { value: '3' } });
    await fireEvent.input(within(box).getByLabelText('Shortest gap between sessions (minutes)'), { target: { value: '0' } });
    expect(within(box).getByText('The shortest gap between sessions must be at least 1 and at most 10,080 minutes.')).toBeTruthy();
    expect(yes.disabled).toBe(true);
    await fireEvent.input(within(box).getByLabelText('Shortest gap between sessions (minutes)'), { target: { value: '60' } });
    await fireEvent.click(within(box).getByLabelText('As often as its limits allow'));
    expect(yes.disabled).toBe(false);
    await fireEvent.click(within(box).getByRole('button', { name: 'No' }));
    expect(posts()).toEqual([]);
  });
});

describe('Goals: once a day, the review follow-ups', () => {
  const limits = (region: HTMLElement) => within(region).getByRole('heading', { name: 'Limits' }).parentElement as HTMLElement;
  const ACTIONS: GoalDetail['actions'] = ['run-now', 'pause', 'stop', 'budget', 'sessions', 'deadline', 'note', 'cancel'];

  it('a once-a-day goal\'s facts say why sessions per day does not count, and its form does not offer it', async () => {
    handlers['GET /goals/options'] = () => [200, OPTIONS({ pacing: PACING() })];
    details['G-0001'] = { ...A, daily_at: '09:00', actions: ACTIONS };
    render(Goals);
    const region = await pane('Tidy the notes');
    expect(within(limits(region)).getByText('One, as it works once a day')).toBeTruthy();
    await fireEvent.click(within(region).getByRole('button', { name: 'Change how it works' }));
    const box = within(region).getByRole('group', { name: 'Confirm: Change how it works' });
    expect(within(box).queryByLabelText('Sessions per day')).toBeNull();
    expect(within(box).getByText('Sessions per day does not apply: a goal that works once a day starts one session a day.')).toBeTruthy();
    await fireEvent.click(within(box).getByLabelText('As often as its limits allow'));
    expect((within(box).getByLabelText('Sessions per day') as HTMLInputElement).value).toBe('4');
    expect(within(box).queryByText(/^Sessions per day does not apply/)).toBeNull();
  });

  it('the pause sentence shows only for a lowered stall limit', async () => {
    handlers['GET /goals/options'] = () => [200, OPTIONS({ pacing: PACING() })];
    details['G-0001'] = { ...A, daily_at: null, actions: ACTIONS };
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Change how it works' }));
    const box = within(region).getByRole('group', { name: 'Confirm: Change how it works' });
    const stall = within(box).getByLabelText('Pause after this many sessions in a row that finish nothing');
    await fireEvent.input(within(box).getByLabelText('Sessions per day'), { target: { value: '2' } });
    expect(within(box).getByText('This applies from the next session. A session that is running now is not changed. To start a session sooner, use Run now.')).toBeTruthy();
    await fireEvent.input(stall, { target: { value: '5' } });
    expect(within(box).getByText('This applies from the next session. A session that is running now is not changed. To start a session sooner, use Run now.')).toBeTruthy();
    await fireEvent.input(stall, { target: { value: '2' } });
    expect(within(box).getByText('This applies from the next session. A session that is running now is not changed. To start a session sooner, use Run now. If its last 2 sessions in a row finished nothing, Nous pauses it at its next check.')).toBeTruthy();
  });

  it('the goal page\'s time says what happens when the time has already passed today', async () => {
    handlers['GET /goals/options'] = () => [200, OPTIONS({ pacing: PACING() })];
    details['G-0001'] = { ...A, daily_at: null, actions: ACTIONS };
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Change how it works' }));
    const box = within(region).getByRole('group', { name: 'Confirm: Change how it works' });
    await fireEvent.click(within(box).getByLabelText('Once a day'));
    expect(within(box).getByText('Time of day in America/New_York. It starts one session at that time each day. If that time has passed today and no session ran today, one starts at once.')).toBeTruthy();
  });
});

describe('Goals: a lost change of session settings', () => {
  afterEach(() => vi.useRealTimers());

  it('stays when a change with other values lands from elsewhere, and is replaced once the values asked for are there', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    handlers['GET /goals/options'] = () => [200, OPTIONS({ pacing: PACING() })];
    details['G-0001'] = { ...A, daily_at: null, actions: ['run-now', 'pause', 'stop', 'budget', 'sessions', 'deadline', 'note', 'cancel'] };
    const ownerRow = (seq: number, text: string) =>
      ({ seq, session_n: null, kind: 'state', actor: 'owner', text, evidence: [], created_at: A.updated_at });
    handlers['POST /goals/G-0001/sessions'] = () => {
      // another tab's change lands instead: a stall limit of 6, not the 5 asked for here
      details['G-0001'] = { ...details['G-0001'], stall_limit: 6, journal: [ownerRow(98, 'Session settings: stall limit 3 -> 6.'), ...A.journal] };
      return [HANG, null];
    };
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Change how it works' }));
    const box = within(region).getByRole('group', { name: 'Confirm: Change how it works' });
    await fireEvent.input(within(box).getByLabelText('Pause after this many sessions in a row that finish nothing'), { target: { value: '5' } });
    await fireEvent.click(within(box).getByRole('button', { name: 'Yes, change how it works' }));
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(within(region).getByText('No answer came back. Check the goal before trying again.')).toBeTruthy());
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await vi.advanceTimersByTimeAsync(1_000);
    expect(within(region).getByText('No answer came back. Check the goal before trying again.')).toBeTruthy();
    expect(within(region).queryByText('Changed. It applies from the next session.')).toBeNull();
    // the change asked for here lands after all
    details['G-0001'] = { ...details['G-0001'], stall_limit: 5, journal: [ownerRow(99, 'Session settings: stall limit 6 -> 5.'), ...details['G-0001'].journal] };
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await vi.waitFor(() => expect(within(region).getByText('Changed. It applies from the next session.')).toBeTruthy());
    expect(within(region).queryByText('No answer came back. Check the goal before trying again.')).toBeNull();
describe('Goals: the page with the activity panel off', () => {
  // Recorded from the page before the folding sections were built: with the server's options as they are (no
  // `activity`), the goal's page renders exactly these headings, cards and ids, and asks for no activity.
  it('renders the same headings, cards and ids in the same order, and reads no activity', async () => {
    go('#/goals/g/G-0001');
    render(Goals);
    const region = await pane('Tidy the notes');
    await waitFor(() => expect(reads('/goals/G-0001/tools')).toBeGreaterThan(0));
    await new Promise((r) => setTimeout(r, 0));
    expect(Array.from(region.querySelectorAll('h3')).map((h) => h.textContent?.trim())).toEqual([
      'Charter', 'Limits', 'Spend', 'Plan', 'Sessions', 'Journal',
    ]);
    expect(region.querySelectorAll('.card').length).toBe(8);
    expect(Array.from(region.querySelectorAll('[id^="goal-"]')).map((e) => e.id)).toEqual([
      'goal-h.G-0001', 'goal-a.G-0001.run-now', 'goal-a.G-0001.pause', 'goal-a.G-0001.stop', 'goal-a.G-0001.budget',
      'goal-a.G-0001.deadline', 'goal-a.G-0001.cancel', 'goal-n.G-0001',
    ]);
    expect(calls.some((c) => c.url.split('?')[0].endsWith('/activity'))).toBe(false);
    expect(region.querySelector('[id^="goal-f."]')).toBeNull();
  });

  it('with the per-goal tools, the run-any-command card and pacing on, the same holds', async () => {
    handlers['GET /goals/options'] = () => [200, OPTIONS({ pacing: PACING(), commands: COMMANDS() })];
    handlers['GET /goals/G-0001/tools'] = () => [200, goalToolsAnswer()];
    details['G-0001'] = { ...A, daily_at: null, commands: COMMANDS_ON, mode: 'commands', actions: [...A.actions, 'contained'] };
    go('#/goals/g/G-0001');
    render(Goals);
    const region = await pane('Tidy the notes');
    await within(region).findByRole('heading', { level: 3, name: 'Tools this goal may use' });
    expect(Array.from(region.querySelectorAll('h3')).map((h) => h.textContent?.trim())).toEqual([
      'May run any command', 'Charter', 'Limits', 'Tools this goal may use', 'Spend', 'Plan', 'Sessions', 'Journal',
    ]);
    expect(region.querySelectorAll('.card').length).toBe(10);
    expect(Array.from(region.querySelectorAll('[id^="goal-"]')).map((e) => e.id)).toEqual([
      'goal-h.G-0001', 'goal-a.G-0001.run-now', 'goal-a.G-0001.pause', 'goal-a.G-0001.stop', 'goal-a.G-0001.budget',
      'goal-a.G-0001.deadline', 'goal-a.G-0001.cancel', 'goal-m.G-0001', 'goal-a.G-0001.contained', 'goal-n.G-0001',
      'goal-n.G-0001',
    ]);
    expect(calls.some((c) => c.url.split('?')[0].endsWith('/activity'))).toBe(false);
    expect(region.querySelector('[id^="goal-f."]')).toBeNull();
  });
});

describe('Goals: the page with the activity panel on', () => {
  const ON = () => OPTIONS({ activity: true });
  /** Each section heading in page order: a fold's title, else the heading's text. */
  const headings = (region: HTMLElement) =>
    Array.from(region.querySelectorAll('h3')).map((h) => (h.querySelector('[id^="goal-l."]') ?? h).textContent?.trim());
  const fold = (id: string) => document.getElementById(`goal-f.G-0001.${id}`) as HTMLButtonElement | null;
  const activityReads = () => reads('/goals/G-0001/activity');

  beforeEach(() => {
    localStorage.clear();
    handlers['GET /goals/options'] = () => [200, ON()];
    handlers['GET /goals/G-0001/activity'] = () => [200, activityAnswer()];
  });
  afterEach(() => {
    vi.useRealTimers();
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
  });

  it('what it is doing comes first, then the journal and the note box, then the other sections; the run-any-command card stays under the header, unfolded', async () => {
    handlers['GET /goals/options'] = () => [200, OPTIONS({ activity: true, commands: COMMANDS() })];
    handlers['GET /goals/G-0001/tools'] = () => [200, goalToolsAnswer()];
    details['G-0001'] = { ...A, commands: COMMANDS_ON, mode: 'commands', actions: [...A.actions, 'contained'] };
    go('#/goals/g/G-0001');
    render(Goals);
    const region = await pane('Tidy the notes');
    await waitFor(() => expect(fold('tools')).not.toBeNull());
    await within(region).findByText('Working on a task: Merge the duplicate pairs');
    expect(headings(region)).toEqual([
      'May run any command', 'What it is doing now', 'Journal', 'Plan', 'Spend', 'Sessions', 'Charter', 'Limits',
      'Tools this goal may use',
    ]);
    const cards = Array.from(region.querySelectorAll('.card'));
    expect(cards[1].querySelector('h3')?.textContent).toBe('May run any command');
    expect(cards[1].querySelector('[id^="goal-f."]')).toBeNull();
    const note = region.querySelector('textarea')!;
    expect(fold('journal')!.compareDocumentPosition(note) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(note.compareDocumentPosition(fold('plan')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('the journal, plan and spend start open; sessions, charter, limits and tools closed; the summaries say how many', async () => {
    handlers['GET /goals/G-0001/tools'] = () => [200, goalToolsAnswer()];
    go('#/goals/g/G-0001');
    render(Goals);
    await pane('Tidy the notes');
    await waitFor(() => expect(fold('tools')).not.toBeNull());
    const open = Object.fromEntries(
      ['journal', 'plan', 'spend', 'sessions', 'charter', 'limits', 'tools'].map((id) => [id, fold(id)!.getAttribute('aria-expanded')]),
    );
    expect(open).toEqual({ journal: 'true', plan: 'true', spend: 'true', sessions: 'false', charter: 'false', limits: 'false', tools: 'false' });
    expect(fold('tools')!.textContent).toContain('1 allowed');
    expect(fold('sessions')!.textContent).toContain('1 so far');
    expect(fold('journal')!.textContent).toContain('Hide');
    expect(fold('charter')!.textContent).toContain('Show');
  });

  it('each section holds what its card held', async () => {
    go('#/goals/g/G-0001');
    render(Goals);
    await pane('Tidy the notes');
    const body = (id: string) => document.getElementById(`goal-b.G-0001.${id}`)!;
    for (const [id, text] of [
      ['journal', 'Use the 2024 folder.'], ['plan', 'Merge each pair'], ['spend', A.spend_note], ['sessions', 'Listed 14 duplicate pairs.'],
      ['charter', 'Merge the duplicate notes in the notes folder'], ['limits', '1 hour'],
    ]) {
      expect(within(body(id)).getAllByText(text, { exact: true }).length, id).toBeGreaterThan(0);
    }
  });

  it('with the per-goal tools off on the server there is no tools section and no button for it', async () => {
    go('#/goals/g/G-0001');
    render(Goals);
    await pane('Tidy the notes');
    await waitFor(() => expect(reads('/goals/G-0001/tools')).toBeGreaterThan(0));
    await new Promise((r) => setTimeout(r, 0));
    expect(fold('tools')).toBeNull();
    expect(screen.queryByText('Tools this goal may use')).toBeNull();
  });

  it("the viewer's choice is kept for the next time the page is shown", async () => {
    go('#/goals/g/G-0001');
    render(Goals);
    await pane('Tidy the notes');
    await fireEvent.click(fold('journal')!);
    await fireEvent.click(fold('charter')!);
    cleanup();
    render(Goals);
    await pane('Tidy the notes');
    await waitFor(() => expect(fold('journal')!.getAttribute('aria-expanded')).toBe('false'));
    expect(fold('charter')!.getAttribute('aria-expanded')).toBe('true');
  });

  it('a confirmation still opens in the header card with the journal folded', async () => {
    go('#/goals/g/G-0001');
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(fold('journal')!);
    await fireEvent.click(within(region).getByRole('button', { name: 'Change budget' }));
    const box = within(region).getByRole('group', { name: 'Confirm: Change budget' });
    expect(region.querySelector('.card')!.contains(box)).toBe(true);
    expect(posts()).toEqual([]);
  });

  it('reads what the goal is doing once per page poll while its session is open, and not while the tab is hidden', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    details['G-0001'] = { ...A, open_session: 4 };
    go('#/goals/g/G-0001');
    render(Goals);
    const region = await pane('Tidy the notes');
    await within(region).findByText('Working on a task: Merge the duplicate pairs');
    const lists = () => calls.filter((c) => c.url.startsWith('/goals?')).length;
    await vi.advanceTimersByTimeAsync(1_000);
    const [lists0, reads0] = [lists(), activityReads()];
    await vi.advanceTimersByTimeAsync(15_000);
    await vi.waitFor(() => expect(lists()).toBe(lists0 + 1));
    await vi.waitFor(() => expect(activityReads()).toBe(reads0 + 1));
    await vi.advanceTimersByTimeAsync(15_000);
    await vi.waitFor(() => expect(lists()).toBe(lists0 + 2));
    await vi.waitFor(() => expect(activityReads()).toBe(reads0 + 2));
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(lists()).toBe(lists0 + 2);
    expect(activityReads()).toBe(reads0 + 2);
  });

  it('with no session open, a page poll does not read what the goal is doing', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    handlers['GET /goals/G-0001/activity'] = () => [200, activityAnswer({ session: null, phase: 'none', current: null, running_count: 0 })];
    go('#/goals/g/G-0001');
    render(Goals);
    await pane('Tidy the notes');
    await screen.findByText('No session has run yet.');
    const lists = () => calls.filter((c) => c.url.startsWith('/goals?')).length;
    await vi.advanceTimersByTimeAsync(1_000);
    const [lists0, reads0] = [lists(), activityReads()];
    await vi.advanceTimersByTimeAsync(30_000);
    await vi.waitFor(() => expect(lists()).toBe(lists0 + 2));
    expect(activityReads()).toBe(reads0);
  });
});
