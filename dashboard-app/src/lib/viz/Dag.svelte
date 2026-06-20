<script lang="ts">
  /**
   * Dag.svelte — Wave-based DAG graph visualization using D3 SVG rendering.
   *
   * Faithful port of the static SVG layout from static/dashboard/js/dag.js
   * (showDagGraph). Uses D3 only for DOM selection/append — no force simulation.
   *
   * Props:
   *   nodes  — DagNode[]:  id, name, status, node_type, wave (number), plus optional
   *             started_at, completed_at, tokens_used, description, result, error,
   *             completion_check, check_attempts
   *   edges  — DagEdge[]:  from_node_id, to_node_id, edge_type (optional string)
   *   onNodeClick — optional callback when user clicks a node
   */
  import { onMount } from 'svelte';

  export type DagNode = {
    id: string;
    name: string;
    status: string;
    node_type?: string;
    wave?: number;
    started_at?: string;
    completed_at?: string;
    tokens_used?: number;
    description?: string;
    result?: string;
    error?: string;
    completion_check?: string;
    check_attempts?: number;
  };

  export type DagEdge = {
    from_node_id: string;
    to_node_id: string;
    edge_type?: string;
  };

  let {
    nodes = [],
    edges = [],
    onNodeClick,
  }: {
    nodes: DagNode[];
    edges: DagEdge[];
    onNodeClick?: (node: DagNode) => void;
  } = $props();

  let container: HTMLDivElement;
  let rendered = false; // plain flag — NOT $state

  // Layout constants (matches dag.js)
  const MARGIN_LEFT = 80;
  const MARGIN_TOP = 50;
  const WAVE_SPACING = 160;
  const NODE_SPACING = 100;
  const NODE_RADIUS = 22;

  const STATUS_COLORS: Record<string, string> = {
    pending: '#6b6b8a',
    ready: '#22d3ee',
    running: '#fbbf24',
    awaiting_check: '#f59e0b',
    completed: '#4ade80',
    failed: '#f87171',
    blocked: '#991b1b',
    cancelled: '#4b4b5a',
  };

  function renderGraph(nodeList: DagNode[], edgeList: DagEdge[]): void {
    if (!container) return;

    // Remove any previous SVG
    const prev = container.querySelector('svg');
    if (prev) prev.remove();

    if (nodeList.length === 0) return;

    // Group nodes by wave
    const waveGroups: Record<number, DagNode[]> = {};
    let maxWave = 0;
    for (const n of nodeList) {
      const w = n.wave ?? 0;
      if (!waveGroups[w]) waveGroups[w] = [];
      waveGroups[w].push(n);
      if (w > maxWave) maxWave = w;
    }

    // Compute SVG dimensions
    let maxNodesInWave = 0;
    for (let w = 0; w <= maxWave; w++) {
      const count = (waveGroups[w] ?? []).length;
      if (count > maxNodesInWave) maxNodesInWave = count;
    }
    const width = MARGIN_LEFT + (maxWave + 1) * WAVE_SPACING + 60;
    const height = MARGIN_TOP + maxNodesInWave * NODE_SPACING + 40;

    // Assign positions
    const nodePositions: Record<string, { x: number; y: number }> = {};
    for (let wv = 0; wv <= maxWave; wv++) {
      const group = waveGroups[wv] ?? [];
      const groupHeight = group.length * NODE_SPACING;
      const startY =
        MARGIN_TOP + (height - MARGIN_TOP - 40 - groupHeight) / 2 + NODE_SPACING / 2;
      group.forEach((n, i) => {
        nodePositions[n.id] = {
          x: MARGIN_LEFT + wv * WAVE_SPACING,
          y: startY + i * NODE_SPACING,
        };
      });
    }

    const svg = d3
      .select(container)
      .append('svg')
      .attr('width', width)
      .attr('height', height)
      .style('display', 'block')
      .style('margin', '0 auto');

    // Arrow marker
    svg
      .append('defs')
      .append('marker')
      .attr('id', 'dag-arrow')
      .attr('viewBox', '0 0 10 10')
      .attr('refX', 10)
      .attr('refY', 5)
      .attr('markerWidth', 8)
      .attr('markerHeight', 8)
      .attr('orient', 'auto')
      .append('path')
      .attr('d', 'M0,0 L10,5 L0,10 Z')
      .attr('fill', 'var(--muted, #6b6b8a)');

    // Wave lane labels
    for (let wl = 0; wl <= maxWave; wl++) {
      svg
        .append('text')
        .attr('class', 'dag-wave-label')
        .attr('x', MARGIN_LEFT + wl * WAVE_SPACING)
        .attr('y', 20)
        .attr('text-anchor', 'middle')
        .attr('fill', 'var(--muted, #6b6b8a)')
        .attr('font-size', '11px')
        .text(`Wave ${wl}`);
    }

    // Edges
    for (const edge of edgeList) {
      const from = nodePositions[edge.from_node_id];
      const to = nodePositions[edge.to_node_id];
      if (!from || !to) continue;
      svg
        .append('line')
        .attr('class', `dag-edge ${edge.edge_type ?? 'dependency'}`)
        .attr('x1', from.x + NODE_RADIUS)
        .attr('y1', from.y)
        .attr('x2', to.x - NODE_RADIUS)
        .attr('y2', to.y)
        .attr('stroke', 'var(--muted, #6b6b8a)')
        .attr('stroke-width', 1.5)
        .attr('marker-end', 'url(#dag-arrow)');
    }

    // Nodes
    const nodeGroups = svg
      .selectAll('.dag-node')
      .data(nodeList)
      .enter()
      .append('g')
      .attr('class', 'dag-node')
      .attr('transform', (d: DagNode) => {
        const pos = nodePositions[d.id];
        return `translate(${pos.x},${pos.y})`;
      })
      .style('cursor', 'pointer')
      .on('click', (_event: MouseEvent, d: DagNode) => {
        onNodeClick?.(d);
      });

    nodeGroups.each(function (this: SVGGElement, d: DagNode) {
      const g = d3.select(this);
      const color = STATUS_COLORS[d.status] ?? '#6b6b8a';
      const isRunning = d.status === 'running';
      const isAwaiting = d.status === 'awaiting_check';
      const anim = isRunning
        ? 'pulse-running 1.5s infinite'
        : isAwaiting
          ? 'pulse-awaiting 3s infinite'
          : 'none';

      const r = NODE_RADIUS;
      if (d.node_type === 'check') {
        // Diamond
        g.append('polygon')
          .attr('points', `0,-${r} ${r},0 0,${r} -${r},0`)
          .attr('fill', `${color}30`)
          .attr('stroke', color)
          .attr('stroke-width', 2)
          .style('animation', anim);
      } else if (d.node_type === 'gate') {
        // Hexagon
        const hex = Array.from({ length: 6 }, (_, i) => {
          const angle = (Math.PI / 3) * i - Math.PI / 6;
          return `${Math.cos(angle) * r},${Math.sin(angle) * r}`;
        }).join(' ');
        g.append('polygon')
          .attr('points', hex)
          .attr('fill', `${color}30`)
          .attr('stroke', color)
          .attr('stroke-width', 2)
          .style('animation', anim);
      } else if (d.node_type === 'callback') {
        // Triangle
        g.append('polygon')
          .attr('points', `0,-${r} ${r},${r * 0.8} -${r},${r * 0.8}`)
          .attr('fill', `${color}30`)
          .attr('stroke', color)
          .attr('stroke-width', 2)
          .style('animation', anim);
      } else {
        // Circle (subtask — default)
        g.append('circle')
          .attr('r', r)
          .attr('fill', `${color}30`)
          .attr('stroke', color)
          .attr('stroke-width', 2)
          .style('animation', anim);
      }

      // Label below node
      g.append('text')
        .attr('class', 'dag-node-label')
        .attr('dy', r + 16)
        .attr('text-anchor', 'middle')
        .attr('fill', 'var(--text, #e2e8f0)')
        .attr('font-size', '11px')
        .text(d.name.length > 12 ? d.name.slice(0, 11) + '…' : d.name);
    });

    rendered = true;
  }

  onMount(() => {
    renderGraph(nodes, edges);
    return () => {
      // Teardown: remove the SVG and stop any D3 touch/zoom handlers
      if (container) {
        const svg = d3.select(container).select('svg');
        if (!svg.empty()) {
          // Detach zoom touch handlers to avoid blocking page scroll
          svg.on('touchstart.zoom', null).on('touchmove.zoom', null);
          svg.remove();
        }
      }
      rendered = false;
    };
  });

  $effect(() => {
    // Re-render when nodes or edges change, but only after mount
    const n = nodes;
    const e = edges;
    if (rendered) {
      renderGraph(n, e);
    }
  });
</script>

<!-- touch-action:pan-y so page scroll works; bounded height -->
<div class="dag-scroll">
  <div class="dag-container" bind:this={container}></div>
</div>

<style>
  .dag-scroll {
    touch-action: pan-y;
    width: 100%;
    overflow-x: auto;
  }
  .dag-container {
    width: 100%;
    min-height: 320px;
    max-height: 70vh;
    overflow: auto;
  }
  @media (max-width: 768px) {
    .dag-container {
      max-height: 50vh;
    }
  }
</style>
