<script lang="ts">
  import { fmtUtc } from '../../lib/harness';
  import { MEASURES, barReadout, sleepChart, type SleepRow } from '../../lib/consolidation';

  // The newest sleeps as one bar each, oldest first: what a sleep gained above the line, what it removed below it.
  // Each bar is a button that opens its sleep in the list; the row of bars is one tab stop, and the arrow keys walk it.
  let { rows, openId, onpick }: { rows: SleepRow[]; openId: string | null; onpick: (id: string) => void } = $props();

  let measureKey = $state(MEASURES[0].key);
  const measure = $derived(MEASURES.find((m) => m.key === measureKey) ?? MEASURES[0]);
  const chart = $derived(sleepChart(rows, measure));

  /** The bar the pointer or the keyboard is on; the readout says it, and the open sleep's when there is none. */
  let pointedId = $state<string | null>(null);
  const readBar = $derived(
    chart.bars.find((b) => b.id === pointedId) ?? chart.bars.find((b) => b.id === openId) ?? chart.bars.at(-1) ?? null,
  );
  /** The one bar in the tab order: the open sleep's, or the newest. */
  const tabId = $derived((chart.bars.find((b) => b.id === openId) ?? chart.bars.at(-1))?.id ?? null);

  const pct = (value: number, max: number) => (max > 0 ? (value / max) * 100 : 0);

  /** The share of the height above the line: in the ratio of the two sides' largest values, but never under 35% or
   *  over 65%, so a side whose numbers are small (9 new facts against 86 retired) is still readable. Each side is
   *  drawn to its own largest value, which the scale prints. */
  const upShare = $derived.by(() => {
    if (!measure.down || chart.maxDown === 0) return 1;
    return Math.min(0.65, Math.max(0.35, chart.maxUp / (chart.maxUp + chart.maxDown)));
  });

  function onkeydown(e: KeyboardEvent, i: number) {
    const step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const bars = (e.currentTarget as HTMLElement).parentElement?.querySelectorAll<HTMLButtonElement>('button.bar');
    bars?.[Math.min(chart.bars.length - 1, Math.max(0, i + step))]?.focus();
  }
</script>

<section class="chart-card" aria-labelledby="sleep-chart-h">
  <div class="chart-head">
    <h2 id="sleep-chart-h">Last {rows.length} {rows.length === 1 ? 'sleep' : 'sleeps'}</h2>
    <div class="seg" role="group" aria-label="What the chart measures">
      {#each MEASURES as m (m.key)}
        <button type="button" class:on={m.key === measureKey} aria-pressed={m.key === measureKey} onclick={() => { measureKey = m.key; }}>
          {m.label}
        </button>
      {/each}
    </div>
  </div>

  {#if readBar}
    <p class="readout">
      <span class="readout-when">{fmtUtc(readBar.row.cycle.started_at)}</span>
      <span class="readout-what">{barReadout(readBar, measure)}</span>
    </p>
  {/if}

  <div class="plot" style:--up={upShare} style:--down={1 - upShare}>
    <div class="scale" aria-hidden="true">
      <span class="half up">{chart.maxUp.toLocaleString('en-US')}</span>
      {#if measure.down && chart.maxDown > 0}<span class="half down">{chart.maxDown.toLocaleString('en-US')}</span>{/if}
    </div>
    <div class="bars" role="group" aria-label="One bar per sleep, oldest first. The arrow keys move between sleeps.">
      {#each chart.bars as bar, i (bar.id)}
        <button
          type="button"
          class="bar"
          class:open={bar.id === openId}
          tabindex={bar.id === tabId ? 0 : -1}
          aria-label={`${fmtUtc(bar.row.cycle.started_at)}: ${barReadout(bar, measure)}. Open this sleep.`}
          onclick={() => onpick(bar.id)}
          onmouseenter={() => { pointedId = bar.id; }}
          onmouseleave={() => { pointedId = null; }}
          onfocus={() => { pointedId = bar.id; }}
          onblur={() => { pointedId = null; }}
          onkeydown={(e) => onkeydown(e, i)}
        >
          <span class="half up"><span class="fill" class:some={bar.up > 0} style:height="{pct(bar.up, chart.maxUp)}%" style:background="var({measure.up.token})"></span></span>
          {#if measure.down && chart.maxDown > 0}
            <span class="half down"><span class="fill" class:some={bar.down > 0} style:height="{pct(bar.down, chart.maxDown)}%" style:background="var({measure.down.token})"></span></span>
          {/if}
        </button>
      {/each}
    </div>
    <div class="ticks" aria-hidden="true">
      {#each chart.bars as bar (bar.id)}
        <span class="tick">{bar.tick}</span>
      {/each}
    </div>
  </div>

  <ul class="legend">
    <!-- Each side is drawn to its own largest value, so the legend says both: a bar's height compares within a side only. -->
    <li><span class="dot" style:background="var({measure.up.token})"></span>{measure.up.label}{measure.down ? ', above the line' : ''}, up to {chart.maxUp.toLocaleString('en-US')}</li>
    {#if measure.down}
      <li><span class="dot" style:background="var({measure.down.token})"></span>{measure.down.label}, below the line, up to {chart.maxDown.toLocaleString('en-US')}</li>
    {/if}
  </ul>
</section>

<style>
  .chart-card {
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem 0.875rem;
  }

  .chart-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem 1rem;
  }

  h2 {
    font-size: 15px;
    font-weight: 650;
    color: var(--text);
    margin: 0;
  }

  /* The measure switch sits on a white card, so its track is a wash and the white raised pill shows. */
  .seg { display: flex; gap: 2px; background: var(--wash); border-radius: var(--ctl-radius); padding: 3px; }
  .seg button { background: transparent; color: var(--muted); border: 0; border-radius: var(--ctl-radius); padding: 0 0.875rem; min-height: 28px; font: inherit; font-size: 0.8125rem; cursor: pointer; transition: background var(--transition), color var(--transition); }
  .seg button:hover { color: var(--text); }
  .seg button.on { background: var(--tab-active-bg); color: var(--text); font-weight: 600; box-shadow: var(--tab-active-shadow); }
  .seg button:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }

  .readout {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.25rem 0.75rem;
    margin: 0.625rem 0 0.5rem;
    min-height: 1.5rem;
    font-size: 0.8125rem;
    color: var(--muted);
  }
  .readout-what { font-size: 0.9375rem; font-weight: 650; font-stretch: 82%; color: var(--text); }

  /* The scale, the bars and the day labels share one grid: the scale's column, then the plot. */
  .plot {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    column-gap: 0.5rem;
  }

  .scale, .bar {
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .scale { height: 7.5rem; font-size: 0.75rem; font-stretch: 82%; color: var(--muted); text-align: right; }
  .scale .down { display: flex; flex-direction: column; justify-content: flex-end; }

  /* The two halves share the height by upShare; each is drawn to its own largest value. */
  .half { flex: var(--up) 1 0px; min-height: 0; }
  .half.down { flex-grow: var(--down); }

  .bars {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(0, 1fr);
    height: 7.5rem;
  }

  .bar {
    padding: 0 1px;
    border: 0;
    border-radius: 4px;
    background: transparent;
    cursor: pointer;
    transition: background var(--transition);
  }
  .bar:hover, .bar.open { background: var(--wash); }
  .bar:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }

  .bar .half { display: flex; justify-content: center; }
  .bar .up { align-items: flex-end; border-bottom: 1px solid var(--border); }
  .bar .down { align-items: flex-start; }

  .fill {
    display: block;
    width: 100%;
    max-width: 1.125rem;
    border-radius: 4px;
  }
  .fill.some { min-height: 2px; }

  .ticks {
    grid-column: 2;
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(0, 1fr);
    margin-top: 0.25rem;
    height: 1.125rem;
    font-size: 0.75rem;
    color: var(--muted);
  }
  /* A label is wider than its bar and runs on past it; sleepChart leaves it the room. */
  .tick { white-space: nowrap; }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 1rem;
    list-style: none;
    margin: 0.5rem 0 0;
    padding: 0;
    font-size: 0.75rem;
    color: var(--muted);
  }
  .legend li { display: flex; align-items: center; gap: 0.375rem; }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }

  @media (max-width: 768px) {
    .seg button { min-height: 44px; }
  }
</style>
