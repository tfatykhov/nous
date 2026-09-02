<script lang="ts">
  // F096 ChipRow — a wrapping row of labelled status chips (data freshness,
  // lane health, environment flags): uppercase muted label, tone-coloured
  // value, muted detail. Values are preformatted; tones closed at render.
  import { store } from '../store.svelte';
  import { flexGrow, resolveDynamic, toDisplayString } from '../functions';
  import { normalizeTone, toneInkVar } from '../chart';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  interface Chip {
    label: string;
    value: string;
    detail: string;
    ink: string;
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
  const chips = $derived.by((): Chip[] => {
    const resolved = resolveDynamic(comp.items, ctx);
    if (!Array.isArray(resolved)) return [];
    return resolved.map((row) => {
      const r = (typeof row === 'object' && row !== null ? row : {}) as Record<string, unknown>;
      return {
        label: toDisplayString(r.label),
        value: toDisplayString(r.value),
        detail: toDisplayString(r.detail),
        ink: toneInkVar(normalizeTone(r.tone)),
      };
    });
  });
</script>

{#if chips.length > 0}
  <div class="chips" style:flex-grow={flexGrow(comp.weight)}>
    {#each chips as chip, i (i)}
      <div class="chip" style:--ink={chip.ink}>
        <span class="l">{chip.label}</span>
        <span class="v">{chip.value}</span>
        {#if chip.detail}<span class="dt">· {chip.detail}</span>{/if}
      </div>
    {/each}
  </div>
{/if}

<style>
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    min-width: 0;
  }
  .chip {
    border: 1px solid var(--border);
    background: var(--bg);
    border-radius: var(--radius-sm);
    padding: 0.45rem 0.7rem;
    font-size: 0.78rem;
    min-width: 0;
  }
  .l {
    display: block;
    font-size: 0.66rem;
    color: var(--muted);
    letter-spacing: 0.05em;
    text-transform: uppercase;
    margin-bottom: 0.15rem;
  }
  .v {
    color: var(--ink);
    font-weight: 600;
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
  }
  .dt {
    color: var(--muted);
    margin-left: 0.3rem;
  }
</style>
