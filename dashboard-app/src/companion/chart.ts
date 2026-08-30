// F094: pure chart geometry — no DOM, no Svelte, unit-tested directly.
//
// The renderer owns every geometry and scale decision here; the model
// reaches none of it (F094 §2). Adapters call these to turn a resolved
// series into SVG coordinates.

export interface SeriesPoint {
  t: string;
  // Single-series sources carry `v`; multi-series carry one numeric key
  // per series (see seriesValues). Index signature keeps both shapes.
  v?: number;
  [key: string]: unknown;
}

export interface ReadSeries {
  /** True when the resolved value is a valid {kind:series} object. */
  ok: boolean;
  points: SeriesPoint[];
  unit: string;
  /** Explicit empty-series reason (missing db, drifted schema — F094 R5). */
  reason?: string;
  downsampledFrom: number | null;
  /** When ok=false, a short note on what the path actually resolved to, so
   * the adapter can render a defensive state rather than throw. The
   * compose-time series-shape rule normally prevents this, but the renderer
   * never trusts its input. */
  shape?: string;
}

/** Normalize a resolved data-model value into a series for the adapters.
 * Never throws — a non-series value yields ok=false with a shape note. */
export function readSeries(value: unknown): ReadSeries {
  if (
    typeof value === 'object' &&
    value !== null &&
    (value as { kind?: unknown }).kind === 'series' &&
    Array.isArray((value as { points?: unknown }).points)
  ) {
    const v = value as {
      points: SeriesPoint[];
      unit?: unknown;
      meta?: { reason?: unknown; downsampled_from?: unknown };
    };
    return {
      ok: true,
      points: v.points,
      unit: typeof v.unit === 'string' ? v.unit : '',
      reason: typeof v.meta?.reason === 'string' ? v.meta.reason : undefined,
      downsampledFrom:
        typeof v.meta?.downsampled_from === 'number' ? v.meta.downsampled_from : null,
    };
  }
  const shape = Array.isArray(value)
    ? 'array'
    : value === null || value === undefined
      ? 'nothing'
      : typeof value;
  return { ok: false, points: [], unit: '', downsampledFrom: null, shape };
}

export type Tone = 'neutral' | 'ok' | 'warn' | 'crit';
const TONES: readonly Tone[] = ['neutral', 'ok', 'warn', 'crit'];

export function normalizeTone(raw: unknown): Tone {
  return typeof raw === 'string' && (TONES as readonly string[]).includes(raw)
    ? (raw as Tone)
    : 'neutral';
}

/** A tone → CSS custom property. neutral has no semantic token, so it maps
 * to the axis grey (a series that means nothing gets a neutral hue). */
export function toneVar(tone: Tone): string {
  return tone === 'neutral' ? 'var(--chart-axis)' : `var(--${tone})`;
}

/** Nth series colour from the renderer-owned ramp (1-based clamp to 4). */
export function seriesVar(index: number): string {
  const n = Math.min(4, Math.max(1, index + 1));
  return `var(--series-${n})`;
}

export function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

/** Extract the numeric value at `key` (default 'v') from each point, keeping
 * the original index so gaps (non-finite → dropped) become line breaks
 * rather than shifting every later point left. */
export function seriesValues(
  points: SeriesPoint[],
  key = 'v',
): { i: number; v: number }[] {
  const out: { i: number; v: number }[] = [];
  points.forEach((p, i) => {
    const raw = p[key];
    if (isFiniteNumber(raw)) out.push({ i, v: raw });
  });
  return out;
}

export function countDropped(points: SeriesPoint[], keys: string[]): number {
  let dropped = 0;
  for (const p of points) {
    if (keys.every((k) => !isFiniteNumber(p[k]))) dropped += 1;
  }
  return dropped;
}

export type Degenerate = 'empty' | 'single' | 'flat' | 'ok';

/** Classify a series for the §3.1 renderer-owned states. `values` is the
 * finite value list (already gap-filtered). */
export function classify(values: number[]): Degenerate {
  if (values.length === 0) return 'empty';
  if (values.length === 1) return 'single';
  const first = values[0];
  return values.every((v) => v === first) ? 'flat' : 'ok';
}

export interface Domain {
  min: number;
  max: number;
  /** True when the domain deliberately excludes zero — the caller draws a
   * `~` break marker (F094 §3: a truncated axis is how a chart lies). */
  zeroBreak: boolean;
}

/** Y-domain for a value list. `zeroBase` forces zero into the domain
 * (mandatory for Bar/Area — §3); Line/Spark pad around the data and flag a
 * break when zero is excluded. All-equal pads symmetrically so a flat line
 * sits mid-height rather than collapsing. */
export function yDomain(values: number[], zeroBase: boolean): Domain {
  if (values.length === 0) return { min: 0, max: 1, zeroBreak: false };
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) {
    // all-equal: pad symmetrically around the value
    const pad = Math.abs(min) > 0 ? Math.abs(min) * 0.1 : 1;
    return { min: min - pad, max: max + pad, zeroBreak: false };
  }
  if (zeroBase) {
    min = Math.min(0, min);
    max = Math.max(0, max);
    return { min, max, zeroBreak: false };
  }
  const span = max - min;
  const pad = span * 0.08;
  const paddedMin = min - pad;
  const paddedMax = max + pad;
  // A break is honest only when the padded domain still excludes zero.
  const zeroBreak = paddedMin > 0 || paddedMax < 0;
  return { min: paddedMin, max: paddedMax, zeroBreak };
}

/** Map a value in [domain.min,domain.max] to a Y pixel in [0,height]
 * (SVG y grows downward, so max is at the top / y=pad). */
export function yScale(domain: Domain, height: number, pad: number) {
  const span = domain.max - domain.min || 1;
  const usable = height - 2 * pad;
  return (v: number) => pad + usable * (1 - (v - domain.min) / span);
}

/** Even X positions for N points across [pad, width-pad]. */
export function xScale(count: number, width: number, pad: number) {
  const usable = width - 2 * pad;
  return (i: number) => (count <= 1 ? width / 2 : pad + (usable * i) / (count - 1));
}

/** Build gap-aware polyline segments: each run of consecutive finite points
 * is its own segment, so a dropped reading breaks the line (never bridged —
 * F094 §3.1 / the health dashboard's "gaps are real"). */
export function lineSegments(
  finite: { i: number; v: number }[],
  x: (i: number) => number,
  y: (v: number) => number,
): string[] {
  const segments: string[] = [];
  let current: string[] = [];
  let prevIndex = -2;
  for (const { i, v } of finite) {
    if (i !== prevIndex + 1 && current.length) {
      segments.push(current.join(' '));
      current = [];
    }
    current.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    prevIndex = i;
  }
  if (current.length) segments.push(current.join(' '));
  return segments;
}

/** Compact numeric tick label (renderer-owned format — model never sets it). */
export function formatTick(v: number): string {
  const abs = Math.abs(v);
  if (abs >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (abs >= 1e3) return (v / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
  if (abs >= 100 || Number.isInteger(v)) return String(Math.round(v));
  return v.toFixed(abs >= 10 ? 1 : 2);
}

/** A few evenly-spaced tick values across a domain. */
export function ticks(domain: Domain, count = 3): number[] {
  const out: number[] = [];
  const span = domain.max - domain.min;
  for (let k = 0; k < count; k++) {
    out.push(domain.min + (span * k) / (count - 1));
  }
  return out;
}
