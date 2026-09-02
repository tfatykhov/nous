import { describe, it, expect } from 'vitest';
import {
  normalizeTone,
  toneVar,
  seriesVar,
  readSeries,
  seriesValues,
  countDropped,
  classify,
  yDomain,
  yScale,
  xScale,
  lineSegments,
  formatTick,
  ticks,
  toneInkVar,
  trendWindow,
  rollingMean,
  focusStartIndex,
  type SeriesPoint,
} from './chart';

const pts = (vs: (number | null)[]): SeriesPoint[] =>
  vs.map((v, i) => ({ t: `2026-08-${String(i + 1).padStart(2, '0')}`, v: v as number }));

describe('tone + series colour (renderer-owned, model picks the name only)', () => {
  it('normalizes tone against the closed enum', () => {
    expect(normalizeTone('ok')).toBe('ok');
    expect(normalizeTone('crit')).toBe('crit');
    expect(normalizeTone('rainbow')).toBe('neutral');
    expect(normalizeTone(undefined)).toBe('neutral');
  });

  it('maps tone and series index to tokens, never to colours', () => {
    expect(toneVar('ok')).toBe('var(--ok)');
    expect(toneVar('neutral')).toBe('var(--chart-axis)');
    expect(seriesVar(0)).toBe('var(--series-1)');
    expect(seriesVar(3)).toBe('var(--series-4)');
    expect(seriesVar(9)).toBe('var(--series-4)'); // clamped
  });
});

describe('finite extraction + gaps', () => {
  it('keeps original index so drops become breaks, not shifts', () => {
    const finite = seriesValues(pts([1, null, 3]));
    expect(finite).toEqual([
      { i: 0, v: 1 },
      { i: 2, v: 3 },
    ]);
  });

  it('drops NaN/Inf and counts fully-dropped points', () => {
    const p: SeriesPoint[] = [
      { t: 'a', v: 1 },
      { t: 'b', v: NaN },
      { t: 'c', v: Infinity },
    ];
    expect(seriesValues(p).map((x) => x.v)).toEqual([1]);
    expect(countDropped(p, ['v'])).toBe(2);
  });

  it('sanitizes a malformed (null) point into a gap instead of throwing', () => {
    // Model-supplied series only pass a kind check at compose; a null point
    // must become an index-preserving gap, never a `p[key]` throw (codex P2).
    const s = readSeries({ kind: 'series', points: [{ t: 'a', v: 1 }, null, { t: 'c', v: 3 }] });
    expect(s.ok).toBe(true);
    expect(s.points[1]).toEqual({});
    const finite = seriesValues(s.points);
    expect(finite).toEqual([
      { i: 0, v: 1 },
      { i: 2, v: 3 },
    ]);
  });

  it('seriesValues/countDropped tolerate a non-object entry directly', () => {
    const p = [{ t: 'a', v: 5 }, null as unknown as SeriesPoint];
    expect(() => seriesValues(p)).not.toThrow();
    expect(seriesValues(p).map((x) => x.v)).toEqual([5]);
    expect(countDropped(p, ['v'])).toBe(1);
  });
});

describe('degenerate classification (§3.1)', () => {
  it('classifies empty / single / flat / ok', () => {
    expect(classify([])).toBe('empty');
    expect(classify([5])).toBe('single');
    expect(classify([5, 5, 5])).toBe('flat');
    expect(classify([1, 2, 3])).toBe('ok');
  });
});

describe('y-domain zero-basing (§3 — the axis must not be able to lie)', () => {
  it('forces zero into the domain when zeroBase (Bar/Area)', () => {
    const d = yDomain([40, 62, 55], true);
    expect(d.min).toBe(0);
    expect(d.max).toBeGreaterThanOrEqual(62);
    expect(d.zeroBreak).toBe(false);
  });

  it('pads and flags a break when Line/Spark excludes zero', () => {
    const d = yDomain([58, 62, 60], false);
    expect(d.min).toBeGreaterThan(0);
    expect(d.zeroBreak).toBe(true);
  });

  it('pads an all-equal series symmetrically instead of collapsing', () => {
    const d = yDomain([50, 50, 50], false);
    expect(d.min).toBeLessThan(50);
    expect(d.max).toBeGreaterThan(50);
    expect(d.min).not.toBe(d.max);
  });

  it('zero-bases all-equal bars instead of padding symmetrically (codex P2)', () => {
    // Bar/Area must include zero even when flat, or the bars render short.
    const d = yDomain([10, 10], true);
    expect(d.min).toBe(0);
    expect(d.max).toBe(10);
    const neg = yDomain([-10, -10], true);
    expect(neg.min).toBe(-10);
    expect(neg.max).toBe(0);
  });

  it('handles an empty value list without dividing by zero', () => {
    const d = yDomain([], false);
    expect(Number.isFinite(d.min)).toBe(true);
    expect(Number.isFinite(d.max)).toBe(true);
  });
});

describe('scales', () => {
  it('maps domain max to the top (smallest y) and min to the bottom', () => {
    const d = yDomain([0, 100], true);
    const y = yScale(d, 100, 10);
    expect(y(d.max)).toBeCloseTo(10, 0); // top
    expect(y(d.min)).toBeCloseTo(90, 0); // bottom
  });

  it('spreads points evenly and centres a single point', () => {
    const x = xScale(3, 100, 10);
    expect(x(0)).toBe(10);
    expect(x(2)).toBe(90);
    expect(xScale(1, 100, 10)(0)).toBe(50);
  });
});

describe('gap-aware line segments', () => {
  it('breaks the polyline at a dropped reading', () => {
    const finite = seriesValues(pts([1, null, 3, 4]));
    const segs = lineSegments(
      finite,
      (i) => i,
      (v) => v,
    );
    // [0]=1 is one segment; [2]=3,[3]=4 is another — the gap at index 1 breaks it.
    expect(segs.length).toBe(2);
    expect(segs[0]).toBe('0.0,1.0');
    expect(segs[1]).toBe('2.0,3.0 3.0,4.0');
  });

  it('is a single segment when there are no gaps', () => {
    const finite = seriesValues(pts([1, 2, 3]));
    expect(lineSegments(finite, (i) => i, (v) => v).length).toBe(1);
  });
});

describe('tick formatting + values', () => {
  it('abbreviates thousands and millions', () => {
    expect(formatTick(1500)).toBe('1.5k');
    expect(formatTick(2_000_000)).toBe('2M');
    expect(formatTick(62)).toBe('62');
    expect(formatTick(3.14159)).toBe('3.14');
  });

  it('produces evenly spaced ticks across the domain', () => {
    const t = ticks({ min: 0, max: 100, zeroBreak: false }, 3);
    expect(t).toEqual([0, 50, 100]);
  });
});

describe('F096 sparkline additions (renderer-owned)', () => {
  it('ink maps neutral to --soft while the stroke keeps the axis grey', () => {
    expect(toneInkVar('neutral')).toBe('var(--soft)');
    expect(toneInkVar('crit')).toBe('var(--crit)');
    expect(toneVar('neutral')).toBe('var(--chart-axis)');
  });

  it('trend window is renderer-owned: max(3, n/8)', () => {
    expect(trendWindow(10)).toBe(3);
    expect(trendWindow(56)).toBe(7);
    expect(trendWindow(200)).toBe(25);
  });

  it('rolling mean never bridges a gap — the window resets per run', () => {
    // [1, 3, gap, 5, 7] window 2: run A = 1, (1+3)/2; run B restarts at 5.
    const finite = seriesValues(pts([1, 3, null, 5, 7]));
    expect(rollingMean(finite, 2)).toEqual([
      { i: 0, v: 1 },
      { i: 1, v: 2 },
      { i: 3, v: 5 },
      { i: 4, v: 6 },
    ]);
    // indices survive, so lineSegments splits the mean at the same gap
    expect(lineSegments(rollingMean(finite, 2), (i) => i, (v) => v).length).toBe(2);
  });

  it('rolling mean with window 1 is the identity', () => {
    const finite = seriesValues(pts([4, 8, 6]));
    expect(rollingMean(finite, 1)).toEqual(finite);
  });

  it('focus start is the first point at or after meta.focus_from', () => {
    const points = pts([1, 2, 3, 4]);
    expect(focusStartIndex(points, '2026-08-03')).toBe(2);
    expect(focusStartIndex(points, '2026-08-02T12:00:00')).toBe(2); // between 02 and 03
    expect(focusStartIndex(points, '2027-01-01')).toBeNull();
    expect(focusStartIndex(points, null)).toBeNull();
    expect(focusStartIndex([{} as SeriesPoint, ...points], '2026-08-01')).toBe(1); // gap placeholder skipped
  });

  it('readSeries exposes meta.focus_from as focusFrom, null when absent or not a string', () => {
    const base = { kind: 'series', points: pts([1, 2]), unit: '' };
    expect(readSeries({ ...base, meta: { focus_from: '2026-08-02' } }).focusFrom).toBe('2026-08-02');
    expect(readSeries({ ...base, meta: { focus_from: 42 } }).focusFrom).toBeNull();
    expect(readSeries(base).focusFrom).toBeNull();
    expect(readSeries([1, 2]).focusFrom).toBeNull();
  });
});
