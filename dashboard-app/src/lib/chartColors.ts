// Chart colours by token. Chart.js draws on a canvas and cytoscape on its own canvas: neither can read var(). A chart's
// colours are therefore written as tokens, `var(--x)` or the `tint(--x, a)` sentinel, and resolved here against the
// element the chart draws into, so a dark island on a paper page resolves dark. Every other value (a literal colour,
// a number, a function) passes through untouched, which keeps the charts of the unconverted screens byte for byte.
// Chart.svelte resolves its data and options here, and again when the look changes; lib/viz/Graph.svelte does the
// same for its nodes and edges. DOM and SVG colours use var() and color-mix() directly and never come here.
import { readToken, withAlpha } from './overviewCharts';
import { getLook } from './look';

/**
 * The dark value of every token a chart reads through this module: what jsdom (no stylesheet) resolves to. The only
 * table of colour literals in the app's scripts; chartColors.test.ts proves each equals app.css's dark block.
 */
export const DARK_FALLBACK: Record<string, string> = {
  '--accent': '#7c6af7',
  '--surface': '#12121a',
  '--text': '#e2e2f0',
  '--muted': '#8e8eab',
  '--green': '#34d399',
  '--yellow': '#fbbf24',
  '--red': '#f87171',
  '--pending': '#22d3ee',
  '--series-1': '#60a5fa',
  '--series-2': '#34d399',
  '--series-3': '#a78bfa',
  '--series-4': '#fb923c',
  '--series-5': '#f87171',
  '--series-6': '#fbbf24',
  '--series-7': '#6b6b8a',
  '--series-8': '#e2e2f0',
  '--series-9': '#22d3ee',
  '--series-10': '#e879f9',
  '--series-11': '#f472b6',
  '--series-12': '#9ca3af',
  '--series-13': '#c4c4d8',
  '--gv-fact': '#34d399',
  '--gv-episode': '#60a5fa',
  '--gv-decision': '#a78bfa',
  '--gv-procedure': '#fbbf24',
  '--gv-chunk': '#06b6d4',
  '--gv-other': '#6b6b8a',
  '--gv-outline': '#0a0a0f',
  '--gv-node-border': 'rgba(0, 0, 0, 0.3)',
  '--gv-select': '#fbbf24',
  '--rel-related-to': '#7c6af7',
  '--rel-extracted-from': '#60a5fa',
  '--rel-supports': '#34d399',
  '--rel-informed-by': '#a78bfa',
  '--rel-evidence-for': '#fbbf24',
  '--rel-contradicts': '#f87171',
  '--rel-supersedes': '#fb923c',
  '--rel-caused-by': '#e2e2f0',
  '--rel-discussed-in': '#6b6b8a',
  '--rel-part-of': '#22d3ee',
  '--rel-summarized-by': '#06b6d4',
  '--track': 'rgba(255, 255, 255, 0.06)',
  '--track-strong': 'rgba(255, 255, 255, 0.08)',
  '--chart-text': '#94a3b8',
  '--chart-grid': '#1e293b',
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

/** A token's colour on `el` as declared (an opaque hex stays the hex; a hex with alpha becomes rgba()), inherited from
 * its ground; the dark value when nothing declares it; '' for an unknown token. */
export function cssColor(el: Element | null, token: string): string {
  const declared = el && el.isConnected ? readToken(el, token) : '';
  return alphaHexToRgba(declared || DARK_FALLBACK[token] || '');
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

/** The element draws on a paper ground: the paper look is on and its closest ground is paper (not a dark island). */
export function isPaperGround(el: Element | null): boolean {
  return getLook() === 'paper' && el?.closest('[data-ground]')?.getAttribute('data-ground') === 'paper';
}

/** `var(--series-N)` for the i-th series, cycling through the thirteen slots. */
export function seriesToken(i: number): string {
  return `var(--series-${(((i % 13) + 13) % 13) + 1})`;
}

const CARTESIAN = new Set(['bar', 'line', 'scatter', 'bubble']);
const RADIAL = new Set(['radar', 'polarArea']);

function mergeUnder(base: Record<string, any>, over: Record<string, any>): Record<string, any> {
  const out: Record<string, any> = { ...base };
  for (const [k, v] of Object.entries(over)) out[k] = isPlainObject(v) && isPlainObject(out[k]) ? mergeUnder(out[k], v) : v;
  return out;
}

/**
 * A paper ground's chart text and grid colours beneath the caller's options (the caller's own values win). Chart.js 4
 * reads a chart's root `color` for the legend only, so every axis gets its own: x and y on a cartesian chart, r on a
 * radial one, plus any axis the caller defines. A doughnut gets no `scales` (it would grow axes).
 */
export function withPaperDefaults(type: string, options: Record<string, any>, text: string, grid: string): Record<string, any> {
  const ids = new Set(Object.keys(options.scales ?? {}));
  if (CARTESIAN.has(type)) ids.add('x').add('y');
  if (RADIAL.has(type)) ids.add('r');
  const axis = { ticks: { color: text }, title: { color: text }, grid: { color: grid }, border: { color: grid } };
  const radial = { ...axis, angleLines: { color: grid }, pointLabels: { color: text } };
  const base: Record<string, any> = { color: text, plugins: { title: { color: text } } };
  if (ids.size) base.scales = Object.fromEntries([...ids].map((id) => [id, id === 'r' ? radial : axis]));
  return mergeUnder(base, options);
}
