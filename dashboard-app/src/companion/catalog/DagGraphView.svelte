<script lang="ts">
  // nous-core DagGraph — hand-rolled SVG wave layout (no vendor JS in the
  // companion entry). Nodes are placed in columns by longest-path depth from
  // the roots and colored by status; edges draw as lines with arrowheads.
  // Presentational: retry/cancel are ordinary Buttons in the surface tree.
  import { store } from '../store.svelte';
  import { resolveDynamic } from '../functions';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  interface DNode {
    name: string;
    status?: string;
    node_type?: string;
  }
  interface DEdge {
    from: string;
    to: string;
  }

  let {
    surfaceId,
    comp,
    scope = null,
  }: {
    surfaceId: string;
    comp: A2uiComponent;
    scope?: Scope | null;
    depth?: number;
    ancestors?: readonly string[];
  } = $props();

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  const nodes = $derived.by(() => {
    const raw = resolveDynamic(comp.nodes, ctx);
    return Array.isArray(raw) ? (raw as DNode[]).filter((n) => n && typeof n.name === 'string') : [];
  });
  const edges = $derived.by(() => {
    const raw = resolveDynamic(comp.edges, ctx);
    const names = new Set(nodes.map((n) => n.name));
    return Array.isArray(raw)
      ? (raw as DEdge[]).filter((e) => e && names.has(e.from) && names.has(e.to))
      : [];
  });

  // Longest-path depth via relaxation. Bounded by |nodes| passes, so a
  // cycle (invalid DAG, but renderer must not hang) just stops relaxing.
  const layout = $derived.by(() => {
    const depths = new Map<string, number>(nodes.map((n) => [n.name, 0]));
    for (let pass = 0; pass < nodes.length; pass++) {
      let changed = false;
      for (const e of edges) {
        const want = (depths.get(e.from) ?? 0) + 1;
        if (want > (depths.get(e.to) ?? 0) && want <= nodes.length) {
          depths.set(e.to, want);
          changed = true;
        }
      }
      if (!changed) break;
    }
    const columns = new Map<number, DNode[]>();
    for (const n of nodes) {
      const d = depths.get(n.name) ?? 0;
      const col = columns.get(d) ?? [];
      col.push(n);
      columns.set(d, col);
    }
    const nCols = Math.max(columns.size, 1);
    const maxRows = Math.max(...[...columns.values()].map((c) => c.length), 1);
    const colW = 150;
    const rowH = 64;
    const width = nCols * colW + 40;
    const height = maxRows * rowH + 30;
    const pos = new Map<string, { x: number; y: number }>();
    for (const [d, col] of columns) {
      col.forEach((n, i) => {
        // Wave: stagger rows in adjacent columns so long edges dodge nodes.
        const y = 40 + i * rowH + (d % 2 === 1 ? rowH / 3 : 0);
        pos.set(n.name, { x: 60 + d * colW, y });
      });
    }
    return { pos, width, height };
  });

  const STATUS_COLORS: Record<string, string> = {
    completed: 'var(--green)',
    running: 'var(--accent)',
    failed: 'var(--red)',
    pending: 'var(--muted)',
    ready: 'var(--muted)',
    cancelled: 'var(--muted)',
    skipped: 'var(--muted)',
  };

  function short(name: string): string {
    return name.length > 16 ? name.slice(0, 15) + '…' : name;
  }
</script>

<div class="dag">
  <svg viewBox="0 0 {layout.width} {layout.height}" role="img" aria-label="DAG graph">
    <defs>
      <marker id="arrow-{surfaceId}-{comp.id}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
        <path d="M0 0 L8 4 L0 8 z" fill="var(--muted)" />
      </marker>
    </defs>
    {#each edges as edge (edge.from + '→' + edge.to)}
      {@const a = layout.pos.get(edge.from)}
      {@const b = layout.pos.get(edge.to)}
      {#if a && b}
        <line
          x1={a.x + 12}
          y1={a.y}
          x2={b.x - 14}
          y2={b.y}
          stroke="var(--border)"
          stroke-width="1.5"
          marker-end="url(#arrow-{surfaceId}-{comp.id})"
        />
      {/if}
    {/each}
    {#each nodes as node (node.name)}
      {@const p = layout.pos.get(node.name)}
      {#if p}
        <g class="node" class:running={node.status === 'running'}>
          <circle cx={p.x} cy={p.y} r="10" fill={STATUS_COLORS[node.status ?? ''] ?? 'var(--muted)'}>
            <title>{node.name} — {node.status ?? 'pending'}{node.node_type ? ` (${node.node_type})` : ''}</title>
          </circle>
          <text x={p.x} y={p.y + 24} text-anchor="middle">{short(node.name)}</text>
        </g>
      {/if}
    {/each}
  </svg>
  <div class="legend">
    <span><i style:background="var(--green)"></i>completed</span>
    <span><i style:background="var(--accent)"></i>running</span>
    <span><i style:background="var(--red)"></i>failed</span>
    <span><i style:background="var(--muted)"></i>pending</span>
  </div>
</div>

<style>
  .dag {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.4rem;
    overflow-x: auto;
  }
  svg {
    display: block;
    width: 100%;
    height: auto;
    min-width: 280px;
  }
  text {
    fill: var(--muted);
    font-size: 11px;
  }
  .node.running circle {
    animation: pulse 1.6s ease-in-out infinite;
  }
  @keyframes pulse {
    50% {
      opacity: 0.45;
    }
  }
  .legend {
    display: flex;
    gap: 0.9rem;
    padding: 0.3rem 0.3rem 0.1rem;
    color: var(--muted);
    font-size: 0.75rem;
  }
  .legend i {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 0.3rem;
  }
</style>
