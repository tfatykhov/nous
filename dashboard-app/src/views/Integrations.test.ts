import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Integrations from './Integrations.svelte';
import { markupOf, styleBlocks } from '../lib/testing/css';

// F112 PR 2b: the Integrations screen over the /integrations/mcp routes. The fixtures have the shapes
// nous/api/mcp_client_routes.py answers with; every value is made up.

type Answer = [number, unknown];
type Call = { url: string; method: string; headers: Record<string, string> | undefined; body: unknown; raw: string | undefined };

const tool = (name: string, over: Record<string, unknown> = {}) => ({
  tool: name, name: `mcp__deepwiki__${name}`, permission: 'deny', registered: false, skip_reason: 'its permission is deny',
  blocked_by_ceiling: false, description: `${name} description`, schema: '{"type":"object","properties":{}}', ...over,
});

const NO_AUTH = () => ({ kind: 'none', header_names: [] as string[], updated_at: null, status: 'ok', reason: null as string | null });

const LIST = () => ({
  ceilings: { pinned_servers: true, background_servers: false },
  secrets_available: true,
  servers: [
    {
      name: 'deepwiki', url: 'https://mcp.deepwiki.com/mcp', enabled: true, background: false, background_in_effect: false,
      background_blocked_by_ceiling: false, call_timeout_seconds: 30, connected: true, blocked_by_ceiling: false, skip_reason: null,
      auth: NO_AUTH(),
      tools: [
        tool('ask_question', { permission: 'allow', registered: true, skip_reason: null }),
        tool('read_wiki'),
        tool('search', { permission: 'allow', blocked_by_ceiling: true,
          skip_reason: "it is not in its server's pin (NOUS_MCP_CLIENT_PINNED_SERVERS)" }),
      ],
    },
    {
      name: 'files', url: 'https://files.example.com/mcp', enabled: false, background: true, background_in_effect: false,
      background_blocked_by_ceiling: true, call_timeout_seconds: 30, connected: false, blocked_by_ceiling: false,
      skip_reason: 'the server is disabled', auth: NO_AUTH(),
      tools: [tool('list', { name: 'mcp__files__list', permission: 'allow', skip_reason: 'the server is disabled' })],
    },
  ],
});

const PROBE = (hash = 'a'.repeat(64)) => ({
  tools: [
    { tool: 'ask_question', name: 'mcp__deepwiki__ask_question', description: 'Ask a question about a repository.',
      schema: '{"type":"object","properties":{"q":{"type":"string"}}}', skip_reason: null },
    { tool: 'read_wiki', name: 'mcp__deepwiki__read_wiki', description: 'Read the wiki.', schema: '{"type":"object"}', skip_reason: null },
    { tool: 'broken', name: 'mcp__deepwiki__broken', description: null, schema: null, skip_reason: 'the input schema is not an object' },
  ],
  definitions_sha256: hash,
});

let calls: Call[] = [];
let listAnswer: () => Answer = () => [200, LIST()];
let handlers: Record<string, (body: unknown) => Answer | Promise<Answer>> = {};
/** The stored servers a stateful list answers with (see `patching`). */
let state: ReturnType<typeof LIST> = LIST();

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    const raw = typeof init.body === 'string' ? init.body : undefined;
    const body = raw === undefined ? undefined : JSON.parse(raw);
    calls.push({ url, method, headers: init.headers as Record<string, string> | undefined, body, raw });
    const [status, payload] = method === 'GET' && url === '/integrations/mcp/servers'
      ? listAnswer()
      : await (handlers[`${method} ${url}`]?.(body) ?? [500, { error: `unexpected ${method} ${url}` }]);
    if (status === 0) throw new TypeError('Failed to fetch');
    // A string is sent as it is (a proxy's page); anything else as JSON.
    return new Response(payload === undefined ? '' : typeof payload === 'string' ? payload : JSON.stringify(payload), { status });
  }));
}

/** A list that answers with `state`, and a PATCH of `server` that changes it as the route would. */
function patching(server: string) {
  listAnswer = () => [200, JSON.parse(JSON.stringify(state))];
  handlers[`PATCH /integrations/mcp/servers/${server}`] = (body) => {
    const s = state.servers.find((x) => x.name === server)!;
    const b = body as { enabled?: boolean; background?: boolean; tools?: Record<string, string> };
    if (b.enabled !== undefined) s.enabled = b.enabled;
    if (b.background !== undefined) s.background = b.background;
    for (const [tool, permission] of Object.entries(b.tools ?? {})) {
      const t = s.tools.find((x) => x.tool === tool)!;
      Object.assign(t, { permission, registered: permission === 'allow', skip_reason: permission === 'allow' ? null : 'its permission is deny' });
    }
    return [200, { server: null }];
  };
}

const lists = () => calls.filter((c) => c.method === 'GET').length;
const changes = () => calls.filter((c) => c.method !== 'GET');
/** A request as sent: compared whole, since the routes refuse an unknown field with 400. */
const sent = (c: Call) => ({ method: c.method, url: c.url, body: c.body });
const card = (name: string) => screen.getByRole('region', { name });
const row = (scope: HTMLElement, toolName: string) =>
  within(scope).getByText(toolName, { selector: '.tool-head code' }).closest('li') as HTMLElement;

/** A promise the test resolves itself, to hold a request in flight. */
function held(): { promise: Promise<Answer>; resolve: (a: Answer) => void } {
  let resolve!: (a: Answer) => void;
  const promise = new Promise<Answer>((r) => { resolve = r; });
  return { promise, resolve };
}

beforeEach(() => {
  calls = [];
  state = LIST();
  listAnswer = () => [200, LIST()];
  handlers = {};
  install();
});
// globals: false (vite.config.ts): testing-library cannot register its own cleanup.
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Integrations: the states', () => {
  it.each([[503, { error: 'the MCP client routes are not configured' }], [503, { error: 'no MCP client is running' }], [404, undefined]] as Answer[])(
    '%i says MCP servers are not available here, with both reasons, and no form', async (status, body) => {
      listAnswer = () => [status, body];
      render(Integrations);
      expect(await screen.findByRole('heading', { name: 'MCP servers are not available here' })).toBeTruthy();
      expect(screen.getByText(/the MCP client is switched off/)).toBeTruthy();
      expect(screen.getByText(/the screen's secret is not configured on the server/)).toBeTruthy();
      expect(screen.queryByRole('button', { name: 'Check server' })).toBeNull();
    });

  it('403 asks for the protected address', async () => {
    listAnswer = () => [403, { error: 'forbidden' }];
    render(Integrations);
    expect(await screen.findByRole('heading', { name: 'Open this screen through the protected address' })).toBeTruthy();
    expect(screen.getByText(/did not come through the login proxy/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Check server' })).toBeNull();
  });

  it("a 500 and a network failure take the screens' error pattern, with the route's message", async () => {
    listAnswer = () => [500, { error: 'the request could not be processed' }];
    render(Integrations);
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load the MCP servers: The request could not be processed.');
    cleanup();
    listAnswer = () => [0, undefined];
    render(Integrations);
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load the MCP servers: No answer from Nous (Failed to fetch).');
  });

  it('a state a later poll finds is not hidden by the list an earlier one loaded', async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    listAnswer = () => [503, { error: 'no MCP client is running' }];
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByRole('heading', { name: 'MCP servers are not available here' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'deepwiki' })).toBeNull();
  });

  it('an empty list invites the first server, and says no host limit is set', async () => {
    listAnswer = () => [200, { ceilings: { pinned_servers: false, background_servers: false }, servers: [] }];
    render(Integrations);
    expect(await screen.findByText(/No MCP servers yet\. Add the first one below/)).toBeTruthy();
    expect(screen.getByText('No limit is set on the host: what you allow here is what the agent may use.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Check server' })).toBeTruthy();
  });

  it('says once that changes apply at once, and the title names the screen as the menu does', async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Integrations');
    expect(screen.getAllByText(/applies at once/)).toHaveLength(1);
  });
});

describe('Integrations: the list', () => {
  it('a card per server: name, address, status in words, and its tools with their status in words', async () => {
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    expect(within(deepwiki).getByText('https://mcp.deepwiki.com/mcp')).toBeTruthy();
    expect(within(deepwiki).getByText('connected', { selector: '.status-pill' })).toBeTruthy();
    expect(within(row(deepwiki, 'ask_question')).getByText('in use')).toBeTruthy();
    expect(within(row(deepwiki, 'read_wiki')).getByText('not allowed')).toBeTruthy();
    const search = row(deepwiki, 'search');
    expect(within(search).getByText('skipped')).toBeTruthy();
    expect(within(search).getByText("it is not in its server's pin (NOUS_MCP_CLIENT_PINNED_SERVERS)")).toBeTruthy();
    expect(within(search).getByText("Blocked by the server's host settings.")).toBeTruthy();
    expect(within(row(deepwiki, 'read_wiki')).queryByText(/Blocked by/)).toBeNull();
    expect(within(row(deepwiki, 'read_wiki')).getByText('mcp__deepwiki__read_wiki')).toBeTruthy();
    expect(within(row(deepwiki, 'read_wiki')).getByText('read_wiki description')).toBeTruthy();

    const files = card('files');
    expect(within(files).getByText('switched off', { selector: '.status-pill' })).toBeTruthy();
    expect(within(row(files, 'list')).getByText('the server is switched off')).toBeTruthy(); // the route says "disabled"
    expect(within(files).queryByText(/the server is disabled/)).toBeNull();
    expect(within(files).getByText(/Background work is blocked by the server's host settings/)).toBeTruthy();
    expect(within(card('deepwiki')).getByRole('button', { name: 'Switch off' })).toBeTruthy();
    expect(within(files).getByRole('button', { name: 'Switch on' })).toBeTruthy();
  });

  it('says plainly which host limits are in effect', async () => {
    render(Integrations);
    expect(await screen.findByRole('heading', { name: 'Limits set on the host are in effect' })).toBeTruthy();
    expect(screen.getByText(/Only the servers and tools pinned on the host/)).toBeTruthy();
    expect(screen.queryByText(/Background work can use only the servers listed/)).toBeNull();
  });

  it('a long description is clamped and opens on request; a schema is a collapsed block', async () => {
    const long = `${'word '.repeat(80)}end`;
    listAnswer = () => {
      const d = LIST();
      d.servers[0].tools[1] = tool('read_wiki', { description: long });
      return [200, d];
    };
    render(Integrations);
    const r = row(await screen.findByRole('region', { name: 'deepwiki' }), 'read_wiki');
    expect(within(r).queryByText(long)).toBeNull();
    const more = within(r).getByRole('button', { name: 'Show all' });
    expect(more.getAttribute('aria-expanded')).toBe('false');
    await fireEvent.click(more);
    expect(within(r).getByText(long.trim())).toBeTruthy();
    expect(within(r).getByRole('button', { name: 'Show less' }).getAttribute('aria-expanded')).toBe('true');
    const details = r.querySelector('details.schema') as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details.querySelector('summary')?.textContent).toBe('Schema');
    expect(details.querySelector('pre')?.textContent).toBe('{\n  "type": "object",\n  "properties": {}\n}');
  });
});

describe('Integrations: changing a server', () => {
  it('Switch off sends enabled false, says so, and reloads the list', async () => {
    patching('deepwiki');
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    const before = lists();
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Switch off' }));
    await waitFor(() => expect(lists()).toBe(before + 1));
    expect(changes().map(sent)).toEqual([{ method: 'PATCH', url: '/integrations/mcp/servers/deepwiki', body: { enabled: false } }]);
    expect((await within(card('deepwiki')).findByRole('status')).textContent).toBe('Switched off: its tools are no longer offered to the agent.');
    expect(within(card('deepwiki')).getByText('switched off', { selector: '.status-pill' })).toBeTruthy();
  });

  it("a server's controls are disabled while a change is in flight, and come back when it lands", async () => {
    const answer = held();
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => answer.promise;
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(deepwiki).getByRole('button', { name: 'Switch off' }));
    const flying = within(deepwiki).getByRole('button', { name: 'Switching off…' }) as HTMLButtonElement;
    expect(flying.disabled).toBe(true);
    for (const name of ['Refresh tools', 'Remove', "Don't allow ask_question", 'Allow read_wiki']) {
      expect((within(deepwiki).getByRole('button', { name }) as HTMLButtonElement).disabled, name).toBe(true);
    }
    expect((within(deepwiki).getByRole('checkbox', { name: 'Allow in background work' }) as HTMLInputElement).disabled).toBe(true);
    // The other server's controls stay usable.
    expect((within(card('files')).getByRole('button', { name: 'Switch on' }) as HTMLButtonElement).disabled).toBe(false);
    answer.resolve([200, { server: null }]);
    await waitFor(() => expect((within(card('deepwiki')).getByRole('button', { name: 'Switch off' }) as HTMLButtonElement).disabled).toBe(false));
  });

  it('an open confirmation cannot be sent while another change of its server is in flight', async () => {
    const answers = [held(), held()];
    let n = 0;
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => answers[n++].promise;
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    const enabledOf = (name: string) => !(within(card('deepwiki')).getByRole('button', { name }) as HTMLButtonElement).disabled;
    // One confirmation is open at a time: Remove's, then background work's.
    await fireEvent.click(within(deepwiki).getByRole('button', { name: 'Remove' }));
    await fireEvent.click(within(deepwiki).getByRole('button', { name: 'Switch off' }));
    expect(enabledOf('Yes, remove')).toBe(false);
    answers[0].resolve([200, { server: null }]);
    await waitFor(() => expect(enabledOf('Yes, remove')).toBe(true));
    await fireEvent.click(within(card('deepwiki')).getByRole('checkbox', { name: 'Allow in background work' }));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Switch off' }));
    expect(enabledOf('Yes, allow in background work')).toBe(false);
    answers[1].resolve([200, { server: null }]);
    await waitFor(() => expect(enabledOf('Yes, allow in background work')).toBe(true));
    expect(changes().map((c) => c.body)).toEqual([{ enabled: false }, { enabled: false }]);
  });

  it("a server's name never takes the id of the form's own elements", async () => {
    listAnswer = () => {
      const d = LIST();
      return [200, { ...d, servers: ['add', 'name', 'url', 'limits'].map((name) => ({ ...d.servers[0], name })) }];
    };
    render(Integrations);
    await screen.findByRole('region', { name: 'name' });
    expect(screen.getByLabelText('Name').tagName).toBe('INPUT');
    expect(screen.getByLabelText('Address').tagName).toBe('INPUT');
    expect(screen.getByRole('region', { name: 'Add a server' })).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Limits set on the host are in effect' })).toBeTruthy();
    const ids = Array.from(document.querySelectorAll('[id]')).map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("a refused change shows the route's message in words next to the server", async () => {
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => [500, { error: 'the change was stored, but applying it failed: see the log' }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    const before = lists();
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Switch off' }));
    expect((await within(card('deepwiki')).findByRole('alert')).textContent).toBe('The change was stored, but applying it failed: see the log.');
    await waitFor(() => expect(lists()).toBe(before + 1)); // reloaded all the same: the change may be stored
  });

  it('turning background work on asks first, and sends background true only when confirmed', async () => {
    patching('deepwiki');
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    expect(within(deepwiki).getByText("Scheduled jobs, subtasks and other unattended work may call this server's tools.")).toBeTruthy();
    const box = within(deepwiki).getByRole('checkbox', { name: 'Allow in background work' }) as HTMLInputElement;
    await fireEvent.click(box);
    expect(box.checked).toBe(false); // nothing is stored yet
    expect(changes()).toEqual([]);
    const ask = within(deepwiki).getByRole('group', { name: 'Confirm background work for deepwiki' });
    await fireEvent.click(within(ask).getByRole('button', { name: 'Cancel' }));
    expect(within(deepwiki).queryByRole('group', { name: /Confirm background/ })).toBeNull();
    expect(changes()).toEqual([]);

    await fireEvent.click(box);
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Yes, allow in background work' }));
    await waitFor(() => expect(changes()).toHaveLength(1));
    expect(changes().map(sent)).toEqual([{ method: 'PATCH', url: '/integrations/mcp/servers/deepwiki', body: { background: true } }]);
    expect((await within(card('deepwiki')).findByRole('status')).textContent).toBe('Background work may now call its tools.');
  });

  it('turning background work off needs no confirmation', async () => {
    patching('files');
    render(Integrations);
    const files = await screen.findByRole('region', { name: 'files' });
    const box = within(files).getByRole('checkbox', { name: 'Allow in background work' }) as HTMLInputElement;
    expect(box.checked).toBe(true);
    await fireEvent.click(box);
    await waitFor(() => expect(changes()).toHaveLength(1));
    expect(changes().map(sent)).toEqual([{ method: 'PATCH', url: '/integrations/mcp/servers/files', body: { background: false } }]);
  });

  it('a tool is allowed or not allowed by its own name', async () => {
    patching('deepwiki');
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(deepwiki).getByRole('button', { name: 'Allow read_wiki' }));
    await waitFor(() => expect(changes()).toHaveLength(1));
    expect((await within(row(card('deepwiki'), 'read_wiki')).findByRole('status')).textContent).toBe('Allowed.');
    await waitFor(() => expect((within(card('deepwiki')).getByRole('button', { name: "Don't allow ask_question" }) as HTMLButtonElement).disabled).toBe(false));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: "Don't allow ask_question" }));
    await waitFor(() => expect(changes()).toHaveLength(2));
    expect(changes().map((c) => c.body)).toEqual([{ tools: { read_wiki: 'allow' } }, { tools: { ask_question: 'deny' } }]);
  });

  it('Refresh tools lists the new, removed and changed tools, and marks a changed one in the list', async () => {
    handlers['POST /integrations/mcp/servers/deepwiki/refresh'] = () =>
      [200, { added: ['new_tool'], removed: ['old_tool'], changed: ['ask_question'], definitions_sha256: 'c'.repeat(64), server: null }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Refresh tools' }));
    const list = await within(card('deepwiki')).findByRole('list', { name: 'What the refresh changed' });
    expect(Array.from(list.querySelectorAll('li')).map((li) => li.textContent)).toEqual([
      'New, not allowed until you allow them: new_tool.',
      'No longer listed by the server, now not allowed: old_tool.',
      'Definition changed, permission kept: ask_question. Read them again below.',
    ]);
    expect(within(row(card('deepwiki'), 'ask_question')).getByText('definition changed')).toBeTruthy();
    expect(within(row(card('deepwiki'), 'read_wiki')).queryByText('definition changed')).toBeNull();
    expect(changes().map(sent)).toEqual([{ method: 'POST', url: '/integrations/mcp/servers/deepwiki/refresh', body: undefined }]);
    // The server's names are data in their own elements, not part of the screen's sentence.
    expect(Array.from(list.querySelectorAll('li code')).map((c) => c.textContent)).toEqual(['new_tool', 'old_tool', 'ask_question']);
  });

  it('Remove asks first, then deletes the server and says so', async () => {
    let removed = false;
    handlers['DELETE /integrations/mcp/servers/deepwiki'] = () => { removed = true; return [200, { deleted: 'deepwiki' }]; };
    listAnswer = () => {
      const d = LIST();
      return [200, removed ? { ...d, servers: d.servers.slice(1) } : d];
    };
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Remove' }));
    const ask = within(card('deepwiki')).getByRole('group', { name: 'Confirm removing deepwiki' });
    expect(ask.textContent).toContain('Remove deepwiki and its 3 tools?');
    await fireEvent.click(within(ask).getByRole('button', { name: 'Cancel' }));
    expect(changes()).toEqual([]);
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Remove' }));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Yes, remove' }));
    expect(await screen.findByText('Removed deepwiki.')).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole('region', { name: 'deepwiki' })).toBeNull());
    expect(changes().map(sent)).toEqual([{ method: 'DELETE', url: '/integrations/mcp/servers/deepwiki', body: undefined }]);
  });
});

describe('Integrations: adding a server', () => {
  async function fill(name: string, url: string) {
    await fireEvent.input(screen.getByLabelText('Name'), { target: { value: name } });
    await fireEvent.input(screen.getByLabelText('Address'), { target: { value: url } });
  }
  const addCard = () => screen.getByRole('region', { name: 'Add a server' });

  it('checks the name and address as the server does, and sends nothing while they are wrong', async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('Deep__Wiki', 'http://mcp.deepwiki.com/mcp');
    expect(screen.getByLabelText('Name').getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByText(/^A name is 1 to 24 lower-case letters/)).toBeTruthy();
    expect(screen.getByText('Only https addresses are accepted.')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    expect(changes()).toEqual([]);
  });

  it('check, choose, add: every tool as the probe returned it, none allowed until ticked; 201 reloads the list', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    handlers['POST /integrations/mcp/servers'] = () => [201, { created: true, definitions_sha256: 'a'.repeat(64), server: null }];
    listAnswer = () => [200, { ceilings: { pinned_servers: false, background_servers: false }, servers: [] }];
    render(Integrations);
    await screen.findByText(/No MCP servers yet/);
    await fill('deepwiki', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    expect(await within(addCard()).findByText('3 tools listed')).toBeTruthy();
    expect(sent(changes()[0])).toEqual({ method: 'POST', url: '/integrations/mcp/probe', body: { name: 'deepwiki', url: 'https://mcp.deepwiki.com/mcp' } });

    const boxes = within(addCard()).getAllByRole('checkbox') as HTMLInputElement[];
    expect(boxes.map((b) => b.checked)).toEqual([false, false, false]);
    expect(within(addCard()).getByText('mcp__deepwiki__ask_question')).toBeTruthy();
    expect(within(addCard()).getByText('Ask a question about a repository.')).toBeTruthy();
    expect(within(addCard()).getByText('would be skipped')).toBeTruthy();
    expect(within(addCard()).getByText('the input schema is not an object')).toBeTruthy();
    expect(addCard().querySelectorAll('details.schema')).toHaveLength(2);

    await fireEvent.click(within(addCard()).getByRole('checkbox', { name: 'Allow read_wiki' }));
    const before = lists();
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Add server' }));
    expect(await within(addCard()).findByText('Added deepwiki. It is listed above.')).toBeTruthy();
    expect(sent(changes()[1])).toEqual({
      method: 'POST', url: '/integrations/mcp/servers',
      body: { name: 'deepwiki', url: 'https://mcp.deepwiki.com/mcp', tools: { ask_question: 'deny', read_wiki: 'allow', broken: 'deny' },
        expected_definitions_sha256: 'a'.repeat(64) },
    });
    await waitFor(() => expect(lists()).toBe(before + 1));
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('');
    expect(within(addCard()).queryByRole('button', { name: 'Add server' })).toBeNull();
  });

  it('409: says the tools changed, checks again and shows the new list, every choice back at not allowed', async () => {
    let probes = 0;
    handlers['POST /integrations/mcp/probe'] = () => {
      probes += 1;
      if (probes === 1) return [200, PROBE()];
      const next = PROBE('b'.repeat(64));
      next.tools.push({ tool: 'new_tool', name: 'mcp__deepwiki__new_tool', description: 'New.', schema: '{}', skip_reason: null });
      return [200, next];
    };
    let adds = 0;
    handlers['POST /integrations/mcp/servers'] = () => {
      adds += 1;
      return adds === 1
        ? [409, { error: "the server's tool definitions changed since they were shown: review them again", definitions_sha256: 'b'.repeat(64) }]
        : [201, { created: true, definitions_sha256: 'b'.repeat(64), server: null }];
    };
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    await fireEvent.click(within(addCard()).getByRole('checkbox', { name: 'Allow read_wiki' }));
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Add server' }));
    expect(await within(addCard()).findByText('4 tools listed')).toBeTruthy();
    expect(within(addCard()).getByRole('alert').textContent).toBe("The server's tools changed since you checked it. The new list is below: choose again.");
    expect(probes).toBe(2);
    // The check runs again for the name and address the first one was made for.
    expect(changes().filter((c) => c.url === '/integrations/mcp/probe').map((c) => c.body))
      .toEqual([{ name: 'wiki2', url: 'https://mcp.deepwiki.com/mcp' }, { name: 'wiki2', url: 'https://mcp.deepwiki.com/mcp' }]);
    expect((within(addCard()).getAllByRole('checkbox') as HTMLInputElement[]).some((b) => b.checked)).toBe(false);
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Add server' }));
    await within(addCard()).findByText('Added wiki2. It is listed above.');
    expect(changes().filter((c) => c.url === '/integrations/mcp/servers').map((c) => (c.body as Record<string, unknown>).expected_definitions_sha256))
      .toEqual(['a'.repeat(64), 'b'.repeat(64)]);
  });

  it.each([
    [400, { error: 'the address guard refused the URL: a private address' }, 'The address guard refused the URL: a private address.'],
    [502, { error: "could not list the server's tools: ConnectError" }, "Could not list the server's tools: ConnectError."],
  ] as [number, unknown, string][])("a probe refused with %i shows the route's message", async (status, body, text) => {
    handlers['POST /integrations/mcp/probe'] = () => [status, body];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://10.0.0.1/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    expect((await within(addCard()).findByRole('alert')).textContent).toBe(text);
    expect(within(addCard()).queryByRole('button', { name: 'Add server' })).toBeNull();
  });

  it("an add refused with 400 or 502 shows the route's message and keeps the choices", async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    handlers['POST /integrations/mcp/servers'] = () => [502, { error: "could not list the server's tools: ReadTimeout" }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    await fireEvent.click(within(addCard()).getByRole('checkbox', { name: 'Allow read_wiki' }));
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Add server' }));
    expect((await within(addCard()).findByRole('alert')).textContent).toBe("Could not list the server's tools: ReadTimeout.");
    expect((within(addCard()).getByRole('checkbox', { name: 'Allow read_wiki' }) as HTMLInputElement).checked).toBe(true);
  });

  it('a name or address edited after the check needs a new check before adding', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    await fireEvent.input(screen.getByLabelText('Name'), { target: { value: 'wiki3' } });
    expect(within(addCard()).queryByRole('button', { name: 'Add server' })).toBeNull();
    expect(within(addCard()).getByText('The name or address changed since the check: check the server again.')).toBeTruthy();
  });

  it('a name already taken says what replacing does, background work off included, and the button says Replace server', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    handlers['POST /integrations/mcp/servers'] = () => [200, { created: false, definitions_sha256: 'a'.repeat(64), server: null }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('deepwiki', 'https://mcp.deepwiki.com/mcp');
    expect(within(addCard()).getByText('A server named deepwiki exists. Adding it again replaces the server: its tools are checked '
      + 'again, it is switched on, and background work is turned off. Its call timeout goes back to 30 s.')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await fireEvent.click(await within(addCard()).findByRole('button', { name: 'Replace server' }));
    expect(await within(addCard()).findByText('Replaced deepwiki. It is switched on, and background work is off.')).toBeTruthy();
    expect(within(addCard()).queryByRole('button', { name: 'Add server' })).toBeNull();
  });

  it('a background confirmation left open is dropped when its server is replaced', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    handlers['POST /integrations/mcp/servers'] = () => [200, { created: false, definitions_sha256: 'a'.repeat(64), server: null }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByLabelText('Allow in background work'));
    expect(within(card('deepwiki')).getByRole('button', { name: 'Yes, allow in background work' })).toBeTruthy();
    await fill('deepwiki', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await fireEvent.click(await within(addCard()).findByRole('button', { name: 'Replace server' }));
    await within(addCard()).findByText('Replaced deepwiki. It is switched on, and background work is off.');
    // the question was about the server that was replaced: it must not be answerable for the new one
    expect(within(card('deepwiki')).queryByRole('button', { name: 'Yes, allow in background work' })).toBeNull();
  });

  it('the buttons are disabled while the check and the add are in flight', async () => {
    const probe = held();
    const add = held();
    handlers['POST /integrations/mcp/probe'] = () => probe.promise;
    handlers['POST /integrations/mcp/servers'] = () => add.promise;
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    expect((await screen.findByRole('button', { name: 'Checking…' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText('Name') as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText('Address') as HTMLInputElement).disabled).toBe(true);
    probe.resolve([200, PROBE()]);
    await fireEvent.click(await within(addCard()).findByRole('button', { name: 'Add server' }));
    expect((within(addCard()).getByRole('button', { name: 'Adding…' }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(addCard()).getByRole('button', { name: 'Check server' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText('Name') as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText('Address') as HTMLInputElement).disabled).toBe(true);
    add.resolve([201, { created: true, definitions_sha256: 'a'.repeat(64), server: null }]);
    expect(await within(addCard()).findByText('Added wiki2. It is listed above.')).toBeTruthy();
    expect((screen.getByLabelText('Name') as HTMLInputElement).disabled).toBe(false);
  });
});

// Review fixes: names that are Object.prototype members, stale notes, focus, a 2xx that is not JSON, raw names as data.
const HOSTILE = ['constructor', 'toString', 'valueOf', 'hasOwnProperty', '__proto__', 'plain'];

describe('Integrations: names a server chose', () => {
  it('B1: a probed tool named like a prototype member is not ticked, and nothing ticked is sent as deny, every name a key', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, {
      tools: HOSTILE.map((tool) => ({ tool, name: `mcp__evil__${tool}`, description: 'd', schema: '{}', skip_reason: null })),
      definitions_sha256: 'a'.repeat(64),
    }];
    handlers['POST /integrations/mcp/servers'] = () => [201, { created: true, definitions_sha256: 'a'.repeat(64), server: null }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.input(screen.getByLabelText('Name'), { target: { value: 'evil' } });
    await fireEvent.input(screen.getByLabelText('Address'), { target: { value: 'https://evil.example.com/mcp' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    const add = screen.getByRole('region', { name: 'Add a server' });
    await within(add).findByText('6 tools listed');
    expect((within(add).getAllByRole('checkbox') as HTMLInputElement[]).map((b) => b.checked)).toEqual(HOSTILE.map(() => false));
    await fireEvent.click(within(add).getByRole('checkbox', { name: 'Allow __proto__' }));
    await fireEvent.click(within(add).getByRole('button', { name: 'Add server' }));
    await within(add).findByText('Added evil. It is listed above.');
    const post = changes().find((c) => c.url === '/integrations/mcp/servers')!;
    expect(post.raw).toBe('{"name":"evil","url":"https://evil.example.com/mcp","tools":{"constructor":"deny","toString":"deny",'
      + '"valueOf":"deny","hasOwnProperty":"deny","__proto__":"allow","plain":"deny"},"expected_definitions_sha256":"' + 'a'.repeat(64) + '"}');
  });

  it('B1: a stored tool named __proto__ or constructor is changed by its own name, sent as an own key', async () => {
    state = LIST();
    state.servers[0].tools = [tool('__proto__'), tool('constructor')];
    patching('deepwiki');
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    expect(within(row(deepwiki, '__proto__')).getByText('not allowed')).toBeTruthy();
    await fireEvent.click(within(deepwiki).getByRole('button', { name: 'Allow __proto__' }));
    expect((await within(row(card('deepwiki'), '__proto__')).findByRole('status')).textContent).toBe('Allowed.');
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Allow constructor' }));
    expect((await within(row(card('deepwiki'), 'constructor')).findByRole('status')).textContent).toBe('Allowed.');
    expect(changes().map((c) => c.raw)).toEqual(['{"tools":{"__proto__":"allow"}}', '{"tools":{"constructor":"allow"}}']);
    expect(within(row(card('deepwiki'), 'constructor')).getByText('in use')).toBeTruthy();
  });

  it('S1: a server named constructor renders, and every one of its controls works', async () => {
    state = LIST();
    state.servers = [{ ...state.servers[0], name: 'constructor' }, { ...state.servers[1], name: 'hasownproperty' }];
    patching('constructor');
    handlers['POST /integrations/mcp/servers/constructor/refresh'] = () =>
      [200, { added: [], removed: [], changed: ['read_wiki'], definitions_sha256: 'c'.repeat(64), server: null }];
    handlers['DELETE /integrations/mcp/servers/constructor'] = () => {
      state.servers = state.servers.slice(1);
      return [200, { deleted: 'constructor' }];
    };
    render(Integrations);
    const c = await screen.findByRole('region', { name: 'constructor' });
    expect(screen.getByRole('region', { name: 'hasownproperty' })).toBeTruthy();
    const enabled = (name: string) => !(within(card('constructor')).getByRole('button', { name }) as HTMLButtonElement).disabled;
    expect(enabled('Switch off')).toBe(true); // busy is not read from a prototype
    await fireEvent.click(within(c).getByRole('button', { name: 'Switch off' }));
    expect((await within(card('constructor')).findByRole('status')).textContent).toBe('Switched off: its tools are no longer offered to the agent.');
    await fireEvent.click(within(card('constructor')).getByRole('button', { name: 'Refresh tools' }));
    expect(await within(card('constructor')).findByRole('list', { name: 'What the refresh changed' })).toBeTruthy();
    const said = (text: string) => waitFor(() => expect(within(card('constructor')).getByRole('status').textContent).toBe(text));
    await said('Read the tool list again.');
    await fireEvent.click(within(card('constructor')).getByRole('checkbox', { name: 'Allow in background work' }));
    await fireEvent.click(within(card('constructor')).getByRole('button', { name: 'Yes, allow in background work' }));
    await said('Background work may now call its tools.');
    await fireEvent.click(within(card('constructor')).getByRole('button', { name: 'Remove' }));
    await fireEvent.click(within(card('constructor')).getByRole('button', { name: 'Yes, remove' }));
    expect(await screen.findByText('Removed constructor.')).toBeTruthy();
    expect(changes().map(sent)).toEqual([
      { method: 'PATCH', url: '/integrations/mcp/servers/constructor', body: { enabled: false } },
      { method: 'POST', url: '/integrations/mcp/servers/constructor/refresh', body: undefined },
      { method: 'PATCH', url: '/integrations/mcp/servers/constructor', body: { background: true } },
      { method: 'DELETE', url: '/integrations/mcp/servers/constructor', body: undefined },
    ]);
  });

  it('a server name that needs no encoding is sent unchanged in the path', async () => {
    state = LIST();
    state.servers[0].name = 'my_srv-1';
    patching('my_srv-1');
    render(Integrations);
    await fireEvent.click(within(await screen.findByRole('region', { name: 'my_srv-1' })).getByRole('button', { name: 'Switch off' }));
    await waitFor(() => expect(changes()).toHaveLength(1));
    expect(changes()[0].url).toBe('/integrations/mcp/servers/my_srv-1');
  });

  it('a very long tool name is cut to 80 characters on screen, the whole name in its title and its control\'s name', async () => {
    const long = `t${'x'.repeat(199)}`;
    listAnswer = () => {
      const d = LIST();
      d.servers[0].tools = [tool(long)];
      return [200, d];
    };
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    const code = deepwiki.querySelector('.tool-head code') as HTMLElement;
    expect(code.textContent).toBe(`${long.slice(0, 79)}…`);
    expect(code.getAttribute('title')).toBe(long);
    expect(within(deepwiki).getByRole('button', { name: `Allow ${long}` })).toBeTruthy();
  });

  it('a tool named like the screen\'s own words is shown as data in a refresh result', async () => {
    handlers['POST /integrations/mcp/servers/deepwiki/refresh'] = () =>
      [200, { added: ['in background work'], removed: [], changed: [], definitions_sha256: 'c'.repeat(64), server: null }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Refresh tools' }));
    const list = await within(card('deepwiki')).findByRole('list', { name: 'What the refresh changed' });
    expect(list.querySelector('li code')?.textContent).toBe('in background work');
  });
});

describe('Integrations: notes stay true', () => {
  it('S3: a tool\'s note goes when the list shows another state for it, and on a refresh', async () => {
    patching('deepwiki');
    handlers['POST /integrations/mcp/servers/deepwiki/refresh'] = () =>
      [200, { added: [], removed: [], changed: [], definitions_sha256: 'c'.repeat(64), server: null }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Allow read_wiki' }));
    expect((await within(row(card('deepwiki'), 'read_wiki')).findByRole('status')).textContent).toBe('Allowed.');
    // Changed elsewhere: the next reload shows it not allowed, and "Allowed." must not stay beside that.
    const t = state.servers[0].tools.find((x) => x.tool === 'read_wiki')!;
    Object.assign(t, { permission: 'deny', registered: false, skip_reason: 'its permission is deny' });
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(within(row(card('deepwiki'), 'read_wiki')).queryByRole('status')).toBeNull());
    expect(within(row(card('deepwiki'), 'read_wiki')).getByText('not allowed')).toBeTruthy();
    // A refresh of the server's tools clears its tools' notes.
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Allow read_wiki' }));
    await within(row(card('deepwiki'), 'read_wiki')).findByRole('status');
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Refresh tools' }));
    await within(card('deepwiki')).findByRole('list', { name: 'What the refresh changed' });
    expect(within(row(card('deepwiki'), 'read_wiki')).queryByRole('status')).toBeNull();
  });

  it('S3: a removed server\'s notes do not come back when a server of that name is added again', async () => {
    patching('deepwiki');
    let removed = false;
    handlers['DELETE /integrations/mcp/servers/deepwiki'] = () => { removed = true; return [200, { deleted: 'deepwiki' }]; };
    const listed = listAnswer;
    listAnswer = () => {
      const [status, d] = listed() as [number, ReturnType<typeof LIST>];
      return [status, removed ? { ...d, servers: d.servers.slice(1) } : d];
    };
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Allow read_wiki' }));
    await within(row(card('deepwiki'), 'read_wiki')).findByRole('status');
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Remove' }));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Yes, remove' }));
    await screen.findByText('Removed deepwiki.');
    removed = false; // added again elsewhere, with the tool allowed as before
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    const again = await screen.findByRole('region', { name: 'deepwiki' });
    expect(within(again).queryByRole('status')).toBeNull();
  });
});

describe('Integrations: focus', () => {
  it('S4: a confirmation takes the focus, and Cancel gives it back to the control that opened it', async () => {
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    within(deepwiki).getByRole('button', { name: 'Remove' }).focus();
    await fireEvent.click(within(deepwiki).getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(document.activeElement?.textContent?.trim()).toBe('Yes, remove'));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(document.activeElement).toBe(within(card('deepwiki')).getByRole('button', { name: 'Remove' })));

    const box = within(card('deepwiki')).getByRole('checkbox', { name: 'Allow in background work' });
    await fireEvent.click(box);
    await waitFor(() => expect(document.activeElement?.textContent?.trim()).toBe('Yes, allow in background work'));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(document.activeElement).toBe(box));
  });

  it('S4: after a confirmed change the focus is on the checkbox; after a removal, on the page title', async () => {
    patching('deepwiki');
    handlers['DELETE /integrations/mcp/servers/deepwiki'] = () => {
      state.servers = state.servers.slice(1);
      return [200, { deleted: 'deepwiki' }];
    };
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('checkbox', { name: 'Allow in background work' }));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Yes, allow in background work' }));
    await within(card('deepwiki')).findByRole('status');
    await waitFor(() => expect(document.activeElement).toBe(within(card('deepwiki')).getByRole('checkbox', { name: 'Allow in background work' })));

    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Remove' }));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Yes, remove' }));
    await screen.findByText('Removed deepwiki.');
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { level: 1 })));
  });
});

describe('Integrations: an answer that is not JSON', () => {
  it('S5: a list answered 200 with a page shows the failure, not "Loading…"', async () => {
    listAnswer = () => [200, '<html><body>Sign in</body></html>'];
    render(Integrations);
    expect((await screen.findByRole('alert')).textContent)
      .toBe('Could not load the MCP servers: The answer could not be read: it is not JSON (HTTP 200).');
    expect(screen.queryByText('Loading…')).toBeNull();
  });

  it('S5: a check answered 200 with a page says so, and lists no tools', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, '<html>Sign in</html>'];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.input(screen.getByLabelText('Name'), { target: { value: 'wiki2' } });
    await fireEvent.input(screen.getByLabelText('Address'), { target: { value: 'https://mcp.deepwiki.com/mcp' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    const add = screen.getByRole('region', { name: 'Add a server' });
    expect((await within(add).findByRole('alert')).textContent).toBe('The answer could not be read: it is not JSON (HTTP 200).');
    expect(within(add).queryByRole('button', { name: 'Add server' })).toBeNull();
  });
});

describe('Integrations: what is sent', () => {
  it('every POST, PATCH and DELETE carries exactly the JSON content type (the routes refuse anything else with 415)', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    handlers['POST /integrations/mcp/servers'] = () => [201, { created: true, definitions_sha256: 'a'.repeat(64), server: null }];
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => [200, { server: null }];
    handlers['POST /integrations/mcp/servers/deepwiki/refresh'] = () => [200, { added: [], removed: [], changed: [], definitions_sha256: 'c', server: null }];
    handlers['DELETE /integrations/mcp/servers/files'] = () => [200, { deleted: 'files' }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    const settle = () => waitFor(() => expect((within(card('deepwiki')).getByRole('button', { name: 'Refresh tools' }) as HTMLButtonElement).disabled).toBe(false));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Allow read_wiki' }));
    await settle();
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Refresh tools' }));
    await settle();
    await fireEvent.click(within(card('files')).getByRole('button', { name: 'Remove' }));
    await fireEvent.click(within(card('files')).getByRole('button', { name: 'Yes, remove' }));
    await screen.findByText('Removed files.');
    await fireEvent.input(screen.getByLabelText('Name'), { target: { value: 'wiki2' } });
    await fireEvent.input(screen.getByLabelText('Address'), { target: { value: 'https://mcp.deepwiki.com/mcp' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Add server' }));
    await screen.findByText('Added wiki2. It is listed above.');
    expect(changes().map((c) => c.method)).toEqual(['PATCH', 'POST', 'DELETE', 'POST', 'POST']);
    for (const c of changes()) expect(c.headers, `${c.method} ${c.url}`).toEqual({ 'Content-Type': 'application/json' });
    for (const c of calls.filter((x) => x.method === 'GET')) expect(c.headers).toBeUndefined();
  });

  it('server text is shown as text, never as markup', () => {
    const src = readFileSync(join(__dirname, 'Integrations.svelte'), 'utf-8');
    expect(markupOf(src)).not.toMatch(/\{@html/);
  });
});

describe('Integrations: style', () => {
  const css = styleBlocks(readFileSync(join(__dirname, 'Integrations.svelte'), 'utf-8')).replace(/\r\n/g, '\n');
  it('controls reach 44px on a phone, and a field is 44px tall', () => {
    const phone = css.slice(css.indexOf('@media (max-width: 768px)'));
    expect(phone).toMatch(/\.check, \.more \{ min-height: 2\.75rem; \}/);
    expect(phone).toMatch(/\.schema summary \{ line-height: 2\.75rem;/);
    expect(css).toMatch(/\.field input \{ height: 2\.75rem;/);
    const markup = markupOf(readFileSync(join(__dirname, 'Integrations.svelte'), 'utf-8'));
    for (const tag of markup.match(/<Button\b[^>]*>/g) ?? []) expect(tag).toContain('max-md:min-h-11');
  });
});

// F112 PR 2c: stored keys. The value is made up; it must never be shown back, kept or sent anywhere but the routes.
const KEY = 'Bearer test-key-0000000000000000';
const NO_KEY_FILE = 'A key cannot be stored here: no key file is configured on the host (NOUS_MCP_CLIENT_KEY_FILE).';
const keyed = (over: Record<string, unknown> = {}) => {
  const d = LIST();
  Object.assign(d.servers[0].auth, { kind: 'headers', header_names: ['Authorization', 'X-API-Key'], ...over });
  return d;
};

describe('Integrations: a key for a new server', () => {
  async function fill(name: string, url: string) {
    await fireEvent.input(screen.getByLabelText('Name'), { target: { value: name } });
    await fireEvent.input(screen.getByLabelText('Address'), { target: { value: url } });
  }
  const addCard = () => screen.getByRole('region', { name: 'Add a server' });
  const valueField = () => within(addCard()).getByLabelText('Header 1 value') as HTMLInputElement;

  async function typeKey(value = KEY) {
    await fireEvent.click(within(addCard()).getByRole('radio', { name: 'API key or header' }));
    await fireEvent.input(valueField(), { target: { value } });
  }

  it('the value is dots until Show, and Check and Add send the typed headers; a success drops them', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    handlers['POST /integrations/mcp/servers'] = () => [201, { created: true, definitions_sha256: 'a'.repeat(64), credentials: 'set', server: null }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    expect(within(addCard()).getByRole('radio', { name: 'None' })).toHaveProperty('checked', true);
    expect((within(addCard()).getByRole('radio', { name: 'Sign in (OAuth)' }) as HTMLInputElement).disabled).toBe(true);
    expect(within(addCard()).getByText('Sign in (OAuth) arrives in the next update.')).toBeTruthy();
    await typeKey();
    expect((within(addCard()).getByLabelText('Header 1 name') as HTMLInputElement).value).toBe('Authorization');
    expect(valueField().type).toBe('password');
    expect(valueField().getAttribute('autocomplete')).toBe('off');
    expect(within(addCard()).getByText('Most servers want Authorization with the value "Bearer <your key>".')).toBeTruthy();
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Show the value of header 1' }));
    expect(valueField().type).toBe('text');
    expect(valueField().value).toBe(KEY); // only what was just typed
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Hide the value of header 1' }));
    expect(valueField().type).toBe('password');

    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    expect(sent(changes()[0]).body).toEqual({ name: 'wiki2', url: 'https://mcp.deepwiki.com/mcp', headers: { Authorization: KEY } });
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Add server' }));
    await within(addCard()).findByText('Added wiki2. It is listed above.');
    expect((changes()[1].body as Record<string, unknown>).headers).toEqual({ Authorization: KEY });
    expect(within(addCard()).getByRole('radio', { name: 'None' })).toHaveProperty('checked', true);
    expect(within(addCard()).queryByLabelText('Header 1 value')).toBeNull();
    await fireEvent.click(within(addCard()).getByRole('radio', { name: 'API key or header' }));
    expect(valueField().value).toBe(''); // nothing typed comes back
  });

  it('rows are added and dropped, at most eight', async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await typeKey();
    for (let i = 0; i < 7; i += 1) await fireEvent.click(within(addCard()).getByRole('button', { name: 'Add another header' }));
    expect(within(addCard()).getByLabelText('Header 8 value')).toBeTruthy();
    expect(within(addCard()).queryByRole('button', { name: 'Add another header' })).toBeNull();
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Drop header 8' }));
    expect(within(addCard()).queryByLabelText('Header 8 value')).toBeNull();
    expect(valueField().value).toBe(KEY);
  });

  it('a header the rules refuse is said beside the rows, without its value, and nothing is sent', async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await typeKey(` ${KEY}`);
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    const alert = within(addCard()).getByRole('alert');
    expect(alert.textContent).toBe('Header 1: a value may not start or end with a space.');
    expect(changes()).toEqual([]);
  });

  it('a value of mostly punctuation is refused before anything is sent', async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await typeKey('Bearer ----++++////====');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    expect(within(addCard()).getByRole('alert').textContent).toBe('Header 1: a value needs at least 8 letters or digits.');
    expect(changes()).toEqual([]);
  });

  it('a 409 for a missing key file drops what was typed, says so, and checks nothing again', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    handlers['POST /integrations/mcp/servers'] = () => [409, { error: 'no key file is configured on the host (NOUS_MCP_CLIENT_KEY_FILE)' }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await typeKey();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await fireEvent.click(await within(addCard()).findByRole('button', { name: 'Add server' }));
    expect((await within(addCard()).findByRole('alert')).textContent).toBe('No key file is configured on the host (NOUS_MCP_CLIENT_KEY_FILE).');
    expect(within(addCard()).getByRole('radio', { name: 'None' })).toHaveProperty('checked', true);
    expect(within(addCard()).queryByLabelText('Header 1 value')).toBeNull();
    expect(changes().filter((c) => c.url === '/integrations/mcp/probe')).toHaveLength(1);
  });

  it('a changed listing (409 with the new hash) is checked again with the same headers', async () => {
    let probes = 0;
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE(probes++ ? 'b'.repeat(64) : 'a'.repeat(64))];
    handlers['POST /integrations/mcp/servers'] = () =>
      [409, { error: "the server's tool definitions changed since they were shown: review them again", definitions_sha256: 'b'.repeat(64) }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await typeKey();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await fireEvent.click(await within(addCard()).findByRole('button', { name: 'Add server' }));
    await waitFor(() => expect(probes).toBe(2));
    const checks = changes().filter((c) => c.url === '/integrations/mcp/probe').map((c) => (c.body as Record<string, unknown>).headers);
    expect(checks).toEqual([{ Authorization: KEY }, { Authorization: KEY }]);
    expect(valueField().value).toBe(KEY);
  });

  it('a key edited after the check needs a new check before adding', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await typeKey();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    await fireEvent.input(valueField(), { target: { value: 'Bearer test-key-1111111111111111' } });
    expect(within(addCard()).queryByRole('button', { name: 'Add server' })).toBeNull();
    expect(within(addCard()).getByText('The key changed since the check: check the server again.')).toBeTruthy();
  });

  it('after Check then None, no typed value is kept anywhere: not in the fields, the page or the check', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await typeKey();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    await fireEvent.click(within(addCard()).getByRole('radio', { name: 'None' }));
    expect(within(addCard()).queryByRole('button', { name: 'Add server' })).toBeNull(); // the check had a key: stale
    expect(document.body.innerHTML).not.toContain('test-key');
    for (const input of Array.from(document.querySelectorAll('input'))) expect(input.value).not.toContain('test-key');
    await fireEvent.click(within(addCard()).getByRole('radio', { name: 'API key or header' }));
    expect(valueField().value).toBe('');
    // Review of #917: the check keeps a count of edits, never the values (it once kept them as JSON).
    const src = readFileSync(join(__dirname, 'Integrations.svelte'), 'utf-8');
    expect(src).not.toMatch(/JSON\.stringify\(newHeaders\)/);
    expect(src).toMatch(/probe = \{ name, url, edits, result \}/);
  });

  it('without a key file on the host the choice is disabled, with one sentence naming the setting', async () => {
    listAnswer = () => [200, { ...LIST(), secrets_available: false }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    expect((within(addCard()).getByRole('radio', { name: 'API key or header' }) as HTMLInputElement).disabled).toBe(true);
    expect(within(addCard()).getByText(NO_KEY_FILE)).toBeTruthy();
    expect((within(card('deepwiki')).getByRole('button', { name: 'Add key' }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(card('deepwiki')).getByText(NO_KEY_FILE)).toBeTruthy();
  });

  it('a typed value is never written to the browser storage', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await typeKey();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    expect(setItem.mock.calls.filter((args) => args.join(' ').includes('test-key'))).toEqual([]);
    setItem.mockRestore();
  });
});

describe('Integrations: a stored key on a card', () => {
  it('names the headers, never a value; Replace key sends the new headers, Cancel drops them', async () => {
    listAnswer = () => [200, keyed()];
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => [200, { credentials: 'replaced', server: null }];
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    expect(within(deepwiki).getByText('Key set: Authorization, X-API-Key')).toBeTruthy();
    expect(within(card('files')).getByText('No key stored.')).toBeTruthy();
    await fireEvent.click(within(deepwiki).getByRole('button', { name: 'Replace key' }));
    const value = within(card('deepwiki')).getByLabelText('Header 1 value') as HTMLInputElement;
    expect(value.value).toBe(''); // the rows open empty: nothing stored comes back
    await fireEvent.input(value, { target: { value: KEY } });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Cancel' }));
    expect(within(card('deepwiki')).queryByLabelText('Header 1 value')).toBeNull();
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Replace key' }));
    expect((within(card('deepwiki')).getByLabelText('Header 1 value') as HTMLInputElement).value).toBe('');
    expect(changes()).toEqual([]);

    await fireEvent.input(within(card('deepwiki')).getByLabelText('Header 1 value'), { target: { value: KEY } });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Save key' }));
    expect((await within(card('deepwiki')).findByRole('status')).textContent).toBe('Key saved: the next call to this server sends it.');
    expect(changes().map(sent)).toEqual([{ method: 'PATCH', url: '/integrations/mcp/servers/deepwiki', body: { headers: { Authorization: KEY } } }]);
    expect(within(card('deepwiki')).queryByLabelText('Header 1 value')).toBeNull();
  });

  it('a value shown before Cancel or Save is dots again when the rows open next', async () => {
    listAnswer = () => [200, keyed()];
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => [200, { credentials: 'replaced', server: null }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    const field = () => within(card('deepwiki')).getByLabelText('Header 1 value') as HTMLInputElement;
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Replace key' }));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Show the value of header 1' }));
    expect(field().type).toBe('text');
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Cancel' }));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Replace key' }));
    expect(field().type).toBe('password');
    await fireEvent.input(field(), { target: { value: KEY } });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Show the value of header 1' }));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Save key' }));
    await within(card('deepwiki')).findByRole('status');
    await waitFor(() => expect((within(card('deepwiki')).getByRole('button', { name: 'Replace key' }) as HTMLButtonElement).disabled).toBe(false));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Replace key' }));
    expect(field().type).toBe('password');
  });

  it('dropping a row puts every value back to dots, so no shown flag moves onto another row', async () => {
    listAnswer = () => [200, keyed()];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Replace key' }));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Add another header' }));
    await fireEvent.input(within(card('deepwiki')).getByLabelText('Header 2 value'), { target: { value: 'test-key-2222' } });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Show the value of header 1' }));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Drop header 1' }));
    const left = within(card('deepwiki')).getByLabelText('Header 1 value') as HTMLInputElement;
    expect(left.value).toBe('test-key-2222');
    expect(left.type).toBe('password');
  });

  it('Remove key asks first, then sends headers null', async () => {
    listAnswer = () => [200, keyed()];
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => [200, { credentials: 'removed', server: null }];
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(deepwiki).getByRole('button', { name: 'Remove key' }));
    const ask = within(card('deepwiki')).getByRole('group', { name: 'Confirm removing the key of deepwiki' });
    await fireEvent.click(within(ask).getByRole('button', { name: 'Cancel' }));
    expect(changes()).toEqual([]);
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Remove key' }));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Yes, remove the key' }));
    expect((await within(card('deepwiki')).findByRole('status')).textContent).toBe('Key removed: calls to this server go without it.');
    expect(changes().map(sent)).toEqual([{ method: 'PATCH', url: '/integrations/mcp/servers/deepwiki', body: { headers: null } }]);
  });

  it('a refused or unreadable key is said in words, with the reason', async () => {
    listAnswer = () => [200, keyed({ status: 'credentials_refused' })];
    render(Integrations);
    expect(within(await screen.findByRole('region', { name: 'deepwiki' })).getByText('the server refused the key', { selector: '.status-pill' })).toBeTruthy();
    cleanup();
    listAnswer = () => [200, keyed({ status: 'unavailable', reason: 'stored with another key' })];
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    expect(within(deepwiki).getByText('key cannot be read on this host', { selector: '.status-pill' })).toBeTruthy();
    expect(within(deepwiki).getByText('stored with another key')).toBeTruthy();
    cleanup();
    listAnswer = () => {
      const d = LIST();
      d.servers[0].connected = false;
      return [200, d];
    };
    render(Integrations);
    expect(within(await screen.findByRole('region', { name: 'deepwiki' })).getByText('connects on first use', { selector: '.status-pill' })).toBeTruthy();
  });
});
