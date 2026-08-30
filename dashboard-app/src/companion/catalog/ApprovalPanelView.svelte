<script lang="ts">
  // nous-core ApprovalPanel — the escalation card (spec Appendix A).
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
  const summary = $derived(toDisplayString(resolveDynamic(comp.summary, ctx)));
  const risk = $derived(toDisplayString(resolveDynamic(comp.risk, ctx)));
</script>

<div class="panel">
  <div class="head">
    <span class="badge">escalation</span>
    <h2>{title}</h2>
  </div>
  {#if summary}
    <p class="summary">{summary}</p>
  {/if}
  {#if risk}
    <p class="risk"><strong>Risk:</strong> {risk}</p>
  {/if}
</div>

<style>
  .panel {
    border: 1px solid var(--border);
    border-left: 3px solid var(--warn);
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
    color: var(--warn);
    border: 1px solid var(--warn);
    border-radius: 999px;
    padding: 0.1rem 0.5rem;
  }
  h2 {
    margin: 0;
    font-size: 1.05rem;
  }
  p {
    margin: 0;
    white-space: pre-wrap;
  }
  .risk {
    color: var(--crit);
  }
</style>
