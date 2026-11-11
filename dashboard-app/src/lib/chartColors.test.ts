import { describe, it, expect, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { DARK_FALLBACK, cssColor, isPaperGround, resolveColors, seriesToken, tint, withPaperDefaults } from './chartColors';
import { DARK_SEL, PAPER_SEL, parseBlocks, scriptBlocks } from './testing/css';

// A canvas (Chart.js) or cytoscape cannot read var(): their colours are tokens (`var(--x)`, or the `tint(--x, a)`
// sentinel) that chartColors resolves against the element the chart draws into, so a dark island on a paper page
// resolves dark. Every other value passes through untouched, which keeps every unconverted chart byte for byte.
const DARK = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')).get(DARK_SEL) ?? {};

function ground(kind: 'paper' | 'dark', vars: Record<string, string>, parent: HTMLElement = document.body): HTMLElement {
  const el = document.createElement('div');
  el.setAttribute('data-ground', kind);
  for (const [k, v] of Object.entries(vars)) el.style.setProperty(k, v);
  parent.appendChild(el);
  return el;
}

afterEach(() => {
  document.body.innerHTML = '';
  document.documentElement.removeAttribute('data-look');
});

describe('DARK_FALLBACK', () => {
  it('equals app.css\'s dark block for every token it lists (the dark parity proof)', () => {
    expect(Object.keys(DARK_FALLBACK).length).toBeGreaterThan(30);
    for (const [k, v] of Object.entries(DARK_FALLBACK)) expect(v, k).toBe(DARK[k]);
  });

  it('holds every series slot and every graph and relation colour token', () => {
    for (let i = 0; i <= 13; i++) expect(DARK_FALLBACK[`--series-${i}`], `--series-${i}`).toBeDefined();
    // The Graph's edge opacities are numbers, read by GraphView itself; the table holds colours only.
    const colourTokens = Object.keys(DARK).filter((t) => (t.startsWith('--gv-') || t.startsWith('--rel-')) && !t.endsWith('-opacity') && !t.includes('-opacity-'));
    for (const k of colourTokens) expect(DARK_FALLBACK[k], k).toBeDefined();
  });

  // Review M4: tint() and withAlpha only parse hex and comma rgb(); any other form (space syntax, hsl, color-mix) would
  // silently lose its alpha and draw opaque. Every token a chart reads must be in one of those forms on both grounds.
  const PAPER = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')).get(PAPER_SEL) ?? {};
  const TINTABLE = /^(#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})|rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*(,\s*[\d.]+\s*)?\))$/i;

  it('the tintable form: hex or comma rgb(a); not space syntax, hsl or color-mix', () => {
    for (const ok of ['#fff', '#14202b59', 'rgb(1, 2, 3)', 'rgba(0, 0, 0, 0.3)']) expect(ok, ok).toMatch(TINTABLE);
    for (const bad of ['rgb(183 121 31 / 70%)', 'hsl(210 50% 40%)', 'color-mix(in srgb, red 50%, blue)', 'var(--x)', 'red']) {
      expect(bad, bad).not.toMatch(TINTABLE);
    }
  });

  it('every token it lists is tintable on both grounds (the paper value, or the dark one where paper inherits it)', () => {
    for (const k of Object.keys(DARK_FALLBACK)) {
      expect(DARK[k], `${k} dark`).toMatch(TINTABLE);
      expect(PAPER[k] ?? DARK[k], `${k} paper`).toMatch(TINTABLE);
    }
  });
});

// Review M5: a mistyped token (`var(--sereis-1)`) resolves to '' and draws in the library's default colour, silently.
// Every token a migrated script names by a literal string must exist in app.css.
describe('every token a migrated script names exists', () => {
  const guard = readFileSync(join(__dirname, '..', 'hexGuard.test.ts'), 'utf-8');
  const files = [...(guard.match(/export const SCRIPT_GUARDED[^=]*=\s*\[([\s\S]*?)\];/)?.[1] ?? '').matchAll(/'([^']+)'/g)].map((m) => m[1]);
  const scripts = [...files.map((f) => [f, scriptBlocks(readFileSync(join(__dirname, '..', f), 'utf-8'))] as const),
    ['lib/overviewCharts.ts', readFileSync(join(__dirname, 'overviewCharts.ts'), 'utf-8')] as const];
  /** Token names in a script's string literals: `var(--x)`, `tint(--x, …)`/`tint('--x', …)`, and a bare `'--x'`. */
  const named = (js: string) => [...new Set([
    ...[...js.matchAll(/var\(\s*(--[a-z0-9-]+)\s*\)/gi)].map((m) => m[1]),
    ...[...js.matchAll(/tint\(\s*['"]?(--[a-z0-9-]+)/gi)].map((m) => m[1]),
    ...[...js.matchAll(/['"`](--[a-z0-9-]+)['"`]/gi)].map((m) => m[1]),
  ])];

  it('finds the names (and would catch a typo)', () => {
    expect(named("const a = 'var(--sereis-1)'; tint('--green', 0.1); c('--gv-select'); `var(--gv-${t})`")).toEqual(['--sereis-1', '--green', '--gv-select']);
    expect(files.length).toBeGreaterThan(20);
  });

  it.each(scripts.map(([f]) => f))('%s', (file) => {
    const js = scripts.find(([f]) => f === file)![1];
    expect(named(js).filter((t) => !(t in DARK))).toEqual([]);
  });
});

describe('cssColor', () => {
  it('reads the token off the element, as declared (a hex stays the hex)', () => {
    const el = ground('paper', { '--series-1': ' #1f5fa8 ' });
    expect(cssColor(el, '--series-1')).toBe('#1f5fa8');
  });

  it('turns a hex with alpha into rgba(): the build minifies rgba() tokens to #rrggbbaa, which cytoscape cannot parse', () => {
    const el = ground('paper', { '--gv-node-border': '#14202b59', '--track': '#fff1', '--gv-outline': '#fff' });
    expect(cssColor(el, '--gv-node-border')).toBe('rgba(20, 32, 43, 0.349)');
    expect(cssColor(el, '--track')).toBe('rgba(255, 255, 255, 0.067)');
    expect(cssColor(el, '--gv-outline')).toBe('#fff'); // an opaque hex stays as declared
    expect(resolveColors('tint(--gv-node-border, 0.5)', el)).toBe('rgba(20, 32, 43, 0.5)');
  });

  it('falls back to the dark value when nothing declares it (jsdom has no stylesheet), and to "" for an unknown token', () => {
    const el = ground('dark', {});
    expect(cssColor(el, '--series-1')).toBe('#60a5fa');
    expect(cssColor(null, '--gv-fact')).toBe('#34d399');
    expect(() => cssColor(el, '--no-such-token')).not.toThrow();
    expect(cssColor(el, '--no-such-token')).toBe('');
  });
});

describe('resolveColors', () => {
  it('leaves literals, numbers, null and functions alone, and never mutates its input', () => {
    const fn = (v: number) => `${v}%`;
    const input = Object.freeze({
      a: '#34d399',
      b: 'rgba(1,2,3,.1)',
      n: 3,
      z: null,
      u: undefined,
      list: Object.freeze(['#fff', 1, null]),
      ticks: Object.freeze({ callback: fn }),
      label: 'var(--series-1) is not a whole-string token',
    });
    const out = resolveColors(input, null);
    expect(out).toEqual(input);
    expect(out).not.toBe(input);
    expect(out.ticks.callback).toBe(fn);
    expect(out.a).toBe('#34d399');
  });

  it('replaces a whole-string var() and tint() token with the colour, in arrays and nested objects', () => {
    const el = ground('paper', { '--series-1': '#1f5fa8', '--green': '#2f8a4e' });
    const out = resolveColors(
      { datasets: [{ borderColor: 'var(--series-1)', backgroundColor: tint('--series-1', 0.12), pointBackgroundColor: ['var(--green)', '#123456'] }] },
      el,
    );
    expect(out.datasets[0]).toEqual({
      borderColor: '#1f5fa8',
      backgroundColor: 'rgba(31, 95, 168, 0.12)',
      pointBackgroundColor: ['#2f8a4e', '#123456'],
    });
  });

  it('tint() is a sentinel string; resolved, it is the token at that alpha', () => {
    expect(tint('--series-1', 0.12)).toBe('tint(--series-1, 0.12)');
    expect(resolveColors(tint('--series-1', 0.12), null)).toBe('rgba(96, 165, 250, 0.12)');
    expect(resolveColors(tint('--green', 0.5), null)).toBe('rgba(52, 211, 153, 0.5)');
  });

  it('a dark island inside a paper page resolves the island\'s value (the closest ground wins)', () => {
    document.documentElement.setAttribute('data-look', 'paper');
    const page = ground('paper', { '--series-1': '#1f5fa8' });
    const island = ground('dark', { '--series-1': '#60a5fa' }, page);
    const inIsland = document.createElement('canvas');
    island.appendChild(inIsland);
    const onPage = document.createElement('canvas');
    page.appendChild(onPage);
    expect(resolveColors('var(--series-1)', inIsland)).toBe('#60a5fa');
    expect(resolveColors('var(--series-1)', onPage)).toBe('#1f5fa8');
    expect(isPaperGround(onPage)).toBe(true);
    expect(isPaperGround(inIsland)).toBe(false);
  });
});

describe('isPaperGround', () => {
  it('only under the paper look, inside a paper ground', () => {
    const page = ground('paper', {});
    expect(isPaperGround(page)).toBe(false); // the look is off
    document.documentElement.setAttribute('data-look', 'paper');
    expect(isPaperGround(page)).toBe(true);
    expect(isPaperGround(document.body)).toBe(false); // no ground at all
    expect(isPaperGround(null)).toBe(false);
  });
});

describe('seriesToken', () => {
  it('cycles through the thirteen slots', () => {
    expect([0, 1, 12, 13, 25].map(seriesToken)).toEqual([
      'var(--series-1)', 'var(--series-2)', 'var(--series-13)', 'var(--series-1)', 'var(--series-13)',
    ]);
  });
});

describe('withPaperDefaults', () => {
  it('gives every axis of a cartesian chart the text and grid colours, beneath the caller\'s own', () => {
    const out = withPaperDefaults('bar', { indexAxis: 'y', scales: { y: { grid: { display: false } }, y2: { ticks: { color: 'red' } } } }, 'T', 'G');
    expect(out.color).toBe('T'); // the legend
    expect(out.plugins.title.color).toBe('T');
    expect(out.indexAxis).toBe('y');
    expect(out.scales.x).toEqual({ ticks: { color: 'T' }, title: { color: 'T' }, grid: { color: 'G' }, border: { color: 'G' } });
    expect(out.scales.y.grid).toEqual({ color: 'G', display: false });
    expect(out.scales.y2.ticks.color).toBe('red');
  });

  it('adds no scales to a doughnut (it would grow axes), and keeps the caller\'s plugins', () => {
    const out = withPaperDefaults('doughnut', { cutout: '60%', plugins: { legend: { position: 'bottom' } } }, 'T', 'G');
    expect(out.scales).toBeUndefined();
    expect(out.plugins).toEqual({ title: { color: 'T' }, legend: { position: 'bottom' } });
    expect(out.cutout).toBe('60%');
  });
});
