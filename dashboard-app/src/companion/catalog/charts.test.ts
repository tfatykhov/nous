import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import SparklineView from './SparklineView.svelte';
import LineChartView from './LineChartView.svelte';
import BarChartView from './BarChartView.svelte';
import { store } from '../store.svelte';

// F094 chart adapter tests: hand-rolled SVG, renderer-owned states. Assert
// against real SVG/DOM. chart.test.ts already covers the pure geometry.

const SURFACE = 'chart-test-surface';

function seed(dataModel: Record<string, unknown>) {
  store.reset();
  store.apply(null, {
    version: 'v1.0',
    createSurface: {
      surfaceId: SURFACE,
      catalogId: 'nous-core',
      components: [],
      dataModel,
      metadata: { extensions: { com_nous_nonce: 'n' } },
    },
  } as never);
}

const series = (vs: (number | null)[], unit = 'bpm', meta: Record<string, unknown> = {}) => ({
  kind: 'series',
  unit,
  points: vs.map((v, i) => ({ t: `2026-08-${String(i + 1).padStart(2, '0')}`, v })),
  meta: { dropped: 0, downsampled_from: null, ...meta },
});

beforeEach(() => store.reset());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('SparklineView', () => {
  function renderSpark(path: string, extra: Record<string, unknown> = {}) {
    return render(SparklineView, {
      props: { surfaceId: SURFACE, comp: { id: 's', component: 'Sparkline', path, ...extra } },
    });
  }

  it('draws a polyline and the current value for a normal series', () => {
    seed({ hr: series([62, 60, 58, 61]) });
    const { container } = renderSpark('/hr', { label: 'resting', tone: 'ok' });
    expect(container.querySelectorAll('polyline').length).toBeGreaterThanOrEqual(1);
    expect(container.textContent).toContain('resting');
    expect(container.textContent).toContain('61 bpm'); // last value
    // tone → token, never a literal colour
    expect((container.querySelector('.spark') as HTMLElement).style.getPropertyValue('--tone')).toBe(
      'var(--ok)',
    );
  });

  it('renders the empty state (§3.1), not a blank box', () => {
    seed({ hr: series([], 'bpm', { reason: 'no rows' }) });
    const { container } = renderSpark('/hr');
    expect(container.querySelector('polyline')).toBeNull();
    expect(container.textContent).toContain('no data');
    expect(container.textContent).toContain('no rows');
  });

  it('renders a single reading as a figure, never a one-point line', () => {
    seed({ hr: series([62]) });
    const { container } = renderSpark('/hr');
    expect(container.querySelector('svg')).toBeNull();
    expect(container.textContent).toContain('single reading');
  });

  it('breaks the line at a gap and reports dropped count', () => {
    seed({ hr: series([62, null, 58, 61]) });
    const { container } = renderSpark('/hr');
    // two segments (gap at index 1)
    expect(container.querySelectorAll('polyline').length).toBe(2);
    expect(container.textContent).toContain('1 gap');
  });

  it('shows a break marker when the domain excludes zero', () => {
    seed({ hr: series([58, 62, 60, 61]) });
    const { container } = renderSpark('/hr');
    expect(container.querySelector('text.brk')?.textContent).toBe('~');
  });

  it('renders a defensive state when the path is not a series', () => {
    seed({ hr: [1, 2, 3] });
    const { container } = renderSpark('/hr');
    expect(container.textContent).toContain('not a series');
    expect(container.textContent).toContain('array');
  });
});

describe('BarChartView', () => {
  const cats = (pairs: [string, number][]) => ({
    kind: 'series',
    unit: '',
    points: pairs.map(([t, v]) => ({ t, v })),
    meta: {},
  });

  it('renders one bar per category, always zero-based', () => {
    seed({ ep: cats([['body_battery', 98], ['hrv', 40], ['spo2', 95]]) });
    const { container } = render(BarChartView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'b', component: 'BarChart', path: '/ep', orientation: 'horizontal', tone: 'ok' },
      },
    });
    expect(container.querySelectorAll('.hrow').length).toBe(3);
    expect(container.textContent).toContain('body_battery');
    // a small value still yields a visible non-negative bar width (zero-based)
    const widths = [...container.querySelectorAll('.track i')].map(
      (i) => (i as HTMLElement).style.width,
    );
    expect(widths.every((w) => parseFloat(w) >= 0)).toBe(true);
  });

  it('renders vertical bars when orientation is vertical (default)', () => {
    seed({ ep: cats([['a', 5], ['b', 9]]) });
    const { container } = render(BarChartView, {
      props: { surfaceId: SURFACE, comp: { id: 'b', component: 'BarChart', path: '/ep' } },
    });
    expect(container.querySelectorAll('.vcol').length).toBe(2);
  });

  it('sizes negative bars by magnitude, not by vanishing (codex P2)', () => {
    // Pre-fix (v-min)/span gave the most-negative bar 0% and reversed the rest.
    seed({ ep: cats([['a', -10], ['b', -5]]) });
    const { container } = render(BarChartView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'b', component: 'BarChart', path: '/ep', orientation: 'horizontal' },
      },
    });
    const fills = [...container.querySelectorAll('.track i')] as HTMLElement[];
    expect(parseFloat(fills[0].style.width)).toBeCloseTo(100); // -10 → longest
    expect(parseFloat(fills[1].style.width)).toBeCloseTo(50); //  -5 → half
    // both are negative, so both carry the sign marker
    expect(fills.every((i) => i.classList.contains('neg'))).toBe(true);
  });

  it('gives equal-magnitude opposite-sign bars equal length, marks the negative', () => {
    seed({ ep: cats([['up', 10], ['down', -10]]) });
    const { container } = render(BarChartView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 'b', component: 'BarChart', path: '/ep', orientation: 'horizontal' },
      },
    });
    const fills = [...container.querySelectorAll('.track i')] as HTMLElement[];
    expect(parseFloat(fills[0].style.width)).toBeCloseTo(100);
    expect(parseFloat(fills[1].style.width)).toBeCloseTo(100);
    expect(fills[0].classList.contains('neg')).toBe(false);
    expect(fills[1].classList.contains('neg')).toBe(true);
  });
});

describe('LineChartView', () => {
  it('draws one polyline group per series with a legend', () => {
    seed({
      out: {
        kind: 'series',
        unit: '',
        keys: ['success', 'failure'],
        points: [
          { t: '2026-08-01', success: 3, failure: 1 },
          { t: '2026-08-02', success: 5, failure: 2 },
          { t: '2026-08-03', success: 4, failure: 0 },
        ],
        meta: {},
      },
    });
    const { container } = render(LineChartView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'l',
          component: 'LineChart',
          path: '/out',
          label: 'outcomes',
          series: [
            { key: 'success', label: 'ok', tone: 'ok' },
            { key: 'failure', label: 'bad', tone: 'crit' },
          ],
        },
      },
    });
    expect(container.querySelectorAll('polyline').length).toBeGreaterThanOrEqual(2);
    expect(container.querySelectorAll('.legend .lg').length).toBe(2);
    expect(container.textContent).toContain('ok');
    expect(container.textContent).toContain('bad');
  });

  it('caps at 4 series', () => {
    const pts = [
      { t: '2026-08-01', a: 1, b: 2, c: 3, d: 4, e: 5 },
      { t: '2026-08-02', a: 2, b: 3, c: 4, d: 5, e: 6 },
    ];
    seed({ m: { kind: 'series', unit: '', points: pts, meta: {} } });
    const { container } = render(LineChartView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'l',
          component: 'LineChart',
          path: '/m',
          series: ['a', 'b', 'c', 'd', 'e'].map((k) => ({ key: k })),
        },
      },
    });
    expect(container.querySelectorAll('.legend .lg').length).toBe(4);
  });
});
