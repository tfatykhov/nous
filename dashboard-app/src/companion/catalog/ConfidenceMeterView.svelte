<script lang="ts">
  // nous-core ConfidenceMeter — horizontal 0..1 bar with the numeric value.
  // Purely presentational; the value may be a literal or a data binding.
  import { store } from '../store.svelte';
  import { flexGrow, resolveDynamic } from '../functions';
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
  const value = $derived.by(() => {
    const raw = Number(resolveDynamic(comp.value, ctx));
    return Number.isFinite(raw) ? Math.min(1, Math.max(0, raw)) : 0;
  });
  // Confidence bands: red below coin-flip territory, yellow mid, green high.
  const band = $derived(value < 0.4 ? 'low' : value < 0.7 ? 'mid' : 'high');
</script>

<div class="meter" style:flex-grow={flexGrow(comp.weight)}>
  <div class="track" role="meter" aria-valuemin={0} aria-valuemax={1} aria-valuenow={value}>
    <div class="fill {band}" style:width="{value * 100}%"></div>
  </div>
  <span class="num">{value.toFixed(2)}</span>
</div>

<style>
  .meter {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-width: 0;
  }
  .track {
    flex: 1;
    height: 6px;
    background: var(--surface-hover);
    border-radius: 3px;
    overflow: hidden;
  }
  .fill {
    height: 100%;
    border-radius: 3px;
    transition: var(--transition);
  }
  .fill.low {
    background: var(--red);
  }
  .fill.mid {
    background: var(--yellow);
  }
  .fill.high {
    background: var(--green);
  }
  .num {
    color: var(--muted);
    font-size: 0.78rem;
    font-variant-numeric: tabular-nums;
  }
</style>
