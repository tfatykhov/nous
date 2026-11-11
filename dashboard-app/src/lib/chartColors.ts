// Chart colours by token. Chart.js draws on a canvas and cytoscape on its own canvas: neither can read var(). A chart's
// colours are therefore written as tokens, `var(--x)` or the `tint(--x, a)` sentinel, and resolved here against the
// element the chart draws into. Every other value (a literal colour, a number, a function) passes through untouched.
// Chart.svelte resolves its data and options here; lib/viz/Graph.svelte does the same for its nodes and edges. DOM and
// SVG colours use var() and color-mix() directly and never come here.
import { readToken, withAlpha } from './overviewCharts';

/**
 * The :root value of every token a chart reads through this module: what jsdom (no stylesheet) resolves to. The only
 * table of colour literals in the app's scripts; chartColors.test.ts proves each equals app.css's :root.
 */
export const TOKEN_FALLBACK: Record<string, string> = {
  '--accent': '#2346c8',
  '--surface': '#ffffff',
  '--text': '#14202b',
  '--muted': '#56687a',
  '--green': '#2f8a4e',
  '--yellow': '#b7791f',
  '--red': '#d2352b',
  '--pending': '#1b6a9c',
  '--series-0': '#274455',
  '--series-1': '#1f5fa8',
  '--series-2': '#159a7a',
  '--series-3': '#a8408f',
  '--series-4': '#d0620d',
  '--series-5': '#7a8a1a',
  '--series-6': '#985c50',
  '--series-7': '#4f5f6f',
  '--series-8': '#1c2f55',
  '--series-9': '#0e7490',
  '--series-10': '#8b5cf6',
  '--series-11': '#6b7280',
  '--series-12': '#908870',
  '--series-13': '#7a3f3f',
  '--gv-fact': '#159a7a',
  '--gv-episode': '#1f5fa8',
  '--gv-decision': '#802c66',
  '--gv-procedure': '#d0620d',
  '--gv-chunk': '#8b5cf6',
  '--gv-other': '#4f5f6f',
  '--gv-outline': '#ffffff',
  '--gv-node-border': 'rgba(20, 32, 43, 0.35)',
  '--gv-select': '#14202b',
  '--rel-related-to': '#4f5f6f',
  '--rel-extracted-from': '#1f5fa8',
  '--rel-supports': '#159a7a',
  '--rel-informed-by': '#a8408f',
  '--rel-evidence-for': '#d0620d',
  '--rel-contradicts': '#985c50',
  '--rel-supersedes': '#7a8a1a',
  '--rel-caused-by': '#1c2f55',
  '--rel-discussed-in': '#6b7280',
  '--rel-part-of': '#0e7490',
  '--rel-summarized-by': '#8b5cf6',
  '--track': '#e3e9ec',
  '--chart-text': '#56687a',
  '--chart-grid': '#e3e9ec',
  '--hit-good': '#23703d',
  '--cache-blue': '#1f5fa8',
  '--cache-grey': '#4f5f6f',
  '--stack-bar': '#b7791f',
  '--st-success': '#23703d',
  '--st-ledger-blocked': '#8a5a0e',
  '--st-neutral': '#55616c',
  '--oc-failed': '#b02a22',
  '--oc-timeout': '#881c34',
  '--oc-errored': '#641400',
  '--oc-unknown': '#7a8794',
  '--oc-blocked': '#b7791f',
  '--err-text': '#b02a22',
  '--warn-text': '#8a5a0e', // Heartbeat names these two beside its charts (tokenUse.test.ts)
  '--st-completed': '#23703d',
  '--border': '#e3e9ec', // Scoring's radar grid, Guards' grid: explicit, so it beats the chart-grid default
  '--signal-self-corrected': '#b87ab3',
  '--oc-success': '#3f8772',
};

/** `#rgba` / `#rrggbbaa` as `rgba(r, g, b, a)`; anything else unchanged. The production build minifies an rgba() token
 * to the 8-digit hex form, which cytoscape does not parse (it falls back to black) and withAlpha cannot tint. */
function alphaHexToRgba(c: string): string {
  const m = /^#([0-9a-f]{4}|[0-9a-f]{8})$/i.exec(c);
  if (!m) return c;
  const h = m[1].length === 4 ? [...m[1]].map((x) => x + x).join('') : m[1];
  const [r, g, b, a] = [0, 2, 4, 6].map((i) => parseInt(h.slice(i, i + 2), 16));
  return `rgba(${r}, ${g}, ${b}, ${Math.round((a / 255) * 1000) / 1000})`;
}

/** A token's colour on `el` as declared (an opaque hex stays the hex; a hex with alpha becomes rgba()); the :root value
 * when nothing declares it (jsdom); '' for an unknown token. */
export function cssColor(el: Element | null, token: string): string {
  const declared = el && el.isConnected ? readToken(el, token) : '';
  return alphaHexToRgba(declared || TOKEN_FALLBACK[token] || '');
}

/** The sentinel for a token at an alpha, `tint(--series-1, 0.12)`; resolveColors turns it into `rgba(…, 0.12)`. */
export function tint(token: string, alpha: number): string {
  return `tint(${token}, ${alpha})`;
}

const VAR = /^var\(\s*(--[\w-]+)\s*\)$/;
const TINT = /^tint\(\s*(--[\w-]+)\s*,\s*([\d.]+)\s*\)$/;

function isPlainObject(v: unknown): v is Record<string, unknown> {
  if (v === null || typeof v !== 'object') return false;
  const proto = Object.getPrototypeOf(v);
  return proto === Object.prototype || proto === null;
}

/**
 * A copy of `value` with every whole-string `var(--x)` and `tint(--x, a)` replaced by its colour on `el`. Only arrays
 * and plain objects are copied; every other value (a literal colour, a number, a function, a gradient) passes through
 * as is, and the input is never changed.
 */
export function resolveColors<T>(value: T, el: Element | null): T {
  const seen = new Map<string, string>();
  const read = (token: string) => {
    if (!seen.has(token)) seen.set(token, cssColor(el, token));
    return seen.get(token)!;
  };
  const walk = (v: unknown): unknown => {
    if (typeof v === 'string') {
      const t = TINT.exec(v);
      if (t) return withAlpha(read(t[1]), Number(t[2]));
      const m = VAR.exec(v);
      return m ? read(m[1]) : v;
    }
    if (Array.isArray(v)) return v.map(walk);
    if (isPlainObject(v)) return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, walk(x)]));
    return v;
  };
  return walk(value) as T;
}

/** `var(--series-N)` for the i-th series, cycling through the thirteen slots. */
export function seriesToken(i: number): string {
  return `var(--series-${(((i % 13) + 13) % 13) + 1})`;
}

/**
 * Legend labels that sample each line with its own dash, 28 px wide (a dashed box outline turns four corners). Chart.js
 * 4.5.1 reads the first point's style for a label under usePointStyle, which has no dash and a 1 px border, so the
 * label takes its dataset's dash, colour and width (2 px at least). `extra` is merged in (a label colour).
 */
export function lineLegendLabels(extra: Record<string, unknown> = {}): Record<string, any> {
  return {
    ...extra,
    usePointStyle: true,
    pointStyle: 'line',
    pointStyleWidth: 28,
    generateLabels: (chart: any) =>
      (globalThis as any).Chart.defaults.plugins.legend.labels.generateLabels(chart).map((item: any) => {
        const ds = chart.data.datasets[item.datasetIndex] ?? {};
        return { ...item, lineDash: ds.borderDash ?? [], strokeStyle: ds.borderColor ?? item.strokeStyle, lineWidth: Math.max(2, ds.borderWidth ?? 0) };
      }),
  };
}

const CARTESIAN = new Set(['bar', 'line', 'scatter', 'bubble']);
const RADIAL = new Set(['radar', 'polarArea']);

function mergeUnder(base: Record<string, any>, over: Record<string, any>): Record<string, any> {
  const out: Record<string, any> = { ...base };
  for (const [k, v] of Object.entries(over)) out[k] = isPlainObject(v) && isPlainObject(out[k]) ? mergeUnder(out[k], v) : v;
  return out;
}

/**
 * The chart text and grid colours beneath the caller's options (the caller's own values win). Chart.js 4 reads a
 * chart's root `color` for the legend only, so every axis gets its own: x and y on a cartesian chart, r on a radial
 * one, plus any axis the caller defines. A doughnut gets no `scales` (it would grow axes).
 */
export function withChartDefaults(type: string, options: Record<string, any>, text: string, grid: string): Record<string, any> {
  const ids = new Set(Object.keys(options.scales ?? {}));
  if (CARTESIAN.has(type)) ids.add('x').add('y');
  if (RADIAL.has(type)) ids.add('r');
  const axis = { ticks: { color: text }, title: { color: text }, grid: { color: grid }, border: { color: grid } };
  const radial = { ...axis, angleLines: { color: grid }, pointLabels: { color: text } };
  const base: Record<string, any> = { color: text, plugins: { title: { color: text } } };
  // Legends are 8px dots, as the authored legends are (finish review fix 8); a line chart keeps its own labels (its
  // dash samples carry the series).
  if (type !== 'line') base.plugins.legend = { labels: { usePointStyle: true, pointStyle: 'circle', boxWidth: 8, boxHeight: 8 } };
  if (ids.size) base.scales = Object.fromEntries([...ids].map((id) => [id, id === 'r' ? radial : axis]));
  return mergeUnder(base, options);
}

/** Bars are rounded with no outline (finish review fix 8: smooth and rounded, no square stock bars): every bar dataset
 * (by its own type or the chart's) takes borderWidth 0 and a 4px radius unless it sets one. Other datasets are left. */
export function finishBars(type: string, data: Record<string, any>): Record<string, any> {
  if (!Array.isArray(data?.datasets)) return data;
  return {
    ...data,
    datasets: data.datasets.map((ds: Record<string, any>) =>
      (ds.type ?? type) === 'bar' ? { ...ds, borderWidth: 0, borderRadius: ds.borderRadius ?? 4, borderSkipped: false } : ds),
  };
}

/** The chart text size: the DOM's 12px label size (finish review fix 7). */
export const CHART_FONT_SIZE = 12;
const UI_FACE = "'Archivo', system-ui, sans-serif";
let redrawQueued = false;

/**
 * Chart.js draws on a canvas, which takes no CSS font: its global default font becomes the UI face (--font-ui, the
 * Archivo stack) at 12px. A canvas drawn before the web fonts load keeps the fallback face, so every live chart is
 * redrawn once `document.fonts.ready` resolves (once per page). Without Chart.js (a test stub) it does nothing.
 */
export function applyChartFont(C: any = (globalThis as any).Chart, doc: Document = document): void {
  if (!C?.defaults?.font) return;
  const family = getComputedStyle(doc.documentElement).getPropertyValue('--font-ui').trim() || UI_FACE;
  C.defaults.font.family = family;
  C.defaults.font.size = CHART_FONT_SIZE;
  const ready = (doc as any).fonts?.ready as Promise<unknown> | undefined;
  if (!ready || (redrawQueued && C === (globalThis as any).Chart)) return;
  if (C === (globalThis as any).Chart) redrawQueued = true;
  void ready.then(() => {
    for (const chart of Object.values(C.instances ?? {})) (chart as any)?.update?.('none');
  });
}
