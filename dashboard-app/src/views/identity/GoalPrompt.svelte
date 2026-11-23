<script lang="ts">
  import { onMount } from 'svelte';
  import { Button } from '../../lib/ui/button';
  import {
    BLANK, CONFIRM_RESET, CONFIRM_SAVE, INTRO, LABEL, RESET, RESET_DONE, SAVE, SAVED, SAVING, SHOWING_DEFAULT, TITLE,
    UNSAVED,
    codePoints, countText, normalized, readPrompt, refusalText, resetPrompt, savePrompt, savedLine,
    type PromptState,
  } from '../../lib/goal_prompt';

  // The default goal prompt, on the Identity screen. One GET when the screen opens: with goals off on the server
  // ({enabled: false}), or a read that fails, nothing is drawn and nothing more is asked. Save and Reset each ask first,
  // and a refusal shows the server's sentence with the draft kept.

  let prompt = $state<PromptState | null>(null);
  let draft = $state('');
  let busy = $state(false);
  let status = $state<{ text: string; ok: boolean } | null>(null);

  const count = $derived(codePoints(normalized(draft)));
  const over = $derived(prompt !== null && count > prompt.max_chars);
  const blank = $derived(count === 0);
  const changed = $derived(prompt !== null && normalized(draft) !== prompt.text);

  function show(next: PromptState) {
    prompt = next;
    draft = next.text;
  }

  onMount(() => {
    const reading = new AbortController();
    readPrompt(reading.signal).then(
      (answer) => { if (answer.enabled === true) show(answer); },
      () => {}, // goals off, an older server, or no answer: the card stays away
    );
    return () => reading.abort();
  });

  async function save() {
    if (!confirm(CONFIRM_SAVE)) return;
    busy = true;
    status = { text: SAVING, ok: true };
    try {
      show(await savePrompt(draft));
      status = { text: SAVED, ok: true };
    } catch (err) {
      status = { text: refusalText(err, 'save'), ok: false };
    } finally {
      busy = false;
    }
  }

  async function reset() {
    if (!confirm(CONFIRM_RESET)) return;
    busy = true;
    status = { text: SAVING, ok: true };
    try {
      show(await resetPrompt());
      status = { text: RESET_DONE, ok: true };
    } catch (err) {
      status = { text: refusalText(err, 'reset'), ok: false };
    } finally {
      busy = false;
    }
  }
</script>

{#if prompt}
  <section class="panel" aria-labelledby="goal-prompt-title">
    <h2 id="goal-prompt-title">{TITLE}</h2>
    <div class="prompt-card">
      <p class="intro">{INTRO}</p>
      <p class="state-line" data-testid="goal-prompt-state">
        {prompt.is_default ? SHOWING_DEFAULT : savedLine(prompt.updated_at)}
      </p>
      <label class="field-label" for="goal-prompt-text">{LABEL}</label>
      <textarea id="goal-prompt-text" rows={12} bind:value={draft} aria-describedby="goal-prompt-count"></textarea>
      <p class="count" class:count-over={over} id="goal-prompt-count">{countText(count, prompt.max_chars)}</p>
      {#if blank}
        <p class="hint hint-err">{BLANK}</p>
      {:else if changed}
        <p class="hint">{UNSAVED}</p>
      {/if}
      <div class="card-actions">
        <Button
          type="button"
          variant="outline"
          size="sm"
          class="max-md:min-h-11 max-md:px-4"
          onclick={save}
          disabled={busy || !changed || blank || over}
        >
          {SAVE}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          class="max-md:min-h-11 max-md:px-4"
          onclick={reset}
          disabled={busy || prompt.is_default}
        >
          {RESET}
        </Button>
        {#if status}
          <span
            class="save-status"
            class:save-ok={status.ok && !busy}
            class:save-err={!status.ok}
            role={status.ok ? 'status' : 'alert'}
          >
            {status.text}
          </span>
        {/if}
      </div>
    </div>
  </section>
{/if}

<style>
  /* The panel, title, field and save message of Identity.svelte, whose rules are scoped there. */
  .panel {
    margin-bottom: 2rem;
  }

  h2 {
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--text);
    margin: 0 0 0.875rem;
  }

  .prompt-card {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 0.875rem;
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius-sm);
    box-shadow: var(--card-shadow);
    min-width: 0;
  }

  .intro,
  .state-line {
    font-size: 0.8125rem;
    line-height: 1.5;
    margin: 0;
    max-width: 72ch;
  }

  .intro {
    color: var(--muted);
  }

  .state-line {
    color: var(--text);
  }

  .field-label {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--muted);
    margin-top: 0.25rem;
  }

  /* A field's boundary is 3:1 on paper (--field-border, WCAG 1.4.11). */
  textarea {
    width: 100%;
    box-sizing: border-box;
    padding: 0.5rem 0.625rem;
    background: var(--bg);
    border: 1px solid var(--field-border);
    border-radius: var(--card-radius-sm);
    color: var(--text);
    font-family: inherit;
    font-size: 0.8125rem;
    line-height: 1.5;
    resize: vertical;
  }

  textarea:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  .count,
  .hint {
    font-size: 0.75rem;
    color: var(--muted);
    margin: 0;
  }

  .count.count-over,
  .hint.hint-err {
    color: var(--err-text);
  }

  .card-actions {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    flex-wrap: wrap;
  }

  .save-status {
    font-size: 0.75rem;
    color: var(--muted);
  }

  .save-status.save-ok {
    color: var(--save-ok);
  }

  .save-status.save-err {
    color: var(--save-err);
  }
</style>
