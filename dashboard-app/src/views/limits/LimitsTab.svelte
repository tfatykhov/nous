<script lang="ts">
  import { tick } from 'svelte';
  import { Button } from '../../lib/ui/button';
  import { makePollStore } from '../../lib/stores/registry';
  import { usePoll } from '../../lib/poll';
  import {
    ALREADY_TEXT, FORBIDDEN_TEXT, LIMITS_SUBTITLE, LOG_TITLE, NOT_LOADED_TITLE, NO_CEILING_TEXT, NO_CHANGES_TEXT,
    OFF_TEXT, OFF_TITLE, READING_TEXT, REFRESH_LABEL, RETRY_LABEL, UNSUPPORTED_TEXT,
    changeLine, changeText, factsText, problemText, rangeText, readGate, readLimits, readLog, readStateText,
    resetLimit, resetText, saveLimit, typedValue,
    type LimitRow, type LimitsAnswer, type LogAnswer,
  } from '../../lib/autonomy_settings';

  // F115: the limits of background work the owner may change (GET /autonomy/limits) and the last 20 changes of them
  // (GET /autonomy/log?kind=limits), read when the tab opens, on Refresh or Retry and after every change. A row keeps
  // what was typed until a change is saved, so a refused value stays in the input beside the server's sentence. Each
  // row says what it limits, its range, where its value comes from (with the host's ceiling), and when a change
  // applies. While the saved settings could not be read the tab shows the server's sentence and no row.

  type Page = { limits: LimitsAnswer; log: LogAnswer | null };
  const store = usePoll(
    makePollStore<Page>(async (signal) => {
      const limits = await readLimits(signal);
      const log = limits.enabled && limits.loaded !== false ? await readLog('limits', signal) : null;
      return { limits, log };
    }, 0),
  );

  type Note = { text: string; ok: boolean };
  /** What was typed, by limit, until it is saved. */
  let drafts = $state<Record<string, string>>({});
  /** The sentence after a change, by limit. */
  let notes = $state<Record<string, Note>>({});
  /** The limit whose change is in flight. */
  let busy = $state<string | null>(null);

  let gate = $derived(readGate($store.error));
  let answer = $derived($store.data?.limits ?? null);
  let rows = $derived(answer?.limits ?? []);
  let changes = $derived($store.data?.log?.changes ?? []);
  let readLine = $derived(readStateText($store, 'limits'));

  const shownValue = (row: LimitRow) => drafts[row.key] ?? String(row.value);
  const edited = (row: LimitRow) => drafts[row.key] !== undefined && drafts[row.key].trim() !== String(row.value);

  function typed(key: string, text: string) {
    drafts = { ...drafts, [key]: text };
  }

  function note(key: string, value: Note | null) {
    const next = { ...notes };
    if (value) next[key] = value;
    else delete next[key];
    notes = next;
  }

  function forget(key: string) {
    const next = { ...drafts };
    delete next[key];
    drafts = next;
  }

  async function settle(key: string, outcome: Note) {
    await store.refresh();
    busy = null;
    note(key, outcome);
    await tick();
    document.getElementById(`lim-n.${key}`)?.focus();
  }

  async function save(row: LimitRow) {
    if (busy) return;
    busy = row.key;
    note(row.key, null);
    let outcome: Note;
    try {
      const changed = await saveLimit(row.key, typedValue(shownValue(row)));
      forget(row.key);
      outcome = { text: changed.changed ? changeText(changed.limit, row.value) : ALREADY_TEXT, ok: true };
    } catch (err) {
      outcome = { text: problemText(err, 'change'), ok: false };
    }
    await settle(row.key, outcome);
  }

  async function useHost(row: LimitRow) {
    if (busy) return;
    busy = row.key;
    note(row.key, null);
    let outcome: Note;
    try {
      const changed = await resetLimit(row.key);
      forget(row.key);
      outcome = { text: resetText(changed.limit, changed.changed), ok: true };
    } catch (err) {
      outcome = { text: problemText(err, 'change'), ok: false };
    }
    await settle(row.key, outcome);
  }
</script>

<header class="view-head">
  <p class="subtitle">{LIMITS_SUBTITLE}</p>
  <div class="head-right">
    <p class="read-state" class:read-failed={$store.error !== null} role="status">{readLine}</p>
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={() => void store.refresh()} disabled={$store.loading}>{$store.loading ? READING_TEXT : $store.error ? RETRY_LABEL : REFRESH_LABEL}</Button>
  </div>
</header>

{#if gate === 'forbidden'}
  <p class="notice" role="status">{FORBIDDEN_TEXT}</p>
{:else if gate === 'unsupported'}
  <p class="notice" role="status">{UNSUPPORTED_TEXT}</p>
{:else if answer && !answer.enabled}
  <section class="notice" role="status">
    <h2>{OFF_TITLE}</h2>
    <p>{OFF_TEXT}</p>
  </section>
{:else if answer && answer.loaded === false}
  <section class="notice" role="status" aria-labelledby="lim-unread">
    <h2 id="lim-unread">{NOT_LOADED_TITLE}</h2>
    <p>{answer.message}</p>
    <div class="notice-actions">
      <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={$store.loading}
        onclick={() => void store.refresh()}>{RETRY_LABEL}</Button>
    </div>
  </section>
{:else if answer}
  <section class="card" aria-labelledby="lim-top">
    <h2 id="lim-top">Limits of background work</h2>
    {#if answer.ceilings_set === false}<p class="hint">{NO_CEILING_TEXT}</p>{/if}
    <ul class="rows">
      {#each rows as row (row.key)}
        {@const said = notes[row.key]}
        <li class="row">
          <div class="what">
            <label class="label" for="lim-i.{row.key}">{row.label}</label>
            <p class="range">{rangeText(row)}</p>
          </div>
          <div class="edit">
            <input id="lim-i.{row.key}" class="value" type="text" inputmode="numeric" autocomplete="off" spellcheck="false"
              value={shownValue(row)} oninput={(e) => typed(row.key, e.currentTarget.value)}
              onkeydown={(e) => { if (e.key === 'Enter' && edited(row)) void save(row); }}
              aria-describedby="lim-d.{row.key} lim-f.{row.key} lim-a.{row.key}" disabled={busy !== null} />
            <span class="unit">{row.unit}</span>
            <Button size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy !== null || !edited(row)}
              aria-label="Save {row.label}" onclick={() => void save(row)}>{busy === row.key ? 'Saving…' : 'Save'}</Button>
            {#if row.source === 'owner'}
              <span class="reset">
                <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy !== null}
                  aria-label="Use the host's value for {row.label}" onclick={() => void useHost(row)}>Use the host's value</Button>
              </span>
            {:else}
              <span class="reset-slot" aria-hidden="true"></span>
            {/if}
          </div>
          <p class="description" id="lim-d.{row.key}">{row.description}</p>
          <p class="facts source-{row.source}" id="lim-f.{row.key}">{factsText(row)}</p>
          <p class="applies" id="lim-a.{row.key}">{row.applies}</p>
          {#if said}
            <p class="note" class:note-err={!said.ok} id="lim-n.{row.key}" tabindex="-1" role={said.ok ? 'status' : 'alert'}>{said.text}</p>
          {/if}
        </li>
      {/each}
    </ul>
  </section>

  <section class="card" aria-labelledby="lim-log">
    <h2 id="lim-log">{LOG_TITLE}</h2>
    {#if changes.length === 0}
      <p class="hint">{NO_CHANGES_TEXT}</p>
    {:else}
      <ul class="log">
        {#each changes as change, i (i)}
          <li>{changeLine(change)}</li>
        {/each}
      </ul>
    {/if}
  </section>
{:else if $store.error}
  <p class="read-error" role="alert">Could not load the limits: {problemText($store.error, 'see')}</p>
{:else}
  <p class="hint">Loading the limits…</p>
{/if}

<style>
  .view-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  .subtitle { font-size: 0.8125rem; color: var(--muted); margin: 0; }
  .read-state { font-size: 0.75rem; color: var(--muted); margin: 0; max-width: 40ch; overflow-wrap: anywhere; }
  .read-failed { color: var(--warn-text); }
  h2 { font-size: 0.9375rem; font-weight: 650; margin: 0 0 0.375rem; }
  .notice { padding: 0.875rem 1rem; border-radius: var(--card-radius-sm); background: var(--warn-bg);
    border: 1px solid var(--warn-border); font-size: 0.8125rem; margin: 0; }
  .notice p { margin: 0; }
  .notice-actions { margin-top: 0.625rem; }
  .read-error { font-size: 0.8125rem; color: var(--warn-text); margin: 0; overflow-wrap: anywhere; }
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem; min-width: 0; }
  .hint { font-size: 0.75rem; color: var(--muted); margin: 0.25rem 0 0; }
  .rows { list-style: none; margin: 0.5rem 0 0; padding: 0; }
  /* A limit: its name and range at the left, the input and its buttons at the right; what it limits, where its value
     comes from, when a change applies and the result sentence under both. One column on a phone. */
  .row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 0.25rem 1rem;
    padding: 0.75rem 0; border-top: 1px solid var(--border); min-width: 0; }
  .row:first-child { border-top: 0; }
  .what { min-width: 0; }
  .label { font-size: 0.875rem; font-weight: 600; overflow-wrap: anywhere; }
  .range { font-size: 0.75rem; color: var(--muted); margin: 0.125rem 0 0; }
  /* One grid for every row, so the inputs, units and buttons line up down the card; the last column is kept even when a
     row has no "Use the host's value". */
  .edit { display: grid; grid-template-columns: 7rem 8rem auto 10.5rem; align-items: center; gap: 0.5rem; }
  .value { width: 7rem; height: 2rem; padding: 0 0.5rem; background: var(--surface); border: 1px solid var(--field-border);
    border-radius: var(--radius-sm); color: var(--text); font: inherit; font-size: 0.875rem; text-align: right;
    font-variant-numeric: tabular-nums; }
  .value:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .value:disabled { opacity: 0.6; }
  .unit { font-size: 0.75rem; color: var(--muted); line-height: 1.2; }
  .description { grid-column: 1 / -1; font-size: 0.8125rem; margin: 0.125rem 0 0; max-width: 80ch; }
  .facts { grid-column: 1 / -1; font-size: 0.75rem; font-weight: 600; color: var(--muted); margin: 0; }
  .source-owner { color: var(--accent-text); }
  .source-clamped { color: var(--warn-text); }
  .applies { grid-column: 1 / -1; font-size: 0.75rem; color: var(--muted); margin: 0; max-width: 80ch; }
  .note { grid-column: 1 / -1; font-size: 0.8125rem; color: var(--save-ok); margin: 0.25rem 0 0; overflow-wrap: anywhere; }
  .note-err { color: var(--save-err); }
  .note:focus { outline: 2px solid var(--focus-ring); outline-offset: 2px; border-radius: var(--radius-sm); }
  .log { list-style: none; margin: 0.25rem 0 0; padding: 0; font-size: 0.8125rem; }
  .log li { padding: 0.375rem 0; border-top: 1px solid var(--border); overflow-wrap: anywhere; }
  .log li:first-child { border-top: 0; }
  @media (max-width: 768px) {
    .row { grid-template-columns: minmax(0, 1fr); }
    .edit { grid-template-columns: 7rem minmax(0, 1fr) auto; }
    .reset { grid-column: 1 / -1; }
    .reset-slot { display: none; }
    .value { height: 2.75rem; }
  }
</style>
