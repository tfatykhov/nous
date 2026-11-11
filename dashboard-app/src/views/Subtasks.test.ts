import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Subtasks from './Subtasks.svelte';
import { ROOT_SEL, block, parseBlocks, styleBlocks } from '../lib/testing/css';

// Redesign PR 3: Subtasks on paper. The outcome colours are status tokens (`--st-*`, `--oc-*`) that the Chart
// component resolves against the element its canvas sits in (with no stylesheet, jsdom: the :root values).

const PAPER = block(parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')), ROOT_SEL, 50);
const TOKEN: Record<string, string> = {
  completed: '--st-success', incomplete_blocked: '--oc-blocked', incomplete_no_terminal: '--oc-failed',
  validation_failed: '--oc-failed', timed_out: '--oc-timeout', errored: '--oc-errored', cancelled: '--st-neutral', unknown: '--oc-unknown',
};
const OUTCOMES = Object.keys(TOKEN);
const ROOT_OF = (o: string) => PAPER[TOKEN[o]];
const per = (f: (o: string, i: number) => unknown) => Object.fromEntries(OUTCOMES.map((o, i) => [o, f(o, i)]));

const SUBTASKS = {
  window_hours: 24,
  totals: { total_terminal: 36, by_outcome: per((_o, i) => i + 1), empty_rate: 0.1, retry_rate: 0.2 },
  tokens_by_outcome: per((_o, i) => ({ mean_total_tokens: 1000 * (i + 1), mean_tool_calls: 2, n: i + 1 })),
  top_failing_tasks: [{ task_prefix: 'Send the weekly report', failures: 2, total: 5, failure_rate: 0.4 }],
  dag_correlation: { completed: 3 },
  recent_outcomes: [{ id: 'r1', completed_at: '2026-11-10T09:00:00+00:00', final_outcome: 'timed_out', attempts: 2,
    tokens_in: 1000, tokens_out: 200, tool_calls_made: 3, task: 'Summarise the inbox' }],
  daily_trend: [
    { date: '2026-11-09', by_outcome: { completed: 3, timed_out: 1 } },
    { date: '2026-11-10', by_outcome: { completed: 4, errored: 1, unknown: 2 } },
  ],
};
const ACTIVITY = {
  events: [], censor_stats: { total: 0, active: 0, auto_created: 0, manual_created: 0, total_activations_7d: 0, false_positives_7d: 0, top_censors: [] },
  schedule_stats: { total: 0, active: 0, fires_7d: 0, next_fires: [] },
  sleep_stats: { total_sleeps: 0, last_sleep: null, facts_created: 0, procedures_created: 0, censors_retired: 0 },
};

type Cfg = { type: string; data: { labels: string[]; datasets: Record<string, any>[] }; options: Record<string, any> };
let configs: Cfg[] = [];
function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.startsWith('/dashboard/subtasks') ? SUBTASKS : url.startsWith('/dashboard/activity') ? ACTIVITY : null;
    if (!body) throw new Error(`unexpected ${url}`);
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
  configs = [];
  vi.stubGlobal('Chart', class {
    data = {}; options = {};
    constructor(_el: unknown, cfg: Cfg) { configs.push(cfg); }
    destroy() {}
    update() {}
  });
}
function paperMain(): HTMLElement {
  const main = document.createElement('main');
  main.classList.add('page-ground');
  for (const [k, v] of Object.entries(PAPER)) if (k.startsWith('--')) main.style.setProperty(k, v);
  document.body.appendChild(main);
  return main;
}
/** `#rrggbb` at an alpha, as Chart.js reads it (channels and alpha; spacing aside). */
const rgba = (hex: string, a: number) => `rgba(${[1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)).join(',')},${a})`;
const norm = (c: unknown) => String(c).replace(/\s+/g, '');
const chart = (t: string) => configs.find((c) => c.type === t)!;

beforeEach(install);
afterEach(() => { cleanup(); vi.unstubAllGlobals(); document.body.innerHTML = ''; });

describe('Subtasks: outcome colours are status tokens', () => {
  it('the doughnut and the tokens bars take the eight outcome tokens, and every outcome is named (no stylesheet)', async () => {
    render(Subtasks);
    await waitFor(() => expect(configs).toHaveLength(3));
    const doughnut = chart('doughnut');
    expect(doughnut.data.labels).toEqual(OUTCOMES); // the legend names every outcome: colour is never alone
    // At phone width (200 px charts) eight 40 px swatches made a fourth legend row that Chart.js cut off.
    expect(doughnut.options.plugins.legend.labels.boxWidth).toBe(12);
    expect(chart('line').options.plugins.legend.labels.boxWidth).toBe(12); // the trend's eight entries left a 20 px plot
    expect(doughnut.data.datasets[0].backgroundColor).toEqual(OUTCOMES.map(ROOT_OF));
    expect(chart('bar').data.datasets[0].backgroundColor).toEqual(OUTCOMES.map(ROOT_OF));
  });

  it('the daily trend draws one dataset per outcome, its line in the outcome colour over a 20% tint (no stylesheet)', async () => {
    render(Subtasks);
    await waitFor(() => expect(configs).toHaveLength(3));
    const sets = chart('line').data.datasets;
    expect(sets.map((d) => d.label)).toEqual(['completed', 'errored', 'timed_out', 'unknown']);
    for (const d of sets) {
      expect(d.borderColor, d.label).toBe(ROOT_OF(d.label));
      expect(norm(d.backgroundColor), d.label).toBe(rgba(ROOT_OF(d.label), 0.2)); // the old `${hex}33`
    }
  });

  it('paper: the same datasets take the paper status and outcome values', async () => {
    render(Subtasks, { target: paperMain() });
    await waitFor(() => expect(configs).toHaveLength(3));
    expect(chart('doughnut').data.datasets[0].backgroundColor).toEqual(OUTCOMES.map((o) => PAPER[TOKEN[o]]));
    const line = chart('line').data.datasets.find((d) => d.label === 'completed')!;
    expect(line.borderColor).toBe(PAPER['--st-success']);
    expect(norm(line.backgroundColor)).toBe(rgba(PAPER['--st-success'], 0.2));
    expect(chart('line').options.scales.x.ticks.color).toBe(PAPER['--chart-text']);
  });

  it('source contract: the header may wrap, so the paper title, the window select and the badge fit a phone', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'Subtasks.svelte'), 'utf-8'));
    expect(css).toMatch(/\.view-head \{[^}]*flex-wrap: wrap;/);
  });

  it('the failing-tasks and recent tables keep every column', async () => {
    render(Subtasks);
    const failing = (await screen.findByRole('heading', { level: 2, name: 'Top failing tasks' })).closest('section') as HTMLElement;
    expect(within(failing).getAllByRole('columnheader').map((th) => th.textContent)).toEqual(['Task (first 80 chars)', 'Failures', 'Total', 'Rate']);
    const recent = screen.getByRole('heading', { level: 2, name: 'Recent terminal subtasks' }).closest('section') as HTMLElement;
    expect(within(recent).getAllByRole('columnheader').map((th) => th.textContent))
      .toEqual(['Completed', 'Outcome', 'Attempts', 'Tokens', 'Tool calls', 'Task']);
  });
});
