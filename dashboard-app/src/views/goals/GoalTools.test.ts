import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import GoalTools from './GoalTools.svelte';
import { LEAD, NEVER, NOTES_SERVER_ID, goalToolsAnswer, toolGrant, toolOption } from '../../lib/testing/goalFixtures';
import type { GoalTools as Answer } from '../../lib/goals';

// F115 per-goal tools: the Goals screen's section over GET, PUT and DELETE /goals/{ref}/tools. The fixtures have the
// shapes nous/api/goal_routes.py answers with and the server's own sentences; every id is made up.

type Reply = [number, unknown];
type Call = { url: string; method: string; body: unknown };
let calls: Call[] = [];
let handlers: Record<string, (body: unknown) => Reply> = {};
let current: Answer;

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    const raw = typeof init.body === 'string' ? init.body : undefined;
    const body = raw === undefined ? undefined : JSON.parse(raw);
    calls.push({ url, method, body });
    const handler = handlers[`${method} ${url}`];
    const [status, payload] = handler ? handler(body) : [500, { error: `unexpected ${method} ${url}` }];
    return new Response(JSON.stringify(payload), { status });
  }));
}

const GOAL = { ref: 'G-0001', state: 'active', updated_at: '2026-11-15T11:00:00+00:00' };
const writes = () => calls.filter((c) => c.method !== 'GET');
const section = () => document.querySelector<HTMLElement>('[data-section="goal-tools"]');
const rowOf = (label: string) => screen.getByText(label, { selector: '.name' }).closest('li') as HTMLElement;

beforeEach(() => {
  calls = [];
  handlers = {};
  current = goalToolsAnswer();
  handlers['GET /goals/G-0001/tools'] = () => [200, current];
  install();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('the tools a goal may use without asking', () => {
  it("shows where they apply, what each allowed one does and when it was allowed, and the server's never sentence", async () => {
    render(GoalTools, { goal: GOAL });
    expect(await screen.findByRole('heading', { level: 3, name: 'Tools this goal may use' })).toBeTruthy();
    expect(screen.getByText(LEAD)).toBeTruthy();
    expect(screen.getByText(NEVER)).toBeTruthy();
    const email = rowOf('send_email');
    expect(
      within(email).getByText(
        "Send email, with files this goal's work wrote, to the addresses on your email allowlist. A sent email cannot be taken back.",
      ),
    ).toBeTruthy();
    expect(within(email).getByText('Allowed Nov 15 10:00 UTC.')).toBeTruthy();
    expect(within(email).getByRole('button', { name: 'Remove' })).toBeTruthy();
    expect(screen.getByText('A goal can have at most 20 tools.')).toBeTruthy();
  });

  it('a tool that cannot be allowed now shows why and has no button', async () => {
    render(GoalTools, { goal: GOAL });
    const file = await waitFor(() => rowOf('send_file'));
    expect(within(file).getByText(/^send_file has no Telegram chat set/)).toBeTruthy();
    expect(within(file).queryByRole('button')).toBeNull();
  });

  it('Allow asks first, repeating the sentence and naming the goal, then sends the confirmation', async () => {
    handlers['PUT /goals/G-0001/tools/browser_task'] = (body) => {
      expect(body).toEqual({ confirm: true });
      current = goalToolsAnswer({ grants: [toolGrant('send_email'), toolGrant('browser_task')], available: [] });
      return [200, { ...current, changed: true }];
    };
    render(GoalTools, { goal: GOAL });
    const browser = await waitFor(() => rowOf('browser_task'));
    await fireEvent.click(within(browser).getByRole('button', { name: 'Allow' }));
    expect(writes()).toEqual([]);
    const question = within(browser).getByRole('group', { name: 'Confirm: allow browser_task' });
    expect(
      within(question).getByText(
        /^Allow G-0001 to open web pages on the sites each call names, click and type without asking you each time\? It has no logins/,
      ),
    ).toBeTruthy();
    await fireEvent.click(within(question).getByRole('button', { name: 'Yes, allow' }));
    await waitFor(() =>
      expect(writes().map((c) => [c.method, c.url, c.body])).toEqual([
        ['PUT', '/goals/G-0001/tools/browser_task', { confirm: true }],
      ]),
    );
    expect(await screen.findByText('Allowed.')).toBeTruthy();
    expect(within(rowOf('browser_task')).getByRole('button', { name: 'Remove' })).toBeTruthy();
  });

  it("an MCP tool's confirmation names its server and host, and the request carries the server's id", async () => {
    handlers['PUT /goals/G-0001/tools/mcp__notes__search'] = () => [200, { ...current, changed: true }];
    render(GoalTools, { goal: GOAL });
    const group = await screen.findByRole('region', { name: 'Tools from other servers: notes (notes.example.com)' });
    await fireEvent.click(within(group).getByRole('button', { name: 'Allow' }));
    expect(
      within(group).getByText(
        /^Allow G-0001 to use search on notes \(notes\.example\.com\) without asking you each time\? It can do whatever that tool does there, which Nous cannot check/,
      ),
    ).toBeTruthy();
    await fireEvent.click(within(group).getByRole('button', { name: 'Yes, allow' }));
    await waitFor(() => expect(writes()[0]?.body).toEqual({ confirm: true, server_id: NOTES_SERVER_ID }));
  });

  it('No closes the confirmation and sends nothing', async () => {
    render(GoalTools, { goal: GOAL });
    const browser = await waitFor(() => rowOf('browser_task'));
    await fireEvent.click(within(browser).getByRole('button', { name: 'Allow' }));
    await fireEvent.click(within(browser).getByRole('button', { name: 'No' }));
    expect(within(browser).queryByRole('group')).toBeNull();
    expect(writes()).toEqual([]);
  });

  it('Remove sends DELETE at once and says so', async () => {
    handlers['DELETE /goals/G-0001/tools/send_email'] = (body) => {
      expect(body).toEqual({});
      current = goalToolsAnswer({ grants: [], available: [toolOption('send_email'), ...(current.available ?? [])] });
      return [200, { ...current, changed: true }];
    };
    render(GoalTools, { goal: GOAL });
    const email = await waitFor(() => rowOf('send_email'));
    await fireEvent.click(within(email).getByRole('button', { name: 'Remove' }));
    await waitFor(() =>
      expect(writes().map((c) => [c.method, c.url])).toEqual([['DELETE', '/goals/G-0001/tools/send_email']]),
    );
    expect(await screen.findByText('Removed.')).toBeTruthy();
    expect(await screen.findByText('None yet.')).toBeTruthy();
  });

  it("a refusal shows the server's sentence as it is", async () => {
    const sentence = 'send_email is switched off for background work. Set it to Allowed under Limits and tools first.';
    current = goalToolsAnswer({ grants: [], available: [toolOption('send_email')] });
    handlers['PUT /goals/G-0001/tools/send_email'] = () => [409, { error: sentence }];
    render(GoalTools, { goal: GOAL });
    const email = await waitFor(() => rowOf('send_email'));
    await fireEvent.click(within(email).getByRole('button', { name: 'Allow' }));
    await fireEvent.click(within(email).getByRole('button', { name: 'Yes, allow' }));
    const line = await screen.findByRole('alert');
    expect(line.textContent).toBe(sentence);
  });

  it('a grant not used now says why in words', async () => {
    current = goalToolsAnswer({
      grants: [toolGrant('send_email', { usable: false, not_usable_why: 'Switched off for background work under Limits and tools.' })],
    });
    render(GoalTools, { goal: GOAL });
    const email = await waitFor(() => rowOf('send_email'));
    expect(within(email).getByText('Not used now. Switched off for background work under Limits and tools.')).toBeTruthy();
  });

  it("a paused goal's grant says it is not used now and why", async () => {
    current = goalToolsAnswer({ grants: [toolGrant('send_email', { usable: false, not_usable_why: 'The goal is paused.' })] });
    render(GoalTools, { goal: { ...GOAL, state: 'paused' } });
    const email = await waitFor(() => rowOf('send_email'));
    expect(within(email).getByText('Not used now. The goal is paused.')).toBeTruthy();
  });

  it('a finished goal lists its tools with no button and nothing to add', async () => {
    current = goalToolsAnswer({ can_edit: false });
    render(GoalTools, { goal: { ...GOAL, state: 'cancelled' } });
    await waitFor(() => rowOf('send_email'));
    expect(within(section()!).queryAllByRole('button')).toEqual([]);
    expect(screen.queryByText('Tools you can allow')).toBeNull();
  });

  it('with the grants off on the server, or goals off, there is no section', async () => {
    for (const off of [{ enabled: true, grants_enabled: false }, { enabled: false }]) {
      current = off as Answer;
      render(GoalTools, { goal: GOAL });
      await waitFor(() => expect(calls.length).toBeGreaterThan(0));
      await new Promise((r) => setTimeout(r, 0));
      expect(section()).toBeNull();
      expect(screen.queryByText('Tools this goal may use')).toBeNull();
      cleanup();
      calls = [];
    }
  });

  it("a read that fails says so in the server's words", async () => {
    handlers['GET /goals/G-0001/tools'] = () => [500, { error: 'the request could not be processed' }];
    render(GoalTools, { goal: GOAL });
    expect(await screen.findByText("Could not read this goal's tools. The request could not be processed.")).toBeTruthy();
  });

  it('every text the server sent is a text node', async () => {
    current = goalToolsAnswer({ grants: [toolGrant('send_email', { says: '<b>bold</b>' })] });
    render(GoalTools, { goal: GOAL });
    expect(await screen.findByText('<b>bold</b>')).toBeTruthy();
    expect(section()!.querySelector('b')).toBeNull();
  });
});
