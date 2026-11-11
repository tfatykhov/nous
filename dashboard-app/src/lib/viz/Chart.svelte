<script lang="ts">
  import { onMount } from 'svelte';
  import { cssColor, resolveColors, withChartDefaults } from '../chartColors';

  // phoneHeight: the height under 768 px (200 px unless a chart with a dense legend or many lines asks for more).
  let { type, data, options = {}, height = '220px', phoneHeight = '200px' }:
    { type: string; data: any; options?: any; height?: string; phoneHeight?: string } = $props();

  let canvas: HTMLCanvasElement;
  let chart: any = null; // plain let — NOT $state to avoid infinite update loop

  // The canvas cannot read var(): tokens in the data and options (`var(--x)`, `tint(--x, a)`) resolve against the
  // element the canvas sits in. The axes and legend take the chart text and grid colours beneath the caller's own.
  function config(d: any, o: any) {
    const opts = withChartDefaults(type, resolveColors(o, canvas), cssColor(canvas, '--chart-text'), cssColor(canvas, '--chart-grid'));
    return { data: resolveColors(d, canvas), options: { responsive: true, maintainAspectRatio: false, ...opts } };
  }

  onMount(() => {
    chart = new Chart(canvas, { type, ...config(data, options) });
    return () => {
      chart?.destroy();
      chart = null;
    };
  });

  // New data or options: re-resolve and redraw in place.
  $effect(() => {
    const d = data;
    const o = options;
    if (chart) {
      const c = config(d, o);
      chart.data = c.data;
      chart.options = c.options;
      chart.update('none');
    }
  });
</script>

<div class="chart-wrap" style:height={height} style:--phone-height={phoneHeight}>
  <canvas bind:this={canvas}></canvas>
</div>

<style>
  .chart-wrap {
    position: relative;
    width: 100%;
  }
  @media (max-width: 768px) {
    .chart-wrap {
      height: var(--phone-height) !important;
    }
  }
</style>
