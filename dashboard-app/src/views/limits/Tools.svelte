<script lang="ts">
  import { tick } from 'svelte';
  import { Button } from '../../lib/ui/button';
  import FilterBar from '../../lib/ui/FilterBar.svelte';
  import { makePollStore } from '../../lib/stores/registry';
  import { usePoll } from '../../lib/poll';
  import {
    ALREADY_SETTING_TEXT, FORBIDDEN_TEXT, GROUPS, GUARD_OFF_TEXT, GUARD_OFF_TITLE, LOCKED_TEXT, LOG_TITLE,
    NOT_HELD_TEXT, NOT_LISTED_TEXT, NOT_LOADED_TITLE, NOT_REGISTERED_TEXT, NO_CHANGES_TEXT, NO_TOOL_MATCH_TEXT,
    READING_TEXT, REFRESH_LABEL, RETRY_LABEL, SETTING_WORDS, TOOLS_DEFAULT_TEXT, TOOLS_SUBTITLE, TOOL_FILTERS,
    UNSUPPORTED_TEXT,
    callsText, changeLine, classesText, denyTool, heldText, notOfferedLines, problemText, readGate, readLog,
    readStateText, readTools, resetTool, toolChangeText, toolShown,
    type LogAnswer, type ToolFilter, type ToolRow, type ToolSetting, type ToolsAnswer,
  } from '../../lib/autonomy_settings';

  // F115: the tools background work may use (GET /autonomy/tools), grouped as the server groups them, each Allowed or
  // Not allowed, and the last 20 changes of them (GET /autonomy/log?kind=tools). Not allowed sends PUT
  // {"setting": "denied"}; Allowed returns the tool to the default (DELETE), which is Allowed. A tool the host locked
  // cannot be changed here. Read when the tab opens, on Refresh or Retry and after every change. While the saved
  // settings could not be read the tab shows the server's sentence and no tool. Names, summaries and reasons the server
  // sends are text nodes.

  type Page = { tools: ToolsAnswer; log: LogAnswer | null };
  const store = usePoll(
    makePollStore<Page>(async (signal) => {
      const tools = await readTools(signal);
      const log = tools.enabled && tools.loaded !== false ? await readLog('tools', signal) : null;
      return { tools, log };
    }, 0),
  );

  type Note = { text: string; ok: boolean };
  let search = $state('');
  let filter = $state<string>('all');
  /** The tool whose change is in flight. */
  let busy = $state<string | null>(null);
  let notes = $state<Record<string, Note>>({});

  let gate = $derived(readGate($store.error));
  let answer = $derived($store.data?.tools ?? null);
  let places = $derived(answer?.places ?? []);
  let recorded = $derived(answer?.recorded === true);
  let held = $derived(heldText(answer?.held_in ?? []));
  let changes = $derived($store.data?.log?.changes ?? []);
  let readLine = $derived(readStateText($store, 'tools'));
  let shown = $derived((answer?.tools ?? []).filter((row) => toolShown(row, filter as ToolFilter, search)));
  let groups = $derived(
    GROUPS.map((g) => ({ ...g, rows: shown.filter((row) => row.group === g.group) })).filter((g) => g.rows.length),
  );

  const SETTINGS: ToolSetting[] = ['allowed', 'denied'];
  const slug = (name: string) => name.replace(/[^A-Za-z0-9_-]/g, '_');

  async function choose(row: ToolRow, setting: ToolSetting) {
    if (busy || row.locked || setting === row.setting) return;
    busy = row.name;
    const next = { ...notes };
    delete next[row.name];
    notes = next;
    let outcome: Note;
    try {
      const changed = setting === 'denied' ? await denyTool(row.name) : await resetTool(row.name);
      outcome = {
        text: changed.changed ? toolChangeText(row.name, changed.tool.setting) : ALREADY_SETTING_TEXT,
        ok: true,
      };
    } catch (err) {
      outcome = { text: problemText(err, 'change'), ok: false };
    }
    await store.refresh();
    busy = null;
    await tick();
    // The radio the owner clicked stays checked in the page even when the change was refused: check the setting the
    // server holds now, before the sentence says what happened.
    const held = ($store.data?.tools.tools ?? []).find((t) => t.name === row.name)?.setting ?? row.setting;
    const radio = document.querySelector<HTMLInputElement>(`input[name="tool-${slug(row.name)}"][value="${held}"]`);
    if (radio) radio.checked = true;
    notes = { ...notes, [row.name]: outcome };
    await tick();
    document.getElementById(`tool-n.${slug(row.name)}`)?.focus();
  }
</script>

<header class="view-head">
  <p class="subtitle">{TOOLS_SUBTITLE}</p>
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
    <h2>{GUARD_OFF_TITLE}</h2>
    <p>{GUARD_OFF_TEXT}</p>
  </section>
{:else if answer && answer.loaded === false}
  <section class="notice" role="status" aria-labelledby="tool-unread">
    <h2 id="tool-unread">{NOT_LOADED_TITLE}</h2>
    <p>{answer.message}</p>
    <div class="notice-actions">
      <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={$store.loading}
        onclick={() => void store.refresh()}>{RETRY_LABEL}</Button>
    </div>
  </section>
{:else if answer}
  <div class="about-guard">
    {#if held}<p class="hint">{held} {NOT_HELD_TEXT}</p>{/if}
    <p class="hint">{TOOLS_DEFAULT_TEXT}</p>
    <p class="hint">{NOT_LISTED_TEXT}</p>
  </div>
  <div class="find">
    <div class="field">
      <label for="tool-search">Search tools</label>
      <input id="tool-search" type="search" bind:value={search} autocomplete="off" spellcheck="false" />
    </div>
    <div class="filters"><FilterBar options={TOOL_FILTERS} bind:value={filter} required label="Which tools to show" /></div>
  </div>

  {#if groups.length === 0}
    <p class="hint" role="status">{NO_TOOL_MATCH_TEXT}</p>
  {/if}
  {#each groups as g (g.group)}
    <section class="card" aria-labelledby="tool-g.{g.group}">
      <h2 id="tool-g.{g.group}">{g.words}</h2>
      <ul class="tools">
        {#each g.rows as row (row.name)}
          {@const said = notes[row.name]}
          <li class="tool">
            <div class="about">
              <span class="name">{row.name}</span>
              <p class="summary">{row.registered ? row.summary : NOT_REGISTERED_TEXT}</p>
              <p class="classes">{classesText(row.classes)}</p>
            </div>
            <fieldset class="choice" disabled={row.locked || busy !== null}>
              <legend class="sr-only">Background work and {row.name}</legend>
              {#each SETTINGS as setting (setting)}
                <label class="opt opt-{setting}" class:on={row.setting === setting}>
                  <input type="radio" name="tool-{slug(row.name)}" value={setting} checked={row.setting === setting}
                    disabled={row.locked || busy !== null} onchange={() => void choose(row, setting)} />
                  <span>{SETTING_WORDS[setting]}</span>
                </label>
              {/each}
            </fieldset>
            <div class="more">
              {#if row.locked}<p class="locked">{LOCKED_TEXT}</p>{/if}
              {#each notOfferedLines(row, places) as line, i (i)}<p class="line">{line}</p>{/each}
              <p class="line">{callsText(row, recorded)}</p>
              {#if busy === row.name}<p class="line" role="status">Saving…</p>{/if}
              {#if said}
                <p class="note" class:note-err={!said.ok} id="tool-n.{slug(row.name)}" tabindex="-1" role={said.ok ? 'status' : 'alert'}>{said.text}</p>
              {/if}
            </div>
          </li>
        {/each}
      </ul>
    </section>
  {/each}

  <section class="card" aria-labelledby="tool-log">
    <h2 id="tool-log">{LOG_TITLE}</h2>
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
  <p class="read-error" role="alert">Could not load the tools: {problemText($store.error, 'see')}</p>
{:else}
  <p class="hint">Loading the tools…</p>
{/if}

<style>
  .view-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  .subtitle { font-size: 0.8125rem; color: var(--muted); margin: 0; max-width: 80ch; }
  .read-state { font-size: 0.75rem; color: var(--muted); margin: 0; max-width: 40ch; overflow-wrap: anywhere; }
  .read-failed { color: var(--warn-text); }
  h2 { font-size: 0.9375rem; font-weight: 650; margin: 0 0 0.375rem; }
  .notice { padding: 0.875rem 1rem; border-radius: var(--card-radius-sm); background: var(--warn-bg);
    border: 1px solid var(--warn-border); font-size: 0.8125rem; margin: 0; }
  .notice p { margin: 0; }
  .notice-actions { margin-top: 0.625rem; }
  .read-error { font-size: 0.8125rem; color: var(--warn-text); margin: 0; overflow-wrap: anywhere; }
  .hint { font-size: 0.75rem; color: var(--muted); margin: 0; max-width: 80ch; }
  .about-guard { display: flex; flex-direction: column; gap: 0.25rem; }
  .find { display: flex; align-items: flex-end; gap: 0.75rem 1rem; flex-wrap: wrap; }
  .field { display: flex; flex-direction: column; gap: 0.25rem; min-width: 0; }
  .field label { font-size: 0.75rem; font-weight: 600; color: var(--muted); }
  .field input { height: 2.25rem; width: 16rem; max-width: 100%; padding: 0 0.625rem; background: var(--surface);
    border: 1px solid var(--field-border); border-radius: var(--radius-sm); color: var(--text); font: inherit; font-size: 0.875rem; }
  .field input:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .filters { display: flex; background: var(--wash); border-radius: var(--ctl-radius); width: fit-content; }
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem; min-width: 0; }
  .tools { list-style: none; margin: 0.25rem 0 0; padding: 0; }
  /* A tool: what it is at the left, its two words at the right, the reasons and the result under both. */
  .tool { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; gap: 0.25rem 1rem;
    padding: 0.625rem 0; border-top: 1px solid var(--border); min-width: 0; }
  .tool:first-child { border-top: 0; }
  .about { min-width: 0; }
  .name { font-family: var(--font-mono); font-size: 0.8125rem; font-weight: 600; overflow-wrap: anywhere; }
  .summary { font-size: 0.8125rem; margin: 0.125rem 0 0; overflow-wrap: anywhere; }
  .classes { font-size: 0.75rem; color: var(--muted); margin: 0.125rem 0 0; }
  /* The two words as one pill: the chosen one tinted, green for Allowed, red for Not allowed. */
  .choice { display: inline-flex; border: 0; margin: 0; padding: 2px; border-radius: var(--ctl-radius); background: var(--wash);
    min-width: 0; }
  .choice:disabled { opacity: 0.7; }
  .opt { position: relative; display: inline-flex; align-items: center; padding: 0.25rem 0.75rem; border-radius: var(--ctl-radius);
    font-size: 0.8125rem; color: var(--muted); cursor: pointer; white-space: nowrap;
    transition: background var(--transition), color var(--transition); }
  .choice:disabled .opt { cursor: default; }
  .opt input { position: absolute; opacity: 0; width: 1px; height: 1px; margin: 0; }
  .opt:has(input:focus-visible) { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .opt-allowed.on { background: var(--ok-bg); color: var(--ok-text); font-weight: 600; }
  .opt-denied.on { background: var(--err-bg); color: var(--err-text); font-weight: 600; }
  .more { grid-column: 1 / -1; min-width: 0; }
  .locked { font-size: 0.75rem; font-weight: 600; color: var(--warn-text); margin: 0.125rem 0 0; }
  .line { font-size: 0.75rem; color: var(--muted); margin: 0.125rem 0 0; overflow-wrap: anywhere; }
  .note { font-size: 0.8125rem; color: var(--save-ok); margin: 0.25rem 0 0; overflow-wrap: anywhere; }
  .note-err { color: var(--save-err); }
  .note:focus { outline: 2px solid var(--focus-ring); outline-offset: 2px; border-radius: var(--radius-sm); }
  .log { list-style: none; margin: 0.25rem 0 0; padding: 0; font-size: 0.8125rem; }
  .log li { padding: 0.375rem 0; border-top: 1px solid var(--border); overflow-wrap: anywhere; }
  .log li:first-child { border-top: 0; }
  @media (max-width: 768px) {
    .tool { grid-template-columns: minmax(0, 1fr); }
    .opt { min-height: 2.75rem; padding: 0 1rem; }
    .field input { height: 2.75rem; }
  }
</style>
