import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import ModelCalls from './ModelCalls.svelte';
import { initRouter } from '../lib/router';

const HOSTILE = '<img src=x onerror=alert(1)>';
const ID1 = '11111111-1111-4111-8111-111111111111';
const ID2 = '22222222-2222-4222-8222-222222222222';
const ID3 = '33333333-3333-4333-8333-333333333333';
const TURN = '44444444-4444-4444-8444-444444444444';

const row = (id: string, over: Record<string, unknown> = {}) => ({
  id, started_at: '2026-11-07T00:07:00+00:00', purpose: 'main_loop', label_source: 'explicit', call_kind: 'messages',
  model: 'claude-opus-5-5', status: 'ok', http_status: 200, stop_reason: 'end_turn',
  input_uncached: 1200, cache_read: 44900, cache_write: 0, input_total: 46100, output_tokens: 820, duration_ms: 4200,
  attempts: 1, call_count: 1, session_id: 'sess/one', turn_id: TURN, turn_number: 2, error: null, problem: null, ...over,
});
const list = (calls: unknown[], over: Record<string, unknown> = {}) => ({
  enabled: true, text_enabled: true, window_days: 7,
  purposes: [
    { purpose: 'main_loop', calls: 1234, input_total: 1, output_tokens: 1, errors: 0, bucketed: false },
    { purpose: 'embedding', calls: 30, input_total: 1, output_tokens: 0, errors: 0, bucketed: true },
  ],
  calls, next_before: null, ...over,
});
const detailCall = (over: Record<string, unknown> = {}) => ({
  ...row(ID1), group_id: null, is_fallback: false, parent_session_id: null, frame_id: null, subtask_id: null, dag_id: null,
  trace_id: null, error_text: null, context_window_size: 200000, total_tokens_est: 46000, utilization_pct: 23, tools_count: 3,
  tool_names: ['a'], messages_count: 4, loaded_facts: 1, loaded_decisions: 0, loaded_procedures: 0, loaded_episodes: 0,
  cache_break: false, cache_break_components: null, cache_break_tokens_lost: null, ...over,
});
const block = (part: string, ord: number, over: Record<string, unknown> = {}) => ({
  part, ord, size_chars: 4000, tokens_est: 1000, reused_by: 1, ...over,
});
const detail = (over: Record<string, unknown> = {}) => ({
  enabled: true, text_enabled: true, call: detailCall(), attempts: [],
  sections: [{ name: 'System prompt', tokens_est: 3000 }, { name: 'Messages', tokens_est: 1000 }],
  turn: { turn_id: TURN, session_id: 'sess/one', session_title: 'Gap Scout check' },
  text_status: 'ok', blocks: [block('system', 0), block('messages', 0), block('response', 0)], ...over,
});

let urls: string[] = [];
type Reply = { status?: number; body?: unknown; hang?: boolean; gate?: Promise<unknown> };
let respond: (url: string) => Reply = () => ({ body: list([]) });
function install() {
  urls = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(url);
    const r = respond(url);
    if (r.hang) return new Promise(() => {});
    if (r.gate) return r.gate.then((body) => ({ ok: true, status: 200, json: async () => body }) as Response);
    const status = r.status ?? 200;
    return { ok: status < 400, status, json: async () => r.body } as Response;
  }));
}
function go(hash: string) {
  location.hash = hash;
  window.dispatchEvent(new HashChangeEvent('hashchange'));
}
const listUrls = () => urls.filter((u) => u.startsWith('/dashboard/model-calls?'));
const lastList = () => new URL(listUrls()[listUrls().length - 1], 'http://x');

describe('Model calls list', () => {
  beforeEach(() => { respond = () => ({ body: list([row(ID1), row(ID2)]) }); install(); location.hash = ''; initRouter(); go('#/model-calls'); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

  it('shows purpose chips with separators, a per-minute mark, and sets purpose= on click', async () => {
    render(ModelCalls);
    const main = await screen.findByRole('button', { name: /main loop/ });
    expect(main.textContent).toContain('1,234');
    const emb = screen.getByRole('button', { name: /embedding/ });
    expect(emb.textContent).toContain('minute totals');
    expect(main.textContent).not.toContain('minute totals');
    await fireEvent.click(emb);
    await waitFor(() => expect(lastList().searchParams.get('purpose')).toBe('embedding'));
    expect(location.hash).toBe('#/model-calls?purpose=embedding');
  });

  it('a row shows purpose, time, model, tokens, cache share, output and duration, and links to the call', async () => {
    const { container } = render(ModelCalls);
    await screen.findByText('Nov 7 00:07 UTC', { selector: '.when' }).catch(() => null);
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(2));
    const a = container.querySelector('a.row')!;
    expect(a.getAttribute('href')).toBe(`#/model-calls/c/${ID1}`);
    expect(a.textContent).toContain('main loop');
    expect(a.textContent).toContain('Nov 7 00:07 UTC');
    expect(a.textContent).toContain('claude-opus-5-5');
    expect(a.textContent).toContain('46.1K in');
    expect(a.textContent).toContain('97% from cache');
    expect(a.textContent).toContain('820 out');
    expect(a.textContent).toContain('4.2 s');
  });

  it('a failed row with no tokens shows dashes, never NaN, and a text-prefixed problem', async () => {
    respond = () => ({ body: list([row(ID1, {
      status: 'error', http_status: 529, input_uncached: null, cache_read: null, cache_write: null, input_total: null,
      output_tokens: null, duration_ms: null, problem: 'Failed (HTTP 529)', error: 'overloaded',
    })]) });
    const { container } = render(ModelCalls);
    await waitFor(() => expect(container.querySelector('a.row')).toBeTruthy());
    const t = container.querySelector('a.row')!.textContent!;
    expect(t).not.toContain('NaN');
    expect(t).toContain('— in');
    expect(t).toContain('— out');
    expect(t).toMatch(/Problem: Failed \(HTTP 529\)/);
  });

  it('every error row offers "full error" that opens the call detail', async () => {
    respond = () => ({ body: list([row(ID1, { status: 'error', error: 'x'.repeat(300), problem: 'Failed' }), row(ID2)]) });
    render(ModelCalls);
    const link = await screen.findByRole('link', { name: /full error/i });
    expect(link.getAttribute('href')).toBe(`#/model-calls/c/${ID1}`);
    expect(screen.getAllByRole('link', { name: /full error/i }).length).toBe(1);
  });

  it('a minute-bucket row shows how many calls it stands for', async () => {
    go('#/model-calls?purpose=embedding');
    respond = () => ({ body: list([row(ID1, { purpose: 'embedding', call_kind: 'embedding', call_count: 30, input_total: null, output_tokens: null })]) });
    render(ModelCalls);
    expect(await screen.findByText(/×30 calls/)).toBeTruthy();
  });

  it('timestamps go through fmtTime, never Invalid Date', async () => {
    respond = () => ({ body: list([row(ID1, { started_at: 'garbage' })]) });
    const { container } = render(ModelCalls);
    await waitFor(() => expect(container.querySelector('a.row')).toBeTruthy());
    expect(container.textContent).not.toContain('Invalid');
  });

  it('Load more sends the cursor and appends; a poll keeps the loaded pages', async () => {
    const cursor = '2026-11-07T00:00:00+00:00|' + ID2;
    respond = (url) => ({ body: url.includes('before=')
      ? list([row(ID3)], { next_before: null }) : list([row(ID1), row(ID2)], { next_before: cursor }) });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { container } = render(ModelCalls);
    const more = await screen.findByRole('button', { name: 'Load more' });
    await fireEvent.click(more);
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(3));
    expect(lastList().searchParams.get('before')).toBe(cursor);
    const before = listUrls().length;
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(listUrls().length).toBeGreaterThan(before));
    // the poll refetched page one (no before=) and the third row is still there, with no duplicates
    expect(new URL(listUrls()[listUrls().length - 1], 'http://x').searchParams.get('before')).toBeNull();
    expect(container.querySelectorAll('a.row').length).toBe(3);
  });

  it('days comes from the link, is sent on every request, and the empty state names the window', async () => {
    go('#/model-calls?days=30&purpose=main_loop');
    respond = () => ({ body: list([], { window_days: 30 }) });
    render(ModelCalls);
    expect(await screen.findByText('No model calls in the last 30 days match.')).toBeTruthy();
    expect(lastList().searchParams.get('days')).toBe('30');
    await fireEvent.click(screen.getByRole('button', { name: /main loop/ })); // toggles the purpose off
    await waitFor(() => expect(location.hash).toBe('#/model-calls?days=30'));
    expect(lastList().searchParams.get('days')).toBe('30');
  });

  it('the default empty state names 7 days', async () => {
    respond = () => ({ body: list([]) });
    render(ModelCalls);
    expect(await screen.findByText('No model calls in the last 7 days match.')).toBeTruthy();
  });

  it('a bad days value is ignored', async () => {
    go('#/model-calls?days=banana');
    render(ModelCalls);
    await screen.findAllByRole('button', { name: /main loop/ });
    expect(lastList().searchParams.get('days')).toBeNull();
  });

  it('reads filters from the URL query and sends them', async () => {
    go('#/model-calls?model=m1&session_id=s%2F1&turn_id=' + TURN + '&status=problem');
    render(ModelCalls);
    await screen.findAllByRole('button', { name: /main loop/ });
    const p = lastList().searchParams;
    expect(p.get('model')).toBe('m1');
    expect(p.get('session_id')).toBe('s/1');
    expect(p.get('turn_id')).toBe(TURN);
    expect(p.get('status')).toBe('problem');
  });

  it('filter clicks replace the history entry instead of pushing one', async () => {
    render(ModelCalls);
    const sel = (await screen.findByLabelText('Status')) as HTMLSelectElement;
    const n = history.length;
    await fireEvent.change(sel, { target: { value: 'error' } });
    await waitFor(() => expect(lastList().searchParams.get('status')).toBe('error'));
    await fireEvent.click(await screen.findByRole('button', { name: /main loop/ }));
    await waitFor(() => expect(lastList().searchParams.get('purpose')).toBe('main_loop'));
    expect(lastList().searchParams.get('status')).toBe('error');
    expect(history.length).toBe(n);
  });

  it('says recording is off when enabled is false', async () => {
    respond = () => ({ body: list([], { enabled: false }) });
    render(ModelCalls);
    expect(await screen.findByText('Model-call recording is off. Set NOUS_MODEL_CALL_LOG_ENABLED.')).toBeTruthy();
  });

  it('a malformed % in the sub path shows Nothing here', () => {
    go('#/model-calls/c/%E0%A4%A');
    render(ModelCalls);
    expect(screen.getByText(/Nothing here/)).toBeTruthy();
  });
});

describe('Model call detail', () => {
  let bodies: Record<string, { size_chars: number; body: string }> = {};
  beforeEach(() => {
    bodies = {};
    respond = (url) => {
      if (url.includes('/blocks/')) {
        const m = url.match(/blocks\/(\w+)\/(\d+)$/)!;
        const b = bodies[`${m[1]}/${m[2]}`] ?? { size_chars: 5, body: `body of ${m[1]}` };
        return { body: { part: m[1], ord: Number(m[2]), ...b } };
      }
      if (url.startsWith(`/dashboard/model-calls/${ID1}`)) return { body: detail() };
      return { body: list([row(ID1), row(ID2)]) };
    };
    install(); location.hash = ''; initRouter(); go(`#/model-calls/c/${ID1}`);
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('shows the list and the stat tiles, and fetches the detail once', async () => {
    const { container } = render(ModelCalls);
    await screen.findByText('Uncached input');
    for (const l of ['Cache read', 'Cache write', 'Total input', 'Output', 'Duration', 'Attempts', 'Stop reason']) {
      expect(screen.getByText(l)).toBeTruthy();
    }
    expect(container.querySelectorAll('a.row').length).toBe(2);
    expect(urls.filter((u) => u === `/dashboard/model-calls/${ID1}`).length).toBe(1);
    const tiles = container.querySelector('.tiles')!.textContent!;
    expect(tiles).toContain('46.1K');
    expect(tiles).toContain('end_turn');
  });

  it('lists attempts with failures marked in text', async () => {
    const base = respond;
    respond = (u) => u === `/dashboard/model-calls/${ID1}` ? { body: detail({ attempts: [
      { attempt_no: 1, started_at: '2026-11-07T00:07:00+00:00', duration_ms: 900, http_status: 529, error_type: 'overloaded_error', error_text: 'busy' },
      { attempt_no: 2, started_at: '2026-11-07T00:07:02+00:00', duration_ms: 4000, http_status: 200, error_type: null, error_text: null },
    ] }) } : base(u);
    const { container } = render(ModelCalls);
    await screen.findByText('Uncached input');
    const items = container.querySelectorAll('.attempts li');
    expect(items.length).toBe(2);
    expect(items[0].textContent).toContain('Failed');
    expect(items[0].textContent).toContain('529');
    expect(items[1].textContent).not.toContain('Failed');
  });

  it('shows the context sections with proportional bars', async () => {
    const { container } = render(ModelCalls);
    await screen.findByText('What filled the context');
    const bars = container.querySelectorAll('.sections .bar');
    expect(bars.length).toBe(2);
    expect((bars[0] as HTMLElement).style.width).toBe('75%');
    expect((bars[1] as HTMLElement).style.width).toBe('25%');
    expect(screen.getByText('System prompt')).toBeTruthy();
  });

  it('says so when there is no section breakdown', async () => {
    const base = respond;
    respond = (u) => u === `/dashboard/model-calls/${ID1}` ? { body: detail({ sections: [] }) } : base(u);
    render(ModelCalls);
    expect(await screen.findByText(/No section breakdown for this call \(only the main loop records one\)/)).toBeTruthy();
  });

  it('links Made by to the work trace turn, built with routeHref', async () => {
    render(ModelCalls);
    const a = await screen.findByRole('link', { name: /Gap Scout check/ });
    expect(a.getAttribute('href')).toBe(`#/work/s/sess%2Fone/${TURN}`);
  });

  it('no Made by link without a turn session', async () => {
    const base = respond;
    respond = (u) => u === `/dashboard/model-calls/${ID1}` ? { body: detail({ turn: null }) } : base(u);
    render(ModelCalls);
    await screen.findByText('Uncached input');
    expect(screen.queryByRole('link', { name: /Gap Scout/ })).toBeNull();
  });

  it('shows the full error text', async () => {
    const full = 'e'.repeat(1000);
    const base = respond;
    respond = (u) => u === `/dashboard/model-calls/${ID1}` ? { body: detail({ call: detailCall({ status: 'error', error_text: full }) }) } : base(u);
    render(ModelCalls);
    expect(await screen.findByText(full)).toBeTruthy();
  });

  it('block disclosures: titles, notes, Response open, bodies fetched only on open and once', async () => {
    const base = respond;
    respond = (u) => u === `/dashboard/model-calls/${ID1}` ? { body: detail({ blocks: [
      block('system', 0, { reused_by: 41, tokens_est: 3200 }), block('messages', 0), block('messages', 1), block('response', 0),
    ] }) } : base(u);
    const { container } = render(ModelCalls);
    await screen.findByText('Uncached input');
    const sums = Array.from(container.querySelectorAll('details.block > summary')).map((s) => s.textContent!.replace(/\s+/g, ' ').trim());
    expect(sums[0]).toContain('System');
    expect(sums[0]).toContain('about 3,200 tokens, sent in 41 calls in the last 7 days');
    expect(sums[1]).toContain('Messages 1');
    expect(sums[2]).toContain('Messages 2');
    expect(sums[1]).toContain('this call only');
    expect(sums[3]).toContain('Response');
    const dets = container.querySelectorAll<HTMLDetailsElement>('details.block');
    expect(dets[3].open).toBe(true);
    expect(dets[0].open).toBe(false);
    await waitFor(() => expect(urls).toContain(`/dashboard/model-calls/${ID1}/blocks/response/0`));
    expect(urls.some((u) => u.includes('/blocks/system/'))).toBe(false);
    // open the system block twice: one fetch
    dets[0].open = true;
    dets[0].dispatchEvent(new Event('toggle'));
    await waitFor(() => expect(urls).toContain(`/dashboard/model-calls/${ID1}/blocks/system/0`));
    dets[0].open = false; dets[0].dispatchEvent(new Event('toggle'));
    dets[0].open = true; dets[0].dispatchEvent(new Event('toggle'));
    await screen.findByText('body of system');
    expect(urls.filter((u) => u.includes('/blocks/system/0')).length).toBe(1);
    expect(dets[0].textContent).toContain('5 characters');
  });

  it('a 200,000 character body sits in a scroll container; the pre wraps; hostile HTML stays text', async () => {
    bodies['response/0'] = { size_chars: 200000, body: HOSTILE + 'y'.repeat(200000 - HOSTILE.length) };
    const { container } = render(ModelCalls);
    await waitFor(() => expect(container.querySelector('details.block pre')).toBeTruthy());
    const pre = container.querySelector('details.block pre')!;
    expect(pre.classList.contains('wrap')).toBe(true);
    expect(pre.parentElement!.classList.contains('scroll')).toBe(true);
    expect(pre.textContent!.startsWith(HOSTILE)).toBe(true);
    expect(container.querySelector('details.block img')).toBeNull();
    expect(container.textContent).toContain('200,000 characters');
  });

  it.each([
    ['off', 'Prompt text is not recorded. Set NOUS_MODEL_CALL_TEXT_ENABLED.'],
    ['expired', 'Prompt text is kept 7 days and this call is older.'],
    ['not_recorded', 'Prompt text was not recorded for this call.'],
  ])('text_status %s gives its sentence and keeps the numbers', async (status, sentence) => {
    const base = respond;
    respond = (u) => u === `/dashboard/model-calls/${ID1}` ? { body: detail({ text_status: status, blocks: [] }) } : base(u);
    const { container } = render(ModelCalls);
    expect(await screen.findByText(sentence)).toBeTruthy();
    expect(screen.getByText('Uncached input')).toBeTruthy();
    expect(container.querySelectorAll('details.block').length).toBe(0);
  });

  it('a 404 says the call is gone', async () => {
    const base = respond;
    respond = (u) => u === `/dashboard/model-calls/${ID1}` ? { status: 404, body: {} } : base(u);
    render(ModelCalls);
    expect(await screen.findByText('Call not found (numbers are kept 30 days).')).toBeTruthy();
  });

  it('recording off in the detail says so', async () => {
    const base = respond;
    respond = (u) => u === `/dashboard/model-calls/${ID1}` ? { body: detail({ enabled: false, call: null, blocks: [] }) } : base(u);
    render(ModelCalls);
    expect(await screen.findByText('Model-call recording is off. Set NOUS_MODEL_CALL_LOG_ENABLED.')).toBeTruthy();
  });

  it('Back to calls keeps the filters', async () => {
    go(`#/model-calls/c/${ID1}?purpose=main_loop&days=30`);
    render(ModelCalls);
    const back = await screen.findByRole('link', { name: /Back to calls/ });
    expect(back.getAttribute('href')).toBe('#/model-calls?purpose=main_loop&days=30');
  });

  it('a call id with a slash is URL-encoded in the request', async () => {
    go('#/model-calls/c/a%2Fb');
    render(ModelCalls);
    await waitFor(() => expect(urls).toContain('/dashboard/model-calls/a%2Fb'));
  });

  it('within the list, the open call is marked current', async () => {
    const { container } = render(ModelCalls);
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(2));
    const [a, b] = Array.from(container.querySelectorAll('a.row'));
    expect(a.getAttribute('aria-current')).toBe('true');
    expect(b.getAttribute('aria-current')).toBeNull();
    expect(within(container as HTMLElement).getAllByRole('link').length).toBeGreaterThan(2);
  });
});

describe('Model calls fix round 1', () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });
  const start = (hash: string, r: (u: string) => Reply) => {
    respond = r; install(); location.hash = ''; initRouter(); go(hash);
  };

  it('the detail says Loading… while its fetch is pending', async () => {
    start(`#/model-calls/c/${ID1}`, (u) => (u === `/dashboard/model-calls/${ID1}` ? { hang: true } : { body: list([row(ID1)]) }));
    const { container } = render(ModelCalls);
    await waitFor(() => expect(container.querySelector('.detail')).toBeTruthy());
    const msg = within(container.querySelector('.detail') as HTMLElement).getByText('Loading…');
    expect(msg.getAttribute('role')).toBe('status');
  });

  it('the detail says it could not load after a failure that is not a 404', async () => {
    start(`#/model-calls/c/${ID1}`, (u) => (u === `/dashboard/model-calls/${ID1}` ? { status: 400, body: {} } : { body: list([row(ID1)]) }));
    render(ModelCalls);
    const msg = await screen.findByText('Could not load this call.');
    expect(msg.getAttribute('role')).toBe('alert');
  });

  it('two filter writes in one turn keep both', async () => {
    start('#/model-calls', () => ({ body: list([row(ID1)]) }));
    render(ModelCalls);
    const sel = (await screen.findByLabelText('Status')) as HTMLSelectElement;
    const chip = await screen.findByRole('button', { name: /main loop/ });
    sel.value = 'error';
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    chip.click();
    await waitFor(() => expect(location.hash).toContain('purpose=main_loop'));
    expect(location.hash).toContain('status=error');
  });

  it('a poll whose page one overlaps a loaded page lists each call once', async () => {
    const cursor = '2026-11-07T00:00:00+00:00|' + ID2;
    let polls = 0;
    start('#/model-calls', (u) => {
      if (u.includes('before=')) return { body: list([row(ID3)]) };
      polls += 1;
      return { body: polls === 1 ? list([row(ID1), row(ID2)], { next_before: cursor }) : list([row(ID3), row(ID1), row(ID2)], { next_before: cursor }) };
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { container } = render(ModelCalls);
    await fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(3));
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(polls).toBe(2));
    await vi.advanceTimersByTimeAsync(50);
    const hrefs = Array.from(container.querySelectorAll('a.row')).map((a) => a.getAttribute('href'));
    // page one is the fresh poll (new order), the loaded page adds nothing twice
    expect(hrefs).toEqual([ID3, ID1, ID2].map((id) => `#/model-calls/c/${id}`));
  });

  it('a filter change drops the appended pages', async () => {
    const cursor = '2026-11-07T00:00:00+00:00|' + ID2;
    start('#/model-calls', (u) => {
      if (u.includes('before=')) return { body: list([row(ID3)]) };
      return { body: u.includes('purpose=embedding') ? list([row(ID1)]) : list([row(ID1), row(ID2)], { next_before: cursor }) };
    });
    const { container } = render(ModelCalls);
    await fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(3));
    await fireEvent.click(screen.getByRole('button', { name: /embedding/ }));
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(1));
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });

  it('a Load more answer that lands after a filter change is dropped', async () => {
    const cursor = '2026-11-07T00:00:00+00:00|' + ID2;
    let release!: (b: unknown) => void;
    const gate = new Promise<unknown>((r) => { release = r; });
    start('#/model-calls', (u) => {
      if (u.includes('before=')) return { gate };
      return { body: u.includes('purpose=embedding') ? list([row(ID1)]) : list([row(ID1), row(ID2)], { next_before: cursor }) };
    });
    const { container } = render(ModelCalls);
    await fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(urls.some((u) => u.includes('before='))).toBe(true));
    await fireEvent.click(screen.getByRole('button', { name: /embedding/ }));
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(1));
    release(list([row(ID3)]));
    await new Promise((r) => setTimeout(r, 30));
    expect(container.querySelectorAll('a.row').length).toBe(1);
  });

  it('Load more is refused while page one of the new filters is loading, so the old cursor is never used', async () => {
    const cursor = '2026-11-07T00:00:00+00:00|' + ID2;
    start('#/model-calls', () => ({ body: list([row(ID1), row(ID2)], { next_before: cursor }) }));
    render(ModelCalls);
    await screen.findByRole('button', { name: 'Load more' });
    let release!: (b: unknown) => void;
    const gate = new Promise<unknown>((r) => { release = r; });
    respond = (u) => (u.includes('before=') ? { body: list([row(ID3)]) } : { gate });
    const before = urls.length;
    await fireEvent.click(screen.getByRole('button', { name: /embedding/ }));
    await waitFor(() => expect(urls.length).toBe(before + 1));
    const more = screen.getByRole('button', { name: 'Load more' }) as HTMLButtonElement;
    expect(more.disabled).toBe(true);
    await fireEvent.click(more);
    expect(urls.slice(before).some((u) => u.includes('before='))).toBe(false);
    release(list([row(ID1)], { next_before: cursor }));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Load more' }) as HTMLButtonElement).disabled).toBe(false));
  });

  it('Load more stays refused until the LATEST page one lands after two quick filter changes', async () => {
    const cursor = '2026-11-07T00:00:00+00:00|' + ID2;
    start('#/model-calls', () => ({ body: list([row(ID1), row(ID2)], { next_before: cursor }) }));
    render(ModelCalls);
    await screen.findByRole('button', { name: 'Load more' });
    // A fetch that honours the abort signal, as the real one does: a superseded page one rejects with AbortError.
    let release!: (b: unknown) => void;
    vi.stubGlobal('fetch', vi.fn((url: string, init?: { signal?: AbortSignal }) => {
      urls.push(url);
      const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as Response;
      if (url.includes('before=')) return Promise.resolve(ok(list([row(ID3)])));
      return new Promise<Response>((resolve, reject) => {
        release = (b) => resolve(ok(b));
        init?.signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
      });
    }));
    const before = urls.length;
    await fireEvent.click(screen.getByRole('button', { name: /embedding/ })); // change 1: its page one hangs
    await waitFor(() => expect(urls.length).toBe(before + 1));
    await fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'error' } }); // change 2 aborts change 1
    await waitFor(() => expect(urls.length).toBe(before + 2));
    await new Promise((r) => setTimeout(r, 30)); // let the aborted refresh settle
    const more = screen.getByRole('button', { name: 'Load more' }) as HTMLButtonElement;
    expect(more.disabled).toBe(true);
    await fireEvent.click(more);
    expect(urls.slice(before).some((u) => u.includes('before='))).toBe(false);
    release(list([row(ID1)], { next_before: null }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull());
  });

  it('Load more stays enabled while a background poll is in flight', async () => {
    const cursor = '2026-11-07T00:00:00+00:00|' + ID2;
    let polls = 0;
    start('#/model-calls', (u) => {
      if (u.includes('before=')) return { body: list([row(ID3)]) };
      polls += 1;
      return polls === 1 ? { body: list([row(ID1), row(ID2)], { next_before: cursor }) } : { hang: true };
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(ModelCalls);
    await screen.findByRole('button', { name: 'Load more' });
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(polls).toBe(2));
    const more = screen.getByRole('button', { name: 'Load more' }) as HTMLButtonElement;
    expect(more.disabled).toBe(false);
    await fireEvent.click(more);
    await waitFor(() => expect(urls.some((u) => u.includes('before='))).toBe(true));
  });

  it('a block used by this call only still shows its token count', async () => {
    start(`#/model-calls/c/${ID1}`, (u) => (u === `/dashboard/model-calls/${ID1}` ? { body: detail() } : u.includes('/blocks/') ? { body: { part: 'response', ord: 0, size_chars: 5, body: 'x' } } : { body: list([row(ID1)]) }));
    const { container } = render(ModelCalls);
    await waitFor(() => expect(container.querySelector('details.block > summary')).toBeTruthy());
    expect(container.querySelector('details.block > summary')!.textContent).toContain('about 1,000 tokens, this call only');
  });

  it('a background poll does not dim the list', async () => {
    let polls = 0;
    start('#/model-calls', () => { polls += 1; return polls === 1 ? { body: list([row(ID1)]) } : { hang: true }; });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { container } = render(ModelCalls);
    await waitFor(() => expect(container.querySelector('a.row')).toBeTruthy());
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(polls).toBe(2));
    expect(container.querySelector('ul.calls')!.classList.contains('dim')).toBe(false);
  });

  it('the clear-filter buttons have accessible names', async () => {
    start(`#/model-calls?model=m1&session_id=s1&turn_id=${TURN}`, () => ({ body: list([row(ID1)]) }));
    render(ModelCalls);
    for (const n of ['Clear model filter', 'Clear session filter', 'Clear turn filter']) {
      expect(await screen.findByRole('button', { name: n })).toBeTruthy();
    }
  });

  it('an error row with no preview still offers full error', async () => {
    start('#/model-calls', () => ({ body: list([row(ID1, { status: 'error', error: null })]) }));
    render(ModelCalls);
    expect((await screen.findByRole('link', { name: /full error/i })).getAttribute('href')).toBe(`#/model-calls/c/${ID1}`);
  });

  it('a failed block fetch can be retried by closing and opening', async () => {
    let fails = 1;
    start(`#/model-calls/c/${ID1}`, (u) => {
      if (u === `/dashboard/model-calls/${ID1}`) return { body: detail() };
      if (u.includes('/blocks/system/')) { if (fails-- > 0) return { status: 400, body: {} }; return { body: { part: 'system', ord: 0, size_chars: 3, body: 'sys text' } }; }
      if (u.includes('/blocks/')) return { body: { part: 'x', ord: 0, size_chars: 1, body: 'x' } };
      return { body: list([row(ID1)]) };
    });
    const { container } = render(ModelCalls);
    await waitFor(() => expect(container.querySelectorAll('details.block').length).toBe(3));
    const d = container.querySelectorAll<HTMLDetailsElement>('details.block')[0];
    d.open = true; d.dispatchEvent(new Event('toggle'));
    await screen.findByText(/Could not load this text/);
    d.open = false; d.dispatchEvent(new Event('toggle'));
    d.open = true; d.dispatchEvent(new Event('toggle'));
    await screen.findByText('sys text');
    expect(urls.filter((u) => u.includes('/blocks/system/0')).length).toBe(2);
  });
});

describe('Model calls PR4 fix round', () => {
  const ID0 = '00000000-0000-4000-8000-000000000000';
  const cursor = '2026-11-07T00:00:00+00:00|' + ID2;
  const hrefs = (c: HTMLElement) => Array.from(c.querySelectorAll('a.row')).map((a) => a.getAttribute('href')?.split('?')[0]);
  const ids = (...xs: string[]) => xs.map((id) => `#/model-calls/c/${id}`);
  beforeEach(() => { respond = () => ({ body: list([]) }); install(); location.hash = ''; initRouter(); go('#/model-calls'); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

  it('a row that falls off page one when newer calls arrive stays listed above the loaded pages', async () => {
    let polls = 0;
    respond = (u) => {
      if (u.includes('before=')) return { body: list([row(ID3)]) };
      polls += 1;
      return { body: polls === 1 ? list([row(ID1), row(ID2)], { next_before: cursor }) : list([row(ID0), row(ID1)], { next_before: cursor }) };
    };
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { container } = render(ModelCalls);
    await fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(3));
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(polls).toBe(2));
    await vi.advanceTimersByTimeAsync(50);
    expect(hrefs(container)).toEqual(ids(ID0, ID1, ID2, ID3));
  });

  it('without a loaded page there is no seam: a poll replaces page one and the list does not grow', async () => {
    let polls = 0;
    respond = () => {
      polls += 1;
      return { body: polls === 1 ? list([row(ID1), row(ID2)], { next_before: cursor }) : list([row(ID0), row(ID1)], { next_before: cursor }) };
    };
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { container } = render(ModelCalls);
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(2));
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(polls).toBe(2));
    await vi.advanceTimersByTimeAsync(50);
    expect(hrefs(container)).toEqual(ids(ID0, ID1));
  });

  it('a filter change whose page one fails never folds the old filters\' rows into the new list', async () => {
    let errorCalls = 0;
    respond = (u) => {
      if (u.includes('status=error')) {
        errorCalls += 1;
        return errorCalls <= 4 ? { status: 500, body: {} } : { body: list([row(ID0, { status: 'error' })]) };
      }
      if (u.includes('before=')) return { body: list([row(ID3)]) };
      return { body: list([row(ID1), row(ID2)], { next_before: cursor }) };
    };
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { container } = render(ModelCalls);
    await fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await waitFor(() => expect(container.querySelectorAll('a.row').length).toBe(3));
    await fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'error' } });
    await vi.advanceTimersByTimeAsync(8_000); // apiGet gives up after 1 + 2 + 4 s
    await waitFor(() => expect(errorCalls).toBe(4));
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(errorCalls).toBe(5));
    await vi.advanceTimersByTimeAsync(50);
    expect(hrefs(container)).toEqual(ids(ID0));
  });

  it('the Loading… cue shows for the first load but not for a background poll', async () => {
    respond = () => ({ body: list([row(ID1)]) });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { container } = render(ModelCalls);
    await waitFor(() => expect(container.querySelector('a.row')).not.toBeNull());
    vi.stubGlobal('fetch', vi.fn(() => { urls.push('poll'); return new Promise<Response>(() => {}); }));
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(urls).toContain('poll'));
    await vi.advanceTimersByTimeAsync(10);
    expect(container.querySelector('.loading')).toBeNull();
  });

  it('keeps the page heading while a call is open', async () => {
    respond = (u) => ({ body: u.startsWith(`/dashboard/model-calls/${ID1}`) ? detail() : list([row(ID1)]) });
    go(`#/model-calls/c/${ID1}`);
    const { container } = render(ModelCalls);
    await screen.findByRole('link', { name: /Back to calls/ });
    // The list column is hidden on a phone while a call is open, so the heading must live outside it.
    expect(container.querySelectorAll('h1')).toHaveLength(1);
    expect(container.querySelector('.listcol h1')).toBeNull();
    expect(screen.getByRole('heading', { level: 1, name: 'Model calls' })).toBeTruthy();
  });
});
