<script lang="ts">
  // nous-core DecisionCard — one decision under review: description,
  // stakes/category badges, current outcome (usually bound to the surface
  // data model so a resolve action repaints it live). Presentational; the
  // outcome buttons are ordinary Buttons in the surface tree.
  import { store } from '../store.svelte';
  import { flexGrow, resolveDynamic, toDisplayString } from '../functions';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

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
  const description = $derived(toDisplayString(resolveDynamic(comp.description, ctx)));
  const stakes = $derived(toDisplayString(resolveDynamic(comp.stakes, ctx)));
  const category = $derived(toDisplayString(resolveDynamic(comp.category, ctx)));
  const outcome = $derived(toDisplayString(resolveDynamic(comp.outcome, ctx)));
  const decisionId = $derived(toDisplayString(resolveDynamic(comp.decisionId, ctx)));
  const settled = $derived(outcome !== '' && outcome !== 'pending');
</script>

<div class="card" class:settled style:flex-grow={flexGrow(comp.weight)}>
  <p class="desc">{description}</p>
  <div class="badges">
    {#if stakes}
      <span class="badge stakes-{stakes}">{stakes}</span>
    {/if}
    {#if category}
      <span class="badge">{category}</span>
    {/if}
    {#if settled}
      <span class="badge outcome outcome-{outcome}">{outcome}</span>
    {/if}
    <span class="id" title={decisionId}>{decisionId.slice(0, 8)}</span>
  </div>
</div>

<style>
  .card {
    padding: 0.65rem 0.8rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: var(--radius-sm);
    min-width: 0;
  }
  .card.settled {
    opacity: 0.65;
    border-left-color: var(--border);
  }
  .desc {
    margin: 0 0 0.4rem;
    overflow-wrap: anywhere;
  }
  .badges {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }
  .badge {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 0.1rem 0.45rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
  }
  .badge.stakes-high,
  .badge.stakes-critical {
    color: var(--red);
    border-color: var(--red);
  }
  .badge.outcome-success {
    color: var(--green);
    border-color: var(--green);
  }
  .badge.outcome-failure {
    color: var(--red);
    border-color: var(--red);
  }
  .badge.outcome-partial {
    color: var(--yellow);
    border-color: var(--yellow);
  }
  .id {
    margin-left: auto;
    color: var(--muted);
    font-size: 0.72rem;
    font-family: var(--mono, monospace);
  }
</style>
