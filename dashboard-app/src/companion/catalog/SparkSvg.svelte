<script lang="ts">
  // F096 §4.3 — the ONE sparkline geometry, shared by the standalone
  // Sparkline (head + foot around it) and the MetricCard's embedded trend
  // (bare). One partial, two frames, so the treatment cannot drift.
  //
  // Renderer-owned, model-unreachable: the domain (F094 pad, break marker,
  // never zero-based and therefore NO area fill — a fill reads as magnitude
  // from zero), the trendline window, the focus-window shading and the end
  // dot. The caller has already decided the series is drawable (≥2 finite
  // points); the §3.1 empty/single states are the frame's to render.
  import {
    seriesValues,
    yDomain,
    yScale,
    xScale,
    lineSegments,
    rollingMean,
    trendWindow,
    focusStartIndex,
    toneVar,
    type ReadSeries,
    type Tone,
  } from '../chart';

  let {
    series,
    tone = 'neutral',
    trendline = false,
    height = 40,
    label = '',
  }: {
    series: ReadSeries;
    tone?: Tone;
    trendline?: boolean;
    height?: number;
    label?: string;
  } = $props();

  const W = 260;
  const PAD = 6;

  const finite = $derived(seriesValues(series.points));
  // The smoothed line is the MAIN line when asked for; the raw series stays
  // faint behind it. Indices are preserved by rollingMean, so both split at
  // the same gaps and the end dot lands on the last real reading.
  const main = $derived(trendline ? rollingMean(finite, trendWindow(series.points.length)) : finite);
  // The domain must cover the RAW readings too, or the faint line clips.
  const domainValues = $derived(
    trendline ? [...finite.map((p) => p.v), ...main.map((p) => p.v)] : finite.map((p) => p.v),
  );
  const domain = $derived(yDomain(domainValues, false));
  const x = $derived(xScale(series.points.length, W, PAD));
  const y = $derived(yScale(domain, height, PAD));
  const rawSegments = $derived(trendline ? lineSegments(finite, x, y) : []);
  const segments = $derived(lineSegments(main, x, y));
  const focusIdx = $derived(focusStartIndex(series.points, series.focusFrom));
  // The end dot marks the CURRENT READING — the last raw finite point, never
  // the last rolling mean: with trendline on, [1, 10, 1] must dot 1, not 4
  // (codex P2 on #630). rollingMean preserves indices, so isolation is the
  // same question either way.
  const last = $derived(finite.length ? finite[finite.length - 1] : null);
  // An isolated final reading (previous index is a gap) is already drawn as a
  // dot by the segment loop; an end dot on top of it would double-dot.
  const lastIsolated = $derived(
    finite.length === 1 ||
      (finite.length > 1 && finite[finite.length - 1].i !== finite[finite.length - 2].i + 1),
  );
</script>

<svg
  viewBox="0 0 {W} {height}"
  preserveAspectRatio="none"
  role="img"
  aria-label={label || 'trend'}
  style:height="{height}px"
  style:--tone={toneVar(tone)}
>
  {#if focusIdx !== null}
    <rect
      class="focus"
      x={x(focusIdx).toFixed(1)}
      y="0"
      width={(W - x(focusIdx)).toFixed(1)}
      height={height}
      fill="var(--tone)"
      opacity="0.07"
    />
  {/if}
  {#if domain.zeroBreak}
    <text class="brk" x={PAD} y={height - 2}>~</text>
  {/if}
  {#each rawSegments as seg, i (i)}
    {#if seg.includes(' ')}
      <polyline
        class="raw"
        points={seg}
        fill="none"
        stroke="var(--tone)"
        stroke-width="1"
        opacity="0.35"
        stroke-linejoin="round"
      />
    {/if}
  {/each}
  {#each segments as seg, i (i)}
    {#if seg.includes(' ')}
      <polyline
        points={seg}
        fill="none"
        stroke="var(--tone)"
        stroke-width="1.5"
        stroke-linejoin="round"
        stroke-linecap="round"
      />
    {:else}
      <!-- A one-coordinate segment (a lone reading between two gaps) has no
           line to stroke — draw it as a dot so it is visible (codex P2). -->
      {@const xy = seg.split(',')}
      <circle cx={xy[0]} cy={xy[1]} r="2" fill="var(--tone)" />
    {/if}
  {/each}
  {#if last && !lastIsolated}
    <circle class="end" cx={x(last.i).toFixed(1)} cy={y(last.v).toFixed(1)} r="3" fill="var(--tone)" />
  {/if}
</svg>

<style>
  svg {
    display: block;
    width: 100%;
  }
  .brk {
    fill: var(--muted);
    font-size: 9px;
  }
</style>
