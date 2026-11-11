import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Rubric from './Rubric.svelte';
import { setLook } from '../lib/look';
import { PAPER_SEL, parseBlocks, styleBlocks } from '../lib/testing/css';

// Redesign PR 4: Scoring (the rubric) on paper. Chart colours are tokens that Chart.svelte resolves against the ground
// (today's colours under dark); the heatmap's cells mix a token with transparent; no arrow glyph in a heading.

const PAPER = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')).get(PAPER_SEL) ?? {};
const OLD = ['#7c6af7', '#34d399', '#60a5fa', '#fb923c', '#f87171', '#a78bfa', '#fbbf24', '#6b6b8a', '#111118', '#1e1e2e'];
const DIM_OLD = ['#7c6af7', '#34d399', '#60a5fa', '#fb923c', '#f87171', '#a78bfa', '#fbbf24'];
const DIM_TOKENS = ['--series-0', '--series-2', '--series-1', '--series-4', '--series-5', '--series-3', '--series-6', '--series-8', '--series-9'];

const dims = (n: number) => Array.from({ length: n }, (_, i) => `dim_${i + 1}`);
const DATA = (n = 7) => ({
  active_rubric: {
    version: '3', status: 'active', dimension_count: 3, created_at: '2026-11-01T10:00:00+00:00',
    dimensions: [
      { name: 'accuracy', weight: 0.4, description: 'Is it right', min_weight: 0.1, max_weight: 0.4 },
      { name: 'clarity', weight: 0.3, description: 'Is it clear', min_weight: 0.1, max_weight: 0.4 },
      { name: 'speed', weight: 0.3, description: 'Is it quick', min_weight: 0.1, max_weight: 0.4 },
    ],
  },
  version_history: [
    { version: '3', status: 'active', change_reason: 'more weight on accuracy', dimension_count: 3, created_at: '2026-11-01T10:00:00+00:00' },
    { version: '2', status: 'rollback', change_reason: null, dimension_count: 3, created_at: '2026-10-20T10:00:00+00:00' },
    { version: '1', status: 'superseded', change_reason: 'seed', dimension_count: 3, created_at: '2026-10-01T10:00:00+00:00' },
  ],
  outcome_signals: {
    total: 15,
    by_type: { completed: 5, praised: 3, corrected: 2, reworked: 2, self_corrected: 2, mystery: 1 },
    recent: [
      { signal_type: 'completed', confidence: 0.9, evidence: 'done', created_at: '2026-11-02T10:00:00+00:00' },
      { signal_type: 'corrected', confidence: 0.6, evidence: 'fixed a typo', created_at: '2026-11-02T11:00:00+00:00' },
      { signal_type: 'reworked', confidence: 0.7, evidence: null, created_at: '2026-11-02T12:00:00+00:00' },
      { signal_type: 'self_corrected', confidence: 0.5, evidence: 'caught it', created_at: '2026-11-02T13:00:00+00:00' },
    ],
    daily_trend: [{ date: '2026-11-01', completed: 2, corrected: 1, praised: 1, reworked: 0, self_corrected: 1 }],
  },
  correlations: {
    data: [
      { dimension: 'accuracy', signal_type: 'completed', pearson_r: 1, spearman_rho: 0.9 },
      { dimension: 'accuracy', signal_type: 'reworked', pearson_r: -0.5, spearman_rho: -0.4 },
      { dimension: 'clarity', signal_type: 'completed', pearson_r: 0, spearman_rho: 0 },
    ],
    sample_size: 40,
  },
  weight_history: [
    { version: '3', created_at: null, weights: Object.fromEntries(dims(n).map((d) => [d, 0.2])) },
    { version: '2', created_at: null, weights: Object.fromEntries(dims(n).map((d) => [d, 0.1])) },
  ],
  config: { rubric_enabled: true, evolution_enabled: true, outcome_detection_enabled: true, min_episodes_for_correlation: 20, weight_change_cap: 0.05 },
});

type Cfg = { type: string; data: { labels: string[]; datasets: Record<string, any>[] }; options: Record<string, any> };
let configs: Cfg[] = [];
let body: unknown = DATA();
function install() {
  configs = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (!url.startsWith('/dashboard/rubric')) throw new Error(`unexpected ${url}`);
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
  vi.stubGlobal('Chart', class {
    data = {}; options = {};
    constructor(_el: unknown, cfg: Cfg) { configs.push(cfg); }
    destroy() {}
    update() {}
  });
}
function paperMain(): HTMLElement {
  const main = document.createElement('main');
  main.setAttribute('data-ground', 'paper');
  for (const [k, v] of Object.entries(PAPER)) if (k.startsWith('--')) main.style.setProperty(k, v);
  document.body.appendChild(main);
  return main;
}
const rgb = (c: string) => {
  if (/^#[0-9a-f]{6}$/i.test(c)) return [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16)).join(',');
  const m = /^rgba?\(([^)]+)\)$/.exec(c);
  return m ? m[1].split(',').slice(0, 3).map((x) => x.trim()).join(',') : c;
};
const alpha = (c: string) => Number(/^rgba\([^)]*,\s*([\d.]+)\)$/.exec(c)?.[1] ?? 1);
function colours(v: unknown, key = '', out: string[] = []): string[] {
  if (typeof v === 'string' && /color/i.test(key)) out.push(v);
  else if (Array.isArray(v)) v.forEach((x) => colours(x, key, out));
  else if (v && typeof v === 'object') for (const [k, x] of Object.entries(v)) colours(x, k, out);
  return out;
}
const chart = (type: string, label?: string) =>
  configs.find((c) => c.type === type && (!label || c.data.datasets.some((d) => d.label === label)))!;
const evolution = () => configs.filter((c) => c.type === 'line').find((c) => c.data.labels[0]?.startsWith('v'))!;

beforeEach(() => { body = DATA(); install(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); setLook('dark'); document.body.innerHTML = ''; });

describe('Scoring: the view', () => {
  it('names the correlations in words, with no arrow glyph', async () => {
    const { container } = render(Rubric);
    expect(await screen.findByRole('heading', { level: 2, name: 'Dimension and signal correlations' })).toBeTruthy();
    expect(container.textContent).not.toMatch(/↔/);
  });

  it('a heatmap cell mixes the fill with transparent at the old alpha (green for r ≥ 0, red below)', async () => {
    render(Rubric);
    const cell = async (text: string) => (await screen.findByText(text, { selector: 'td.corr-cell' })) as HTMLElement;
    expect((await cell('1.00')).style.getPropertyValue('--heat')).toBe('color-mix(in srgb, var(--green) 65%, transparent)');
    expect((await cell('-0.50')).style.getPropertyValue('--heat')).toBe('color-mix(in srgb, var(--red) 35%, transparent)');
    expect((await cell('0.00')).style.getPropertyValue('--heat')).toBe('color-mix(in srgb, var(--green) 5%, transparent)');
  });

  it('badges say the status or signal, each in its own class', async () => {
    render(Rubric);
    const badge = async (text: string) => (await screen.findAllByText(text, { selector: '.badge' }))[0];
    expect((await badge('active')).classList.contains('badge-success')).toBe(true);
    expect((await badge('rollback')).classList.contains('badge-failure')).toBe(true);
    expect((await badge('superseded')).classList.contains('badge-pending')).toBe(true);
    expect((await badge('completed')).classList.contains('badge-success')).toBe(true);
    expect((await badge('corrected')).classList.contains('badge-partial')).toBe(true);
    expect((await badge('reworked')).classList.contains('badge-failure')).toBe(true);
    expect((await badge('self corrected')).classList.contains('badge-pending')).toBe(true);
  });

  it('keeps its empty states', async () => {
    body = { ...DATA(), active_rubric: null };
    render(Rubric);
    expect(await screen.findByText(/No active rubric/)).toBeTruthy();
    cleanup();
    body = { ...DATA(), correlations: { data: [], sample_size: 0 } };
    render(Rubric);
    expect(await screen.findByText(/No correlation data yet/)).toBeTruthy();
  });

  it('Refresh is the shared outline pill button', async () => {
    render(Rubric);
    expect((await screen.findByRole('button', { name: 'Refresh' })).getAttribute('data-slot')).toBe('button');
  });
});

describe('Scoring: chart colours are tokens', () => {
  it('dark: the radar, the doughnut and the signal trend are today\'s colours (the slice gap is the card colour)', async () => {
    render(Rubric);
    await waitFor(() => expect(configs).toHaveLength(4));
    const radar = chart('radar');
    expect(radar.data.datasets[0].borderColor).toBe('#7c6af7');
    expect(rgb(radar.data.datasets[0].backgroundColor)).toBe('124,106,247');
    expect(alpha(radar.data.datasets[0].backgroundColor)).toBe(0.2);
    // Review I1: the bounds are full-strength lines (a dark-only shift from 40%), Min dotted and Max dashed.
    expect(radar.data.datasets[1].borderColor).toBe('#f87171');
    expect(radar.data.datasets[2].borderColor).toBe('#34d399');
    expect(radar.data.datasets[1].borderDash).toEqual([2, 3]);
    expect(radar.data.datasets[2].borderDash).toEqual([8, 4]);
    expect(radar.options.scales.r.grid.color).toBe('#1e1e2e');
    expect(radar.options.scales.r.angleLines.color).toBe('#1e1e2e');
    // Review M2: the tick labels sit on the weights; with no backdrop box they do not hide them.
    expect(radar.options.scales.r.ticks).toMatchObject({ stepSize: 0.1, showLabelBackdrop: false });
    const doughnut = chart('doughnut');
    expect(doughnut.data.labels).toEqual(['completed', 'praised', 'corrected', 'reworked', 'self corrected', 'mystery']);
    expect(doughnut.data.datasets[0].backgroundColor).toEqual(['#34d399', '#60a5fa', '#fb923c', '#f87171', '#a78bfa', '#6b6b8a']);
    expect(doughnut.data.datasets[0].borderColor).toBe('#12121a'); // D4: was #111118, one step from the card
    const trend = chart('line', 'Completed');
    expect(trend.data.datasets.map((d) => d.borderColor)).toEqual(['#34d399', '#60a5fa', '#fb923c', '#f87171', '#a78bfa']);
    for (const d of trend.data.datasets) {
      expect(rgb(d.backgroundColor), d.label).toBe(rgb(d.borderColor));
      expect(alpha(d.backgroundColor), d.label).toBe(0.15);
    }
  });

  it('dark: the weight evolution keeps the seven dimension colours, and every line has a dash of its own', async () => {
    render(Rubric);
    await waitFor(() => expect(configs).toHaveLength(4));
    const sets = evolution().data.datasets;
    expect(sets.map((d) => d.borderColor)).toEqual(DIM_OLD);
    const dashes = sets.map((d) => JSON.stringify(d.borderDash));
    expect(new Set(dashes).size).toBe(sets.length); // red-green colour blindness merges some hues; the dash tells them apart
    expect(sets[0].borderDash).toEqual([]);
  });

  it('dimensions whose hues merge under colour blindness have dashes of different kinds (solid, long dash, dotted, dash-dot)', async () => {
    body = DATA(9);
    render(Rubric);
    await waitFor(() => expect(configs).toHaveLength(4));
    const kind = (d: number[]) => (d.length === 0 ? 'solid' : d.length > 2 ? 'dash-dot' : d[0] <= 2 ? 'dotted' : 'dash');
    const dash = evolution().data.datasets.map((d) => kind(d.borderDash));
    // The open dimension pairs of tokens.test.ts (by index in DIM_TOKENS): series-0/8, 2/6, 1/3, 4/5, 3/9, 1/9.
    for (const [a, b] of [[0, 7], [1, 6], [2, 5], [3, 4], [5, 8], [2, 8]]) expect(dash[a], `${DIM_TOKENS[a]} and ${DIM_TOKENS[b]}`).not.toBe(dash[b]);
  });

  it('every weight line is at least a third ink, so a dotted one can be followed where lines cross (review M3)', async () => {
    body = DATA(9);
    render(Rubric);
    await waitFor(() => expect(configs).toHaveLength(4));
    for (const d of evolution().data.datasets) {
      const dash: number[] = d.borderDash;
      if (dash.length === 0) continue;
      const ink = dash.filter((_, i) => i % 2 === 0).reduce((a, b) => a + b, 0) / dash.reduce((a, b) => a + b, 0);
      expect(ink, `${d.label} ${JSON.stringify(dash)}`).toBeGreaterThanOrEqual(1 / 3);
    }
  });

  it('a dimension keeps its hue and dash by name when the oldest version leaves the history (review M4)', async () => {
    const version = (v: string, names: string[]) => ({ version: v, created_at: null, weights: Object.fromEntries(names.map((n) => [n, 0.2])) });
    const active = ['accuracy', 'clarity', 'speed'];
    const look = async (history: ReturnType<typeof version>[]) => {
      configs = [];
      body = { ...DATA(), weight_history: history };
      const r = render(Rubric);
      await waitFor(() => expect(configs).toHaveLength(4));
      const sets = evolution().data.datasets;
      r.unmount();
      return Object.fromEntries(sets.map((d) => [d.label, [d.borderColor, JSON.stringify(d.borderDash)]]));
    };
    // The endpoint sends the 20 newest versions; the oldest here listed a retired dimension first.
    const before = await look([version('4', active), version('3', active), version('2', ['legacy', ...active])]);
    const after = await look([version('4', active), version('3', active)]);
    for (const n of active) expect(after[n], n).toEqual(before[n]);
    expect(before.accuracy[0]).toBe('#7c6af7'); // the active rubric's order first: accuracy is the first dimension
    expect(Object.keys(before)).toEqual([...active, 'legacy']); // a name seen only in the history comes after, sorted
  });

  it('dimensions past the seventh take series 8, then series 9, each with its own dash', async () => {
    body = DATA(9);
    render(Rubric);
    await waitFor(() => expect(configs).toHaveLength(4));
    const sets = evolution().data.datasets;
    expect(sets.map((d) => d.borderColor).slice(7)).toEqual(['#e2e2f0', '#22d3ee']);
    expect(new Set(sets.map((d) => JSON.stringify(d.borderDash))).size).toBe(9);
  });

  it('paper: every colour is its token\'s paper value, and no old literal is left', async () => {
    body = DATA(9);
    setLook('paper');
    render(Rubric, { target: paperMain() });
    await waitFor(() => expect(configs).toHaveLength(4));
    for (const cfg of configs) {
      for (const c of [...colours(cfg.data), ...colours(cfg.options)]) expect(OLD.map(rgb), c).not.toContain(rgb(c));
    }
    expect(evolution().data.datasets.map((d) => d.borderColor)).toEqual(DIM_TOKENS.map((t) => PAPER[t]));
    const radar = chart('radar');
    expect(radar.data.datasets[0].borderColor).toBe(PAPER['--series-0']);
    expect(radar.options.scales.r.grid.color).toBe(PAPER['--border']);
    const doughnut = chart('doughnut');
    expect(doughnut.data.datasets[0].borderColor).toBe(PAPER['--surface']);
    expect(doughnut.data.datasets[0].backgroundColor).toEqual(
      ['--oc-success', '--series-1', '--series-4', '--err-text', '--signal-self-corrected', '--series-7'].map((t) => PAPER[t]));
  });
});

describe('Scoring at phone width (the screenshot round)', () => {
  it('the doughnut and the trend legends set a 12 px swatch; the radar\'s and the weight lines\' legends sample the line, 28 px wide', async () => {
    render(Rubric);
    await waitFor(() => expect(configs).toHaveLength(4));
    // Review M1: a dash drawn as the outline of a box turns four corners; a sample of the line shows it straight.
    // Under usePointStyle Chart.js caps boxWidth at the font size, so the width is pointStyleWidth.
    for (const c of [chart('radar'), evolution()]) {
      expect(c.options.plugins.legend.labels, c.type).toMatchObject({ usePointStyle: true, pointStyle: 'line', pointStyleWidth: 28 });
      expect(typeof c.options.plugins.legend.labels.generateLabels, c.type).toBe('function'); // the dash comes from the dataset
    }
    for (const c of [chart('doughnut'), chart('line', 'Completed')]) expect(c.options.plugins.legend.labels?.boxWidth, c.type).toBe(12);
  });

  it('the radar, the signal trend and the weight lines ask for a 300 px phone height; the doughnut keeps 200 (review I3)', async () => {
    const { container } = render(Rubric);
    await waitFor(() => expect(configs).toHaveLength(4));
    const heights = [...container.querySelectorAll('.chart-wrap')].map((el) => (el as HTMLElement).style.getPropertyValue('--phone-height'));
    expect(heights).toEqual(['300px', '200px', '300px', '300px']); // radar, doughnut, trend, weight evolution
  });

  it('the signal trend labels its days month-day (as Decisions does)', async () => {
    render(Rubric);
    await waitFor(() => expect(configs).toHaveLength(4));
    expect(chart('line', 'Completed').data.labels).toEqual(['11-01']);
  });

  it('source contract: under 768 px the radar cell does not reserve 320 px around its 300 px chart', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'Rubric.svelte'), 'utf-8'));
    expect(css).toMatch(/@media \(max-width: 768px\) \{[^@]*\.radar-wrap \{\s*min-height: 0;/);
    // ... and after the 320 px rule, which would win otherwise (same specificity).
    expect(css.search(/\.radar-wrap \{\s*min-height: 0;/)).toBeGreaterThan(css.search(/\.radar-wrap \{\s*min-height: 320px;/));
  });
});

describe('Scoring: the paper look\'s structure (source contracts)', () => {
  const rules = parseBlocks(styleBlocks(readFileSync(join(__dirname, 'Rubric.svelte'), 'utf-8')));

  it('cards read the card tokens; tiles the small radius; table heads the label tokens', () => {
    for (const sel of ['.chart-card', '.empty-state']) {
      expect(rules.get(sel), sel).toMatchObject({ border: 'var(--card-border)', 'border-radius': 'var(--card-radius)', 'box-shadow': 'var(--card-shadow)' });
    }
    expect(rules.get('.dim-card')).toMatchObject({ 'border-radius': 'var(--card-radius-sm)' });
    expect(rules.get('.data-table th')).toMatchObject({ 'text-transform': 'var(--label-transform)', 'letter-spacing': 'var(--label-tracking)' });
    expect(rules.get('.corr-cell')).toMatchObject({ background: 'var(--heat, transparent)' });
  });

  it('the badges read text tokens on their tints (no status fill as text)', () => {
    expect(rules.get('.badge-success')).toEqual({ background: 'var(--badge-good-bg)', color: 'var(--ok-text)' });
    expect(rules.get('.badge-failure')).toEqual({ background: 'var(--badge-bad-bg)', color: 'var(--err-text)' });
    expect(rules.get('.badge-partial')).toEqual({ background: 'color-mix(in srgb, var(--st-partial) 12.5%, transparent)', color: 'var(--st-partial)' });
    expect(rules.get('.badge-pending')).toEqual({ background: 'var(--wash)', color: 'var(--muted)' });
  });

  it('the title takes the paper size under the paper look only', () => {
    expect(rules.get(':global(html[data-look="paper"] [data-ground="paper"]) h1')).toMatchObject({ 'font-size': '30px', 'font-weight': '650' });
  });
});
