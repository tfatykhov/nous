import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Goals from './Goals.svelte';
import { initRouter } from '../lib/router';
import { markupOf } from '../lib/testing/css';
import { OPTIONS, goalDetail, goalRow } from '../lib/testing/goalFixtures';
import type { Action, GoalDetail } from '../lib/goals';

// F114 goals PR 2: the Goals screen over the /goals routes. The fixtures have the shapes nous/api/goal_routes.py
// answers with; every value is made up.

type Answer = [number, unknown];
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
  handlers['GET /goals'] = () => [200, { enabled: true, goals: Object.values(details).map(rowOf) }];
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
    else answer = [500, { error: `unexpected ${method} ${url}` }];
    const [status, payload] = await answer;
    if (status === 0) throw new TypeError('Failed to fetch');
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
  calls = [];
  handlers = {};
  install();
  go('#/goals');
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Goals: the states', () => {
  it('goals off shows the card and no form', async () => {
    handlers['GET /goals'] = () => [200, { enabled: false, goals: [] }];
    handlers['GET /goals/options'] = () => [200, { enabled: false }];
    render(Goals);
    expect(await screen.findByRole('heading', { name: 'Goals are off on this server.' })).toBeTruthy();
    expect(screen.queryByRole('navigation', { name: 'Goals' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Create draft' })).toBeNull();
  });

  it('a 403 asks for the sign-in, in words', async () => {
    handlers['GET /goals'] = () => [403, { error: 'forbidden' }];
    render(Goals);
    expect(await screen.findByText('Your sign-in is needed to see goals. Open the dashboard through the login page.')).toBeTruthy();
    expect(screen.queryByText('forbidden')).toBeNull();
  });

  it('a failed list says so', async () => {
    handlers['GET /goals'] = () => [500, { error: 'the request could not be processed' }];
    render(Goals);
    expect(await screen.findByText('Could not load the goals: The request could not be processed.')).toBeTruthy();
  });

  it('no goal yet: the list says so and the pane is the form', async () => {
    install([]);
    render(Goals);
    expect(await screen.findByText('No goals yet.')).toBeTruthy();
    expect(await screen.findByRole('region', { name: 'New goal' })).toBeTruthy();
  });

  it('the list shows state in words, spend, deadline and next wake', async () => {
    render(Goals);
    const nav = await screen.findByRole('navigation', { name: 'Goals' });
    await within(nav).findByText('Tidy the notes');
    expect(within(nav).getByText('Waiting for its next session')).toBeTruthy();
    expect(within(nav).getByText('Paused for its budget')).toBeTruthy();
    expect(within(nav).queryByText('Old report')).toBeNull(); // finished: not in Open
    expect(within(nav).getAllByText('Today 23,000 of 2,000,000').length).toBe(2);
    expect(within(nav).getByText('In all 10,000,000 of 10,000,000 tokens')).toBeTruthy();
    expect(within(nav).getAllByText(/Deadline in \d+ days?|Deadline passed/).length).toBeGreaterThan(0);
    await fireEvent.click(within(nav).getByRole('button', { name: 'Finished' }));
    expect(within(nav).getByText('Old report')).toBeTruthy();
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
    expect(within(region).getByText('Waiting for its next session')).toBeTruthy();
    expect((within(region).getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('a 403 on a change asks for the sign-in, and a network failure says nothing was changed', async () => {
    handlers['POST /goals/G-0001/pause'] = () => [403, { error: 'forbidden' }];
    render(Goals);
    const region = await pane('Tidy the notes');
    await fireEvent.click(within(region).getByRole('button', { name: 'Pause' }));
    expect(await within(region).findByText('Your sign-in is needed to change goals. Open the dashboard through the login page.')).toBeTruthy();
    handlers['POST /goals/G-0001/pause'] = () => [0, null];
    await fireEvent.click(within(region).getByRole('button', { name: 'Pause' }));
    expect(await within(region).findByText('Could not reach the server. Nothing was changed.')).toBeTruthy();
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
      journal: [{ seq: 30, session_n: 3, kind: 'state', actor: 'harness', text: 'Session 3 could not start: the queue was full.', evidence: [], created_at: A.updated_at }],
    };
    details['G-0002'] = { ...B, pause_reason: 'waiting_for_owner', resume_blocked: null, actions: ['resume', 'confirm-done', 'cancel'] };
    render(Goals);
    const region = await pane('Tidy the notes');
    expect(within(region).getByText('Paused after an error')).toBeTruthy();
    expect(within(region).getAllByText('Session 3 could not start: the queue was full.').length).toBe(2); // reason and journal
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
    expect(within(region).getByText('A stop was asked and is waiting for the driver.')).toBeTruthy();
    expect(within(region).getByText('Working')).toBeTruthy();
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
  });
});

describe('Goals: the source', () => {
  const read = (f: string) => markupOf(readFileSync(join(__dirname, f), 'utf-8'));
  it.each(['Goals.svelte', 'goals/GoalPane.svelte', 'goals/NewGoal.svelte'])('%s renders text only and builds its links', (f) => {
    const src = read(f);
    expect(src).not.toMatch(/\{@html/);
    expect(src).not.toMatch(/href="#\//);
  });
});
