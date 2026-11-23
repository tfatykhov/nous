<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { Button } from '../../lib/ui/button';
  import {
    CHECK_WORDS, HELP, INTRO, KEY_LABEL, LIMIT_LABEL, LOAD_FAILED, MODEL_HINT, MODEL_LABEL, NO_KEY_FILE, SAVED, SERVER_VALUE,
    SWITCH_LABEL, TITLE, YES_REMOVE, YES_SEND,
    changeSecond, checkSecond, confirmation, keyLine, limitRule, mostText, parseLimit, readSecond, refusalText,
    removeKey, removeQuestion, saveKey, sourceText, statusText, statusTone,
    type SecondState,
  } from '../../lib/outside_judge';

  // F118 PR 3: the second judge's settings. One GET when the screen opens: with the setting off on the server
  // ({enabled: false}), or a first read that fails, nothing is drawn and nothing more is asked. While the server has not
  // read its saved settings (loaded: false) the card asks again every 30 s, and stops once it has. Each change is sent
  // at once; one that needs the owner's yes is refused with the server's sentence and the daily limit it names, shown
  // in a box, and sent again with that number only on "Yes".
  // The API key is bound to `keyText` alone, cleared when its call returns, on Cancel, and when the box closes.

  type Kind = 'change' | 'key' | 'remove';
  /** One change's request; `confirmed` is the daily limit of the sentence the owner said yes to, or null. */
  type Send = (confirmed: number | null) => Promise<SecondState>;
  interface Pending { kind: Kind; text: string; calls: number; send: Send }

  const REREAD_MS = 30_000;

  let card = $state<SecondState | null>(null);
  let seen = false; // a read has said enabled: true
  let failed = $state(false);
  let busy = $state(false);
  let note = $state<{ text: string; ok: boolean } | null>(null);
  let modelText = $state('');
  let limitText = $state('');
  let editingKey = $state(false);
  let keyText = $state('');
  let pending = $state<Pending | null>(null);
  let askingRemove = $state(false);
  let checkText = $state<string | null>(null);

  const PILL = { ok: 'status-pill--ok', warn: 'status-pill--warn', neutral: '' } as const;

  function show(next: SecondState) {
    card = next;
    modelText = next.model.value;
    limitText = String(next.daily_calls.value);
  }

  let mounted = true;
  let reading: AbortController | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;

  async function load() {
    timer = null;
    reading = new AbortController();
    try {
      const answer = await readSecond(reading.signal);
      if (!mounted) return;
      if (answer.enabled === false) return; // off on the server: nothing drawn, nothing more asked
      seen = true;
      failed = false;
      show(answer);
    } catch {
      if (!mounted || !seen) return; // a first read that fails draws nothing and asks no more
      failed = true;
    }
    if (card && !card.loaded) timer = setTimeout(() => void load(), REREAD_MS);
  }

  onMount(() => {
    void load();
    return () => {
      mounted = false;
      reading?.abort();
      if (timer) clearTimeout(timer);
    };
  });

  async function focusConfirm() {
    await tick();
    document.getElementById('jev-confirm')?.querySelector('button')?.focus();
  }

  /** Send one change. A refusal that asks for the owner's yes (also after a yes, when the limit changed meanwhile)
   * opens the box with the server's sentence and the number it names. */
  async function attempt(kind: Kind, send: Send, confirmed: number | null = null) {
    busy = true;
    note = null;
    checkText = null;
    try {
      show(await send(confirmed));
      pending = null;
      note = { text: SAVED, ok: true };
      if (kind === 'key') editingKey = false;
    } catch (err) {
      const asked = confirmation(err);
      if (asked) {
        pending = { kind, text: asked.text, calls: asked.calls, send };
        void focusConfirm();
        return;
      }
      pending = null;
      note = { text: refusalText(err), ok: false };
    } finally {
      busy = false;
      if (pending === null) keyText = '';
    }
  }

  function cancelPending() {
    pending = null;
    keyText = '';
  }

  function onSwitch(e: Event) {
    const box = e.currentTarget as HTMLInputElement;
    const want = box.checked;
    box.checked = card?.on.value ?? false; // what is stored, until the server says otherwise
    void attempt('change', (confirmed) => changeSecond({ on: want }, confirmed));
  }

  function saveModel() {
    const model = modelText.trim();
    void attempt('change', (confirmed) => changeSecond({ model }, confirmed));
  }

  function saveLimit() {
    if (!card) return;
    const value = parseLimit(limitText, card.daily_calls.most);
    if (value === null) {
      note = { text: limitRule(card.daily_calls.most), ok: false };
      return;
    }
    void attempt('change', (confirmed) => changeSecond({ daily_calls: value }, confirmed));
  }

  function submitKey() {
    void attempt('key', (confirmed) => saveKey(keyText, confirmed));
  }

  function closeKey() {
    editingKey = false;
    keyText = '';
  }

  function confirmRemove() {
    askingRemove = false;
    void attempt('remove', (confirmed) => removeKey(confirmed));
  }

  async function check() {
    busy = true;
    note = null;
    checkText = null;
    try {
      checkText = CHECK_WORDS[(await checkSecond()).result] ?? null;
    } catch (err) {
      note = { text: refusalText(err), ok: false };
    } finally {
      busy = false;
    }
  }
</script>

{#if card}
  {@const s = card}
  {@const locked = s.locked === 'off'}
  {@const blocked = busy || pending !== null}
  <section class="panel" aria-labelledby="jev-title">
    <div class="panel-head">
      <h2 id="jev-title">{TITLE}</h2>
      <span class="status-pill {PILL[statusTone(s)]}" data-testid="jev-status">{statusText(s)}</span>
    </div>
    <p class="small muted intro">{INTRO}</p>
    {#if failed}<p class="note note-err" role="alert">{LOAD_FAILED}</p>{/if}
    {#if s.loaded}
      <div class="rows">
        <div class="row">
          <label class="check">
            <input type="checkbox" checked={s.on.value} disabled={locked || blocked} onchange={onSwitch} />
            {SWITCH_LABEL}
          </label>
          <p class="hint">{sourceText(s.on.source)}</p>
        </div>

        <div class="row field">
          <label for="jev-model">{MODEL_LABEL}</label>
          <div class="inline">
            <input id="jev-model" bind:value={modelText} autocomplete="off" spellcheck="false" disabled={locked || blocked}
              aria-describedby="jev-model-hint" />
            <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={locked || blocked}
              onclick={saveModel}>Save</Button>
            {#if s.model.source === 'stored' && !locked}
              <Button variant="ghost" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={blocked}
                onclick={() => void attempt('change', (confirmed) => changeSecond({ model: null }, confirmed))}>{SERVER_VALUE}</Button>
            {/if}
          </div>
          <p class="hint" id="jev-model-hint">{MODEL_HINT} · {sourceText(s.model.source)}</p>
        </div>

        <div class="row field">
          <label for="jev-limit">{LIMIT_LABEL}</label>
          <div class="inline">
            <input id="jev-limit" class="number" bind:value={limitText} inputmode="numeric" autocomplete="off"
              disabled={locked || blocked} aria-describedby="jev-limit-hint" />
            <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={locked || blocked}
              onclick={saveLimit}>Save</Button>
            {#if s.daily_calls.source !== 'server' && !locked}
              <Button variant="ghost" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={blocked}
                onclick={() => void attempt('change', (confirmed) => changeSecond({ daily_calls: null }, confirmed))}>{SERVER_VALUE}</Button>
            {/if}
          </div>
          <p class="hint" id="jev-limit-hint">{mostText(s.daily_calls.most)} · {sourceText(s.daily_calls.source)}</p>
        </div>

        <div class="row">
          <p class="key-line" data-testid="jev-key">{keyLine(s)}</p>
          {#if editingKey && !locked}
            <div class="field key-field">
              <label for="jev-key-input">{KEY_LABEL}</label>
              <input id="jev-key-input" type="password" bind:value={keyText} autocomplete="off" spellcheck="false"
                disabled={blocked} />
              <div class="actions">
                <Button size="sm" class="max-md:min-h-11 max-md:px-4" disabled={blocked || !keyText.trim()} onclick={submitKey}>Save key</Button>
                <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy} onclick={closeKey}>Cancel</Button>
              </div>
            </div>
          {:else}
            <div class="actions">
              {#if !locked}
                <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={blocked || !s.can_store_key}
                  onclick={() => { editingKey = true; askingRemove = false; }}>{s.key.source === 'stored' ? 'Replace key' : 'Add key'}</Button>
              {/if}
              {#if s.key.source === 'stored'}
                <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={blocked}
                  onclick={() => { askingRemove = true; }}>Remove key</Button>
              {/if}
              {#if !locked}
                <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={blocked} onclick={() => void check()}>Check</Button>
              {/if}
            </div>
            {#if !s.can_store_key && !locked}<p class="hint">{NO_KEY_FILE}</p>{/if}
          {/if}
          {#if askingRemove}
            <div class="confirm" role="group" aria-label="Confirm removing the key">
              <p>{removeQuestion(s)}</p>
              <div class="actions">
                <Button variant="destructive" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={blocked} onclick={confirmRemove}>{YES_REMOVE}</Button>
                <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" onclick={() => { askingRemove = false; }}>Cancel</Button>
              </div>
            </div>
          {/if}
          {#if checkText}<p class="note" role="status">{checkText}</p>{/if}
        </div>
      </div>

      {#if pending}
        <!-- The server's sentence: plain text interpolation only. -->
        <div class="confirm" id="jev-confirm" role="group" aria-label="Confirm sending to {s.sends_to}">
          <p>{pending.text}</p>
          <div class="actions">
            <Button size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy}
              onclick={() => pending && void attempt(pending.kind, pending.send, pending.calls)}>{YES_SEND}</Button>
            <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy} onclick={cancelPending}>Cancel</Button>
          </div>
        </div>
      {/if}
      {#if note}<p class="note" class:note-err={!note.ok} role={note.ok ? 'status' : 'alert'}>{note.text}</p>{/if}
      <p class="small muted help">{HELP}</p>
    {/if}
  </section>
{/if}

<style>
  .panel { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.25rem; margin-top: 1.25rem; min-width: 0; }
  .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem 1rem; flex-wrap: wrap; margin-bottom: 0.5rem; }
  h2 { font-size: 0.9375rem; font-weight: 600; margin: 0; }
  .intro { margin: 0 0 0.75rem; max-width: 72ch; }
  .rows { display: flex; flex-direction: column; gap: 0.875rem; }
  .row { min-width: 0; }
  .check { display: inline-flex; align-items: center; gap: 0.5rem; font-size: 0.875rem; cursor: pointer; }
  .check input { width: 1rem; height: 1rem; accent-color: var(--action); cursor: pointer; }
  .check input:disabled { cursor: default; }
  .field { display: flex; flex-direction: column; gap: 0.25rem; max-width: 36rem; }
  .field label { font-size: 0.75rem; font-weight: 600; color: var(--muted); }
  .inline { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .field input { height: 2.5rem; padding: 0 0.625rem; background: var(--surface); border: 1px solid var(--field-border);
    border-radius: var(--card-radius-sm); color: var(--text); font: inherit; font-size: 0.875rem; min-width: 0; flex: 1 1 12rem; }
  .field input.number { flex: 0 1 8rem; }
  .field input:focus-visible, .check input:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .field input:disabled { opacity: 0.6; }
  .key-field { margin-top: 0.5rem; }
  .key-line { font-size: 0.875rem; margin: 0; overflow-wrap: anywhere; }
  .hint { font-size: 0.75rem; color: var(--muted); margin: 0.25rem 0 0; }
  .actions { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.5rem; }
  .confirm { margin-top: 0.75rem; padding: 0.75rem 0.875rem; border-radius: var(--card-radius-sm); background: var(--wash); font-size: 0.875rem; }
  .confirm p { margin: 0; max-width: 72ch; }
  .note { font-size: 0.8125rem; color: var(--save-ok); margin: 0.5rem 0 0; overflow-wrap: anywhere; }
  .note-err { color: var(--save-err); }
  .help { margin: 0.875rem 0 0; }
  .small { font-size: 0.75rem; }
  .muted { color: var(--muted); }
</style>
