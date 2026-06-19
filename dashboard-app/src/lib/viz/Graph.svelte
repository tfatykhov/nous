<script lang="ts">
  import { onMount } from 'svelte';

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
    style = [],
    onNodeClick,
    searchQuery = '',
    hiddenTypes = new Set<string>(),
    minEdges = 0,
  }: {
    elements: any[];
    layout?: any;
    style?: any[];
    onNodeClick?: (node: GraphNode) => void;
    searchQuery?: string;
    hiddenTypes?: Set<string>;
    minEdges?: number;
  } = $props();

  let el: HTMLDivElement;
  let cy: any = null; // plain let — NOT $state to avoid infinite update loop

  onMount(() => {
    cy = cytoscape({ container: el, elements, layout, style });

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

  // Re-load elements when they change (e.g. initial data arrives)
  $effect(() => {
    const e = elements;
    if (cy) {
      cy.json({ elements: e });
      cy.layout(layout).run();
    }
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
