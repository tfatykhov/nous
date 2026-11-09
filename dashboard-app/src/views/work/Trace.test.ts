import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import Trace from './Trace.svelte';
import Work from '../Work.svelte';
import { initRouter } from '../../lib/router';

const HOSTILE = '<img src=x onerror=alert(1)>';
const T1 = '11111111-1111-4111-8111-111111111111';
const T2 = '22222222-2222-4222-8222-222222222222';
const T3 = '33333333-3333-4333-8333-333333333333';
const LEDGER = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';

const call = (i: number, over: Record<string, unknown> = {}) => ({
  id: `call-${i}`, call_index: i, tool: 'recall_deep', tool_called: null, lane: 'memory',
  reason: `Reason ${i}`, input: `input ${i}`, result: `result ${i}`, outcome: 'ran', is_error: false,
  error: null, refused_by: null, duration_ms: 1400, started_at: '2026-11-07T00:07:30+00:00', ledger: null, ...over,
});
const turn = (id: string, n: number | null, over: Record<string, unknown> = {}) => ({
  turn_id: id, turn_number: n, has_turn_row: true, started_at: '2026-11-07T00:07:00+00:00', ended_at: '2026-11-07T00:08:00+00:00',
  frame_id: 'question', trigger: `Message ${n}`,
  memory: { decision: [], fact: [], procedure: [], episode: [] },
  critic: null, claim_check: null, reply: `Reply ${n}`, error: null, tool_call_count: 0,
  model_calls: [], calls: [], ...over,
});
const session = (over: Record<string, unknown> = {}) => ({
  session_id: 'sess/one', parent_session_id: null, kind: 'conversation', work_kind: 'interactive',
  title: 'Gap Scout status check', started_at: '2026-11-07T00:07:00+00:00', ended_at: '2026-11-07T00:31:00+00:00',
  turns: 2, tool_calls: 3, errors: 1, ...over,
});
const trace = (turns: unknown[], over: Record<string, unknown> = {}) => ({
  enabled: true, session: session(), turns, next_after: null, ...over,
});

let urls: string[] = [];
let respond: (url: string) => { status?: number; body?: unknown } = () => ({ body: trace([]) });
function install() {
  urls = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(url);
    const r = respond(url);
    const status = r.status ?? 200;
    return { ok: status < 400, status, json: async () => r.body } as Response;
  }));
}

describe('Work trace', () => {
  beforeEach(() => { install(); location.hash = ''; initRouter(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

  it('fetches the URL-encoded session and shows the header counts', async () => {
    respond = () => ({ body: trace([turn(T1, 1)]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    expect(await screen.findByText('Gap Scout status check')).toBeTruthy();
    expect(urls[0]).toBe('/dashboard/work/sessions/sess%2Fone');
    const head = container.querySelector('header')!;
    expect(head.textContent).toContain('Conversation');
    expect(head.textContent).toContain('Nov 7 00:07 UTC');
    expect(head.textContent).toContain('2 turns');
    expect(head.textContent).toContain('3 tool calls');
    expect(head.textContent).toContain('1 error');
    expect(screen.getByRole('link', { name: /Back to Work/ }).getAttribute('href')).toBe('#/work');
  });

  it('a header with no turns shows a dash for the time, never Invalid Date', async () => {
    respond = () => ({ body: trace([], { session: session({ started_at: null, ended_at: null, turns: 0, tool_calls: 0, errors: 0 }) }) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Gap Scout status check');
    expect(container.textContent).not.toContain('Invalid');
    expect(container.querySelector('header')!.textContent).toContain('—');
    expect(container.querySelector('header')!.textContent).toContain('No errors');
  });

  it('turns render in the order returned with the message and context chips', async () => {
    respond = () => ({ body: trace([
      turn(T1, 1, { memory: { fact: ['a', 'b', 'c', 'd', 'e', 'f'], episode: ['x', 'y'], procedure: ['p'], decision: ['d'] } }),
      turn(T2, 2),
    ]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Message 1');
    const arts = Array.from(container.querySelectorAll('article.turn'));
    expect(arts.map((a) => a.id)).toEqual([`turn-${T1}`, `turn-${T2}`]);
    const first = arts[0] as HTMLElement;
    for (const c of ['6 facts', '2 past conversations', '1 procedure', '1 decision']) {
      expect(within(first).getByText(c)).toBeTruthy();
    }
    expect(first.textContent).toContain('Already in context before acting');
    expect((arts[1] as HTMLElement).textContent).not.toContain('Already in context');
  });

  it('a step shows the reason first, then tool, lane, duration, input and result', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { calls: [call(0, { tool: 'db_query', lane: 'live', duration_ms: 40, tool_called: 'sql' })], tool_call_count: 1 })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reason 0', { selector: '.step *' });
    const step = container.querySelector('.step')!;
    const txt = step.textContent ?? '';
    const order = ['Reason 0', 'db_query', 'Live state', '40 ms', 'input 0', 'Got: result 0'].map((s) => txt.indexOf(s));
    expect(order.every((i) => i >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(txt).toContain('called as sql');
  });

  it('a call with no reason says so in muted text', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { calls: [call(0, { reason: null })] })]) });
    render(Trace, { sessionId: 'sess/one' });
    const el = await screen.findByText('No reason recorded');
    expect(el.classList.contains('muted')).toBe(true);
  });

  it('refused and gated calls carry a badge naming who refused; errors show the error text', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { calls: [
      call(0, { outcome: 'refused', refused_by: 'offered_set' }),
      call(1, { outcome: 'error', is_error: true, error: 'connection reset' }),
      call(2, { outcome: 'gated', refused_by: 'action_gate' }),
    ] })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reason 0', { selector: '.step *' });
    const steps = Array.from(container.querySelectorAll('.step')) as HTMLElement[];
    const b0 = steps[0].querySelector('.badge')!;
    expect(b0.textContent).toMatch(/refused/i);
    expect(b0.textContent).toContain('offered_set');
    expect(within(steps[1]).getByText(/connection reset/)).toBeTruthy();
    expect(steps[1].querySelector('.badge')).toBeNull();
    expect(steps[2].querySelector('.badge')!.textContent).toContain('action_gate');
  });

  it('a ran call has no badge; every other outcome does', async () => {
    const outs = ['ran', 'refused', 'gated', 'suppressed', 'retired', 'bad_input'];
    respond = () => ({ body: trace([turn(T1, 1, { calls: outs.map((o, i) => call(i, { outcome: o })) })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reason 0', { selector: '.step *' });
    const steps = Array.from(container.querySelectorAll('.step')) as HTMLElement[];
    expect(steps[0].querySelector('.badge')).toBeNull();
    outs.slice(1).forEach((o, i) => expect(steps[i + 1].querySelector('.badge')!.textContent).toBe(o));
  });

  it('an error or refused call with no result text shows no empty Got line', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { calls: [
      call(0, { outcome: 'error', is_error: true, error: 'boom', result: '' }),
      call(1, { outcome: 'refused', refused_by: 'policy', result: '' }),
      call(2),
    ] })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reason 0', { selector: '.step *' });
    const steps = Array.from(container.querySelectorAll('.step')) as HTMLElement[];
    expect(steps[0].textContent).toContain('boom');
    expect(steps[0].textContent).not.toContain('Got:');
    expect(steps[1].textContent).not.toContain('Got:');
    expect(steps[2].textContent).toContain('Got: result 2');
  });

  it('the header links all model calls of the conversation even when no turn has main-loop calls', async () => {
    respond = () => ({ body: trace([turn(T1, 1)]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    const link = within(container.querySelector('header') as HTMLElement).getByRole('link', { name: 'all model calls of this conversation' });
    expect(link.getAttribute('href')).toBe('#/model-calls?session_id=sess%2Fone');
  });

  it('a turn with no calls says no tools were used and still shows its reply', async () => {
    respond = () => ({ body: trace([turn(T1, 1)]) });
    render(Trace, { sessionId: 'sess/one' });
    expect(await screen.findByText('No tools used this turn')).toBeTruthy();
    expect(screen.getByText('Reply 1')).toBeTruthy();
  });

  it('claim check lines', async () => {
    respond = () => ({ body: trace([
      turn(T1, 1, { claim_check: { verified: true, claim_count: 2, violation_count: 0 } }),
      turn(T2, 2, { claim_check: { verified: false, claim_count: 3, violation_count: 2 } }),
      turn(T3, 3, { claim_check: null }),
    ]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    const arts = Array.from(container.querySelectorAll('article.turn')) as HTMLElement[];
    expect(arts[0].textContent).toContain('Claim check passed');
    expect(arts[1].textContent).toContain('Claim check found 2 problems');
    expect(arts[2].textContent).not.toContain('Claim check');
  });

  it('a verified turn with no claims says nothing about claim checks', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { claim_check: { verified: true, claim_count: 0, violation_count: 0 } })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    expect(container.textContent).not.toContain('Claim check');
  });

  it('critic line, heuristic disagreement, and a collapsed rationale', async () => {
    respond = () => ({ body: trace([
      turn(T1, 1, { critic: { critic_frame: 'question', heuristic_frame: 'task', agreed: false, rationale: 'It asks, not orders.' } }),
      turn(T2, 2, { critic: { critic_frame: 'task', heuristic_frame: 'task', agreed: true } }),
    ]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    const arts = Array.from(container.querySelectorAll('article.turn')) as HTMLElement[];
    expect(arts[0].textContent).toContain('Critic: question');
    expect(arts[0].textContent).toContain('(heuristic said task)');
    const det = arts[0].querySelector('details')!;
    expect(det.open).toBe(false);
    expect(det.textContent).toContain('It asks, not orders.');
    expect(arts[1].textContent).toContain('Critic: task');
    expect(arts[1].textContent).not.toContain('heuristic said');
    expect(arts[1].querySelector('details')).toBeNull();
    expect(arts[1].textContent).not.toContain('undefined');
  });

  it('unknown ledger status shows the undo statements; success does not; a ref shows the status pill', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { calls: [
      call(0, { tool: 'send_email', lane: 'acts', ledger: { id: LEDGER, status: 'unknown', side_effect_type: 'external', external_ref: null, tombstone: false } }),
      call(1, { tool: 'send_email', lane: 'acts', ledger: { id: 'other-id', status: 'success', side_effect_type: 'external', external_ref: null, tombstone: false } }),
      call(2),
    ] })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reason 0', { selector: '.step *' });
    const steps = Array.from(container.querySelectorAll('.step')) as HTMLElement[];
    expect(steps[0].querySelectorAll('code.sql')).toHaveLength(2);
    expect(steps[0].textContent).toContain(`WHERE id = '${LEDGER}' AND status = 'unknown'`);
    expect(steps[0].querySelector('.pill')!.textContent).toContain('unknown');
    expect(steps[1].querySelector('code.sql')).toBeNull();
    expect(steps[1].querySelector('.pill')!.textContent).toContain('success');
    expect(steps[2].querySelector('.pill')).toBeNull();
  });

  it('the rail has one coloured segment per step, in order', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { calls: [
      call(0, { lane: 'memory' }), call(1, { lane: 'live' }), call(2, { lane: 'acts' }),
    ] })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reason 0', { selector: '.step *' });
    const segs = Array.from(container.querySelectorAll('.rail .seg')) as HTMLElement[];
    expect(segs).toHaveLength(3);
    expect(segs.map((s) => s.style.backgroundColor)).toEqual(['rgb(96, 165, 250)', 'rgb(251, 191, 36)', 'rgb(244, 114, 182)']);
  });

  it('a turn without a turn row is labelled and still lists its calls; repeated turn numbers both render', async () => {
    respond = () => ({ body: trace([
      turn(T1, 1, { has_turn_row: false, trigger: null, reply: null, calls: [call(0)] }),
      turn(T2, 1), turn(T3, 1),
    ]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reason 0', { selector: '.step *' });
    expect(screen.getByText('Turn record missing (single call)')).toBeTruthy();
    expect(container.querySelectorAll('article.turn')).toHaveLength(3);
    expect(container.textContent).not.toContain('null');
  });

  it('main-loop model calls: counted, linked, and each call links to its page', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { model_calls: [
      { id: 'mc-1', purpose: 'main_loop', model: 'claude-x', status: 'ok', input_total: 54500, output_tokens: 300 },
      { id: 'mc-2', purpose: 'main_loop', model: null, status: 'ok', input_total: null, output_tokens: null },
    ] }), turn(T2, 2)]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    const arts = Array.from(container.querySelectorAll('article.turn')) as HTMLElement[];
    const n = within(arts[0]).getByRole('link', { name: '2 main-loop model calls' });
    expect(n.getAttribute('href')).toBe(`#/model-calls?turn_id=${T1}`);
    expect(arts[0].textContent).not.toMatch(/\d model calls/);
    const all = within(arts[0]).getByRole('link', { name: 'all model calls of this conversation' });
    expect(all.getAttribute('href')).toBe('#/model-calls?session_id=sess%2Fone');
    expect(Array.from(arts[0].querySelectorAll('a')).map((a) => a.getAttribute('href'))).toEqual(
      expect.arrayContaining(['#/model-calls/c/mc-1', '#/model-calls/c/mc-2']));
    expect(arts[0].textContent).toContain('54.5K');
    expect(within(arts[1]).queryByRole('link', { name: /main-loop/ })).toBeNull();
  });

  it('1 main-loop call is singular', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { model_calls: [{ id: 'mc-1', purpose: 'main_loop', model: 'm', status: 'ok', input_total: 1, output_tokens: 1 }] })]) });
    render(Trace, { sessionId: 'sess/one' });
    expect(await screen.findByRole('link', { name: '1 main-loop model call' })).toBeTruthy();
  });

  it('long unbroken input wraps anywhere and hostile HTML renders as text', async () => {
    const long = 'x'.repeat(5000);
    respond = () => ({ body: trace([turn(T1, 1, { trigger: HOSTILE, reply: HOSTILE,
      critic: { critic_frame: 'q', agreed: true, rationale: HOSTILE },
      calls: [call(0, { reason: HOSTILE, input: long, result: HOSTILE })] })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText(long);
    expect(screen.getByText(long).classList.contains('wrap')).toBe(true);
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain(HOSTILE);
  });

  it('a 404 says the session is not in the log and links back', async () => {
    respond = () => ({ status: 404, body: { error: 'not found' } });
    render(Trace, { sessionId: 'nope' });
    expect(await screen.findByText('This session is not in the work log (it may be older than 30 days).')).toBeTruthy();
    expect(screen.getByRole('link', { name: /Back to Work/ }).getAttribute('href')).toBe('#/work');
  });

  it('recording off shows the off notice', async () => {
    respond = () => ({ body: { enabled: false, session: null, turns: [], next_after: null } });
    render(Trace, { sessionId: 'sess/one' });
    expect(await screen.findByText(/Tool-call recording is off/)).toBeTruthy();
  });

  it('turnId scrolls to and highlights the turn', async () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    respond = () => ({ body: trace([turn(T1, 1), turn(T2, 2)]) });
    const { container } = render(Trace, { sessionId: 'sess/one', turnId: T2 });
    await waitFor(() => expect(scroll).toHaveBeenCalled());
    const el = container.querySelector(`#turn-${T2}`)!;
    expect(el.classList.contains('target')).toBe(true);
    expect(container.querySelector(`#turn-${T1}`)!.classList.contains('target')).toBe(false);
    expect(scroll.mock.instances[0]).toBe(el);
  });

  it('"What it used" lists each tool with its count and the reasons per turn', async () => {
    respond = () => ({ body: trace([
      turn(T1, 1, { calls: [call(0, { tool: 'db_query', lane: 'live', reason: 'Check A' }), call(1, { tool: 'db_query', lane: 'live', reason: 'Check B' })] }),
      turn(T2, 2, { calls: [call(0, { tool: 'bash', lane: 'workspace', reason: null })] }),
    ]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Check A', { selector: '.step *' });
    const used = container.querySelector('section.used')!;
    expect(used.textContent).toContain('What it used, and why');
    expect(used.textContent).toContain('db_query');
    expect(used.textContent).toContain('2 calls');
    expect(used.textContent).toContain('Turn 1: Check A');
    expect(used.textContent).toContain('Turn 1: Check B');
    expect(used.textContent).toContain('1 call');
  });

  describe('pages and polling', () => {
    const CURSOR = '2026-11-07T00:07:00.123456+00:00|' + T1;
    it('appends the next page through next_after and a poll keeps the loaded pages', async () => {
      let page1Reply = 'Reply 1';
      respond = (url) => url.includes('after=')
        ? { body: trace([turn(T2, 2)], { next_after: null }) }
        : { body: trace([turn(T1, 1, { reply: page1Reply })], { next_after: CURSOR }) };
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      const { container } = render(Trace, { sessionId: 'sess/one' });
      await vi.advanceTimersByTimeAsync(0);
      await screen.findByText('Reply 1');
      await fireEvent.click(screen.getByRole('button', { name: 'Load more turns' }));
      await screen.findByText('Reply 2');
      const afterUrl = new URL(urls[urls.length - 1], 'http://x');
      expect(afterUrl.searchParams.get('after')).toBe(CURSOR);
      expect(screen.queryByRole('button', { name: 'Load more turns' })).toBeNull();
      // 15 s later the first page is refetched: it changed, and page two must still be there.
      page1Reply = 'Reply 1 updated';
      const before = urls.length;
      await vi.advanceTimersByTimeAsync(15000);
      await screen.findByText('Reply 1 updated');
      expect(urls.length).toBeGreaterThan(before);
      expect(urls[before]).not.toContain('after=');
      expect(screen.getByText('Reply 2')).toBeTruthy();
      expect(container.querySelectorAll('article.turn')).toHaveLength(2);
    });
  });

  describe('pages, overlap and scrolling', () => {
    const C1 = '2026-11-07T00:07:00+00:00|' + T1;
    const C2 = '2026-11-07T00:08:00+00:00|' + T2;
    const order = (c: HTMLElement) => Array.from(c.querySelectorAll('article.turn')).map((a) => a.id);

    it('three pages append, every turn stays, in order', async () => {
      respond = (url) => url.includes(encodeURIComponent(C2))
        ? { body: trace([turn(T3, 3)], { next_after: null }) }
        : url.includes('after=') ? { body: trace([turn(T2, 2)], { next_after: C2 }) }
        : { body: trace([turn(T1, 1)], { next_after: C1 }) };
      const { container } = render(Trace, { sessionId: 'sess/one' });
      await screen.findByText('Reply 1');
      await fireEvent.click(screen.getByRole('button', { name: 'Load more turns' }));
      await screen.findByText('Reply 2');
      await fireEvent.click(screen.getByRole('button', { name: 'Load more turns' }));
      await screen.findByText('Reply 3');
      expect(order(container)).toEqual([`turn-${T1}`, `turn-${T2}`, `turn-${T3}`]);
    });

    it('a re-poll whose first page overlaps loaded pages does not duplicate turns', async () => {
      let first = [turn(T1, 1)];
      respond = (url) => url.includes('after=')
        ? { body: trace([turn(T2, 2)], { next_after: null }) }
        : { body: trace(first, { next_after: C1 }) };
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      const { container } = render(Trace, { sessionId: 'sess/one' });
      await vi.advanceTimersByTimeAsync(0);
      await screen.findByText('Reply 1');
      await fireEvent.click(screen.getByRole('button', { name: 'Load more turns' }));
      await screen.findByText('Reply 2');
      first = [turn(T1, 1), turn(T2, 2, { reply: 'Reply 2 fresh' })];   // page one grew over page two
      await vi.advanceTimersByTimeAsync(15000);
      await screen.findByText('Reply 2 fresh');
      expect(order(container)).toEqual([`turn-${T1}`, `turn-${T2}`]);
    });

    it('turnId scrolls once; a later poll does not scroll again', async () => {
      const scroll = vi.fn();
      Element.prototype.scrollIntoView = scroll;
      let reply = 'Reply 2';
      respond = () => ({ body: trace([turn(T1, 1), turn(T2, 2, { reply })]) });
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      render(Trace, { sessionId: 'sess/one', turnId: T2 });
      await vi.advanceTimersByTimeAsync(0);
      await screen.findByText('Reply 2');
      expect(scroll).toHaveBeenCalledTimes(1);
      reply = 'Reply 2 later';
      await vi.advanceTimersByTimeAsync(15000);
      await screen.findByText('Reply 2 later');
      expect(scroll).toHaveBeenCalledTimes(1);
    });
  });

  describe('mounted in Work', () => {
    const go = (hash: string) => { location.hash = hash; window.dispatchEvent(new HashChangeEvent('hashchange')); };
    it('s/<id>/<turn> decodes both segments', async () => {
      Element.prototype.scrollIntoView = vi.fn();
      respond = () => ({ body: trace([turn(T1, 1)]) });
      go(`#/work/s/sess%2Fone/${T1}`);
      render(Work);
      await screen.findByText('Gap Scout status check');
      expect(urls[0]).toBe('/dashboard/work/sessions/sess%2Fone');
    });
    it('a malformed escape shows Nothing here and fetches nothing', async () => {
      go('#/work/s/%E0%A4%A');
      render(Work);
      expect(await screen.findByText(/Nothing here/)).toBeTruthy();
      expect(urls).toHaveLength(0);
    });
  });
});
