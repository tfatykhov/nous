<script lang="ts">
  import { PREVIEW_CHARS, reportSections, upkeepCounts, type SleepRow } from '../../lib/consolidation';
  import type { ConsolidationAction } from '../../lib/types/api';

  // One sleep's report: its change rows as sections a person can read (what was learned, updated, merged, resolved and
  // retired), then the graph and housekeeping counters, then the raw record. Nothing recorded is hidden: a row in a
  // shape this screen does not know is shown with its recorded before and after.
  let { row, actions }: { row: SleepRow; actions: ConsolidationAction[] } = $props();

  /** A section shows this many rows until it is asked for all of them. */
  const SHOWN = 5;

  const sections = $derived(reportSections(actions));
  const counts = $derived(upkeepCounts(row.cycle));
  const n = $derived(row.numbers);
  /** The totals say facts changed, yet there is no change row to read (pruned by the audit's retention, or never written). */
  const rowsGone = $derived(sections.length === 0 && n.newFacts + n.merges + n.retired + n.contradictionsResolved > 0);

  let showAll = $state<Record<string, boolean>>({});

  function fmtJson(val: unknown): string {
    if (typeof val === 'string') return val;
    try {
      return JSON.stringify(val, null, 2);
    } catch {
      return String(val);
    }
  }
</script>

{#if row.state === 'running'}
  <p class="state-note">No finish is recorded for this sleep yet. A phase records its changes when it ends, so more may follow.</p>
{:else if row.state === 'unfinished'}
  <p class="state-note">No finish was recorded for this sleep. What it recorded before it stopped is below.</p>
{:else if row.state === 'failed'}
  <p class="state-note">This sleep failed. What it recorded before the failure is below.</p>
{/if}

{#each sections as s (s.key)}
  {@const items = showAll[s.key] ? s.items : s.items.slice(0, SHOWN)}
  <section class="sec">
    <h3>
      {s.title}
      <span class="sec-n">{s.items.length}</span>
      <span class="sec-src">{s.source}</span>
    </h3>
    {#if s.note}<p class="sec-note">{s.note}</p>{/if}
    <ul class="items">
      {#each items as item (item.id)}
        <li class="item">
          {#if item.title}<p class="item-title">{item.title}</p>{/if}
          <div class="item-body">
          {#if item.text}<p class="item-text">{item.text}</p>{/if}
          {#if item.earlier.length}
            <details class="earlier" open={item.earlier.length <= 3}>
              <summary><svg class="chev" viewBox="0 0 20 20" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M7 5l6 5-6 5" /></svg>{s.earlierLabel}{item.earlier.length > 1 ? ` ${item.earlier.length} facts` : ''}</summary>
              <ul>
                {#each item.earlier as text}<li>{text}</li>{/each}
              </ul>
            </details>
          {/if}
          {#if item.why}<p class="why"><span class="why-label">Why</span>{item.why}</p>{/if}
          {#if item.raw}
            <div class="raw-sides">
              <div>
                <span class="raw-label">Before</span>
                {#if item.raw.before == null}<em class="raw-none">none</em>{:else}<pre>{fmtJson(item.raw.before)}</pre>{/if}
              </div>
              <div>
                <span class="raw-label">After</span>
                {#if item.raw.after == null}<em class="raw-none">none</em>{:else}<pre>{fmtJson(item.raw.after)}</pre>{/if}
              </div>
            </div>
          {/if}
          {#if item.ids.length}
            <p class="ids">{#each item.ids as id}<code>{id}</code>{/each}</p>
          {/if}
          </div>
        </li>
      {/each}
    </ul>
    {#if s.items.length > SHOWN && !showAll[s.key]}
      <button type="button" class="more" onclick={() => { showAll[s.key] = true; }}>Show all {s.items.length}</button>
    {/if}
  </section>
{/each}

{#if sections.length === 0}
  <p class="state-note">
    {#if rowsGone}
      No change rows are kept for this sleep; its counts remain. The audit prunes change rows after its retention window.
    {:else if row.state === 'finished'}
      This sleep changed no facts.
    {:else}
      No change is recorded for this sleep.
    {/if}
  </p>
{/if}

{#if counts.graph.length || counts.housekeeping.length}
  <div class="counts">
    {#each [{ title: 'Graph upkeep', rows: counts.graph }, { title: 'Housekeeping', rows: counts.housekeeping }] as list (list.title)}
      {#if list.rows.length}
        <section class="sec">
          <h3>{list.title}</h3>
          <dl class="count-list">
            {#each list.rows as c (c.key)}
              <div>
                <dt>{c.label} <span class="count-key">{c.key}</span></dt>
                <dd>{c.value}</dd>
              </div>
            {/each}
          </dl>
        </section>
      {/if}
    {/each}
  </div>
{/if}

{#if sections.some((s) => s.cut)}
  <p class="foot-note">A text that ends in an ellipsis is its first {PREVIEW_CHARS} characters: that is what the audit keeps.</p>
{/if}

<details class="record">
  <summary><svg class="chev" viewBox="0 0 20 20" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M7 5l6 5-6 5" /></svg>Raw record</summary>
  <dl class="record-ids">
    <div><dt>Cycle</dt><dd><code>{row.cycle.cycle_id}</code></dd></div>
    {#if row.cycle.trace_id}<div><dt>Trace</dt><dd><code>{row.cycle.trace_id}</code></dd></div>{/if}
    <div>
      <dt>Phases finished ({row.cycle.phases_run.length})</dt>
      <dd class="phases">
        {#each row.cycle.phases_run as phase}<code>{phase}</code>{:else}<em class="raw-none">none recorded</em>{/each}
      </dd>
    </div>
  </dl>
  <span class="raw-label">Totals</span>
  <pre>{fmtJson(row.cycle.totals)}</pre>
</details>

<style>
  .state-note, .sec-note, .foot-note {
    margin: 0;
    font-size: 0.8125rem;
    color: var(--muted);
  }
  .state-note { padding: 0.75rem 0; }
  .foot-note { padding-top: 0.75rem; border-top: 1px solid var(--border); }

  /* Sections are divided by rules, not boxed. */
  .sec { padding: 0.875rem 0; border-top: 1px solid var(--border); }

  h3 {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.25rem 0.5rem;
    margin: 0 0 0.375rem;
    font-size: 0.9375rem;
    font-weight: 650;
    color: var(--text);
  }
  .sec-n { font-stretch: 82%; color: var(--muted); }
  .sec-src { margin-left: auto; font-family: var(--font-mono, monospace); font-size: 0.71875rem; font-weight: 400; color: var(--muted); }
  .sec-note { margin-bottom: 0.5rem; }

  .items { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.875rem; }
  /* What it is about on the left, what was written on the right, as the Work trace sets a turn beside its rows. */
  .item { display: grid; grid-template-columns: 13rem minmax(0, 1fr); gap: 0.25rem 1.25rem; align-items: baseline; }
  .item p { margin: 0; }
  .item-title { font-size: 0.8125rem; font-weight: 600; color: var(--muted); overflow-wrap: anywhere; }
  .item-body { grid-column: 2; display: flex; flex-direction: column; gap: 0.3125rem; min-width: 0; }
  .item-text { font-size: 0.9375rem; line-height: 1.5; color: var(--text); max-width: 72ch; overflow-wrap: anywhere; }

  /* What a change replaced hangs off a rule under the text that replaced it. */
  .earlier { margin: 0.125rem 0 0; padding-left: 0.75rem; border-left: 1px solid var(--border); max-width: 72ch; }
  /* A disclosure's own drawn chevron in place of the browser's marker; it turns when open. */
  summary { display: flex; align-items: center; gap: 0.25rem; width: fit-content; list-style: none; cursor: pointer; border-radius: 4px; }
  summary::-webkit-details-marker { display: none; }
  .chev { flex-shrink: 0; transition: transform var(--transition); }
  details[open] > summary .chev { transform: rotate(90deg); }
  .earlier summary { font-size: 0.75rem; font-weight: 600; color: var(--muted); }
  .earlier summary:focus-visible, .record summary:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .earlier ul { list-style: none; margin: 0.25rem 0 0; padding: 0; display: flex; flex-direction: column; gap: 0.375rem; }
  .earlier li { font-size: 0.8125rem; line-height: 1.45; color: var(--muted); overflow-wrap: anywhere; }

  .why { font-size: 0.8125rem; color: var(--text); max-width: 72ch; overflow-wrap: anywhere; }
  .why-label { margin-right: 0.5rem; font-size: 0.75rem; font-weight: 600; color: var(--muted); }

  .ids, .phases { display: flex; flex-wrap: wrap; gap: 0.25rem; }
  code {
    font-family: var(--font-mono, monospace);
    font-size: 0.71875rem;
    padding: 1px 6px;
    border-radius: 999px;
    background: var(--wash);
    color: var(--muted);
  }

  .more {
    margin-top: 0.75rem;
    min-height: 28px;
    padding: 0 0.875rem;
    border: 1px solid var(--border);
    border-radius: var(--ctl-radius);
    background: var(--surface);
    color: var(--text);
    font: inherit;
    font-size: 0.8125rem;
    cursor: pointer;
    transition: border-color var(--transition), box-shadow var(--transition);
  }
  .more:hover { border-color: var(--field-border); box-shadow: var(--card-shadow); }
  .more:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }

  /* The two count lists sit side by side where there is room. */
  .counts { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 20rem), 1fr)); column-gap: 2rem; }
  .count-list { margin: 0; }
  .count-list > div { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; padding: 0.25rem 0; font-size: 0.8125rem; }
  .count-list dt { color: var(--text); min-width: 0; overflow-wrap: anywhere; }
  .count-key { margin-left: 0.375rem; font-family: var(--font-mono, monospace); font-size: 0.6875rem; color: var(--muted); }
  .count-list dd { margin: 0; font-weight: 650; font-stretch: 82%; font-size: 0.9375rem; font-variant-numeric: tabular-nums; color: var(--text); }

  .record { padding: 0.75rem 0 0; border-top: 1px solid var(--border); }
  .record summary { font-size: 0.8125rem; font-weight: 600; color: var(--muted); }
  .record-ids { margin: 0.625rem 0; display: flex; flex-direction: column; gap: 0.375rem; font-size: 0.8125rem; }
  .record-ids > div { display: flex; flex-wrap: wrap; gap: 0.25rem 0.75rem; align-items: baseline; }
  .record-ids dt { color: var(--muted); flex: 0 0 9.5rem; }
  .record-ids dd { margin: 0; min-width: 0; flex: 1 1 12rem; }

  .raw-sides { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0.5rem; }
  .raw-sides > div { display: flex; flex-direction: column; gap: 0.25rem; min-width: 0; }
  .raw-label { font-size: 0.75rem; font-weight: 600; color: var(--muted); }
  .raw-none { font-size: 0.75rem; font-style: italic; color: var(--muted); }
  pre {
    margin: 0;
    padding: 0.5rem 0.625rem;
    border-radius: var(--radius-sm);
    background: var(--wash);
    font-family: var(--font-mono, monospace);
    font-size: 0.78125rem;
    line-height: 1.5;
    color: var(--text);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .record pre { margin-top: 0.25rem; max-height: 22rem; overflow: auto; }

  @media (max-width: 1024px) {
    /* No room for two columns beside the rail: what it is about sits above what was written. */
    .item { grid-template-columns: minmax(0, 1fr); }
    .item-body { grid-column: 1; }
  }

  @media (max-width: 640px) {
    .raw-sides { grid-template-columns: minmax(0, 1fr); }
    .sec-src { margin-left: 0; flex-basis: 100%; }
    .more { min-height: 44px; }
  }
</style>
