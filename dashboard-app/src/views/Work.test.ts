import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import Work from './Work.svelte';
import { initRouter } from '../lib/router';

const HOSTILE = '<img src=x onerror=alert(1)>';
const CURSOR = '2026-11-07T00:07:00.123456+00:00|sess/two+x';

const session = (id: string, title: string, over: Record<string, unknown> = {}) => ({
  session_id: id, parent_session_id: null, kind: 'conversation', work_kind: 'interactive', context_kind: null,
  title, first_message: `first of ${id}`, started_at: '2026-11-07T00:07:00+00:00', ended_at: '2026-11-07T00:30:00+00:00',
  turns: 4, tool_calls: 9, errors: 0,
  tools: [
    { tool: 'db_query', calls: 3, lane: 'live' },
    { tool: 'recall_deep', calls: 1, lane: 'memory' },
  ],
  flags: [], ...over,
});
const PAGE1 = {
  enabled: true, window_days: 7,
  counts: {
    conversation: { sessions: 62, turns: 161 },
    task: { sessions: 1144, turns: 2000 },
    background: { sessions: 4102, turns: 5000 },
  },
  tool_names: ['bash', 'db_query', 'recall_deep'],
  sessions: [session('s/one', 'Gap Scout status check'), session('s-two', HOSTILE)],
  next_before: CURSOR,
};
const PAGE2 = { ...PAGE1, sessions: [session('s-three', 'Third page row')], next_before: null };

let urls: string[] = [];
let respond: (url: string) => { status?: number; body?: unknown } = () => ({ body: PAGE1 });
function install() {
  urls = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(url);
    const r = respond(url);
    const status = r.status ?? 200;
    return { ok: status < 400, status, json: async () => r.body } as Response;
  }));
}
function go(hash: string) {
  location.hash = hash;
  window.dispatchEvent(new HashChangeEvent('hashchange'));
}
const last = () => new URL(urls[urls.length - 1], 'http://x');
const lastParams = () => last().searchParams;

describe('Work sessions list', () => {
  beforeEach(() => {
    respond = (url) => ({ body: url.includes('before=') ? PAGE2 : PAGE1 });
    install();
    location.hash = '';
    initRouter();
    go('#/work');
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

  it('renders the three count cards and the rows', async () => {
    const { container } = render(Work);
    expect(await screen.findByText('Gap Scout status check')).toBeTruthy();
    const totals = container.querySelector('.stat-grid')!;
    expect(within(totals as HTMLElement).getByText('62')).toBeTruthy();
    expect(within(totals as HTMLElement).getByText('1,144')).toBeTruthy();
    expect(within(totals as HTMLElement).getByText('4,102')).toBeTruthy();
    const row = screen.getByText('Gap Scout status check').closest('li')!;
    expect(within(row).getByText('Conversation')).toBeTruthy();   // kind pill
    expect(within(row).getByText('db_query ×3')).toBeTruthy();    // tool chip with a text label
    expect(within(row).getByText('recall_deep')).toBeTruthy();
    expect(row.textContent).toContain('Nov 7, 00:07 UTC');         // start time
    expect(row.textContent).toContain('4 turns');
    const dot = within(row).getByText('db_query ×3').querySelector('.dot') as HTMLElement;
    expect(dot.getAttribute('aria-hidden')).toBe('true');
    expect(dot.style.backgroundColor).toBe('rgb(251, 191, 36)');   // the live lane
    expect(urls).toHaveLength(1);
  });

  it('a row is a link to the trace with the session id URL-encoded', async () => {
    const { container } = render(Work);
    await screen.findByText('Gap Scout status check');
    const link = screen.getByText('Gap Scout status check').closest('a')!;
    expect(link.getAttribute('href')).toBe('#/work/s/s%2Fone');
    expect(link.closest('ul')).not.toBeNull();
    expect(container.querySelector('tr')).toBeNull();
  });

  it('kind buttons set aria-pressed and refetch with kind=', async () => {
    render(Work);
    await screen.findByText('Gap Scout status check');
    expect(lastParams().get('kind')).toBe('all');
    expect(screen.getByRole('button', { name: 'All but background' }).getAttribute('aria-pressed')).toBe('true');
    await fireEvent.click(screen.getByRole('button', { name: 'Tasks' }));
    await waitFor(() => expect(lastParams().get('kind')).toBe('task'));
    expect(screen.getByRole('button', { name: 'Tasks' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: 'All but background' }).getAttribute('aria-pressed')).toBe('false');
    expect(location.hash).toBe('#/work?kind=task');
    await fireEvent.click(screen.getByRole('button', { name: 'Background' }));
    await waitFor(() => expect(lastParams().get('kind')).toBe('background'));
    await fireEvent.click(screen.getByRole('button', { name: 'Conversations' }));
    await waitFor(() => expect(lastParams().get('kind')).toBe('conversation'));
  });

  it('lens chips set lens= and toggle off on a second click', async () => {
    render(Work);
    await screen.findByText('Gap Scout status check');
    expect(lastParams().get('lens')).toBeNull();
    const chip = () => screen.getByRole('button', { name: 'A tool failed' });
    await fireEvent.click(chip());
    await waitFor(() => expect(lastParams().get('lens')).toBe('error'));
    expect(chip().getAttribute('aria-pressed')).toBe('true');
    await fireEvent.click(chip());
    await waitFor(() => expect(lastParams().get('lens')).toBeNull());
    expect(chip().getAttribute('aria-pressed')).toBe('false');
  });

  it('the tool select sets tool=', async () => {
    render(Work);
    await screen.findByText('Gap Scout status check');
    const select = screen.getByLabelText('Tool') as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['', 'bash', 'db_query', 'recall_deep']);
    await fireEvent.change(select, { target: { value: 'bash' } });
    await waitFor(() => expect(lastParams().get('tool')).toBe('bash'));
  });

  it('the search box sets q= after 300 ms of quiet', async () => {
    render(Work);
    await screen.findByText('Gap Scout status check');
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const before = urls.length;
    const box = screen.getByLabelText('Search') as HTMLInputElement;
    await fireEvent.input(box, { target: { value: 'gap' } });
    await vi.advanceTimersByTimeAsync(200);
    await fireEvent.input(box, { target: { value: 'gap scout' } });
    await vi.advanceTimersByTimeAsync(200);
    expect(urls.length).toBe(before);                     // 400 ms in, but the timer restarted at 200
    await vi.advanceTimersByTimeAsync(150);
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    vi.useRealTimers();
    await waitFor(() => expect(lastParams().get('q')).toBe('gap scout'));
    expect(urls.length).toBe(before + 1);
  });

  it('filters survive a reload: they are read from the URL query', async () => {
    go('#/work?kind=task&lens=repeat&tool=bash&q=hello%20there');
    render(Work);
    await screen.findByText('Gap Scout status check');
    const p = lastParams();
    expect([p.get('kind'), p.get('lens'), p.get('tool'), p.get('q')]).toEqual(['task', 'repeat', 'bash', 'hello there']);
    expect(screen.getByRole('button', { name: 'Tasks' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: 'Same call repeated' }).getAttribute('aria-pressed')).toBe('true');
    expect((screen.getByLabelText('Tool') as HTMLSelectElement).value).toBe('bash');
    expect((screen.getByLabelText('Search') as HTMLInputElement).value).toBe('hello there');
  });

  it('Load more appears only with next_before, sends the cursor encoded and appends the next page', async () => {
    render(Work);
    await screen.findByText('Gap Scout status check');
    await fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(await screen.findByText('Third page row')).toBeTruthy();
    expect(urls[urls.length - 1]).toContain('before=2026-11-07T00%3A07%3A00.123456%2B00%3A00%7Csess%2Ftwo%2Bx');
    expect(lastParams().get('before')).toBe(CURSOR);
    expect(screen.getByText('Gap Scout status check')).toBeTruthy();   // first page stays
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });

  it('no Load more on the last page', async () => {
    respond = () => ({ body: { ...PAGE1, next_before: null } });
    render(Work);
    await screen.findByText('Gap Scout status check');
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });

  it('a filter change drops the appended pages', async () => {
    render(Work);
    await screen.findByText('Gap Scout status check');
    await fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByText('Third page row');
    await fireEvent.click(screen.getByRole('button', { name: 'Tasks' }));
    await waitFor(() => expect(screen.queryByText('Third page row')).toBeNull());
    expect(screen.getByText('Gap Scout status check')).toBeTruthy();
  });

  it('Refresh refetches once, and nothing polls', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout'] });
    render(Work);
    await vi.advanceTimersByTimeAsync(50);
    expect(screen.getByText('Gap Scout status check')).toBeTruthy();
    expect(urls).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(urls).toHaveLength(1);
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await vi.advanceTimersByTimeAsync(0);
    expect(urls).toHaveLength(2);
  });

  it('says the log is off, and shows no table, when enabled is false', async () => {
    respond = () => ({ body: { enabled: false, window_days: 7, counts: { conversation: { sessions: 0, turns: 0 }, task: { sessions: 0, turns: 0 }, background: { sessions: 0, turns: 0 } }, tool_names: [], sessions: [], next_before: null } });
    const { container } = render(Work);
    expect(await screen.findByText('Tool-call recording is off. Set NOUS_TOOL_CALL_LOG_ENABLED to record what Nous does.')).toBeTruthy();
    expect(screen.queryByText('No sessions match.')).toBeNull();
    expect(container.querySelector('ul.sessions')).toBeNull();
    expect(container.querySelector('table')).toBeNull();
  });

  it('says No sessions match. for an empty list with the log on', async () => {
    respond = () => ({ body: { ...PAGE1, sessions: [], next_before: null } });
    render(Work);
    expect(await screen.findByText('No sessions match.')).toBeTruthy();
    expect(screen.queryByText(/recording is off/)).toBeNull();
  });

  it('shows the StaleBadge error state when the request fails', async () => {
    respond = () => ({ status: 400, body: { error: 'bad' } });
    const { container } = render(Work);
    await waitFor(() => expect(container.querySelector('.stale-badge.error')).not.toBeNull());
    expect(container.querySelector('ul.sessions')).toBeNull();
  });

  it('renders a hostile title as text, never as an element', async () => {
    const { container } = render(Work);
    expect(await screen.findByText(HOSTILE)).toBeTruthy();
    expect(container.querySelector('img')).toBeNull();
  });

  it('PIN: no Work file uses the raw-HTML directive', () => {
    for (const f of ['Work.svelte', 'work/SessionList.svelte']) {
      expect(readFileSync(resolve(__dirname, f), 'utf-8')).not.toContain('{@html');
    }
  });
});

describe('Work host dispatch', () => {
  beforeEach(() => {
    respond = () => ({ body: PAGE1 });
    install();
    location.hash = '';
    initRouter();
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('has Sessions and Patterns in the sub navigation, Sessions current', async () => {
    go('#/work');
    render(Work);
    const nav = screen.getByRole('navigation', { name: 'Work views' });
    const links = within(nav).getAllByRole('link');
    expect(links.map((a) => a.textContent)).toEqual(['Sessions', 'Patterns']);
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['#/work', '#/work/patterns']);
    expect(links[0].getAttribute('aria-current')).toBe('page');
    await screen.findByText('Gap Scout status check');
  });

  it('shows a stub for patterns and does not fetch the list', async () => {
    go('#/work/patterns');
    render(Work);
    expect(await screen.findByText('patterns')).toBeTruthy();
    expect(urls).toHaveLength(0);
  });

  it('shows a stub for a trace', async () => {
    go('#/work/s/abc%2Fdef/turn1');
    render(Work);
    expect(await screen.findByText('trace')).toBeTruthy();
    expect(urls).toHaveLength(0);
  });

  it('treats a malformed escape in the path as not found, without throwing', async () => {
    go('#/work/s/%E0%A4%A');
    render(Work);
    expect(await screen.findByText('Nothing here.')).toBeTruthy();
    expect(urls).toHaveLength(0);
  });

  it('switches from the list to a stub when the hash changes', async () => {
    go('#/work');
    render(Work);
    await screen.findByText('Gap Scout status check');
    go('#/work/patterns');
    expect(await screen.findByText('patterns')).toBeTruthy();
    expect(screen.queryByText('Gap Scout status check')).toBeNull();
  });
});
