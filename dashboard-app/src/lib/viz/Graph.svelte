<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { getLook, look } from '../look';
  import { resolveColors } from '../chartColors';

  export type GraphNode = {
    id: string;
    type: string;
    label: string;
    category: string | null;
    edge_count: number;
    created_at: string | null;
  };

  let {
    elements,
    layout = { name: 'cose' },
    style = () => [],
    colorFor,
    onNodeClick,
    searchQuery = '',
    hiddenTypes = new Set<string>(),
    minEdges = 0,
  }: {
    elements: any[];
    layout?: any;
    /** The cytoscape stylesheet, built against the graph's own element (cytoscape cannot read var()). */
    style?: (el: Element) => any[];
    /** A node's (by type) or an edge's (by relation) colour as a token, `var(--x)`; the graph resolves it. */
    colorFor?: (kind: 'node' | 'edge', key: string) => string;
    onNodeClick?: (node: GraphNode) => void;
    searchQuery?: string;
    hiddenTypes?: Set<string>;
    minEdges?: number;
  } = $props();

  let el: HTMLDivElement;
  let cy: any = null; // plain let — NOT $state to avoid infinite update loop
  let painted = getLook(); // the look the stylesheet and colours were last resolved under

  const isEdge = (data: any) => data?.source != null;
  function colorOf(data: any): string | undefined {
    if (!colorFor) return undefined;
    return resolveColors(colorFor(isEdge(data) ? 'edge' : 'node', (isEdge(data) ? data.relation : data.type) ?? ''), el);
  }
  /** The elements with each one's colour (a `color` data field the stylesheet reads) resolved on this ground. */
  function paint(els: any[]): any[] {
    if (!colorFor) return els;
    return els.map((e) => ({ ...e, data: { ...e.data, color: colorOf(e.data) } }));
  }

  onMount(() => {
    painted = getLook();
    cy = cytoscape({ container: el, elements: paint(elements), layout, style: style(el) });

    cy.on('tap', 'node', (evt: any) => {
      const node = evt.target;
      onNodeClick?.({
        id: node.id(),
        type: node.data('type') ?? '',
        label: node.data('label') ?? '',
        category: node.data('category') ?? null,
        edge_count: node.data('edge_count') ?? 0,
        created_at: node.data('created_at') ?? null,
      });
    });

    return () => {
      cy?.destroy();
      cy = null;
    };
  });

  // Re-load elements when they change (e.g. initial data arrives). The only place the layout runs.
  $effect(() => {
    const e = elements;
    untrack(() => {
      if (cy) {
        cy.json({ elements: paint(e) });
        cy.layout(layout).run();
      }
    });
  });

  // A look switch: a new stylesheet and new colour data, in place. The layout is random (cose), so it never re-runs
  // here: every node keeps its position, and the filters and search marks (bypasses and classes) stay.
  $effect(() => {
    const now = $look;
    untrack(() => {
      if (!cy || now === painted) return;
      painted = now;
      cy.style(style(el));
      if (!colorFor) return;
      cy.batch(() => {
        cy.nodes().forEach((n: any) => n.data('color', colorOf({ type: n.data('type') })));
        cy.edges().forEach((e: any) => e.data('color', colorOf({ source: e.data('source'), relation: e.data('relation') })));
      });
    });
  });

  // Apply type/minEdges visibility filter
  $effect(() => {
    const hidden = hiddenTypes;
    const min = minEdges;
    if (!cy) return;
    cy.batch(() => {
      cy.nodes().forEach((n: any) => {
        const visible =
          !hidden.has(n.data('type')) &&
          (n.data('edge_count') ?? 0) >= min;
        n.style('display', visible ? 'element' : 'none');
      });
    });
  });

  // Apply search highlight
  $effect(() => {
    const q = searchQuery.toLowerCase().trim();
    if (!cy) return;
    cy.batch(() => {
      if (!q) {
        cy.elements().removeClass('faded searchHit');
        return;
      }
      const matchSet = new Set<string>();
      cy.nodes().forEach((n: any) => {
        const label = (n.data('label') ?? '').toLowerCase();
        if (label.includes(q)) {
          n.removeClass('faded').addClass('searchHit');
          matchSet.add(n.id());
        } else {
          n.removeClass('searchHit').addClass('faded');
        }
      });
      cy.edges().forEach((e: any) => {
        if (matchSet.has(e.source().id()) || matchSet.has(e.target().id())) {
          e.removeClass('faded');
        } else {
          e.addClass('faded');
        }
      });
    });
  });
</script>

<!-- touch-action:pan-y on wrapper so page scroll works; no touch-action:none on the canvas -->
<div class="cy-scroll">
  <div class="cy" bind:this={el}></div>
</div>

<style>
  .cy-scroll {
    touch-action: pan-y;
    width: 100%;
  }
  .cy {
    width: 100%;
    height: 60vh;
    min-height: 320px;
  }
  @media (max-width: 768px) {
    .cy {
      height: 50vh;
    }
  }
</style>
