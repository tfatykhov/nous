<script lang="ts">
  // nous-core ActionReviewCard — post-hoc advisory review (spec Appendix A2).
  // compensation.revertible=false renders the plain statement of why, and the
  // builder omits the Revert button entirely.
  import { store } from '../store.svelte';
  import { resolveDynamic, toDisplayString } from '../functions';
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
  const title = $derived(toDisplayString(resolveDynamic(comp.title, ctx)));
  const did = $derived(toDisplayString(resolveDynamic(comp.did, ctx)));
  const why = $derived(toDisplayString(resolveDynamic(comp.why, ctx)));
  const cost = $derived(toDisplayString(resolveDynamic(comp.cost, ctx)));
  const compensation = $derived(
    resolveDynamic(comp.compensation, ctx) as {
      revertible?: boolean;
      note?: string;
    } | null,
  );
</script>

<div class="review">
  <div class="head">
    <span class="badge">action review</span>
    <h2>{title}</h2>
  </div>
  <dl>
    {#if did}<dt>Did</dt>
      <dd>{did}</dd>{/if}
    {#if why}<dt>Why</dt>
      <dd>{why}</dd>{/if}
    {#if cost}<dt>Cost</dt>
      <dd>{cost}</dd>{/if}
    <dt>Undo</dt>
    <dd class:no={!compensation?.revertible}>
      {#if compensation?.revertible}
        Revertible.
      {:else}
        Not revertible.{#if compensation?.note}&nbsp;{compensation.note}{/if}
      {/if}
    </dd>
  </dl>
</div>

<style>
  .review {
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: var(--radius-sm);
    background: var(--surface);
    padding: 0.9rem 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }
  .badge {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--accent);
    border: 1px solid var(--accent);
    border-radius: 999px;
    padding: 0.1rem 0.5rem;
  }
  h2 {
    margin: 0;
    font-size: 1.05rem;
  }
  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.3rem 0.8rem;
    margin: 0;
  }
  dt {
    color: var(--muted);
    font-size: 0.82rem;
  }
  dd {
    margin: 0;
    white-space: pre-wrap;
  }
  dd.no {
    color: var(--warn);
  }
</style>
