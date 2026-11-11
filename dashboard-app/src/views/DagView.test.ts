import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import DagView from './DagView.svelte';
import Dag from '../lib/viz/Dag.svelte';
import { DAG_NODE_STATUSES } from '../lib/status';
import { setLook } from '../lib/look';
import { PAPER_SEL, parseBlocks, styleBlocks } from '../lib/testing/css';

// Redesign PR 3: Workflows on paper. Status badges and DAG nodes are painted by token (`--st-*`, color-mix tints), so
// the paper look restyles them and the dark look keeps today's colours; the budget chart's colours are tokens the
// Chart component resolves against its ground.

const PAPER = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')).get(PAPER_SEL) ?? {};
const HOSTILE = '<img src=x onerror=alert(1)>' + 'n'.repeat(300);
const dash = (s: string) => s.replaceAll('_', '-');

const node = (id: string, name: string, status: string, over: Record<string, unknown> = {}) => ({
  id, name, description: '', node_type: 'subtask', wave: 0, status, result: '', error: '', tokens_used: 0,
  started_at: null, completed_at: null, ...over,
});
const EVERY = DAG_NODE_STATUSES.map((s, i) => node(`n${i}`, `step-${s}`, s, { wave: i % 3 }));

const DAG = {
  active_dags: [{
    id: 'd1', name: 'weekly-report', description: '', status: 'running', source: 'agent',
    created_at: '2026-11-10T10:00:00+00:00', started_at: null, token_budget: 50000, tokens_consumed: 20000,
    waiting: 0, held_reason: null, edges: [{ from_node_id: 'n0', to_node_id: 'n1', edge_type: null }],
    nodes: [...EVERY, node('nh', HOSTILE, 'failed')],
  }, {
    id: 'd2', name: 'approve-mail', description: '', status: 'running', source: 'agent',
    created_at: '2026-11-10T10:00:00+00:00', started_at: null, token_budget: 0, tokens_consumed: 0,
    waiting: 1, held_reason: null, edges: [],
    nodes: [node('q1', 'approve-send', 'awaiting_input', { node_type: 'approval' })],
  }],
  recent_dags: [{ id: 'r1', name: 'invoice-followup', status: 'failed', source: 'agent', created_at: null,
    completed_at: '2026-11-09T09:00:00+00:00', token_budget: 0, tokens_consumed: 0, result_summary: 'done', postmortem: 'why',
    node_count: 3, completed_count: 1, stopped_by: null, stops: [] }],
  waiting_on_you: [],
  stats: { active_count: 2, nodes_completed_24h: 3, success_rate: 0.9, avg_completion_seconds: 60, waiting_count: 1 },
  phase2_signals: {},
};

type Cfg = { type: string; data: { labels: string[]; datasets: Record<string, any>[] }; options: Record<string, any> };
let configs: Cfg[] = [];
function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (!url.startsWith('/dashboard/dag')) throw new Error(`unexpected ${url}`);
    return { ok: true, status: 200, json: async () => DAG } as Response;
  }));
  configs = [];
  vi.stubGlobal('Chart', class {
    data = {}; options = {};
    constructor(_el: unknown, cfg: Cfg) { configs.push(cfg); }
    destroy() {}
    update() {}
  });
}

// d3 is a CDN global: a fake that records every attr and style call, with the datum of the node it was made for.
type Call = { tag: string; kind: 'attr' | 'style' | 'text'; k: string; v: unknown; id?: string };
let calls: Call[] = [];
function sel(tag: string, datum?: { id: string }, data?: unknown[]): any {
  const s: any = {
    _data: data,
    append: (t: string) => sel(t, datum, s._data),
    attr: (k: string, v: unknown) => { calls.push({ tag, kind: 'attr', k, v: typeof v === 'function' ? '[fn]' : v, id: datum?.id }); return s; },
    style: (k: string, v: unknown) => { calls.push({ tag, kind: 'style', k, v, id: datum?.id }); return s; },
    text: (v: unknown) => { calls.push({ tag, kind: 'text', k: 'text', v, id: datum?.id }); return s; },
    on: () => s, enter: () => s, remove: () => s, empty: () => true,
    select: () => sel('?'), selectAll: () => s,
    data: (list: unknown[]) => { s._data = list; return s; },
    each: (fn: (this: unknown, d: unknown) => void) => { for (const d of s._data ?? []) fn.call({ __datum: d }, d); return s; },
  };
  return s;
}
function installD3() {
  calls = [];
  vi.stubGlobal('d3', { select: (x: { __datum?: { id: string } }) => (x && x.__datum ? sel('g', x.__datum) : sel('root')) });
}

function paperMain(): HTMLElement {
  const main = document.createElement('main');
  main.classList.add('page-ground');
  for (const [k, v] of Object.entries(PAPER)) if (k.startsWith('--')) main.style.setProperty(k, v);
  document.body.appendChild(main);
  return main;
}

beforeEach(() => { install(); installD3(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); setLook('dark'); document.body.innerHTML = ''; });

describe('Workflows: status by token', () => {
  it('every DAG status renders a badge from its --st-* token with a color-mix tint, never a hex', async () => {
    const { container } = render(DagView);
    await fireEvent.click((await screen.findAllByRole('button', { name: 'View Graph' }))[0]);
    const badges = Array.from(container.querySelectorAll<HTMLElement>('.node-list .status-badge'));
    for (const s of DAG_NODE_STATUSES) {
      const b = badges.find((x) => x.textContent === s);
      expect(b, s).toBeTruthy();
      const style = b!.getAttribute('style') ?? '';
      expect(style, s).toContain(`var(--st-${dash(s)})`);
      expect(style, s).toContain('color-mix(in srgb');
      expect(style, s).not.toContain('#');
    }
  });

  it('a DAG waiting on you shows its badge in the waiting colour, and its Now pill names the question', async () => {
    const { container } = render(DagView);
    const badge = await waitFor(() => {
      const b = Array.from(container.querySelectorAll<HTMLElement>('.dag-table .status-badge')).find((x) => x.textContent === 'waiting on you');
      if (!b) throw new Error('no waiting badge');
      return b;
    });
    expect(badge.style.color).toBe('var(--waiting)');
    expect(container.querySelector('.now-waiting')?.textContent).toBe('Question · approve-send');
  });

  it('source contract: the Now pills read the wait and running tokens, have room, and round as blocks when wrapped', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'DagView.svelte'), 'utf-8'));
    expect(css).toMatch(/\.now-waiting \{[^}]*color: var\(--wait-ink\); background: var\(--wait-bg\);/);
    // At phone width the Now column squeezed each pill over four lines; a pill radius on a block of text cut it.
    expect(css).toMatch(/\.now-cell \{ min-width: 10rem; \}/);
    expect(css).toMatch(/\.now-waiting \{[^}]*border-radius: 0\.75rem;/);
    expect(css).toMatch(/\.now-held \{[^}]*border-radius: 0\.75rem;/);
  });

  it('a hostile node name is text', async () => {
    const { container } = render(DagView);
    await fireEvent.click((await screen.findAllByRole('button', { name: 'View Graph' }))[0]);
    expect(container.textContent).toContain(HOSTILE);
    expect(container.querySelector('.node-list img')).toBeNull();
  });

  it('source contract: a node button may wrap its name anywhere, with the small card radius', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'DagView.svelte'), 'utf-8'));
    expect(css).toMatch(/\.node-btn \.mono \{[^}]*overflow-wrap: anywhere;/);
    // Wrapped over several lines, a pill (999px on paper) cuts the text at its corners: the button takes the small card radius.
    expect(css).toMatch(/\.node-btn \{[^}]*border-radius: var\(--card-radius-sm\);/);
  });
});

describe('Workflows: the DAG graph', () => {
  it('a node fills with its status tint (color-mix, 18.8%) and strokes with its --st-* token; nothing is a hex', () => {
    const nodes = EVERY.map(({ id, name, status, node_type, wave }) => ({ id, name, status, node_type, wave }));
    render(Dag, { props: { nodes, edges: [{ from_node_id: 'n0', to_node_id: 'n1' }] } });
    for (const n of EVERY) {
      const shape = calls.filter((c) => c.id === n.id && c.tag !== 'text' && c.kind === 'style');
      const v = `var(--st-${dash(n.status)})`;
      expect(shape.find((c) => c.k === 'fill')?.v, n.status).toBe(`color-mix(in srgb, ${v} 18.8%, transparent)`);
      expect(shape.find((c) => c.k === 'stroke')?.v, n.status).toBe(v);
    }
    const colours = calls.filter((c) => ['fill', 'stroke'].includes(c.k));
    expect(colours.length).toBeGreaterThan(EVERY.length * 2);
    expect(colours.filter((c) => c.kind === 'attr')).toEqual([]); // var() and color-mix only through style
    expect(colours.filter((c) => String(c.v).includes('#'))).toEqual([]);
  });
});

describe('Workflows: a node\'s status is a word, never colour alone (review I2)', () => {
  it('every node shows its status in words under its name, and its title names it, its type and its status', () => {
    const nodes = EVERY.map(({ id, name, status, node_type, wave }) => ({ id, name, status, node_type, wave }));
    render(Dag, { props: { nodes, edges: [] } });
    for (const n of nodes) {
      const mine = calls.filter((c) => c.id === n.id && c.kind === 'text');
      const label = n.name.length > 12 ? n.name.slice(0, 11) + '…' : n.name;
      const word = n.status.replaceAll('_', ' ');
      expect([...new Set(mine.filter((c) => c.tag === 'text').map((c) => c.v))], n.status).toEqual([label, word]);
      expect([...new Set(mine.filter((c) => c.tag === 'title').map((c) => c.v))], n.status).toEqual([`${n.name}: ${n.node_type}, ${word}`]);
    }
    // The status line is muted text, by token.
    const statusFill = calls.filter((c) => c.tag === 'text' && c.k === 'fill').map((c) => c.v);
    expect(statusFill).toContain('var(--muted)');
  });
});

describe('Workflows: no dead animation (review M9)', () => {
  it('no node names an animation, since no stylesheet defines its keyframes; the status word is the cue', () => {
    const nodes = EVERY.map(({ id, name, status, node_type, wave }) => ({ id, name, status, node_type, wave }));
    render(Dag, { props: { nodes, edges: [] } });
    expect(calls.filter((c) => c.k === 'animation')).toEqual([]);
    // Guard the premise: if pulse keyframes are ever added, the animation can come back with them.
    const sources = import.meta.glob(['/src/**/*.{svelte,css}', '!/src/**/*.test.ts'], { query: '?raw', import: 'default', eager: true });
    expect(Object.keys(sources).length).toBeGreaterThan(50);
    expect(Object.entries(sources).filter(([, s]) => /@keyframes\s+pulse-(running|awaiting)/.test(String(s))).map(([f]) => f)).toEqual([]);
  });
});

describe('Workflows: no invented glyph in the graph', () => {
  it('an approval node is a plain rounded square named by its label below: no question mark (ruling 4)', () => {
    const nodes = [{ id: 'a1', name: 'approve-send', status: 'awaiting_input', node_type: 'approval', wave: 0 }];
    render(Dag, { props: { nodes, edges: [] } });
    const mine = calls.filter((c) => c.id === 'a1');
    // (The graph draws on mount and again from its effect: compare the set of texts.)
    expect([...new Set(mine.filter((c) => c.kind === 'text' && c.tag === 'text').map((c) => c.v))]).toEqual(['approve-send', 'awaiting input']);
    expect(mine.some((c) => c.tag === 'rect' && c.k === 'stroke' && c.v === 'var(--st-awaiting-input)')).toBe(true);
    expect(readFileSync(join(__dirname, '..', 'lib', 'viz', 'Dag.svelte'), 'utf-8')).not.toMatch(/\.text\('\?'\)/);
  });
});

describe('Workflows: the token budget chart', () => {
  const ds = (label: string) => configs.find((c) => c.type === 'bar')!.data.datasets.find((d) => d.label === label)!;
  const norm = (c: unknown) => String(c).replace(/\s+/g, '');

  it('dark: consumed, remaining and the axes are today\'s colours exactly', async () => {
    render(DagView);
    await waitFor(() => expect(configs.filter((c) => c.type === 'bar')).toHaveLength(1));
    expect(norm(ds('Consumed').backgroundColor)).toBe('rgba(251,191,36,0.7)');
    expect(norm(ds('Budget').backgroundColor)).toBe('rgba(255,255,255,0.08)');
    const o = configs.find((c) => c.type === 'bar')!.options;
    expect(o.plugins.legend.labels.color).toBe('#94a3b8');
    expect([o.scales.x.ticks.color, o.scales.y.ticks.color]).toEqual(['#94a3b8', '#94a3b8']);
    expect([o.scales.x.grid.color, o.scales.y.grid.color]).toEqual(['#1e293b', '#1e293b']);
  });

  it('paper: the same chart takes the paper values', async () => {
    setLook('paper');
    render(DagView, { target: paperMain() });
    await waitFor(() => expect(configs.filter((c) => c.type === 'bar')).toHaveLength(1));
    expect(ds('Consumed').backgroundColor).toBe(PAPER['--stack-bar']);
    expect(ds('Budget').backgroundColor).toBe(PAPER['--track-strong']);
    const o = configs.find((c) => c.type === 'bar')!.options;
    expect(o.plugins.legend.labels.color).toBe(PAPER['--chart-text']);
    expect(o.scales.y.grid.color).toBe(PAPER['--chart-grid']);
  });
});
