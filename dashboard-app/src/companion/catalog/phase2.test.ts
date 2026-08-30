import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import ConfidenceMeterView from './ConfidenceMeterView.svelte';
import DecisionCardView from './DecisionCardView.svelte';
import MemoryGraphView from './MemoryGraphView.svelte';
import DagGraphView from './DagGraphView.svelte';
import { store } from '../store.svelte';
import { transport } from '../transport';

// Phase 2 adapter smoke tests. The graphs are hand-rolled SVG (no vendor JS
// in the companion entry), so the assertions are on real SVG elements; the
// MemoryGraph test also proves the tap→expand→merge loop against a spied
// transport, because that loop is the reason the component exists.

const SURFACE = 'phase2-test-surface';

/** Let the async click handler chain settle, then flush Svelte's DOM work. */
async function settle(): Promise<void> {
  await new Promise((r) => setTimeout(r, 0));
  await tick();
}

function seedSurface(dataModel: Record<string, unknown>) {
  store.reset();
  store.apply(null, {
    version: 'v1.0',
    createSurface: {
      surfaceId: SURFACE,
      catalogId: 'nous-core',
      components: [],
      dataModel,
      metadata: { extensions: { com_nous_nonce: 'test-nonce' } },
    },
  } as never);
}

beforeEach(() => store.reset());
afterEach(() => vi.restoreAllMocks());

describe('ConfidenceMeterView', () => {
  function renderMeter(value: unknown) {
    seedSurface({});
    return render(ConfidenceMeterView, {
      props: { surfaceId: SURFACE, comp: { id: 'm', component: 'ConfidenceMeter', value } },
    });
  }

  it('renders the value as bar width and text', () => {
    const { container } = renderMeter(0.75);
    const fill = container.querySelector('.fill') as HTMLElement;
    expect(fill.style.width).toBe('75%');
    expect(fill.classList.contains('high')).toBe(true);
    expect(container.textContent).toContain('0.75');
  });

  it('clamps out-of-range and non-numeric values', () => {
    expect((renderMeter(3).container.querySelector('.fill') as HTMLElement).style.width).toBe(
      '100%',
    );
    expect((renderMeter('junk').container.querySelector('.fill') as HTMLElement).style.width).toBe(
      '0%',
    );
  });

  it('colors the low band red', () => {
    const { container } = renderMeter(0.2);
    expect(container.querySelector('.fill')?.classList.contains('low')).toBe(true);
  });
});

describe('DecisionCardView', () => {
  it('renders description, badges and a bound outcome', () => {
    seedSurface({ decisions: { 'd-1': 'success' } });
    const { container } = render(DecisionCardView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'c',
          component: 'DecisionCard',
          decisionId: 'd-1-aaaa-bbbb',
          description: 'Ship the eval harness',
          stakes: 'high',
          category: 'architecture',
          outcome: { path: '/decisions/d-1' },
        },
      },
    });

    expect(container.textContent).toContain('Ship the eval harness');
    expect(container.textContent).toContain('architecture');
    expect(container.querySelector('.outcome-success')?.textContent).toBe('success');
    expect(container.querySelector('.card')?.classList.contains('settled')).toBe(true);
  });

  it('stays unsettled while the bound outcome is pending', () => {
    seedSurface({ decisions: { 'd-1': 'pending' } });
    const { container } = render(DecisionCardView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'c',
          component: 'DecisionCard',
          decisionId: 'd-1',
          description: 'Ship it',
          outcome: { path: '/decisions/d-1' },
        },
      },
    });

    expect(container.querySelector('.card')?.classList.contains('settled')).toBe(false);
    expect(container.querySelector('.outcome')).toBeNull();
  });
});

describe('DagGraphView', () => {
  it('renders a circle per node colored by status and an edge per pair', () => {
    seedSurface({
      nodes: [
        { name: 'collect', status: 'completed' },
        { name: 'analyze', status: 'failed' },
        { name: 'report', status: 'pending' },
      ],
      edges: [
        { from: 'collect', to: 'analyze' },
        { from: 'analyze', to: 'report' },
      ],
    });
    const { container } = render(DagGraphView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'g',
          component: 'DagGraph',
          nodes: { path: '/nodes' },
          edges: { path: '/edges' },
        },
      },
    });

    const circles = container.querySelectorAll('g.node circle');
    expect(circles.length).toBe(3);
    expect(circles[0].getAttribute('fill')).toBe('var(--ok)');
    expect(circles[1].getAttribute('fill')).toBe('var(--crit)');
    expect(container.querySelectorAll('line').length).toBe(2);
    expect(container.textContent).toContain('analyze');
  });

  it('dedupes parallel edges between the same pair (duplicate keyed-each crash)', () => {
    seedSurface({
      nodes: [
        { name: 'a', status: 'completed' },
        { name: 'b', status: 'running' },
      ],
      edges: [
        { from: 'a', to: 'b' },
        { from: 'a', to: 'b' },
      ],
    });
    const { container } = render(DagGraphView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'g',
          component: 'DagGraph',
          nodes: { path: '/nodes' },
          edges: { path: '/edges' },
        },
      },
    });

    expect(container.querySelectorAll('line').length).toBe(1);
  });

  it('dedupes duplicate node names and sizes the SVG from the layout', () => {
    seedSurface({
      nodes: [
        { name: 'a', status: 'completed' },
        { name: 'a', status: 'failed' },
        { name: 'b', status: 'running' },
      ],
      edges: [{ from: 'a', to: 'b' }],
    });
    const { container } = render(DagGraphView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'g',
          component: 'DagGraph',
          nodes: { path: '/nodes' },
          edges: { path: '/edges' },
        },
      },
    });

    expect(container.querySelectorAll('g.node circle').length).toBe(2);
    // Two depth columns → 2*150+40 = 340; min-width must track the layout so
    // wide DAGs scroll inside the wrapper instead of scaling to illegibility.
    const svg = container.querySelector('svg') as SVGSVGElement;
    expect(svg.style.minWidth).toBe('340px');
  });

  it('drops edges that reference unknown nodes instead of crashing', () => {
    seedSurface({
      nodes: [{ name: 'only', status: 'running' }],
      edges: [{ from: 'only', to: 'ghost' }],
    });
    const { container } = render(DagGraphView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'g',
          component: 'DagGraph',
          nodes: { path: '/nodes' },
          edges: { path: '/edges' },
        },
      },
    });

    expect(container.querySelectorAll('g.node circle').length).toBe(1);
    expect(container.querySelectorAll('line').length).toBe(0);
  });
});

describe('MemoryGraphView', () => {
  const FOCUS = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
  const NEIGHBOR = '11111111-2222-4333-8444-555555555555';

  function renderGraph() {
    seedSurface({
      nodes: [{ id: FOCUS, type: 'fact', label: 'focus fact' }],
      edges: [],
      focus: FOCUS,
    });
    return render(MemoryGraphView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'g',
          component: 'MemoryGraph',
          nodes: { path: '/nodes' },
          edges: { path: '/edges' },
          focusNodeId: FOCUS,
        },
      },
    });
  }

  it('renders the focus node larger than the ring nodes', async () => {
    const { container } = renderGraph();
    const circle = container.querySelector('g.node circle')!;
    expect(circle.getAttribute('r')).toBe('14');
  });

  it('dedupes duplicate seed node ids (duplicate keyed-each crash)', () => {
    seedSurface({
      nodes: [
        { id: FOCUS, type: 'fact', label: 'focus fact' },
        { id: FOCUS, type: 'fact', label: 'focus fact again' },
      ],
      edges: [],
      focus: FOCUS,
    });
    const { container } = render(MemoryGraphView, {
      props: {
        surfaceId: SURFACE,
        comp: {
          id: 'g',
          component: 'MemoryGraph',
          nodes: { path: '/nodes' },
          edges: { path: '/edges' },
          focusNodeId: FOCUS,
        },
      },
    });

    expect(container.querySelectorAll('g.node circle').length).toBe(1);
  });

  it('tap → callAgentFunction → merges the neighborhood into the data model', async () => {
    const spy = vi.spyOn(transport, 'callAgentFunction').mockResolvedValue({
      ok: true,
      message: '',
      value: {
        nodes: [{ id: NEIGHBOR, type: 'decision', label: 'a neighbor' }],
        edges: [{ source: FOCUS, target: NEIGHBOR, relation: 'informed_by', weight: 0.7 }],
      },
    });
    const { container } = renderGraph();

    await fireEvent.click(container.querySelector('g.node')!);
    await settle();

    expect(spy).toHaveBeenCalledWith(SURFACE, 'expandGraphNode', {
      nodeId: FOCUS,
      nodeType: 'fact',
    });
    // The merge mutates the surface data model, which feeds back into the
    // component through the same path binding a server push would use.
    const dm = store.surfaces[SURFACE].dataModel as { nodes: unknown[]; edges: unknown[] };
    expect(dm.nodes.length).toBe(2);
    expect(dm.edges.length).toBe(1);
    const texts = [...container.querySelectorAll('text')].map((t) => t.textContent);
    expect(texts).toContain('a neighbor');
    expect(container.querySelectorAll('line').length).toBe(1);
  });

  it('re-tapping an expanded node does not duplicate or re-fetch', async () => {
    const spy = vi.spyOn(transport, 'callAgentFunction').mockResolvedValue({
      ok: true,
      message: '',
      value: { nodes: [{ id: NEIGHBOR, type: 'decision', label: 'a neighbor' }], edges: [] },
    });
    const { container } = renderGraph();

    await fireEvent.click(container.querySelector('g.node')!);
    await settle();
    await fireEvent.click(container.querySelector('g.node')!);
    await settle();

    expect(spy).toHaveBeenCalledTimes(1);
    const dm = store.surfaces[SURFACE].dataModel as { nodes: unknown[] };
    expect(dm.nodes.length).toBe(2);
  });

  it('paints the error inline when the call fails', async () => {
    vi.spyOn(transport, 'callAgentFunction').mockResolvedValue({
      ok: false,
      message: 'too many calls; slow down',
    });
    const { container } = renderGraph();

    await fireEvent.click(container.querySelector('g.node')!);
    await settle();

    expect(container.querySelector('[role="alert"]')?.textContent).toContain('too many calls');
    const dm = store.surfaces[SURFACE].dataModel as { nodes: unknown[] };
    expect(dm.nodes.length).toBe(1);
  });
});
