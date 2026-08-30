<script lang="ts">
  // F094 BarChart — categorical bars, y-axis ALWAYS zero-based (a truncated
  // bar axis is the commonest way a chart lies, and the model cannot reach
  // the domain). Each point's `t` is a category label, `v` its value.
  // Horizontal is the readable choice for long labels — an enum, not a CSS
  // decision the model makes.
  import { store } from '../store.svelte';
  import { resolveDynamic, toDisplayString } from '../functions';
  import {
    readSeries,
    seriesValues,
    classify,
    yDomain,
    normalizeTone,
    toneVar,
    formatTick,
  } from '../chart';
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
  const series = $derived(readSeries(resolveDynamic({ path: String(comp.path ?? '') }, ctx)));
  const label = $derived(toDisplayString(comp.label));
  const tone = $derived(normalizeTone(comp.tone));
  const horizontal = $derived(comp.orientation === 'horizontal');

  const bars = $derived(
    seriesValues(series.points).map((p) => ({
      cat: toDisplayString(series.points[p.i]?.t),
      v: p.v,
    })),
  );
  const kind = $derived(classify(bars.map((b) => b.v)));
  // Always zero-based (§3): the domain includes 0, so bar length is |v| as a
  // fraction of the largest magnitude present. A negative bar must be as long
  // as a positive bar of equal size — its sign shows in the value label and
  // the `neg` marker, never by vanishing (an earlier (v-min)/span reversed
  // negative magnitudes; codex P2). Bidirectional geometry is deferred.
  const domain = $derived(yDomain(bars.map((b) => b.v), true));
  const maxAbs = $derived(Math.max(Math.abs(domain.min), Math.abs(domain.max)) || 1);
  function frac(v: number): number {
    return Math.max(0, Math.min(100, (Math.abs(v) / maxAbs) * 100));
  }
</script>

<div class="bars" style:--tone={toneVar(tone)}>
  {#if label}<div class="label">{label}</div>{/if}
  {#if !series.ok}
    <div class="state">not a series ({series.shape})</div>
  {:else if kind === 'empty'}
    <div class="state">no data{series.reason ? ` — ${series.reason}` : ''}</div>
  {:else if horizontal}
    <div class="hlist">
      {#each bars as bar, i (i)}
        <div class="hrow">
          <span class="cat" title={bar.cat}>{bar.cat}</span>
          <span class="track"><i class:neg={bar.v < 0} style:width="{frac(bar.v)}%"></i></span>
          <span class="val">{formatTick(bar.v)}</span>
        </div>
      {/each}
    </div>
  {:else}
    <div class="vwrap">
      {#each bars as bar, i (i)}
        <div class="vcol">
          <span class="val">{formatTick(bar.v)}</span>
          <span class="vtrack"><i class:neg={bar.v < 0} style:height="{frac(bar.v)}%"></i></span>
          <span class="cat" title={bar.cat}>{bar.cat}</span>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .bars {
    min-width: 0;
    overflow-x: auto;
  }
  .label {
    color: var(--muted);
    font-size: 0.78rem;
    margin-bottom: 0.4rem;
  }
  .state {
    color: var(--muted);
    font-size: 0.78rem;
  }
  .hlist {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .hrow {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.8rem;
  }
  .cat {
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 40%;
    flex: none;
    width: 40%;
  }
  .track {
    flex: 1;
    height: 8px;
    background: var(--chart-grid);
    border-radius: 4px;
    overflow: hidden;
  }
  .track i,
  .vtrack i {
    display: block;
    background: var(--tone);
    border-radius: 4px;
  }
  /* A negative bar is as long as its magnitude; the diagonal hatch + the
     signed value label carry the sign so it is never mistaken for positive. */
  .track i.neg,
  .vtrack i.neg {
    background-image: repeating-linear-gradient(
      45deg,
      var(--scrim) 0,
      var(--scrim) 2px,
      transparent 2px,
      transparent 5px
    );
  }
  .track i {
    height: 100%;
  }
  .val {
    color: var(--text);
    font-variant-numeric: tabular-nums;
    font-size: 0.78rem;
    min-width: 2.5rem;
    text-align: right;
  }
  .vwrap {
    display: flex;
    gap: 0.5rem;
    align-items: flex-end;
    min-height: 120px;
  }
  .vcol {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.25rem;
    flex: 1;
    min-width: 32px;
  }
  .vtrack {
    width: 60%;
    height: 96px;
    background: var(--chart-grid);
    border-radius: 4px;
    display: flex;
    align-items: flex-end;
    overflow: hidden;
  }
  .vtrack i {
    width: 100%;
  }
  .vcol .cat {
    width: auto;
    max-width: 100%;
    font-size: 0.72rem;
  }
</style>
