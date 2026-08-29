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
</script>

<section class="app-section" class:model={modelSupplied}>
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
    border-left: 3px solid var(--yellow);
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
    color: var(--yellow);
    border: 1px solid var(--yellow);
    border-radius: 999px;
    padding: 0.1rem 0.5rem;
  }
</style>
