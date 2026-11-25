import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Limits from '../Limits.svelte';
import { initRouter } from '../../lib/router';
import { markupOf, scriptBlocks } from '../../lib/testing/css';
import type { KindNumbers, LoadingAnswer, OnNumbers, ToolRow } from '../../lib/tool_loading';

// F120 tool loading: the third sub tab of the Limits and tools screen, over GET and PUT
// /dashboard/tool-loading. The answers have the shapes nous/api/tool_loading_routes.py gives; every value is made up.

type Answer = [number, unknown];
type Call = { url: string; method: string; body: unknown };
let calls: Call[] = [];
let handlers: Record<string, (body: unknown) => Answer> = {};

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
    calls.push({ url, method, body });
    const handler = handlers[`${method} ${url}`];
    const [status, payload] = handler ? handler(body) : [500, { error: `unexpected ${method} ${url}` }];
    return new Response(JSON.stringify(payload), { status });
  }));
}

const HOSTILE = '<img src=x onerror=alert(1)>';
const KIND_WORDS: [string, string][] = [
  ['chat', 'Chat and calls from other agents'],
  ['subtask', 'Background tasks and scheduled tasks'],
  ['workflow_step', 'Workflow steps'],
  ['heartbeat', 'Heartbeat triage, checks and their callbacks'],
  ['other_background', 'Workflow summaries, companion taps and other background turns'],
];
const numbers = (over: Partial<KindNumbers> = {}): KindNumbers[] =>
  KIND_WORDS.map(([key, words]) => ({
    key, words, on: key === 'chat', calls: 0, read_calls: 0, written_calls: 0, deferred_avg: 0, deferred_tokens_avg: 0,
    index_tokens_avg: 0, saved_per_call: null, turns: 0, turns_used_left_out: 0, calls_used_left_out: 0, on_mode: null,
    ...(key === 'chat' ? over : {}),
  }));
const inOn: OnNumbers = {
  calls: 6, applied: 5, full_avg: 12, deferred_avg: 40, read_calls: 4, written_calls: 1, saved_per_call: 900,
  turns: 2, loads_per_turn: 1.5, refusals_per_turn: 0, fallbacks: 0, not_applied: [{ why: 'turn', calls: 1 }],
};
const tool = (over: Partial<ToolRow>): ToolRow => ({
  name: 'x', server: null, registered: true, tokens_est: 100, calls_30d: 3, sessions_30d: 2, state: 'left_out',
  why: null, ...over,
});

function answer(over: Partial<LoadingAnswer> = {}): LoadingAnswer {
  return {
    enabled: true,
    mode: 'off',
    kinds: KIND_WORDS.map(([key, words]) => ({ key, words, on: false })),
    core_default: true,
    core: ['db_query'],
    pinned: [{ name: 'bash', why: 'The instructions tell the model to call it by name.' }],
    updated_at: null,
    updated_by: null,
    problem: null,
    recorded: { model_calls: true, tool_calls: true },
    deferring_client: true,
    days: 7,
    numbers: numbers({
      calls: 12, read_calls: 9, written_calls: 2, deferred_avg: 3, deferred_tokens_avg: 1200, index_tokens_avg: 200,
      saved_per_call: 140, turns: 4, turns_used_left_out: 1, calls_used_left_out: 1,
    }),
    totals: { tools: 4, kept: 2, left_out: 2, tokens_est: 900, left_out_tokens_est: 400, index_tokens_est: 120 },
    tools: [
      tool({ name: 'bash', state: 'pinned', why: 'The instructions tell the model to call it by name.' }),
      tool({ name: 'db_query', state: 'core' }),
      tool({ name: 'mcp__ha__HassTurnOn', server: 'ha', calls_30d: 0, sessions_30d: 0 }),
      tool({ name: 'mcp__gone__lookup', server: 'gone', registered: false, tokens_est: null, state: 'core' }),
      tool({ name: HOSTILE }),
    ],
    ...over,
  };
}

let current: LoadingAnswer;
const go = (hash: string) => { location.hash = hash; initRouter(); };
const sent = (method: string) => calls.filter((c) => c.method === method);
const rowOf = (name: string) => screen.getByText(name, { selector: '.name' }).closest('li') as HTMLElement;

beforeEach(() => {
  calls = [];
  handlers = {};
  current = answer();
  install();
  for (const days of [1, 7, 30]) handlers[`GET /dashboard/tool-loading?days=${days}`] = () => [200, current];
  go('#/limits/loading');
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Limits and tools: tool loading', () => {
  it('is the third sub view, a link built by the router', async () => {
    render(Limits);
    const nav = screen.getByRole('navigation', { name: 'Limits and tools views' });
    const links = within(nav).getAllByRole('link');
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      ['Limits', '#/limits'], ['Tools', '#/limits/tools'], ['Tool loading', '#/limits/loading'],
    ]);
    expect(links[2].getAttribute('aria-current')).toBe('page');
    expect(await screen.findByRole('heading', { name: 'Mode' })).toBeTruthy();
    expect(calls.map((c) => c.url)).toEqual(['/dashboard/tool-loading?days=7']);
  });

  it('with the setting off on the server it says so and shows no control', async () => {
    current = { enabled: false };
    render(Limits);
    expect(await screen.findByRole('heading', { name: 'Tool loading is off on this server.' })).toBeTruthy();
    expect(screen.getByText(/NOUS_TOOL_LOADING_ENABLED=true/)).toBeTruthy();
    expect(screen.queryByRole('radio')).toBeNull();
  });

  it('shows the mode, the kinds, the numbers per kind and the last change', async () => {
    current = answer({ updated_at: '2026-11-24T10:05:00+00:00', updated_by: 'proxy' });
    render(Limits);
    const mode = await screen.findByRole('group', { name: 'Mode' });
    expect((within(mode).getByRole('radio', { name: 'Off' }) as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText('Every tool is sent in full, as now, and nothing is recorded.')).toBeTruthy();
    for (const [, words] of KIND_WORDS) expect(screen.getAllByText(words).length).toBeGreaterThanOrEqual(2); // switch, numbers
    expect(screen.getByText(/^12 successful calls recorded\. Each would have left out 3 tools on average/)).toBeTruthy();
    expect(screen.getByText('Weighed, that is worth about 140 uncached input tokens a call (9 read the tools from the cache and 2 wrote them).')).toBeTruthy();
    expect(screen.getByText('1 of 4 turns then used a tool that would have been left out.')).toBeTruthy();
    expect(screen.getByText('Last changed from the dashboard, Nov 24 10:05 UTC.')).toBeTruthy();
  });

  it('the mode is saved when chosen, with the mode alone', async () => {
    handlers['PUT /dashboard/tool-loading'] = () => {
      current = answer({ mode: 'watch' });
      return [200, { ...current, changed: true }];
    };
    render(Limits);
    await fireEvent.click(await screen.findByRole('radio', { name: 'Watch only' }));
    const said = await screen.findByText('Saved. It applies from the next model call.');
    expect(sent('PUT')).toEqual([{ url: '/dashboard/tool-loading', method: 'PUT', body: { mode: 'watch' } }]);
    expect(screen.getByText(/^Every tool is still sent in full\./)).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(said));
  });

  it('On is a mode like the others: saved when chosen, with its words and its numbers', async () => {
    handlers['PUT /dashboard/tool-loading'] = () => {
      current = answer({ mode: 'on', numbers: numbers({ on_mode: inOn }) });
      return [200, { ...current, changed: true }];
    };
    render(Limits);
    const mode = await screen.findByRole('group', { name: 'Mode' });
    await fireEvent.click(within(mode).getByRole('radio', { name: 'On' }));
    await screen.findByText('Saved. It applies from the next model call.');
    expect(sent('PUT')).toEqual([{ url: '/dashboard/tool-loading', method: 'PUT', body: { mode: 'on' } }]);
    expect((within(mode).getByRole('radio', { name: 'On' }) as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText(/^In a kind switched on below, the tools outside the core set are left out/)).toBeTruthy();
    expect(screen.getByText('On: 6 calls recorded, 5 sent with tools left out.')).toBeTruthy();
    expect(screen.getByText('1 call sent with every tool in full: their turn began before On was chosen.')).toBeTruthy();
    expect(screen.queryByText(/^On does nothing on this server/)).toBeNull(); // this server's client can
  });

  it('On on a server whose model client cannot leave tools out says so', async () => {
    current = answer({ mode: 'on', deferring_client: false });
    render(Limits);
    expect(await screen.findByText(/^On does nothing on this server: the model client in use cannot leave tools out/)).toBeTruthy();
    current = answer({ mode: 'watch', deferring_client: false });
    cleanup();
    render(Limits);
    await screen.findByRole('group', { name: 'Mode' });
    expect(screen.queryByText(/^On does nothing on this server/)).toBeNull(); // only said in On
  });

  it('a refused mode or kind change shows the server sentence and checks the saved choice again', async () => {
    handlers['PUT /dashboard/tool-loading'] = () => [500, { error: 'The request could not be processed.' }];
    render(Limits);
    const mode = await screen.findByRole('group', { name: 'Mode' });
    const watch = within(mode).getByRole('radio', { name: 'Watch only' }) as HTMLInputElement;
    await fireEvent.click(watch);
    expect(await screen.findByText('The request could not be processed.')).toBeTruthy();
    await waitFor(() => expect((within(mode).getByRole('radio', { name: 'Off' }) as HTMLInputElement).checked).toBe(true));
    expect(watch.checked).toBe(false);
    await fireEvent.click(watch); // the same choice again is a new request
    await waitFor(() => expect(sent('PUT')).toHaveLength(2));
    await waitFor(() => expect(watch.disabled || watch.checked).toBe(false)); // that change has come back
    const steps = (await screen.findAllByRole('group', { name: 'Workflow steps' }))[0];
    const on = within(steps).getByRole('radio', { name: 'On' }) as HTMLInputElement;
    await fireEvent.click(on);
    await waitFor(() => expect(sent('PUT')).toHaveLength(3));
    await waitFor(() => expect((within(steps).getByRole('radio', { name: 'Off' }) as HTMLInputElement).checked).toBe(true));
    expect(on.checked).toBe(false);
  });

  it('a kind of call is saved when switched, with that kind alone', async () => {
    handlers['PUT /dashboard/tool-loading'] = () => [200, { ...current, changed: true }];
    render(Limits);
    const switches = (await screen.findAllByRole('group', { name: 'Workflow steps' }))[0];
    await fireEvent.click(within(switches).getByRole('radio', { name: 'On' }));
    await screen.findByText('Saved. It applies from the next model call.');
    expect(sent('PUT').map((c) => c.body)).toEqual([{ kinds: { workflow_step: true } }]);
  });

  it('the core set is edited, then saved with one Save, sorted', async () => {
    handlers['PUT /dashboard/tool-loading'] = (body) => {
      current = answer({ core: (body as { core: string[] }).core, core_default: false });
      return [200, { ...current, changed: true }];
    };
    render(Limits);
    await screen.findByRole('heading', { name: 'The core set' });
    const save = screen.getByRole('button', { name: 'Save the core set' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true); // nothing changed yet
    await fireEvent.click(within(rowOf('mcp__ha__HassTurnOn')).getByRole('radio', { name: 'In full' }));
    await fireEvent.click(within(rowOf('db_query')).getByRole('radio', { name: 'Left out' }));
    expect(screen.getByText('The core set has changes that are not saved.')).toBeTruthy();
    expect(sent('PUT')).toEqual([]); // nothing is sent before Save
    await fireEvent.click(save);
    await screen.findByText('Saved. It applies from the next model call.');
    expect(sent('PUT').map((c) => c.body)).toEqual([{ core: ['mcp__ha__HassTurnOn'] }]);
    expect(screen.queryByText('The core set has changes that are not saved.')).toBeNull();
    expect(screen.getByRole('button', { name: 'Use the default core set' })).toBeTruthy();
  });

  it('the default core set is one PUT with a null core', async () => {
    current = answer({ core_default: false, core: [] });
    handlers['PUT /dashboard/tool-loading'] = () => {
      current = answer();
      return [200, { ...current, changed: true }];
    };
    render(Limits);
    await fireEvent.click(await screen.findByRole('button', { name: 'Use the default core set' }));
    await screen.findByText('Saved. It applies from the next model call.');
    expect(sent('PUT').map((c) => c.body)).toEqual([{ core: null }]);
  });

  it('a pinned tool has its reason and no control; a stored name not registered says so', async () => {
    render(Limits);
    await screen.findByRole('heading', { name: 'The core set' });
    const bash = rowOf('bash');
    expect(within(bash).getByText('Always sent in full: The instructions tell the model to call it by name.')).toBeTruthy();
    expect(within(bash).queryByRole('radio')).toBeNull();
    const gone = rowOf('mcp__gone__lookup');
    expect(within(gone).getByText(/^Not registered now\./)).toBeTruthy();
    expect((within(gone).getByRole('radio', { name: 'In full' }) as HTMLInputElement).checked).toBe(false);
    expect(within(rowOf('mcp__ha__HassTurnOn')).getByText('From the server ha')).toBeTruthy();
    expect(within(rowOf('mcp__ha__HassTurnOn')).getByText('About 100 tokens. No calls in 30 days.')).toBeTruthy();
  });

  it('a name from the server is text, never markup', async () => {
    render(Limits);
    expect(await screen.findByText(HOSTILE)).toBeTruthy();
    expect(document.querySelector('img')).toBeNull();
  });

  it('a refused change shows the server\'s sentence', async () => {
    handlers['PUT /dashboard/tool-loading'] = () => [400, { error: 'Not registered now: mcp__ha__HassTurnOn' }];
    render(Limits);
    await screen.findByRole('heading', { name: 'The core set' });
    await fireEvent.click(within(rowOf('mcp__ha__HassTurnOn')).getByRole('radio', { name: 'In full' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Save the core set' }));
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByText('Not registered now: mcp__ha__HassTurnOn.')).toBeTruthy();
    expect(screen.getByText('The core set has changes that are not saved.')).toBeTruthy(); // the draft is kept
  });

  it('says when model calls or tool calls are not recorded, and an unreadable saved setting', async () => {
    current = answer({ recorded: { model_calls: false, tool_calls: false }, numbers: null, problem: 'the stored mode is not off or watch' });
    render(Limits);
    expect((await screen.findAllByText(/NOUS_MODEL_CALL_LOG_ENABLED/)).length).toBeGreaterThan(0);
    expect(screen.getByText(/NOUS_TOOL_CALL_LOG_ENABLED/)).toBeTruthy();
    expect(screen.getByText(/^The saved setting could not be read \(the stored mode is not off or watch\)/)).toBeTruthy();
  });

  it('numbers that could not be read say so, and the mode and the switches stay', async () => {
    current = answer({ numbers: null, numbers_failed: true, usage_failed: true });
    render(Limits);
    expect(await screen.findByText(/^The numbers could not be read just now\./)).toBeTruthy();
    expect(screen.getByText("The tools' calls could not be read just now; Refresh reads them again.")).toBeTruthy();
    expect(screen.queryByText(/NOUS_MODEL_CALL_LOG_ENABLED/)).toBeNull(); // recorded, only not read now
    const mode = screen.getByRole('group', { name: 'Mode' });
    expect(within(mode).getByRole('radio', { name: 'Watch only' })).toBeTruthy();
    expect(screen.getAllByRole('group', { name: 'Workflow steps' }).length).toBeGreaterThan(0);
  });

  it('a new window reads again with that window', async () => {
    render(Limits);
    await screen.findByRole('heading', { name: 'Mode' });
    await fireEvent.click(screen.getByRole('button', { name: 'Last 30 days' }));
    await waitFor(() => expect(calls.map((c) => c.url)).toContain('/dashboard/tool-loading?days=30'));
  });
});

describe('Limits and tools: tool loading, the source', () => {
  const src = readFileSync(join(__dirname, 'ToolLoading.svelte'), 'utf-8');
  it('no raw HTML, no hand-written link, no colour literal, no raw fetch', () => {
    expect(src).not.toMatch(/\{@html/);
    expect(markupOf(src)).not.toMatch(/href=\{?\s*["'`]#\//);
    expect(src).not.toMatch(/#[0-9a-fA-F]{3,8}\b(?![\w-])/);
    expect(scriptBlocks(src)).not.toMatch(/\bfetch\(/);
  });

  it('no shared badge; the read line, then one Refresh or Retry', () => {
    const markup = markupOf(src);
    expect(markup).not.toMatch(/<StaleBadge/);
    const head = markup.slice(markup.indexOf('<header'), markup.indexOf('</header>'));
    expect(head.indexOf('class="read-state"')).toBeGreaterThan(-1);
    expect(head.indexOf('class="read-state"')).toBeLessThan(head.indexOf('<Button'));
    expect(head.match(/<Button/g)).toHaveLength(1);
    expect(head).toMatch(/RETRY_LABEL : REFRESH_LABEL/);
  });
});
