import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Integrations from './Integrations.svelte';
import { initRouter } from '../lib/router';
import { markupOf, parseBlocks, styleBlocks } from '../lib/testing/css';

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
      background_blocked_by_ceiling: false, images: false, call_timeout_seconds: 30, connected: true, blocked_by_ceiling: false, skip_reason: null,
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
      background_blocked_by_ceiling: true, images: true, call_timeout_seconds: 30, connected: false, blocked_by_ceiling: false,
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
    const b = body as { enabled?: boolean; background?: boolean; images?: boolean; tools?: Record<string, string> };
    if (b.enabled !== undefined) s.enabled = b.enabled;
    if (b.background !== undefined) s.background = b.background;
    if (b.images !== undefined) s.images = b.images;
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

// The screen's pane follows the address. initRouter() applies location.hash to the router's stores at once (the
// hashchange jsdom fires later parses to the same place), so a test moves to an address deterministically.
const go = (hash: string) => { location.hash = hash; initRouter(); };
/** The width under which the screen is one column: the script's matchMedia query and the stylesheet's breakpoint. */
const ONE_COLUMN = '(max-width: 1040px)';
const LIST_ADDRESS = '#/integrations';
const ADD_ADDRESS = '#/integrations/add';
const serverAddress = (name: string) => `#/integrations/s/${encodeURIComponent(name)}`;
/** Show one server's page and wait for its region. */
async function openServer(name: string) {
  go(serverAddress(name));
  return screen.findByRole('region', { name });
}
/** Show the "Add a server" form and wait for it. */
async function openForm() {
  go(ADD_ADDRESS);
  return screen.findByRole('region', { name: 'Add a server' });
}
async function fill(name: string, url: string) {
  await openForm();
  await fireEvent.input(screen.getByLabelText('Name'), { target: { value: name } });
  await fireEvent.input(screen.getByLabelText('Address'), { target: { value: url } });
}
const addCard = () => screen.getByRole('region', { name: 'Add a server' });
const nav = () => screen.getByRole('navigation', { name: 'MCP servers' });
/** The list's link to a server, by its address. */
const link = (name: string) => nav().querySelector(`a[href="${serverAddress(name)}"]`) as HTMLAnchorElement | null;
/** A POST /integrations/mcp/servers that stores the server in `state` (answering `created` as the route would), and
 * a list that answers with `state`: after an add the pane shows the server it made. */
function adding(over: Record<string, unknown> = {}) {
  listAnswer = () => [200, JSON.parse(JSON.stringify(state))];
  handlers['POST /integrations/mcp/servers'] = (body) => {
    const b = body as { name: string; url: string; headers?: Record<string, string> };
    const at = state.servers.findIndex((s) => s.name === b.name);
    const s = { ...LIST().servers[0], name: b.name, url: b.url, tools: [], ...over } as (typeof state.servers)[number];
    if (at >= 0) state.servers[at] = s;
    else state.servers.push(s);
    const credentials = b.headers && at < 0 ? { credentials: 'set' } : {};
    return [at >= 0 ? 200 : 201, { created: at < 0, definitions_sha256: 'a'.repeat(64), ...credentials, server: null }];
  };
}

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
  go(LIST_ADDRESS); // every test starts at the screen's own address, the router live
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
    expect(within(await screen.findByRole('navigation', { name: 'MCP servers' })).getByText('No MCP servers yet.')).toBeTruthy();
    expect(screen.getByText('No limit is set on the host: what you allow here is what the agent may use.')).toBeTruthy();
    // With no server the pane is the form, at the screen's own address.
    expect(screen.getByRole('region', { name: 'Add a server' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Check server' })).toBeTruthy();
    expect(within(nav()).getByRole('link', { name: 'Add server' }).getAttribute('aria-current')).toBe('page');
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
    // What the server wrote about a tool shows once its row is opened.
    await fireEvent.click(within(row(deepwiki, 'read_wiki')).getByRole('button', { name: 'Details of read_wiki' }));
    expect(within(row(card('deepwiki'), 'read_wiki')).getByText('mcp__deepwiki__read_wiki')).toBeTruthy();
    expect(within(row(card('deepwiki'), 'read_wiki')).getByText('read_wiki description')).toBeTruthy();
    expect(within(card('deepwiki')).getByRole('button', { name: 'Switch off' })).toBeTruthy();

    const files = await openServer('files');
    expect(within(files).getByText('switched off', { selector: '.status-pill' })).toBeTruthy();
    expect(within(row(files, 'list')).getByText('the server is switched off')).toBeTruthy(); // the route says "disabled"
    expect(within(files).queryByText(/the server is disabled/)).toBeNull();
    expect(within(files).getByText(/Background work is blocked by the server's host settings/)).toBeTruthy();
    expect(within(files).getByRole('button', { name: 'Switch on' })).toBeTruthy();
  });

  it('says plainly which host limits are in effect', async () => {
    render(Integrations);
    expect(await screen.findByRole('heading', { name: 'Limits set on the host are in effect' })).toBeTruthy();
    expect(screen.getByText(/Only the servers and tools pinned on the host/)).toBeTruthy();
    expect(screen.queryByText(/Background work can use only the servers listed/)).toBeNull();
  });

  it('a tool is one closed line; opened, it shows the whole description and a collapsed schema, and closes again', async () => {
    const long = `${'word '.repeat(100)}end`; // 503 characters: the old clamp cut at 180
    listAnswer = () => {
      const d = LIST();
      d.servers[0].tools[1] = tool('read_wiki', { description: long });
      return [200, d];
    };
    render(Integrations);
    const r = row(await screen.findByRole('region', { name: 'deepwiki' }), 'read_wiki');
    const open = within(r).getByRole('button', { name: 'Details of read_wiki' });
    expect(open.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText(long)).toBeNull();
    expect(screen.queryByText(/Offered as/)).toBeNull();
    expect(document.querySelector('details.schema')).toBeNull();
    expect(screen.getAllByRole('button', { name: /^Details of / }).map((b) => b.getAttribute('aria-expanded')))
      .toEqual(['false', 'false', 'false']); // every row starts closed
    await fireEvent.click(open);
    expect(open.getAttribute('aria-expanded')).toBe('true');
    expect(within(r).getByText(long).textContent).toBe(long); // whole: no ellipsis, no "Show all"
    expect(within(r).queryByRole('button', { name: /Show (all|less)/ })).toBeNull();
    expect(within(r).getByText('Offered as').querySelector('code')?.textContent).toBe('mcp__deepwiki__read_wiki');
    const details = r.querySelector('details.schema') as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details.querySelector('summary')?.textContent).toBe('Schema');
    expect(details.querySelector('pre')?.textContent).toBe('{\n  "type": "object",\n  "properties": {}\n}');
    // Only that row opened.
    expect(within(row(card('deepwiki'), 'ask_question')).queryByText('ask_question description')).toBeNull();
    await fireEvent.click(open);
    expect(open.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText(long)).toBeNull();
    expect(r.querySelector('details.schema')).toBeNull();
  });

  it('an opened tool with no name, description or schema says the server gave none', async () => {
    listAnswer = () => {
      const d = LIST();
      d.servers[0].tools = [tool('bare', { name: null, description: null, schema: null, skip_reason: 'the input schema is not an object' })];
      return [200, d];
    };
    render(Integrations);
    const r = row(await screen.findByRole('region', { name: 'deepwiki' }), 'bare');
    expect(within(r).queryByText('The server gave no description and no schema for it.')).toBeNull();
    await fireEvent.click(within(r).getByRole('button', { name: 'Details of bare' }));
    expect(within(r).getByText('The server gave no description and no schema for it.')).toBeTruthy();
    expect(within(r).queryByText(/Offered as/)).toBeNull();
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
    const files = await openServer('files');
    expect((within(files).getByRole('button', { name: 'Switch on' }) as HTMLButtonElement).disabled).toBe(false);
    // Back on deepwiki, its change is still in flight.
    expect((within(await openServer('deepwiki')).getByRole('button', { name: 'Switching off…' }) as HTMLButtonElement).disabled).toBe(true);
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

  it("a server's name never takes the address or the ids of the form or the page", async () => {
    const NAMES = ['add', 'name', 'url', 'limits', 'top'];
    listAnswer = () => {
      const d = LIST();
      return [200, { ...d, servers: NAMES.map((name) => ({ ...d.servers[0], name })) }];
    };
    render(Integrations);
    await screen.findByRole('region', { name: 'add' });
    const unique = () => {
      const ids = Array.from(document.querySelectorAll('[id]')).map((e) => e.id);
      expect(new Set(ids).size).toBe(ids.length);
    };
    for (const name of NAMES) {
      await openServer(name); // a server named "add" is at s/add, never at the form's address
      expect(screen.queryByRole('region', { name: 'Add a server' })).toBeNull();
      expect(screen.getByRole('region', { name: 'Limits set on the host are in effect' })).toBeTruthy();
      expect(screen.getByRole('heading', { level: 1 }).id).toBe('mcp-top');
      unique();
    }
    await openForm();
    expect(screen.getByLabelText('Name').tagName).toBe('INPUT');
    expect(screen.getByLabelText('Address').tagName).toBe('INPUT');
    expect(screen.queryByRole('region', { name: 'add' })).toBeNull();
    expect(screen.getByRole('region', { name: 'Limits set on the host are in effect' })).toBeTruthy();
    unique();
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
    await screen.findByRole('region', { name: 'deepwiki' });
    const files = await openServer('files');
    const box = within(files).getByRole('checkbox', { name: 'Allow in background work' }) as HTMLInputElement;
    expect(box.checked).toBe(true);
    await fireEvent.click(box);
    await waitFor(() => expect(changes()).toHaveLength(1));
    expect(changes().map(sent)).toEqual([{ method: 'PATCH', url: '/integrations/mcp/servers/files', body: { background: false } }]);
  });

  it('sending images to the model asks first, and sends images true only when confirmed', async () => {
    patching('deepwiki');
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    expect(within(deepwiki).getByText("PNG and JPEG images in a tool's result, a screenshot for one, go to the model with the "
      + "result's text. While this is off, each is a line of text.")).toBeTruthy();
    const box = within(deepwiki).getByRole('checkbox', { name: 'Send its images to the model' }) as HTMLInputElement;
    expect(box.checked).toBe(false);
    await fireEvent.click(box);
    expect(box.checked).toBe(false); // nothing is stored yet
    expect(changes()).toEqual([]);
    const ask = within(deepwiki).getByRole('group', { name: 'Confirm images for deepwiki' });
    expect(within(ask).getByText("Send deepwiki's images to the model? An image can hold instructions that no check of the text sees.")).toBeTruthy();
    await waitFor(() => expect(document.activeElement?.textContent?.trim()).toBe('Yes, send its images'));
    await fireEvent.click(within(ask).getByRole('button', { name: 'Cancel' }));
    expect(within(deepwiki).queryByRole('group', { name: /^Confirm/ })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(box));
    expect(changes()).toEqual([]);

    await fireEvent.click(box);
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Yes, send its images' }));
    await waitFor(() => expect(changes()).toHaveLength(1));
    expect(changes().map(sent)).toEqual([{ method: 'PATCH', url: '/integrations/mcp/servers/deepwiki', body: { images: true } }]);
    expect((await within(card('deepwiki')).findByRole('status')).textContent).toBe('Images in its results now go to the model.');
    const after = within(card('deepwiki')).getByRole('checkbox', { name: 'Send its images to the model' }) as HTMLInputElement;
    expect(after.checked).toBe(true);
    await waitFor(() => expect(document.activeElement).toBe(after));
    expect(within(card('deepwiki')).queryByRole('group', { name: /^Confirm/ })).toBeNull();
  });

  it('stopping images needs no confirmation', async () => {
    patching('files');
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    const files = await openServer('files');
    const box = within(files).getByRole('checkbox', { name: 'Send its images to the model' }) as HTMLInputElement;
    expect(box.checked).toBe(true);
    await fireEvent.click(box);
    await waitFor(() => expect(changes()).toHaveLength(1));
    expect(changes().map(sent)).toEqual([{ method: 'PATCH', url: '/integrations/mcp/servers/files', body: { images: false } }]);
    expect(within(card('files')).queryByRole('group', { name: /^Confirm/ })).toBeNull();
    expect((await within(card('files')).findByRole('status')).textContent).toBe('Images in its results are no longer sent to the model.');
  });

  it('one question is open at a time: asking about images closes the one about background work, and the other way', async () => {
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    const groups = () => within(deepwiki).queryAllByRole('group', { name: /^Confirm/ }).map((g) => g.getAttribute('aria-label'));
    await fireEvent.click(within(deepwiki).getByRole('checkbox', { name: 'Allow in background work' }));
    expect(groups()).toEqual(['Confirm background work for deepwiki']);
    await fireEvent.click(within(deepwiki).getByRole('checkbox', { name: 'Send its images to the model' }));
    expect(groups()).toEqual(['Confirm images for deepwiki']);
    await fireEvent.click(within(deepwiki).getByRole('checkbox', { name: 'Allow in background work' }));
    expect(groups()).toEqual(['Confirm background work for deepwiki']);
    expect(changes()).toEqual([]);
  });

  it('a yes to one question never sends the other: each button sends its own field', async () => {
    patching('deepwiki');
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('checkbox', { name: 'Send its images to the model' }));
    expect(within(card('deepwiki')).queryByRole('button', { name: 'Yes, allow in background work' })).toBeNull();
    await fireEvent.click(within(card('deepwiki')).getByRole('checkbox', { name: 'Allow in background work' }));
    expect(within(card('deepwiki')).queryByRole('button', { name: 'Yes, send its images' })).toBeNull();
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Yes, allow in background work' }));
    await waitFor(() => expect(changes()).toHaveLength(1));
    expect(changes().map((c) => c.body)).toEqual([{ background: true }]);
    await within(card('deepwiki')).findByRole('status');
    expect((within(card('deepwiki')).getByRole('checkbox', { name: 'Send its images to the model' }) as HTMLInputElement).checked).toBe(false);
  });

  it('an images question left open goes when another server is opened, and is not there on the way back', async () => {
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(deepwiki).getByRole('checkbox', { name: 'Send its images to the model' }));
    expect(within(deepwiki).getByRole('button', { name: 'Yes, send its images' })).toBeTruthy();
    await openServer('files');
    const back = await openServer('deepwiki');
    expect(within(back).queryByRole('group', { name: /^Confirm/ })).toBeNull();
    expect(changes()).toEqual([]);
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
  it('checks the name and address as the server does, and sends nothing while they are wrong', async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('Deep__Wiki', 'ftp://mcp.deepwiki.com/mcp');
    expect(screen.getByLabelText('Name').getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByText(/^A name is 1 to 24 lower-case letters/)).toBeTruthy();
    expect(screen.getByText('Only https and http addresses are accepted.')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    expect(changes()).toEqual([]);
  });

  it('takes an http address and says it is not encrypted; the server decides whether its host is listed', async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('win', 'https://192.168.1.127:8000/mcp');
    expect(screen.queryByText(/^An http address is not encrypted/)).toBeNull();
    await fill('win', 'http://192.168.1.127:8000/mcp');
    expect(screen.getByLabelText('Address').getAttribute('aria-invalid')).toBeNull();
    expect(screen.getByText(/^An http address is not encrypted/).textContent).toContain('NOUS_MCP_CLIENT_ALLOW_PRIVATE_HOSTS');
    expect((screen.getByRole('button', { name: 'Check server' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('check, choose, add: every tool as the probe returned it, none allowed until ticked; 201 reloads the list', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    state = { ...LIST(), ceilings: { pinned_servers: false, background_servers: false }, servers: [] };
    adding();
    render(Integrations);
    await screen.findByText('No MCP servers yet.');
    await fill('deepwiki', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    expect(await within(addCard()).findByText('3 tools listed')).toBeTruthy();
    expect(sent(changes()[0])).toEqual({ method: 'POST', url: '/integrations/mcp/probe', body: { name: 'deepwiki', url: 'https://mcp.deepwiki.com/mcp' } });

    const boxes = within(addCard()).getAllByRole('checkbox') as HTMLInputElement[];
    expect(boxes.map((b) => b.checked)).toEqual([false, false, false]);
    expect(within(addCard()).getByText('would be skipped')).toBeTruthy();
    expect(within(addCard()).getByText('the input schema is not an object')).toBeTruthy();
    // A probed tool's name, description and schema show once its row is opened.
    expect(within(addCard()).queryByText('Ask a question about a repository.')).toBeNull();
    expect(addCard().querySelectorAll('details.schema')).toHaveLength(0);
    for (const name of ['ask_question', 'read_wiki', 'broken']) {
      const open = within(addCard()).getByRole('button', { name: `Details of ${name}` });
      expect(open.getAttribute('aria-expanded')).toBe('false');
      await fireEvent.click(open);
      expect(open.getAttribute('aria-expanded')).toBe('true');
    }
    expect(within(addCard()).getByText('mcp__deepwiki__ask_question')).toBeTruthy();
    expect(within(addCard()).getByText('Ask a question about a repository.')).toBeTruthy();
    expect(addCard().querySelectorAll('details.schema')).toHaveLength(2);

    await fireEvent.click(within(addCard()).getByRole('checkbox', { name: 'Allow read_wiki' }));
    const before = lists();
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Add server' }));
    // The form gives way to the server it made, and the note says so above the list.
    expect((await screen.findByText('Added deepwiki.')).getAttribute('role')).toBe('status');
    expect(sent(changes()[1])).toEqual({
      method: 'POST', url: '/integrations/mcp/servers',
      body: { name: 'deepwiki', url: 'https://mcp.deepwiki.com/mcp', tools: { ask_question: 'deny', read_wiki: 'allow', broken: 'deny' },
        expected_definitions_sha256: 'a'.repeat(64) },
    });
    await waitFor(() => expect(lists()).toBe(before + 1));
    // The address moves to the new server once that reload has landed (see "after an add" below).
    await waitFor(() => expect(location.hash).toBe('#/integrations/s/deepwiki'));
    expect(await screen.findByRole('region', { name: 'deepwiki' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Add a server' })).toBeNull();
    await waitFor(() => expect(link('deepwiki')?.getAttribute('aria-current')).toBe('page'));
    // The form starts over: no name, no tools from the check.
    await openForm();
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('');
    expect(within(addCard()).queryByRole('button', { name: 'Add server' })).toBeNull();
    expect(within(addCard()).queryByText(/tools? listed/)).toBeNull();
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
    await screen.findByText('Added wiki2.');
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

  it('a name already taken says what replacing does, background work and images off included, and the button says Replace server', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    adding();
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('deepwiki', 'https://mcp.deepwiki.com/mcp');
    expect(within(addCard()).getByText('A server named deepwiki exists. Adding it again replaces the server: its tools are checked '
      + 'again, it is switched on, and background work and images are turned off. Its call timeout goes back to 30 s.')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await fireEvent.click(await within(addCard()).findByRole('button', { name: 'Replace server' }));
    // The form gives way to the server it replaced.
    expect((await screen.findByText('Replaced deepwiki. It is switched on, and background work and images are off.')).getAttribute('role')).toBe('status');
    await waitFor(() => expect(location.hash).toBe('#/integrations/s/deepwiki'));
    expect(await screen.findByRole('region', { name: 'deepwiki' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Add a server' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Replace server' })).toBeNull();
  });

  it('a background confirmation left open is closed when the form opens, so the replaced server cannot be granted it', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    adding();
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByLabelText('Allow in background work'));
    expect(within(card('deepwiki')).getByRole('button', { name: 'Yes, allow in background work' })).toBeTruthy();
    await fill('deepwiki', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await fireEvent.click(await within(addCard()).findByRole('button', { name: 'Replace server' }));
    await screen.findByText('Replaced deepwiki. It is switched on, and background work and images are off.');
    // the question was about the server that was replaced: it must not be answerable for the new one
    const replaced = await screen.findByRole('region', { name: 'deepwiki' });
    expect(within(replaced).queryByRole('button', { name: 'Yes, allow in background work' })).toBeNull();
    expect(within(replaced).queryByRole('group', { name: /^Confirm/ })).toBeNull();
    expect(changes().filter((c) => c.method === 'PATCH')).toEqual([]);
  });

  it('an images question left open is closed when the form opens, so the replaced server cannot be granted it', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    adding();
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByLabelText('Send its images to the model'));
    expect(within(card('deepwiki')).getByRole('button', { name: 'Yes, send its images' })).toBeTruthy();
    await fill('deepwiki', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await fireEvent.click(await within(addCard()).findByRole('button', { name: 'Replace server' }));
    await screen.findByText('Replaced deepwiki. It is switched on, and background work and images are off.');
    const replaced = await screen.findByRole('region', { name: 'deepwiki' });
    expect(within(replaced).queryByRole('button', { name: 'Yes, send its images' })).toBeNull();
    expect(within(replaced).queryByRole('group', { name: /^Confirm/ })).toBeNull();
    expect(changes().filter((c) => c.method === 'PATCH')).toEqual([]);
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
    expect(await screen.findByText('Added wiki2.')).toBeTruthy();
    // This list never holds wiki2 (a reload that failed, or a list read before the add): the form stays, with the
    // note, and the address does not move to a server the list does not have. Nothing is disabled any more.
    await waitFor(() => expect((within(addCard()).getByRole('button', { name: 'Check server' }) as HTMLButtonElement).disabled).toBe(false));
    await new Promise((r) => setTimeout(r, 20)); // a hashchange, had one been asked for
    expect(location.hash).toBe('#/integrations/add');
    expect(screen.queryByText(/There is no server named/)).toBeNull();
    expect((screen.getByLabelText('Name') as HTMLInputElement).disabled).toBe(false);
    expect((screen.getByLabelText('Address') as HTMLInputElement).disabled).toBe(false);
    expect((within(addCard()).getByRole('button', { name: 'Check server' }) as HTMLButtonElement).disabled).toBe(false);
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
    await fill('evil', 'https://evil.example.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    const add = screen.getByRole('region', { name: 'Add a server' });
    await within(add).findByText('6 tools listed');
    expect((within(add).getAllByRole('checkbox') as HTMLInputElement[]).map((b) => b.checked)).toEqual(HOSTILE.map(() => false));
    await fireEvent.click(within(add).getByRole('checkbox', { name: 'Allow __proto__' }));
    await fireEvent.click(within(add).getByRole('button', { name: 'Add server' }));
    await screen.findByText('Added evil.');
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
    expect(link('hasownproperty')?.querySelector('.item-name')?.textContent).toBe('hasownproperty');
    expect(link('constructor')?.getAttribute('aria-current')).toBe('page');
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
    expect(await screen.findByRole('region', { name: 'hasownproperty' })).toBeTruthy(); // the first server left
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

  describe("a tool's name may break after _, - and .", () => {
    /** A name's code element as text runs and <wbr>s (Svelte's comment anchors left out). */
    const parts = (code: Element) => {
      const out: string[] = [];
      let inText = false;
      for (const n of Array.from(code.childNodes)) {
        if (n.nodeType === Node.TEXT_NODE) {
          if (inText) out[out.length - 1] += n.textContent ?? '';
          else out.push(n.textContent ?? '');
          inText = true;
        } else if (n.nodeType === Node.ELEMENT_NODE) {
          out.push(`<${(n as Element).tagName.toLowerCase()}>`);
          inText = false;
        }
      }
      return out;
    };
    const named = async (...names: string[]) => {
      listAnswer = () => {
        const d = LIST();
        d.servers[0].tools = names.map((n) => tool(n));
        return [200, d];
      };
      render(Integrations);
      const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
      return (name: string) => within(deepwiki).getByRole('button', { name: `Details of ${name}` }).querySelector('code.tool-name') as HTMLElement;
    };

    it('a <wbr> after each separator; the text is the whole name', async () => {
      const code = (await named('get_file-contents.v2', 'readwiki'))('get_file-contents.v2');
      expect(code.textContent).toBe('get_file-contents.v2');
      expect(parts(code)).toEqual(['get_', '<wbr>', 'file-', '<wbr>', 'contents.', '<wbr>', 'v2']);
    });

    it('a name without a separator has none', async () => {
      const code = (await named('get_file-contents.v2', 'readwiki'))('readwiki');
      expect(code.textContent).toBe('readwiki');
      expect(code.querySelectorAll('wbr')).toHaveLength(0);
      expect(parts(code)).toEqual(['readwiki']);
    });

    it('a name that looks like markup is still text and <wbr> only', async () => {
      const hostile = '<img src=x onerror=alert(1)>_x';
      const code = (await named(hostile))(hostile);
      expect(code.textContent).toBe(hostile);
      expect(parts(code)).toEqual(['<img src=x onerror=alert(1)>_', '<wbr>', 'x']);
      expect(Array.from(code.children).map((e) => e.tagName)).toEqual(['WBR']);
      expect(document.querySelector('img')).toBeNull();
    });

    it('a long name with separators is still cut to 80 characters, the whole name in its title', async () => {
      const long = `get_${'x'.repeat(100)}-end`;
      const code = (await named(long))(long);
      expect(code.textContent).toBe(`${long.slice(0, 79)}…`);
      expect(code.getAttribute('title')).toBe(long);
      expect(parts(code)).toEqual(['get_', '<wbr>', `${long.slice(4, 79)}…`]);
    });

    it('separators in a row, first and last: a break after each, none after the last character', async () => {
      const code = (await named('_a__b.-c_'))('_a__b.-c_');
      expect(code.textContent).toBe('_a__b.-c_');
      expect(parts(code)).toEqual(['_', '<wbr>', 'a_', '<wbr>', '_', '<wbr>', 'b.', '<wbr>', '-', '<wbr>', 'c_']);
    });

    it('the screen has no lookbehind in a pattern: a browser without it would not parse the bundle at all', () => {
      const src = readFileSync(join(__dirname, 'Integrations.svelte'), 'utf-8');
      expect(src).not.toMatch(/\(\?<[=!]/);
    });
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
  it("a server's images note goes when the list shows them stopped elsewhere", async () => {
    patching('deepwiki');
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('checkbox', { name: 'Send its images to the model' }));
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Yes, send its images' }));
    expect((await within(card('deepwiki')).findByRole('status')).textContent).toBe('Images in its results now go to the model.');
    // Stopped elsewhere: "now go to the model" must not stay beside an empty box.
    state.servers[0].images = false;
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(within(card('deepwiki')).queryByRole('status')).toBeNull());
    expect((within(card('deepwiki')).getByRole('checkbox', { name: 'Send its images to the model' }) as HTMLInputElement).checked).toBe(false);
  });

  it('a note about switching a server on or off stays while only that is what the list shows', async () => {
    patching('deepwiki');
    state.servers[0].images = true;
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Switch off' }));
    expect((await within(card('deepwiki')).findByRole('status')).textContent).toBe('Switched off: its tools are no longer offered to the agent.');
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(lists()).toBeGreaterThanOrEqual(3));
    expect(within(card('deepwiki')).getByRole('status').textContent).toBe('Switched off: its tools are no longer offered to the agent.');
  });

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
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
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
    const files = await openServer('files');
    await fireEvent.click(within(files).getByRole('button', { name: 'Remove' }));
    await fireEvent.click(within(card('files')).getByRole('button', { name: 'Yes, remove' }));
    await screen.findByText('Removed files.');
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Add server' }));
    await screen.findByText('Added wiki2.');
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
  // parseBlocks merges a selector's blocks across media queries: each part of the sheet is parsed on its own.
  const atOneColumn = css.indexOf(`@media ${ONE_COLUMN}`);
  const at768 = css.indexOf('@media (max-width: 768px)');
  const wide = parseBlocks(css.slice(0, Math.min(atOneColumn, at768)));
  const tablet = parseBlocks(css.slice(atOneColumn, at768));
  const phone = parseBlocks(css.slice(at768));
  it('controls reach 44px on a phone, and a field is 44px tall', () => {
    expect(atOneColumn).toBeGreaterThan(0); // the stylesheet's one-column width is the script's
    expect(at768).toBeGreaterThan(atOneColumn);
    expect(css).not.toMatch(/@media \(max-width: 900px\)/);
    // The list's rows, the checkbox rows and a tool's line.
    expect(phone.get('.item, .check, .open')).toEqual({ 'min-height': '2.75rem' });
    expect(phone.get('.schema summary')?.['line-height']).toBe('2.75rem');
    expect(phone.get('.search')?.height).toBe('2.75rem');
    expect(wide.get('.field input')?.height).toBe('2.75rem');
    const markup = markupOf(readFileSync(join(__dirname, 'Integrations.svelte'), 'utf-8'));
    for (const tag of markup.match(/<Button\b[^>]*>/g) ?? []) expect(tag).toContain('max-md:min-h-11');
  });
  it('the list and the pane are two columns, one on a narrow screen, where a picked pane hides the list', () => {
    expect(wide.get('.split')?.['grid-template-columns']).toBe('17rem minmax(0, 1fr)');
    expect(tablet.get('.split')?.['grid-template-columns']).toBe('minmax(0, 1fr)');
    expect(tablet.get('.split.picked .list')).toEqual({ display: 'none' });
    expect(wide.get('.back')?.display).toBe('none'); // the way back to the list shows only in one column
    expect(tablet.get('.back')?.display).toBe('inline-flex');
  });
  it("a long tool name gives way on a tool's line: its state and its Allow control keep their size", () => {
    const open = wide.get('.open') ?? {};
    const shrink = (flex: string | undefined) => (flex === 'none' ? 0 : Number(flex?.split(' ')[1] ?? 1));
    expect(open.flex).toBeDefined();
    expect(open.flex).not.toBe('none');
    expect(shrink(open.flex)).toBeGreaterThan(0);
    expect(open['min-width']).toBe('0');
    expect(open['text-align']).toBe('left');
    expect(wide.get('.tool-name')?.['min-width']).toBe('0');
    expect(wide.get('.tool-act')?.flex).toBe('none');
    expect(wide.get('.tool-head .status-pill')?.flex).toBe('none');
  });
  it('the server shown is marked by a quiet background, not a filled accent', () => {
    const current = wide.get(".item[aria-current='page']");
    expect(current).toEqual({ background: 'var(--bg)' });
    for (const [selector, decls] of [...wide, ...tablet, ...phone]) {
      if (!selector.includes('aria-current')) continue;
      for (const value of Object.values(decls)) expect(value, selector).not.toMatch(/--(accent|action)/);
    }
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
  const valueField = () => within(addCard()).getByLabelText('Header 1 value') as HTMLInputElement;

  async function typeKey(value = KEY) {
    await openForm();
    await fireEvent.click(within(addCard()).getByRole('radio', { name: 'API key or header' }));
    await fireEvent.input(valueField(), { target: { value } });
  }

  it('the value is dots until Show, and Check and Add send the typed headers; a success drops them', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    adding({ auth: { ...NO_AUTH(), kind: 'headers', header_names: ['Authorization'] } });
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    expect(within(addCard()).getByRole('radio', { name: 'None' })).toHaveProperty('checked', true);
    expect((within(addCard()).getByRole('radio', { name: 'Sign in (OAuth)' }) as HTMLInputElement).disabled).toBe(true);
    // PR 2d: without the public https address (oauth_available absent here), the choice says what it needs.
    expect(within(addCard()).getByText('Sign-in needs NOUS_A2UI_PUBLIC_BASE_URL set to the https address of this Nous.')).toBeTruthy();
    await typeKey();
    expect((within(addCard()).getByLabelText('Header 1 name') as HTMLInputElement).value).toBe('Authorization');
    expect(valueField().type).toBe('password');
    expect(valueField().getAttribute('autocomplete')).toBe('off');
    expect(within(addCard()).getByText('Most servers want Authorization with the value "Bearer <your key>".')).toBeTruthy();
    // What the scrub hides, in plain words: the value, and the second word of a two-word value on its own.
    expect(within(addCard()).getByText('Tool results never show a stored value; for a value of two words, not its second word either.')).toBeTruthy();
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Show the value of header 1' }));
    expect(valueField().type).toBe('text');
    expect(valueField().value).toBe(KEY); // only what was just typed
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Hide the value of header 1' }));
    expect(valueField().type).toBe('password');

    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    expect(sent(changes()[0]).body).toEqual({ name: 'wiki2', url: 'https://mcp.deepwiki.com/mcp', headers: { Authorization: KEY } });
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Add server' }));
    await screen.findByText('Added wiki2.');
    expect((changes()[1].body as Record<string, unknown>).headers).toEqual({ Authorization: KEY });
    // The new server's page names its header, never the value.
    expect(within(await screen.findByRole('region', { name: 'wiki2' })).getByText('Key set: Authorization')).toBeTruthy();
    expect(document.body.innerHTML).not.toContain('test-key');
    await openForm();
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
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    expect((within(deepwiki).getByRole('button', { name: 'Add key' }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(deepwiki).getAllByText(NO_KEY_FILE)).toHaveLength(1);
    await openForm();
    expect((within(addCard()).getByRole('radio', { name: 'API key or header' }) as HTMLInputElement).disabled).toBe(true);
    expect(within(addCard()).getAllByText(NO_KEY_FILE)).toHaveLength(1);
  });

  it('leaving the form drops a typed key: back on it, "None" is chosen and the rows open empty and hidden', async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await typeKey();
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Show the value of header 1' }));
    expect(valueField().type).toBe('text');
    const nowhere = () => {
      expect(document.body.innerHTML).not.toContain('test-key');
      for (const input of Array.from(document.querySelectorAll('input'))) expect(input.value).not.toContain('test-key');
    };
    await openServer('deepwiki');
    nowhere();
    await openForm();
    nowhere();
    expect(within(addCard()).getByRole('radio', { name: 'None' })).toHaveProperty('checked', true);
    expect(within(addCard()).queryByLabelText('Header 1 value')).toBeNull();
    // The name and the address are no secret: they stay.
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('wiki2');
    expect((screen.getByLabelText('Address') as HTMLInputElement).value).toBe('https://mcp.deepwiki.com/mcp');
    await fireEvent.click(within(addCard()).getByRole('radio', { name: 'API key or header' }));
    expect(valueField().value).toBe('');
    expect(valueField().type).toBe('password');
    expect(changes()).toEqual([]);
  });

  it("leaving the form drops the check made before it and the form's note; the name and the address stay", async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    await openServer('deepwiki');
    await openForm();
    expect(within(addCard()).queryByText(/tools? listed/)).toBeNull();
    expect(within(addCard()).queryByRole('checkbox', { name: 'Allow read_wiki' })).toBeNull();
    expect(within(addCard()).queryByRole('button', { name: 'Add server' })).toBeNull();
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('wiki2');
    expect((screen.getByLabelText('Address') as HTMLInputElement).value).toBe('https://mcp.deepwiki.com/mcp');
    handlers['POST /integrations/mcp/probe'] = () => [502, { error: "could not list the server's tools: ConnectError" }];
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    expect((await within(addCard()).findByRole('alert')).textContent).toBe("Could not list the server's tools: ConnectError.");
    await openServer('deepwiki');
    await openForm();
    expect(within(addCard()).queryByRole('alert')).toBeNull();
  });

  it('leaving the form drops a finished sign-in: back on it, "None" is chosen and the check is made without it', async () => {
    listAnswer = () => [200, { ...LIST(), oauth_available: true }];
    handlers['POST /integrations/mcp/oauth/start'] = () => [200, {
      flow_id: 'flow-1', authorization_url: 'https://auth.example.com/authorize?client_id=c&state=s', authorization_host: 'auth.example.com',
      token_host: 'auth.example.com', registration: 'dynamic', scope: 'read', expires_in: 600,
    }];
    handlers['GET /integrations/mcp/oauth/flows/flow-1'] = () => [200, { status: 'pending', reason: null, stored: false }];
    handlers['POST /integrations/mcp/oauth/complete'] = () => [200, { status: 'complete', reason: null, stored: false }];
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://wiki2.example.com/mcp');
    await fireEvent.click(within(addCard()).getByRole('radio', { name: 'Sign in (OAuth)' }));
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Sign in' }));
    const pasted = await within(addCard()).findByLabelText('Paste the address instead');
    await fireEvent.input(pasted, { target: { value: 'https://nous.example.com/integrations/mcp/oauth/callback?code=c&state=s' } });
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Use this address' }));
    expect(await within(addCard()).findByText('Signed in for wiki2. Check the server next: it is checked with this sign-in.')).toBeTruthy();
    await openServer('deepwiki');
    await openForm();
    expect(within(addCard()).getByRole('radio', { name: 'None' })).toHaveProperty('checked', true);
    expect(within(addCard()).getByRole('radio', { name: 'Sign in (OAuth)' })).toHaveProperty('checked', false);
    expect(within(addCard()).queryByText(/^Signed in for/)).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    expect(changes().filter((c) => c.url === '/integrations/mcp/probe').map((c) => c.body))
      .toEqual([{ name: 'wiki2', url: 'https://wiki2.example.com/mcp' }]); // no oauth_flow_id
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

describe('Integrations: a server replaced elsewhere', () => {
  // Another tab, another device or the seed command put another address under the same name: what was open on this
  // page was opened for the old server, and must not be answered, stored or signed in for the new one.
  const elsewhere = (change: (s: (typeof state.servers)[number]) => void) => {
    change(state.servers[0]);
    return fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  };
  const listedUrl = (url: string) => waitFor(() => expect(within(card('deepwiki')).getByText(url)).toBeTruthy());

  it.each([
    ['Send its images to the model', 'Yes, send its images'],
    ['Allow in background work', 'Yes, allow in background work'],
  ])('a question opened by "%s" is closed when the address under the name changed', async (box, yes) => {
    patching('deepwiki');
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('checkbox', { name: box }));
    expect(within(card('deepwiki')).getByRole('button', { name: yes })).toBeTruthy();
    // A reload that shows the same server leaves the question as it is.
    await elsewhere((s) => { s.connected = false; });
    await waitFor(() => expect(lists()).toBeGreaterThanOrEqual(2));
    await settle();
    expect(within(card('deepwiki')).getByRole('button', { name: yes })).toBeTruthy();
    await elsewhere((s) => { s.url = 'https://other.example.com/mcp'; });
    await listedUrl('https://other.example.com/mcp');
    expect(within(card('deepwiki')).queryByRole('button', { name: yes })).toBeNull();
    expect(within(card('deepwiki')).queryByRole('group', { name: /^Confirm/ })).toBeNull();
    expect(changes()).toEqual([]);
  });

  it('a Remove question is closed when the address under the name changed', async () => {
    patching('deepwiki');
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Remove' }));
    expect(within(card('deepwiki')).getByRole('button', { name: 'Yes, remove' })).toBeTruthy();
    await elsewhere((s) => { s.url = 'https://other.example.com/mcp'; });
    await listedUrl('https://other.example.com/mcp');
    expect(within(card('deepwiki')).queryByRole('button', { name: 'Yes, remove' })).toBeNull();
    expect(changes()).toEqual([]);
  });

  it('a key being typed is dropped when the address under the name changed: it is not stored for another host', async () => {
    state = keyed() as typeof state;
    patching('deepwiki');
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Replace key' }));
    await fireEvent.input(within(card('deepwiki')).getByLabelText('Header 1 value'), { target: { value: KEY } });
    await elsewhere((s) => { s.url = 'https://other.example.com/mcp'; });
    await listedUrl('https://other.example.com/mcp');
    expect(within(card('deepwiki')).queryByLabelText('Header 1 value')).toBeNull();
    expect(within(card('deepwiki')).queryByRole('button', { name: 'Save key' })).toBeNull();
    for (const input of Array.from(document.querySelectorAll('input'))) expect(input.value).not.toContain('test-key');
    expect(changes()).toEqual([]);
  });

  it('a sign-in panel is closed when the address under the name changed', async () => {
    state = { ...LIST(), oauth_available: true } as typeof state;
    patching('deepwiki');
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Sign in (OAuth)' }));
    expect(within(card('deepwiki')).getByRole('group', { name: 'Sign in for deepwiki' })).toBeTruthy();
    await elsewhere((s) => { s.connected = false; });
    await waitFor(() => expect(lists()).toBeGreaterThanOrEqual(2));
    await settle();
    expect(within(card('deepwiki')).getByRole('group', { name: 'Sign in for deepwiki' })).toBeTruthy();
    await elsewhere((s) => { s.url = 'https://other.example.com/mcp'; });
    await listedUrl('https://other.example.com/mcp');
    expect(within(card('deepwiki')).queryByRole('group', { name: 'Sign in for deepwiki' })).toBeNull();
    expect(changes()).toEqual([]);
  });

  it('a key being typed stays through a reload that shows the same address', async () => {
    state = keyed() as typeof state;
    patching('deepwiki');
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Replace key' }));
    await fireEvent.input(within(card('deepwiki')).getByLabelText('Header 1 value'), { target: { value: KEY } });
    await elsewhere((s) => { s.connected = false; });
    await waitFor(() => expect(lists()).toBeGreaterThanOrEqual(2));
    await settle();
    expect((within(card('deepwiki')).getByLabelText('Header 1 value') as HTMLInputElement).value).toBe(KEY);
  });
});

describe('Integrations: a stored key on a card', () => {
  it('names the headers, never a value; Replace key sends the new headers, Cancel drops them', async () => {
    listAnswer = () => [200, keyed()];
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => [200, { credentials: 'replaced', server: null }];
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    expect(within(deepwiki).getByText('Key set: Authorization, X-API-Key')).toBeTruthy();
    expect(within(deepwiki).queryByText('No key stored.')).toBeNull();
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
    // A server with no key says so.
    expect(within(await openServer('files')).getByText('No key stored.')).toBeTruthy();
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

// The screen is a list of the servers beside one pane: a server, the form, or a sentence for a name that is not there.
describe('Integrations: the list of servers', () => {
  const item = (name: string) => {
    const a = link(name);
    return a && {
      name: a.querySelector('.item-name')?.textContent,
      count: a.querySelector('.item-count')?.textContent,
      pills: Array.from(a.querySelectorAll('.status-pill')).map((p) => p.textContent),
    };
  };
  const current = () => Array.from(nav().querySelectorAll('[aria-current="page"]')).map((a) => a.getAttribute('href'));

  it('one link per server: its address, how many tools are allowed, its status, and a block on the host', async () => {
    listAnswer = () => {
      const d = LIST();
      d.servers[1].blocked_by_ceiling = true;
      d.servers.push({ ...d.servers[0], name: 'empty', connected: false, tools: [] });
      return [200, d];
    };
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    expect(within(nav()).getAllByRole('link').map((a) => a.getAttribute('href')))
      .toEqual([ADD_ADDRESS, '#/integrations/s/deepwiki', '#/integrations/s/files', '#/integrations/s/empty']);
    expect(within(nav()).getByRole('link', { name: 'Add server' }).getAttribute('href')).toBe(ADD_ADDRESS);
    expect(item('deepwiki')).toEqual({ name: 'deepwiki', count: '2 of 3 allowed', pills: ['connected'] });
    expect(item('files')).toEqual({ name: 'files', count: '1 of 1 allowed', pills: ['switched off', 'blocked on the host'] });
    expect(item('empty')).toEqual({ name: 'empty', count: 'no tools', pills: ['connects on first use'] });
  });

  it("the shown server's link is the current page; on the form, the Add server link is", async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    expect(current()).toEqual(['#/integrations/s/deepwiki']); // the first server, at the screen's own address
    await openServer('files');
    expect(current()).toEqual(['#/integrations/s/files']);
    await openForm();
    expect(current()).toEqual([ADD_ADDRESS]);
    expect(within(nav()).getByRole('link', { name: 'Add server' }).getAttribute('aria-current')).toBe('page');
  });

  it('the Add server link, current on the form, takes the paper fill a current server takes', async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    const add = within(nav()).getByRole('link', { name: 'Add server' });
    expect(add.className).toContain('aria-[current=page]:bg-[var(--bg)]');
    expect(add.className).not.toContain('--wash');
  });
});

describe('Integrations: the pane follows the address', () => {
  it('no address shows the first server, s/<name> that server, add the form', async () => {
    render(Integrations);
    expect(await screen.findByRole('region', { name: 'deepwiki' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'files' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Add a server' })).toBeNull();
    await openServer('files');
    expect(screen.queryByRole('region', { name: 'deepwiki' })).toBeNull();
    const form = await openForm();
    expect(within(form).getByText("Check the server's address, choose which of its tools the agent may use, then add it.")).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'files' })).toBeNull();
    // Back at the screen's own address: the server that was open last, not the first.
    go(LIST_ADDRESS);
    expect(await screen.findByRole('region', { name: 'files' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'deepwiki' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Add a server' })).toBeNull();
    expect(link('files')?.getAttribute('aria-current')).toBe('page');
  });

  it.each([
    ['a name that is not there', '#/integrations/s/nothere', 'nothere'],
    ['an address that does not decode', '#/integrations/s/%E0%A4%A', '%E0%A4%A'],
  ])('%s names no server, and says to choose one from the list', async (_, hash, shownName) => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    go(hash);
    const missing = await screen.findByText((_text, el) => el?.tagName === 'P'
      && el.textContent === `There is no server named ${shownName} here. Choose one from the list.`);
    expect(missing.closest('[role="status"]')).toBeTruthy();
    expect(missing.querySelector('code')?.textContent).toBe(shownName); // the name as data, in its own element
    expect(screen.queryByRole('region', { name: 'deepwiki' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'files' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Add a server' })).toBeNull();
    expect(nav().querySelector('[aria-current]')).toBeNull();
  });

  it.each(['#/integrations/foo', '#/integrations/s', '#/integrations/s/'])(
    '%s names neither the form nor a server: it shows what the bare address shows', async (address) => {
      go(address);
      render(Integrations);
      expect(await screen.findByRole('region', { name: 'deepwiki' })).toBeTruthy(); // the first server
      expect(document.querySelector('.split')?.classList.contains('picked')).toBe(false);
      expect(link('deepwiki')?.getAttribute('aria-current')).toBe('page');
      expect(screen.queryByText(/There is no server named/)).toBeNull();
      cleanup();
      listAnswer = () => [200, { ...LIST(), servers: [] }];
      render(Integrations);
      expect(await screen.findByRole('region', { name: 'Add a server' })).toBeTruthy(); // no server: the form
      expect(document.querySelector('.split')?.classList.contains('picked')).toBe(false);
    });

  it.each(['#/work/s/files', '#/work/add'])("another screen's address %s, shaped like this one's, picks nothing here", async (address) => {
    go(address); // the screen rendered on its own while the router is at another route
    render(Integrations);
    expect(await screen.findByRole('region', { name: 'deepwiki' })).toBeTruthy(); // the default pane
    expect(screen.queryByRole('region', { name: 'files' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Add a server' })).toBeNull();
    expect(document.querySelector('.split')?.classList.contains('picked')).toBe(false);
  });

  it('the bare address keeps its server while it exists, when a reload puts another one first', async () => {
    let servers = (d: ReturnType<typeof LIST>) => d.servers;
    listAnswer = () => {
      const d = LIST();
      return [200, { ...d, servers: servers(d) }];
    };
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    servers = (d) => [{ ...d.servers[0], name: 'aaa' }, ...d.servers]; // a new server, first in name order
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(link('aaa')).toBeTruthy());
    expect(screen.getByRole('region', { name: 'deepwiki' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'aaa' })).toBeNull();
    expect(link('deepwiki')?.getAttribute('aria-current')).toBe('page');
    expect(location.hash).toBe(LIST_ADDRESS);
    servers = (d) => [{ ...d.servers[0], name: 'aaa' }, d.servers[1]]; // deepwiki is gone: the first server shows
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByRole('region', { name: 'aaa' })).toBeTruthy();
    expect(link('aaa')?.getAttribute('aria-current')).toBe('page');
  });

  it('the bare address returns to the server shown last, whichever address showed it, while it exists', async () => {
    let gone = false;
    listAnswer = () => {
      const d = LIST();
      return [200, gone ? { ...d, servers: d.servers.filter((s) => s.name !== 'files') } : d];
    };
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await openServer('files');
    go(LIST_ADDRESS);
    await waitFor(() => expect(location.hash).toBe(LIST_ADDRESS));
    await settle();
    expect(screen.getByRole('region', { name: 'files' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'deepwiki' })).toBeNull();
    expect(link('files')?.getAttribute('aria-current')).toBe('page');
    expect(link('deepwiki')?.getAttribute('aria-current')).toBeNull();
    gone = true; // a reload without files: the first server shows
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByRole('region', { name: 'deepwiki' })).toBeTruthy();
    expect(link('files')).toBeNull();
    expect(link('deepwiki')?.getAttribute('aria-current')).toBe('page');
    expect(screen.queryByText(/There is no server named/)).toBeNull();
  });

  it('the pane is a group named for what it shows: the server, "Add a server", or "No such server"', async () => {
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    const pane = () => document.getElementById('mcp-pane') as HTMLElement;
    expect(screen.getByRole('group', { name: 'deepwiki' })).toBe(pane());
    expect(pane().contains(deepwiki)).toBe(true);
    await openServer('files');
    expect(screen.getByRole('group', { name: 'files' })).toBe(pane());
    expect(screen.queryByRole('group', { name: 'deepwiki' })).toBeNull();
    const form = await openForm();
    expect(screen.getByRole('group', { name: 'Add a server' })).toBe(pane());
    expect(pane().contains(form)).toBe(true);
    go('#/integrations/s/nothere');
    await screen.findByText(/There is no server named/);
    expect(screen.getByRole('group', { name: 'No such server' })).toBe(pane());
  });

  it('a server whose name needs encoding is linked by its encoded address, and opens there', async () => {
    const odd = 'a/b?c%d'; // not a name the server accepts: the address must still round-trip whatever the list holds
    listAnswer = () => {
      const d = LIST();
      d.servers[1] = { ...d.servers[1], name: odd };
      return [200, d];
    };
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    const a = within(nav()).getAllByRole('link').find((l) => l.querySelector('.item-name')?.textContent === odd)!;
    expect(a.getAttribute('href')).toBe('#/integrations/s/a%2Fb%3Fc%25d');
    go(a.getAttribute('href')!);
    const shown = await screen.findByRole('region', { name: odd });
    expect(within(shown).getByRole('heading', { level: 2 }).textContent).toBe(odd);
    expect(a.getAttribute('aria-current')).toBe('page');
    expect(screen.queryByRole('region', { name: 'deepwiki' })).toBeNull();
  });
});

describe('Integrations: a narrow screen', () => {
  /** A matchMedia that matches the screen's one-column width, and the listeners the screen added to it. */
  function narrowScreen() {
    const media = { matches: true, listeners: new Set<() => void>() };
    const matchMedia = vi.fn((query: string) => ({
      get matches() { return query === ONE_COLUMN && media.matches; },
      media: query,
      addEventListener: (_type: string, f: () => void) => { media.listeners.add(f); },
      removeEventListener: (_type: string, f: () => void) => { media.listeners.delete(f); },
    }));
    vi.stubGlobal('matchMedia', matchMedia);
    return { media, matchMedia };
  }
  const split = () => document.querySelector('.split') as HTMLElement;

  it('with no address only the list shows: no server, no form, nothing marked current', async () => {
    const { media, matchMedia } = narrowScreen();
    render(Integrations);
    await screen.findByRole('navigation', { name: 'MCP servers' });
    expect(matchMedia).toHaveBeenCalledWith(ONE_COLUMN);
    expect(link('deepwiki')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'deepwiki' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Add a server' })).toBeNull();
    expect(document.querySelector('.detail')).toBeNull();
    expect(nav().querySelector('[aria-current]')).toBeNull();
    expect(split().classList.contains('picked')).toBe(false); // the stylesheet keeps the list in view
    // Wider again: the pane comes back with the first server.
    media.matches = false;
    for (const f of media.listeners) f();
    expect(await screen.findByRole('region', { name: 'deepwiki' })).toBeTruthy();
  });

  it('with an address the pane shows, with a link back to the list', async () => {
    narrowScreen();
    render(Integrations);
    await screen.findByRole('navigation', { name: 'MCP servers' });
    await openServer('files');
    expect(split().classList.contains('picked')).toBe(true); // the stylesheet hides the list
    expect(screen.getByRole('link', { name: 'All servers' }).getAttribute('href')).toBe(LIST_ADDRESS);
    expect(link('files')?.getAttribute('aria-current')).toBe('page');
    await openForm();
    expect(split().classList.contains('picked')).toBe(true);
    go(LIST_ADDRESS);
    await waitFor(() => expect(document.querySelector('.detail')).toBeNull());
    expect(split().classList.contains('picked')).toBe(false);
  });

  it('a move between the list and the pane puts the focus on what came into view; the first render moves none', async () => {
    narrowScreen();
    render(Integrations);
    await screen.findByRole('navigation', { name: 'MCP servers' });
    await new Promise((r) => setTimeout(r, 20));
    expect(document.activeElement).toBe(document.body);
    await openServer('files');
    await waitFor(() => expect(document.activeElement?.id).toBe('mcp-pane'));
    expect(document.activeElement?.contains(card('files'))).toBe(true);
    expect(document.activeElement?.getAttribute('tabindex')).toBe('-1');
    go(LIST_ADDRESS);
    await waitFor(() => expect(document.activeElement).toBe(nav()));
    expect(nav().id).toBe('mcp-list');
    expect(nav().getAttribute('tabindex')).toBe('-1');
    await openForm();
    await waitFor(() => expect(document.activeElement?.id).toBe('mcp-pane'));
    expect(document.activeElement?.contains(addCard())).toBe(true);
  });

  it('on a wide screen a move does not take the focus', async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await openServer('files');
    await new Promise((r) => setTimeout(r, 20));
    expect(document.activeElement).toBe(document.body);
    go(LIST_ADDRESS);
    await waitFor(() => expect(link('files')?.getAttribute('aria-current')).toBe('page')); // the last server open
    await new Promise((r) => setTimeout(r, 20));
    expect(document.activeElement).toBe(document.body);
    await openForm();
    await new Promise((r) => setTimeout(r, 20));
    expect(document.activeElement).toBe(document.body);
  });

  it.each(['#/integrations/foo', '#/integrations/s', '#/integrations/s/'])(
    '%s names neither the form nor a server: the list shows, not hidden, and no pane', async (address) => {
      narrowScreen();
      go(address);
      render(Integrations);
      await screen.findByRole('navigation', { name: 'MCP servers' });
      expect(link('deepwiki')).toBeTruthy();
      expect(split().classList.contains('picked')).toBe(false); // the stylesheet keeps the list in view
      expect(document.querySelector('.detail')).toBeNull();
      expect(screen.queryByRole('region', { name: 'deepwiki' })).toBeNull();
      expect(screen.queryByRole('region', { name: 'Add a server' })).toBeNull();
      expect(screen.queryByText(/There is no server named/)).toBeNull();
      expect(nav().querySelector('[aria-current]')).toBeNull();
    });
});

describe('Integrations: finding a tool', () => {
  const allow = { permission: 'allow', registered: true, skip_reason: null };
  const NINE = () => [
    tool('fetch_page', allow), tool('fetch_feed'), tool('list_files'), tool('read_file', allow), tool('write_file'),
    tool('delete_file'), tool('search_docs', allow), tool('ping', { description: 'Checks that the server answers.' }),
    tool('status', { description: 'Reports the uptime.' }),
  ];
  const withTools = (n: number) => {
    listAnswer = () => {
      const d = LIST();
      d.servers[0].tools = NINE().slice(0, n);
      return [200, d];
    };
  };
  const shownTools = () => Array.from(card('deepwiki').querySelectorAll('.tool-head .tool-name')).map((c) => c.textContent);
  const filter = () => within(card('deepwiki')).getByRole('group', { name: 'Which tools of deepwiki to show' });
  const search = () => within(card('deepwiki')).getByRole('searchbox', { name: 'Find a tool of deepwiki' }) as HTMLInputElement;
  const pressed = () => within(filter()).getAllByRole('button')
    .filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.textContent?.trim());
  const choose = (label: string) => fireEvent.click(within(filter()).getByRole('button', { name: label }));
  const find = (text: string) => fireEvent.input(search(), { target: { value: text } });

  it.each([[8, false], [9, true]] as [number, boolean][])('with %i tools, the search and the filter show: %s', async (n, shown) => {
    withTools(n);
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    expect(within(deepwiki).getByRole('heading', { level: 3, name: 'Tools' })).toBeTruthy();
    expect(deepwiki.querySelector('.tools-head .count')?.textContent).toBe(`3 of ${n} allowed`);
    expect(!!within(deepwiki).queryByRole('searchbox', { name: 'Find a tool of deepwiki' })).toBe(shown);
    expect(!!within(deepwiki).queryByRole('group', { name: 'Which tools of deepwiki to show' })).toBe(shown);
    expect(shownTools()).toHaveLength(n);
  });

  it('Allowed, Not allowed and All show those tools, and say how many of all are shown', async () => {
    withTools(9);
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    expect(within(filter()).getAllByRole('button').map((b) => b.textContent?.trim())).toEqual(['All', 'Allowed', 'Not allowed']);
    expect(pressed()).toEqual(['All']);
    expect(shownTools()).toHaveLength(9);
    expect(within(deepwiki).queryByText(/^Showing/)).toBeNull();
    await choose('Allowed');
    expect(pressed()).toEqual(['Allowed']);
    expect(shownTools()).toEqual(['fetch_page', 'read_file', 'search_docs']);
    expect(within(deepwiki).getByText('Showing 3 of 9.')).toBeTruthy();
    await choose('Not allowed');
    expect(pressed()).toEqual(['Not allowed']);
    expect(shownTools()).toEqual(['fetch_feed', 'list_files', 'write_file', 'delete_file', 'ping', 'status']);
    expect(within(deepwiki).getByText('Showing 6 of 9.')).toBeTruthy();
    await choose('All');
    expect(shownTools()).toHaveLength(9);
    expect(within(deepwiki).queryByText(/^Showing/)).toBeNull();
  });

  it('the search finds a tool by its name or its description, without case or spaces around, with the filter', async () => {
    withTools(9);
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    await find('FILE');
    expect(shownTools()).toEqual(['list_files', 'read_file', 'write_file', 'delete_file']);
    expect(within(deepwiki).getByText('Showing 4 of 9.')).toBeTruthy();
    await find('  uptime ');
    expect(shownTools()).toEqual(['status']); // by its description, which is not on screen while the row is closed
    await find('file');
    await choose('Allowed');
    expect(shownTools()).toEqual(['read_file']);
    expect(within(deepwiki).getByText('Showing 1 of 9.')).toBeTruthy();
    await find('nothing like it');
    expect(shownTools()).toEqual([]);
    expect(within(deepwiki).getByText('No tool matches.')).toBeTruthy();
    expect(within(deepwiki).queryByText(/^Showing/)).toBeNull();
    await find('');
    await choose('All');
    expect(shownTools()).toHaveLength(9);
  });

  /** A list whose deepwiki tools the test changes between reloads; the page's Refresh reloads it. */
  const reloading = () => {
    const now = { tools: NINE() };
    listAnswer = () => {
      const d = LIST();
      d.servers[0].tools = now.tools;
      return [200, d];
    };
    return now;
  };
  const reloadTo = async (now: { tools: ReturnType<typeof NINE> }, tools: ReturnType<typeof NINE>) => {
    now.tools = tools;
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(card('deepwiki').querySelector('.tools-head .count')?.textContent).toBe(`3 of ${tools.length} allowed`));
  };
  const EIGHT = () => NINE().filter((t) => t.tool !== 'status');

  it('a typed query keeps the search while a reload leaves fewer than nine tools, until it is cleared', async () => {
    const now = reloading();
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await find('uptime');
    expect(shownTools()).toEqual(['status']);
    await reloadTo(now, EIGHT()); // the one tool it found is gone
    expect(search().value).toBe('uptime');
    expect(filter()).toBeTruthy();
    expect(shownTools()).toEqual([]);
    expect(within(card('deepwiki')).getByText('No tool matches.')).toBeTruthy();
    await find('');
    expect(shownTools()).toHaveLength(8);
    expect(within(card('deepwiki')).queryByText('No tool matches.')).toBeNull();
    // Nothing typed and All: eight tools are read at a glance again.
    expect(within(card('deepwiki')).queryByRole('searchbox', { name: 'Find a tool of deepwiki' })).toBeNull();
    expect(within(card('deepwiki')).queryByRole('group', { name: 'Which tools of deepwiki to show' })).toBeNull();
  });

  it('a pressed filter stays while a reload leaves fewer than nine tools, until All is pressed', async () => {
    const now = reloading();
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await choose('Not allowed');
    await reloadTo(now, EIGHT());
    expect(pressed()).toEqual(['Not allowed']);
    expect(search().value).toBe('');
    expect(shownTools()).toEqual(['fetch_feed', 'list_files', 'write_file', 'delete_file', 'ping']);
    expect(within(card('deepwiki')).getByText('Showing 5 of 8.')).toBeTruthy();
    await choose('All');
    expect(shownTools()).toHaveLength(8);
    expect(within(card('deepwiki')).queryByText(/^Showing/)).toBeNull();
    expect(within(card('deepwiki')).queryByRole('group', { name: 'Which tools of deepwiki to show' })).toBeNull();
    expect(within(card('deepwiki')).queryByRole('searchbox', { name: 'Find a tool of deepwiki' })).toBeNull();
  });

  it('a tool changed under a filter stays listed, with its note and the focus, until the filter or the query changes', async () => {
    state.servers[0].tools = NINE();
    patching('deepwiki');
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    const NOT_ALLOWED = ['fetch_feed', 'list_files', 'write_file', 'delete_file', 'ping', 'status'];
    const settled = () => waitFor(() => expect((within(card('deepwiki')).getByRole('button', { name: 'Refresh tools' }) as HTMLButtonElement).disabled).toBe(false));
    await choose('Not allowed');
    expect(shownTools()).toEqual(NOT_ALLOWED);
    const allowFeed = within(card('deepwiki')).getByRole('button', { name: 'Allow fetch_feed' });
    allowFeed.focus();
    await fireEvent.click(allowFeed);
    expect((await within(row(card('deepwiki'), 'fetch_feed')).findByRole('status')).textContent).toBe('Allowed.');
    await settled();
    expect(shownTools()).toEqual(NOT_ALLOWED); // allowed now, and still where it was pressed
    expect(within(row(card('deepwiki'), 'fetch_feed')).getByText('in use')).toBeTruthy();
    expect(within(card('deepwiki')).getByText('Showing 6 of 9.')).toBeTruthy();
    await waitFor(() => expect(document.activeElement)
      .toBe(within(row(card('deepwiki'), 'fetch_feed')).getByRole('button', { name: "Don't allow fetch_feed" })));
    // Choosing the filter again lists what it says.
    await choose('All');
    await choose('Not allowed');
    expect(shownTools()).toEqual(NOT_ALLOWED.filter((t) => t !== 'fetch_feed'));
    expect(within(card('deepwiki')).getByText('Showing 5 of 9.')).toBeTruthy();
    // So does a change of the query.
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Allow list_files' }));
    expect((await within(row(card('deepwiki'), 'list_files')).findByRole('status')).textContent).toBe('Allowed.');
    await settled();
    expect(shownTools()).toContain('list_files');
    await find('file');
    expect(shownTools()).toEqual(['write_file', 'delete_file']);
  });

  it('the search and the filter start over when another server or the form is shown', async () => {
    withTools(9);
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    for (const away of [() => openServer('files'), openForm]) {
      await find('file');
      await choose('Not allowed');
      expect(shownTools()).toEqual(['list_files', 'write_file', 'delete_file']);
      await away();
      await openServer('deepwiki');
      expect(search().value).toBe('');
      expect(pressed()).toEqual(['All']);
      expect(shownTools()).toHaveLength(9);
    }
  });
});

describe('Integrations: leaving a server', () => {
  it('closes its key form: what was typed is not kept, and the rows open empty and hidden on the way back', async () => {
    listAnswer = () => [200, keyed()];
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(deepwiki).getByRole('button', { name: 'Replace key' }));
    await fireEvent.input(within(card('deepwiki')).getByLabelText('Header 1 value'), { target: { value: KEY } });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Show the value of header 1' }));
    await openServer('files');
    const back = await openServer('deepwiki');
    expect(within(back).queryByLabelText('Header 1 value')).toBeNull();
    expect(document.body.innerHTML).not.toContain('test-key');
    for (const input of Array.from(document.querySelectorAll('input'))) expect(input.value).not.toContain('test-key');
    await fireEvent.click(within(back).getByRole('button', { name: 'Replace key' }));
    const field = within(card('deepwiki')).getByLabelText('Header 1 value') as HTMLInputElement;
    expect(field.value).toBe('');
    expect(field.type).toBe('password');
    expect(changes()).toEqual([]);
  });

  it('closes an open confirmation, so it is not answered behind the pane', async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Remove' }));
    expect(within(card('deepwiki')).getByRole('group', { name: 'Confirm removing deepwiki' })).toBeTruthy();
    await openServer('files');
    let back = await openServer('deepwiki');
    expect(within(back).queryByRole('group', { name: /^Confirm/ })).toBeNull();
    await fireEvent.click(within(back).getByRole('checkbox', { name: 'Allow in background work' }));
    expect(within(card('deepwiki')).getByRole('group', { name: 'Confirm background work for deepwiki' })).toBeTruthy();
    await openForm();
    back = await openServer('deepwiki');
    expect(within(back).queryByRole('group', { name: /^Confirm/ })).toBeNull();
    expect(changes()).toEqual([]);
  });

  it('closes its sign-in panel', async () => {
    listAnswer = () => [200, { ...LIST(), oauth_available: true }];
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    const signIn = () => within(card('deepwiki')).getByRole('button', { name: 'Sign in (OAuth)' });
    await fireEvent.click(within(deepwiki).getByRole('button', { name: 'Sign in (OAuth)' }));
    expect(signIn().getAttribute('aria-expanded')).toBe('true');
    expect(within(card('deepwiki')).getByRole('group', { name: 'Sign in for deepwiki' })).toBeTruthy();
    await openServer('files');
    const back = await openServer('deepwiki');
    expect(within(back).queryByRole('group', { name: 'Sign in for deepwiki' })).toBeNull();
    expect(signIn().getAttribute('aria-expanded')).toBe('false');
    expect(changes()).toEqual([]);
  });
});

describe('Integrations: opened rows', () => {
  it("a server's opened row is closed once another server or the form has been shown", async () => {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    const details = () => within(card('deepwiki')).getByRole('button', { name: 'Details of read_wiki' });
    for (const away of [() => openServer('files'), openForm]) {
      await fireEvent.click(details());
      expect(details().getAttribute('aria-expanded')).toBe('true');
      expect(screen.getByText('read_wiki description')).toBeTruthy();
      await away();
      await openServer('deepwiki');
      expect(details().getAttribute('aria-expanded')).toBe('false');
      expect(screen.queryByText('read_wiki description')).toBeNull();
    }
  });

  it("a probed row opened before a new check is closed on the new check's list", async () => {
    let probes = 0;
    handlers['POST /integrations/mcp/probe'] = () => { probes += 1; return [200, PROBE()]; };
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    const details = () => within(addCard()).getByRole('button', { name: 'Details of read_wiki' });
    await fireEvent.click(details());
    expect(details().getAttribute('aria-expanded')).toBe('true');
    expect(within(addCard()).getByText('Read the wiki.')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await waitFor(() => expect(probes).toBe(2));
    // The second check has landed (the button says Check server again): the same tool's row starts closed.
    await waitFor(() => expect((within(addCard()).getByRole('button', { name: 'Check server' }) as HTMLButtonElement).disabled).toBe(false));
    expect(details().getAttribute('aria-expanded')).toBe('false');
    expect(within(addCard()).queryByText('Read the wiki.')).toBeNull();
  });
});

/** Time for a hashchange (or a focus) that would follow, were one coming. */
const settle = () => new Promise((r) => setTimeout(r, 20));

describe('Integrations: after an add', () => {
  /** The form checked for wiki2, with an add that stores it (`adding`). */
  async function checked() {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    adding();
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
  }
  /** Every list request after the add waits until `release()`; `count()` says how many are waiting. */
  function holdReload() {
    const inner = globalThis.fetch;
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    let added = false;
    let count = 0;
    vi.stubGlobal('fetch', async (url: string, init: RequestInit = {}) => {
      if ((init.method ?? 'GET') === 'GET' && added) {
        count += 1;
        await gate;
      }
      const answer = await inner(url, init);
      if (init.method === 'POST' && url === '/integrations/mcp/servers') added = true;
      return answer;
    });
    return { release: () => release(), count: () => count };
  }
  const addServer = () => fireEvent.click(within(addCard()).getByRole('button', { name: 'Add server' }));

  it("the address stays the form's until the reload has landed, and no \"no server\" sentence shows meanwhile", async () => {
    await checked();
    const reload = holdReload();
    // "Never in the document": every change of the page is looked at, not only the moments the test asserts.
    const seen: string[] = [];
    const watch = new MutationObserver(() => {
      if (document.body.textContent?.includes('There is no server named')) seen.push(location.hash);
    });
    watch.observe(document.body, { childList: true, subtree: true, characterData: true });
    try {
      await addServer();
      await screen.findByText('Added wiki2.');
      await waitFor(() => expect(reload.count()).toBe(1)); // the reload is in flight
      await settle();
      expect(location.hash).toBe(ADD_ADDRESS);
      expect(screen.getByRole('region', { name: 'Add a server' })).toBeTruthy();
      expect(screen.queryByText(/There is no server named/)).toBeNull();
      reload.release();
      expect(await screen.findByRole('region', { name: 'wiki2' })).toBeTruthy();
      expect(location.hash).toBe('#/integrations/s/wiki2');
      expect(link('wiki2')?.getAttribute('aria-current')).toBe('page');
      expect(screen.getByText('Added wiki2.').getAttribute('role')).toBe('status'); // a success note outlives the move
    } finally {
      reload.release();
      watch.disconnect();
    }
    expect(seen).toEqual([]);
  });

  it('the address stays on a server the owner opened while the reload was in flight', async () => {
    await checked();
    const reload = holdReload();
    await addServer();
    await waitFor(() => expect(reload.count()).toBe(1));
    await openServer('files');
    reload.release();
    await waitFor(() => expect(link('wiki2')).toBeTruthy()); // the reload has landed
    await settle();
    expect(location.hash).toBe('#/integrations/s/files');
    expect(screen.getByRole('region', { name: 'files' })).toBeTruthy();
  });

  it('the address is not pulled back from another screen opened while the reload was in flight', async () => {
    await checked();
    const reload = holdReload();
    await addServer();
    await waitFor(() => expect(reload.count()).toBe(1));
    go('#/overview'); // the screen stays rendered here: the router is at another route
    reload.release();
    await waitFor(() => expect(link('wiki2')).toBeTruthy());
    await settle();
    expect(location.hash).toBe('#/overview');
  });

  it('the address stays on a form the owner came back to while the reload was in flight, with what is typed on it', async () => {
    await checked();
    const reload = holdReload();
    await addServer();
    await waitFor(() => expect(reload.count()).toBe(1));
    await openServer('files');
    await openForm(); // another form: the one that made wiki2 was left
    await fill('wiki3', 'https://wiki3.example.com/mcp');
    reload.release();
    await waitFor(() => expect(link('wiki2')).toBeTruthy()); // the reload has landed
    await settle();
    expect(location.hash).toBe(ADD_ADDRESS);
    expect(screen.getByRole('region', { name: 'Add a server' })).toBeTruthy();
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('wiki3');
    expect((screen.getByLabelText('Address') as HTMLInputElement).value).toBe('https://wiki3.example.com/mcp');
    expect(screen.getByText('Added wiki2.').getAttribute('role')).toBe('status'); // said above the pane all the same
  });

  it('a reload that fails leaves the form: the address never names a server the list does not hold', async () => {
    await checked();
    const stored = listAnswer;
    const post = handlers['POST /integrations/mcp/servers'];
    let added = false;
    handlers['POST /integrations/mcp/servers'] = (body) => { added = true; return post(body); };
    listAnswer = () => (added ? [500, { error: 'the request could not be processed' }] : stored());
    const before = lists();
    await addServer();
    await screen.findByText('Added wiki2.');
    await waitFor(() => expect(lists()).toBe(before + 1));
    await settle();
    expect(location.hash).toBe(ADD_ADDRESS);
    expect(screen.queryByText(/There is no server named/)).toBeNull();
  });

  /** The form checked for wiki2 (`probes` counts the checks), its add held: `answer` lands it. */
  async function heldAdd() {
    const counter = { probes: 0 };
    handlers['POST /integrations/mcp/probe'] = () => { counter.probes += 1; return [200, PROBE()]; };
    const answer = held();
    handlers['POST /integrations/mcp/servers'] = () => answer.promise;
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    await addServer();
    return { counter, answer };
  }
  /** Answer the held add and wait for the reload it ends with. */
  async function land(answer: ReturnType<typeof held>, a: Answer) {
    const before = lists();
    answer.resolve(a);
    await waitFor(() => expect(lists()).toBe(before + 1)); // the add is over: it reloads the list last
    await settle();
  }
  const CONFLICT: Answer = [409, { error: "the server's tool definitions changed since they were shown: review them again", definitions_sha256: 'b'.repeat(64) }];
  const UNREACHABLE: Answer = [502, { error: "could not list the server's tools: ReadTimeout" }];

  it('a 409 that lands after the form was left is said above the pane, and checks nothing again', async () => {
    const { counter, answer } = await heldAdd();
    await openServer('deepwiki');
    await land(answer, CONFLICT);
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toBe('wiki2 was not added: its tools changed since the check');
    expect(alert.closest('.split')).toBeNull(); // above the list and the pane
    expect(screen.getByRole('region', { name: 'deepwiki' })).toBeTruthy(); // still on the server the owner went to
    expect(location.hash).toBe('#/integrations/s/deepwiki');
    expect(counter.probes).toBe(1);
    expect(changes().filter((c) => c.url === '/integrations/mcp/probe')).toHaveLength(1);
    // Back on the form: no sentence about a new list, and no list.
    await openForm();
    expect(within(addCard()).queryByText(/The server's tools changed since you checked it/)).toBeNull();
    expect(within(addCard()).queryByText(/tools? listed/)).toBeNull();
    expect(within(addCard()).queryByRole('checkbox', { name: /^Allow / })).toBeNull();
    expect(within(addCard()).queryByRole('alert')).toBeNull();
  });

  it('a 502 that lands after the form was left is said above the pane, and not on the form', async () => {
    const { counter, answer } = await heldAdd();
    await openServer('files');
    await land(answer, UNREACHABLE);
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toBe("wiki2 was not added: Could not list the server's tools: ReadTimeout.");
    expect(alert.closest('.split')).toBeNull();
    expect(screen.getByRole('region', { name: 'files' })).toBeTruthy();
    expect(counter.probes).toBe(1);
    await openForm();
    expect(within(addCard()).queryByRole('alert')).toBeNull();
    expect(within(addCard()).queryByText(/ReadTimeout/)).toBeNull();
    // Nowhere else says why wiki2 is not in the list: it stays above the pane until the owner tries again.
    expect(screen.getByRole('alert').textContent).toBe("wiki2 was not added: Could not list the server's tools: ReadTimeout.");
    expect(screen.getByRole('alert').closest('.split')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    expect(screen.queryByText(/was not added/)).toBeNull();
    expect(counter.probes).toBe(2);
  });

  it('an add that failed after its form was left is still said when the owner moves on to another server', async () => {
    const { answer } = await heldAdd();
    await openServer('deepwiki');
    await land(answer, UNREACHABLE);
    await openServer('files');
    await settle();
    expect(screen.getByRole('alert').textContent).toBe("wiki2 was not added: Could not list the server's tools: ReadTimeout.");
    // A change the owner makes next starts over: what is said above the pane is about that change.
    handlers['PATCH /integrations/mcp/servers/files'] = () => [200, { server: null }];
    await fireEvent.click(within(card('files')).getByRole('button', { name: 'Switch on' }));
    await waitFor(() => expect(screen.queryByText(/was not added/)).toBeNull());
  });

  it('an add that failed after its form was left is said even when its reload takes the shown server away', async () => {
    const { answer } = await heldAdd();
    await openServer('files');
    const listed = listAnswer;
    listAnswer = () => {
      const [status, d] = listed() as [number, ReturnType<typeof LIST>];
      return [status, { ...d, servers: d.servers.filter((s) => s.name !== 'files') }]; // removed elsewhere meanwhile
    };
    await land(answer, UNREACHABLE);
    await screen.findByText(/There is no server named/);
    await settle();
    expect(screen.getByRole('alert').textContent).toBe("wiki2 was not added: Could not list the server's tools: ReadTimeout.");
  });

  it('an add that lands while the owner is away leaves no name on the form to replace the server with', async () => {
    const { answer } = await heldAdd();
    await openServer('files');
    await land(answer, [201, { created: true, definitions_sha256: 'a'.repeat(64), server: null }]);
    expect(screen.getByText('Added wiki2.').getAttribute('role')).toBe('status');
    await openForm();
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('Address') as HTMLInputElement).value).toBe('');
    // An emptied form is not one the owner tried: its fields are not said to be wrong.
    expect(screen.getByLabelText('Name').getAttribute('aria-invalid')).toBeNull();
    expect(screen.getByLabelText('Address').getAttribute('aria-invalid')).toBeNull();
    expect(within(addCard()).queryByText('Enter a name.')).toBeNull();
  });

  it('an add that lands on a form the owner came back to leaves its name and address as they are', async () => {
    const { answer } = await heldAdd();
    await openServer('files');
    await openForm(); // the name and the address stay across a leave: this form shows them
    await land(answer, [201, { created: true, definitions_sha256: 'a'.repeat(64), server: null }]);
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('wiki2');
    expect((screen.getByLabelText('Address') as HTMLInputElement).value).toBe('https://mcp.deepwiki.com/mcp');
    expect(location.hash).toBe(ADD_ADDRESS);
  });

  it('an add that lands while the owner is away keeps another name typed on the form since', async () => {
    const { answer } = await heldAdd();
    await openServer('files');
    await openForm();
    await fill('wiki3', 'https://wiki3.example.com/mcp');
    await openServer('files');
    await land(answer, [201, { created: true, definitions_sha256: 'a'.repeat(64), server: null }]);
    await openForm();
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('wiki3');
    expect((screen.getByLabelText('Address') as HTMLInputElement).value).toBe('https://wiki3.example.com/mcp');
  });

  it.each([
    ['only the name', 'wiki3', 'https://mcp.deepwiki.com/mcp'],
    ['only the address', 'wiki2', 'https://wiki3.example.com/mcp'],
  ])('an add that lands while the owner is away keeps a form on which %s was changed since', async (_, name, url) => {
    const { answer } = await heldAdd();
    await openServer('files');
    await openForm();
    await fill(name, url);
    await openServer('files');
    await land(answer, [201, { created: true, definitions_sha256: 'a'.repeat(64), server: null }]);
    await openForm();
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe(name);
    expect((screen.getByLabelText('Address') as HTMLInputElement).value).toBe(url);
  });

  /** The checked form's add, held: `store()` stores the server as the route would and loses the answer. */
  function lostAnswer() {
    const post = handlers['POST /integrations/mcp/servers'];
    const gate = held();
    handlers['POST /integrations/mcp/servers'] = async (body) => {
      await gate.promise;
      await post(body);
      return [0, undefined]; // stored, and the answer never arrived
    };
    return () => gate.resolve([200, {}]);
  }

  it('an add that was stored though its answer was lost is not said to have failed once the list holds the server', async () => {
    await checked();
    const store = lostAnswer();
    await addServer();
    await openServer('files');
    store();
    await waitFor(() => expect(link('wiki2')).toBeTruthy()); // the reload shows it
    await settle();
    expect(screen.queryByText(/was not added/)).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('a replace that failed after its form was left stays said: the list held that name before', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    const answer = held();
    handlers['POST /integrations/mcp/servers'] = () => answer.promise;
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('deepwiki', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await fireEvent.click(await within(addCard()).findByRole('button', { name: 'Replace server' }));
    await openServer('files');
    await land(answer, UNREACHABLE);
    expect(screen.getByRole('alert').textContent).toBe("deepwiki was not added: Could not list the server's tools: ReadTimeout.");
  });

  it('an add that failed after its form was left stays said while the list does not hold the server', async () => {
    await checked();
    const answer = held();
    handlers['POST /integrations/mcp/servers'] = () => answer.promise;
    await addServer();
    await openServer('files');
    await land(answer, [0, undefined]);
    expect(screen.getByRole('alert').textContent).toMatch(/^wiki2 was not added: /);
  });

  it('a 502 that lands while the form is shown is said on the form, not above the pane', async () => {
    const { answer } = await heldAdd();
    await land(answer, UNREACHABLE);
    const alert = within(addCard()).getByRole('alert');
    expect(alert.textContent).toBe("Could not list the server's tools: ReadTimeout.");
    expect(screen.getAllByRole('alert')).toEqual([alert]);
    expect(screen.queryByText(/was not added/)).toBeNull();
  });

  // A check or an add belongs to the form it was made on: once that form is left, its answer is not shown on the
  // form opened next, and no second check is made with a key the new form does not hold.
  /** The form for wiki2 with a key typed: the key the leave must drop. */
  async function keyedForm() {
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(within(addCard()).getByRole('radio', { name: 'API key or header' }));
    await fireEvent.input(within(addCard()).getByLabelText('Header 1 value'), { target: { value: KEY } });
  }
  const leaveAndReturn = async () => {
    await openServer('deepwiki');
    await openForm();
  };
  const checkEnabled = () => waitFor(() =>
    expect((within(addCard()).getByRole('button', { name: 'Check server' }) as HTMLButtonElement).disabled).toBe(false));
  /** The form as a leave leaves it: "None", no key, no check, no note. */
  function freshForm() {
    expect(within(addCard()).getByRole('radio', { name: 'None' })).toHaveProperty('checked', true);
    expect(within(addCard()).queryByLabelText('Header 1 value')).toBeNull();
    expect(within(addCard()).queryByText(/tools? listed/)).toBeNull();
    expect(within(addCard()).queryByText(/changed since the check/)).toBeNull();
    expect(within(addCard()).queryByRole('button', { name: 'Add server' })).toBeNull();
    expect(within(addCard()).queryByRole('alert')).toBeNull();
    for (const input of Array.from(document.querySelectorAll('input'))) expect(input.value).not.toContain('test-key');
  }

  it('a check that answers after its form was left and opened again shows nothing on the new form', async () => {
    const answer = held();
    handlers['POST /integrations/mcp/probe'] = () => answer.promise;
    await keyedForm();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await leaveAndReturn();
    answer.resolve([200, PROBE()]);
    await checkEnabled();
    await settle();
    freshForm();
  });

  it('a check that fails after its form was left and opened again says nothing on the new form', async () => {
    const answer = held();
    handlers['POST /integrations/mcp/probe'] = () => answer.promise;
    await keyedForm();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await leaveAndReturn();
    answer.resolve(UNREACHABLE);
    await checkEnabled();
    await settle();
    freshForm();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it.each([
    ['a 409', CONFLICT, 'wiki2 was not added: its tools changed since the check'],
    ['a 502', UNREACHABLE, "wiki2 was not added: Could not list the server's tools: ReadTimeout."],
  ] as [string, Answer, string][])(
    '%s for an add whose form was left and opened again: no second check, said above the pane, not on the form',
    async (_, failure, said) => {
      let probes = 0;
      handlers['POST /integrations/mcp/probe'] = () => { probes += 1; return [200, PROBE()]; };
      const answer = held();
      handlers['POST /integrations/mcp/servers'] = () => answer.promise;
      await keyedForm();
      await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
      await within(addCard()).findByText('3 tools listed');
      await addServer();
      await leaveAndReturn(); // back on a form before the add answers: another form now
      await land(answer, failure);
      expect(probes).toBe(1);
      expect(changes().filter((c) => c.url === '/integrations/mcp/probe')).toHaveLength(1);
      const alert = screen.getByRole('alert');
      expect(alert.textContent).toBe(said);
      expect(alert.closest('.split')).toBeNull();
      freshForm();
    });

  /** Every request to `route` held until the test answers it, in order. */
  const holdEach = (route: string) => {
    const answers: ReturnType<typeof held>[] = [];
    handlers[route] = () => {
      const h = held();
      answers.push(h);
      return h.promise;
    };
    return answers;
  };
  const field = (label: string) => screen.getByLabelText(label) as HTMLInputElement;
  const button = (name: string) => within(addCard()).getByRole('button', { name }) as HTMLButtonElement;
  /** The returned form is usable at once: its fields and Check server, with nothing said to be in flight. */
  function usable() {
    expect(field('Name').disabled).toBe(false);
    expect(field('Address').disabled).toBe(false);
    expect(button('Check server').disabled).toBe(false);
    expect(within(addCard()).queryByRole('button', { name: 'Checking…' })).toBeNull();
    expect(within(addCard()).queryByRole('button', { name: 'Adding…' })).toBeNull();
  }

  it("a check from a left form neither holds the returned form nor ends the returned form's own check", async () => {
    const checks = holdEach('POST /integrations/mcp/probe');
    await keyedForm();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await waitFor(() => expect(checks).toHaveLength(1));
    expect(button('Checking…').disabled).toBe(true);
    await leaveAndReturn();
    usable(); // at once: the old check has not answered
    await fireEvent.click(button('Check server'));
    await waitFor(() => expect(checks).toHaveLength(2));
    const checkingNow = () => {
      expect(button('Checking…').disabled).toBe(true);
      expect(field('Name').disabled).toBe(true);
      expect(field('Address').disabled).toBe(true);
    };
    checkingNow();
    checks[0].resolve([200, PROBE()]); // the old check answers: not this form's
    await settle();
    checkingNow();
    expect(within(addCard()).queryByText(/tools? listed/)).toBeNull();
    checks[1].resolve([200, PROBE()]);
    expect(await within(addCard()).findByText('3 tools listed')).toBeTruthy();
    usable();
    expect(changes().filter((c) => c.url === '/integrations/mcp/probe')).toHaveLength(2);
  });

  it.each([
    ['a 409', CONFLICT],
    ['a 502', UNREACHABLE],
  ] as [string, Answer][])(
    "an add from a left form neither holds the returned form nor ends the returned form's own add (old add: %s)",
    async (_, oldAnswer) => {
      let probes = 0;
      handlers['POST /integrations/mcp/probe'] = () => { probes += 1; return [200, PROBE()]; };
      const adds = holdEach('POST /integrations/mcp/servers');
      await keyedForm();
      await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
      await within(addCard()).findByText('3 tools listed');
      await addServer();
      await waitFor(() => expect(adds).toHaveLength(1));
      expect(button('Adding…').disabled).toBe(true);
      await leaveAndReturn();
      usable(); // at once: the old add has not answered
      await fireEvent.click(button('Check server'));
      await within(addCard()).findByText('3 tools listed');
      await addServer();
      await waitFor(() => expect(adds).toHaveLength(2));
      const addingNow = () => {
        expect(button('Adding…').disabled).toBe(true);
        expect(button('Check server').disabled).toBe(true);
        expect(field('Name').disabled).toBe(true);
        expect(field('Address').disabled).toBe(true);
      };
      addingNow();
      await land(adds[0], oldAnswer); // the old add answers: not this form's
      addingNow();
      expect(probes).toBe(2); // the old add's answer checked nothing again
      expect(screen.getByRole('alert').textContent).toMatch(/^wiki2 was not added: /);
      await land(adds[1], [201, { created: true, definitions_sha256: 'a'.repeat(64), server: null }]);
      expect(screen.getByText('Added wiki2.')).toBeTruthy();
      expect(within(addCard()).queryByRole('button', { name: 'Adding…' })).toBeNull();
    });

  // An add from a left form that succeeds says so above the pane and does nothing else: the returned form is
  // another form, so what is typed on it stays and the address is not moved from under it.
  const CREATED: Answer = [201, { created: true, definitions_sha256: 'a'.repeat(64), server: null }];
  /** The server an add stored, in the list `adding()` answers with. */
  const stored = (name: string) => {
    state.servers.push({ ...LIST().servers[0], name, url: 'https://mcp.deepwiki.com/mcp', tools: [] });
  };
  /** An add of wiki2 held, then the form left and opened again. */
  async function addThenReturn() {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    adding(); // the list answers with `state`
    const adds = holdEach('POST /integrations/mcp/servers');
    await keyedForm();
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    await addServer();
    await waitFor(() => expect(adds).toHaveLength(1));
    await leaveAndReturn();
    return adds;
  }

  it('an add from a left form that succeeds leaves what is typed on the returned form, and the address', async () => {
    const adds = await addThenReturn();
    const typed = 'Bearer test-key-3333333333333333';
    await fireEvent.input(field('Name'), { target: { value: 'wiki3' } });
    await fireEvent.click(within(addCard()).getByRole('radio', { name: 'API key or header' }));
    await fireEvent.input(within(addCard()).getByLabelText('Header 1 value'), { target: { value: typed } });
    stored('wiki2'); // the reload that follows holds the server the old add made
    await land(adds[0], CREATED);
    await waitFor(() => expect(link('wiki2')).toBeTruthy());
    await settle();
    expect(screen.getByText('Added wiki2.').getAttribute('role')).toBe('status');
    expect(location.hash).toBe(ADD_ADDRESS);
    expect(screen.getByRole('region', { name: 'Add a server' })).toBeTruthy();
    expect(field('Name').value).toBe('wiki3');
    expect(field('Address').value).toBe('https://mcp.deepwiki.com/mcp');
    expect(within(addCard()).getByRole('radio', { name: 'API key or header' })).toHaveProperty('checked', true);
    expect((within(addCard()).getByLabelText('Header 1 value') as HTMLInputElement).value).toBe(typed);
    usable();
  });

  it("an add from a left form that succeeds leaves the returned form's own add in flight as it is", async () => {
    const adds = await addThenReturn();
    await fireEvent.input(field('Name'), { target: { value: 'wiki3' } });
    await fireEvent.click(button('Check server'));
    await within(addCard()).findByText('3 tools listed');
    await addServer();
    await waitFor(() => expect(adds).toHaveLength(2));
    stored('wiki2');
    await land(adds[0], CREATED); // the old add (wiki2) lands while the new one (wiki3) is in flight
    await waitFor(() => expect(link('wiki2')).toBeTruthy());
    await settle();
    expect(screen.getByText('Added wiki2.')).toBeTruthy();
    expect(location.hash).toBe(ADD_ADDRESS);
    expect(button('Adding…').disabled).toBe(true);
    expect(within(addCard()).getByText('3 tools listed')).toBeTruthy();
    expect(field('Name').value).toBe('wiki3');
    // The new add is the returned form's own: when it lands, that form starts over and gives way to its server.
    stored('wiki3');
    await land(adds[1], CREATED);
    expect(await screen.findByRole('region', { name: 'wiki3' })).toBeTruthy();
    expect(location.hash).toBe('#/integrations/s/wiki3');
    expect(screen.getByText('Added wiki3.')).toBeTruthy();
    await openForm();
    expect(field('Name').value).toBe('');
    expect(within(addCard()).queryByText(/tools? listed/)).toBeNull();
  });
});

describe('Integrations: after a removal', () => {
  it('the address goes back to the screen, the note says so, and the first server left is shown', async () => {
    let removed = false;
    handlers['DELETE /integrations/mcp/servers/files'] = () => { removed = true; return [200, { deleted: 'files' }]; };
    listAnswer = () => {
      const d = LIST();
      return [200, removed ? { ...d, servers: d.servers.slice(0, 1) } : d];
    };
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    const files = await openServer('files');
    expect(location.hash).toBe('#/integrations/s/files');
    await fireEvent.click(within(files).getByRole('button', { name: 'Remove' }));
    await fireEvent.click(within(card('files')).getByRole('button', { name: 'Yes, remove' }));
    expect((await screen.findByText('Removed files.')).getAttribute('role')).toBe('status');
    await waitFor(() => expect(location.hash).toBe(LIST_ADDRESS));
    expect(await screen.findByRole('region', { name: 'deepwiki' })).toBeTruthy();
    await waitFor(() => expect(link('files')).toBeNull());
    expect(link('deepwiki')?.getAttribute('aria-current')).toBe('page');
    expect(screen.queryByText(/There is no server named/)).toBeNull();
    expect(screen.getByText('Removed files.').getAttribute('role')).toBe('status'); // a success note outlives the move
  });

  /** A list without `name` once its DELETE has landed, and that DELETE held until the test answers it. */
  function heldRemoval(name: string) {
    const answer = held();
    let removed = false;
    handlers[`DELETE /integrations/mcp/servers/${name}`] = async () => {
      const a = await answer.promise;
      removed = true;
      return a;
    };
    listAnswer = () => {
      const d = LIST();
      return [200, removed ? { ...d, servers: d.servers.filter((s) => s.name !== name) } : d];
    };
    return () => answer.resolve([200, { deleted: name }]);
  }
  const confirmRemove = async (name: string) => {
    await fireEvent.click(within(card(name)).getByRole('button', { name: 'Remove' }));
    await fireEvent.click(within(card(name)).getByRole('button', { name: 'Yes, remove' }));
  };

  it('replaces the address, so Back does not return to the removed server', async () => {
    let removed = false;
    handlers['DELETE /integrations/mcp/servers/files'] = () => { removed = true; return [200, { deleted: 'files' }]; };
    listAnswer = () => {
      const d = LIST();
      return [200, removed ? { ...d, servers: d.servers.slice(0, 1) } : d];
    };
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    const entries = history.length;
    await openServer('files');
    expect(history.length).toBe(entries + 1); // a move adds an entry: the measure can see one
    await confirmRemove('files');
    await waitFor(() => expect(location.hash).toBe(LIST_ADDRESS));
    expect(history.length).toBe(entries + 1); // the removal replaced the entry it was on
  });

  it('the address stays on a server the owner opened while the removal was in flight', async () => {
    const answer = heldRemoval('files');
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await openServer('files');
    await confirmRemove('files');
    await openServer('deepwiki');
    answer();
    await screen.findByText('Removed files.');
    await waitFor(() => expect(link('files')).toBeNull());
    await settle();
    expect(location.hash).toBe('#/integrations/s/deepwiki');
    expect(screen.getByRole('region', { name: 'deepwiki' })).toBeTruthy();
  });

  it('the address is not pulled back from another screen opened while the removal was in flight', async () => {
    const answer = heldRemoval('deepwiki');
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' }); // at the bare address, the server it shows
    await confirmRemove('deepwiki');
    go('#/overview'); // the screen stays rendered, showing the same server at its own default
    answer();
    await screen.findByText('Removed deepwiki.');
    await settle();
    expect(location.hash).toBe('#/overview');
  });
});

describe('Integrations: why a tool is skipped', () => {
  const titled = (text: string) => Array.from(document.querySelectorAll('[title]')).filter((e) => e.getAttribute('title')?.includes(text));

  it('on its own line under the tool, as text, while the row is closed; no title holds it', async () => {
    render(Integrations);
    const deepwiki = await screen.findByRole('region', { name: 'deepwiki' });
    const reason = "it is not in its server's pin (NOUS_MCP_CLIENT_PINNED_SERVERS)";
    const search = row(deepwiki, 'search');
    expect(within(search).getByRole('button', { name: 'Details of search' }).getAttribute('aria-expanded')).toBe('false');
    const why = within(search).getByText(reason);
    expect(why.tagName).toBe('P');
    expect(why.closest('.tool-head')).toBeNull();
    expect(within(search).getByText("Blocked by the server's host settings.").closest('.tool-head')).toBeNull();
    expect(titled(reason)).toEqual([]);
    const list = row(await openServer('files'), 'list');
    expect(within(list).getByText('the server is switched off').closest('.tool-head')).toBeNull();
    expect(titled('the server is switched off')).toEqual([]);
  });

  it('in the check, under the tool that would be skipped, as text; no title holds it', async () => {
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE()];
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fill('wiki2', 'https://mcp.deepwiki.com/mcp');
    await fireEvent.click(screen.getByRole('button', { name: 'Check server' }));
    await within(addCard()).findByText('3 tools listed');
    const broken = within(addCard()).getByRole('checkbox', { name: 'Allow broken' }).closest('li') as HTMLElement;
    expect(within(broken).getByText('would be skipped').closest('.tool-head')).toBeTruthy();
    const why = within(broken).getByText('the input schema is not an object');
    expect(why.tagName).toBe('P');
    expect(why.closest('.tool-head')).toBeNull();
    expect(titled('the input schema is not an object')).toEqual([]);
  });
});

describe('Integrations: a change that fails elsewhere', () => {
  const FAILED = [502, { error: 'could not reach the server: ConnectError' }] as Answer;

  it('is said above the pane, naming its server, while another server is shown', async () => {
    const answer = held();
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => answer.promise;
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Switch off' }));
    const files = await openServer('files');
    answer.resolve(FAILED);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe('deepwiki: Could not reach the server: ConnectError.');
    expect(alert.closest('.split')).toBeNull(); // above the list and the pane
    expect(files.contains(alert)).toBe(false);
    // Its own note waits on its page; the alert above the pane is read once the owner moves on, so it shows once.
    const deepwiki = await openServer('deepwiki');
    expect(within(deepwiki).getByRole('alert').textContent).toBe('Could not reach the server: ConnectError.');
    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(screen.queryByText(/^deepwiki:/)).toBeNull();
  });

  it('is read once the form is opened: the alert above the pane goes', async () => {
    const answer = held();
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => answer.promise;
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Switch off' }));
    await openServer('files');
    answer.resolve(FAILED);
    expect((await screen.findByRole('alert')).textContent).toBe('deepwiki: Could not reach the server: ConnectError.');
    await openForm();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText(/^deepwiki:/)).toBeNull();
  });

  it('is said only beside its server while that server is shown', async () => {
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => FAILED;
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Switch off' }));
    const alert = await within(card('deepwiki')).findByRole('alert');
    await settle();
    expect(screen.getAllByRole('alert')).toEqual([alert]);
  });

  it('a change that succeeds elsewhere says nothing above the pane', async () => {
    const answer = held();
    handlers['PATCH /integrations/mcp/servers/deepwiki'] = () => answer.promise;
    render(Integrations);
    await screen.findByRole('region', { name: 'deepwiki' });
    await fireEvent.click(within(card('deepwiki')).getByRole('button', { name: 'Switch off' }));
    await openServer('files');
    const before = lists();
    answer.resolve([200, { server: null }]);
    await waitFor(() => expect(lists()).toBe(before + 1)); // the change has landed while files is shown
    await settle();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText(/^deepwiki:/)).toBeNull();
    const deepwiki = await openServer('deepwiki'); // its note is on its own page
    expect(within(deepwiki).getByRole('status').textContent).toBe('Switched off: its tools are no longer offered to the agent.');
  });
});
