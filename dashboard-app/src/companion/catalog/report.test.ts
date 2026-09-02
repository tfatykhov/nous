import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { render, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import Renderer from '../Renderer.svelte';
import MetricCardView from './MetricCardView.svelte';
import ScoreCardView from './ScoreCardView.svelte';
import DeltaListView from './DeltaListView.svelte';
import DataTableView from './DataTableView.svelte';
import ChipRowView from './ChipRowView.svelte';
import SectionView from './SectionView.svelte';
import AppHeaderView from './AppHeaderView.svelte';
import TimelineView from './TimelineView.svelte';
import KeyValueTableView from './KeyValueTableView.svelte';
import { store } from '../store.svelte';

// F096 report-vocabulary adapters. Same harness discipline as
// microapp.test.ts / charts.test.ts: module-singleton store reset per test,
// assertions against the real DOM. The through-line: every string renders
// PREFORMATTED (never rounded, never re-signed), tone lands on the pill /
// rule / row ink and never on a value, and every empty state is a state.

const SURFACE = 'report-test-surface';

function seed(dataModel: Record<string, unknown>, components: Record<string, unknown>[] = []) {
  store.reset();
  store.apply(null, {
    version: 'v1.0',
    createSurface: {
      surfaceId: SURFACE,
      catalogId: 'nous-core',
      components,
      dataModel,
      metadata: { extensions: { com_nous_nonce: 'n' } },
    },
  } as never);
}

const series = (vs: (number | null)[], meta: Record<string, unknown> = {}) => ({
  kind: 'series',
  unit: 'bpm',
  points: vs.map((v, i) => ({ t: `2026-08-${String(i + 1).padStart(2, '0')}`, v })),
  meta: { dropped: 0, downsampled_from: null, ...meta },
});

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function mount(View: any, comp: Record<string, unknown>) {
  return render(View, { props: { surfaceId: SURFACE, comp } });
}

beforeEach(() => store.reset());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('MetricCardView', () => {
  const card = {
    id: 'm',
    component: 'MetricCard',
    label: 'Resting HR',
    value: '65.0',
    unit: 'bpm',
    delta: '↓4.3 bpm · improving',
    tone: 'ok',
    caption: '68.9 → 64.6 (28d avg, n=28)',
    footnote: 'last 2026-09-01',
  };

  it('renders the full story with the tone on the pill and the trend, never the value', () => {
    seed({ hr: series([68, 66, 67, 65, 64, 65]) });
    const { container } = mount(MetricCardView, { ...card, trend: '/hr' });
    const root = container.querySelector('.metric') as HTMLElement;
    expect(root.style.getPropertyValue('--ink')).toBe('var(--ok)');
    expect(root.style.getPropertyValue('--tone')).toBe('var(--ok)');
    expect(container.querySelector('.pill')?.textContent).toBe('↓4.3 bpm · improving');
    expect(container.querySelector('.value')?.textContent).toBe('65.0bpm');
    expect(container.querySelector('.caption')?.textContent).toBe('68.9 → 64.6 (28d avg, n=28)');
    expect(container.querySelector('.foot')?.textContent).toBe('last 2026-09-01');
    expect(container.querySelector('svg')).not.toBeNull();
    expect(container.querySelectorAll('circle.end').length).toBe(1);
    expect(container.querySelector('polygon')).toBeNull();
    // the embedded frame renders no current-value head (the agent's value is above)
    expect(container.querySelector('.cur')).toBeNull();
  });

  it('never reformats: a long preformatted value renders verbatim', () => {
    seed({});
    const { container } = mount(MetricCardView, { ...card, value: '1234.5678', delta: undefined });
    expect(container.querySelector('.value')?.textContent).toBe('1234.5678bpm');
    expect(container.textContent).not.toContain('1.2k');
    expect(container.querySelector('.pill')).toBeNull(); // no delta ⇒ no pill
  });

  it('neutral tone: pill ink is --soft, the stroke is the axis grey', () => {
    seed({});
    const { container } = mount(MetricCardView, { ...card, tone: 'neutral' });
    const root = container.querySelector('.metric') as HTMLElement;
    expect(root.style.getPropertyValue('--ink')).toBe('var(--soft)');
    expect(root.style.getPropertyValue('--tone')).toBe('var(--chart-axis)');
  });

  it('no trend prop, or a trend resolving to nothing, renders no chart region at all', () => {
    seed({ metrics: [{ label: 'a' }] });
    for (const comp of [card, { ...card, trend: '/metrics/0/trend' }]) {
      const { container } = mount(MetricCardView, comp);
      expect(container.querySelector('svg')).toBeNull();
      expect(container.querySelector('.state')).toBeNull();
      cleanup();
    }
  });

  it('single reading and empty series are states — without a renderer-rounded figure', () => {
    seed({ one: series([62]), none: series([], { reason: 'no rows in window' }) });
    const single = mount(MetricCardView, { ...card, trend: '/one' });
    expect(single.container.querySelector('.state')?.textContent).toBe('single reading');
    expect(single.container.querySelector('svg')).toBeNull();
    cleanup();
    const empty = mount(MetricCardView, { ...card, trend: '/none' });
    expect(empty.container.querySelector('.state')?.textContent).toBe('no data — no rows in window');
    cleanup();
    seed({ bad: [1, 2, 3] });
    const bad = mount(MetricCardView, { ...card, trend: '/bad' });
    expect(bad.container.querySelector('.state')?.textContent).toContain('not a series');
  });

  it('resolves a relative trend and a bound tone per item inside a repeat scope', () => {
    seed({
      metrics: [
        { tone: 'crit', trend: series([1, 2, 3, 4, 5, 6, 7, 8], { focus_from: '2026-08-05' }) },
        { tone: 'purple' },
      ],
    });
    const { container } = render(MetricCardView, {
      props: {
        surfaceId: SURFACE,
        comp: { ...card, trend: 'trend', trendline: true, tone: { path: 'tone' } },
        scope: { base: '/metrics/0', index: 0 },
      },
    });
    expect(container.querySelector('rect.focus')).not.toBeNull();
    expect(container.querySelectorAll('polyline.raw').length).toBe(1);
    expect((container.querySelector('.metric') as HTMLElement).style.getPropertyValue('--ink')).toBe(
      'var(--crit)',
    );
    cleanup();
    // an unknown resolved tone closes to neutral — never a literal colour
    const second = render(MetricCardView, {
      props: {
        surfaceId: SURFACE,
        comp: { ...card, tone: { path: 'tone' } },
        scope: { base: '/metrics/1', index: 1 },
      },
    });
    expect(
      (second.container.querySelector('.metric') as HTMLElement).style.getPropertyValue('--ink'),
    ).toBe('var(--soft)');
  });
});

describe('ScoreCardView', () => {
  it('renders the verdict, the top-rule ink, and per-row tones; no value is legal', () => {
    seed({
      goals: [
        {
          items: [
            { label: 'Resting HR', value: '↓4.3 bpm', tone: 'ok' },
            { label: 'Sleep', value: '↓0.40 h', tone: 'crit' },
            { label: 'SpO₂', value: '↑0.4 %' },
          ],
        },
      ],
    });
    const { container } = mount(ScoreCardView, {
      id: 's',
      component: 'ScoreCard',
      title: 'Improve health',
      status: 'on track',
      tone: 'ok',
      items: { path: '/goals/0/items' },
      note: 'Cardio-recovery markers plus blood pressure.',
    });
    const root = container.querySelector('.score') as HTMLElement;
    expect(root.style.getPropertyValue('--ink')).toBe('var(--ok)');
    expect(container.querySelector('.status')?.textContent).toBe('on track');
    expect(container.querySelector('.value')).toBeNull();
    const rows = container.querySelectorAll('li');
    expect(rows.length).toBe(3);
    expect((rows[1] as HTMLElement).style.getPropertyValue('--row-ink')).toBe('var(--crit)');
    expect((rows[2] as HTMLElement).style.getPropertyValue('--row-ink')).toBe('var(--soft)');
    expect(rows[0].querySelector('.rv')?.textContent).toBe('↓4.3 bpm');
    expect(container.querySelector('.note')?.textContent).toContain('Cardio-recovery');
  });

  it('renders the headline value + unit verbatim and tolerates absent items', () => {
    seed({});
    const { container } = mount(ScoreCardView, {
      id: 's',
      component: 'ScoreCard',
      title: 'Lose fat',
      status: 'slipping',
      tone: 'crit',
      value: '28.3',
      unit: 'kg',
      caption: 'Fat mass · ↓0.4 vs prior 28d',
    });
    expect(container.querySelector('.value')?.textContent).toBe('28.3kg');
    expect(container.querySelector('.caption')?.textContent).toBe('Fat mass · ↓0.4 vs prior 28d');
    expect(container.querySelector('ul')).toBeNull();
  });

  it('semantic prose detection: short prose stays stacked, long figure stays inline', () => {
    // "payment pending" (15 chars) must not inherit figure styling
    seed({ items: [{ label: 'Status', value: 'payment pending' }] });
    const proseResult = mount(ScoreCardView, {
      id: 's', component: 'ScoreCard', title: 'T', status: 'S', tone: 'neutral',
      items: { path: '/items' },
    });
    expect(proseResult.container.querySelector('ul.prose')).not.toBeNull();
    cleanup();

    // "EUR 1,234,567.89" (17 chars) and ISO datetime must not be forced into stacked mode
    seed({
      items: [
        { label: 'Revenue', value: 'EUR 1,234,567.89' },
        { label: 'As of', value: '2026-09-02T15:40:43Z' },
      ],
    });
    const figResult = mount(ScoreCardView, {
      id: 's', component: 'ScoreCard', title: 'T', status: 'S', tone: 'neutral',
      items: { path: '/items' },
    });
    expect(figResult.container.querySelector('ul.prose')).toBeNull();
  });

  it('explicit row format="prose" overrides figure inference', () => {
    // A figure-shaped value ("42") forced to prose by the producer
    seed({ items: [{ label: 'Note', value: '42', format: 'prose' }] });
    const { container } = mount(ScoreCardView, {
      id: 's', component: 'ScoreCard', title: 'T', status: 'S', tone: 'neutral',
      items: { path: '/items' },
    });
    expect(container.querySelector('ul.prose')).not.toBeNull();
  });

  it('explicit row format="figure" overrides prose inference', () => {
    // A prose-shaped value ("payment pending") forced to figure treatment by the producer
    seed({ items: [{ label: 'Label', value: 'payment pending', format: 'figure' }] });
    const { container } = mount(ScoreCardView, {
      id: 's', component: 'ScoreCard', title: 'T', status: 'S', tone: 'neutral',
      items: { path: '/items' },
    });
    expect(container.querySelector('ul.prose')).toBeNull();
  });

  it('card-level format="prose" forces stacked mode for all figure-shaped rows', () => {
    seed({ items: [{ label: 'HR', value: '65.0 bpm' }, { label: 'SpO₂', value: '99%' }] });
    const { container } = mount(ScoreCardView, {
      id: 's', component: 'ScoreCard', title: 'T', status: 'S', tone: 'neutral',
      format: 'prose', items: { path: '/items' },
    });
    expect(container.querySelector('ul.prose')).not.toBeNull();
  });

  it('card-level format="figure" prevents stacked mode even with prose-shaped rows', () => {
    seed({ items: [{ label: 'Status', value: 'payment pending' }] });
    const { container } = mount(ScoreCardView, {
      id: 's', component: 'ScoreCard', title: 'T', status: 'S', tone: 'neutral',
      format: 'figure', items: { path: '/items' },
    });
    expect(container.querySelector('ul.prose')).toBeNull();
  });
});

describe('DeltaListView', () => {
  const list = { id: 'd', component: 'DeltaList', rows: { path: '/movers' } };

  it('renders label · tone-coloured delta · from → to', () => {
    seed({
      movers: [
        { label: 'Resting HR', delta: '↓4.3 bpm', from: '68.9', to: '64.6', tone: 'ok' },
        { label: 'Weight', delta: '↑0.3 kg', to: '91.3', tone: 'crit' },
      ],
    });
    const { container } = mount(DeltaListView, list);
    const rows = container.querySelectorAll('li');
    expect(rows.length).toBe(2);
    expect(rows[0].querySelector('.d')?.textContent).toBe('↓4.3 bpm');
    expect(rows[0].querySelector('.r')?.textContent).toBe('68.9 → 64.6');
    expect((rows[0] as HTMLElement).style.getPropertyValue('--row-ink')).toBe('var(--ok)');
    expect(rows[1].querySelector('.r')?.textContent).toBe('91.3'); // one side only
    expect((rows[1] as HTMLElement).style.getPropertyValue('--row-ink')).toBe('var(--crit)');
  });

  it('an empty list is a state: emptyText, with a default', () => {
    seed({ movers: [] });
    const custom = mount(DeltaListView, { ...list, emptyText: 'no significant adverse moves' });
    expect(custom.container.querySelector('.empty')?.textContent).toBe('no significant adverse moves');
    cleanup();
    const dflt = mount(DeltaListView, list);
    expect(dflt.container.querySelector('.empty')?.textContent).toBe('nothing to report');
    cleanup();
    seed({ movers: 'nope' });
    expect(mount(DeltaListView, list).container.querySelector('.empty')).not.toBeNull();
  });
});

describe('DataTableView', () => {
  const table = {
    id: 't',
    component: 'DataTable',
    columns: [
      { key: 'week', label: 'Week' },
      { key: 'sessions', label: 'Sessions', align: 'end' },
      { key: 'types', label: 'Types', secondary: true },
    ],
    rows: { path: '/log' },
  };

  it('renders headers and cells with end/secondary classes, verbatim', () => {
    seed({
      log: [
        { week: 'week of 2026-08-31', sessions: '2', types: 'indoor cardio, rowing' },
        { week: 'week of 2026-08-24', sessions: '6' },
      ],
    });
    const { container } = mount(DataTableView, table);
    expect(container.querySelectorAll('th').length).toBe(3);
    expect(container.querySelector('th.end')?.textContent).toBe('Sessions');
    const cells = container.querySelectorAll('tbody tr:first-child td');
    expect(cells[1].classList.contains('end')).toBe(true);
    expect(cells[2].classList.contains('secondary')).toBe(true);
    expect(cells[2].textContent).toBe('indoor cardio, rowing');
    // browser check on a 390px phone: "2026-08-31" wrapped as "2026-/08-31"
    // because a hyphen is a break opportunity — single-token cells get
    // `token` (nowrap); multi-word cells keep wrapping at spaces.
    expect(cells[0].classList.contains('token')).toBe(false); // "week of 2026-08-31"
    expect(cells[1].classList.contains('token')).toBe(true); // "2"
    expect(cells[2].classList.contains('token')).toBe(false);
    expect(container.querySelector('.scroll table.dtable')).not.toBeNull();
    // a missing field is an empty cell, never a throw
    expect(container.querySelectorAll('tbody tr')[1].querySelectorAll('td')[2].textContent).toBe('');
  });

  it('survives duplicate column keys without a keyed-each crash', () => {
    // The grammar rejects duplicates at compose time, but the renderer never
    // trusts its input: Svelte's keyed each throws in prod on a duplicate key
    // and would take down the whole surface (review P1; LineChart precedent).
    seed({ log: [{ a: '1' }] });
    const { container } = mount(DataTableView, {
      ...table,
      columns: [{ key: 'a', label: 'A' }, { key: 'a', label: 'A again' }],
    });
    expect(container.querySelectorAll('th').length).toBe(2);
    expect(container.querySelectorAll('td').length).toBe(2);
  });

  it('renders emptyText when there are no rows and caps columns at six', () => {
    seed({ log: [] });
    const empty = mount(DataTableView, { ...table, emptyText: 'no sessions logged' });
    expect(empty.container.querySelector('.empty')?.textContent).toBe('no sessions logged');
    expect(empty.container.querySelector('table')).toBeNull();
    cleanup();
    seed({ log: [{ a: 1 }] });
    const wide = mount(DataTableView, {
      ...table,
      columns: Array.from({ length: 8 }, (_, i) => ({ key: `k${i}`, label: String(i) })),
    });
    expect(wide.container.querySelectorAll('th').length).toBe(6);
  });
});

describe('ChipRowView', () => {
  it('renders labelled chips with the value in the tone ink', () => {
    seed({
      lanes: [
        { label: 'garmin', value: 'today', detail: '410 days', tone: 'ok' },
        { label: 'health connect (bp)', value: '8d ago', detail: 'export present', tone: 'crit' },
        { label: 'scale', value: 'today' },
      ],
    });
    const { container } = mount(ChipRowView, { id: 'c', component: 'ChipRow', items: { path: '/lanes' } });
    const chips = container.querySelectorAll('.chip');
    expect(chips.length).toBe(3);
    expect(chips[0].querySelector('.l')?.textContent).toBe('garmin');
    expect(chips[0].querySelector('.v')?.textContent).toBe('today');
    expect(chips[0].querySelector('.dt')?.textContent).toBe('· 410 days');
    expect((chips[1] as HTMLElement).style.getPropertyValue('--ink')).toBe('var(--crit)');
    expect(chips[2].querySelector('.dt')).toBeNull();
    cleanup();
    seed({ lanes: null });
    expect(
      mount(ChipRowView, { id: 'c', component: 'ChipRow', items: { path: '/lanes' } }).container.querySelector('.chip'),
    ).toBeNull();
  });
});

describe('Section caption + cards layout (F096 §4.1 / §4.2)', () => {
  it('renders a bound caption in the head and maps cards to a class', () => {
    seed({ meta: { window: '28d vs prior 28d' } }, [
      { id: 'body', component: 'Text', text: 'x' },
    ]);
    const { container } = mount(SectionView, {
      id: 's',
      component: 'Section',
      title: 'Retrieval',
      child: 'body',
      layout: 'cards',
      caption: { path: '/meta/window' },
    });
    expect(container.querySelector('section.app-section.cards')).not.toBeNull();
    expect(container.querySelector('.head .caption')?.textContent).toBe('28d vs prior 28d');
  });

  it('keeps the caption inside the accordion toggle', () => {
    seed({}, [{ id: 'body', component: 'Text', text: 'x' }]);
    const { container } = mount(SectionView, {
      id: 's',
      component: 'Section',
      title: 'Raw',
      child: 'body',
      layout: 'accordion',
      caption: 'retrieval_log',
    });
    expect(container.querySelector('button.toggle .caption')?.textContent).toBe('retrieval_log');
  });
});

describe('whole-feature report app (F096 AC1)', () => {
  // The fixture is exported from tests/test_a2ui_report.py (the python side
  // asserts the export is current), so both harnesses render ONE app.
  const fixture = JSON.parse(
    readFileSync('src/companion/catalog/__fixtures__/f096-report-app.json', 'utf-8'),
  ) as { components: Record<string, unknown>[]; dataModel: Record<string, unknown> };

  it('renders every record through every new component with no placeholder', async () => {
    seed(fixture.dataModel, fixture.components);
    const { container } = render(Renderer, { props: { surfaceId: SURFACE, componentId: 'root' } });
    const retrieval = fixture.dataModel.retrieval as unknown[];
    const goals = fixture.dataModel.goals as unknown[];
    const sleep = fixture.dataModel.sleep as unknown[];
    expect(container.querySelectorAll('.metric').length).toBe(retrieval.length);
    expect(container.querySelectorAll('.score').length).toBe(goals.length);
    // three trended metrics draw; the count card has no chart region
    expect(container.querySelectorAll('.metric svg').length).toBe(retrieval.length - 1);
    expect(container.querySelectorAll('.metric rect.focus').length).toBe(retrieval.length - 1);
    expect(container.querySelectorAll('section.app-section.cards').length).toBe(2);
    expect(container.querySelector('.deltas .empty')?.textContent).toBe('no significant adverse moves');
    expect(container.querySelectorAll('.chip').length).toBe(3);
    expect(container.querySelector('.app-header .note')?.textContent).toBe('data through 2026-09-01');
    expect(container.querySelector('.ph')).toBeNull();
    // the accordion keeps the raw table collapsed until tapped — so open it,
    // or AC1's "every record" claim would never cover the DataTable (review P2)
    expect(container.querySelector('table.dtable')).toBeNull();
    expect(container.querySelector('button.toggle .caption')?.textContent).toBe('last 4 nights');
    await fireEvent.click(container.querySelector('button.toggle') as HTMLElement);
    await tick();
    expect(container.querySelectorAll('table.dtable tbody tr').length).toBe(sleep.length);
    expect(container.querySelectorAll('table.dtable th').length).toBe(4);
    expect(container.querySelector('.ph')).toBeNull();
  });
});

describe('server truncation marker (F096 §6.1 — codex P2 on #630)', () => {
  // `_bound` appends {_truncated, omitted} to a record list it could not fit.
  // That entry is NOT a record: it must never render as a blank card/row, and
  // the omitted count must be visible.
  const marker = { _truncated: true, omitted: 3 };

  it('DeltaList, DataTable, ChipRow, Timeline and KeyValueTable skip it and say how many were cut', () => {
    seed({
      rows: [{ label: 'a', delta: '↑1', key: 'k', value: 'v', at: 't', night: 'n' }, marker],
    });
    const cases: [unknown, Record<string, unknown>, string][] = [
      [DeltaListView, { id: 'd', component: 'DeltaList', rows: { path: '/rows' } }, 'li:not(.none)'],
      [
        DataTableView,
        { id: 't', component: 'DataTable', columns: [{ key: 'night', label: 'N' }], rows: { path: '/rows' } },
        'tbody tr',
      ],
      [ChipRowView, { id: 'c', component: 'ChipRow', items: { path: '/rows' } }, '.chip:not(.omitted)'],
      [TimelineView, { id: 'tl', component: 'Timeline', items: { path: '/rows' } }, 'li'],
      [KeyValueTableView, { id: 'kv', component: 'KeyValueTable', rows: { path: '/rows' } }, 'tr'],
    ];
    for (const [View, comp, rowSel] of cases) {
      const { container } = mount(View, comp);
      expect(container.querySelectorAll(rowSel).length, comp.component as string).toBe(1);
      expect(container.querySelector('.omitted')?.textContent, comp.component as string).toContain(
        '3 more',
      );
      cleanup();
    }
  });

  it('ScoreCard evidence filters the marker; a marker-only ChipRow still reports the omission', () => {
    seed({ items: [{ label: 'l', value: 'v' }, marker], only: [marker] });
    const score = mount(ScoreCardView, {
      id: 's',
      component: 'ScoreCard',
      title: 'T',
      status: 'ok',
      items: { path: '/items' },
    });
    expect(score.container.querySelectorAll('li').length).toBe(1);
    expect(score.container.querySelector('.omitted')?.textContent).toContain('3 more');
    cleanup();
    const chips = mount(ChipRowView, { id: 'c', component: 'ChipRow', items: { path: '/only' } });
    expect(chips.container.querySelectorAll('.chip:not(.omitted)').length).toBe(0);
    expect(chips.container.querySelector('.omitted')?.textContent).toContain('3 more');
  });

  it('a marker-only source is truncated, not empty: no emptyText, just the note', () => {
    seed({ only: [marker] });
    const dl = mount(DeltaListView, { id: 'd', component: 'DeltaList', rows: { path: '/only' }, emptyText: 'none' });
    expect(dl.container.querySelector('.empty')).toBeNull();
    expect(dl.container.querySelector('.omitted')?.textContent).toContain('3 more');
    cleanup();
    const dt = mount(DataTableView, {
      id: 't',
      component: 'DataTable',
      columns: [{ key: 'a', label: 'A' }],
      rows: { path: '/only' },
      emptyText: 'none',
    });
    expect(dt.container.querySelector('.empty')).toBeNull();
    expect(dt.container.querySelector('.omitted')?.textContent).toContain('3 more');
  });

  it('a repeat template skips the marker and keeps real item scopes', () => {
    seed({ metrics: [{ label: 'a', value: '1' }, { label: 'b', value: '2' }, marker] }, [
      { id: 'col', component: 'Column', children: { componentId: 'm', path: '/metrics' } },
      { id: 'm', component: 'MetricCard', label: { path: 'label' }, value: { path: 'value' } },
    ]);
    const { container } = render(Renderer, { props: { surfaceId: SURFACE, componentId: 'col' } });
    expect(container.querySelectorAll('.metric').length).toBe(2);
    expect(container.textContent).toContain('b');
    expect(container.querySelector('.omitted')?.textContent).toContain('3 more');
  });
});

describe('AppHeader note (F096 §4.4)', () => {
  it('renders the data-reach line under the freshness stamp', () => {
    seed({ meta: { composedAt: new Date().toISOString(), reach: 'data through 2026-09-01' } });
    const { container } = mount(AppHeaderView, {
      id: 'h',
      component: 'AppHeader',
      title: 'Health',
      composedAt: { path: '/meta/composedAt' },
      note: { path: '/meta/reach' },
    });
    expect(container.querySelector('.stamp')).not.toBeNull();
    expect(container.querySelector('.note')?.textContent).toBe('data through 2026-09-01');
  });
});
