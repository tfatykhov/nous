<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { Button } from '../../lib/ui/button';
  import { problemText } from '../../lib/autonomy_settings';
  import {
    DONE_TEXT, FACT_UNREADABLE_TEXT, FACTS_TITLE, LABELS, LINES_TITLE, LOADING_TEXT, NO_LINES_TEXT, REMOVED_PROFILE_TEXT,
    REMOVED_TITLE, STATUS_HELP, UNKEYED_TEXT, WORKING_TEXT, WRITING_TEXT,
    markNotEntity, readFact, readProfile, rebuild, rebuildText, removeLine, removeProfile, removedText, restoreLine,
    restoreProfile, unmarkNotEntity, verdictText, writtenText,
    type ProfileDetail, type ProfileRow,
  } from '../../lib/entity_profiles';

  // F119 PR 2: one profile, opened from its row on Browse's Profiles tab (GET /entity-profiles/{key}): its live lines,
  // each with its date and, on demand, the text of the facts it cites (the graph's node route); the lines the owner
  // removed, each with who and when; and the owner's corrections. Each correction is one request, then the profile and
  // the list are read again (onchanged). A removed profile is not read (the route answers 404): it shows its Restore.
  // Everything the server sends is a text node.

  let { row, onchanged }: { row: ProfileRow; onchanged: () => void } = $props();

  type Note = { text: string; ok: boolean };
  let profile = $state<ProfileDetail | null>(null);
  let readError = $state<string | null>(null);
  let busy = $state<string | null>(null);
  let note = $state<Note | null>(null);
  /** Fact id → its text, '' while it is read, null when it could not be read. */
  let facts = $state<Record<string, string | null>>({});
  let open = $state<Record<string, boolean>>({});
  const noteId = `profile-note-${Math.random().toString(36).slice(2, 8)}`;

  async function load() {
    readError = null;
    try {
      profile = await readProfile(row.key);
    } catch (err) {
      readError = problemText(err, 'see');
    }
  }

  onMount(() => {
    if (row.active) void load();
  });

  async function toggleFacts(lineId: string, ids: string[]) {
    open[lineId] = !open[lineId];
    if (!open[lineId]) return;
    for (const id of ids) {
      if (id in facts) continue;
      facts[id] = '';
      try {
        const found = await readFact(id);
        facts[id] = found.node?.content ?? null;
      } catch {
        facts[id] = null;
      }
    }
  }

  async function act(name: string, request: () => Promise<unknown>, done: (answer: unknown) => string) {
    if (busy) return;
    busy = name;
    note = null;
    let said: Note;
    try {
      said = { text: done(await request()), ok: true };
    } catch (err) {
      said = { text: problemText(err, 'change'), ok: false };
    }
    // A removed profile cannot be read (404); the list, read again below, shows where it went.
    if (row.active && !(name === 'removeProfile' && said.ok)) await load();
    busy = null;
    note = said;
    onchanged();
    await tick();
    document.getElementById(noteId)?.focus();
  }

  const key = $derived(row.key);
</script>

<div class="profile">
  {#if !row.active}
    <p class="help">{REMOVED_PROFILE_TEXT}</p>
    <div class="actions">
      <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy !== null}
        onclick={() => void act('restoreProfile', () => restoreProfile(key), () => DONE_TEXT.restoreProfile)}>{LABELS.restoreProfile}</Button>
    </div>
  {:else if readError}
    <p class="warn" role="alert">{readError}</p>
  {:else if !profile}
    <p class="help" role="status">{LOADING_TEXT}</p>
  {:else}
    {#if profile.owner_verdict}
      <p class="verdict">{verdictText(profile.owner_verdict)}</p>
    {:else if STATUS_HELP[profile.status]}
      <p class="help">{STATUS_HELP[profile.status]}</p>
    {/if}
    <p class="help">{writtenText(profile)}</p>
    {#if profile.unkeyed_mentions > 0}<p class="help">{UNKEYED_TEXT(profile.unkeyed_mentions)}</p>{/if}

    {#if profile.status === 'profile'}
      <h3>{LINES_TITLE}</h3>
      {#if profile.lines.length === 0}
        <p class="help">{NO_LINES_TEXT}</p>
      {:else}
        <ul class="lines">
          {#each profile.lines as line (line.id)}
            <li class="line">
              <p class="text"><span class="date">{line.said_on}</span> {line.text}</p>
              <div class="actions">
                <Button variant="ghost" size="sm" class="max-md:min-h-11 max-md:px-4" aria-expanded={open[line.id] ? 'true' : 'false'}
                  onclick={() => void toggleFacts(line.id, line.fact_ids)}>{open[line.id] ? LABELS.hideFacts : LABELS.showFacts}</Button>
                <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy !== null}
                  onclick={() => void act('removeLine', () => removeLine(key, line.id), () => DONE_TEXT.removeLine)}>{LABELS.removeLine}</Button>
              </div>
              {#if open[line.id]}
                <div class="facts" aria-label={FACTS_TITLE}>
                  {#each line.fact_ids as id (id)}
                    <p class="fact">
                      <span class="mono">{id}</span>
                      {#if facts[id] === ''}<span class="muted-text">{LOADING_TEXT}</span>
                      {:else if facts[id] === null}<span class="muted-text">{FACT_UNREADABLE_TEXT}</span>
                      {:else}<span>{facts[id]}</span>{/if}
                    </p>
                  {/each}
                </div>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    {/if}

    {#if profile.removed_lines.length > 0}
      <h3>{REMOVED_TITLE}</h3>
      <ul class="lines">
        {#each profile.removed_lines as line (line.id)}
          <li class="line">
            <p class="text"><span class="date">{line.said_on}</span> {line.text}</p>
            <p class="help">{removedText(line)}</p>
            <div class="actions">
              <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy !== null}
                onclick={() => void act('restoreLine', () => restoreLine(key, line.id), () => DONE_TEXT.restoreLine)}>{LABELS.restoreLine}</Button>
            </div>
          </li>
        {/each}
      </ul>
    {/if}

    <div class="actions profile-actions">
      {#if !profile.owner_verdict}
        <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy !== null}
          onclick={() => void act('rebuild', () => rebuild(key), (a) => rebuildText((a as { outcome: string }).outcome))}>{LABELS.rebuild}</Button>
        <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy !== null}
          onclick={() => void act('notEntity', () => markNotEntity(key), () => DONE_TEXT.notEntity)}>{LABELS.notEntity}</Button>
      {:else}
        <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy !== null}
          onclick={() => void act('undoNotEntity', () => unmarkNotEntity(key), () => DONE_TEXT.undoNotEntity)}>{LABELS.undoNotEntity}</Button>
      {/if}
      <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy !== null}
        onclick={() => void act('removeProfile', () => removeProfile(key), () => DONE_TEXT.removeProfile)}>{LABELS.removeProfile}</Button>
    </div>
  {/if}
  {#if busy}<p class="help" role="status">{busy === 'rebuild' ? WRITING_TEXT : WORKING_TEXT}</p>{/if}
  {#if note}
    <p class="note" class:note-err={!note.ok} id={noteId} tabindex="-1" role={note.ok ? 'status' : 'alert'}>{note.text}</p>
  {/if}
</div>

<style>
  /* Tokens only (hexGuard). One profile inside its expanded row: plain lines, words as buttons. */
  .profile { display: flex; flex-direction: column; gap: 0.375rem; padding: 0.5rem 0; min-width: 0; font-size: 0.8125rem; }
  h3 { font-size: 0.8125rem; font-weight: 650; margin: 0.5rem 0 0; }
  .help { margin: 0; color: var(--muted); max-width: 80ch; overflow-wrap: anywhere; }
  .verdict { margin: 0; padding: 0.5rem 0.75rem; border-radius: var(--card-radius-sm); background: var(--wash); max-width: 80ch; }
  .warn { margin: 0; color: var(--warn-text); overflow-wrap: anywhere; }
  .lines { list-style: none; margin: 0; padding: 0; }
  .line { padding: 0.5rem 0; border-top: 1px solid var(--border); min-width: 0; }
  .line:first-child { border-top: 0; }
  .text { margin: 0; overflow-wrap: anywhere; }
  .date { color: var(--muted); font-variant-numeric: tabular-nums; margin-right: 0.375rem; }
  .facts { margin: 0.375rem 0 0; padding: 0.375rem 0.625rem; background: var(--wash); border-radius: var(--card-radius-sm); }
  .fact { display: flex; flex-direction: column; gap: 0.125rem; margin: 0.25rem 0; overflow-wrap: anywhere; min-width: 0; }
  .actions { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.375rem; }
  .profile-actions { margin-top: 0.75rem; }
  .note { margin: 0.25rem 0 0; color: var(--save-ok); overflow-wrap: anywhere; }
  .note-err { color: var(--save-err); }
  .note:focus { outline: 2px solid var(--focus-ring); outline-offset: 2px; border-radius: var(--radius-sm); }
</style>
