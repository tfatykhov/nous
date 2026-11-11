import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Decisions from './Decisions.svelte';
import { setLook } from '../lib/look';
import { chartPalette } from '../lib/overviewCharts';
import { PAPER_SEL, parseBlocks, styleBlocks } from '../lib/testing/css';

// Redesign PR 4: Decision Intelligence on paper. Every chart colour is a token that Chart.svelte resolves against the
// ground the canvas sits in: today's colours under dark, the paper values under paper, again on a look switch.

const PAPER = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')).get(PAPER_SEL) ?? {};
const OLD = ['#7c6af7', '#34d399', '#fbbf24', '#f87171', '#6b6b8a', '#a78bfa'];

const DATA = {
  calibration_curve: [
    { bucket: '0.5', actual_success_rate: 0.4, total: 10, successes: 4, avg_confidence: 0.52 },
    { bucket: '0.8', actual_success_rate: 0.75, total: 20, successes: 15, avg_confidence: 0.81 },
  ],
  confidence_histogram: [{ range: '0.5-0.6', count: 10 }, { range: '0.8-0.9', count: 20 }],
  outcome_by_category: { architecture: { success: 5, partial: 1, failure: 2, pending: 3 }, process: { success: 2 } },
  outcome_by_stakes: { low: { success: 4, pending: 1 }, high: { failure: 1, partial: 2 } },
  reason_type_stats: { pattern: { count: 7, success_rate: 0.8, successes: 4, reviewed: 5 }, analysis: { count: 3, success_rate: 1, successes: 2, reviewed: 2 } },
  brier_history: [{ date: '2026-11-01', brier_score: 0.2 }, { date: '2026-11-02', brier_score: 0.12 }],
  daily_decisions: [{ date: '2026-11-01', count: 6, successes: 3, reviewed: 4 }, { date: '2026-11-02', count: 4, successes: 2, reviewed: 2 }],
};

type Cfg = { type: string; data: { labels: string[]; datasets: Record<string, any>[] }; options: Record<string, any> };
let configs: Cfg[] = [];
let updates: { data: Cfg['data'] }[] = [];
let body: unknown = DATA;
let status = 200;

function install() {
  configs = [];
  updates = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (!url.startsWith('/dashboard/calibration')) throw new Error(`unexpected ${url}`);
    return { ok: status < 400, status, json: async () => body } as Response;
  }));
  vi.stubGlobal('Chart', class {
    data: Cfg['data']; options = {};
    constructor(_el: unknown, cfg: Cfg) { configs.push(cfg); this.data = cfg.data; }
    destroy() {}
    update() { updates.push({ data: this.data }); }
  });
}
/** A paper <main> with the paper tokens set on it (jsdom has no stylesheet). */
function paperMain(withTokens = true): HTMLElement {
  const main = document.createElement('main');
  main.setAttribute('data-ground', 'paper');
  if (withTokens) setPaperTokens(main);
  document.body.appendChild(main);
  return main;
}
function setPaperTokens(el: HTMLElement) {
  for (const [k, v] of Object.entries(PAPER)) if (k.startsWith('--')) el.style.setProperty(k, v);
}
/** Every colour string in a chart config (datasets and options), for "no old literal" checks. */
function colours(cfg: { data: Cfg['data']; options?: Record<string, any> }): string[] {
  const out: string[] = [];
  const walk = (v: unknown, key = '') => {
    if (typeof v === 'string' && /color/i.test(key)) out.push(v);
    else if (Array.isArray(v)) v.forEach((x) => walk(x, key));
    else if (v && typeof v === 'object') for (const [k, x] of Object.entries(v)) walk(x, k);
  };
  walk(cfg.data);
  walk(cfg.options);
  return out;
}
const rgb = (c: string) => {
  const h = /^#([0-9a-f]{6})$/i.exec(c);
  if (h) return [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16)).join(',');
  const m = /^rgba?\(([^)]+)\)$/.exec(c);
  return m ? m[1].split(',').slice(0, 3).map((x) => x.trim()).join(',') : c;
};
const alpha = (c: string) => Number(/^rgba\([^)]*,\s*([\d.]+)\)$/.exec(c)?.[1] ?? 1);

beforeEach(() => { body = DATA; status = 200; install(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); setLook('dark'); document.body.innerHTML = ''; });

describe('Decisions: the view', () => {
  it('shows the title, the four stats and the latest Brier score with its word', async () => {
    render(Decisions);
    expect(await screen.findByRole('heading', { level: 1, name: 'Decision Intelligence' })).toBeTruthy();
    await screen.findByText('Decisions (30d)');
    const valueOf = (label: string) => screen.getByText(label).previousElementSibling?.textContent?.trim();
    expect(valueOf('Decisions (30d)')).toBe('10');
    expect(valueOf('Reviewed (30d)')).toBe('6');
    expect(valueOf('Accuracy (30d)')).toBe('83.3%');
    expect(valueOf('Brier score')).toBe('0.120');
    // Status is never colour alone: the dot comes with a word.
    const card = screen.getByText('Latest Brier score (lower is better)').closest('.stat-card')!;
    expect(card.querySelector('.indicator')?.classList.contains('good')).toBe(true);
    expect(card.textContent).toContain('0.1200');
    expect(card.querySelector('.indicator-word')?.textContent).toBe('good');
  });

  it.each([
    [0.12, 'good', 'good'], [0.2, 'warn', 'fair'], [0.3, 'bad', 'poor'],
  ])('a Brier score of %s is %s, said as "%s"', async (score, cls, word) => {
    body = { ...DATA, brier_history: [{ date: '2026-11-02', brier_score: score }] };
    render(Decisions);
    await screen.findByText('Latest Brier score (lower is better)');
    expect(document.querySelector('.indicator')?.classList.contains(cls)).toBe(true);
    expect(document.querySelector('.indicator-word')?.textContent).toBe(word);
  });

  it('with no Brier history the indicator is neutral and says nothing', async () => {
    body = { ...DATA, brier_history: [] };
    render(Decisions);
    await screen.findByText('Latest Brier score (lower is better)');
    expect(document.querySelector('.indicator')?.classList.contains('neutral')).toBe(true);
    expect(document.querySelector('.indicator-word')).toBeNull();
  });

  it('keeps its empty texts and its error text', async () => {
    body = { ...DATA, calibration_curve: [] };
    render(Decisions);
    expect(await screen.findByText('Not enough data for calibration curve')).toBeTruthy();
    cleanup();
    status = 400;
    render(Decisions);
    expect(await screen.findByText(/Failed to load decision intelligence data/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'retry' })).toBeTruthy();
  });

  it('Refresh is the shared outline pill button', async () => {
    render(Decisions);
    const btn = await screen.findByRole('button', { name: 'Refresh' });
    expect(btn.getAttribute('data-slot')).toBe('button');
  });

  it('source contract: no raw-HTML directive', () => {
    expect(readFileSync(join(__dirname, 'Decisions.svelte'), 'utf-8')).not.toContain('{@html');
  });
});

describe('Decisions: chart colours are tokens', () => {
  it('dark: every chart is today\'s colours (violet lines and bars, the outcome palette, the lilac daily bars)', async () => {
    render(Decisions);
    await waitFor(() => expect(configs).toHaveLength(7));
    const [curve, hist, cat, stakes, reason, brier, daily] = configs;
    expect(curve.data.datasets[0].borderColor).toBe('#7c6af7');
    expect(curve.data.datasets[0].pointBackgroundColor).toBe('#7c6af7');
    expect(rgb(curve.data.datasets[0].backgroundColor)).toBe('124,106,247');
    expect(alpha(curve.data.datasets[0].backgroundColor)).toBe(0.1);
    // D3: the perfect-calibration line is --muted at 40% (was #6b6b8a at 40%).
    expect(rgb(curve.data.datasets[1].borderColor)).toBe('142,142,171');
    expect(alpha(curve.data.datasets[1].borderColor)).toBe(0.4);
    expect(curve.data.datasets[1].borderDash).toEqual([5, 5]);
    for (const c of [hist, reason]) {
      expect(c.data.datasets[0].borderColor).toBe('#7c6af7');
      expect(rgb(c.data.datasets[0].backgroundColor)).toBe('124,106,247');
      expect(alpha(c.data.datasets[0].backgroundColor)).toBe(0.5);
    }
    for (const c of [cat, stakes]) {
      expect(c.data.datasets.map((d) => d.label)).toEqual(['Success', 'Partial', 'Failure', 'Pending']);
      expect(c.data.datasets.map((d) => d.backgroundColor)).toEqual(['#34d399', '#fbbf24', '#f87171', '#6b6b8a']);
    }
    expect(brier.data.datasets[0].borderColor).toBe('#7c6af7');
    expect(brier.data.datasets[0].pointBackgroundColor).toBe('#7c6af7');
    expect(alpha(brier.data.datasets[0].backgroundColor)).toBe(0.1);
    expect(daily.data.datasets[0].borderColor).toBe('#a78bfa');
    expect(rgb(daily.data.datasets[0].backgroundColor)).toBe('167,139,250');
    expect(alpha(daily.data.datasets[0].backgroundColor)).toBe(0.5);
  });

  it('dark: the outcome colours mean what they mean on the Overview (one hue, one meaning)', async () => {
    render(Decisions);
    await waitFor(() => expect(configs).toHaveLength(7));
    const tokenOf = chartPalette((name) => name).outcome; // the Overview's token per outcome
    const src = readFileSync(join(__dirname, 'Decisions.svelte'), 'utf-8');
    for (const o of ['success', 'partial', 'failure', 'pending']) expect(src).toContain(`${o}: 'var(${tokenOf(o)})'`);
  });

  it('paper: no old literal is left, and each colour is the paper value of its token', async () => {
    setLook('paper');
    render(Decisions, { target: paperMain() });
    await waitFor(() => expect(configs).toHaveLength(7));
    for (const cfg of configs) {
      for (const c of colours(cfg)) expect(OLD.map((o) => rgb(o)), c).not.toContain(rgb(c));
    }
    const [curve, hist, cat] = configs;
    expect(curve.data.datasets[0].borderColor).toBe(PAPER['--series-0']); // not the accent: on paper that is the question frame's blue
    expect(rgb(curve.data.datasets[1].borderColor)).toBe(rgb(PAPER['--muted']));
    expect(hist.data.datasets[0].borderColor).toBe(PAPER['--series-0']);
    expect(cat.data.datasets.map((d) => d.backgroundColor)).toEqual(['--green', '--yellow', '--red', '--series-7'].map((t) => PAPER[t]));
    expect(configs[6].data.datasets[0].borderColor).toBe(PAPER['--series-3']);
    expect(curve.options.scales.x.ticks.color).toBe(PAPER['--chart-text']);
  });

  it('a look switch recolours every chart without a reload', async () => {
    const main = paperMain(false);
    render(Decisions, { target: main });
    await waitFor(() => expect(configs).toHaveLength(7));
    expect(configs[0].data.datasets[0].borderColor).toBe('#7c6af7');
    const before = updates.length;
    setPaperTokens(main); // what the paper ground's stylesheet declares once the look is on
    setLook('paper');
    await tick();
    const after = updates.slice(before);
    expect(after).toHaveLength(7); // one redraw per chart
    expect(after[0].data.datasets[0].borderColor).toBe(PAPER['--series-0']);
    expect(after[2].data.datasets[0].backgroundColor).toBe(PAPER['--green']);
  });
});

describe('Decisions: the paper look\'s structure (source contracts)', () => {
  const css = styleBlocks(readFileSync(join(__dirname, 'Decisions.svelte'), 'utf-8'));
  const rules = parseBlocks(css);

  it('cards read the card tokens; the chart cards the large radius, the Brier card the small one', () => {
    expect(rules.get('.chart-card')).toMatchObject({ border: 'var(--card-border)', 'border-radius': 'var(--card-radius)', 'box-shadow': 'var(--card-shadow)' });
    expect(rules.get('.stat-card')).toMatchObject({ border: 'var(--card-border)', 'border-radius': 'var(--card-radius-sm)', 'box-shadow': 'var(--card-shadow)' });
    expect(rules.get('.stat-label')).toMatchObject({ 'text-transform': 'var(--label-transform)', 'letter-spacing': 'var(--label-tracking)' });
  });

  it('the title takes the paper size under the paper look only, and chart cells may shrink at phone width', () => {
    expect(rules.get(':global(html[data-look="paper"] [data-ground="paper"]) h1')).toMatchObject({ 'font-size': '30px', 'font-weight': '650' });
    expect(rules.get('.chart-row > *')).toMatchObject({ 'min-width': '0' });
    expect(rules.get('.view-head')).toMatchObject({ 'flex-wrap': 'wrap' });
  });
});
