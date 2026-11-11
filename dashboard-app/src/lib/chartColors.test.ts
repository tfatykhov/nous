import { describe, it, expect, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { CHART_FONT_SIZE, TOKEN_FALLBACK, applyChartFont, cssColor, finishBars, lineLegendLabels, resolveColors, seriesToken, tint, withChartDefaults } from './chartColors';
import { ROOT_SEL, block, parseBlocks, scriptBlocks } from './testing/css';
import { sourceFiles } from './testing/sources';

// A canvas (Chart.js) or cytoscape cannot read var(): their colours are tokens (`var(--x)`, or the `tint(--x, a)`
// sentinel) that chartColors resolves against the element the chart draws into. Every other value passes through
// untouched.
const ROOT = block(parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')), ROOT_SEL, 50);

/** A box with token values set on it (jsdom has no stylesheet). */
function box(vars: Record<string, string>, parent: HTMLElement = document.body): HTMLElement {
  const el = document.createElement('div');
  for (const [k, v] of Object.entries(vars)) el.style.setProperty(k, v);
  parent.appendChild(el);
  return el;
}

afterEach(() => {
  document.body.innerHTML = '';
});

describe('TOKEN_FALLBACK', () => {
  it('equals app.css\'s :root for every token it lists (what jsdom resolves is what the page declares)', () => {
    expect(Object.keys(TOKEN_FALLBACK).length).toBeGreaterThan(30);
    for (const [k, v] of Object.entries(TOKEN_FALLBACK)) expect(v, k).toBe(ROOT[k]);
  });

  it('holds every series slot and every graph and relation colour token', () => {
    for (let i = 0; i <= 13; i++) expect(TOKEN_FALLBACK[`--series-${i}`], `--series-${i}`).toBeDefined();
    // The Graph's edge opacities are numbers, read by GraphView itself; the table holds colours only.
    const colourTokens = Object.keys(ROOT).filter((t) => (t.startsWith('--gv-') || t.startsWith('--rel-')) && !t.endsWith('-opacity') && !t.includes('-opacity-'));
    for (const k of colourTokens) expect(TOKEN_FALLBACK[k], k).toBeDefined();
  });

  // Review M4: tint() and withAlpha only parse hex and comma rgb(); any other form (space syntax, hsl, color-mix) would
  // silently lose its alpha and draw opaque. Every token a chart reads must be in one of those forms.
  const TINTABLE = /^(#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})|rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*(,\s*[\d.]+\s*)?\))$/i;

  it('the tintable form: hex or comma rgb(a); not space syntax, hsl or color-mix', () => {
    for (const ok of ['#fff', '#14202b59', 'rgb(1, 2, 3)', 'rgba(0, 0, 0, 0.3)']) expect(ok, ok).toMatch(TINTABLE);
    for (const bad of ['rgb(183 121 31 / 70%)', 'hsl(210 50% 40%)', 'color-mix(in srgb, red 50%, blue)', 'var(--x)', 'red']) {
      expect(bad, bad).not.toMatch(TINTABLE);
    }
  });

  it('every token it lists is tintable', () => {
    for (const k of Object.keys(TOKEN_FALLBACK)) expect(ROOT[k], k).toMatch(TINTABLE);
  });
});

// Review M5: a mistyped token (`var(--sereis-1)`) resolves to '' and draws in the library's default colour, silently.
// Every token a script names by a literal string must exist in app.css. PR 7: every source file's script or code
// (lib/testing/sources.ts), not the list the screens joined one PR at a time.
describe('every token a script names exists', () => {
  const code = (f: string) => {
    const src = readFileSync(join(__dirname, '..', f), 'utf-8');
    return scriptBlocks(f.endsWith('.svelte') ? src : `<script>${src}</script>`);
  };
  const scripts = sourceFiles().map((f) => [f, code(f)] as const);
  /** Token names in a script's string literals: `var(--x)`, `tint(--x, …)`/`tint('--x', …)`, and a bare `'--x'`. */
  const named = (js: string) => [...new Set([
    ...[...js.matchAll(/var\(\s*(--[a-z0-9-]+)\s*\)/gi)].map((m) => m[1]),
    ...[...js.matchAll(/tint\(\s*['"]?(--[a-z0-9-]+)/gi)].map((m) => m[1]),
    ...[...js.matchAll(/['"`](--[a-z0-9-]+)['"`]/gi)].map((m) => m[1]),
  ])];

  it('finds the names (and would catch a typo)', () => {
    expect(named("const a = 'var(--sereis-1)'; tint('--green', 0.1); c('--gv-select'); `var(--gv-${t})`")).toEqual(['--sereis-1', '--green', '--gv-select']);
    expect(scripts.length).toBeGreaterThan(100);
    expect(scripts.map(([f]) => f)).toEqual(expect.arrayContaining(['lib/overviewCharts.ts', 'views/Overview.svelte']));
  });

  it.each(scripts.map(([f]) => f))('%s', (file) => {
    const js = scripts.find(([f]) => f === file)![1];
    expect(named(js).filter((t) => !(t in ROOT))).toEqual([]);
  });
});

// Redesign PR 4 review M1: a legend that samples each line with its dash. Chart.js 4.5.1's default label generator reads
// the first point's style under usePointStyle (`getStyle(usePointStyle ? 0 : undefined)`), which has no dash, so the
// sample came out solid and 1 px (seen in the capture): the labels take the dash and a 2 px width from the dataset.
describe('lineLegendLabels', () => {
  afterEach(() => { delete (globalThis as any).Chart; });

  it('a 28 px line sample (pointStyleWidth: under usePointStyle Chart.js caps boxWidth at the font size), keeping other options', () => {
    expect(lineLegendLabels({ color: 'var(--muted)' })).toMatchObject({ color: 'var(--muted)', usePointStyle: true, pointStyle: 'line', pointStyleWidth: 28 });
  });

  it('each label takes its dataset\'s dash and colour and a 2 px line over Chart.js\'s own labels', () => {
    const base = [{ datasetIndex: 0, text: 'a', lineDash: undefined, lineWidth: 1, strokeStyle: '#000' }, { datasetIndex: 1, text: 'b', lineWidth: 1 }];
    (globalThis as any).Chart = { defaults: { plugins: { legend: { labels: { generateLabels: () => base } } } } };
    const chart = { data: { datasets: [{ borderDash: [2, 3], borderColor: '#f87171', borderWidth: 1 }, { borderColor: '#34d399', borderWidth: 3 }] } };
    const items = lineLegendLabels().generateLabels(chart);
    expect(items.map((i: any) => [i.text, i.lineDash, i.strokeStyle, i.lineWidth])).toEqual([['a', [2, 3], '#f87171', 2], ['b', [], '#34d399', 3]]);
  });
});

describe('cssColor', () => {
  it('reads the token off the element, as declared (a hex stays the hex)', () => {
    const el = box({ '--series-1': ' #1f5fa8 ' });
    expect(cssColor(el, '--series-1')).toBe('#1f5fa8');
  });

  it('turns a hex with alpha into rgba(): the build minifies rgba() tokens to #rrggbbaa, which cytoscape cannot parse', () => {
    const el = box({ '--gv-node-border': '#14202b59', '--track': '#fff1', '--gv-outline': '#fff' });
    expect(cssColor(el, '--gv-node-border')).toBe('rgba(20, 32, 43, 0.349)');
    expect(cssColor(el, '--track')).toBe('rgba(255, 255, 255, 0.067)');
    expect(cssColor(el, '--gv-outline')).toBe('#fff'); // an opaque hex stays as declared
    expect(resolveColors('tint(--gv-node-border, 0.5)', el)).toBe('rgba(20, 32, 43, 0.5)');
  });

  it('falls back to the :root value when nothing declares it (jsdom has no stylesheet), and to "" for an unknown token', () => {
    const el = box({});
    expect(cssColor(el, '--series-1')).toBe('#1f5fa8');
    expect(cssColor(null, '--gv-fact')).toBe('#159a7a');
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
    const el = box({ '--series-1': '#1f5fa8', '--green': '#2f8a4e' });
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
    expect(resolveColors(tint('--series-1', 0.12), null)).toBe('rgba(31, 95, 168, 0.12)');
    expect(resolveColors(tint('--green', 0.5), null)).toBe('rgba(47, 138, 78, 0.5)');
  });

  it('a token declared closer to the canvas wins (CSS inheritance)', () => {
    const page = box({ '--series-1': '#1f5fa8' });
    const inner = box({ '--series-1': '#60a5fa' }, page);
    const inInner = document.createElement('canvas');
    inner.appendChild(inInner);
    const onPage = document.createElement('canvas');
    page.appendChild(onPage);
    expect(resolveColors('var(--series-1)', inInner)).toBe('#60a5fa');
    expect(resolveColors('var(--series-1)', onPage)).toBe('#1f5fa8');
  });
});

describe('seriesToken', () => {
  it('cycles through the thirteen slots', () => {
    expect([0, 1, 12, 13, 25].map(seriesToken)).toEqual([
      'var(--series-1)', 'var(--series-2)', 'var(--series-13)', 'var(--series-1)', 'var(--series-13)',
    ]);
  });
});

describe('withChartDefaults', () => {
  it('gives every axis of a cartesian chart the text and grid colours, beneath the caller\'s own', () => {
    const out = withChartDefaults('bar', { indexAxis: 'y', scales: { y: { grid: { display: false } }, y2: { ticks: { color: 'red' } } } }, 'T', 'G');
    expect(out.color).toBe('T'); // the legend
    expect(out.plugins.title.color).toBe('T');
    expect(out.indexAxis).toBe('y');
    expect(out.scales.x).toEqual({ ticks: { color: 'T' }, title: { color: 'T' }, grid: { color: 'G' }, border: { color: 'G' } });
    expect(out.scales.y.grid).toEqual({ color: 'G', display: false });
    expect(out.scales.y2.ticks.color).toBe('red');
  });

  it('adds no scales to a doughnut (it would grow axes), and keeps the caller\'s plugins', () => {
    const out = withChartDefaults('doughnut', { cutout: '60%', plugins: { legend: { position: 'bottom' } } }, 'T', 'G');
    expect(out.scales).toBeUndefined();
    // The legend is dots under the caller's own settings (finish review fix 8).
    expect(out.plugins).toEqual({ title: { color: 'T' }, legend: { labels: { usePointStyle: true, pointStyle: 'circle', boxWidth: 8, boxHeight: 8 }, position: 'bottom' } });
    expect(out.cutout).toBe('60%');
  });
});

// Finish review fix 7: chart text is the UI face. Chart.js draws on a canvas, which takes no CSS font, so its global
// default font is set to the --font-ui stack at the DOM's 12px label size, and every live chart is redrawn once the
// web fonts have loaded (a canvas drawn before keeps the fallback face).
describe('applyChartFont', () => {
  it('sets the default family from --font-ui and the size, then redraws every chart when the fonts are ready', async () => {
    let ready!: () => void;
    const fonts = { ready: new Promise<void>((r) => { ready = r; }) };
    const a = { update: vi.fn() };
    const b = { update: vi.fn() };
    const C = { defaults: { font: { family: 'Helvetica', size: 10 } }, instances: { 1: a, 2: b } };
    document.documentElement.style.setProperty('--font-ui', "'Archivo', system-ui, sans-serif");
    try {
      applyChartFont(C, { fonts, documentElement: document.documentElement } as unknown as Document);
      expect(C.defaults.font).toEqual({ family: "'Archivo', system-ui, sans-serif", size: CHART_FONT_SIZE });
      expect(CHART_FONT_SIZE).toBe(12);
      expect(a.update).not.toHaveBeenCalled();
      ready();
      await fonts.ready;
      await Promise.resolve();
      expect(a.update).toHaveBeenCalledWith('none');
      expect(b.update).toHaveBeenCalledWith('none');
    } finally {
      document.documentElement.style.removeProperty('--font-ui');
    }
  });

  it('falls back to the Archivo stack when the token is not readable, and does nothing without Chart.js', () => {
    const C = { defaults: { font: {} as Record<string, unknown> }, instances: {} };
    applyChartFont(C, { documentElement: document.documentElement } as unknown as Document);
    expect(C.defaults.font.family).toBe("'Archivo', system-ui, sans-serif");
    expect(() => applyChartFont(undefined, document)).not.toThrow();
    expect(() => applyChartFont({}, document)).not.toThrow();
  });

  it('the Chart component applies it on mount (source contract)', () => {
    const src = readFileSync(join(__dirname, 'viz/Chart.svelte'), 'utf-8');
    expect(scriptBlocks(src)).toMatch(/applyChartFont\(\)/);
  });
});

// Finish review fix 8: chart furniture in the world's language. Legends are dots (a line chart keeps its dash samples,
// which carry the series), bars are rounded with no outline, and a single series is the one neutral colour.
describe('chart furniture', () => {
  it('legends are 8px dots on bar, doughnut and radar charts; a line chart keeps its own labels', () => {
    for (const type of ['bar', 'doughnut', 'radar']) {
      expect(withChartDefaults(type, {}, '#000', '#ccc').plugins.legend.labels, type).toMatchObject(
        { usePointStyle: true, pointStyle: 'circle', boxWidth: 8, boxHeight: 8 });
    }
    expect(withChartDefaults('line', {}, '#000', '#ccc').plugins.legend?.labels?.pointStyle).toBeUndefined();
  });

  it('finishBars rounds every bar dataset and removes its outline; line datasets are left alone', () => {
    const data = { datasets: [{ data: [1], borderColor: 'x', borderWidth: 1 }, { type: 'line', data: [2], borderWidth: 2 }] };
    const out = finishBars('bar', data);
    expect(out.datasets[0]).toMatchObject({ borderWidth: 0, borderRadius: 4, borderSkipped: false });
    expect(out.datasets[1]).toEqual({ type: 'line', data: [2], borderWidth: 2 });
    expect(finishBars('line', { datasets: [{ data: [1], borderWidth: 2 }] }).datasets[0]).toEqual({ data: [1], borderWidth: 2 });
    expect(finishBars('bar', { datasets: [{ data: [1], borderRadius: 2 }] }).datasets[0].borderRadius).toBe(2);
    expect(scriptBlocks(readFileSync(join(__dirname, 'viz/Chart.svelte'), 'utf-8'))).toMatch(/finishBars\(type, resolveColors\(d, canvas\)\)/);
  });

  it('a single series with no meaning in its colour is the neutral --series-0 (Node degree, Decisions per day)', () => {
    const graph = readFileSync(join(__dirname, '../views/health/GraphTab.svelte'), 'utf-8');
    expect(graph).toMatch(/label: 'Node count',\s*data: [^\n]*\n\s*backgroundColor: tint\('--series-0', 0\.5\),/);
    const dec = readFileSync(join(__dirname, '../views/Decisions.svelte'), 'utf-8');
    expect(dec).toMatch(/label: 'Decisions',\s*data: [^\n]*\n\s*backgroundColor: tint\('--series-0', 0\.5\),/);
  });
});
