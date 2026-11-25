<script lang="ts">
  import { tick } from 'svelte';
  import { Button } from '../../lib/ui/button';
  import FilterBar from '../../lib/ui/FilterBar.svelte';
  import { makePollStore } from '../../lib/stores/registry';
  import { usePoll } from '../../lib/poll';
  import {
    FORBIDDEN_TEXT, READING_TEXT, REFRESH_LABEL, RETRY_LABEL, UNSUPPORTED_TEXT, problemText, readGate, readStateText,
  } from '../../lib/autonomy_settings';
  import {
    ALREADY_TEXT, CORE_HELP, CORE_TITLE, CORE_WORDS, DEFAULT_LABEL, DEFAULT_TEXT, DISCARD_LABEL, HARNESS_TEXT,
    KINDS_HELP, KINDS_TITLE, MODE_HELP, MODE_TITLE, MODE_WORDS, MODEL_CALLS_OFF_TEXT, NO_DEFERRING_TEXT, NOT_REGISTERED_TEXT,
    NUMBERS_TITLE, OFF_TEXT, OFF_TITLE, SAVED_TEXT, SAVES_AT_ONCE_TEXT, SAVE_LABEL, SUBTITLE, SWITCH_WORDS,
    NUMBERS_FAILED_TEXT, TOOL_CALLS_OFF_TEXT, UNSAVED_TEXT, USAGE_FAILED_TEXT, WEIGHED_TEXT, WINDOW_OPTIONS,
    changeLoading, draftChanged, draftList, kindLines, lastChangeText, pinnedText, problemLine, readLoading,
    sourceText, toolNumbersText, totalsText,
    type Change, type LoadingAnswer, type Mode, type ToolRow, type Window,
  } from '../../lib/tool_loading';

  // F120 tool loading (GET /dashboard/tool-loading): the mode (Off, Watch only, On), the kinds of call, what Watch only
  // and On recorded per kind over the chosen window, and the core set over every registered tool. The mode and each
  // kind are saved when chosen (PUT with that field alone, merged by the server); the core set is edited here and saved
  // with one Save (PUT {core}), or set back to the default (PUT {core: null}). Read when the tab opens, on Refresh or
  // Retry, when the window changes and after every change. Names, words and reasons the server sends are text nodes.

  let windowValue = $state('7');
  const store = usePoll(
    makePollStore<LoadingAnswer>((signal) => readLoading(Number(windowValue) as Window, signal), 0),
  );

  type Note = { text: string; ok: boolean };
  let busy = $state(false);
  let note = $state<Note | null>(null);
  let coreNote = $state<Note | null>(null);
  /** The core set as the owner is editing it; null until the first read, and again after a save or a discard. */
  let draft = $state<Set<string> | null>(null);

  let gate = $derived(readGate($store.error));
  let answer = $derived($store.data);
  let readLine = $derived(readStateText($store, 'tool loading'));
  let saved = $derived(answer?.core ?? []);
  let editing = $derived(draft ?? new Set(saved));
  let unsaved = $derived(draft !== null && draftChanged(draft, saved));
  let tools = $derived(answer?.tools ?? []);

  const MODES: Mode[] = ['off', 'watch', 'on'];
  const slug = (name: string) => name.replace(/[^A-Za-z0-9_-]/g, '_');

  let lastWindow = '7';
  $effect(() => {
    if (windowValue !== lastWindow) {
      lastWindow = windowValue;
      void store.refresh();
    }
  });

  async function send(change: Change, after: (changed: boolean) => string): Promise<Note> {
    try {
      const done = await changeLoading(change);
      return { text: done.changed ? after(true) : after(false), ok: true };
    } catch (err) {
      return { text: problemText(err, 'change'), ok: false };
    }
  }

  async function choose(change: Change) {
    if (busy) return;
    busy = true;
    note = null;
    const said = await send(change, (changed) => (changed ? SAVED_TEXT : ALREADY_TEXT));
    await store.refresh();
    busy = false;
    await tick();
    // The radio the owner clicked stays checked in the page even when the change was refused: check what the server
    // holds now, before the sentence says what happened.
    recheck();
    note = said;
    await tick();
    document.getElementById('loading-note')?.focus();
  }

  function recheck() {
    const held = $store.data;
    if (!held?.mode || !MODES.includes(held.mode)) return;
    const radios = [
      `input[name="loading-mode"][value="${held.mode}"]`,
      ...(held.kinds ?? []).map((kind) => `input[name="kind-${slug(kind.key)}"][value="${kind.on ? 'on' : 'off'}"]`),
    ];
    for (const selector of radios) {
      const radio = document.querySelector<HTMLInputElement>(selector);
      if (radio) radio.checked = true;
    }
  }

  function toggle(name: string, inFull: boolean) {
    const next = new Set(editing);
    if (inFull) next.add(name);
    else next.delete(name);
    draft = next;
    coreNote = null;
  }

  async function saveCore(core: string[] | null) {
    if (busy) return;
    busy = true;
    coreNote = null;
    const said = await send({ core }, (changed) => (changed ? SAVED_TEXT : core === null ? DEFAULT_TEXT : ALREADY_TEXT));
    if (said.ok) draft = null;
    await store.refresh();
    busy = false;
    coreNote = said;
    await tick();
    document.getElementById('core-note')?.focus();
  }

  const inFull = (row: ToolRow) => row.state === 'pinned' || editing.has(row.name);
</script>

<header class="view-head">
  <p class="subtitle">{SUBTITLE}</p>
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
{:else if answer && answer.mode}
  {#if answer.problem}<p class="warn" role="alert">{problemLine(answer.problem)}</p>{/if}
  {#if answer.recorded && !answer.recorded.model_calls}<p class="hint">{MODEL_CALLS_OFF_TEXT}</p>{/if}
  {#if answer.recorded && !answer.recorded.tool_calls}<p class="hint">{TOOL_CALLS_OFF_TEXT}</p>{/if}

  <section class="card" aria-labelledby="loading-mode">
    <h2 id="loading-mode">{MODE_TITLE}</h2>
    <fieldset class="choice" disabled={busy}>
      <legend class="sr-only">{MODE_TITLE}</legend>
      {#each MODES as mode (mode)}
        <label class="opt" class:on={answer.mode === mode}>
          <input type="radio" name="loading-mode" value={mode} checked={answer.mode === mode} disabled={busy}
            onchange={() => void choose({ mode })} />
          <span>{MODE_WORDS[mode]}</span>
        </label>
      {/each}
    </fieldset>
    <p class="hint">{MODE_HELP[answer.mode]}</p>
    {#if answer.mode === 'on' && answer.deferring_client === false}<p class="warn" role="status">{NO_DEFERRING_TEXT}</p>{/if}

    <h2 id="loading-kinds" class="sub">{KINDS_TITLE}</h2>
    <ul class="rows" aria-labelledby="loading-kinds">
      {#each answer.kinds ?? [] as kind (kind.key)}
        <li class="row">
          <span class="what">{kind.words}</span>
          <fieldset class="choice" disabled={busy}>
            <legend class="sr-only">{kind.words}</legend>
            {#each [true, false] as on (on)}
              <label class="opt" class:on={kind.on === on}>
                <input type="radio" name="kind-{slug(kind.key)}" value={on ? 'on' : 'off'} checked={kind.on === on}
                  disabled={busy} onchange={() => void choose({ kinds: { [kind.key]: on } })} />
                <span>{on ? SWITCH_WORDS.on : SWITCH_WORDS.off}</span>
              </label>
            {/each}
          </fieldset>
        </li>
      {/each}
    </ul>
    <p class="hint">{KINDS_HELP}</p>
    <p class="hint">{SAVES_AT_ONCE_TEXT}</p>
    <p class="hint">{lastChangeText({ updated_at: answer.updated_at ?? null, updated_by: answer.updated_by ?? null })}</p>
    {#if busy && !coreNote}<p class="line" role="status">Saving…</p>{/if}
    {#if note}
      <p class="note" class:note-err={!note.ok} id="loading-note" tabindex="-1" role={note.ok ? 'status' : 'alert'}>{note.text}</p>
    {/if}
  </section>

  <section class="card" aria-labelledby="loading-numbers">
    <h2 id="loading-numbers">{NUMBERS_TITLE}</h2>
    <div class="filters"><FilterBar options={WINDOW_OPTIONS} bind:value={windowValue} required label="The window" /></div>
    {#if answer.numbers}
      <ul class="rows">
        {#each answer.numbers as kind (kind.key)}
          <li class="kind">
            <span class="what">{kind.words}</span>
            {#each kindLines(kind) as line, i (i)}<p class="line">{line}</p>{/each}
          </li>
        {/each}
      </ul>
      <p class="hint">{WEIGHED_TEXT}</p>
    {:else if answer.numbers_failed}
      <p class="warn" role="status">{NUMBERS_FAILED_TEXT}</p>
    {:else}
      <p class="hint">{MODEL_CALLS_OFF_TEXT}</p>
    {/if}
  </section>

  <section class="card" aria-labelledby="loading-core">
    <h2 id="loading-core">{CORE_TITLE}</h2>
    <p class="hint">{CORE_HELP}</p>
    <p class="hint">{HARNESS_TEXT}</p>
    {#if answer.totals}<p class="hint">{totalsText(answer.totals)}</p>{/if}
    {#if answer.usage_failed}<p class="warn" role="status">{USAGE_FAILED_TEXT}</p>{/if}
    <ul class="tools">
      {#each tools as row (row.name)}
        <li class="tool">
          <div class="about">
            <span class="name">{row.name}</span>
            <p class="line">{sourceText(row)}</p>
            {#if !row.registered}<p class="line">{NOT_REGISTERED_TEXT}</p>{/if}
            {#if toolNumbersText(row)}<p class="line">{toolNumbersText(row)}</p>{/if}
          </div>
          {#if row.state === 'pinned'}
            <p class="pinned">{pinnedText(row)}</p>
          {:else}
            <fieldset class="choice" disabled={busy}>
              <legend class="sr-only">The core set and {row.name}</legend>
              {#each [true, false] as full (full)}
                <label class="opt" class:on={inFull(row) === full}>
                  <input type="radio" name="core-{slug(row.name)}" value={full ? 'in' : 'out'}
                    checked={inFull(row) === full} disabled={busy} onchange={() => toggle(row.name, full)} />
                  <span>{full ? CORE_WORDS.in : CORE_WORDS.out}</span>
                </label>
              {/each}
            </fieldset>
          {/if}
        </li>
      {/each}
    </ul>
    {#if unsaved}<p class="warn" role="status">{UNSAVED_TEXT}</p>{/if}
    <div class="actions">
      <Button size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy || !unsaved}
        onclick={() => void saveCore(draftList(editing))}>{SAVE_LABEL}</Button>
      {#if unsaved}
        <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy}
          onclick={() => { draft = null; coreNote = null; }}>{DISCARD_LABEL}</Button>
      {/if}
      {#if !answer.core_default}
        <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy}
          onclick={() => void saveCore(null)}>{DEFAULT_LABEL}</Button>
      {/if}
    </div>
    {#if coreNote}
      <p class="note" class:note-err={!coreNote.ok} id="core-note" tabindex="-1" role={coreNote.ok ? 'status' : 'alert'}>{coreNote.text}</p>
    {/if}
  </section>
{:else if $store.error}
  <p class="read-error" role="alert">Could not load tool loading: {problemText($store.error, 'see')}</p>
{:else}
  <p class="hint">Loading tool loading…</p>
{/if}

<style>
  .view-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  .subtitle { font-size: 0.8125rem; color: var(--muted); margin: 0; max-width: 80ch; }
  .read-state { font-size: 0.75rem; color: var(--muted); margin: 0; max-width: 40ch; overflow-wrap: anywhere; }
  .read-failed { color: var(--warn-text); }
  h2 { font-size: 0.9375rem; font-weight: 650; margin: 0 0 0.375rem; }
  h2.sub { margin-top: 1rem; }
  .notice { padding: 0.875rem 1rem; border-radius: var(--card-radius-sm); background: var(--warn-bg);
    border: 1px solid var(--warn-border); font-size: 0.8125rem; margin: 0; }
  .notice p { margin: 0; }
  .read-error { font-size: 0.8125rem; color: var(--warn-text); margin: 0; overflow-wrap: anywhere; }
  .hint { font-size: 0.75rem; color: var(--muted); margin: 0.25rem 0 0; max-width: 80ch; }
  .warn { font-size: 0.8125rem; color: var(--warn-text); margin: 0.25rem 0 0; max-width: 80ch; overflow-wrap: anywhere; }
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem; min-width: 0; }
  .filters { display: flex; background: var(--wash); border-radius: var(--ctl-radius); width: fit-content; margin: 0.25rem 0 0.5rem; }
  .rows, .tools { list-style: none; margin: 0.25rem 0 0; padding: 0; }
  .row, .tool { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; gap: 0.25rem 1rem;
    padding: 0.5rem 0; border-top: 1px solid var(--border); min-width: 0; }
  .row:first-child, .tool:first-child, .kind:first-child { border-top: 0; }
  .kind { padding: 0.5rem 0; border-top: 1px solid var(--border); min-width: 0; }
  .what { font-size: 0.8125rem; font-weight: 600; overflow-wrap: anywhere; }
  .about { min-width: 0; }
  .name { font-family: var(--font-mono); font-size: 0.8125rem; font-weight: 600; overflow-wrap: anywhere; }
  .line { font-size: 0.75rem; color: var(--muted); margin: 0.125rem 0 0; overflow-wrap: anywhere; }
  .pinned { font-size: 0.75rem; color: var(--muted); margin: 0; max-width: 32ch; overflow-wrap: anywhere; }
  /* The words as one pill: the chosen one tinted. */
  .choice { display: inline-flex; border: 0; margin: 0; padding: 2px; border-radius: var(--ctl-radius); background: var(--wash);
    min-width: 0; width: fit-content; }
  .choice:disabled { opacity: 0.7; }
  .opt { position: relative; display: inline-flex; align-items: center; padding: 0.25rem 0.75rem; border-radius: var(--ctl-radius);
    font-size: 0.8125rem; color: var(--muted); cursor: pointer; white-space: nowrap;
    transition: background var(--transition), color var(--transition); }
  .choice:disabled .opt { cursor: default; }
  .opt input { position: absolute; opacity: 0; width: 1px; height: 1px; margin: 0; }
  .opt:has(input:focus-visible) { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .opt.on { background: var(--surface); color: var(--text); font-weight: 600; box-shadow: var(--card-shadow); }
  .actions { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.625rem; }
  .note { font-size: 0.8125rem; color: var(--save-ok); margin: 0.375rem 0 0; overflow-wrap: anywhere; }
  .note-err { color: var(--save-err); }
  .note:focus { outline: 2px solid var(--focus-ring); outline-offset: 2px; border-radius: var(--radius-sm); }
  @media (max-width: 768px) {
    .row, .tool { grid-template-columns: minmax(0, 1fr); }
    .opt { min-height: 2.75rem; padding: 0 1rem; }
  }
</style>
