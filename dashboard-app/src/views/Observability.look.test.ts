import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Observability from './Observability.svelte';
import { setLook } from '../lib/look';
import { PAPER_SEL, parseBlocks, styleBlocks } from '../lib/testing/css';

// Redesign PR 5: Observability on paper. The handler dots and the error-trend chart read tokens (Chart.svelte resolves
// the chart's against the ground its canvas sits in), the trace toggle and the anomaly line are words, not glyphs, and
// an anomaly's severity is said, not only coloured.

const PAPER = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')).get(PAPER_SEL) ?? {};
const SOURCE = readFileSync(join(__dirname, 'Observability.svelte'), 'utf-8');

const handler = (invocations: number, errors: number, error_rate: number) => ({
  invocations, successes: invocations - errors, errors, error_rate, avg_duration_ms: 3.2,
});
const OBS = {
  event_bus: {
    total_processed: 1200, total_dropped: 0, queue_depth: 0, uptime_seconds: 3700, event_counts: {},
    handlers: {
      'nous.handlers.fact_extractor.handle': handler(40, 0, 0),
      'nous.handlers.episode_summarizer.handle': handler(20, 1, 0.05),
      'nous.handlers.sleep_handler.handle': handler(10, 2, 0.2),
    },
  },
  recent_traces: [
    { trace_id: 't-1', root_type: 'turn_completed', timestamp: '2026-11-07T00:07:00+00:00', event_count: 3, has_modifications: true },
    { trace_id: 't-2', root_type: null, timestamp: null, event_count: 1, has_modifications: false },
  ],
  recent_modifications: [
    { event_id: 'e-1', type: 'censor_created', trace_id: 't-1', modifies: 'censor', timestamp: '2026-11-07T00:07:00+00:00' },
    { event_id: 'e-2', type: 'procedure_updated', trace_id: null, modifies: null, timestamp: null },
  ],
  drift: {
    timestamp: '2026-11-07T00:00:00+00:00',
    metrics: { fact_count: 120, fact_count_delta: 3, episode_count: 40, active_censor_count: 2, procedure_count: 5, handler_error_rate: 0.02 },
    anomalies: [
      { metric: 'fact_count_delta', severity: 'alert', current: 4, direction: 'up', mean: 2, stddev: 1.5, z_score: 3.1,
        raw_current: 12, residualized_by: 'inactive_fact_delta' },
      { metric: 'handler_error_rate', severity: 'warning', current: 0.05, direction: 'up', mean: 0.01, stddev: 0.01, z_score: 2.1 },
      { metric: 'procedure_count', severity: 'alert', current: 5, direction: 'up', mean: 3, stddev: 0, z_score: null },
    ],
  },
  drift_trends: { handler_error_rate: [{ t: '2026-11-05T10:00:00Z', v: 0.01 }, { t: '2026-11-06T10:00:00Z', v: 0.05 }] },
  context_log: [],
};
const ACTIVITY = {
  events: [{ type: 'sleep_completed', created_at: '2026-11-07T00:07:00+00:00', data: { facts_created: 2, procedures_created: 1 } }],
  censor_stats: { total: 0, active: 0, auto_created: 0, manual_created: 0, total_activations_7d: 0, false_positives_7d: 0, top_censors: [] },
  schedule_stats: { total: 0, active: 0, fires_7d: 0, next_fires: [] },
  sleep_stats: { total_sleeps: 0, last_sleep: null, facts_created: 0, procedures_created: 0, censors_retired: 0 },
};

type Cfg = { type: string; data: { labels: string[]; datasets: Record<string, any>[] }; options: Record<string, any> };
let configs: Cfg[] = [];
let updates: { data: Cfg['data'] }[] = [];
let obs: unknown = OBS;

beforeEach(() => {
  configs = [];
  updates = [];
  obs = OBS;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.startsWith('/dashboard/observability') ? obs : url.startsWith('/dashboard/activity') ? ACTIVITY : {};
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
  vi.stubGlobal('Chart', class {
    data: Cfg['data']; options = {};
    constructor(_el: unknown, cfg: Cfg) { configs.push(cfg); this.data = cfg.data; }
    destroy() {}
    update() { updates.push({ data: this.data }); }
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); setLook('dark'); document.body.innerHTML = ''; });

/** A paper <main> with the paper tokens set on it (jsdom has no stylesheet). */
function paperMain(withTokens = true): HTMLElement {
  const main = document.createElement('main');
  main.setAttribute('data-ground', 'paper');
  if (withTokens) for (const [k, v] of Object.entries(PAPER)) if (k.startsWith('--')) main.style.setProperty(k, v);
  document.body.appendChild(main);
  return main;
}
const rgb = (c: string) => {
  if (/^#[0-9a-f]{6}$/i.test(c)) return [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16)).join(',');
  const m = /^rgba?\(([^)]+)\)$/.exec(c);
  return m ? m[1].split(',').slice(0, 3).map((x) => x.trim()).join(',') : c;
};
const alpha = (c: string) => Number(/^rgba\([^)]*,\s*([\d.]+)\)$/.exec(c)?.[1] ?? 1);
const text = (el: Element | null) => el?.textContent?.replace(/\s+/g, ' ').trim();

describe('Observability: handler dots and the error trend read tokens', () => {
  it('a handler dot carries its band as a token: none, some and more than 10% errors', async () => {
    render(Observability);
    await screen.findByText('fact_extractor');
    const dotOf = (name: string) => screen.getByText(name).closest('.handler-row')!.querySelector<HTMLElement>('.handler-dot')!;
    // The success mark (PR 4's --oc-success): 10.5 from the red dot under deuteranopia, where --st-completed was 5.5.
    expect(dotOf('fact_extractor').style.getPropertyValue('--dot')).toBe('var(--oc-success)');
    expect(dotOf('episode_summarizer').style.getPropertyValue('--dot')).toBe('var(--yellow)');
    // --err-text, not --red, for the top band: under deuteranopia it is 12.5 from the amber (red: 5.1); dark is the same #f87171.
    expect(dotOf('sleep_handler').style.getPropertyValue('--dot')).toBe('var(--err-text)');
    // The band is also said in figures beside the dot.
    expect(text(screen.getByText('sleep_handler').closest('.handler-row'))).toContain('20.0% err');
  });

  it('a handler\'s error figure is in the error colour when that handler is above 10%, whatever the bus as a whole', async () => {
    render(Observability);
    await screen.findByText('fact_extractor');
    const warned = (name: string) => screen.getByText(name).closest('.handler-row')!.querySelector('.handler-rate')!
      .classList.contains('handler-rate--warn');
    // The bus is at 3 errors in 70 (4.3%); the 20% handler's figure still says so in colour, like its dot.
    expect(['fact_extractor', 'episode_summarizer', 'sleep_handler'].map(warned)).toEqual([false, false, true]);
  });

  it('a healthy handler\'s figure stays muted when the bus as a whole is above 10%', async () => {
    obs = { ...OBS, event_bus: { ...OBS.event_bus, handlers: {
      'nous.handlers.flaky.handle': handler(10, 5, 0.5),
      'nous.handlers.steady.handle': handler(20, 0, 0),
    } } };
    render(Observability);
    await screen.findByText('steady');
    const rate = (name: string) => screen.getByText(name).closest('.handler-row')!.querySelector('.handler-rate')!;
    expect(rate('steady').classList.contains('handler-rate--warn')).toBe(false); // the bus is at 5 in 30, 16.7%
    expect(rate('flaky').classList.contains('handler-rate--warn')).toBe(true);
  });

  it('dark: the error-trend line and fill are today\'s red', async () => {
    render(Observability);
    await waitFor(() => expect(configs).toHaveLength(1));
    const [ds] = configs[0].data.datasets;
    expect(rgb(ds.borderColor)).toBe('248,113,113');
    expect(rgb(ds.backgroundColor)).toBe('248,113,113');
    expect(alpha(ds.backgroundColor)).toBe(0.1);
  });

  it('paper: the error-trend line is the paper red, no old literal is left, and the axes take the chart text', async () => {
    setLook('paper');
    render(Observability, { target: paperMain() });
    await waitFor(() => expect(configs).toHaveLength(1));
    const [ds] = configs[0].data.datasets;
    expect(ds.borderColor).toBe(PAPER['--red']);
    expect(rgb(ds.backgroundColor)).toBe(rgb(PAPER['--red']));
    expect(alpha(ds.backgroundColor)).toBe(0.1);
    expect([ds.borderColor, ds.backgroundColor].map(rgb)).not.toContain('248,113,113');
    expect(configs[0].options.scales.x.ticks.color).toBe(PAPER['--chart-text']);
  });

  it('a look switch redraws the trend chart without a reload (paper to dark)', async () => {
    setLook('paper');
    const main = paperMain();
    render(Observability, { target: main });
    await waitFor(() => expect(configs).toHaveLength(1));
    expect(configs[0].data.datasets[0].borderColor).toBe(PAPER['--red']);
    const before = updates.length;
    for (const k of Object.keys(PAPER)) main.style.removeProperty(k); // the paper ground's rule stops matching
    setLook('dark');
    await tick();
    const after = updates.slice(before);
    expect(after.length).toBeGreaterThanOrEqual(1);
    expect(rgb(after.at(-1)!.data.datasets[0].borderColor)).toBe('248,113,113');
  });
});

describe('Observability: words, not glyphs', () => {
  it('a trace row says Show, opens on Enter (Hide, expanded), and closes on Space', async () => {
    render(Observability);
    const type = await screen.findByText('turn_completed');
    const row = type.closest<HTMLElement>('.trace-row')!;
    expect(row.getAttribute('role')).toBe('button');
    expect(row.getAttribute('aria-expanded')).toBe('false');
    expect(text(row.querySelector('.trace-toggle'))).toBe('Show');
    await fireEvent.keyDown(row, { key: 'Enter' });
    expect(row.getAttribute('aria-expanded')).toBe('true');
    expect(text(row.querySelector('.trace-toggle'))).toBe('Hide');
    expect(screen.getByText('t-1')).toBeTruthy(); // the detail is open
    const space = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
    row.dispatchEvent(space);
    await tick();
    expect(space.defaultPrevented).toBe(true); // Space does not scroll the page
    expect(row.getAttribute('aria-expanded')).toBe('false');
    expect(text(row.querySelector('.trace-toggle'))).toBe('Show');
    await fireEvent.click(row); // the click still toggles
    expect(row.getAttribute('aria-expanded')).toBe('true');
  });

  it('each anomaly is one sentence with its severity in words, a comma where the arrow was, and its spaces', async () => {
    render(Observability);
    await screen.findByText('Active Drift Anomalies');
    const lines = [...document.querySelectorAll('.anomaly')].map((a) => text(a));
    expect(lines).toEqual([
      'Alert fact_count_delta: 12 raw, 4 unexplained after inactive_fact_delta (up from 2 ± 1.5)',
      'Warning handler_error_rate: 0.05 (up from 0.01 ± 0.01)',
      'Alert procedure_count: 5 (up from 3, previously constant)',
    ]);
    const [alert, warning] = [...document.querySelectorAll('.anomaly')];
    expect(alert.classList.contains('anomaly--alert')).toBe(true);
    expect(warning.classList.contains('anomaly--alert')).toBe(false);
  });

  it('the Facts stat says "change", and no arrow, delta sign, triangle or lone question mark is shown', async () => {
    render(Observability);
    await screen.findByText('Active Drift Anomalies');
    // The change is the stat's note, so the value stays one short figure (in the value, "120 (change +3)" took three
    // lines of the large type at phone width).
    const facts = screen.getByText('Facts');
    expect(text(facts.previousElementSibling)).toBe('120');
    expect(text(facts.nextElementSibling)).toBe('change +3');
    const shown = document.body.textContent ?? '';
    for (const g of ['→', 'Δ', '▶', '▼']) expect(shown, g).not.toContain(g);
    // A trace or modification with no type says so in a word (it showed "?").
    expect(text(screen.getAllByText('unknown')[0])).toBe('unknown');
    expect(document.querySelectorAll('.trace-type, .mod-type').length).toBe(4);
    for (const el of document.querySelectorAll('.trace-type, .mod-type')) expect(text(el)).not.toBe('?');
  });

  it('Refresh on the Event Timeline is the shared outline pill button', async () => {
    render(Observability);
    const btn = await screen.findByRole('button', { name: 'Refresh' });
    expect(btn.getAttribute('data-slot')).toBe('button');
  });
});

describe('Observability: the paper look\'s structure (source contracts)', () => {
  const rules = parseBlocks(styleBlocks(SOURCE));

  it('a trace row shows the focus ring on keyboard focus', () => {
    expect(rules.get('.trace-row:focus-visible')).toMatchObject({ outline: '2px solid var(--focus-ring)', 'outline-offset': '2px' });
  });

  it('cards read the card tokens, labels the label tokens, and the title takes the paper size under paper only', () => {
    expect(rules.get('.chart-card')).toMatchObject({ border: 'var(--card-border)', 'border-radius': 'var(--card-radius)', 'box-shadow': 'var(--card-shadow)' });
    expect(rules.get('.event-badge')).toMatchObject({ 'text-transform': 'var(--label-transform)', 'letter-spacing': 'var(--label-tracking)' });
    expect(rules.get(':global(html[data-look="paper"] [data-ground="paper"]) h1')).toMatchObject({ 'font-size': '30px', 'font-weight': '650' });
  });

  it('the dot paints its own token, the severity word reads a text token, and the MOD badge the 12.5% badge tint', () => {
    expect(rules.get('.handler-dot')).toMatchObject({ background: 'var(--dot)' });
    expect(rules.get('.anomaly-level')).toMatchObject({ color: 'var(--warn-text)' });
    expect(rules.get('.anomaly--alert .anomaly-level')).toMatchObject({ color: 'var(--err-text)' });
    expect(rules.get('.trace-badge--mod')).toMatchObject({ background: 'color-mix(in srgb, var(--st-partial) 12.5%, transparent)' });
  });

  it('a handler row keeps its four figures together, after the name', async () => {
    render(Observability);
    const row = (await screen.findByText('sleep_handler')).closest('.handler-row')!;
    const stats = row.querySelector('.handler-stats')!;
    // No last-invoked time in this fixture, so three figures.
    expect([...stats.children].map((c) => text(c))).toEqual(['8/10', '3.2ms', '20.0% err']);
    expect(row.firstElementChild?.classList.contains('handler-dot')).toBe(true);
    expect(stats.previousElementSibling?.classList.contains('handler-name')).toBe(true);
  });

  it('source contract: at phone width the rows wrap (name, then the figures) instead of widening the page', () => {
    expect(rules.get('.handler-row')).toMatchObject({ 'flex-wrap': 'wrap' });
    expect(rules.get('.handler-name')).toMatchObject({ flex: '1 1 8rem', 'min-width': '0', 'overflow-wrap': 'anywhere' });
    expect(rules.get('.handler-stats')).toMatchObject({ display: 'flex', 'margin-left': 'auto' });
    expect(rules.get('.trace-row')).toMatchObject({ 'flex-wrap': 'wrap' });
    expect(rules.get('.trace-type')).toMatchObject({ flex: '1 1 8rem', 'min-width': '0' });
    expect(rules.get('.event-header')).toMatchObject({ 'flex-wrap': 'wrap' });
    expect(rules.get('.event-badge')).toMatchObject({ 'max-width': '100%', 'overflow-wrap': 'anywhere' });
    expect(rules.get('.event-badge')?.['white-space']).toBeUndefined();
    expect(rules.get('.snapshot-grid')).toMatchObject({ 'margin-top': '1rem' }); // the snapshot cards do not touch the chart card
  });

  it('no raw-HTML directive', () => {
    expect(SOURCE).not.toContain('{@html');
  });
});
