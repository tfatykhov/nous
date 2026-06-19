<script lang="ts">
  import { onMount } from 'svelte';

  let { type, data, options = {}, height = '220px' }:
    { type: string; data: any; options?: any; height?: string } = $props();

  let canvas: HTMLCanvasElement;
  let chart: any = null; // plain let — NOT $state to avoid infinite update loop

  onMount(() => {
    chart = new Chart(canvas, {
      type,
      data,
      options: { responsive: true, maintainAspectRatio: false, ...options },
    });
    return () => {
      chart?.destroy();
      chart = null;
    };
  });

  $effect(() => {
    const d = data;
    const o = options;
    if (chart) {
      chart.data = d;
      chart.options = { responsive: true, maintainAspectRatio: false, ...o };
      chart.update('none');
    }
  });
</script>

<div class="chart-wrap" style:height={height}>
  <canvas bind:this={canvas}></canvas>
</div>

<style>
  .chart-wrap {
    position: relative;
    width: 100%;
  }
  @media (max-width: 768px) {
    .chart-wrap {
      height: 200px !important;
    }
  }
</style>
