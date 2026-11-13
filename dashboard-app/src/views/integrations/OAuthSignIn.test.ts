import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import OAuthSignIn from './OAuthSignIn.svelte';

// F112 PR 2d (Codex): a sign-in panel closed mid-request asks nothing more and reports nothing, whatever answers late.
// Every value is made up.

const START = {
  flow_id: 'flow-1', authorization_url: 'https://auth.example.com/authorize?state=s', authorization_host: 'auth.example.com',
  token_host: 'auth.example.com', registration: 'dynamic', scope: null, expires_in: 600,
};

type Held = { promise: Promise<Response>; resolve: (r: Response) => void };
function held(): Held {
  let resolve!: (r: Response) => void;
  const promise = new Promise<Response>((r) => { resolve = r; });
  return { promise, resolve };
}
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

let calls: string[] = [];
let answer: (url: string, method: string) => Promise<Response>;

beforeEach(() => {
  calls = [];
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    calls.push(`${method} ${url}`);
    return answer(url, method);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

const polls = () => calls.filter((c) => c.startsWith('GET /integrations/mcp/oauth/flows/')).length;

describe('OAuthSignIn: closed mid-request', () => {
  it('a poll answered after the panel closed reports nothing and asks no more', async () => {
    const late = held();
    answer = (url, method) => (method === 'POST' ? Promise.resolve(json(START)) : late.promise);
    const onDone = vi.fn();
    const { unmount } = render(OAuthSignIn, { props: { name: 'alpha', url: 'https://alpha.example.com/mcp', idPrefix: 't', onDone } });
    await fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await screen.findByRole('link', { name: 'Continue to sign in' });
    await vi.advanceTimersByTimeAsync(2000); // the first poll is in flight
    expect(polls()).toBe(1);
    unmount();
    late.resolve(json({ status: 'complete', reason: null, stored: false }));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(onDone).not.toHaveBeenCalled();
    expect(polls()).toBe(1);
  });

  it('a poll that fails after the panel closed schedules no other', async () => {
    const late = held();
    answer = (url, method) => (method === 'POST' ? Promise.resolve(json(START)) : late.promise);
    const { unmount } = render(OAuthSignIn, { props: { name: 'alpha', url: 'https://alpha.example.com/mcp', idPrefix: 't', onDone: vi.fn() } });
    await fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await screen.findByRole('link', { name: 'Continue to sign in' });
    await vi.advanceTimersByTimeAsync(2000);
    unmount();
    late.resolve(new Response('', { status: 502 })); // the miss that would otherwise be asked again
    await vi.advanceTimersByTimeAsync(10_000);
    expect(polls()).toBe(1);
  });

  it('a start answered after the panel closed starts no polling', async () => {
    const late = held();
    answer = (url, method) => (method === 'POST' ? late.promise : Promise.resolve(json({ status: 'complete', reason: null, stored: false })));
    const onDone = vi.fn();
    const { unmount } = render(OAuthSignIn, { props: { name: 'alpha', url: 'https://alpha.example.com/mcp', idPrefix: 't', onDone } });
    await fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    unmount();
    late.resolve(json(START));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(polls()).toBe(0);
    expect(onDone).not.toHaveBeenCalled();
  });
});
