import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import Goals from './Goals.svelte';
import { initRouter } from '../lib/router';
import { OPTIONS, goalDetail } from '../lib/testing/goalFixtures';
import type { GoalOptions } from '../lib/goals';

// F114 goals PR 2: the form that makes a draft (`#/goals/new`), over a stubbed POST /goals. Every value is made up.

type Answer = [number, unknown];
let calls: { url: string; method: string; body: unknown }[] = [];
let options: GoalOptions = OPTIONS();
let created: Answer = [201, goalDetail({ id: 'n', ref: 'G-0002', title: 'Sort the photos', state: 'draft', deadline_at: null })];
let listed: unknown[] = [];

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
    calls.push({ url, method, body });
    let answer: Answer = [500, { error: 'unexpected' }];
    if (method === 'GET' && url === '/goals') answer = [200, { enabled: true, goals: listed }];
    else if (url === '/goals/options') answer = [200, options];
    else if (url === '/background/state') answer = [404, { error: 'not found' }];
    else if (method === 'POST' && url === '/goals') {
      answer = created;
      if (created[0] === 201) listed = [created[1]];
    } else if (method === 'GET' && url.startsWith('/goals/G-0002')) answer = [200, created[1]];
    return new Response(JSON.stringify(answer[1]), { status: answer[0] });
  }));
}

const go = (hash: string) => { location.hash = hash; initRouter(); };
const posts = () => calls.filter((c) => c.method === 'POST');
async function openForm() {
  render(Goals);
  return screen.findByRole('region', { name: 'New goal' });
}
async function type(form: HTMLElement, label: string, value: string) {
  await fireEvent.input(within(form).getByLabelText(label), { target: { value } });
}
async function fillTexts(form: HTMLElement) {
  await type(form, 'Title', 'Sort the photos');
  await type(form, 'Objective', 'Put the trip photos in folders by day');
  await type(form, 'What done looks like', 'Every photo is in a folder');
}

beforeEach(() => {
  calls = [];
  options = OPTIONS();
  listed = [];
  created = [201, goalDetail({ id: 'n', ref: 'G-0002', title: 'Sort the photos', state: 'draft', deadline_at: null })];
  install();
  go('#/goals/new');
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Goals: the create form', () => {
  it('the presets come from the options, not from constants', async () => {
    options = OPTIONS({
      presets: {
        ...OPTIONS().presets!,
        small: { total_budget: 3_000_000, day_budget: 700_000, deadline_days: 4, sessions_per_day: 2, min_interval_seconds: 7200, jobs_per_day: 1, quiet_hours_ok: false },
      },
    });
    const form = await openForm();
    expect(within(form).getByText('3,000,000 tokens, 700,000 a day, 4 days')).toBeTruthy();
    expect(within(form).getByText('10,000,000 tokens, 2,000,000 a day, 10 days')).toBeTruthy();
    expect(within(form).getByText('Nothing runs until you approve it.')).toBeTruthy();
  });

  it('customise starts from the chosen preset and validates before sending', async () => {
    const form = await openForm();
    await fillTexts(form);
    await type(form, 'Channel', 'telegram:4242');
    await fireEvent.click(within(form).getByLabelText(/Medium/));
    await fireEvent.click(within(form).getByRole('button', { name: 'Customise the limits' }));
    const total = within(form).getByLabelText('Total budget (tokens)') as HTMLInputElement;
    await waitFor(() => expect(total.value).toBe('10000000'));
    expect(within(form).getByText('1 to 1,000,000,000', { selector: '#goal-new-total_budget-hint' })).toBeTruthy();
    await type(form, 'Day budget (tokens)', '20000000');
    await type(form, 'Sessions per day', '0');
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    const problems = within(form).getByRole('alert');
    expect(within(problems).getByText('Sessions per day must be at least 1 and at most 24.')).toBeTruthy();
    expect(posts()).toEqual([]);
    await type(form, 'Sessions per day', '3');
    expect(within(form).getByText('The day budget cannot be larger than the total budget.')).toBeTruthy();
    await type(form, 'Day budget (tokens)', '1000000');
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    await waitFor(() => expect(posts().length).toBe(1));
    expect(posts()[0].body).toEqual({
      title: 'Sort the photos', objective: 'Put the trip photos in folders by day', done_when: 'Every photo is in a folder',
      preset: 'custom', origin_channel: 'telegram:4242',
      limits: { total_budget: 10_000_000, day_budget: 1_000_000, deadline_days: 10, sessions_per_day: 3, min_interval_seconds: 3600, jobs_per_day: 2, quiet_hours_ok: false },
    });
  });

  it('the form requires a channel when there is no default, and not when there is', async () => {
    let form = await openForm();
    await fillTexts(form);
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    expect(within(form).getByText('Give it a channel: this server has no default chat to ask you.')).toBeTruthy();
    expect(posts()).toEqual([]);
    cleanup();
    options = OPTIONS({ has_default_channel: true });
    form = await openForm();
    expect(within(form).getByLabelText('Channel (optional)')).toBeTruthy();
    await fillTexts(form);
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    await waitFor(() => expect(posts().length).toBe(1));
    expect(posts()[0].body).toEqual({
      title: 'Sort the photos', objective: 'Put the trip photos in folders by day', done_when: 'Every photo is in a folder', preset: 'small',
    });
  });

  it('create posts and opens the new draft', async () => {
    const form = await openForm();
    await fillTexts(form);
    await type(form, 'Channel', 'telegram:4242');
    await fireEvent.click(within(form).getByLabelText(/Large/));
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    await waitFor(() => expect(location.hash).toBe('#/goals/g/G-0002'));
    expect(posts()[0].body).toMatchObject({ preset: 'large' });
    expect(await screen.findByRole('region', { name: 'Sort the photos' })).toBeTruthy();
  });

  it('a server refusal keeps the form filled and shows the sentence', async () => {
    created = [409, { error: "the origin channel must be <kind>:<id> of a kind the owner is sent to (telegram:<chat id>), not 'telegram:'" }];
    const form = await openForm();
    await fillTexts(form);
    await type(form, 'Channel', 'telegram:4242');
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    expect(await within(form).findByText(/^The origin channel must be/)).toBeTruthy();
    expect((within(form).getByLabelText('Title') as HTMLInputElement).value).toBe('Sort the photos');
    expect((within(form).getByLabelText('Channel') as HTMLInputElement).value).toBe('telegram:4242');
    expect(location.hash).toBe('#/goals/new');
  });
});
