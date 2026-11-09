import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import ModelCalls from './ModelCalls.svelte';
import { initRouter } from '../lib/router';

const id = (n: number) => `${String(n).padStart(8, '0')}-0000-4000-8000-000000000000`;
const TURN = '44444444-4444-4444-8444-444444444444';
const row = (i: string) => ({
  id: i, started_at: '2026-11-07T00:07:00+00:00', purpose: 'main_loop', label_source: 'explicit', call_kind: 'messages',
  model: 'm', status: 'ok', http_status: 200, stop_reason: 'end_turn', input_uncached: 1, cache_read: 1, cache_write: 0,
  input_total: 2, output_tokens: 1, duration_ms: 1, attempts: 1, call_count: 1, session_id: 's', turn_id: TURN,
  turn_number: 2, error: null, problem: null,
});
const list = (calls: unknown[], next_before: string | null) => ({
  enabled: true, text_enabled: true, window_days: 7,
  purposes: [{ purpose: 'main_loop', calls: 1, input_total: 1, output_tokens: 1, errors: 0, bucketed: false }],
  calls, next_before,
});
let urls: string[] = [];
type Reply = { status?: number; body?: unknown; gate?: Promise<unknown> };
let respond: (url: string) => Reply = () => ({ body: list([], null) });
function install() {
  urls = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(url);
    const r = respond(url);
    if (r.gate) return r.gate.then((body) => ({ ok: true, status: 200, json: async () => body }) as Response);
    const status = r.status ?? 200;
    return { ok: status < 400, status, json: async () => r.body } as Response;
  }));
}
function go(hash: string) { location.hash = hash; window.dispatchEvent(new HashChangeEvent('hashchange')); }
const shown = (c: HTMLElement) => Array.from(c.querySelectorAll('a.row')).map((a) => a.getAttribute('href')!.split('?')[0].split('/c/')[1]);

describe('Model calls list polls with Load more pages', () => {
  beforeEach(() => { install(); location.hash = ''; initRouter(); go('#/model-calls'); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

  it('three polls in a row cascade the fallen rows in order, no duplicates, no gaps', async () => {
    // page size 3; the newest id is the highest number; a newer call arrives before each poll
    let polls = 0;
    respond = (u) => {
      if (u.includes('before=')) return { body: list([row(id(2)), row(id(1))], null) };
      polls += 1;
      const top = 4 + polls; // poll1: 5,4,3  poll2: 6,5,4  poll3: 7,6,5  poll4: 8,7,6
      return { body: list([row(id(top)), row(id(top - 1)), row(id(top - 2))], 'x|' + id(top - 2)) };
    };
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { container } = render(ModelCalls);
    await fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(5));
    for (let p = 2; p <= 4; p++) {
      await vi.advanceTimersByTimeAsync(60_000);
      await waitFor(() => expect(polls).toBe(p));
      await vi.advanceTimersByTimeAsync(50);
    }
    expect(shown(container)).toEqual([8, 7, 6, 5, 4, 3, 2, 1].map(id));
  });

  it('a poll landing while Load more is in flight: no duplicate, no gap, in order', async () => {
    let polls = 0;
    let release!: (v: unknown) => void;
    const gate = new Promise((r) => { release = r; });
    respond = (u) => {
      if (u.includes('before=')) return { gate }; // page two held open
      polls += 1;
      return { body: polls === 1 ? list([row(id(5)), row(id(4))], 'x|' + id(4)) : list([row(id(6)), row(id(5))], 'x|' + id(5)) };
    };
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { container } = render(ModelCalls);
    await fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(urls.some((u) => u.includes('before='))).toBe(true));
    // the poll lands first, pushing 4 off page one while page two is still loading
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(polls).toBe(2));
    await vi.advanceTimersByTimeAsync(50);
    // page two was requested with cursor 4, so it starts below 4: the fallen 4 must be kept while Load more is in flight
    expect(shown(container)).toEqual([6, 5, 4].map(id));
    release(list([row(id(3)), row(id(2))], null));
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(5));
    expect(shown(container)).toEqual([6, 5, 4, 3, 2].map(id));
  });

  it('a filter change after folds starts clean, and later polls under the new filters fold again', async () => {
    let polls = 0;
    respond = (u) => {
      const err = u.includes('status=error');
      if (u.includes('before=')) return { body: list([row(id(err ? 12 : 2))], null) };
      polls += 1;
      if (!err) return { body: polls === 1 ? list([row(id(5)), row(id(4))], 'x|' + id(4)) : list([row(id(6)), row(id(5))], 'x|' + id(5)) };
      return { body: polls === 3 ? list([row(id(15)), row(id(14))], 'x|' + id(14)) : list([row(id(16)), row(id(15))], 'x|' + id(15)) };
    };
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { container } = render(ModelCalls);
    await fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(3));
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(polls).toBe(2));
    await vi.advanceTimersByTimeAsync(50);
    expect(shown(container)).toEqual([6, 5, 4, 2].map(id));
    await fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'error' } });
    await waitFor(() => expect(polls).toBe(3));
    await vi.advanceTimersByTimeAsync(50);
    expect(shown(container)).toEqual([15, 14].map(id)); // nothing of the old list folded in
    await fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(3));
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(polls).toBe(4));
    await vi.advanceTimersByTimeAsync(50);
    expect(shown(container)).toEqual([16, 15, 14, 12].map(id));
  });

  it('60 new calls between polls leave no gap: the pagination restarts from the new page one', async () => {
    let polls = 0;
    respond = (u) => {
      if (u.includes('before=')) return { body: list([row(id(1))], null) };
      polls += 1;
      // page size 3; the second poll shares no row with the first
      return { body: polls === 1 ? list([row(id(4)), row(id(3)), row(id(2))], 'x|' + id(2)) : list([row(id(9)), row(id(8)), row(id(7))], 'y|' + id(7)) };
    };
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { container } = render(ModelCalls);
    await fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(shown(container)).toEqual([4, 3, 2, 1].map(id)));
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(polls).toBe(2));
    await vi.advanceTimersByTimeAsync(50);
    expect(shown(container)).toEqual([9, 8, 7].map(id));
    await fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(urls.some((u) => u.includes('before=y'))).toBe(true));
  });

  it('a failed page-one refresh after a filter change leaves Load more shut: the old cursor is never sent', async () => {
    respond = (u) => {
      if (u.includes('status=error')) return { status: 500, body: {} };
      return { body: list([row(id(4)), row(id(3))], 'x|' + id(3)) };
    };
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(ModelCalls);
    await screen.findByRole('button', { name: 'Load more' });
    await fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'error' } });
    await vi.advanceTimersByTimeAsync(8_000); // apiGet gives up after 1 + 2 + 4 s
    await waitFor(() => expect(urls.filter((u) => u.includes('status=error')).length).toBe(4));
    await vi.advanceTimersByTimeAsync(50);
    const more = screen.getByRole('button', { name: 'Load more' }) as HTMLButtonElement;
    expect(more.disabled).toBe(true);
    await fireEvent.click(more);
    expect(urls.some((u) => u.includes('before='))).toBe(false);
  });
});
