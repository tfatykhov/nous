import { describe, it, expect } from 'vitest';
import {
  CHANGED_TEXT, FORBIDDEN_TEXT, ceilingLines, clampText, createPayload, isConflict, listGate, nameProblem, prettySchema,
  problemText, refreshLineText, refreshLines, serverPath, serverStatus, shortName, toolChange, toolStatus, toolStatusText,
  urlProblem, type McpServer, type McpTool,
} from './integrations';

const err = (status: number | undefined, body?: unknown, message = `HTTP ${status}`) =>
  Object.assign(new Error(message), { status, body });

const tool = (over: Partial<McpTool> = {}): McpTool => ({
  tool: 'ask_question', name: 'mcp__deepwiki__ask_question', permission: 'deny', registered: false,
  skip_reason: 'its permission is deny', blocked_by_ceiling: false, description: 'Ask.', schema: '{}', ...over,
});

const server = (over: Partial<McpServer> = {}): McpServer => ({
  name: 'deepwiki', url: 'https://mcp.deepwiki.com/mcp', enabled: true, background: false, background_in_effect: false,
  background_blocked_by_ceiling: false, call_timeout_seconds: 30, connected: false, blocked_by_ceiling: false,
  skip_reason: null, tools: [], ...over,
});

describe('the name rule (config.SERVER_NAME and no "__")', () => {
  it.each(['a', 'deepwiki', '0x', 'my-server_2', 'a'.repeat(24), 'a_b-c'])('accepts %s', (name) => {
    expect(nameProblem(name)).toBeNull();
  });
  it.each(['', 'Deepwiki', '-a', '_a', 'a'.repeat(25), 'a__b', 'a b', 'a.b', 'é'])('refuses %j', (name) => {
    expect(nameProblem(name)).not.toBeNull();
  });
  it('says the rule in words', () => {
    expect(nameProblem('A')).toBe('A name is 1 to 24 lower-case letters, digits, - and _, starting with a letter or digit, with no __.');
    expect(nameProblem('')).toBe('Enter a name.');
  });
});

describe('the address rule (config.url_problem)', () => {
  it.each(['https://mcp.deepwiki.com/mcp', 'https://h:8443/x', 'https://[::1]/'])('accepts %s', (url) => {
    expect(urlProblem(url)).toBeNull();
  });
  it.each([
    ['', "Enter the server's address."],
    ['mcp.deepwiki.com', 'This is not a valid address.'],
    ['https://h:99999/', 'This is not a valid address.'],
    ['http://mcp.deepwiki.com/mcp', 'Only https addresses are accepted.'],
    ['ftp://h/', 'Only https addresses are accepted.'],
    ['https://user:pw@h/', 'The address may not carry a user name or password.'],
    ['https://user@h/', 'The address may not carry a user name or password.'],
    ['https://h/?key=1', 'The address may not carry a query string or fragment.'],
    ['https://h/#x', 'The address may not carry a query string or fragment.'],
  ])('refuses %j', (url, why) => {
    expect(urlProblem(url)).toBe(why);
  });
});

describe('the list states', () => {
  it('403 is the proxy state; 404 (routes not mounted) and 503 (no secret, no client) are "not available"', () => {
    expect(listGate(err(403))).toBe('forbidden');
    expect(listGate(err(404))).toBe('unavailable');
    expect(listGate(err(503, { error: 'the MCP client routes are not configured' }))).toBe('unavailable');
  });
  it('a 500, a network failure and no error are not a state: the error pattern or the list shows', () => {
    expect(listGate(err(500))).toBeNull();
    expect(listGate(new TypeError('Failed to fetch'))).toBeNull();
    expect(listGate(null)).toBeNull();
  });
});

describe('a refused request in words', () => {
  it("shows the route's own message as a sentence", () => {
    expect(problemText(err(400, { error: 'name: a server name is 1 to 24 of a-z' }))).toBe('Name: a server name is 1 to 24 of a-z.');
    expect(problemText(err(502, { error: "could not list the server's tools: ConnectError" })))
      .toBe("Could not list the server's tools: ConnectError.");
    expect(problemText(err(500, { error: 'the change was stored, but applying it failed: see the log' })))
      .toBe('The change was stored, but applying it failed: see the log.');
    expect(problemText(err(503, { error: 'the change was stored, but no MCP client is running to apply it' })))
      .toBe('The change was stored, but no MCP client is running to apply it.');
  });
  it('403 asks for the protected address, 415 says JSON; a missing body or a network failure has a fixed sentence', () => {
    expect(problemText(err(403, { error: 'forbidden' }))).toBe(FORBIDDEN_TEXT);
    expect(problemText(err(415, { error: 'Content-Type must be application/json' }))).toBe('The request was not sent as JSON, so it was refused.');
    expect(problemText(err(503))).toBe('MCP servers are not available here.');
    expect(problemText(err(404))).toBe('Not found: the MCP client may be switched off.');
    expect(problemText(err(500, 'not json'))).toBe('The request failed (HTTP 500).');
    expect(problemText(err(undefined, undefined, 'Failed to fetch'))).toBe('No answer from Nous (Failed to fetch).');
    expect(problemText(err(200))).toBe('The answer could not be read: it is not JSON (HTTP 200).');
  });
  it('409 is a conflict', () => {
    expect(isConflict(err(409, { error: 'changed', definitions_sha256: 'b'.repeat(64) }))).toBe(true);
    expect(isConflict(err(400))).toBe(false);
    expect(CHANGED_TEXT).toMatch(/^The server's tools changed since you checked it\./);
  });
});

describe('status words', () => {
  // The reasons are manager.plan's own texts; the words come from the fields, never from matching them.
  it('a registered tool is in use', () => {
    expect(toolStatusText(tool({ permission: 'allow', registered: true, skip_reason: null }))).toBe('in use');
  });
  it('a tool the owner did not allow is not allowed, whatever skipped it first (a stored "ask" too)', () => {
    expect(toolStatusText(tool())).toBe('not allowed');
    expect(toolStatusText(tool({ skip_reason: 'the server is disabled' }))).toBe('not allowed');
    expect(toolStatusText(tool({ permission: 'ask', skip_reason: 'its permission is ask, which is not offered yet' }))).toBe('not allowed');
  });
  it('a tool not allowed that could not be offered anyway (no schema) says why', () => {
    const broken = tool({ name: null, description: null, schema: null, skip_reason: 'the input schema is not an object' });
    expect(toolStatusText(broken)).toBe('not allowed: the input schema is not an object');
  });
  it('an allowed tool that does not register is skipped, with the reason', () => {
    for (const reason of ['the server is disabled', "it is not in its server's pin (NOUS_MCP_CLIENT_PINNED_SERVERS)",
      'a tool of that name is already registered', 'over the 60-tool cap', 'it is not pinned (NOUS_MCP_CLIENT_PINNED_SERVERS)']) {
      expect(toolStatusText(tool({ permission: 'allow', skip_reason: reason }))).toBe(`skipped: ${reason}`);
    }
    expect(toolStatus(tool({ permission: 'allow', skip_reason: null })).detail).toBe('it is not registered');
    // A switched-off server's tools: the server's "disabled" is said in the screen's words (from the field, not the text).
    expect(toolStatusText(tool({ permission: 'allow', skip_reason: 'the server is disabled' }), false)).toBe('skipped: the server is switched off');
    expect(toolStatusText(tool({ permission: 'allow', registered: false, skip_reason: 'the server is disabled' }), true))
      .toBe('skipped: the server is disabled');
    expect(toolStatus(tool({ permission: 'allow', skip_reason: 'x' })).tone).toBe('warn');
  });
  it('a server is switched off, connected or not connected yet', () => {
    expect(serverStatus(server({ enabled: false, connected: true })).word).toBe('switched off');
    expect(serverStatus(server({ connected: true })).word).toBe('connected');
    expect(serverStatus(server()).word).toBe('not connected yet');
  });
  it('the host limits in effect, in words, and none when neither is set', () => {
    expect(ceilingLines({ pinned_servers: false, background_servers: false })).toEqual([]);
    expect(ceilingLines({ pinned_servers: true, background_servers: true })).toEqual([
      'Only the servers and tools pinned on the host (NOUS_MCP_CLIENT_PINNED_SERVERS) can be used, whatever is allowed here.',
      'Background work can use only the servers listed on the host (NOUS_MCP_CLIENT_BACKGROUND_SERVERS).',
    ]);
  });
});

describe('the add request', () => {
  const probe = {
    tools: [
      { tool: 'ask_question', name: 'mcp__deepwiki__ask_question', description: 'Ask.', schema: '{}', skip_reason: null },
      { tool: 'read_wiki', name: 'mcp__deepwiki__read_wiki', description: 'Read.', schema: '{}', skip_reason: null },
      { tool: 'bad', name: null, description: null, schema: null, skip_reason: 'the schema is not an object' },
    ],
    definitions_sha256: 'a'.repeat(64),
  };
  it('names every probed tool by its own name, allowed only when ticked, with the probe\'s hash', () => {
    const allowed = new Map([['read_wiki', true], ['ask_question', false]]);
    expect(createPayload('deepwiki', 'https://mcp.deepwiki.com/mcp', probe, allowed)).toEqual({
      name: 'deepwiki',
      url: 'https://mcp.deepwiki.com/mcp',
      tools: { ask_question: 'deny', read_wiki: 'allow', bad: 'deny' },
      expected_definitions_sha256: 'a'.repeat(64),
    });
  });
  it('nothing ticked is nothing allowed', () => {
    expect(Object.values(createPayload('d', 'https://h/', probe, new Map()).tools)).toEqual(['deny', 'deny', 'deny']);
  });

  // Review B1: a tool named like a member of Object.prototype was read from the prototype (truthy) and sent as allow.
  const HOSTILE = ['constructor', 'toString', 'valueOf', 'hasOwnProperty', '__proto__', 'plain'];
  const hostile = {
    tools: HOSTILE.map((tool) => ({ tool, name: `mcp__x__${tool}`, description: 'd', schema: '{}', skip_reason: null })),
    definitions_sha256: 'a'.repeat(64),
  };
  it('a tool named like a prototype member is not allowed unless ticked, and every name is an own key of the body', () => {
    const body = JSON.stringify(createPayload('x', 'https://h/', hostile, new Map()).tools);
    expect(body).toBe('{"constructor":"deny","toString":"deny","valueOf":"deny","hasOwnProperty":"deny","__proto__":"deny","plain":"deny"}');
  });
  it('only true ticks a tool, and a ticked __proto__ is sent as a key, not set as a prototype', () => {
    const allowed = new Map<string, boolean>([['__proto__', true], ['constructor', true], ['plain', 'yes' as unknown as boolean]]);
    const tools = createPayload('x', 'https://h/', hostile, allowed).tools;
    expect(Object.getPrototypeOf(tools)).toBe(Object.prototype);
    expect(JSON.stringify(tools)).toBe('{"constructor":"allow","toString":"deny","valueOf":"deny","hasOwnProperty":"deny","__proto__":"allow","plain":"deny"}');
  });
  it('a tool change names the tool as an own key, whatever its name', () => {
    expect(JSON.stringify(toolChange('__proto__', true))).toBe('{"tools":{"__proto__":"allow"}}');
    expect(JSON.stringify(toolChange('constructor', false))).toBe('{"tools":{"constructor":"deny"}}');
    expect(toolChange('read_wiki', true)).toEqual({ tools: { read_wiki: 'allow' } });
  });
});

describe('a server\'s route', () => {
  it('encodes the name as one path segment; a valid name passes unchanged', () => {
    expect(serverPath('my_srv-1')).toBe('/integrations/mcp/servers/my_srv-1');
    expect(serverPath('a/b?c#d %')).toBe('/integrations/mcp/servers/a%2Fb%3Fc%23d%20%25');
    expect(serverPath('..')).toBe('/integrations/mcp/servers/..'); // the name rule refuses it; the path builder is not the guard
  });
});

describe('what a refresh changed', () => {
  it('names the new, removed and redefined tools apart from the words; a redefined one keeps its permission', () => {
    const lines = refreshLines({ added: ['a', 'b'], removed: ['c'], changed: ['d'] });
    expect(lines).toEqual([
      { lead: 'New, not allowed until you allow them:', tools: ['a', 'b'], tail: '' },
      { lead: 'No longer listed by the server, now not allowed:', tools: ['c'], tail: '' },
      { lead: 'Definition changed, permission kept:', tools: ['d'], tail: 'Read them again below.' },
    ]);
    expect(lines.map(refreshLineText)).toEqual([
      'New, not allowed until you allow them: a, b.',
      'No longer listed by the server, now not allowed: c.',
      'Definition changed, permission kept: d. Read them again below.',
    ]);
  });
  it('says when nothing changed', () => {
    expect(refreshLines({ added: [], removed: [], changed: [] }).map(refreshLineText)).toEqual(['No change: the server lists the same tools.']);
  });
});

describe('text helpers', () => {
  it('clamps a long description at a word, and leaves a short one whole', () => {
    expect(clampText('short')).toEqual({ text: 'short', cut: false });
    const long = `${'word '.repeat(60)}end`;
    const c = clampText(long, 50);
    expect(c.cut).toBe(true);
    expect(c.text.endsWith('word…')).toBe(true);
    expect(c.text.length).toBeLessThanOrEqual(51);
  });
  it('cuts a very long raw name at 80 characters', () => {
    expect(shortName('read_wiki')).toBe('read_wiki');
    expect(shortName('x'.repeat(80))).toBe('x'.repeat(80));
    expect(shortName('x'.repeat(200))).toBe(`${'x'.repeat(79)}…`);
  });
  it('indents a schema, and shows a non-JSON one as given', () => {
    expect(prettySchema('{"type":"object"}')).toBe('{\n  "type": "object"\n}');
    expect(prettySchema('not json')).toBe('not json');
  });
});
