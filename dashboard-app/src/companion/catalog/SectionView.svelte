<script lang="ts">
  // F092.1 Section — titled content block. Light chrome (title + top rule):
  // the companion shell already wraps every surface in a card, and five
  // nested cards read as boxes-in-boxes. The visible border is reserved for
  // provenance === 'model': data no registered source produced, rendered
  // amber with a chip so the gap is visible, never silent.
  import Renderer from '../Renderer.svelte';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  let {
    surfaceId,
    comp,
    scope = null,
    depth = 0,
    ancestors = [],
  }: {
    surfaceId: string;
    comp: A2uiComponent;
    scope?: Scope | null;
    depth?: number;
    ancestors?: readonly string[];
  } = $props();

  // Static enum prop, narrowed against an allowlist — never a binding
  // (app_spec's provenance map is server-side audit and never ships).
  const modelSupplied = $derived(comp.provenance === 'model');
  // F093 §6.1 layout enum. hero enlarges the heading; grid/rail turn the
  // child's DIRECT container into a grid/scroll rail via :global below (the
  // child is a Column/Row whose items become the cells). Unknown → stack.
  const LAYOUTS = ['stack', 'hero', 'grid-2', 'grid-3', 'rail'];
  const layout = $derived(
    typeof comp.layout === 'string' && LAYOUTS.includes(comp.layout) ? comp.layout : 'stack',
  );
</script>

<section class="app-section {layout}" class:model={modelSupplied}>
  <div class="head">
    <h3>{comp.title}</h3>
    {#if modelSupplied}
      <span class="chip">model-supplied</span>
    {/if}
  </div>
  {#if typeof comp.child === 'string'}
    <Renderer {surfaceId} componentId={comp.child} {scope} {depth} {ancestors} />
  {/if}
</section>

<style>
  .app-section {
    border-top: 1px solid var(--border);
    padding: 0.6rem 0 0.2rem;
  }
  .app-section.model {
    border-left: 3px solid var(--warn);
    padding-left: 0.7rem;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.45rem;
  }
  h3 {
    margin: 0;
    font-size: 0.82rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
  }
  .chip {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--warn);
    border: 1px solid var(--warn);
    border-radius: 999px;
    padding: 0.1rem 0.5rem;
  }

  /* hero: the primary section — larger heading, more air. */
  .app-section.hero {
    padding-top: 1rem;
  }
  .app-section.hero h3 {
    font-size: 1rem;
    text-transform: none;
    letter-spacing: -0.01em;
    color: var(--text);
    font-family: var(--font-display);
  }

  /* grid/rail reshape the child's direct container (a Column/Row → .col/.row,
     a StatRow → .stat-row) so its items become cells. :global reaches the
     rendered child, which Svelte places as a direct descendant. */
  .app-section.grid-2 > :global(.col),
  .app-section.grid-2 > :global(.row) {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.6rem;
  }
  .app-section.grid-3 > :global(.col),
  .app-section.grid-3 > :global(.row) {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 0.6rem;
  }
  .app-section.rail > :global(.col),
  .app-section.rail > :global(.row) {
    display: flex;
    flex-direction: row;
    /* A Row child keeps its own flex-wrap: wrap, so without this reset rail
       items wrap onto new lines instead of scrolling horizontally (codex P2). */
    flex-wrap: nowrap;
    gap: 0.6rem;
    overflow-x: auto;
    padding-bottom: 0.3rem;
  }
  .app-section.rail > :global(.col) > :global(*),
  .app-section.rail > :global(.row) > :global(*) {
    flex: 0 0 auto;
    min-width: 160px;
  }
</style>
