<script lang="ts">
  // F096 MetricCard — one metric's full story: label + tone pill, headline
  // value (+unit), comparison caption, an optional embedded trend (SparkSvg,
  // the same geometry as Sparkline) and a footnote. Every string arrives
  // PREFORMATTED; nothing here rounds, re-signs or adds a unit. The VALUE is
  // never coloured — tone lands on the pill and the trend: a number is a
  // fact, a direction is an opinion.
  import { store } from '../store.svelte';
  import { flexGrow, resolveDynamic, toDisplayString } from '../functions';
  import {
    readSeries,
    seriesValues,
    countDropped,
    classify,
    normalizeTone,
    toneVar,
    toneInkVar,
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
  const label = $derived(toDisplayString(resolveDynamic(comp.label, ctx)));
  const value = $derived(toDisplayString(resolveDynamic(comp.value, ctx)));
  const unit = $derived(toDisplayString(resolveDynamic(comp.unit, ctx)));
  // %, ° and friends are set tight against the number; kg/bpm/ms keep the gap.
  const unitTight = $derived(/^[%°‰′″]/.test(unit));
  const delta = $derived(toDisplayString(resolveDynamic(comp.delta, ctx)));
  const caption = $derived(toDisplayString(resolveDynamic(comp.caption, ctx)));
  const footnote = $derived(toDisplayString(resolveDynamic(comp.footnote, ctx)));
  // `tone` may be a literal or a {path} binding (a repeat template's cards
  // each carry their own record's tone); normalizeTone closes the resolved
  // value — unknown ⇒ neutral, never a literal colour.
  const tone = $derived(normalizeTone(resolveDynamic(comp.tone, ctx)));
  const trendline = $derived(comp.trendline === true);

  // `trend` is a bare string path (like Sparkline.path), scope-aware inside a
  // Repeat. Absent or blank ⇒ no chart region at all (a count is not a
  // series); a present path resolving to nothing ⇒ the same no-trend state
  // (a count mixed into a grid of trended metrics — spec §3.1).
  const trendPath = $derived(
    typeof comp.trend === 'string' && comp.trend.trim() ? comp.trend.trim() : '',
  );
  const resolved = $derived(trendPath ? resolveDynamic({ path: trendPath }, ctx) : undefined);
  const series = $derived(
    resolved === undefined || resolved === null ? null : readSeries(resolved),
  );
  const values = $derived(series ? seriesValues(series.points).map((p) => p.v) : []);
  const kind = $derived(classify(values));
  const dropped = $derived(series ? countDropped(series.points, ['v']) : 0);
</script>

<div
  class="metric"
  style:flex-grow={flexGrow(comp.weight)}
  style:--tone={toneVar(tone)}
  style:--ink={toneInkVar(tone)}
>
  <div class="top">
    <span class="label">{label}</span>
    {#if delta}<span class="pill">{delta}</span>{/if}
  </div>
  <div class="value">{value}{#if unit}<span class="unit" class:tight={unitTight}>{unit}</span>{/if}</div>
  {#if caption}<div class="caption">{caption}</div>{/if}
  {#if series}
    {#if !series.ok}
      <div class="state">not a series ({series.shape})</div>
    {:else if kind === 'empty'}
      <div class="state">no data{series.reason ? ` — ${series.reason}` : ''}</div>
    {:else if kind === 'single'}
      <div class="state">single reading</div>
    {:else}
      <SparkSvg {series} {tone} {trendline} height={56} {label} />
      {#if dropped > 0 || series.downsampledFrom}
        <div class="chartnote">
          {#if dropped > 0}{dropped} gap{dropped === 1 ? '' : 's'}{/if}
          {#if series.downsampledFrom}{dropped > 0 ? ' · ' : ''}of {series.downsampledFrom}{/if}
        </div>
      {/if}
    {/if}
  {/if}
  {#if footnote}<div class="foot">{footnote}</div>{/if}
</div>

<style>
  .metric {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
    padding: 0.8rem 0.9rem 0.55rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }
  .top {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
  }
  .label {
    color: var(--muted);
    font-size: 0.8rem;
    letter-spacing: 0.03em;
    /* Basis floor beside the nowrap delta pill (see ScoreCardView). */
    flex: 1 1 6rem;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .pill {
    flex: 0 0 auto;
    font-size: 0.68rem;
    line-height: 1.4;
    padding: 0.1rem 0.5rem;
    border-radius: 999px;
    white-space: nowrap;
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
    color: var(--ink);
    background: color-mix(in srgb, var(--ink) 13%, transparent);
  }
  .value {
    margin-top: 0.3rem;
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
    font-size: 1.55rem;
    font-weight: 600;
    line-height: 1.15;
    overflow-wrap: anywhere;
  }
  .unit {
    margin-left: 0.25rem;
    color: var(--muted);
    font-family: var(--font-ui);
    font-size: 0.82rem;
    font-weight: 400;
  }
  .unit.tight {
    margin-left: 0;
  }
  .caption,
  .state,
  .chartnote,
  .foot {
    color: var(--muted);
    font-size: 0.74rem;
  }
  .state {
    height: 56px;
    display: flex;
    align-items: center;
    margin-top: 0.4rem;
    font-style: italic;
  }
  .metric :global(svg) {
    margin-top: 0.4rem;
  }
  .foot {
    font-size: 0.68rem;
    text-align: right;
    margin-top: 0.1rem;
  }
</style>
