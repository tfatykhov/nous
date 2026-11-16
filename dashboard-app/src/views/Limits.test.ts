import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Limits from './Limits.svelte';
import { initRouter } from '../lib/router';
import { markupOf, scriptBlocks } from '../lib/testing/css';
import { NOT_LOADED_MESSAGE, TURNS, change, limitRow, limitsAnswer, toolRow, toolsAnswer } from '../lib/testing/autonomyFixtures';
import type { LimitRow, ToolRow } from '../lib/autonomy_settings';

// F115: the Limits and tools screen over the /autonomy routes. The fixtures have the shapes
// nous/api/autonomy_routes.py answers with; every value is made up.

type Answer = [number, unknown];
type Call = { url: string; method: string; body: unknown; type: string | null };
let calls: Call[] = [];
let handlers: Record<string, (body: unknown) => Answer> = {};

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    const raw = typeof init.body === 'string' ? init.body : undefined;
    const body = raw === undefined ? undefined : JSON.parse(raw);
    const type = (init.headers as Record<string, string> | undefined)?.['Content-Type'] ?? null;
    calls.push({ url, method, body, type });
    const handler = handlers[`${method} ${url}`];
    const [status, payload] = handler ? handler(body) : [500, { error: `unexpected ${method} ${url}` }];
    if (status === 0) throw new TypeError('Failed to fetch');
    return new Response(payload === undefined ? '' : JSON.stringify(payload), { status });
  }));
}

let limits: LimitRow[] = [];
let tools: ToolRow[] = [];
function serve(over: { limits?: LimitRow[]; tools?: ToolRow[] } = {}) {
  limits = over.limits ?? [TURNS(), limitRow()];
  tools = over.tools ?? [
    toolRow(),
    toolRow({ name: 'bash', summary: 'Run a shell command.', group: 'writing', classes: ['Changes things in Nous'] }),
    toolRow({
      name: 'send_email', summary: 'Send an email.', group: 'reaching_outside', classes: ['Reaches outside Nous'],
      setting: 'denied', source: 'server', locked: true,
      not_offered: [
        { place: 'chain_decisions', why: 'A chain keeps to a fixed set of tools that act inside Nous.' },
        { place: 'chain_work', why: 'A chain keeps to a fixed set of tools that act inside Nous.' },
      ],
    }),
  ];
  handlers['GET /autonomy/limits'] = () => [200, limitsAnswer(limits)];
  handlers['GET /autonomy/log?kind=limits'] = () => [200, { enabled: true, changes: [change()] }];
  handlers['GET /autonomy/log?kind=tools'] = () =>
    [200, { enabled: true, changes: [change({ key: 'tool_guard:background:web_search', label: null, tool: 'web_search', new: 'denied', actor: 'owner' })] }];
  handlers['GET /autonomy/tools'] = () => [200, toolsAnswer(tools)];
}

const go = (hash: string) => { location.hash = hash; initRouter(); };
const sent = (method: string) => calls.filter((c) => c.method === method);
const rowOf = (label: string) => screen.getByLabelText(label).closest('li') as HTMLElement;

beforeEach(() => {
  calls = [];
  handlers = {};
  install();
  serve();
  go('#/limits');
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Limits and tools: the limits', () => {
  it('shows the card, and each row what it limits, its range, where its value comes from and when it applies', async () => {
    render(Limits);
    expect(screen.getByRole('heading', { level: 1, name: 'Limits and tools' })).toBeTruthy();
    expect(screen.getByText(/You can set each limit within the range shown, and each change is recorded\./)).toBeTruthy();
    expect(screen.queryByText(/always win/)).toBeNull();
    const input = (await screen.findByLabelText('Tool calls per background task')) as HTMLInputElement;
    expect(input.value).toBe('50');
    const row = rowOf('Tool calls per background task');
    expect(within(row).getByText('From 1 to 200 tool calls')).toBeTruthy();
    expect(within(row).getByText(/^How many tool calls one run of a background task may make/)).toBeTruthy();
    expect(within(row).getByText("Server's value: 50.")).toBeTruthy();
    expect(within(row).getByText('From the next task that starts.')).toBeTruthy();
    expect(within(row).getByText('tool calls')).toBeTruthy(); // the unit beside the input
    expect(within(rowOf('Rounds of tool use per turn')).getByText(/in chat and in background work alike\./)).toBeTruthy();
    expect(screen.getByText('No host ceiling set.')).toBeTruthy();
    // no "Use the host's value" while the value is the host's
    expect(within(row).queryByRole('button', { name: /Use the host's value/ })).toBeNull();
    // the last changes of the limits, in words, with who made them
    expect(screen.getByText('Tool calls per background task: host value to 80, from the dashboard, Nov 16 14:31 UTC')).toBeTruthy();
    expect(calls.map((c) => c.url)).toContain('/autonomy/log?kind=limits');
    expect(calls.map((c) => c.url)).not.toContain('/autonomy/log?kind=tools');
  });

  it('a lowered value, a ceiling and a host value equal to its ceiling say so once', async () => {
    serve({
      limits: [
        TURNS({ host_value: 600, baseline: 300, value: 300, ceiling: 300, source: 'clamped' }),
        limitRow({ host_value: 60, baseline: 60, value: 60, ceiling: 60 }),
      ],
    });
    handlers['GET /autonomy/limits'] = () => [200, limitsAnswer(limits, { ceilings_set: true })];
    render(Limits);
    await screen.findByLabelText('Tool calls per background task');
    expect(within(rowOf('Rounds of tool use per turn')).getByText("Server's value 600, lowered to its ceiling of 300.")).toBeTruthy();
    expect(within(rowOf('Tool calls per background task')).getByText("Server's value: 60. Ceiling: 60.")).toBeTruthy();
    expect(screen.queryByText(/Lowered by the server/)).toBeNull();
    expect(screen.queryByText('No host ceiling set.')).toBeNull();
  });

  it('a save sends PUT with the whole number as JSON and says what it does', async () => {
    handlers['PUT /autonomy/limits/subtask_tool_call_limit'] = (body) => {
      limits = [TURNS(), limitRow({ value: (body as { value: number }).value, source: 'owner' })];
      return [200, { enabled: true, changed: true, limit: limits[1] }];
    };
    render(Limits);
    const input = await screen.findByLabelText('Tool calls per background task');
    const save = within(rowOf('Tool calls per background task')).getByRole('button', { name: 'Save Tool calls per background task' });
    expect((save as HTMLButtonElement).disabled).toBe(true); // nothing typed yet
    await fireEvent.input(input, { target: { value: '100' } });
    await fireEvent.click(save);
    const said = await screen.findByText('This lets background tasks do more: up to 100 tool calls per run.');
    expect(sent('PUT')).toEqual([{ url: '/autonomy/limits/subtask_tool_call_limit', method: 'PUT', body: { value: 100 }, type: 'application/json' }]);
    await waitFor(() => expect(document.activeElement).toBe(said));
    const row = rowOf('Tool calls per background task');
    expect(within(row).getByText("Your value. The server's value is 50.")).toBeTruthy();
    expect(within(row).getByRole('button', { name: "Use the host's value for Tool calls per background task" })).toBeTruthy();
  });

  it('lowering says it stops sooner', async () => {
    handlers['PUT /autonomy/limits/subtask_tool_call_limit'] = () => {
      limits = [TURNS(), limitRow({ value: 10, source: 'owner' })];
      return [200, { enabled: true, changed: true, limit: limits[1] }];
    };
    render(Limits);
    const input = await screen.findByLabelText('Tool calls per background task');
    await fireEvent.input(input, { target: { value: '10' } });
    await fireEvent.keyDown(input, { key: 'Enter' });
    expect(await screen.findByText('This stops background tasks sooner.')).toBeTruthy();
  });

  it('a refused change shows the server\'s sentence and keeps what was typed', async () => {
    handlers['PUT /autonomy/limits/subtask_tool_call_limit'] = () =>
      [400, { error: 'Tool calls per background task must be a whole number from 1 to 200 (tool calls).' }];
    render(Limits);
    const input = (await screen.findByLabelText('Tool calls per background task')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: '500' } });
    await fireEvent.click(within(rowOf('Tool calls per background task')).getByRole('button', { name: /^Save/ }));
    const said = await screen.findByText('Tool calls per background task must be a whole number from 1 to 200 (tool calls).');
    expect(said.getAttribute('role')).toBe('alert');
    expect(input.value).toBe('500');
    expect(screen.queryByText('error')).toBeNull();
  });

  it('a value that is not a whole number is sent as typed, and the server says why not', async () => {
    let got: unknown;
    handlers['PUT /autonomy/limits/subtask_tool_call_limit'] = (body) => {
      got = body;
      return [400, { error: 'Tool calls per background task must be a whole number from 1 to 200 (tool calls).' }];
    };
    render(Limits);
    const input = await screen.findByLabelText('Tool calls per background task');
    await fireEvent.input(input, { target: { value: '12.5' } });
    await fireEvent.click(within(rowOf('Tool calls per background task')).getByRole('button', { name: /^Save/ }));
    await screen.findByText(/must be a whole number/);
    expect(got).toEqual({ value: '12.5' });
  });

  it("\"Use the host's value\" sends DELETE as JSON", async () => {
    serve({ limits: [TURNS(), limitRow({ value: 80, source: 'owner' })] });
    handlers['DELETE /autonomy/limits/subtask_tool_call_limit'] = () => {
      limits = [TURNS(), limitRow()];
      return [200, { enabled: true, changed: true, limit: limits[1] }];
    };
    render(Limits);
    await screen.findByLabelText('Tool calls per background task');
    await fireEvent.click(screen.getByRole('button', { name: "Use the host's value for Tool calls per background task" }));
    expect(await screen.findByText("Back to the host's value: 50 tool calls.")).toBeTruthy();
    expect(sent('DELETE')).toEqual([{ url: '/autonomy/limits/subtask_tool_call_limit', method: 'DELETE', body: undefined, type: 'application/json' }]);
  });

  it('the settings off show a card naming the host setting', async () => {
    handlers['GET /autonomy/limits'] = () => [200, { enabled: false }];
    handlers['GET /autonomy/log'] = () => [200, { enabled: false }];
    render(Limits);
    expect(await screen.findByRole('heading', { name: 'Autonomy settings are off on this server.' })).toBeTruthy();
    expect(screen.getByText(/NOUS_AUTONOMY_SETTINGS_ENABLED=true/)).toBeTruthy();
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('a save of the value already saved says so', async () => {
    // Another tab saved 80 meanwhile: this one still shows the host's 50, and its save of 80 changes nothing.
    handlers['PUT /autonomy/limits/subtask_tool_call_limit'] = () =>
      [200, { enabled: true, changed: false, limit: limitRow({ value: 80, source: 'owner' }) }];
    render(Limits);
    const input = await screen.findByLabelText('Tool calls per background task');
    await fireEvent.input(input, { target: { value: '80' } });
    await fireEvent.click(within(rowOf('Tool calls per background task')).getByRole('button', { name: /^Save/ }));
    expect(await screen.findByText('That was already the saved value.')).toBeTruthy();
    expect(sent('PUT').map((c) => c.body)).toEqual([{ value: 80 }]);
  });

  it('a write while the saved settings could not be read shows the server\'s sentence', async () => {
    handlers['PUT /autonomy/limits/subtask_tool_call_limit'] = () =>
      [503, { error: 'The saved settings could not be read, so nothing can be changed until they can.' }];
    render(Limits);
    const input = await screen.findByLabelText('Tool calls per background task');
    await fireEvent.input(input, { target: { value: '70' } });
    await fireEvent.click(within(rowOf('Tool calls per background task')).getByRole('button', { name: /^Save/ }));
    const said = await screen.findByText('The saved settings could not be read, so nothing can be changed until they can.');
    expect(said.getAttribute('role')).toBe('alert');
  });

  it('settings that could not be read show the server\'s sentence and no row, and Retry reads again', async () => {
    let reads = 0;
    handlers['GET /autonomy/limits'] = () => {
      reads += 1;
      return reads === 1 ? [200, { enabled: true, loaded: false, message: NOT_LOADED_MESSAGE }] : [200, limitsAnswer(limits)];
    };
    render(Limits);
    expect(await screen.findByRole('heading', { name: 'The saved settings could not be read.' })).toBeTruthy();
    expect(screen.getByText(NOT_LOADED_MESSAGE)).toBeTruthy();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(calls.map((c) => c.url)).not.toContain('/autonomy/log?kind=limits');
    const retry = screen.getAllByRole('button', { name: 'Retry' }).find((b) => b.closest('section'));
    await fireEvent.click(retry as HTMLElement);
    expect(await screen.findByLabelText('Tool calls per background task')).toBeTruthy();
    expect(reads).toBe(2);
  });

  it('the header says when the last read was, or what failed with a Retry, and no shared badge', async () => {
    render(Limits);
    await screen.findByLabelText('Tool calls per background task');
    const head = document.querySelector('header.view-head') as HTMLElement;
    expect(within(head).getByText(/^Read at \d\d:\d\d UTC\.$/)).toBeTruthy();
    expect(within(head).getByRole('button', { name: 'Refresh' })).toBeTruthy();
    expect(head.textContent).not.toMatch(/retrying|updated|ago/);
    handlers['GET /autonomy/limits'] = () => [500, { error: 'The request could not be processed.' }];
    await fireEvent.click(within(head).getByRole('button', { name: 'Refresh' }));
    expect(await within(head).findByText(
      'Could not read the limits: The request could not be processed. What is shown is from the last read.',
    )).toBeTruthy();
    serve();
    await fireEvent.click(within(head).getByRole('button', { name: 'Retry' }));
    expect(await within(head).findByText(/^Read at /)).toBeTruthy();
  });

  it('a 403 asks for the sign-in, in words', async () => {
    handlers['GET /autonomy/limits'] = () => [403, { error: 'forbidden' }];
    render(Limits);
    expect(await screen.findByText('Sign in through the login page to see and change these settings.')).toBeTruthy();
    expect(screen.queryByText('forbidden')).toBeNull();
  });
});

describe('Limits and tools: the tools', () => {
  beforeEach(() => go('#/limits/tools'));

  it('groups the tools and gives each two words, the locked one disabled with its reason', async () => {
    render(Limits);
    expect(await screen.findByRole('heading', { name: 'Reading' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Writing' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Reaching outside' })).toBeTruthy();
    const web = screen.getByText('web_search').closest('li') as HTMLElement;
    const radios = within(web).getAllByRole('radio') as HTMLInputElement[];
    expect(radios.map((r) => r.closest('label')?.textContent?.trim())).toEqual(['Allowed', 'Not allowed']);
    expect(radios[0].checked).toBe(true);
    expect(within(web).getByText('Reads only, and brings in outside text.')).toBeTruthy();
    expect(within(web).getByText('Background calls are not recorded on this server.')).toBeTruthy();
    const mail = screen.getByText('send_email').closest('li') as HTMLElement;
    expect(within(mail).getByText('Locked by the server')).toBeTruthy();
    for (const r of within(mail).getAllByRole('radio') as HTMLInputElement[]) expect(r.disabled).toBe(true);
    expect((within(mail).getByRole('radio', { name: 'Not allowed' }) as HTMLInputElement).checked).toBe(true);
    expect(within(mail).getByText(
      'Not offered here: Chain decisions, Work in a chain: A chain keeps to a fixed set of tools that act inside Nous.',
    )).toBeTruthy();
  });

  it('Not allowed sends PUT {"setting": "denied"}; Allowed returns the tool to the default with DELETE', async () => {
    handlers['PUT /autonomy/tools/web_search'] = () => {
      tools = [toolRow({ setting: 'denied', source: 'owner' }), ...tools.slice(1)];
      return [200, { enabled: true, changed: true, tool: tools[0] }];
    };
    handlers['DELETE /autonomy/tools/web_search'] = () => {
      tools = [toolRow(), ...tools.slice(1)];
      return [200, { enabled: true, changed: true, tool: tools[0] }];
    };
    render(Limits);
    await screen.findByText('web_search');
    let web = screen.getByText('web_search').closest('li') as HTMLElement;
    await fireEvent.click(within(web).getByRole('radio', { name: 'Not allowed' }));
    const said = await screen.findByText('web_search is not allowed for background work now.');
    expect(sent('PUT')).toEqual([{ url: '/autonomy/tools/web_search', method: 'PUT', body: { setting: 'denied' }, type: 'application/json' }]);
    await waitFor(() => expect(document.activeElement).toBe(said));
    web = screen.getByText('web_search').closest('li') as HTMLElement;
    expect((within(web).getByRole('radio', { name: 'Not allowed' }) as HTMLInputElement).checked).toBe(true);
    await fireEvent.click(within(web).getByRole('radio', { name: 'Allowed' }));
    expect(await screen.findByText('web_search is allowed for background work again.')).toBeTruthy();
    expect(sent('DELETE')).toEqual([{ url: '/autonomy/tools/web_search', method: 'DELETE', body: undefined, type: 'application/json' }]);
  });

  it('a refused tool change shows the server\'s sentence and keeps the setting it had', async () => {
    handlers['PUT /autonomy/tools/web_search'] = () => [409, { error: 'web_search is locked by the server and cannot be changed here.' }];
    render(Limits);
    await screen.findByText('web_search');
    const web = screen.getByText('web_search').closest('li') as HTMLElement;
    await fireEvent.click(within(web).getByRole('radio', { name: 'Not allowed' }));
    expect(await screen.findByText('web_search is locked by the server and cannot be changed here.')).toBeTruthy();
    expect((within(web).getByRole('radio', { name: 'Allowed' }) as HTMLInputElement).checked).toBe(true);
  });

  it('the filter and the search', async () => {
    tools = [];
    serve({
      tools: [
        toolRow(),
        toolRow({ name: 'bash', summary: 'Run a shell command.', group: 'writing', setting: 'denied', source: 'owner' }),
        toolRow({ name: 'read_file', summary: 'Read a file.', group: 'reading' }),
      ],
    });
    render(Limits);
    await screen.findByText('web_search');
    await fireEvent.click(screen.getByRole('button', { name: 'Changed' }));
    expect(screen.queryByText('web_search')).toBeNull();
    expect(screen.getByText('bash')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'All' }));
    await fireEvent.input(screen.getByLabelText('Search tools'), { target: { value: 'FILE' } });
    expect(screen.getByText('read_file')).toBeTruthy();
    expect(screen.queryByText('web_search')).toBeNull();
    await fireEvent.input(screen.getByLabelText('Search tools'), { target: { value: 'nothing like it' } });
    expect(screen.getByText('No tool matches.')).toBeTruthy();
  });

  it('calls are counted when the server records them', async () => {
    handlers['GET /autonomy/tools'] = () =>
      [200, toolsAnswer([toolRow({ calls_30d: 3, last_call: '2026-11-16T14:31:00+00:00' })], { recorded: true })];
    render(Limits);
    expect(await screen.findByText('3 background calls in 30 days, the last Nov 16 14:31 UTC.')).toBeTruthy();
  });

  it('says where Not allowed applies, from the server\'s list, what is not listed, and the tool changes', async () => {
    render(Limits);
    await screen.findByText('web_search');
    expect(screen.getByText(/^Not allowed applies to background tasks, workflow steps, scheduled tasks, chain decisions,/)).toBeTruthy();
    expect(screen.getByText(/a chat or MCP turn in a session background work started\. Chat itself, a tap on a companion card and an action you approved are not held\.$/)).toBeTruthy();
    expect(screen.getByText(/a newly added server's tools start Allowed\.$/)).toBeTruthy();
    expect(screen.getByText("web_search: default to Not allowed, with the owner's secret, Nov 16 14:31 UTC")).toBeTruthy();
    expect(calls.map((c) => c.url)).toContain('/autonomy/log?kind=tools');
    expect(calls.map((c) => c.url)).not.toContain('/autonomy/log?kind=limits');
  });

  it('the places come from the answer, not from the screen', async () => {
    handlers['GET /autonomy/tools'] = () => [200, toolsAnswer(tools, { held_in: ['scheduled tasks', 'chain decisions'] })];
    render(Limits);
    expect(await screen.findByText(/^Not allowed applies to scheduled tasks and chain decisions\./)).toBeTruthy();
  });

  it('a tool setting already saved says so', async () => {
    handlers['PUT /autonomy/tools/web_search'] = () => [200, { enabled: true, changed: false, tool: toolRow({ setting: 'denied', source: 'owner' }) }];
    render(Limits);
    await screen.findByText('web_search');
    const web = screen.getByText('web_search').closest('li') as HTMLElement;
    await fireEvent.click(within(web).getByRole('radio', { name: 'Not allowed' }));
    expect(await screen.findByText('That was already the saved setting.')).toBeTruthy();
  });

  it('tool settings that could not be read show the server\'s sentence and no tool, and Retry reads again', async () => {
    let reads = 0;
    handlers['GET /autonomy/tools'] = () => {
      reads += 1;
      return reads === 1 ? [200, { enabled: true, loaded: false, message: NOT_LOADED_MESSAGE }] : [200, toolsAnswer(tools)];
    };
    render(Limits);
    expect(await screen.findByText(NOT_LOADED_MESSAGE)).toBeTruthy();
    expect(screen.queryByRole('radio')).toBeNull();
    expect(screen.queryByText('web_search')).toBeNull();
    const retry = screen.getAllByRole('button', { name: 'Retry' }).find((b) => b.closest('section'));
    await fireEvent.click(retry as HTMLElement);
    expect(await screen.findByText('web_search')).toBeTruthy();
    expect(reads).toBe(2);
  });

  it('the tool guard off shows a card naming both host settings', async () => {
    handlers['GET /autonomy/tools'] = () => [200, { enabled: false }];
    render(Limits);
    expect(await screen.findByRole('heading', { name: 'The tool guard is off on this server.' })).toBeTruthy();
    expect(screen.getByText(/NOUS_TOOL_GUARD_ENABLED=true, with NOUS_AUTONOMY_SETTINGS_ENABLED=true/)).toBeTruthy();
    expect(screen.queryByRole('radio')).toBeNull();
  });

  it('the sub views are links built by the router, the current one marked', async () => {
    render(Limits);
    const nav = screen.getByRole('navigation', { name: 'Limits and tools views' });
    const links = within(nav).getAllByRole('link');
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([['Limits', '#/limits'], ['Tools', '#/limits/tools']]);
    expect(links[1].getAttribute('aria-current')).toBe('page');
  });
});

describe('Limits and tools: the source', () => {
  const FILES = ['Limits.svelte', 'limits/LimitsTab.svelte', 'limits/Tools.svelte'];
  it.each(FILES)('%s: no raw HTML, no hand-written link, no colour literal, no raw fetch', (file) => {
    const src = readFileSync(join(__dirname, file), 'utf-8');
    expect(src).not.toMatch(/\{@html/);
    expect(markupOf(src)).not.toMatch(/href=\{?\s*["'`]#\//);
    expect(src).not.toMatch(/#[0-9a-fA-F]{3,8}\b(?![\w-])/);
    expect(scriptBlocks(src)).not.toMatch(/\bfetch\(/);
  });

  it.each(['limits/LimitsTab.svelte', 'limits/Tools.svelte'])('%s: no shared badge; the read line, then one Refresh or Retry', (file) => {
    const markup = markupOf(readFileSync(join(__dirname, file), 'utf-8'));
    expect(markup).not.toMatch(/<StaleBadge/);
    const head = markup.slice(markup.indexOf('<header'), markup.indexOf('</header>'));
    expect(head.indexOf('class="read-state"')).toBeGreaterThan(-1);
    expect(head.indexOf('class="read-state"')).toBeLessThan(head.indexOf('<Button'));
    expect(head.match(/<Button/g)).toHaveLength(1);
    expect(head).toMatch(/RETRY_LABEL : REFRESH_LABEL/);
  });

  it('the requests go through apiRequest only', () => {
    const lib = readFileSync(join(__dirname, '../lib/autonomy_settings.ts'), 'utf-8');
    expect(lib).not.toMatch(/\bfetch\(/);
    expect(lib.match(/apiRequest</g)?.length).toBe(7);
  });
});
