import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Integrations from './Integrations.svelte';
import { markupOf, styleBlocks } from '../lib/testing/css';

// F112 PR 2b: the Integrations screen over the /integrations/mcp routes. The fixtures have the shapes
// nous/api/mcp_client_routes.py answers with; every value is made up.

type Answer = [number, unknown];
type Call = { url: string; method: string; headers: Record<string, string> | undefined; body: unknown };

const tool = (name: string, over: Record<string, unknown> = {}) => ({
  tool: name, name: `mcp__deepwiki__${name}`, permission: 'deny', registered: false, skip_reason: 'its permission is deny',
  blocked_by_ceiling: false, description: `${name} description`, schema: '{"type":"object","properties":{}}', ...over,
});

const LIST = () => ({
  ceilings: { pinned_servers: true, background_servers: false },
  servers: [
    {
      name: 'deepwiki', url: 'https://mcp.deepwiki.com/mcp', enabled: true, background: false, background_in_effect: false,
      background_blocked_by_ceiling: false, call_timeout_seconds: 30, connected: true, blocked_by_ceiling: false, skip_reason: null,
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
      skip_reason: 'the server is disabled',
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

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
    calls.push({ url, method, headers: init.headers as Record<string, string> | undefined, body });
    const [status, payload] = method === 'GET' && url === '/integrations/mcp/servers'
      ? listAnswer()
      : await (handlers[`${method} ${url}`]?.(body) ?? [500, { error: `unexpected ${method} ${url}` }]);
    if (status === 0) throw new TypeError('Failed to fetch');
    return new Response(payload === undefined ? '' : JSON.stringify(payload), { status });
  }));
}

const lists = () => calls.filter((c) => c.method === 'GET').length;
const changes = () => calls.filter((c) => c.method !== 'GET');
const card = (name: string) => screen.getByRole('region', { name });
const row = (scope: HTMLElement, toolName: string) => within(scope).getByText(toolName, { selector: 'code' }).closest('li') as HTMLElement;

/** A promise the test resolves itself, to hold a request in flight. */
function held(): { promise: Promise<Answer>; resolve: (a: Answer) => void } {
  let resolve!: (a: Answer) => void;
  const promise = new Promise<Answer>((r) => { resolve = r; });
  return { promise, resolve };
}

beforeEach(() => {
  calls = [];
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
    expect(within(row(files, 'list')).getByText('the server is disabled')).toBeTruthy();
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
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => [200, { server: null }];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    const before = lists();
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Switch off' }));
    await waitFor(() => expect(lists()).toBe(before + 1));
    expect(changes()).toMatchObject([{ method: 'PATCH', url: '/integrations/mcp/servers/deepwiki', body: { enabled: false } }]);
    expect(within(card('deepwiki')).getByRole('status').textContent).toBe('Switched off: its tools are no longer offered to the agent.');
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
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => [200, { server: null }];
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
    expect(changes()[0]).toMatchObject({ method: 'PATCH', url: '/integrations/mcp/servers/deepwiki', body: { background: true } });
    expect((await within(card('deepwiki')).findByRole('status')).textContent).toBe('Background work may now call its tools.');
  });

  it('turning background work off needs no confirmation', async () => {
    handlers['PATCH /integrations/mcp/servers/files'] = () => [200, { server: null }];
    render(Integrations);
    const files = await screen.findByRole('region', { name: 'files' });
    const box = within(files).getByRole('checkbox', { name: 'Allow in background work' }) as HTMLInputElement;
    expect(box.checked).toBe(true);
    await fireEvent.click(box);
    await waitFor(() => expect(changes()).toHaveLength(1));
    expect(changes()[0].body).toEqual({ background: false });
  });

  it('a tool is allowed or not allowed by its own name', async () => {
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => [200, { server: null }];
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
    expect(changes()[0]).toMatchObject({ method: 'POST', url: '/integrations/mcp/servers/deepwiki/refresh', body: undefined });
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
    expect(changes()).toMatchObject([{ method: 'DELETE', url: '/integrations/mcp/servers/deepwiki' }]);
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
    expect(changes()[0]).toMatchObject({ method: 'POST', url: '/integrations/mcp/probe', body: { name: 'deepwiki', url: 'https://mcp.deepwiki.com/mcp' } });

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
    expect(changes()[1]).toMatchObject({
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

  it('a name already taken says that adding replaces the server', async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('deepwiki', 'https://mcp.deepwiki.com/mcp');
    expect(within(addCard()).getByText(/A server named deepwiki exists: adding replaces its address and tools/)).toBeTruthy();
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
    probe.resolve([200, PROBE()]);
    await fireEvent.click(await within(addCard()).findByRole('button', { name: 'Add server' }));
    expect((within(addCard()).getByRole('button', { name: 'Adding…' }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(addCard()).getByRole('button', { name: 'Check server' }) as HTMLButtonElement).disabled).toBe(true);
    add.resolve([200, { created: false, definitions_sha256: 'a'.repeat(64), server: null }]);
    expect(await within(addCard()).findByText('Replaced wiki2.')).toBeTruthy();
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
