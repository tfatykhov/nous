import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import Goals from './Goals.svelte';
import { initRouter } from '../lib/router';
import { COMMANDS, COMMANDS_ON, DESCRIPTION, OPTIONS, goalDetail, goalRow } from '../lib/testing/goalFixtures';
import { commandsSentence, confirmText, type GoalDetail, type GoalOptions } from '../lib/goals';

// A goal that may run any command, on the Goals screen: the create form's part only when the server sends it, the
// sentence from the server's template typed exactly before Create can be pressed, the body POST /goals takes, the
// goal page's card in the server's words with Return to contained (no question first), and the approve question that
// names the hours. Every value is made up.

type Answer = [number, unknown];
type Call = { url: string; method: string; body: unknown };
let calls: Call[] = [];
let options: GoalOptions = OPTIONS({ commands: COMMANDS() });
let details: Record<string, GoalDetail> = {};
let created: Answer = [201, {}];
let contained: Answer = [200, {}];

function rowOf(d: GoalDetail) {
  const keys = Object.keys(goalRow()) as (keyof ReturnType<typeof goalRow>)[];
  return Object.fromEntries(keys.map((k) => [k, d[k]]));
}

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    const body = typeof init.body === 'string' && init.body ? JSON.parse(init.body) : undefined;
    calls.push({ url, method, body });
    const path = url.split('?')[0];
    const ref = decodeURIComponent(path.split('/')[2] ?? '');
    let answer: Answer = [500, { error: `unexpected ${method} ${url}` }];
    if (method === 'GET' && path === '/goals') answer = [200, { enabled: true, goals: Object.values(details).map(rowOf), total: Object.keys(details).length }];
    else if (path === '/goals/options') answer = [200, options];
    else if (path === '/background/state') answer = [404, { error: 'not found' }];
    else if (method === 'POST' && path === '/goals') answer = created;
    else if (method === 'POST' && path.endsWith('/contained')) answer = contained;
    else if (method === 'GET' && path.split('/').length === 3 && ref in details) answer = [200, details[ref]];
    else if (method === 'GET' && path.endsWith('/tools')) answer = [200, { enabled: true, grants_enabled: false }];
    return new Response(JSON.stringify(answer[1]), { status: answer[0] });
  }));
}

const go = (hash: string) => { location.hash = hash; initRouter(); };
const posts = () => calls.filter((c) => c.method === 'POST');

beforeEach(() => {
  sessionStorage.clear();
  calls = [];
  options = OPTIONS({ commands: COMMANDS() });
  details = {};
  created = [201, goalDetail({ id: 'n', ref: 'G-0002', title: 'Ship the fix', state: 'draft', deadline_at: null })];
  contained = [200, {}];
  install();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function openForm() {
  go('#/goals/new');
  render(Goals);
  return screen.findByRole('region', { name: 'New goal' });
}

async function fill(form: HTMLElement) {
  await fireEvent.input(within(form).getByLabelText('Title'), { target: { value: 'Ship the fix' } });
  await fireEvent.input(within(form).getByLabelText('Objective'), { target: { value: 'Push the fix to a branch' } });
  await fireEvent.input(within(form).getByLabelText('What done looks like'), { target: { value: 'Pushed' } });
  await fireEvent.input(within(form).getByLabelText('Channel'), { target: { value: 'telegram:4242' } });
}

describe('the create form', () => {
  it('has no part for running any command when the server sends none', async () => {
    options = OPTIONS();
    const form = await openForm();
    expect(within(form).queryByText(COMMANDS().words.checkbox)).toBeNull();
    expect(form.textContent).not.toContain('run any command');
    expect(form.querySelector('#goal-new-commands')).toBeNull();
    expect(within(form).queryAllByRole('checkbox')).toHaveLength(0);
  });

  it('shows the server words, the sentence from its template, and keeps Create off until it is typed exactly', async () => {
    const form = await openForm();
    await fill(form);
    const submit = within(form).getByRole('button', { name: 'Create draft' }) as HTMLButtonElement;
    expect(submit.disabled).toBe(false);
    await fireEvent.click(within(form).getByRole('checkbox', { name: COMMANDS().words.checkbox }));
    expect(within(form).getByText(DESCRIPTION)).toBeTruthy();
    expect((within(form).getByLabelText('Hours from approval') as HTMLInputElement).value).toBe('24');
    expect(within(form).getByText('allow this goal to run any command for 24 hours')).toBeTruthy();
    expect(within(form).getByText('1 to 24')).toBeTruthy();
    expect(submit.disabled).toBe(true);
    const typed = within(form).getByLabelText('Type this sentence to allow it');
    await fireEvent.input(typed, { target: { value: 'Allow this goal to run any command for 24 hours' } });
    expect(submit.disabled).toBe(true); // the case counts
    await fireEvent.input(within(form).getByLabelText('Hours from approval'), { target: { value: '1' } });
    expect(within(form).getByText('allow this goal to run any command for 1 hour')).toBeTruthy();
    await fireEvent.input(typed, { target: { value: 'allow this goal to run any command for 1 hour' } });
    expect(submit.disabled).toBe(false);
    await fireEvent.click(submit);
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0].body).toEqual({
      title: 'Ship the fix',
      objective: 'Push the fix to a branch',
      done_when: 'Pushed',
      preset: 'small',
      origin_channel: 'telegram:4242',
      commands: { hours: 1, confirm: 'allow this goal to run any command for 1 hour' },
    });
  });

  it('bounds the hours by the goal deadline and by the server ceiling, in the server words', async () => {
    const refusals = { hours_range: 'From 1 to {n} hours here.', confirm: 'The sentence differs.' };
    options = OPTIONS({ commands: COMMANDS({ max_hours: 2160, refusals }) });
    const form = await openForm();
    await fireEvent.click(within(form).getByRole('checkbox', { name: COMMANDS().words.checkbox }));
    expect(within(form).getByText('1 to 72')).toBeTruthy(); // small: 3 days
    expect(within(form).getByText('The sentence differs.')).toBeTruthy();
    await fireEvent.input(within(form).getByLabelText('Hours from approval'), { target: { value: '73' } });
    expect(within(form).getByText('From 1 to 72 hours here.')).toBeTruthy();
  });

  it('leaves the part out of the body when it is not chosen', async () => {
    const form = await openForm();
    await fill(form);
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0].body).not.toHaveProperty('commands');
  });

  it('shows a refusal in the server words', async () => {
    created = [409, { error: 'Another goal may run any command already. Only one can at a time.' }];
    const form = await openForm();
    await fill(form);
    await fireEvent.click(within(form).getByRole('checkbox', { name: COMMANDS().words.checkbox }));
    await fireEvent.input(within(form).getByLabelText('Type this sentence to allow it'), {
      target: { value: 'allow this goal to run any command for 24 hours' },
    });
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    expect(await within(form).findByText('Another goal may run any command already. Only one can at a time.')).toBeTruthy();
  });

  it('builds the sentence exactly as the server does', () => {
    const words = COMMANDS();
    expect(commandsSentence(words, 1)).toBe('allow this goal to run any command for 1 hour');
    expect(commandsSentence(words, 24)).toBe('allow this goal to run any command for 24 hours');
  });
});

describe('the goal page', () => {
  const show = async (goal: GoalDetail) => {
    details = { [goal.ref]: goal };
    go(`#/goals/${goal.ref}`);
    render(Goals);
    return screen.findByRole('heading', { name: goal.title });
  };

  it('a contained goal on a server with the mode off has no card', async () => {
    options = OPTIONS();
    await show(goalDetail({ ref: 'G-0001' }));
    expect(screen.queryByRole('heading', { name: 'May run any command' })).toBeNull();
    expect(screen.getByText('Contained')).toBeTruthy();
  });

  it('a goal in the mode shows the server words and a Return to contained that asks nothing', async () => {
    const goal = goalDetail({
      ref: 'G-0003', title: 'Ship the fix', mode: 'commands', mode_hours: 8, mode_until: '2026-11-15T20:00:00+00:00',
      commands: COMMANDS_ON, actions: ['pause', 'stop', 'note', 'cancel', 'contained'],
    });
    await show(goal);
    const card = screen.getByRole('heading', { name: 'May run any command' }).closest('.card') as HTMLElement;
    expect(within(card).getByText('On until 2026-11-15 20:00 UTC.')).toBeTruthy();
    expect(within(card).getByText(DESCRIPTION)).toBeTruthy();
    expect(within(card).getByText(COMMANDS().words.stops)).toBeTruthy();
    expect(within(card).getByText(`${COMMANDS().words.return_note} ${COMMANDS().words.return_stop}`)).toBeTruthy();
    // Not among the other actions.
    const head = screen.getByRole('heading', { name: 'Ship the fix' }).closest('.card') as HTMLElement;
    expect(within(head).queryByRole('button', { name: 'Return to contained' })).toBeNull();
    contained = [200, { ...goal, mode: 'contained', mode_hours: null, mode_until: null, actions: ['pause', 'stop', 'note', 'cancel'],
      commands: { state: 'ended', says: 'Ended: Returned to contained.', usable: false, not_usable_why: null, approve: null } }];
    details[goal.ref] = contained[1] as GoalDetail;
    await fireEvent.click(within(card).getByRole('button', { name: 'Return to contained' }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0].url).toBe('/goals/G-0003/contained');
    expect(posts()[0].body).toBeUndefined();
    expect(await screen.findByText('Ended: Returned to contained.')).toBeTruthy();
    expect(await screen.findByText('Returned to contained.')).toBeTruthy();
  });

  it('says why the tasks do not run any command now', async () => {
    await show(goalDetail({
      ref: 'G-0004', title: 'Held', mode: 'commands', mode_hours: 8, mode_until: '2026-11-15T20:00:00+00:00',
      commands: { ...COMMANDS_ON, usable: false, not_usable_why: 'bash is locked by the server.' },
      actions: ['contained'],
    }));
    expect(screen.getByText('bash is locked by the server.')).toBeTruthy();
  });

  it('an armed draft says it is set, and the approve question names the hours', async () => {
    const goal = goalDetail({
      ref: 'G-0005', title: 'Armed', state: 'draft', deadline_at: null, approved_at: null, mode: 'commands', mode_hours: 5,
      commands: {
        state: 'set', says: 'Set: 5 hours from approval.', usable: false,
        not_usable_why: 'The goal is a draft: its time starts when you approve it.',
        approve: "Approving starts its time: this goal's tasks may then run any command for 5 hours.",
      },
      actions: ['approve', 'budget', 'deadline', 'note', 'cancel', 'contained'],
    });
    await show(goal);
    expect(screen.getByText('Set: 5 hours from approval.')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    expect(await screen.findByText(confirmText('approve', goal, { driverRunning: false }))).toBeTruthy();
    expect(confirmText('approve', goal, { driverRunning: false })).toContain("this goal's tasks may then run any command for 5 hours.");
  });

  it('a goal without Stop now (an armed draft) has the note under Return to contained without naming Stop now', async () => {
    const { return_note: note, return_stop: stop } = COMMANDS().words;
    expect(stop).toBe('Use Stop now for the session.');
    await show(goalDetail({
      ref: 'G-0007', title: 'Armed draft', state: 'draft', deadline_at: null, approved_at: null, mode: 'commands',
      mode_hours: 5, actions: ['approve', 'note', 'cancel', 'contained'],
      commands: { state: 'set', says: 'Set: 5 hours from approval.', usable: false, not_usable_why: null, approve: null },
    }));
    const shown = screen.getByText(note);
    expect(shown.textContent).toBe(note);
    expect(screen.queryByText(stop, { exact: false })).toBeNull();
    expect(screen.getByRole('button', { name: 'Return to contained' }).getAttribute('aria-describedby')).toBe(shown.id);
  });

  it('a goal where Stop now is offered names it after the note', async () => {
    const { return_note: note, return_stop: stop } = COMMANDS().words;
    await show(goalDetail({
      ref: 'G-0008', title: 'Running', mode: 'commands', mode_hours: 8, mode_until: '2026-11-15T20:00:00+00:00',
      commands: COMMANDS_ON, actions: ['pause', 'stop', 'note', 'cancel', 'contained'],
    }));
    const shown = screen.getByText(`${note} ${stop}`);
    expect(screen.getByRole('button', { name: 'Return to contained' }).getAttribute('aria-describedby')).toBe(shown.id);
  });

  it('an ended mode says why, with the server words', async () => {
    await show(goalDetail({
      ref: 'G-0006', title: 'Ended',
      commands: { state: 'ended', says: 'Ended: Its end time passed.', usable: false, not_usable_why: null, approve: null },
    }));
    expect(screen.getByText('Ended: Its end time passed.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Return to contained' })).toBeNull();
  });
});
