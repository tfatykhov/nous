<script lang="ts">
  // F094 LineChart — multi-series (≤4) with axes and a legend. Points carry
  // one numeric key per series; `series[]` names which keys to draw and what
  // each means. The renderer owns scale/ticks/colour ramp; the model names
  // series and meaning only.
  import { store } from '../store.svelte';
  import { resolveDynamic, toDisplayString } from '../functions';
  import {
    readSeries,
    seriesValues,
    isFiniteNumber,
    classify,
    yDomain,
    lineSegments,
    normalizeTone,
    toneVar,
    seriesVar,
    formatTick,
    ticks,
  } from '../chart';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  interface SeriesSpec {
    key: string;
    label?: string;
    tone?: string;
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

  const W = 320;
  const H = 160;
  const PAD_L = 34;
  const PAD_R = 8;
  const PAD_T = 8;
  const PAD_B = 22;

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  const series = $derived(readSeries(resolveDynamic({ path: String(comp.path ?? '') }, ctx)));
  const label = $derived(toDisplayString(comp.label));
  const specs = $derived(
    (Array.isArray(comp.series) ? (comp.series as SeriesSpec[]) : [])
      .filter((s) => s && typeof s.key === 'string')
      .slice(0, 4),
  );

  const allValues = $derived(
    specs.flatMap((s) => series.points.map((p) => p[s.key]).filter(isFiniteNumber) as number[]),
  );
  const kind = $derived(classify(allValues));
  const domain = $derived(yDomain(allValues, false));

  // Positions map straight into the plot area (left axis gutter PAD_L);
  // xScale/yScale from chart.ts assume symmetric padding, so the axis-gutter
  // layout is computed inline here.
  function px(i: number): number {
    const usable = W - PAD_L - PAD_R;
    return series.points.length <= 1
      ? PAD_L + usable / 2
      : PAD_L + (usable * i) / (series.points.length - 1);
  }
  function py(v: number): number {
    const usable = H - PAD_T - PAD_B;
    const span = domain.max - domain.min || 1;
    return PAD_T + usable * (1 - (v - domain.min) / span);
  }

  const lines = $derived(
    specs.map((s, si) => {
      const finite = seriesValues(series.points, s.key);
      const stroke =
        s.tone && s.tone !== 'neutral' ? toneVar(normalizeTone(s.tone)) : seriesVar(si);
      return {
        key: s.key,
        label: s.label ?? s.key,
        stroke,
        segments: lineSegments(finite, px, py),
      };
    }),
  );
  const yTicks = $derived(ticks(domain, 3));
</script>

<div class="line">
  {#if label}<div class="label">{label}</div>{/if}
  {#if !series.ok}
    <div class="state">not a series ({series.shape})</div>
  {:else if kind === 'empty'}
    <div class="state">no data{series.reason ? ` — ${series.reason}` : ''}</div>
  {:else if specs.length === 0}
    <div class="state">no series selected</div>
  {:else}
    <svg viewBox="0 0 {W} {H}" role="img" aria-label={label || 'line chart'}>
      {#each yTicks as t (t)}
        <line x1={PAD_L} y1={py(t)} x2={W - PAD_R} y2={py(t)} stroke="var(--chart-grid)" stroke-width="0.5" />
        <text class="tick" x={PAD_L - 4} y={py(t) + 3} text-anchor="end">{formatTick(t)}</text>
      {/each}
      {#if domain.zeroBreak}
        <text class="brk" x={PAD_L - 4} y={H - PAD_B + 10} text-anchor="end">~</text>
      {/if}
      {#each lines as ln, li (li)}
        {#each ln.segments as seg, si (si)}
          {#if seg.includes(' ')}
            <polyline points={seg} fill="none" stroke={ln.stroke} stroke-width="1.5" />
          {:else}
            <!-- A one-coordinate segment (single timestamp, or a lone reading
                 between two gaps) has no line to stroke — draw the point as a
                 dot so the value is visible instead of an empty plot (codex P2). -->
            {@const xy = seg.split(',')}
            <circle cx={xy[0]} cy={xy[1]} r="2.5" fill={ln.stroke} />
          {/if}
        {/each}
      {/each}
      {#if comp.yLabel}<text class="axl" x={4} y={PAD_T + 4}>{toDisplayString(comp.yLabel)}</text>{/if}
      {#if comp.xLabel}<text class="axl" x={W - PAD_R} y={H - 4} text-anchor="end">{toDisplayString(comp.xLabel)}</text>{/if}
    </svg>
    <div class="legend">
      {#each lines as ln, li (li)}
        <span class="lg"><i style:background={ln.stroke}></i>{ln.label}</span>
      {/each}
    </div>
  {/if}
</div>

<style>
  .line {
    min-width: 0;
  }
  .label {
    color: var(--muted);
    font-size: 0.78rem;
    margin-bottom: 0.3rem;
  }
  .state {
    color: var(--muted);
    font-size: 0.78rem;
  }
  svg {
    display: block;
    width: 100%;
    height: auto;
  }
  .tick,
  .axl {
    fill: var(--chart-axis);
    font-size: 8px;
  }
  .brk {
    fill: var(--muted);
    font-size: 9px;
  }
  .legend {
    display: flex;
    gap: 0.8rem;
    flex-wrap: wrap;
    margin-top: 0.3rem;
  }
  .lg {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    color: var(--muted);
    font-size: 0.75rem;
  }
  .lg i {
    width: 9px;
    height: 3px;
    border-radius: 2px;
  }
</style>
