import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Consolidation from './Consolidation.svelte';
import { styleBlocks } from '../lib/testing/css';

// The sleep journal: a chart of the newest sleeps, every sleep under its day as one sentence, and a report that opens
// in place. Every value below is made up. The totals are in the shape the sleep handler writes (counters beside
// whole-graph snapshots, a nested object and `_attempted`), so a test fails if the page lists them again.
const FILES = ['Consolidation.svelte', 'consolidation/SleepChart.svelte', 'consolidation/SleepReport.svelte'];
const SRC = FILES.map((f) => readFileSync(join(__dirname, f), 'utf-8').replace(/\r\n/g, '\n'));

const TOTALS = {
  facts_created: 3, clusters_merged: 1, stale_deactivated: 7, stale_examined: 3988, contradictions_found: 2,
  orphan_edges_created: 539, cooccurrence_edges: 86, dead_edges_pruned: 200, rubric_evolved: true,
  f044_n_edges: 144750, _attempted: 381291, tool_notes: { written: 1 },
};
const cycle = (id: string, started: string, over: Record<string, unknown> = {}) => ({
  cycle_id: id, trace_id: `t-${id}`, started_at: started, finished_at: started.replace(':00:00', ':17:00'), status: 'completed',
  phases_run: ['review', 'reflect', 'stale_scan'], totals: TOTALS, action_count: 12, ...over,
});
const LIST = {
  cycles: [
    cycle('c-run', '2026-11-13T21:00:00+00:00', { status: 'running', finished_at: null, totals: {}, phases_run: [], action_count: 0 }),
    cycle('c-done', '2026-11-13T11:00:00+00:00'),
    cycle('c-quiet', '2026-11-12T15:00:00+00:00', { totals: { stale_examined: 3900 }, action_count: 0 }),
    cycle('c-fail', '2026-11-12T09:00:00+00:00', { status: 'failed', totals: { facts_created: 1 } }),
    cycle('c-lost', '2026-11-12T03:00:00+00:00', { status: 'running', finished_at: null, totals: {}, action_count: 0 }),
  ],
};
const LONG = 'w'.repeat(600);
const T = '2026-11-13T11:05:00+00:00';
const stale = (i: number) => ({
  action_id: `s-${i}`, phase: 'stale_scan', op: 'deactivate', target_ids: [`f-stale-${i}`], rationale: 'stale: aged > 60d, no recall in window',
  before: { subject: `old subject ${i}`, content_preview: `old text ${i}` }, after: null, created_at: T,
});
const DETAIL = {
  cycle: LIST.cycles[1],
  actions: [
    ...Array.from({ length: 7 }, (_, i) => stale(i)),
    { action_id: 'l-1', phase: 'reflect', op: 'learn', target_ids: ['f-new'], rationale: 'sleep_reflection fact',
      before: null, after: { subject: 'kettle', content_preview: LONG }, created_at: T },
    { action_id: 'm-1', phase: 'f027_consolidate', op: 'merge', target_ids: ['f-a', 'f-b', 'f-c'], rationale: "cluster merge of 2 facts on 'storage'",
      before: [{ id: 'f-a', content_preview: 'uses Postgres' }, { id: 'f-b', content_preview: 'uses pgvector' }],
      after: { id: 'f-c', content_preview: 'uses Postgres with pgvector' }, created_at: T },
    { action_id: 'g-1', phase: 'graph_densify', op: 'edge_add', target_ids: [], rationale: 'graph_densify phase summary',
      before: null, after: { counts: { orphan_edges_created: 539 } }, created_at: T },
    // Two rows in a shape no section knows: one with a missing side, one whose recorded side is the string "none".
    { action_id: 'o-1', phase: 'later_phase', op: 'reshape', target_ids: [], rationale: null, before: null, after: { anything: 1 }, created_at: T },
    { action_id: 'o-2', phase: 'later_phase', op: 'reshape', target_ids: [], rationale: null, before: 'none', after: { anything: 2 }, created_at: T },
  ],
};
const ACTIVITY = {
  events: [], censor_stats: {}, schedule_stats: {},
  sleep_stats: { last_sleep: '2026-11-13T21:00:00+00:00', total_sleeps: 657, facts_created: 9, procedures_created: 1, censors_retired: 2 },
};

let urls: string[] = [];
let list: unknown = LIST;
let failing: string[] = [];
function install() {
  urls = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(url);
    if (failing.some((p) => url.startsWith(p))) return { ok: false, status: 400, json: async () => ({}) } as Response;
    const body = url.startsWith('/dashboard/consolidation/') ? DETAIL : url.startsWith('/dashboard/consolidation') ? list
      : url.startsWith('/dashboard/activity') ? ACTIVITY : null;
    if (!body) return { ok: false, status: 404, json: async () => ({}) } as Response;
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
}
const detailUrls = () => urls.filter((u) => u.startsWith('/dashboard/consolidation/'));
const rowOf = (container: HTMLElement, clock: string) =>
  Array.from(container.querySelectorAll<HTMLButtonElement>('button.sleep')).find((b) => b.textContent?.includes(clock))!;
const loaded = async (container: HTMLElement) => {
  await waitFor(() => expect(container.querySelectorAll('button.sleep')).toHaveLength(5));
  await screen.findByRole('heading', { level: 3, name: /Merged/ });
};

afterEach(() => { cleanup(); vi.unstubAllGlobals(); list = LIST; failing = []; });

describe('the sleep journal', () => {
  it('lists every sleep under its day as one sentence, with its duration or its state in words', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual(['Last 5 sleeps', '13 November', '12 November']);
    const rows = Array.from(container.querySelectorAll('button.sleep')).map((b) => b.textContent?.replace(/\s+/g, ' ').trim());
    expect(rows[0]).toMatch(/^21:00 UTC Running started .+ ago Totals are written when the sleep ends$/);
    expect(rows[1]).toBe('11:00 UTC 17 min 3 new facts · 1 merge · 7 retired as stale Graph: 625 links added · 200 pruned');
    expect(rows[2]).toBe('15:00 UTC 17 min No fact changes');
    expect(rows[3]).toBe('09:00 UTC Failed 1 new fact');
    expect(rows[4]).toBe('03:00 UTC No finish recorded No totals recorded');
    // A state is a word in the shared pill; a finished sleep shows its duration instead.
    expect(Array.from(container.querySelectorAll('button.sleep .status-pill')).map((p) => [p.textContent, p.className.match(/status-pill--\w+/)?.[0]]))
      .toEqual([['Running', 'status-pill--info'], ['Failed', 'status-pill--bad'], ['No finish recorded', 'status-pill--warn']]);
    // The stats dict is never listed: no snapshot, no summed total and no counter name in a row.
    for (const r of rows) expect(r).not.toMatch(/f044|_attempted|144,?750|381,?291|facts_created/);
  });

  it('opens on the newest finished sleep, fetches only that one, and opens one sleep at a time', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    expect(rowOf(container, '11:00').getAttribute('aria-expanded')).toBe('true');
    expect(rowOf(container, '21:00').getAttribute('aria-expanded')).toBe('false');
    expect(detailUrls()).toEqual(['/dashboard/consolidation/c-done']);
    expect(container.querySelector('#report-c-done')).not.toBeNull();

    await fireEvent.click(rowOf(container, '15:00'));
    await waitFor(() => expect(container.querySelector('#report-c-quiet')).not.toBeNull());
    expect(container.querySelector('#report-c-done')).toBeNull();
    expect(detailUrls()).toEqual(['/dashboard/consolidation/c-done', '/dashboard/consolidation/c-quiet']);

    // Closing and reopening a sleep reads what was fetched.
    await fireEvent.click(rowOf(container, '15:00'));
    expect(container.querySelector('#report-c-quiet')).toBeNull();
    await fireEvent.click(rowOf(container, '11:00'));
    await screen.findByRole('heading', { level: 3, name: /Merged/ });
    expect(detailUrls()).toHaveLength(2);
  });

  it('the header says when the last sleep started, how many there were, and how many have no finish', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    const pills = Array.from(container.querySelectorAll('.meta > span')).map((p) => p.textContent?.trim());
    expect(pills[0]).toMatch(/^Last sleep started Nov 13 21:00 UTC · .+/);
    expect(pills.slice(1)).toEqual(['657 sleeps in all', '1 with no finish recorded']);
    expect(container.querySelector('.meta .warn')?.textContent).toBe('1 with no finish recorded');
  });

  it('Refresh reloads the sleeps, the counts and the open report', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(false));
    urls = [];
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(urls.sort()).toEqual(['/dashboard/activity?hours=168', '/dashboard/consolidation/c-done', '/dashboard/consolidation?limit=30']));
    await screen.findByRole('heading', { level: 3, name: /Merged/ });
  });

  it('offers older sleeps when a full page came back, and asks for 30 more', async () => {
    list = { cycles: Array.from({ length: 30 }, (_, i) => cycle(`c-${i}`, `2026-10-${String(30 - i).padStart(2, '0')}T04:00:00+00:00`)) };
    install();
    const { container } = render(Consolidation);
    await waitFor(() => expect(container.querySelectorAll('button.sleep')).toHaveLength(30));
    await fireEvent.click(await screen.findByRole('button', { name: 'Show 30 older sleeps' }));
    await waitFor(() => expect(urls).toContain('/dashboard/consolidation?limit=60'));
    cleanup();
    list = LIST;
    install();
    const short = render(Consolidation);
    await loaded(short.container);
    expect(screen.queryByRole('button', { name: /older sleeps/ })).toBeNull();
  });
});

describe('the report of a sleep', () => {
  it('reads as sections in order, each with its count and the phase it came from; phase summaries stay out', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    const report = container.querySelector('#report-c-done') as HTMLElement;
    const heads = Array.from(report.querySelectorAll('h3')).map((h) => h.textContent?.replace(/\s+/g, ' ').trim());
    expect(heads).toEqual([
      'Learned 1 reflect · learn', 'Merged 1 f027_consolidate · merge', 'Retired as stale 7 stale_scan · deactivate',
      'Other changes 2 later_phase · reshape', 'Graph upkeep', 'Housekeeping',
    ]);
    expect(report.textContent).not.toContain('graph_densify phase summary');
  });

  it('a merge says how many facts became one, with the new text above the texts it came from, and why', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    const merged = (await screen.findByRole('heading', { level: 3, name: /Merged/ })).closest('section') as HTMLElement;
    expect(within(merged).getByText('2 facts became one')).toBeTruthy();
    expect(merged.querySelector('.item-text')?.textContent).toBe('uses Postgres with pgvector');
    const sources = merged.querySelector('details.earlier') as HTMLDetailsElement;
    expect(sources.open).toBe(true);
    expect(sources.querySelector('summary')?.textContent?.trim()).toBe('From 2 facts');
    expect(Array.from(sources.querySelectorAll('li')).map((l) => l.textContent)).toEqual(['uses Postgres', 'uses pgvector']);
    expect(merged.querySelector('.why')?.textContent?.replace(/\s+/g, ' ')).toBe("Whycluster merge of 2 facts on 'storage'");
    expect(Array.from(merged.querySelectorAll('.ids code')).map((c) => c.textContent)).toEqual(['f-a', 'f-b', 'f-c']);
  });

  it('a text the audit cut ends in an ellipsis, and the report says what the ellipsis means', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    const learned = (await screen.findByRole('heading', { level: 3, name: /Learned/ })).closest('section') as HTMLElement;
    expect(within(learned).getByText('kettle')).toBeTruthy();
    expect(learned.querySelector('.item-text')?.textContent).toBe(`${LONG}…`);
    expect(container.querySelector('.foot-note')?.textContent).toBe('A text that ends in an ellipsis is its first 200 characters: that is what the audit keeps.');
  });

  it('a long section shows five rows until asked for all; a rationale every row shares is said once', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    const retired = (await screen.findByRole('heading', { level: 3, name: /Retired as stale/ })).closest('section') as HTMLElement;
    expect(retired.querySelectorAll('li.item')).toHaveLength(5);
    expect(retired.querySelector('.sec-note')?.textContent).toBe('stale: aged > 60d, no recall in window');
    expect(retired.querySelector('.why')).toBeNull();
    await fireEvent.click(within(retired).getByRole('button', { name: 'Show all 7' }));
    expect(retired.querySelectorAll('li.item')).toHaveLength(7);
    expect(within(retired).queryByRole('button', { name: /Show all/ })).toBeNull();
    expect(within(retired).getByText('old subject 6')).toBeTruthy();
  });

  it('a row in a shape the screen does not know keeps its recorded before and after; a missing side is not the string "none"', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    const other = (await screen.findByRole('heading', { level: 3, name: /Other changes/ })).closest('section') as HTMLElement;
    const sides = Array.from(other.querySelectorAll('.raw-sides'));
    expect(sides).toHaveLength(2);
    for (const s of sides) expect(Array.from(s.querySelectorAll('.raw-label')).map((l) => l.textContent)).toEqual(['Before', 'After']);
    // Review M9 (the owner's no-symbol rule): a missing side is a muted italic word outside the code block, so it
    // differs from a recorded value "none", which stays in its <pre>.
    expect(sides[0].querySelector(':scope > div:first-child pre')).toBeNull();
    expect(sides[0].querySelector(':scope > div:first-child em.raw-none')?.textContent).toBe('none');
    expect(sides[1].querySelector(':scope > div:first-child pre')?.textContent).toBe('none');
    expect(sides[1].querySelector(':scope > div:first-child .raw-none')).toBeNull();
    expect(document.body.textContent).not.toMatch(/[→∅▼▸]/);
  });

  it('the counters are said in plain words beside their names, and the whole record stays one disclosure away', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    const graph = (await screen.findByRole('heading', { level: 3, name: 'Graph upkeep' })).closest('section') as HTMLElement;
    expect(Array.from(graph.querySelectorAll('.count-list > div')).map((d) => d.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
      'Links for memories that had none orphan_edges_created 539', 'Co-occurrence links cooccurrence_edges 86',
      'Links to inactive memories pruned dead_edges_pruned 200',
    ]);
    const house = (await screen.findByRole('heading', { level: 3, name: 'Housekeeping' })).closest('section') as HTMLElement;
    expect(house.textContent).toMatch(/Facts examined for staleness\s+stale_examined\s*3,988/);
    expect(house.textContent).toMatch(/Rubric evolved\s+rubric_evolved\s*yes/);
    const record = container.querySelector('#report-c-done details.record') as HTMLDetailsElement;
    expect(record.open).toBe(false);
    expect(record.querySelector('summary')?.textContent?.trim()).toBe('Raw record');
    expect(JSON.parse(record.querySelector('pre')!.textContent!)).toEqual(TOTALS);
    expect(Array.from(record.querySelectorAll('.phases code')).map((c) => c.textContent)).toEqual(['review', 'reflect', 'stale_scan']);
  });

  it('a sleep with nothing to itemise says which kind of nothing it is', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    const say = async (clock: string, id: string) => {
      await fireEvent.click(rowOf(container, clock));
      await waitFor(() => expect(container.querySelector(`#report-${id} .state-note`)).not.toBeNull());
      return Array.from(container.querySelectorAll(`#report-${id} .state-note`)).map((p) => p.textContent?.replace(/\s+/g, ' ').trim());
    };
    // Every report answers with DETAIL's cycle; only c-quiet's empty answer matters here, so serve none for the rest.
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ cycle: null, actions: [] }) }) as Response));
    expect(await say('15:00', 'c-quiet')).toEqual(['This sleep changed no facts.']);
    expect(await say('09:00', 'c-fail')).toEqual([
      'This sleep failed. What it recorded before the failure is below.',
      'No change rows are kept for this sleep; its counts remain. The audit prunes change rows after its retention window.',
    ]);
    expect(await say('03:00', 'c-lost')).toEqual([
      'No finish was recorded for this sleep. What it recorded before it stopped is below.', 'No change is recorded for this sleep.',
    ]);
    expect(await say('21:00', 'c-run')).toEqual([
      'No finish is recorded for this sleep yet. A phase records its changes when it ends, so more may follow.', 'No change is recorded for this sleep.',
    ]);
  });

  it('a report that did not load says which sleep, and Try again fetches it', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    failing = ['/dashboard/consolidation/'];
    await fireEvent.click(rowOf(container, '15:00'));
    const msg = await waitFor(() => {
      const m = container.querySelector('#report-c-quiet .report-msg.error');
      expect(m).not.toBeNull();
      return m as HTMLElement;
    });
    expect(msg.textContent?.replace(/\s+/g, ' ').trim()).toBe('The changes of the sleep of Nov 12 15:00 UTC did not load. Try again');
    failing = [];
    await fireEvent.click(within(msg).getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(container.querySelector('#report-c-quiet h3')).not.toBeNull());
  });
});

describe('the chart of the last sleeps', () => {
  it('draws one bar per sleep, oldest first, each named by its time and numbers', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    const bars = Array.from(container.querySelectorAll('button.bar'));
    expect(bars.map((b) => b.getAttribute('aria-label'))).toEqual([
      'Nov 12 03:00 UTC: New facts 0 · Retired as stale 0. Open this sleep.',
      'Nov 12 09:00 UTC: New facts 1 · Retired as stale 0. Open this sleep.',
      'Nov 12 15:00 UTC: New facts 0 · Retired as stale 0. Open this sleep.',
      'Nov 13 11:00 UTC: New facts 3 · Retired as stale 7. Open this sleep.',
      'Nov 13 21:00 UTC: New facts 0 · Retired as stale 0. Open this sleep.',
    ]);
    // One tab stop: the open sleep's bar. The readout says the open sleep until a bar is pointed at.
    expect(bars.map((b) => b.getAttribute('tabindex'))).toEqual(['-1', '-1', '-1', '0', '-1']);
    expect(container.querySelector('.readout')?.textContent?.replace(/\s+/g, ' ').trim()).toBe('Nov 13 11:00 UTC New facts 3 · Retired as stale 7');
    await fireEvent.mouseEnter(bars[1]);
    expect(container.querySelector('.readout')?.textContent?.replace(/\s+/g, ' ').trim()).toBe('Nov 12 09:00 UTC New facts 1 · Retired as stale 0');
    expect(Array.from(container.querySelectorAll('.legend li')).map((l) => l.textContent?.trim())).toEqual(
      ['New facts, above the line, up to 3', 'Retired as stale, below the line, up to 7']);
  });

  it('a bar opens its sleep in the list', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    await fireEvent.click(container.querySelectorAll('button.bar')[2]);
    await waitFor(() => expect(rowOf(container, '15:00').getAttribute('aria-expanded')).toBe('true'));
    expect(rowOf(container, '11:00').getAttribute('aria-expanded')).toBe('false');
    expect(container.querySelector('button.bar.open')?.getAttribute('aria-label')).toMatch(/^Nov 12 15:00 UTC/);
  });

  it('switches between facts, graph and duration; the duration has no lower half', async () => {
    install();
    const { container } = render(Consolidation);
    await loaded(container);
    const seg = screen.getByRole('group', { name: 'What the chart measures' });
    const pressed = () => within(seg).getAllByRole('button').map((b) => [b.textContent?.trim(), b.getAttribute('aria-pressed')]);
    expect(pressed()).toEqual([['Facts', 'true'], ['Graph', 'false'], ['Duration', 'false']]);
    await fireEvent.click(within(seg).getByRole('button', { name: 'Graph' }));
    expect(pressed()[1]).toEqual(['Graph', 'true']);
    expect(container.querySelector('.readout-what')?.textContent).toBe('Links added 625 · Links pruned 200');
    expect(container.querySelectorAll('button.bar .half.down')).toHaveLength(5);
    await fireEvent.click(within(seg).getByRole('button', { name: 'Duration' }));
    expect(container.querySelector('.readout-what')?.textContent).toBe('Minutes 17');
    expect(container.querySelectorAll('button.bar .half.down')).toHaveLength(0);
    expect(Array.from(container.querySelectorAll('.legend li')).map((l) => l.textContent?.trim())).toEqual(['Minutes, up to 17']);
  });
});

describe('the states of the page', () => {
  it('with no audited sleep it names the flag, and keeps the last finished sleep\'s counts from the activity', async () => {
    list = { cycles: [] };
    install();
    const { container } = render(Consolidation);
    await screen.findByRole('heading', { level: 2, name: 'No sleep is in the audit yet' });
    expect(container.querySelector('.empty-card code')?.textContent).toBe('NOUS_CONSOLIDATION_AUDIT_ENABLED');
    await waitFor(() => expect(container.querySelector('.last-finished')?.textContent?.replace(/\s+/g, ' ').trim())
      .toBe('The last sleep that finished: 9 facts created · 1 procedures created · 2 censors retired.'));
    expect(container.querySelector('button.bar')).toBeNull();
  });

  it('when the sleeps do not load it says so and Try again fetches them; the counts still show', async () => {
    failing = ['/dashboard/consolidation'];
    install();
    const { container } = render(Consolidation);
    const msg = await screen.findByText(/The sleeps did not load\./);
    await waitFor(() => expect(container.querySelector('.meta')?.textContent).toContain('657 sleeps in all'));
    failing = [];
    await fireEvent.click(within(msg.closest('p') as HTMLElement).getByRole('button', { name: 'Try again' }));
    await loaded(container);
  });

  it('source contract: no colour literal and no raw HTML in the three files; the header may wrap', () => {
    for (const src of SRC) {
      expect(src).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(/);
      expect(src).not.toContain('{@html');
    }
    expect(styleBlocks(SRC[0])).toMatch(/\.view-head \{[^}]*flex-wrap: wrap;/);
  });
});
