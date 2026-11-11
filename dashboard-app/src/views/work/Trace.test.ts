import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import Trace from './Trace.svelte';
import Work from '../Work.svelte';
import { setLastListQuery } from '../../lib/work';
import { initRouter } from '../../lib/router';

const HOSTILE = '<img src=x onerror=alert(1)>';
const T1 = '11111111-1111-4111-8111-111111111111';
const T2 = '22222222-2222-4222-8222-222222222222';
const T3 = '33333333-3333-4333-8333-333333333333';
const LEDGER = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';

const call = (i: number, over: Record<string, unknown> = {}) => ({
  id: `call-${i}`, call_index: i, tool: 'recall_deep', tool_called: null, lane: 'memory',
  reason: `Reason ${i}`, input: `input ${i}`, result: `result ${i}`, outcome: 'ran', is_error: false,
  error: null, refused_by: null, duration_ms: 1400, started_at: '2026-11-07T00:07:30+00:00', ledger: null, result_full: false, ...over,
});
const turn = (id: string, n: number | null, over: Record<string, unknown> = {}) => ({
  turn_id: id, turn_number: n, has_turn_row: true, started_at: '2026-11-07T00:07:00+00:00', ended_at: '2026-11-07T00:08:00+00:00',
  frame_id: 'question', trigger: `Message ${n}`,
  memory: { decision: [], fact: [], procedure: [], episode: [] },
  critic: null, claim_check: null, reply: `Reply ${n}`, error: null, tool_call_count: 0,
  frame_reason: 'pattern', critic_frame: null, had_error: false, input_tokens: 18200, model_call_count: 1,
  model_calls: [], calls: [], ...over,
});
const session = (over: Record<string, unknown> = {}) => ({
  session_id: 'sess/one', parent_session_id: null, kind: 'conversation', work_kind: 'interactive',
  title: 'Gap Scout status check', started_at: '2026-11-07T00:07:00+00:00', ended_at: '2026-11-07T00:31:00+00:00',
  turns: 2, tool_calls: 3, errors: 1, gated: 0, ...over,
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
    expect(link.getAttribute('href')).toBe('#/model-calls?session_id=sess%2Fone&days=30');
  });

  it('caps the reasons per tool at 3 and folds the rest into "and N more"', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { calls: [1, 2, 3, 4].map((i) => call(i, { tool: 'db_query', lane: 'live', reason: `Why ${i}` })) })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Why 1', { selector: '.step *' });
    const used = container.querySelector('section.used') as HTMLElement;
    const det = used.querySelector('details')!;
    expect(det.open).toBe(false);
    expect(within(det).getByText('and 1 more')).toBeTruthy();
    expect(det.textContent).toContain('Turn 1: Why 4');
    const direct = Array.from(used.querySelectorAll('li.reason')).map((l) => l.textContent);
    expect(direct).toEqual(['Turn 1: Why 1', 'Turn 1: Why 2', 'Turn 1: Why 3']);
  });

  it('the Back link keeps the list filters', async () => {
    setLastListQuery({ kind: 'task', lens: 'error' });
    respond = () => ({ body: trace([turn(T1, 1)]) });
    render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    expect(screen.getByRole('link', { name: /Back to Work/ }).getAttribute('href')).toBe('#/work?kind=task&lens=error');
    setLastListQuery({});
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

  it('critic pill with a collapsed Why under it', async () => {
    respond = () => ({ body: trace([
      turn(T1, 1, { frame_id: 'question', frame_reason: 'critic', critic_frame: 'question',
        critic: { critic_frame: 'question', heuristic_frame: 'task', agreed: false, rationale: 'It asks, not orders.' } }),
      turn(T2, 2, { frame_id: 'task', critic_frame: 'task', critic: { critic_frame: 'task', heuristic_frame: 'task', agreed: true } }),
    ]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    const arts = Array.from(container.querySelectorAll('article.turn')) as HTMLElement[];
    expect(arts[0].querySelector('.crit')!.textContent).toBe('Critic overrode it');
    const det = arts[0].querySelector('.gutter details') as HTMLDetailsElement;
    expect(det.open).toBe(false);
    expect(det.querySelector('summary')!.textContent).toBe('Why');
    expect(det.textContent).toContain('It asks, not orders.');
    expect(arts[1].querySelector('.crit')!.textContent).toBe('Critic agreed');
    expect(arts[1].querySelector('details')).toBeNull();
    expect(arts[1].textContent).not.toContain('undefined');
  });

  it('critic pill: agreed, disagreed and kept the frame, overrode it; none without a model recommendation or a reason', async () => {
    respond = () => ({ body: trace([
      turn('a1', 1, { frame_id: 'question', critic_frame: 'question' }),
      turn('a2', 2, { frame_id: 'question', critic_frame: 'task' }),
      turn('a3', 3, { frame_id: 'decision', frame_reason: 'critic', critic_frame: 'decision' }),
      turn('a4', 4, { frame_id: 'question', critic_frame: null }),
      turn('a5', 5, { frame_id: 'question', critic_frame: 'unknown' }),
      turn('a6', 6, { frame_id: 'question', frame_reason: null, critic_frame: 'task' }),
    ]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 6');
    const pills = Array.from(container.querySelectorAll('article.turn')).map((a) => a.querySelector('.crit'));
    expect(pills.map((p) => p?.textContent ?? null)).toEqual([
      'Critic agreed', 'Critic disagreed, kept question', 'Critic overrode it', null, null, null,
    ]);
    expect(pills[2]!.classList.contains('no')).toBe(true);
    expect(pills[0]!.classList.contains('no')).toBe(false);
    expect(pills[1]!.classList.contains('no')).toBe(false);
    expect(container.textContent).not.toContain('passed it');
  });

  it('a critic rationale with no pill still shows, folded', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { critic_frame: null, critic: { heuristic_frame: 'question', rationale: 'Kept the heuristic.' } })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    expect(container.querySelector('.crit')).toBeNull();
    const det = container.querySelector('.gutter details') as HTMLDetailsElement;
    expect(det.open).toBe(false);
    expect(det.textContent).toContain('Kept the heuristic.');
  });

  it('the frame column shows time, frame, chosen by, tokens in, seconds to reply and tool calls', async () => {
    respond = () => ({ body: trace([turn(T1, 1, {
      started_at: '2026-11-10T09:12:04+00:00', ended_at: '2026-11-10T09:12:11.800+00:00', tool_call_count: 3, frame_id: 'question',
    })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    const g = container.querySelector('article.turn .gutter') as HTMLElement;
    for (const s of ['09:12:04', 'question', 'chosen by pattern', '18.2K tokens in', '7.8 s to reply', '3 tool calls']) {
      expect(g.textContent, s).toContain(s);
    }
    expect(g.querySelector('.frame')!.textContent).toBe('question');
    expect((container.querySelector('article.turn') as HTMLElement).style.getPropertyValue('--fc')).toBe('var(--f-question)');
  });

  it('one tool call is singular in the frame column', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { tool_call_count: 1 })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    expect(container.querySelector('.gutter')!.textContent).toContain('1 tool call');
    expect(container.querySelector('.gutter')!.textContent).not.toContain('1 tool calls');
  });

  it('a frame change between two turns is said in one sentence with its reason', async () => {
    respond = () => ({ body: trace([
      turn('c1', 1, { frame_id: 'question' }),
      turn('c2', 2, { frame_id: 'task', frame_reason: 'referent' }),
      turn('c3', 3, { frame_id: 'decision', frame_reason: 'critic', critic_frame: 'decision', critic: { heuristic_frame: 'task' } }),
      turn('c4', 4, { frame_id: 'debug', frame_reason: 'pattern' }),
    ]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 4');
    const sentences = Array.from(container.querySelectorAll('.change')).map((c) => c.textContent?.replace(/\s+/g, ' ').trim());
    expect(sentences).toEqual([
      'Frame changed from question to task. Your short reply answered my offer.',
      'Frame changed from task to decision. The critic chose decision over task.',
      'Frame changed from decision to debug. A new topic.',
    ]);
    // Each sentence sits between the two turns it joins.
    const c = container.querySelector('.change') as HTMLElement;
    expect(c.previousElementSibling?.id).toBe('turn-c1');
    expect(c.nextElementSibling?.id).toBe('turn-c2');
    expect(c.style.getPropertyValue('--from')).toBe('var(--f-question)');
    expect(c.style.getPropertyValue('--to')).toBe('var(--f-task)');
  });

  it('no sentence for a carried frame, an unchanged frame, or the first turn', async () => {
    respond = () => ({ body: trace([
      turn('d1', 1, { frame_id: 'question' }),
      turn('d2', 2, { frame_id: 'question', frame_reason: 'pattern' }),
      turn('d3', 3, { frame_id: 'task', frame_reason: 'carried' }),
    ]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 3');
    expect(container.querySelectorAll('.change')).toHaveLength(0);
  });

  it('Review Focus 4: no reason, no frame, no end, no tokens', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { frame_reason: null, frame_id: null, ended_at: null, input_tokens: null, critic_frame: 'task' })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    const g = container.querySelector('.gutter') as HTMLElement;
    expect(g.textContent).toContain('No frame recorded');
    expect(g.textContent).toContain('reason not recorded');
    expect(g.textContent).toContain('— tokens in');
    expect(g.textContent).toContain('— to reply');
    expect(container.querySelector('.crit')).toBeNull();
    expect((container.querySelector('article.turn') as HTMLElement).style.getPropertyValue('--fc')).toBe('var(--f-conversation)');
    expect(container.textContent).not.toContain('undefined');
    expect(container.textContent).not.toContain('null');
  });

  it('an older API without the new keys reads as not recorded', async () => {
    const old = turn(T1, 1);
    for (const k of ['frame_reason', 'critic_frame', 'had_error', 'input_tokens', 'model_call_count']) delete (old as Record<string, unknown>)[k];
    const s = session();
    delete (s as Record<string, unknown>).gated;
    respond = () => ({ body: trace([old, turn(T2, 2, { frame_id: 'task' })], { session: s }) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    const g = container.querySelector('.gutter') as HTMLElement;
    expect(g.textContent).toContain('reason not recorded');
    expect(g.textContent).toContain('— tokens in');
    expect(container.querySelector('.crit')).toBeNull();
    expect(container.querySelector('header')!.textContent).not.toContain('gated');
    expect(container.textContent).not.toContain('undefined');
    expect(container.textContent).not.toContain('NaN');
  });

  it('Review Focus 3: hostile ids and texts render as text; the layout contract is in the source', async () => {
    const reason = 'r'.repeat(1000) + HOSTILE;
    const frame = 'f'.repeat(30);
    const result = JSON.stringify({ rows: 'x'.repeat(5000) });
    respond = () => ({ body: trace([turn(T1, 1, { frame_id: frame, frame_reason: HOSTILE,
      calls: [call(0, { reason, result })] })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText(reason);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('.gutter .frame')!.textContent).toBe(frame);
    expect(container.textContent).toContain(`chosen by ${HOSTILE}`);
    expect(container.textContent).toContain(result);
    const src = readFileSync(resolve(__dirname, 'Trace.svelte'), 'utf-8');
    expect(src).not.toContain('{@html');
    expect(src).toMatch(/\.rows \{[^}]*min-width: 0/);
    expect(src.match(/overflow-wrap: anywhere/g)!.length).toBeGreaterThanOrEqual(4);
    expect(src).toMatch(/\.gutter[^{]*\{[^}]*overflow-wrap: anywhere/);
    expect(src).toMatch(/@media \(max-width: 640px\) \{[^@]*\.turn \{ grid-template-columns: minmax\(0, 1fr\)/);
    expect(src).toMatch(/@media \(max-width: 640px\) \{[^@]*\.gutter \{[^}]*flex-wrap: wrap/);
    expect(src).toMatch(/color-mix\(in srgb, var\(--fc\) 7%, transparent\)/);
  });

  it('header pills: gated only when there is one, the error count keeps its tint', async () => {
    respond = () => ({ body: trace([turn(T1, 1)], { session: session({ gated: 1 }) }) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    const head = container.querySelector('header')!;
    const gated = within(head as HTMLElement).getByText('1 gated');
    expect(gated.classList.contains('warn')).toBe(true);
    expect(within(head as HTMLElement).getByText('1 error').classList.contains('bad')).toBe(true);
    cleanup();
    respond = () => ({ body: trace([turn(T1, 1)]) });
    const r2 = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 1');
    expect(r2.container.querySelector('header')!.textContent).not.toContain('gated');
  });

  it('replay: one step per loaded turn; moving it marks that turn, a gutter click moves it', async () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    respond = () => ({ body: trace([
      turn('e1', 1, { started_at: '2026-11-10T09:12:04+00:00' }),
      turn('e2', 2, { started_at: '2026-11-10T09:14:36+00:00' }),
      turn('e3', 3, { started_at: '2026-11-10T09:21:10+00:00' }),
    ]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 3');
    const range = screen.getByRole('slider', { name: 'Replay turn' }) as HTMLInputElement;
    expect(range.min).toBe('0');
    expect(range.max).toBe('2');
    expect(range.step).toBe('1');
    expect(scroll).not.toHaveBeenCalled();
    await fireEvent.input(range, { target: { value: '1' } });
    const arts = Array.from(container.querySelectorAll('article.turn'));
    expect(arts.map((a) => a.classList.contains('hl'))).toEqual([false, true, false]);
    expect(container.querySelector('output')!.textContent).toContain('09:14:36');
    expect(scroll).toHaveBeenCalledTimes(1);
    expect(scroll.mock.instances[0]).toBe(arts[1]);
    await fireEvent.click(arts[2].querySelector('.gutter')!);
    expect(range.value).toBe('2');
    expect(arts.map((a) => a.classList.contains('hl'))).toEqual([false, false, true]);
    expect(container.querySelector('output')!.textContent).toContain('09:21:10');
  });

  it('the frame key lists only the frames of the loaded turns', async () => {
    respond = () => ({ body: trace([
      turn('k1', 1, { frame_id: 'task' }), turn('k2', 2, { frame_id: 'question' }), turn('k3', 3, { frame_id: 'task' }), turn('k4', 4, { frame_id: null }),
    ]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reply 4');
    const key = container.querySelector('.key')!;
    expect(Array.from(key.querySelectorAll('li')).map((l) => l.textContent?.trim())).toEqual(['task', 'question']);
  });

  it('unknown ledger status shows the undo statements; success does not; a ref shows the status pill', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { calls: [
      call(0, { tool: 'send_email', lane: 'acts', ledger: { id: LEDGER, status: 'unknown', side_effect_type: 'external', external_ref: null, tombstone: false, held: true } }),
      call(1, { tool: 'send_email', lane: 'acts', ledger: { id: 'other-id', status: 'success', side_effect_type: 'external', external_ref: null, tombstone: false, held: false } }),
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

  it('an unknown ledger row that holds nothing (held false) shows no undo statements', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { calls: [
      call(0, { ledger: { id: LEDGER, status: 'unknown', side_effect_type: 'external', external_ref: null, tombstone: false, held: false } }),
    ] })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reason 0', { selector: '.step *' });
    expect(container.querySelector('code.sql')).toBeNull();
    expect(container.querySelector('.pill')!.textContent).toContain('unknown');
  });

  it('Refresh reloads the loaded turns, so an older turn settled elsewhere updates', async () => {
    let status = 'unknown';
    const led = () => ({ id: LEDGER, status, side_effect_type: 'external', external_ref: null, tombstone: false, held: status === 'unknown' });
    const t = (id: string, n: number, over: Record<string, unknown> = {}) => turn(id, n, { started_at: `2026-11-07T00:0${n}:00+00:00`, ...over });
    respond = () => ({ body: trace([t(T1, 1, { calls: [call(0, { ledger: led() })] }), t(T2, 2), t(T3, 3)]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reason 0', { selector: '.step *' });
    expect(container.querySelector('code.sql')).not.toBeNull();
    status = 'success';
    const before = urls.length;
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(container.querySelector('code.sql')).toBeNull());
    expect(container.querySelector('.pill')!.textContent).toContain('success');
    expect(urls[before]).not.toContain('after=');
    expect(container.querySelectorAll('article.turn')).toHaveLength(3);
  });

  it('the lane is a plain word in the step; a failed call tints its row', async () => {
    respond = () => ({ body: trace([turn(T1, 1, { calls: [
      call(0, { lane: 'memory' }), call(1, { lane: 'live', is_error: true, outcome: 'error', error: 'boom' }), call(2, { lane: 'nonsense' }),
    ] })]) });
    const { container } = render(Trace, { sessionId: 'sess/one' });
    await screen.findByText('Reason 0', { selector: '.step *' });
    const steps = Array.from(container.querySelectorAll('.step')) as HTMLElement[];
    expect(steps.map((s) => s.querySelector('.lane')!.textContent)).toEqual(['Memory', 'Live state', 'Other']);
    expect(steps.map((s) => s.querySelector('.lane')!.getAttribute('style'))).toEqual([null, null, null]);
    expect(steps.map((s) => s.classList.contains('err'))).toEqual([false, true, false]);
    expect(container.querySelector('.rail')).toBeNull();
    // The "What it used" dot carries the lane's token.
    const dot = container.querySelector('section.used .dot') as HTMLElement;
    expect(dot.style.getPropertyValue('--dot')).toBe('var(--lane-memory)');
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
    expect(n.getAttribute('href')).toBe(`#/model-calls?turn_id=${T1}&days=30`);
    expect(arts[0].textContent).not.toMatch(/\d model calls/);
    const all = within(arts[0]).getByRole('link', { name: 'all model calls of this conversation' });
    expect(all.getAttribute('href')).toBe('#/model-calls?session_id=sess%2Fone&days=30');
    expect(Array.from(arts[0].querySelectorAll('a')).map((a) => a.getAttribute('href'))).toEqual(
      expect.arrayContaining(['#/model-calls/c/mc-1?days=30', '#/model-calls/c/mc-2?days=30']));
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

  describe('pages, polling and scrolling', () => {
    const at = (n: number) => `2026-11-07T00:0${n}:00.123456+00:00`;
    const cur = (t: { started_at: string; turn_id: string }) => `${t.started_at}|${t.turn_id}`;
    const tn = (id: string, n: number, over: Record<string, unknown> = {}) => turn(id, n, { started_at: at(n), ...over });
    const order = (c: HTMLElement) => Array.from(c.querySelectorAll('article.turn')).map((a) => a.id);

    it('three pages append, every turn stays, in order', async () => {
      const t1 = tn(T1, 1); const t2 = tn(T2, 2);
      respond = (url) => url.includes(encodeURIComponent(cur(t2)))
        ? { body: trace([tn(T3, 3)], { next_after: null }) }
        : url.includes('after=') ? { body: trace([t2], { next_after: cur(t2) }) }
        : { body: trace([t1], { next_after: cur(t1) }) };
      const { container } = render(Trace, { sessionId: 'sess/one' });
      await screen.findByText('Reply 1');
      await fireEvent.click(screen.getByRole('button', { name: 'Load more turns' }));
      await screen.findByText('Reply 2');
      await fireEvent.click(screen.getByRole('button', { name: 'Load more turns' }));
      await screen.findByText('Reply 3');
      expect(order(container)).toEqual([`turn-${T1}`, `turn-${T2}`, `turn-${T3}`]);
    });

    it('says how many of the session turns are shown beside Load more', async () => {
      respond = () => ({ body: trace([tn(T1, 1)], { next_after: cur(tn(T1, 1)), session: session({ turns: 3000 }) }) });
      render(Trace, { sessionId: 'sess/one' });
      await screen.findByText('Reply 1');
      expect(screen.getByText('Showing 1 of 3,000 turns')).toBeTruthy();
    });

    it('does not poll while more pages remain', async () => {
      respond = () => ({ body: trace([tn(T1, 1)], { next_after: cur(tn(T1, 1)) }) });
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      render(Trace, { sessionId: 'sess/one' });
      await vi.advanceTimersByTimeAsync(0);
      await screen.findByText('Reply 1');
      await vi.advanceTimersByTimeAsync(45000);
      expect(urls).toHaveLength(1);
    });

    it('once every turn is loaded, a poll asks after the second-to-last turn, replaces it and appends new ones', async () => {
      const t1 = tn(T1, 1); const t2 = tn(T2, 2);
      let phase = 0;
      respond = (url) => {
        if (!url.includes('after=')) return { body: trace([t1, t2]) };
        phase += 1;
        return { body: trace([tn(T2, 2, { reply: 'Reply 2 grew' }), tn(T3, 3)]) };
      };
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      const { container } = render(Trace, { sessionId: 'sess/one' });
      await vi.advanceTimersByTimeAsync(0);
      await screen.findByText('Reply 2');
      await vi.advanceTimersByTimeAsync(15000);
      await screen.findByText('Reply 2 grew');
      expect(phase).toBe(1);
      const u = new URL(urls[urls.length - 1], 'http://x');
      expect(u.searchParams.get('after')).toBe(cur(t1));
      expect(order(container)).toEqual([`turn-${T1}`, `turn-${T2}`, `turn-${T3}`]);
      // the next poll moves its cursor to the new second-to-last turn
      await vi.advanceTimersByTimeAsync(15000);
      expect(new URL(urls[urls.length - 1], 'http://x').searchParams.get('after')).toBe(cur(tn(T2, 2)));
    });

    it('with a single turn a poll refetches page one and merges by id', async () => {
      let reply = 'Reply 1';
      respond = () => ({ body: trace([tn(T1, 1, { reply })]) });
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      const { container } = render(Trace, { sessionId: 'sess/one' });
      await vi.advanceTimersByTimeAsync(0);
      await screen.findByText('Reply 1');
      reply = 'Reply 1 later';
      await vi.advanceTimersByTimeAsync(15000);
      await screen.findByText('Reply 1 later');
      expect(urls[1]).not.toContain('after=');
      expect(container.querySelectorAll('article.turn')).toHaveLength(1);
    });

    it('no full-result button when result_full is false; a button when true', async () => {
      respond = () => ({ body: trace([tn(T1, 1, { calls: [call(0), call(1, { result_full: true })] })]) });
      render(Trace, { sessionId: 'sess/one' });
      await screen.findByText('Reply 1');
      expect(screen.getAllByRole('button', { name: 'Show full result' })).toHaveLength(1);
    });

    it('an open full result stays open across a poll that swaps the call objects, with no refetch', async () => {
      const mk = () => trace([tn(T1, 1, { calls: [call(0, { result_full: true })] })]);
      respond = (url) => (url.includes('/result') ? { body: { call_id: 'call-0', result: 'FULL BODY' } } : { body: mk() });
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      render(Trace, { sessionId: 'sess/one' });
      await vi.advanceTimersByTimeAsync(0);
      await fireEvent.click(await screen.findByRole('button', { name: 'Show full result' }));
      await screen.findByText('FULL BODY');
      const before = urls.filter((u) => u.includes('/result')).length;
      const polls = urls.length;
      await vi.advanceTimersByTimeAsync(15000);
      expect(urls.length).toBeGreaterThan(polls);
      expect(screen.getByText('FULL BODY')).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Hide full result' }).getAttribute('aria-expanded')).toBe('true');
      expect(urls.filter((u) => u.includes('/result')).length).toBe(before);
    });

    it('a 404 on a poll stops polling', async () => {
      let n = 0;
      respond = () => (++n === 1 ? { body: trace([tn(T1, 1)]) } : { status: 404, body: { error: 'not found' } });
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
      render(Trace, { sessionId: 'sess/one' });
      await vi.advanceTimersByTimeAsync(0);
      await screen.findByText('Reply 1');
      await vi.advanceTimersByTimeAsync(15000);
      const after404 = urls.length;
      await vi.advanceTimersByTimeAsync(60000);
      expect(urls).toHaveLength(after404);
    });

    it('turnId scrolls once; a later poll does not scroll again', async () => {
      const scroll = vi.fn();
      Element.prototype.scrollIntoView = scroll;
      let reply = 'Reply 2';
      respond = () => ({ body: trace([tn(T1, 1), tn(T2, 2, { reply })]) });
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

    it('a turnId on page 3 loads pages until it is found, then scrolls to it', async () => {
      const scroll = vi.fn();
      Element.prototype.scrollIntoView = scroll;
      const t1 = tn(T1, 1); const t2 = tn(T2, 2);
      respond = (url) => url.includes(encodeURIComponent(cur(t2)))
        ? { body: trace([tn(T3, 3)]) }
        : url.includes('after=') ? { body: trace([t2], { next_after: cur(t2) }) }
        : { body: trace([t1], { next_after: cur(t1) }) };
      render(Trace, { sessionId: 'sess/one', turnId: T3 });
      await waitFor(() => expect(scroll).toHaveBeenCalledTimes(1));
      expect(urls).toHaveLength(3);
      expect(screen.queryByText(/That turn is further on/)).toBeNull();
    });

    it('stops after 10 pages and tells the owner how to reach the turn', async () => {
      let n = 0;
      respond = () => {
        n += 1;
        const t = tn(`aaaaaaaa-0000-4000-8000-${String(n).padStart(12, '0')}`, 1, { started_at: `2026-11-07T00:00:${String(n).padStart(2, '0')}+00:00` });
        return { body: trace([t], { next_after: cur(t) }) };
      };
      render(Trace, { sessionId: 'sess/one', turnId: T3 });
      expect(await screen.findByText('That turn is further on. Use Load more to reach it.')).toBeTruthy();
      expect(urls).toHaveLength(11);   // page one plus at most 10 automatic pages
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
