import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import FullResult from './FullResult.svelte';

const HOSTILE = '<img src=x onerror=alert(1)>';
let urls: string[] = [];
let respond: (url: string) => { status?: number; body?: unknown; hang?: boolean } = () => ({ body: { call_id: 'c', result: 'x' } });
function install() {
  urls = [];
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    urls.push(url);
    const r = respond(url);
    if (r.hang) return new Promise(() => {});
    const status = r.status ?? 200;
    return Promise.resolve({ ok: status < 400, status, json: async () => r.body } as Response);
  }));
}
const btn = () => screen.getByRole('button', { name: /full result/i });

describe('FullResult', () => {
  beforeEach(() => { respond = () => ({ body: { call_id: 'c', result: 'x' } }); install(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('starts closed with aria-expanded false and no fetch', () => {
    render(FullResult, { callId: 'call/1' });
    expect(btn().textContent).toBe('Show full result');
    expect(btn().getAttribute('aria-expanded')).toBe('false');
    expect(urls).toHaveLength(0);
  });

  it('opening fetches the URL-encoded result once and shows text with the character count', async () => {
    respond = () => ({ body: { call_id: 'call/1', result: 'line one\nline two' } });
    const { container } = render(FullResult, { callId: 'call/1' });
    await fireEvent.click(btn());
    expect(await screen.findByText(/line one/)).toBeTruthy();
    expect(urls).toEqual(['/dashboard/work/calls/call%2F1/result']);
    expect(btn().textContent).toBe('Hide full result');
    expect(btn().getAttribute('aria-expanded')).toBe('true');
    expect(container.textContent).toContain('17 characters');
    expect(container.querySelector('pre')!.textContent).toBe('line one\nline two');
  });

  it('shows Loading while the fetch is pending', async () => {
    respond = () => ({ hang: true });
    render(FullResult, { callId: 'c' });
    await fireEvent.click(btn());
    expect(screen.getByText('Loading…')).toBeTruthy();
  });

  it('closing and reopening does not fetch again', async () => {
    render(FullResult, { callId: 'c' });
    await fireEvent.click(btn());
    await screen.findByText('x');
    await fireEvent.click(btn());
    expect(btn().getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('x')).toBeNull();
    await fireEvent.click(btn());
    expect(await screen.findByText('x')).toBeTruthy();
    expect(urls).toHaveLength(1);
  });

  it('notes the cut when the text holds the middle-cut marker', async () => {
    respond = () => ({ body: { call_id: 'c', result: 'head\n…[middle cut]…\ntail' } });
    const { container } = render(FullResult, { callId: 'c' });
    await fireEvent.click(btn());
    await screen.findByText(/head/);
    expect(container.textContent).toContain('(cut at 20,000 characters)');
  });

  it('does not mention a cut when there is no marker', async () => {
    const { container } = render(FullResult, { callId: 'c' });
    await fireEvent.click(btn());
    await screen.findByText('x');
    expect(container.textContent).not.toContain('cut at');
  });

  it('a null result says it is not kept', async () => {
    respond = () => ({ body: { call_id: 'c', result: null } });
    render(FullResult, { callId: 'c' });
    await fireEvent.click(btn());
    expect(await screen.findByText('Full result not available (kept 7 days).')).toBeTruthy();
  });

  it('an HTTP failure shows an error and Retry refetches', async () => {
    respond = () => ({ status: 500 });
    render(FullResult, { callId: 'c' });
    await fireEvent.click(btn());
    expect(await screen.findByText('Could not load the full result.', {}, { timeout: 4000 })).toBeTruthy();
    respond = () => ({ body: { call_id: 'c', result: 'second try' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('second try')).toBeTruthy();
    expect(urls).toHaveLength(3);
    expect(screen.queryByText('Could not load the full result.')).toBeNull();
  });

  it('hostile HTML stays text', async () => {
    respond = () => ({ body: { call_id: 'c', result: HOSTILE } });
    const { container } = render(FullResult, { callId: 'c' });
    await fireEvent.click(btn());
    await screen.findByText(HOSTILE);
    expect(container.querySelector('img')).toBeNull();
  });

  it('a 5xx is retried once (two requests) before the error shows', async () => {
    respond = () => ({ status: 500 });
    render(FullResult, { callId: 'c' });
    await fireEvent.click(btn());
    expect(await screen.findByText('Could not load the full result.', {}, { timeout: 4000 })).toBeTruthy();
    expect(urls).toHaveLength(2);
  });

  it('a 404 shows the not-kept line, not the error', async () => {
    respond = () => ({ status: 404, body: { error: 'not found' } });
    render(FullResult, { callId: 'c' });
    await fireEvent.click(btn());
    expect(await screen.findByText('Full result not available (kept 7 days).')).toBeTruthy();
    expect(screen.queryByText('Could not load the full result.')).toBeNull();
    expect(urls).toHaveLength(1);
  });

  it('the toggle controls the panel by a sanitised id', async () => {
    const { container } = render(FullResult, { callId: 'call/1 <x>' });
    await fireEvent.click(btn());
    await screen.findByText('x');
    const id = btn().getAttribute('aria-controls')!;
    expect(id).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(container.querySelector(`#${id}`)).toBeTruthy();
  });

  it('after Retry focus stays inside the panel', async () => {
    respond = () => ({ status: 500 });
    const { container } = render(FullResult, { callId: 'c' });
    await fireEvent.click(btn());
    const retry = await screen.findByRole('button', { name: 'Retry' }, { timeout: 4000 });
    retry.focus();
    respond = () => ({ body: { call_id: 'c', result: 'second try' } });
    await fireEvent.click(retry);
    await screen.findByText('second try');
    const panel = container.querySelector(`#${btn().getAttribute('aria-controls')}`)!;
    expect(panel.contains(document.activeElement)).toBe(true);
  });
});
