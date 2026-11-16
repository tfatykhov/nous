<script lang="ts">
  import { tick } from 'svelte';
  import { Button } from '../../lib/ui/button';
  import {
    TOOLS_ADD_TITLE, TOOLS_NONE_TEXT, TOOLS_NOT_USED_TEXT, TOOLS_TITLE, TOOL_ALLOWED_TEXT, TOOL_ALREADY_ALLOWED_TEXT,
    TOOL_CHOOSE_LABEL,
    TOOL_ALREADY_REMOVED_TEXT, TOOL_RELOAD_TEXT, TOOL_REMOVED_TEXT,
    allowTool, goalTools, removeTool, toolAllowedOnText, toolGroups, toolRefusalText, toolsLimitText, toolsReadFailedText,
    type GoalToolOption, type GoalTools, type NewGoalTools,
  } from '../../lib/goals';

  // F115 per-goal tools: the tools this goal's own background sessions may use without asking you (GET
  // /goals/{ref}/tools), each with what it does, when it was allowed and, when it is not used now, why; and the tools
  // it could be allowed, each with what it does, or why it cannot be allowed now. Allow asks once, repeating the
  // server's sentence; Remove asks nothing. Every sentence is the server's; the page lays them out. Read when the goal
  // shown or its state changes, and after every change. With the grants off on the server, nothing is shown.
  // On the create form (`pick`: GET /goals/options `tools`), the same rows with a box each instead of a button: the
  // form asks once for the lot before it creates the goal.
  let {
    goal = null,
    pick = null,
    chosen = $bindable([]),
    disabled = false,
  }: {
    goal?: { ref: string; state: string; updated_at: string | null } | null;
    pick?: NewGoalTools | null;
    chosen?: string[];
    disabled?: boolean;
  } = $props();

  type Note = { text: string; ok: boolean };
  let answer = $state<GoalTools | null>(null);
  let readError = $state<unknown>(null);
  let busy = $state<string | null>(null);
  let confirming = $state<string | null>(null);
  let notes = $state<Record<string, Note>>({});

  const slug = (name: string) => name.replace(/[^A-Za-z0-9_-]/g, '_');
  const byId = (id: string) => document.getElementById(id);

  /** The goal this section shows now; '' once it is gone. Read after every await instead of the props. */
  let shown = '';

  async function read(ref: string) {
    try {
      const got = await goalTools(ref);
      if (ref !== shown) return; // another goal is shown now, or none
      answer = got;
      readError = null;
    } catch (err) {
      if (ref !== shown) return;
      readError = err;
    }
  }

  // The goal shown, its state or its last change: read again (a pause or a resume changes where the tools apply). For
  // another goal, nothing of this one's carries over.
  $effect(() => {
    if (!goal) return;
    const ref = goal.ref;
    void goal.state;
    void goal.updated_at;
    if (ref !== shown) {
      shown = ref;
      answer = null;
      readError = null;
      confirming = null;
      notes = {};
    }
    void read(ref);
  });
  $effect(() => () => {
    shown = '';
  });

  let on = $derived(answer !== null && answer.enabled && answer.grants_enabled === true);
  let canEdit = $derived(answer?.can_edit === true);
  let grants = $derived(answer?.grants ?? []);
  let groups = $derived(toolGroups(answer?.available ?? []));

  async function ask(option: GoalToolOption) {
    if (busy) return;
    confirming = option.tool;
    await tick();
    byId(`tool-q.${slug(option.tool)}`)?.focus();
  }

  async function no(tool: string) {
    confirming = null;
    await tick();
    byId(`tool-a.${slug(tool)}`)?.focus();
  }

  /** One change; every control of the section waits for it and the read after it. */
  async function change(tool: string, run: () => Promise<GoalTools>, done: [string, string]) {
    if (busy || !goal) return;
    const ref = goal.ref;
    busy = tool;
    confirming = null;
    const next = { ...notes };
    delete next[tool];
    notes = next;
    let outcome: Note;
    try {
      const changed = await run();
      outcome = { text: changed.reload ? TOOL_RELOAD_TEXT : changed.changed ? done[0] : done[1], ok: true };
    } catch (err) {
      outcome = { text: toolRefusalText(err), ok: false };
    }
    await read(ref);
    busy = null;
    if (ref !== shown) return;
    notes = { ...notes, [tool]: outcome };
    await tick();
    byId(`tool-n.${slug(tool)}`)?.focus();
  }

  const allow = (option: GoalToolOption) =>
    change(option.tool, () => allowTool(goal!.ref, option), [TOOL_ALLOWED_TEXT, TOOL_ALREADY_ALLOWED_TEXT]);
  const remove = (tool: string) =>
    change(tool, () => removeTool(goal!.ref, tool), [TOOL_REMOVED_TEXT, TOOL_ALREADY_REMOVED_TEXT]);

  let pickGroups = $derived(toolGroups(pick?.available ?? []));
  function toggle(tool: string, on: boolean) {
    chosen = on ? [...chosen.filter((t) => t !== tool), tool] : chosen.filter((t) => t !== tool);
  }
</script>

<!-- Escape closes an open confirmation, as No does. -->
<svelte:window onkeydown={(e) => { if (e.key === 'Escape' && confirming && busy === null) void no(confirming); }} />

{#snippet said(tool: string)}
  {@const note = notes[tool]}
  {#if note}
    <p class="note" class:note-err={!note.ok} id="tool-n.{slug(tool)}" tabindex="-1" role={note.ok ? 'status' : 'alert'}>{note.text}</p>
  {/if}
{/snippet}

{#if pick}
  <div class="card picking" data-section="goal-tools">
    <h3>{TOOLS_TITLE}</h3>
    <p class="hint">{pick.applies_in}</p>
    {#each pickGroups as group (group.key)}
      <section class="group" aria-label={group.words}>
        <p class="group-words">{group.words}</p>
        <ul class="tools">
          {#each group.rows as option (option.tool)}
            <li class="tool">
              <div class="about">
                <span class="name">{option.label}</span>
                <p class="says">{option.says}</p>
                {#if option.blocked}<p class="line blocked">{option.blocked.sentence}</p>{/if}
              </div>
              {#if !option.blocked}
                <label class="choose">
                  <input type="checkbox" id="tool-c.{slug(option.tool)}" checked={chosen.includes(option.tool)}
                    disabled={disabled || (!chosen.includes(option.tool) && chosen.length >= pick.limit)}
                    onchange={(e) => toggle(option.tool, e.currentTarget.checked)} />
                  <span>{TOOL_CHOOSE_LABEL}</span>
                </label>
              {/if}
            </li>
          {/each}
        </ul>
      </section>
    {/each}
    <p class="hint">{toolsLimitText(pick.limit)}</p>
    <p class="hint">{pick.never}</p>
  </div>
{:else if on && answer}
  <div class="card" data-section="goal-tools">
    <h3>{TOOLS_TITLE}</h3>
    <p class="hint">{answer.applies_in}</p>
    {#if grants.length === 0}
      <p class="hint">{TOOLS_NONE_TEXT}</p>
    {:else}
      <ul class="tools" aria-label={TOOLS_TITLE}>
        {#each grants as grant (grant.tool)}
          <li class="tool">
            <div class="about">
              <span class="name">{grant.label}</span>
              <p class="says">{grant.says}</p>
              <p class="line">{toolAllowedOnText(grant.granted_at)}</p>
              {#if !grant.usable}<p class="line not-used">{TOOLS_NOT_USED_TEXT} {grant.not_usable_why ?? ''}</p>{/if}
            </div>
            {#if canEdit}
              <div class="buttons">
                <Button id="tool-r.{slug(grant.tool)}" size="sm" variant="outline" class="max-md:min-h-11 max-md:px-4"
                  disabled={busy !== null} onclick={() => void remove(grant.tool)}>{busy === grant.tool ? 'Working…' : 'Remove'}</Button>
              </div>
            {/if}
            <div class="more">{@render said(grant.tool)}</div>
          </li>
        {/each}
      </ul>
    {/if}

    {#if canEdit && groups.length}
      <h4>{TOOLS_ADD_TITLE}</h4>
      {#each groups as group (group.key)}
        <section class="group" aria-label={group.words}>
          <p class="group-words">{group.words}</p>
          <ul class="tools">
            {#each group.rows as option (option.tool)}
              <li class="tool">
                <div class="about">
                  <span class="name">{option.label}</span>
                  <p class="says">{option.says}</p>
                  {#if option.blocked}<p class="line blocked">{option.blocked.sentence}</p>{/if}
                </div>
                {#if !option.blocked}
                  <div class="buttons">
                    <Button id="tool-a.{slug(option.tool)}" size="sm" variant="outline" class="max-md:min-h-11 max-md:px-4"
                      disabled={busy !== null} aria-expanded={confirming === option.tool}
                      onclick={() => void ask(option)}>{busy === option.tool ? 'Working…' : 'Allow'}</Button>
                  </div>
                {/if}
                <div class="more">
                  {#if confirming === option.tool}
                    <div class="confirm" role="group" aria-label="Confirm: allow {option.label}">
                      <p id="tool-q.{slug(option.tool)}" tabindex="-1">{option.confirm}</p>
                      <div class="actions">
                        <Button size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy !== null}
                          onclick={() => void allow(option)}>Yes, allow</Button>
                        <Button size="sm" variant="outline" class="max-md:min-h-11 max-md:px-4" disabled={busy !== null}
                          onclick={() => void no(option.tool)}>No</Button>
                      </div>
                    </div>
                  {/if}
                  {@render said(option.tool)}
                </div>
              </li>
            {/each}
          </ul>
        </section>
      {/each}
    {/if}
    {#each Object.keys(notes).filter((tool) => !grants.some((g) => g.tool === tool) && !groups.some((g) => g.rows.some((r) => r.tool === tool))) as tool (tool)}
      {@render said(tool)}
    {/each}
    {#if canEdit && answer.limit}<p class="hint">{toolsLimitText(answer.limit)}</p>{/if}
    <p class="hint">{answer.never}</p>
  </div>
{:else if readError !== null}
  <div class="card" data-section="goal-tools">
    <h3>{TOOLS_TITLE}</h3>
    <p class="hint">{toolsReadFailedText(readError)}</p>
  </div>
{/if}

<style>
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem; margin-bottom: 1.25rem; min-width: 0; }
  h3 { font-size: 0.9375rem; font-weight: 650; margin: 0 0 0.5rem; }
  h4 { font-size: 0.8125rem; font-weight: 650; margin: 1rem 0 0.25rem; }
  .hint { font-size: 0.75rem; color: var(--muted); margin: 0.25rem 0 0; max-width: 80ch; }
  .group { margin-top: 0.5rem; }
  .group-words { font-size: 0.75rem; font-weight: 600; color: var(--muted); margin: 0.25rem 0 0; overflow-wrap: anywhere; }
  .tools { list-style: none; margin: 0.25rem 0 0; padding: 0; }
  /* A tool: what it does at the left, its button at the right, the confirmation and the result under both. */
  .tool { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; gap: 0.25rem 1rem;
    padding: 0.625rem 0; border-top: 1px solid var(--border); min-width: 0; }
  .tool:first-child { border-top: 0; }
  .about { min-width: 0; }
  .name { font-family: var(--font-mono); font-size: 0.8125rem; font-weight: 600; overflow-wrap: anywhere; }
  .says { font-size: 0.8125rem; margin: 0.125rem 0 0; overflow-wrap: anywhere; }
  .line { font-size: 0.75rem; color: var(--muted); margin: 0.125rem 0 0; overflow-wrap: anywhere; }
  .not-used, .blocked { color: var(--warn-text); }
  .more { grid-column: 1 / -1; min-width: 0; }
  .confirm { margin-top: 0.25rem; padding: 0.75rem 0.875rem; border-radius: var(--card-radius-sm); background: var(--wash); font-size: 0.875rem; }
  .confirm p { margin: 0; overflow-wrap: anywhere; }
  .confirm p:focus { outline: 2px solid var(--focus-ring); outline-offset: 2px; border-radius: var(--radius-sm); }
  .actions { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.5rem; }
  .note { font-size: 0.8125rem; color: var(--save-ok); margin: 0.25rem 0 0; overflow-wrap: anywhere; }
  .note-err { color: var(--warn-text); }
  .note:focus { outline: 2px solid var(--focus-ring); outline-offset: 2px; border-radius: var(--radius-sm); }
  /* On the create form the form's own card frames it. */
  .picking { margin: 0; padding: 0; border: 0; box-shadow: none; background: none; }
  .choose { display: inline-flex; align-items: center; gap: 0.5rem; font-size: 0.8125rem; cursor: pointer; white-space: nowrap; }
  .choose input { width: 1rem; height: 1rem; }
  @media (max-width: 768px) {
    .tool { grid-template-columns: minmax(0, 1fr); }
    .choose { min-height: 2.75rem; }
  }
</style>
