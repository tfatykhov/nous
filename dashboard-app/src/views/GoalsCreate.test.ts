import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import Goals from './Goals.svelte';
import { initRouter } from '../lib/router';
import { LEAD, NOTES_IDENTITY, OPTIONS, PACING, goalDetail, newGoalToolsAnswer } from '../lib/testing/goalFixtures';
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
    if (method === 'GET' && url.startsWith('/goals?state=')) answer = [200, { enabled: true, goals: listed, total: listed.length }];
    else if (url === '/goals/options') answer = [200, options];
    else if (url === '/background/state') answer = [404, { error: 'not found' }];
    else if (method === 'POST' && url === '/goals') {
      answer = created;
      if (created[0] === 201) listed = [created[1]];
    } else if (method === 'GET' && url.startsWith('/goals/G-0002')) answer = [200, created[1]];
    if (answer[0] === -1) {
      // never answers: it ends only when its signal aborts
      return new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('The operation was aborted.', 'AbortError')));
      });
    }
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
  sessionStorage.clear(); // the goal last shown is remembered per browser tab
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

describe('Goals: the create form and a lost answer', () => {
  afterEach(() => vi.useRealTimers());

  it('a create with no answer is given up after 15 s, and the form keeps what was typed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    created = [-1, null];
    const form = await openForm();
    await fillTexts(form);
    await type(form, 'Channel', 'telegram:4242');
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    await vi.advanceTimersByTimeAsync(16_000);
    await vi.waitFor(() => expect(within(form).getByText('No answer came back. Check the goal before trying again.')).toBeTruthy());
    expect((within(form).getByLabelText('Title') as HTMLInputElement).value).toBe('Sort the photos');
    expect((within(form).getByRole('button', { name: 'Create draft' }) as HTMLButtonElement).disabled).toBe(false);
  });
});

describe('Goals: the create form and the tools a new goal may use (F115)', () => {
  it('with the grants off there is no tools section and the body has no tools', async () => {
    const form = await openForm();
    expect(within(form).queryByText('Tools this goal may use')).toBeNull();
    await fillTexts(form);
    await type(form, 'Channel', 'telegram:4242');
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    await waitFor(() => expect(posts().length).toBe(1));
    expect('tools' in (posts()[0].body as object)).toBe(false);
  });

  it('shows the same section after the size, asks once for the lot, then sends each chosen tool', async () => {
    options = OPTIONS({ tools: newGoalToolsAnswer() });
    const form = await openForm();
    const section = within(form).getByText('Tools this goal may use').closest('[data-section="goal-tools"]') as HTMLElement;
    expect(form.innerHTML.indexOf('goal-new-preset-small')).toBeLessThan(form.innerHTML.indexOf('data-section="goal-tools"'));
    expect(within(section).getByText(LEAD)).toBeTruthy();
    expect(within(section).getByText(/^send_file has no Telegram chat set/)).toBeTruthy();
    expect(section.querySelector('[id="tool-c.send_file"]')).toBeNull(); // a blocked tool has no box
    await fireEvent.click(section.querySelector('[id="tool-c.send_email"]') as HTMLElement);
    await fireEvent.click(section.querySelector('[id="tool-c.mcp__notes__search"]') as HTMLElement);
    await fillTexts(form);
    await type(form, 'Channel', 'telegram:4242');
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    expect(posts()).toEqual([]);
    const ask = within(form).getByRole('group', { name: 'Confirm: the tools of the new goal' });
    expect(within(ask).getByText(/^Allow this goal to use these tools without asking you each time/)).toBeTruthy();
    expect(within(ask).getByText(/Send email, with files this goal's work wrote, to the addresses on your email allowlist/)).toBeTruthy();
    expect(within(ask).getByText(/Use search on notes \(notes\.example\.com\)/)).toBeTruthy();
    await fireEvent.click(within(ask).getByRole('button', { name: 'Yes, create' }));
    await waitFor(() => expect(posts().length).toBe(1));
    expect((posts()[0].body as { tools: unknown }).tools).toEqual([
      { tool: 'send_email', confirm: true },
      { tool: 'mcp__notes__search', confirm: true, identity: NOTES_IDENTITY },
    ]);
  });

  it('No closes the question and sends nothing; a refusal is the server sentence as sent', async () => {
    options = OPTIONS({ tools: newGoalToolsAnswer() });
    const sentence = 'send_email is switched off for background work. Set it to Allowed under Limits and tools first.';
    created = [409, { error: sentence, tool: 'send_email' }];
    const form = await openForm();
    await fireEvent.click(form.querySelector('[id="tool-c.send_email"]') as HTMLElement);
    await fillTexts(form);
    await type(form, 'Channel', 'telegram:4242');
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    await fireEvent.click(within(form).getByRole('button', { name: 'No' }));
    expect(posts()).toEqual([]);
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    await fireEvent.click(within(form).getByRole('button', { name: 'Yes, create' }));
    const line = await within(form).findByRole('alert');
    expect(line.textContent).toBe(sentence);
  });

  it('without a chosen tool, Create asks nothing and sends no tools', async () => {
    options = OPTIONS({ tools: newGoalToolsAnswer() });
    const form = await openForm();
    await fillTexts(form);
    await type(form, 'Channel', 'telegram:4242');
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    await waitFor(() => expect(posts().length).toBe(1));
    expect('tools' in (posts()[0].body as object)).toBe(false);
  });
});

describe('Goals: the create form and how often a goal works (goal pacing)', () => {
  it('without pacing there is no choice and the body has no time', async () => {
    const form = await openForm();
    expect(within(form).queryByText('How often should it work?')).toBeNull();
    await fillTexts(form);
    await type(form, 'Channel', 'telegram:4242');
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    await waitFor(() => expect(posts().length).toBe(1));
    expect('daily_at' in (posts()[0].body as object)).toBe(false);
  });

  it('with pacing: after the sizes, as often as the size allows by default, and no time sent then', async () => {
    options = OPTIONS({ pacing: PACING() });
    const form = await openForm();
    const group = within(form).getByRole('group', { name: 'How often should it work?' });
    expect(form.innerHTML.indexOf('goal-new-preset-small')).toBeLessThan(form.innerHTML.indexOf('goal-new-often'));
    expect((within(group).getByLabelText('As often as its size allows') as HTMLInputElement).checked).toBe(true);
    expect(within(group).queryByLabelText('Time of day')).toBeNull();
    await fillTexts(form);
    await type(form, 'Channel', 'telegram:4242');
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    await waitFor(() => expect(posts().length).toBe(1));
    expect('daily_at' in (posts()[0].body as object)).toBe(false);
  });

  it('once a day starts at 09:00, names the zone, and sends the time chosen', async () => {
    options = OPTIONS({ pacing: PACING() });
    const form = await openForm();
    const group = within(form).getByRole('group', { name: 'How often should it work?' });
    await fireEvent.click(within(group).getByLabelText('Once a day'));
    const time = within(group).getByLabelText('Time of day') as HTMLInputElement;
    expect(time.value).toBe('09:00');
    expect(within(group).getByText('Time of day in America/New_York. It starts one session at that time each day. Approved after that time, it starts its first session at once.')).toBeTruthy();
    await fireEvent.input(time, { target: { value: '06:45' } });
    await fillTexts(form);
    await type(form, 'Channel', 'telegram:4242');
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    await waitFor(() => expect(posts().length).toBe(1));
    expect(posts()[0].body).toMatchObject({ preset: 'small', daily_at: '06:45' });
  });

  it('an empty time is said before anything is sent', async () => {
    options = OPTIONS({ pacing: PACING() });
    const form = await openForm();
    await fireEvent.click(within(form).getByLabelText('Once a day'));
    await fireEvent.input(within(form).getByLabelText('Time of day'), { target: { value: '' } });
    await fillTexts(form);
    await type(form, 'Channel', 'telegram:4242');
    await fireEvent.click(within(form).getByRole('button', { name: 'Create draft' }));
    expect(within(within(form).getByRole('alert')).getByText('Type the time of day as HH:MM, from 00:00 to 23:59.')).toBeTruthy();
    expect(posts()).toEqual([]);
  });
});

describe('Goals: the create form, once a day, the review follow-ups', () => {
  it('custom limits keep sessions per day but do not let it be typed for a once-a-day goal, and say why', async () => {
    options = OPTIONS({ pacing: PACING() });
    const form = await openForm();
    await fireEvent.click(within(form).getByRole('button', { name: 'Customise the limits' }));
    const perDay = within(form).getByLabelText('Sessions per day') as HTMLInputElement;
    expect(perDay.disabled).toBe(false);
    await fireEvent.click(within(form).getByLabelText('Once a day'));
    expect(perDay.disabled).toBe(true);
    expect(within(form).getByText('Sessions per day does not apply: a goal that works once a day starts one session a day.')).toBeTruthy();
    await fireEvent.click(within(form).getByLabelText('As often as its size allows'));
    expect(perDay.disabled).toBe(false);
  });
});
