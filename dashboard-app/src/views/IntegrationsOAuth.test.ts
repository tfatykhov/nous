import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within, waitFor } from '@testing-library/svelte';
import Integrations from './Integrations.svelte';

// F112 PR 2d: OAuth sign-in on the Integrations screen. The fixtures have the shapes nous/api/mcp_client_routes.py
// answers with; every value is made up.

type Answer = [number, unknown];
type Call = { url: string; method: string; body: unknown };

const AUTH = (over: Record<string, unknown> = {}) => ({ kind: 'none', header_names: [] as string[], updated_at: null, status: 'ok', reason: null, ...over });
const SERVER = (name: string, auth = AUTH()) => ({
  name, url: `https://${name}.example.com/mcp`, enabled: true, background: false, background_in_effect: false,
  background_blocked_by_ceiling: false, call_timeout_seconds: 30, connected: false, blocked_by_ceiling: false, skip_reason: null,
  auth, tools: [],
});
const LIST = (servers: unknown[] = [], oauth = true) => ({
  ceilings: { pinned_servers: false, background_servers: false }, secrets_available: true, oauth_available: oauth, servers,
});
const START = (over: Record<string, unknown> = {}) => ({
  flow_id: 'flow-1', authorization_url: 'https://auth.example.com/authorize?client_id=c&state=s', authorization_host: 'auth.example.com',
  token_host: 'auth.example.com', registration: 'dynamic', scope: 'read', expires_in: 600, ...over,
});
const PROBE = { tools: [{ tool: 'search', name: 'mcp__alpha__search', description: 'Search.', schema: '{"type":"object"}', skip_reason: null }], definitions_sha256: 'a'.repeat(64) };

let calls: Call[] = [];
let listAnswer: () => Answer = () => [200, LIST()];
let handlers: Record<string, (body: unknown) => Answer> = {};

beforeEach(() => {
  calls = [];
  listAnswer = () => [200, LIST()];
  handlers = {};
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
    calls.push({ url, method, body });
    const [status, payload] = method === 'GET' && url === '/integrations/mcp/servers'
      ? listAnswer()
      : handlers[`${method} ${url}`]?.(body) ?? [500, { error: `unexpected ${method} ${url}` }];
    return new Response(JSON.stringify(payload), { status });
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

const addCard = () => screen.getByRole('region', { name: 'Add a server' });
const posts = () => calls.filter((c) => c.method !== 'GET').map((c) => ({ method: c.method, url: c.url, body: c.body }));

async function chooseSignIn() {
  render(Integrations);
  await screen.findByRole('region', { name: 'Add a server' });
  await fireEvent.input(within(addCard()).getByLabelText('Name'), { target: { value: 'alpha' } });
  await fireEvent.input(within(addCard()).getByLabelText('Address'), { target: { value: 'https://alpha.example.com/mcp' } });
  await fireEvent.click(within(addCard()).getByRole('radio', { name: 'Sign in (OAuth)' }));
}

describe('Integrations: OAuth sign-in for a new server', () => {
  it('Sign in shows both hosts and a link that opens a new tab; once signed in, Check and Add send the sign-in', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const states: Answer[] = [[200, { status: 'pending', reason: null, stored: false }], [200, { status: 'complete', reason: null, stored: false }]];
    handlers['POST /integrations/mcp/oauth/start'] = () => [200, START()];
    handlers['GET /integrations/mcp/oauth/flows/flow-1'] = () => states.shift() ?? [200, { status: 'complete', reason: null, stored: false }];
    handlers['POST /integrations/mcp/probe'] = () => [200, PROBE];
    handlers['POST /integrations/mcp/servers'] = () => [201, { created: true, definitions_sha256: 'a'.repeat(64), credentials: 'set', server: null }];
    await chooseSignIn();
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Sign in' }));
    expect(await within(addCard()).findByText('You will sign in at auth.example.com. Tokens are fetched from auth.example.com.')).toBeTruthy();
    const link = within(addCard()).getByRole('link', { name: 'Continue to sign in' });
    expect([link.getAttribute('href'), link.getAttribute('target'), link.getAttribute('rel')]).toEqual([
      'https://auth.example.com/authorize?client_id=c&state=s', '_blank', 'noopener noreferrer',
    ]);
    expect(within(addCard()).queryByRole('alert')).toBeNull(); // the hosts are the same: no warning

    await vi.advanceTimersByTimeAsync(2000); // pending
    await vi.advanceTimersByTimeAsync(2000); // complete
    expect(await within(addCard()).findByText('Signed in.')).toBeTruthy();
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Check server' }));
    await fireEvent.click(await within(addCard()).findByRole('button', { name: 'Add server' }));
    await waitFor(() => expect(posts().length).toBe(3));
    expect(posts()).toEqual([
      { method: 'POST', url: '/integrations/mcp/oauth/start', body: { name: 'alpha', url: 'https://alpha.example.com/mcp' } },
      { method: 'POST', url: '/integrations/mcp/probe', body: { name: 'alpha', url: 'https://alpha.example.com/mcp', oauth_flow_id: 'flow-1' } },
      {
        method: 'POST', url: '/integrations/mcp/servers',
        body: { name: 'alpha', url: 'https://alpha.example.com/mcp', tools: { search: 'deny' }, expected_definitions_sha256: 'a'.repeat(64), oauth_flow_id: 'flow-1' },
      },
    ]);
  });

  it('warns when the token host is not the sign-in host, and offers no link to an address that is not https', async () => {
    handlers['POST /integrations/mcp/oauth/start'] = () => [200, START({ token_host: 'tokens.example.net', authorization_url: 'http://auth.example.com/authorize' })];
    handlers['GET /integrations/mcp/oauth/flows/flow-1'] = () => [200, { status: 'pending', reason: null, stored: false }];
    await chooseSignIn();
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Sign in' }));
    expect((await within(addCard()).findByRole('alert')).textContent).toBe(
      'The token host is not the sign-in host: continue only if you trust tokens.example.net.',
    );
    expect(within(addCard()).queryByRole('link', { name: 'Continue to sign in' })).toBeNull();
  });

  it('the pasted address completes the sign-in; a refused one says why', async () => {
    handlers['POST /integrations/mcp/oauth/start'] = () => [200, START()];
    handlers['GET /integrations/mcp/oauth/flows/flow-1'] = () => [200, { status: 'pending', reason: null, stored: false }];
    const answers: Answer[] = [
      [400, { error: "this is not the sign-in's return address: paste the whole address from the address bar" }],
      [200, { status: 'complete', reason: null, stored: false }],
    ];
    handlers['POST /integrations/mcp/oauth/complete'] = () => answers.shift()!;
    await chooseSignIn();
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Sign in' }));
    const field = await within(addCard()).findByLabelText('Paste the address instead');
    await fireEvent.input(field, { target: { value: 'https://elsewhere.example.com/?code=c' } });
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Use this address' }));
    expect((await within(addCard()).findByRole('alert')).textContent).toBe(
      "This is not the sign-in's return address: paste the whole address from the address bar.",
    );
    await fireEvent.input(field, { target: { value: 'https://nous.example.com/integrations/mcp/oauth/callback?code=c&state=s' } });
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Use this address' }));
    expect(await within(addCard()).findByText('Signed in.')).toBeTruthy();
    expect(posts().at(-1)).toEqual({
      method: 'POST', url: '/integrations/mcp/oauth/complete',
      body: { flow_id: 'flow-1', callback_url: 'https://nous.example.com/integrations/mcp/oauth/callback?code=c&state=s' },
    });
  });

  it('the Advanced client secret is sent with its client id and cleared once the sign-in starts', async () => {
    handlers['POST /integrations/mcp/oauth/start'] = () => [200, START({ registration: 'provided' })];
    handlers['GET /integrations/mcp/oauth/flows/flow-1'] = () => [200, { status: 'pending', reason: null, stored: false }];
    await chooseSignIn();
    await fireEvent.input(within(addCard()).getByLabelText('Client id'), { target: { value: 'my-app' } });
    const secret = within(addCard()).getByLabelText('Client secret (if the app has one)') as HTMLInputElement;
    expect(secret.type).toBe('password');
    await fireEvent.input(secret, { target: { value: 'test-secret-0000' } });
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Sign in' }));
    await within(addCard()).findByRole('link', { name: 'Continue to sign in' });
    expect(posts()[0].body).toEqual({ name: 'alpha', url: 'https://alpha.example.com/mcp', client_id: 'my-app', client_secret: 'test-secret-0000' });
    await fireEvent.click(within(addCard()).getByRole('button', { name: 'Start again' }));
    expect(posts()[1].body).toEqual({ name: 'alpha', url: 'https://alpha.example.com/mcp', client_id: 'my-app' });
  });

  it('without the public https address, the choice is off and says what it needs', async () => {
    listAnswer = () => [200, LIST([], false)];
    render(Integrations);
    await screen.findByRole('region', { name: 'Add a server' });
    expect((within(addCard()).getByRole('radio', { name: 'Sign in (OAuth)' }) as HTMLInputElement).disabled).toBe(true);
    expect(within(addCard()).getByText('Sign-in needs NOUS_A2UI_PUBLIC_BASE_URL set to the https address of this Nous.')).toBeTruthy();
  });
});

describe('Integrations: OAuth on a server card', () => {
  it('a signed-in server says so, signs out after a confirmation that the grant stays, and can sign in again', async () => {
    listAnswer = () => [200, LIST([SERVER('alpha', AUTH({ kind: 'oauth' }))])];
    handlers['POST /integrations/mcp/servers/alpha/sign-out'] = () => [200, { credentials: 'removed', server: null }];
    render(Integrations);
    const card = await screen.findByRole('region', { name: 'alpha' });
    expect(within(card).getByText('Signed in with OAuth.')).toBeTruthy();
    await fireEvent.click(within(card).getByRole('button', { name: 'Sign out' }));
    expect(within(card).getByText(/the access you granted at the provider stays until you revoke it there/)).toBeTruthy();
    await fireEvent.click(within(card).getByRole('button', { name: 'Yes, sign out' }));
    expect((await within(card).findByRole('status')).textContent).toBe('Signed out: calls to this server now go without the sign-in.');
    expect(posts()).toEqual([{ method: 'POST', url: '/integrations/mcp/servers/alpha/sign-out', body: undefined }]);
    await fireEvent.click(within(card).getByRole('button', { name: 'Sign in again' }));
    expect(within(card).getByRole('group', { name: 'Sign in for alpha' })).toBeTruthy();
  });

  it('Sign in again stores the new sign-in with the server at once', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    listAnswer = () => [200, LIST([SERVER('alpha', AUTH({ kind: 'oauth', status: 'needs_sign_in' }))])];
    handlers['POST /integrations/mcp/oauth/start'] = () => [200, START({ registration: 'reused' })];
    handlers['GET /integrations/mcp/oauth/flows/flow-1'] = () => [200, { status: 'complete', reason: null, stored: true }];
    render(Integrations);
    const card = await screen.findByRole('region', { name: 'alpha' });
    expect(within(card).getByText('needs sign-in')).toBeTruthy();
    expect(within(card).getByText('Needs sign-in: sign in again.')).toBeTruthy();
    await fireEvent.click(within(card).getByRole('button', { name: 'Sign in again' }));
    await fireEvent.click(within(within(card).getByRole('group', { name: 'Sign in for alpha' })).getByRole('button', { name: 'Sign in' }));
    expect(posts()[0]).toEqual({ method: 'POST', url: '/integrations/mcp/oauth/start', body: { name: 'alpha', url: 'https://alpha.example.com/mcp' } });
    await vi.advanceTimersByTimeAsync(2000);
    expect((await within(card).findByRole('status')).textContent).toBe('Signed in: the next call to this server uses the new sign-in.');
  });
});
