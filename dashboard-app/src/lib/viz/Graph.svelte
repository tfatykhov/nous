<script lang="ts">
  import { onMount } from 'svelte';

  let { elements, layout = { name: 'cose' }, style = [] }:
    { elements: any[]; layout?: any; style?: any[] } = $props();

  let el: HTMLDivElement;
  let cy: any = null; // plain let — NOT $state to avoid infinite update loop

  onMount(() => {
    cy = cytoscape({ container: el, elements, layout, style });
    return () => {
      cy?.destroy();
      cy = null;
    };
  });

  $effect(() => {
    const e = elements;
    if (cy) {
      cy.json({ elements: e });
      cy.layout(layout).run();
    }
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
