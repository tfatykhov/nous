import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import OutsideJudge from './OutsideJudge.svelte';
import type { Disagreement, JudgeData, SweepRuntime } from '../../lib/outside_judge_numbers';

// F118: the panel "The outside judge". The fetch stand-in answers each read with the next of `replies`, else
// `fallback`. A reply is a status and a body, a network error, a read that waits until its signal aborts, or a read
// the test resolves itself (it ignores the abort, so the panel's own rule about older reads is what is tested). Every
// value is made up; dates are fixed UTC strings.

type Reply =
  | { status: number; body: unknown }
  | { kind: 'network' }
  | { kind: 'hang' }
  | { kind: 'later'; promise: Promise<Response> };

const answer = (status: number, body: unknown) =>
  ({ ok: status < 400, status, text: async () => JSON.stringify(body) }) as Response;

let replies: Reply[] = [];
let fallback: Reply;
let urls: string[] = [];

function install() {
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit = {}) => {
    urls.push(url);
    const reply = replies.shift() ?? fallback;
    if (!('kind' in reply)) return Promise.resolve(answer(reply.status, reply.body));
    if (reply.kind === 'network') return Promise.reject(new TypeError('network'));
    if (reply.kind === 'later') return reply.promise;
    return new Promise<Response>((_, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    });
  }));
}
/** A read the test answers when it wants. */
function later(): { reply: Reply; resolve: (body: unknown) => void } {
  let resolve!: (r: Response) => void;
  const promise = new Promise<Response>((r) => { resolve = r; });
  return { reply: { kind: 'later', promise }, resolve: (body) => resolve(answer(200, body)) };
}

const ID = (n: number) => `${String(n).padStart(8, '0')}-0000-4000-8000-000000000000`;
const disagreement = (over: Partial<Disagreement> = {}): Disagreement => ({
  subject_id: ID(1), title: null, against_grader: 'a2ui', against_grade: 'success', verdict: 'partial', confidence: 0.55,
  labels: ['evidence_partial'], reason: 'Only one of two files was written.', judged_at: '2026-11-20T10:00:00+00:00', ...over,
});
const group = (over: Record<string, unknown> = {}) =>
  ({ n: 11, agree: 1, judge_harsher: 1, judge_kinder: 0, cannot_tell: 9, agree_rate: 0.5, ...over });
const RUNTIME: SweepRuntime = {
  passes: 3, queued_decisions: { a2ui: 2 }, queued_reports: 1, held_at_half_limit: 0, failed: 0, last_pass_at: '2026-11-20T16:56:00+00:00',
};
/** The thin state: few decisions graded, most verdicts "could not tell", reports and the sweep on. */
const THIN = (over: Partial<JudgeData> = {}): JudgeData => ({
  enabled: true, mode: 'shadow', judge: 'background_model', version: 'dj-2', model: 'claude-test-model', days: 7,
  decisions: {
    judged: 11, by_verdict: { cannot_tell: 9, success: 1, partial: 1 }, skipped_by_label: { evidence_missing: 3 }, errors: 1,
    by_grader: { a2ui: group() },
  },
  cost: { calls: 14, tokens_in: 80_000, tokens_out: 4_900, tokens_per_verdict_avg: 7_107.4, latency_ms_p50: 3_631.5, today: { calls: 16, cap: 100 } },
  disagreements: [disagreement()],
  runtime: { queued: 0, dropped: 0, over_cap: 0, worker_alive: true },
  reports: {
    judge: 'background_model', version: 'rj-1', judged: 4, by_verdict: { supported: 4 }, skipped_by_label: { evidence_missing: 2 }, errors: 0,
    vs_finish_check: { n: 4, compared: 4, agree: 4, agree_rate: 1, matrix: {} },
    vs_audit: { n: 0, compared: 0, agree: 0, agree_rate: null, matrix: {} },
    cost: { calls: 4, tokens_in: 10_734, tokens_out: 826, tokens_per_verdict_avg: 2_890, latency_ms_p50: 1_889.5 },
    not_supported: [],
  },
  sweep: { agreement: { outcome_grades: group(), stated_confidence: group({ n: 0, agree: 0, judge_harsher: 0, cannot_tell: 0, agree_rate: null }) }, runtime: RUNTIME },
  ...over,
});
const BARE = (over: Partial<JudgeData> = {}): JudgeData => ({ ...THIN(), reports: undefined, sweep: undefined, ...over });

const props = (over: { days?: string; refresh?: number } = {}) => ({ days: '7', refresh: 0, ...over });
const heading = () => screen.queryByRole('heading', { name: 'The outside judge' });
/** The panel once drawn. */
const found = async () => (await screen.findByRole('heading', { name: 'The outside judge' })).closest('section')!;
/** Draw the panel for the last 7 days and find it. */
const panel = async () => { render(OutsideJudge, props()); return found(); };
const settle = () => new Promise((r) => setTimeout(r, 20));
const LOAD_FAILED = "The outside judge's numbers could not be read. Use Refresh at the top of the page to try again.";
const INTRO = 'The outside judge only records its own verdicts beside the grades Nous already has, and changes no grade.';
/** A tile's value and its sub line, by its label. */
const tile = (section: HTMLElement, label: string) => {
  const dt = Array.from(section.querySelectorAll('.tiles dt')).find((el) => el.textContent === label)!;
  return [dt.nextElementSibling!.textContent, dt.nextElementSibling!.nextElementSibling!.textContent];
};

beforeEach(() => { replies = []; fallback = { status: 200, body: THIN() }; urls = []; install(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('off', () => {
  it.each([
    ['enabled: false', { enabled: false }],
    ['an answer without enabled', {}],
  ])('%s draws nothing and asks once, whatever the window or Refresh do afterwards', async (_name, body) => {
    fallback = { status: 200, body };
    const { container, rerender } = render(OutsideJudge, props());
    await waitFor(() => expect(urls).toEqual(['/dashboard/outside-judge?days=7']));
    await settle();
    expect(container.textContent?.trim()).toBe('');
    await rerender(props({ days: '30' }));
    await rerender(props({ days: '30', refresh: 1 }));
    await settle();
    expect(urls).toHaveLength(1);
    expect(container.textContent?.trim()).toBe('');
  });
});

describe('the thin state', () => {
  it('draws the title, the strip, the tiles, the bar, the grader row, the disagreement and the reports', async () => {
    const section = await panel();
    const text = section.textContent ?? '';
    expect(text).toContain(INTRO);
    const strip = section.querySelector('[data-testid="oj-strip"]')!;
    expect(strip.classList.contains('verdict-ok')).toBe(true);
    expect(strip.textContent).toContain('Shadow mode: it records verdicts and changes nothing');
    expect(strip.textContent).toContain('These numbers cover only judge background_model, instructions dj-2, over the last 7 days.');
    expect(Array.from(strip.querySelectorAll('dt')).map((dt) => `${dt.textContent}: ${dt.nextElementSibling?.textContent}`)).toEqual([
      'Judge: background_model', 'Instructions: dj-2', 'Model: claude-test-model', 'Worker: Running', 'Waiting: 0', 'Dropped: 0',
      'Over the daily limit: 0',
    ]);
    expect(tile(section, 'Decisions judged')).toEqual(['11', 'in the last 7 days']);
    expect(tile(section, 'Agreement with outcomes')).toEqual(['50%', "1 of 2 graded verdicts match your taps and the reviewer's outcome signals"]);
    expect(tile(section, 'Could not tell')).toEqual(['82%', '9 of 11 verdicts']);
    expect(tile(section, 'Calls today')).toEqual(['16 of 100', 'today, every instructions version, decisions and reports']);
    expect(screen.getByRole('img', { name: 'Success 1, Partial 1, Could not tell 9' })).toBeTruthy();
    expect(Array.from(section.querySelectorAll('.legend li')).slice(0, 3).map((li) => li.textContent))
      .toEqual(['Success 1 (9%)', 'Partial 1 (9%)', 'Could not tell 9 (82%)']);
    const row = screen.getByText('Your tap on a card', { selector: 'th' }).closest('tr')!;
    expect(Array.from(row.querySelectorAll('td')).map((td) => td.textContent)).toEqual(['11', '1', '1', '0', '9', '50%']);
    const [item] = Array.from(section.querySelectorAll('[data-testid="oj-disagreements"] li'));
    expect(item.textContent).toContain('Decision 00000001');
    expect(item.textContent).toContain('Graded success by your tap on a card. The judge says partial, harsher than that grade.');
    expect(item.textContent).toContain('Only one of two files was written.');
    expect(item.textContent).toContain('Nov 20 10:00 UTC');
    expect(item.textContent).toContain("Judge's confidence 55%");
    expect(item.querySelector('.status-pill')?.textContent).toBe('Evidence covers part of it');
    expect(text).toContain('1 in the last 7 days, newest first');
    expect(text).toContain('Judge background_model, instructions rj-1');
    expect(text).toContain('Agreement with the finish checks: 100% (4 of 4 compared)');
    expect(text).toContain('Agreement with your spot checks: Not enough compared yet');
    expect(text).toContain('The judge found every judged report supported or could not tell.');
    expect(text).toContain('Decisions: 3 skipped (no record of what happened was found: 3); 1 judgement failed or ran out of time');
    expect(text).toContain('Average tokens per verdict');
    expect(text).toContain('3.6 s');
    expect(text).not.toMatch(/NaN|undefined|null/);
    expect(section.getAttribute('aria-busy')).toBeNull();
  });

  it('a disagreement with a title shows the title, not the id', async () => {
    fallback = { status: 200, body: BARE({ disagreements: [disagreement({ title: 'Retry the upload' })] }) };
    const section = await panel();
    const [item] = Array.from(section.querySelectorAll('[data-testid="oj-disagreements"] li'));
    expect(item.querySelector('.item-title')?.textContent).toBe('Retry the upload');
    expect(item.textContent).not.toContain('Decision 00000001');
  });

  it('a worker that is not running turns the strip to a warning, in words too', async () => {
    fallback = { status: 200, body: BARE({ runtime: { queued: 2, dropped: 0, over_cap: 0, worker_alive: false } }) };
    const strip = (await panel()).querySelector('[data-testid="oj-strip"]')!;
    expect(strip.classList.contains('verdict-warn')).toBe(true);
    expect(strip.textContent).toContain('Not running');
  });

  it('a null agreement rate reads "Not enough graded yet", never 0%', async () => {
    const none = group({ agree: 0, judge_harsher: 0, cannot_tell: 11, agree_rate: null });
    fallback = {
      status: 200,
      body: THIN({
        decisions: { ...THIN().decisions, by_grader: { a2ui: none } },
        sweep: { agreement: { outcome_grades: none, stated_confidence: none }, runtime: RUNTIME },
      }),
    };
    const section = await panel();
    expect(tile(section, 'Agreement with outcomes')).toEqual(['Not enough graded yet', '0 graded so far']);
    const row = screen.getByText('Your tap on a card', { selector: 'th' }).closest('tr')!;
    expect(row.lastElementChild?.textContent).toBe('Not enough graded yet');
    expect(section.textContent).not.toMatch(/(?<!\d)0%/); // a share of 0%, not the reports' 100%
  });
});

describe('reads', () => {
  it('a new window asks with it, and Refresh asks again', async () => {
    const { rerender } = render(OutsideJudge, props());
    await found();
    await rerender(props({ days: '30' }));
    await waitFor(() => expect(urls).toEqual(['/dashboard/outside-judge?days=7', '/dashboard/outside-judge?days=30']));
    await rerender(props({ days: '30', refresh: 1 }));
    await waitFor(() => expect(urls).toHaveLength(3));
    expect(urls[2]).toBe('/dashboard/outside-judge?days=30');
  });

  it('an older read that answers after a newer one never replaces it', async () => {
    const old = later();
    replies = [{ status: 200, body: BARE() }, old.reply, { status: 200, body: BARE({ days: 30, judge: 'newer_judge' }) }];
    const { rerender } = render(OutsideJudge, props());
    await found();
    await rerender(props({ refresh: 1 }));
    await rerender(props({ days: '30', refresh: 1 }));
    await waitFor(() => expect(screen.getByText(/judge newer_judge/)).toBeTruthy());
    old.resolve(BARE({ judge: 'older_judge' }));
    await settle();
    expect(screen.queryByText(/judge older_judge/)).toBeNull();
    expect(screen.getByText(/judge newer_judge/)).toBeTruthy();
  });

  it('while a later read runs the numbers stay and the panel is marked busy', async () => {
    const next = later();
    replies = [{ status: 200, body: BARE() }, next.reply];
    const { rerender } = render(OutsideJudge, props());
    const section = await found();
    expect(section.getAttribute('aria-busy')).toBeNull();
    await rerender(props({ refresh: 1 }));
    await waitFor(() => expect(section.getAttribute('aria-busy')).toBe('true'));
    expect(section.textContent).toContain('Graded success by your tap on a card.');
    next.resolve(BARE());
    await waitFor(() => expect(section.getAttribute('aria-busy')).toBeNull());
  });

  it('an older read that ends while a newer one runs leaves the panel busy', async () => {
    const old = later();
    const next = later();
    replies = [{ status: 200, body: BARE() }, old.reply, next.reply];
    const { rerender } = render(OutsideJudge, props());
    const section = await found();
    await rerender(props({ refresh: 1 }));
    await rerender(props({ refresh: 2 }));
    await waitFor(() => expect(urls).toHaveLength(3));
    old.resolve(BARE());
    await settle();
    expect(section.getAttribute('aria-busy')).toBe('true');
    next.resolve(BARE());
    await waitFor(() => expect(section.getAttribute('aria-busy')).toBeNull());
  });
});

describe('blocks', () => {
  it('a block the answer does not have is not drawn', async () => {
    fallback = { status: 200, body: BARE() };
    await panel();
    for (const name of ['Task reports', 'The hourly pass', 'The second judge']) {
      expect(screen.queryByRole('heading', { name }), name).toBeNull();
    }
    expect(screen.queryByText(/^Reports:/)).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Cost of judging decisions' })).toBeTruthy();
  });

  it('the task reports: the bar, both agreement lines, a report not found supported, and the cost', async () => {
    const reports = {
      ...THIN().reports!, judged: 3, by_verdict: { supported: 2, not_supported: 1 },
      vs_audit: { n: 1, compared: 1, agree: 1, agree_rate: 1, matrix: {} },
      vs_finish_check: { n: 0, compared: 0, agree: 0, agree_rate: null, matrix: {} },
      not_supported: [{
        subject_id: ID(7), title: 'Deploy the new build', confidence: 0.8, labels: ['claim_not_backed'], reason: 'The deploy failed.',
        finish_check: 'clean', audit: 'wrong', judged_at: '2026-11-21T09:30:00+00:00',
      }, {
        subject_id: ID(8), title: null, confidence: null, labels: [], reason: null, finish_check: null, audit: null,
        judged_at: '2026-11-21T08:00:00+00:00',
      }],
    };
    fallback = { status: 200, body: THIN({ reports }) };
    const section = await panel();
    expect(screen.getByRole('img', { name: 'Supported 2, Not supported 1' })).toBeTruthy();
    expect(section.textContent).toContain('Agreement with the finish checks: Not enough compared yet');
    expect(section.textContent).toContain('Agreement with your spot checks: 100% (1 of 1 compared)');
    expect(section.textContent).toContain('Cost: 4 calls, 10,734 tokens in, 826 tokens out.');
    expect(screen.getByRole('heading', { name: 'Latest reports the judge did not find supported' })).toBeTruthy();
    const [first, second] = Array.from(section.querySelectorAll('[data-testid="oj-not-supported"] li'));
    expect(first.querySelector('.item-title')?.textContent).toBe('Deploy the new build');
    expect(first.textContent).toContain('The deploy failed.');
    expect(first.textContent).toContain('Finish check: clean · Your spot check: wrong');
    expect(first.querySelector('.status-pill')?.textContent).toBe('Report claims more than the evidence shows');
    expect(second.querySelector('.item-title')?.textContent).toBe('Task 00000008');
    expect(second.textContent).toContain('The judge gave no sentence.');
    expect(second.textContent).toContain('No finish check · No spot check yet');
    expect(section.textContent).not.toContain('The judge found every judged report supported');
  });

  it('the hourly pass: what it did, with the sentence that says what it is', async () => {
    const section = await panel();
    expect(section.textContent).toContain('Once an hour the judge picks up decisions and reports that have no final verdict yet.');
    expect(section.textContent).toContain(
      'Since this process started: 3 passes, the last at Nov 20 16:56 UTC. It queued 2 decisions (your tap on a card 2) and 1 report.');
    expect(section.textContent).toContain("Agreement with Nous's stated confidence, which is not an outcome: Not enough graded yet");
  });

  it('the hourly pass with no pass yet, and with no numbers in this process (then without its sentence)', async () => {
    fallback = { status: 200, body: THIN({ sweep: { ...THIN().sweep!, runtime: { ...RUNTIME, passes: 0 } } }) };
    let section = await panel();
    expect(section.textContent).toContain('No hourly pass has run yet in this server process.');
    expect(section.textContent).toContain('Once an hour the judge picks up');
    cleanup();
    fallback = { status: 200, body: THIN({ sweep: { ...THIN().sweep!, runtime: null } }) };
    section = await panel();
    expect(section.textContent).toContain('The hourly pass has no numbers in this server process.');
    expect(section.textContent).not.toContain('Once an hour');
    expect(section.textContent).not.toContain('No hourly pass has run');
  });

  it('the second judge: its numbers, how the two compare, and one pair', async () => {
    fallback = {
      status: 200,
      body: BARE({
        second: {
          judge: 'jev', version: 'jq-2:jev-1.14.0', model: 'jev-1.14.0',
          decisions: { judged: 4, by_verdict: {}, skipped_by_label: {}, errors: 0, by_grader: {} },
          cost: { calls: 4, tokens_in: 1, tokens_out: 1, tokens_per_verdict_avg: null, latency_ms_p50: null, today: { calls: 1, cap: 50 } },
        },
        between_judges: {
          pairs: 4, agree: 2, either_cannot_tell: 1, agree_rate: 0.6667, matrix: {}, evidence_differs: 0, other_claim: 1,
          disagreements: [{
            subject_id: ID(3), title: 'Rotate the keys', claim_at: '2026-11-20T10:00:00+00:00', against_grader: 'agent',
            against_grade: 'success', first_verdict: 'success', second_verdict: 'failure', first_confidence: 0.8,
            second_confidence: 0.9, first_labels: [], second_labels: [], evidence_differs: false,
          }],
        },
      }),
    };
    const section = await panel();
    expect(screen.getByRole('heading', { name: 'The second judge' })).toBeTruthy();
    expect(section.textContent).toContain('Judge jev, instructions jq-2:jev-1.14.0, model jev-1.14.0. It judged 4 decisions; calls today 1 of 50.');
    expect(section.textContent).toContain(
      'On the same claim, both judges judged 4 decisions: they agreed on 2, one could not tell on 1, and they differed on 1.');
    expect(section.textContent).toContain('Agreement: 67%');
    expect(section.textContent).toContain('1 more were judged on different claims and are not compared.');
    const [pair] = Array.from(section.querySelectorAll('[data-testid="oj-pairs"] li'));
    expect(pair.textContent).toContain('Rotate the keys');
    expect(pair.textContent).toContain('The first judge says success, the second says failure.');
  });
});

describe('empty lists', () => {
  it('nothing judged: the tiles stay, the bar and the table give their sentences', async () => {
    fallback = {
      status: 200,
      body: THIN({
        days: 30,
        decisions: { judged: 0, by_verdict: {}, skipped_by_label: {}, errors: 0, by_grader: {} },
        disagreements: [],
        reports: { ...THIN().reports!, judged: 0, by_verdict: {}, skipped_by_label: {} },
      }),
    };
    const section = await panel();
    expect(tile(section, 'Decisions judged')).toEqual(['0', 'in the last 30 days']);
    expect(tile(section, 'Could not tell')).toEqual(['—', 'nothing judged yet']);
    expect(section.textContent).toContain('No decision has been judged in the last 30 days.');
    expect(section.textContent).toContain('No verdict has been compared with a grade yet.');
    expect(section.textContent).toContain('The judge has not disagreed with a grade in the last 30 days.');
    expect(section.textContent).toContain('No task report has been judged in the last 30 days.');
    expect(section.textContent).toContain('Decisions: Nothing was left unjudged.');
    expect(section.textContent).not.toContain('The judge found every judged report supported');
    expect(section.querySelector('table')).toBeNull();
    expect(section.querySelector('[role="img"]')).toBeNull();
    expect(section.textContent).not.toMatch(/NaN|undefined/);
  });
});

describe('failures', () => {
  it.each([
    ['a 500', (): Reply => ({ status: 500, body: { error: 'the judge\'s numbers could not be read' } })],
    ['a network error', (): Reply => ({ kind: 'network' })],
  ])('%s on the first read draws the title, the intro and the sentence only; Refresh asks again', async (_name, reply) => {
    replies = [reply()];
    const { rerender } = render(OutsideJudge, props());
    const section = await found();
    expect(screen.getByRole('alert').textContent).toBe(LOAD_FAILED);
    expect(Array.from(section.children).map((el) => el.textContent?.trim())).toEqual(['The outside judge', INTRO, LOAD_FAILED]);
    await rerender(props({ refresh: 1 }));
    await waitFor(() => expect(screen.getByText('Shadow mode: it records verdicts and changes nothing')).toBeTruthy());
    expect(urls).toHaveLength(2);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('a first read that runs out of time is a failure too, with the sentence', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    replies = [{ kind: 'hang' }];
    render(OutsideJudge, props());
    await waitFor(() => expect(urls).toHaveLength(1));
    expect(heading()).toBeNull();
    await vi.advanceTimersByTimeAsync(15_000);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe(LOAD_FAILED));
    expect(heading()).toBeTruthy();
  });

  it('a later read that fails keeps the numbers and adds the sentence; a later good read clears it', async () => {
    replies = [{ status: 200, body: BARE() }, { kind: 'network' }];
    const { rerender } = render(OutsideJudge, props());
    const section = await found();
    await rerender(props({ refresh: 1 }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe(LOAD_FAILED));
    expect(section.textContent).toContain('Graded success by your tap on a card.');
    await rerender(props({ refresh: 2 }));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });
});

describe('the list of disagreements', () => {
  const many = (n: number) => Array.from({ length: n }, (_, i) => disagreement({ subject_id: ID(i + 1), title: `Decision number ${i + 1}` }));
  const items = (section: HTMLElement) => section.querySelectorAll('[data-testid="oj-disagreements"] li').length;

  it('shows the first 10, then all, then 10 again', async () => {
    fallback = { status: 200, body: BARE({ disagreements: many(12) }) };
    const section = await panel();
    expect(items(section)).toBe(10);
    expect(section.textContent).toContain('12 in the last 7 days, newest first');
    await fireEvent.click(screen.getByRole('button', { name: 'Show all 12' }));
    expect(items(section)).toBe(12);
    await fireEvent.click(screen.getByRole('button', { name: 'Show fewer' }));
    expect(items(section)).toBe(10);
  });

  it('no button with 10 or fewer; "the latest 50" when the server cut the list', async () => {
    fallback = { status: 200, body: BARE({ disagreements: many(10) }) };
    let section = await panel();
    expect(items(section)).toBe(10);
    expect(section.querySelector('button')).toBeNull();
    cleanup();
    fallback = { status: 200, body: BARE({ disagreements: many(50) }) };
    section = await panel();
    expect(section.textContent).toContain('The latest 50, newest first');
    expect(screen.getByRole('button', { name: 'Show all 50' })).toBeTruthy();
  });
});

describe('text, not markup', () => {
  it('a title and a sentence from the server are drawn as text', async () => {
    fallback = { status: 200, body: BARE({ disagreements: [disagreement({ title: '<b>bold</b>', reason: '<b>bold</b>' })] }) };
    const section = await panel();
    const [item] = Array.from(section.querySelectorAll('[data-testid="oj-disagreements"] li'));
    expect(item.querySelector('.item-title')?.textContent).toBe('<b>bold</b>');
    expect(item.querySelector('.reason')?.textContent).toBe('<b>bold</b>');
    expect(section.querySelector('b')).toBeNull();
  });
});
