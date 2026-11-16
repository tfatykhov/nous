import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Limits from './Limits.svelte';
import { initRouter } from '../lib/router';
import { markupOf, scriptBlocks } from '../lib/testing/css';
import { TURNS, change, limitRow, limitsAnswer, toolRow, toolsAnswer } from '../lib/testing/autonomyFixtures';
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
  handlers['GET /autonomy/log'] = () => [200, { enabled: true, changes: [change()] }];
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
  it('shows the preferences card, and each row its range, source, host value and when it applies', async () => {
    render(Limits);
    expect(screen.getByRole('heading', { level: 1, name: 'Limits and tools' })).toBeTruthy();
    expect(screen.getByText(/These are your preferences, kept with a record of each change\./)).toBeTruthy();
    const input = (await screen.findByLabelText('Tool calls per background turn')) as HTMLInputElement;
    expect(input.value).toBe('50');
    const row = rowOf('Tool calls per background turn');
    expect(within(row).getByText('From 1 to 200 tool calls')).toBeTruthy();
    expect(within(row).getByText('Host value')).toBeTruthy();
    expect(within(row).getByText('Host value: 50')).toBeTruthy();
    expect(within(row).getByText('From the next background turn.')).toBeTruthy();
    expect(within(rowOf('Tool-loop iterations per turn')).getByText('From the next turn. This applies to every turn, chat included.')).toBeTruthy();
    expect(screen.getByText('No host ceiling set.')).toBeTruthy();
    // no "Use the host's value" while the value is the host's
    expect(within(row).queryByRole('button', { name: /Use the host's value/ })).toBeNull();
    // the last changes, in words
    expect(screen.getByText('Tool calls per background turn: host value to 80, by you, Nov 16 14:31 UTC')).toBeTruthy();
  });

  it('a clamped value and a ceiling say so', async () => {
    serve({ limits: [limitRow({ host_value: 80, baseline: 60, value: 60, ceiling: 60, source: 'clamped' })] });
    handlers['GET /autonomy/limits'] = () => [200, limitsAnswer(limits, { ceilings_set: true })];
    render(Limits);
    const row = (await screen.findByLabelText('Tool calls per background turn')).closest('li') as HTMLElement;
    expect(within(row).getByText('Lowered by the server')).toBeTruthy();
    expect(within(row).getByText('Lowered by the server to 60')).toBeTruthy();
    expect(within(row).getByText('Host value: 80')).toBeTruthy();
    expect(screen.queryByText('No host ceiling set.')).toBeNull();
  });

  it('a save sends PUT with the whole number as JSON and says what it does', async () => {
    handlers['PUT /autonomy/limits/subtask_tool_call_limit'] = (body) => {
      limits = [TURNS(), limitRow({ value: (body as { value: number }).value, source: 'owner' })];
      return [200, { enabled: true, changed: true, limit: limits[1] }];
    };
    render(Limits);
    const input = await screen.findByLabelText('Tool calls per background turn');
    const save = within(rowOf('Tool calls per background turn')).getByRole('button', { name: 'Save Tool calls per background turn' });
    expect((save as HTMLButtonElement).disabled).toBe(true); // nothing typed yet
    await fireEvent.input(input, { target: { value: '100' } });
    await fireEvent.click(save);
    const said = await screen.findByText('This lets background work run longer: up to 100 tool calls per turn.');
    expect(sent('PUT')).toEqual([{ url: '/autonomy/limits/subtask_tool_call_limit', method: 'PUT', body: { value: 100 }, type: 'application/json' }]);
    await waitFor(() => expect(document.activeElement).toBe(said));
    const row = rowOf('Tool calls per background turn');
    expect(within(row).getByText('Your value')).toBeTruthy();
    expect(within(row).getByRole('button', { name: "Use the host's value for Tool calls per background turn" })).toBeTruthy();
  });

  it('lowering says it stops sooner', async () => {
    handlers['PUT /autonomy/limits/subtask_tool_call_limit'] = () => {
      limits = [TURNS(), limitRow({ value: 10, source: 'owner' })];
      return [200, { enabled: true, changed: true, limit: limits[1] }];
    };
    render(Limits);
    const input = await screen.findByLabelText('Tool calls per background turn');
    await fireEvent.input(input, { target: { value: '10' } });
    await fireEvent.keyDown(input, { key: 'Enter' });
    expect(await screen.findByText('This stops background turns sooner.')).toBeTruthy();
  });

  it('a refused change shows the server\'s sentence and keeps what was typed', async () => {
    handlers['PUT /autonomy/limits/subtask_tool_call_limit'] = () =>
      [400, { error: 'Tool calls per background turn must be a whole number from 1 to 200 (tool calls).' }];
    render(Limits);
    const input = (await screen.findByLabelText('Tool calls per background turn')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: '500' } });
    await fireEvent.click(within(rowOf('Tool calls per background turn')).getByRole('button', { name: /^Save/ }));
    const said = await screen.findByText('Tool calls per background turn must be a whole number from 1 to 200 (tool calls).');
    expect(said.getAttribute('role')).toBe('alert');
    expect(input.value).toBe('500');
    expect(screen.queryByText('error')).toBeNull();
  });

  it('a value that is not a whole number is sent as typed, and the server says why not', async () => {
    let got: unknown;
    handlers['PUT /autonomy/limits/subtask_tool_call_limit'] = (body) => {
      got = body;
      return [400, { error: 'Tool calls per background turn must be a whole number from 1 to 200 (tool calls).' }];
    };
    render(Limits);
    const input = await screen.findByLabelText('Tool calls per background turn');
    await fireEvent.input(input, { target: { value: '12.5' } });
    await fireEvent.click(within(rowOf('Tool calls per background turn')).getByRole('button', { name: /^Save/ }));
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
    await screen.findByLabelText('Tool calls per background turn');
    await fireEvent.click(screen.getByRole('button', { name: "Use the host's value for Tool calls per background turn" }));
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
    expect(within(web).getByText('Reads only · Brings in outside text')).toBeTruthy();
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

  it('the requests go through apiRequest only', () => {
    const lib = readFileSync(join(__dirname, '../lib/autonomy_settings.ts'), 'utf-8');
    expect(lib).not.toMatch(/\bfetch\(/);
    expect(lib.match(/apiRequest</g)?.length).toBe(7);
  });
});
