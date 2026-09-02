<script lang="ts">
  // F094 Sparkline — inline single-series trend, no axes/grid/point-labels.
  // Hand-rolled SVG (the companion entry ships no vendor JS); the renderer
  // owns scale, domain, gaps, downsample display and colour. The model
  // bound `path` to a series object and picked a `tone` — nothing else.
  // F096: the SVG itself lives in SparkSvg (shared with MetricCard.trend);
  // this frame owns the head (label + current value) and the foot.
  import { store } from '../store.svelte';
  import { resolveDynamic, toDisplayString } from '../functions';
  import {
    readSeries,
    seriesValues,
    countDropped,
    classify,
    normalizeTone,
    toneVar,
    formatTick,
  } from '../chart';
  import SparkSvg from './SparkSvg.svelte';
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
  // `path` is a plain string prop (binding-mandatory); wrap it so it
  // resolves scope-aware — a Sparkline inside a Repeat template resolves
  // relative to its item.
  const series = $derived(readSeries(resolveDynamic({ path: String(comp.path ?? '') }, ctx)));
  const label = $derived(toDisplayString(comp.label));
  const tone = $derived(normalizeTone(comp.tone));
  const trendline = $derived(comp.trendline === true);

  const values = $derived(seriesValues(series.points).map((p) => p.v));
  const kind = $derived(classify(values));
  const dropped = $derived(countDropped(series.points, ['v']));
  const last = $derived(values.length ? values[values.length - 1] : null);
</script>

<div class="spark" style:--tone={toneVar(tone)}>
  <div class="head">
    {#if label}<span class="label">{label}</span>{/if}
    {#if series.ok && last !== null && kind !== 'empty'}
      <span class="cur">{formatTick(last)}{series.unit ? ` ${series.unit}` : ''}</span>
    {/if}
  </div>

  {#if !series.ok}
    <div class="state">not a series ({series.shape})</div>
  {:else if kind === 'empty'}
    <div class="state">no data{series.reason ? ` — ${series.reason}` : ''}</div>
  {:else if kind === 'single'}
    <div class="single">{formatTick(values[0])}<span class="note"> · single reading</span></div>
  {:else}
    <SparkSvg {series} {tone} {trendline} height={40} {label} />
    {#if dropped > 0 || series.downsampledFrom}
      <div class="foot">
        {#if dropped > 0}{dropped} gap{dropped === 1 ? '' : 's'}{/if}
        {#if series.downsampledFrom}{dropped > 0 ? ' · ' : ''}of {series.downsampledFrom}{/if}
      </div>
    {/if}
  {/if}
</div>

<style>
  .spark {
    min-width: 0;
  }
  .head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.5rem;
    margin-bottom: 0.2rem;
  }
  .label {
    color: var(--muted);
    font-size: 0.78rem;
  }
  .cur {
    color: var(--text);
    font-size: 0.85rem;
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }
  .state,
  .foot,
  .note {
    color: var(--muted);
    font-size: 0.75rem;
  }
  .single {
    font-size: 1.3rem;
    font-weight: 600;
    color: var(--text);
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
  }
  .foot {
    margin-top: 0.15rem;
  }
</style>
