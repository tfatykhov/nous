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
    expect([rgb(radar.data.datasets[1].borderColor), alpha(radar.data.datasets[1].borderColor)]).toEqual(['248,113,113', 0.4]);
    expect([rgb(radar.data.datasets[2].borderColor), alpha(radar.data.datasets[2].borderColor)]).toEqual(['52,211,153', 0.4]);
    expect(radar.options.scales.r.grid.color).toBe('#1e1e2e');
    expect(radar.options.scales.r.angleLines.color).toBe('#1e1e2e');
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
      ['--green', '--series-1', '--series-4', '--err-text', '--series-3', '--series-7'].map((t) => PAPER[t]));
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
