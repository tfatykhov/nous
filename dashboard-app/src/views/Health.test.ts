import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, within, waitFor } from '@testing-library/svelte';
import Health from './Health.svelte';
import Overview from './Overview.svelte';
import Observability from './Observability.svelte';
import { initRouter } from '../lib/router';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { setLook } from '../lib/look';
import { PAPER_SEL, parseBlocks, styleBlocks } from '../lib/testing/css';

// F108 Phase 2 T12: Graph Health, Density, Admission, Memory write health and the growth charts become one
// "Memory health" screen with three tabs (Graph, Writes, Growth). Every value below is made up.

const HEALTH = {
  daily_edges: [{ date: '2026-11-07', count: 5, auto: 3, manual: 2 }, { date: '2026-11-08', count: 4, auto: 4, manual: 0 }],
  degree_distribution: [{ degree: 1, count: 10 }, { degree: 2, count: 4 }],
  density: 1.5,
  density_history: [{ date: '2026-11-07', density: 1.4 }, { date: '2026-11-08', density: 1.5 }],
  orphan_counts: { facts: 3, decisions: 1 },
  orphan_trend: [{ date: '2026-11-07', count: 5 }, { date: '2026-11-08', count: 4 }],
  total_orphans: 4,
  total_edges: 9,
  connected_nodes: 14,
};
// The same graph as HEALTH, seen through /dashboard/density.
const DENSITY = {
  total_nodes: 18, total_edges: 9, total_orphans: 4, orphan_rate: 0.2222, avg_degree: 0.64, connected_nodes: 14,
  density_by_type: { fact: { total: 10, orphan: 3, orphan_rate: 0.3 }, decision: { total: 8, orphan: 1, orphan_rate: 0.125 } },
  edge_distribution: { related_to: 6, part_of: 3 },
  backfill_progress: [{ date: '2026-11-08', edges: 4 }],
};
const MWH = {
  enabled: true, window_days: 30, as_of: '2026-11-08T00:00:00+00:00',
  days: [], live: null, rules: [], acknowledgements: [],
  checks: [
    { id: 'H1', name: 'H1', state: 'tripped', value: 2, threshold: 1, urgency: 'high', summary: 'memory rows vanished', detail: {}, acknowledged: null },
    { id: 'F1', name: 'F1', state: 'ok', value: 40, threshold: 100, urgency: 'normal', summary: '', detail: {}, acknowledged: null },
  ],
};
const ADMISSION = {
  config: { enabled: true, shadow_mode: true, threshold: 0.55, weights: { utility: 1, confidence: 1, novelty: 1, recency: 1, type_prior: 1 } },
  summary: { total_scored: 10, admitted: 7, would_reject: 3, bypassed: 1, avg_composite_score: 0.6, rejection_rate: 0.3, threshold_note: '', _pre_migration_note: '' },
  score_distribution: [{ bucket: '0.40-0.45', count: 3 }, { bucket: '0.60-0.65', count: 7 }],
  dimension_stats: {}, by_source: {}, by_category: {}, daily_trend: [], bypass_breakdown: {},
};
const REJECTED = {
  facts: [{ id: 'f-1', content_preview: 'a rejected fact', content_full: 'a rejected fact', category: 'general', source: 'user_direct',
    composite_score: 0.41, scores: {}, created_at: '2026-11-07T00:00:00+00:00' }],
  total: 1, limit: 25, offset: 0,
};
const series = (counts: number[]) => counts.map((count, i) => ({ date: `2026-11-0${i + 1}`, count }));
const STATUS = {
  agent_id: 'a', agent_name: 'Nous', model: 'm',
  memory: { active_conversations: 1, active_censors: 2, total_decisions: 30, total_facts: 100, total_episodes: 20, total_procedures: 3, total_chunks: 40 },
  calibration: { brier_score: 0.1, accuracy: 0.9, total_decisions: 30, reviewed_decisions: 10 },
  execution_integrity: {},
  dashboard: {
    deltas: {
      facts: { total: 100, last_7_days: 5 }, episodes: { total: 20, last_7_days: 2 },
      decisions: { total: 30, last_7_days: 4 }, procedures: { total: 3, last_7_days: 0 },
    },
    distributions: {
      fact_categories: { preference: 4, technical: 6 },
      decision_outcomes: { success: 3, pending: 1 },
      decision_categories: {},
      edge_relations: { related_to: 6 },
    },
    // 30-day sums: facts 1,234, episodes 2, decisions 4.
    timeseries: { facts: series([1200, 34]), episodes: series([1, 1]), decisions: series([4, 0]), procedures: series([0, 0]) },
    graph_density: 1.5,
  },
};
const point = (v: number) => ({ t: '2026-11-08T10:00:00+00:00', v });
const OBS = {
  event_bus: { total_processed: 12, total_dropped: 0, queue_depth: 0, uptime_seconds: 3700, event_counts: {}, handlers: {} },
  recent_traces: [], recent_modifications: [], drift: null, context_log: [],
  drift_trends: { fact_count_delta: [point(3), point(5)], handler_error_rate: [point(0.01)] },
};

let urls: string[] = [];
let obs: Record<string, unknown> = OBS;
let admission: Record<string, unknown> = ADMISSION;
/** While set, /dashboard/density answers only once it resolves (a slow density endpoint). */
let densityGate: Promise<void> | null = null;
function install() {
  urls = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(url);
    if (url === '/dashboard/density' && densityGate) await densityGate;
    const body =
      url === '/dashboard/health?days=30' ? HEALTH
      : url === '/dashboard/density' ? DENSITY
      : url.startsWith('/dashboard/memory-write-health') ? MWH
      : url.startsWith('/dashboard/admission/rejected') ? REJECTED
      : url === '/dashboard/admission' ? admission
      : url === '/status?dashboard=true' ? STATUS
      : url === '/dashboard/observability' ? obs
      : null;
    if (!body) return { ok: false, status: 404, json: async () => ({}) } as Response;
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
  vi.stubGlobal('Chart', class { destroy() {} update() {} data = {}; options = {}; });
}
function go(hash: string) {
  location.hash = hash;
  window.dispatchEvent(new HashChangeEvent('hashchange'));
}
function start(hash: string) {
  install();
  location.hash = '';
  initRouter();
  go(hash);
}
const h2 = (name: string) => screen.queryAllByRole('heading', { level: 2, name });
const h2Texts = () => screen.queryAllByRole('heading', { level: 2 }).map((h) => h.textContent?.trim() ?? '');
const tabsNav = () => screen.getByRole('navigation', { name: 'Memory health views' });

const GRAPH_HEADINGS = [
  'Graph Density Over Time', 'Edge Creation Rate', 'Node Degree Distribution', 'Orphan Nodes Over Time',
  'Auto vs Manual Edges', 'Orphan Breakdown by Type', 'Orphan Rate by Type', 'Edge Distribution',
  'Backfill Progress (Last 7 Days)',
];
const statLabel = (label: string) => screen.queryAllByText(label, { selector: '.stat-label' });
const statValue = (label: string) => screen.getByText(label, { selector: '.stat-label' }).previousElementSibling?.textContent?.trim();
const GROWTH_HEADINGS = ['Memory growth (30 days)', 'Fact categories', 'Fact Growth Rate (7d)'];

afterEach(() => { cleanup(); vi.unstubAllGlobals(); obs = OBS; admission = ADMISSION; densityGate = null; });

describe('Memory health: Graph tab', () => {
  for (const hash of ['#/health', '#/health/graph']) {
    it(`${hash} renders every graph chart exactly once under one "Memory health" title`, async () => {
      start(hash);
      render(Health);
      for (const name of GRAPH_HEADINGS) await waitFor(() => expect(h2(name)).toHaveLength(1));
      expect(screen.getAllByRole('heading', { level: 1 }).map((h) => h.textContent?.trim())).toEqual(['Memory health']);
      expect(urls).toContain('/dashboard/health?days=30');
      expect(urls).toContain('/dashboard/density');
    });
  }

  it('shows no <h2> twice', async () => {
    start('#/health/graph');
    render(Health);
    for (const name of GRAPH_HEADINGS) await waitFor(() => expect(h2(name)).toHaveLength(1));
    const texts = h2Texts();
    expect(new Set(texts).size).toBe(texts.length);
  });

  it('moves Density\'s tables with their rows, and keeps both sources\' staleness badges', async () => {
    start('#/health/graph');
    const { container } = render(Health);
    const edges = (await screen.findByRole('heading', { level: 2, name: 'Edge Distribution' })).closest('section') as HTMLElement;
    expect(within(edges).getByText('related_to')).toBeTruthy();
    const backfill = screen.getByRole('heading', { level: 2, name: 'Backfill Progress (Last 7 Days)' }).closest('section') as HTMLElement;
    expect(within(backfill).getByText('2026-11-08')).toBeTruthy();
    const orphans = screen.getByRole('heading', { level: 2, name: 'Orphan Breakdown by Type' }).closest('section') as HTMLElement;
    expect(within(orphans).getByText('facts')).toBeTruthy();
    expect(container.querySelectorAll('.stale-badge')).toHaveLength(2);
  });

  it('keeps Density\'s orphan rate by type, with the per-type totals and rates the breakdown does not show', async () => {
    start('#/health/graph');
    render(Health);
    const rates = (await screen.findByRole('heading', { level: 2, name: 'Orphan Rate by Type' })).closest('section') as HTMLElement;
    expect(within(rates).getAllByRole('columnheader').map((c) => c.textContent?.trim())).toEqual(['Type', 'Total', 'Orphans', 'Rate']);
    const fact = within(rates).getByText('facts').closest('tr') as HTMLElement;
    expect(within(fact).getAllByRole('cell').map((c) => c.textContent?.trim())).toEqual(['facts', '10', '3', '30.0%']);
  });

  it('names the node types the same way in the orphan breakdown and the orphan rate table', async () => {
    start('#/health/graph');
    render(Health);
    const rates = (await screen.findByRole('heading', { level: 2, name: 'Orphan Rate by Type' })).closest('section') as HTMLElement;
    const breakdown = screen.getByRole('heading', { level: 2, name: 'Orphan Breakdown by Type' }).closest('section') as HTMLElement;
    const inBreakdown = Array.from(breakdown.querySelectorAll('.orphan-type')).map((e) => e.textContent?.trim()).filter((t) => t !== 'Total');
    const inRates = within(rates).getAllByRole('row').slice(1).map((r) => r.querySelector('td')?.textContent?.trim());
    expect(inRates.sort()).toEqual(inBreakdown.sort());
    expect(inRates.sort()).toEqual(['decisions', 'facts']);
  });

  it('labels the orphan trend as a running total that may differ from the count above', async () => {
    start('#/health/graph');
    render(Health);
    const trend = (await screen.findByRole('heading', { level: 2, name: 'Orphan Nodes Over Time' })).closest('section') as HTMLElement;
    expect(within(trend).getByText('Running total; may differ from the count above')).toBeTruthy();
  });

  it('shows Density\'s Loading… while /dashboard/density is still in flight', async () => {
    let release!: () => void;
    densityGate = new Promise<void>((r) => { release = r; });
    start('#/health/graph');
    render(Health);
    await screen.findByRole('heading', { level: 2, name: 'Graph Density Over Time' });
    // Health has loaded, so the one Loading… line left is Density's.
    expect(screen.getAllByText('Loading…', { selector: 'p' })).toHaveLength(1);
    expect(statLabel('Total Nodes')).toHaveLength(0);
    release();
    await waitFor(() => expect(statLabel('Total Nodes')).toHaveLength(1));
    expect(screen.queryAllByText('Loading…', { selector: 'p' })).toHaveLength(0);
  });

  it('puts Density\'s totals and the overall orphan rate at the top, each number once, without the summary footer', async () => {
    start('#/health/graph');
    render(Health);
    const first = await screen.findByRole('heading', { level: 2, name: 'Graph Density Over Time' });
    await waitFor(() => expect(statLabel('Total Nodes')).toHaveLength(1));
    expect(statValue('Total Nodes')).toBe('18');
    expect(statValue('Total Edges')).toBe('9');
    expect(statValue('Avg Degree')).toBe('0.64');
    expect(statValue('Connected Nodes')).toBe('14');
    const banner = screen.getByText('Overall Orphan Rate').parentElement as HTMLElement;
    expect(banner.textContent?.replace(/\s+/g, ' ')).toContain('Overall Orphan Rate 22.2% (moderate) (4 of 18 nodes unconnected)');
    // Density's block comes first: before Health's stats and the first chart.
    const top = screen.getByText('Total Nodes', { selector: '.stat-label' });
    expect(top.compareDocumentPosition(banner) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(banner.compareDocumentPosition(screen.getByText('Current Density', { selector: '.stat-label' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(banner.compareDocumentPosition(first) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Health's other numbers stay; its orphan count is the banner's, so it is not shown again.
    for (const label of ['Current Density', 'Edges (30d)', 'Auto-linked']) expect(statLabel(label)).toHaveLength(1);
    expect(statLabel('Orphan Nodes')).toHaveLength(0);
    // The footer only repeated the grid (connected nodes) and the banner (orphans).
    expect(screen.queryByText(/Connected nodes:/)).toBeNull();
    expect(screen.queryByText(/Total orphans:/)).toBeNull();
  });
});

describe('Memory health: Writes tab', () => {
  it('renders the write-health verdict, then the admission sections, with neither page\'s own title', async () => {
    start('#/health/writes');
    const { container } = render(Health);
    const verdict = await screen.findByRole('heading', { level: 2, name: '1 rule tripped: you decide' });
    const sim = await screen.findByRole('heading', { level: 2, name: 'Threshold Simulator' });
    const rejected = screen.getByRole('heading', { level: 2, name: 'Rejected Facts' });
    // A visible heading separates the two pages: after the write-health sections, before the admission ones.
    const admission = screen.getByRole('heading', { level: 2, name: 'Admission' });
    const acks = screen.getByRole('heading', { level: 2, name: 'Acknowledgements' });
    expect(acks.compareDocumentPosition(admission) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(admission.compareDocumentPosition(sim) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(verdict.compareDocumentPosition(sim) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(sim.compareDocumentPosition(rejected) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(await screen.findByText('a rejected fact')).toBeTruthy();
    expect(screen.getAllByRole('heading', { level: 1 }).map((h) => h.textContent?.trim())).toEqual(['Memory health']);
    expect(container.querySelectorAll('.stale-badge')).toHaveLength(2);
    expect(urls.some((u) => u.startsWith('/dashboard/health'))).toBe(false);
  });
});

describe('Memory health: Growth tab', () => {
  it('renders memory growth, fact categories and the fact growth rate', async () => {
    start('#/health/growth');
    const { container } = render(Health);
    for (const name of GROWTH_HEADINGS) await waitFor(() => expect(h2(name)).toHaveLength(1));
    expect(urls).toContain('/status?dashboard=true');
    expect(urls).toContain('/dashboard/observability');
    expect(container.querySelectorAll('.stale-badge')).toHaveLength(2);
  });
});

describe('Memory health: tabs', () => {
  const CASES: [string, string][] = [
    ['#/health', 'Graph'], ['#/health/graph', 'Graph'], ['#/health/writes', 'Writes'], ['#/health/growth', 'Growth'],
  ];
  for (const [hash, label] of CASES) {
    it(`${hash} marks ${label} as the current tab`, async () => {
      start(hash);
      render(Health);
      const nav = tabsNav();
      for (const other of ['Graph', 'Writes', 'Growth']) {
        const current = within(nav).getByRole('link', { name: other }).getAttribute('aria-current');
        expect(current).toBe(other === label ? 'page' : null);
      }
    });
  }

  it('each tab is a link to its own route', () => {
    start('#/health');
    render(Health);
    const nav = tabsNav();
    expect(within(nav).getByRole('link', { name: 'Graph' }).getAttribute('href')).toBe('#/health/graph');
    expect(within(nav).getByRole('link', { name: 'Writes' }).getAttribute('href')).toBe('#/health/writes');
    expect(within(nav).getByRole('link', { name: 'Growth' }).getAttribute('href')).toBe('#/health/growth');
  });

  it('switches tab when the route changes', async () => {
    start('#/health');
    render(Health);
    await screen.findByRole('heading', { level: 2, name: 'Edge Distribution' });
    go('#/health/growth');
    expect(await screen.findByRole('heading', { level: 2, name: 'Memory growth (30 days)' })).toBeTruthy();
    expect(h2('Edge Distribution')).toHaveLength(0);
    expect(within(tabsNav()).getByRole('link', { name: 'Growth' }).getAttribute('aria-current')).toBe('page');
  });

  it('an unknown sub path says so and links back to the Graph tab', async () => {
    start('#/health/nope');
    render(Health);
    const msg = await screen.findByRole('status');
    expect(msg.textContent).toContain('Nothing here');
    expect(within(msg).getByRole('link').getAttribute('href')).toBe('#/health/graph');
    expect(h2('Edge Distribution')).toHaveLength(0);
  });
});

describe('Overview after the growth move', () => {
  it('shows one growth summary line with the three counts over the series\' window, linking to the Growth tab', async () => {
    start('#/overview');
    render(Overview);
    const link = await screen.findByRole('link', { name: 'Memory growth' });
    expect(link.getAttribute('href')).toBe('#/health/growth');
    expect(link.closest('p')?.textContent?.replace(/\s+/g, ' ').trim())
      // The window is the series' own length (31 calendar days, today included, in the endpoint; 2 in this fixture).
      .toBe('Created in the last 2 days: 1,234 facts, 2 episodes, 4 decisions. Memory growth');
    expect(screen.getAllByRole('link').filter((a) => a.getAttribute('href') === '#/health/growth')).toHaveLength(1);
  });

  it('no longer renders the growth chart or the fact categories, and keeps decision outcomes and edge types', async () => {
    start('#/overview');
    render(Overview);
    expect(await screen.findByRole('heading', { level: 2, name: 'Decision outcomes' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Edge types' })).toBeTruthy();
    expect(h2('Memory growth (30 days)')).toHaveLength(0);
    expect(h2('Fact categories')).toHaveLength(0);
  });
});

describe('Observability after the growth move', () => {
  it('no longer renders the fact growth rate, and keeps the handler error rate', async () => {
    start('#/observability');
    render(Observability);
    expect(await screen.findByRole('heading', { level: 2, name: 'Handler Error Rate (7d)' })).toBeTruthy();
    expect(h2('Fact Growth Rate (7d)')).toHaveLength(0);
  });

  it('with only fact growth points, says there is no drift trend data', async () => {
    obs = { ...OBS, drift_trends: { fact_count_delta: [point(3)] } };
    start('#/observability');
    render(Observability);
    expect(await screen.findByText('No drift trend data available yet')).toBeTruthy();
    expect(h2('Fact Growth Rate (7d)')).toHaveLength(0);
  });
});

// ── PR 2: the charts read tokens (Chart.js resolves them against the ground the chart sits in) ────────────────────

const PAPER = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')).get(PAPER_SEL) ?? {};
type Ds = Record<string, any>;
let configs: { type: string; data: { datasets: Ds[] }; options: Record<string, any> }[] = [];
/** Replaces the no-op Chart stub with one that records every chart's resolved config. */
function recordCharts() {
  configs = [];
  vi.stubGlobal('Chart', class {
    data = {}; options = {};
    constructor(_el: unknown, cfg: (typeof configs)[number]) { configs.push(cfg); }
    destroy() {}
    update() {}
  });
}
const dataset = (label: string) => {
  const ds = configs.flatMap((c) => c.data.datasets).find((d) => d.label === label);
  if (!ds) throw new Error(`no dataset ${label}`);
  return ds;
};
const chartOf = (label: string) => configs.find((c) => c.data.datasets.some((d) => d.label === label))!;
/** Compare colours by channels and alpha: the old literals were written with and without spaces. */
const norm = (c: unknown): any => (Array.isArray(c) ? c.map(norm) : String(c).replace(/\s+/g, ''));
/** A paper <main> with the paper ground's tokens set on it (jsdom has no stylesheet), as App renders it. */
function paperMain(): HTMLElement {
  const main = document.createElement('main');
  main.setAttribute('data-ground', 'paper');
  for (const [k, v] of Object.entries(PAPER)) if (k.startsWith('--')) main.style.setProperty(k, v);
  document.body.appendChild(main);
  return main;
}

describe('Memory health charts read tokens', () => {
  afterEach(() => { setLook('dark'); document.body.innerHTML = ''; });

  it('dark: the Graph tab\'s datasets are today\'s colours exactly', async () => {
    start('#/health/graph');
    recordCharts();
    render(Health);
    await waitFor(() => expect(configs.length).toBe(5));
    const want: [string, string, unknown, unknown][] = [
      ['Density', 'borderColor', '#7c6af7', 'rgba(124, 106, 247, 0.1)'],
      ['Edges Created', 'borderColor', '#7c6af7', 'rgba(124, 106, 247, 0.5)'],
      ['Node Count', 'borderColor', '#60a5fa', 'rgba(96, 165, 250, 0.5)'],
      ['Orphan Nodes', 'borderColor', '#f87171', 'rgba(248, 113, 113, 0.1)'],
      ['Auto-linked', 'borderColor', '#34d399', 'rgba(52, 211, 153, 0.5)'],
      ['Manual', 'borderColor', '#7c6af7', 'rgba(124, 106, 247, 0.5)'],
    ];
    for (const [label, , border, fill] of want) {
      expect(norm(dataset(label).borderColor), label).toBe(norm(border));
      expect(norm(dataset(label).backgroundColor), label).toBe(norm(fill));
    }
    // Density 1.4 and 1.5: "watch" (amber); orphans' points in red.
    expect(dataset('Density').pointBackgroundColor).toEqual(['#fbbf24', '#fbbf24']);
    expect(dataset('Orphan Nodes').pointBackgroundColor).toBe('#f87171');
    // Chart.js's own defaults stay on a dark ground: nothing is added to the options.
    expect(chartOf('Node Count').options.color).toBeUndefined();
  });

  it('dark: the Growth tab\'s datasets and the doughnut are today\'s colours exactly', async () => {
    start('#/health/growth');
    recordCharts();
    render(Health);
    await waitFor(() => expect(configs.length).toBe(3));
    expect(norm(['Facts', 'Episodes', 'Decisions'].map((l) => dataset(l).borderColor))).toEqual(['#60a5fa', '#34d399', '#a78bfa']);
    expect(norm(['Facts', 'Episodes', 'Decisions'].map((l) => dataset(l).backgroundColor))).toEqual(
      norm(['rgba(96,165,250,0.10)', 'rgba(52,211,153,0.10)', 'rgba(167,139,250,0.10)']).map((c: string) => c.replace('0.10)', '0.1)')),
    );
    expect(norm(dataset('Fact delta').borderColor)).toBe('#60a5fa');
    expect(norm(dataset('Fact delta').backgroundColor)).toBe('rgba(96,165,250,0.1)');
    const doughnut = configs.find((c) => c.type === 'doughnut')!;
    expect(doughnut.data.datasets[0].backgroundColor).toEqual(['#60a5fa', '#34d399']); // two categories in the fixture
  });

  it('paper: the same datasets take the paper series and status colours, and the axes the chart text and grid', async () => {
    setLook('paper');
    const target = paperMain();
    start('#/health/graph');
    recordCharts();
    render(Health, { target });
    await waitFor(() => expect(configs.length).toBe(5));
    expect(dataset('Node Count').borderColor).toBe(PAPER['--series-1']);
    expect(dataset('Auto-linked').borderColor).toBe(PAPER['--series-2']);
    expect(dataset('Density').borderColor).toBe(PAPER['--accent']);
    expect(dataset('Orphan Nodes').borderColor).toBe(PAPER['--red']);
    expect(dataset('Density').pointBackgroundColor).toEqual([PAPER['--yellow'], PAPER['--yellow']]);
    const o = chartOf('Node Count').options;
    expect(o.scales.x.ticks.color).toBe(PAPER['--chart-text']);
    expect(o.scales.y.grid.color).toBe(PAPER['--chart-grid']);
    for (const c of configs) expect(JSON.stringify(c.data), c.type).not.toMatch(/7c6af7|124, 106, 247/);
  });

  it('the orphan-rate badges keep their classes and read the badge tokens', async () => {
    start('#/health/graph');
    const { container } = render(Health);
    await waitFor(() => expect(container.querySelectorAll('.badge')).not.toHaveLength(0));
    const classes = Array.from(container.querySelectorAll('.badge')).map((b) => [...b.classList].find((c) => c.startsWith('badge-')));
    expect(classes).toEqual(['badge-warn', 'badge-warn', 'badge-good']); // 22.2% overall, then facts 30.0%, decisions 12.5%
    const css = styleBlocks(readFileSync(join(__dirname, 'health', 'GraphTab.svelte'), 'utf-8'));
    expect(css).toMatch(/\.badge-good\s*\{\s*background:\s*var\(--badge-good-bg\);\s*color:\s*var\(--ok-text\);/);
    expect(css).toMatch(/\.badge-bad\s*\{\s*background:\s*var\(--badge-bad-bg\);\s*color:\s*var\(--err-text\);/);
  });
});

describe('Memory health: the Writes tab\'s admission charts read tokens', () => {
  const FULL_ADMISSION = {
    ...ADMISSION,
    config: { ...ADMISSION.config, shadow_mode: false },
    by_source: { user_direct: { admitted: 5, rejected: 1, bypassed: 2 } },
    by_category: { general: { admitted: 4, rejected: 2 } },
    daily_trend: [{ date: '2026-11-07', scored: 4, admitted: 3, avg_score: 0.6 }, { date: '2026-11-08', scored: 6, admitted: 4, avg_score: 0.62 }],
    bypass_breakdown: { user_direct: 1, pinned: 1 },
  };
  afterEach(() => { setLook('dark'); document.body.innerHTML = ''; });

  it('dark: admitted and rejected are the status green and red, bypassed the series slate, the trends today\'s colours', async () => {
    admission = FULL_ADMISSION;
    start('#/health/writes');
    recordCharts();
    render(Health);
    await waitFor(() => expect(configs.length).toBe(6));
    const scores = configs.find((c) => c.data.datasets[0].label === 'Facts')!.data.datasets[0];
    expect(scores.backgroundColor).toEqual(['#f87171', '#34d399']); // 0.40-0.45 below the 0.55 threshold, 0.60-0.65 above
    const bySource = configs.find((c) => c.data.datasets.length === 3)!.data.datasets;
    expect(bySource.map((d) => d.backgroundColor)).toEqual(['#34d399', '#f87171', '#6b6b8a']);
    expect(norm(dataset('Admission Rate (%)').backgroundColor)).toBe('rgba(52,211,153,0.1)');
    expect(dataset('Avg Score').borderColor).toBe('#7c6af7');
    const bypass = configs.find((c) => c.type === 'doughnut')!.data.datasets[0];
    expect(bypass.backgroundColor).toEqual(['#7c6af7', '#60a5fa']);
    expect(bypass.borderColor).toBe('#12121a'); // the card behind it (it was an unreadable 'var(--surface)' string)
  });

  it('paper: the same datasets take the paper status and series values', async () => {
    admission = FULL_ADMISSION;
    setLook('paper');
    const target = paperMain();
    start('#/health/writes');
    recordCharts();
    render(Health, { target });
    await waitFor(() => expect(configs.length).toBe(6));
    const bySource = configs.find((c) => c.data.datasets.length === 3)!.data.datasets;
    expect(bySource.map((d) => d.backgroundColor)).toEqual([PAPER['--green'], PAPER['--red'], PAPER['--series-7']]);
    expect(configs.find((c) => c.type === 'doughnut')!.data.datasets[0].borderColor).toBe(PAPER['--surface']);
  });

  it('the box plots tint with color-mix (no hex alpha suffix), and the score note is in words', async () => {
    const src = readFileSync(join(__dirname, 'Admission.svelte'), 'utf-8');
    expect(src).toContain('style:background="color-mix(in srgb, {color} 18.8%, transparent)"');
    expect(src).toContain('style:background="color-mix(in srgb, {color} 50.2%, transparent)"');
    expect(src).not.toMatch(/≥|#[0-9a-fA-F]{3,8}\b|rgba?\(/);
  });
});
