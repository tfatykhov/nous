<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { get } from 'svelte/store';
  import { Button } from '../../lib/ui/button';
  import { makePollStore } from '../../lib/stores/registry';
  import { usePoll } from '../../lib/poll';
  import {
    CODE_HELP, EMPTY_TEXT, END_TEXT, FORBIDDEN_TEXT, INTRO_TEXT, LIMITED_TEXT, NAME_HINT, NAME_MAX, REPLACED_TEXT,
    REVOKE_TEXT, UNSUPPORTED_TEXT,
    codeEnd, deviceLine, listDevices, listGate, openPairing, pairingLine, problemText, revokeDevice, revokedLine,
    triesText, validForText,
    type CodeEnd, type Device, type DeviceList,
  } from '../../lib/devices';

  // The companion devices paired with this Nous (GET /a2ui/devices), the one pairing code the owner can open for a
  // new one, and Revoke. It sits outside the MCP screen's gate: it is shown whether or not the MCP client is on. The
  // code lives only in this component's state, never in the poll store, storage or the address, so a reload drops it.
  // The list is read when the screen opens, after every change, on the page's Refresh, and every 15 s while a code is
  // on screen (to see it used); never while the tab is hidden. A device's name is a text node.

  /** How often the list is read while a code is on screen. */
  const POLL_MS = 15_000;

  /** How many codes were made. A read carries the count it started under: one that started before the code on screen
   * was made says nothing about that code. */
  let made = 0;
  type Page = { list: DeviceList; made: number };
  const store = usePoll(makePollStore<Page>((signal) => {
    const asked = made;
    return listDevices(signal).then((list) => ({ list, made: asked }));
  }, 0));

  /** Read the list now: the screen's Refresh reads it too. */
  export function refresh(): Promise<void> {
    return store.refresh();
  }

  type Shown = { code: string; deadline: number; made: number; replaced: boolean };
  type Note = { text: string; ok: boolean };
  let shown = $state<Shown | null>(null);
  /** How the code on screen ended: said in its place until a new one is made. */
  let ended = $state<CodeEnd | null>(null);
  let name = $state('');
  /** The change in flight: 'pair', or the id of the device being revoked. */
  let busy = $state<string | null>(null);
  let pairError = $state<string | null>(null);
  let note = $state<Note | null>(null);
  /** The device whose Revoke asks its sentence. */
  let confirming = $state<string | null>(null);

  // 403 and 404 of the list replace the section; read before the data, so an older list never hides them.
  let gate = $derived(listGate($store.error));
  let list = $derived($store.data?.list ?? null);
  let live = $derived(list?.devices.filter((d) => !d.revoked_at) ?? []);
  let revoked = $derived(list?.devices.filter((d) => d.revoked_at) ?? []);
  /** The read that speaks of the code on screen, if any has landed. */
  let current = $derived(shown && $store.data?.made === shown.made ? $store.data.list.pairing : undefined);
  let tries = $derived(current === undefined ? '' : triesText(current));
  let serverLine = $derived(!shown && !ended && list ? pairingLine(list.pairing) : null);

  // The countdown's clock, ticking while a code is on screen.
  let clock = $state(Date.now());
  $effect(() => {
    if (!shown) return;
    const ticking = setInterval(() => (clock = Date.now()), 1000);
    return () => clearInterval(ticking);
  });

  function finish(end: CodeEnd) {
    shown = null;
    ended = end;
  }

  // A read made after the code: once it says the code is used, closed, expired or gone, the code leaves the screen.
  $effect(() => {
    const page = $store.data;
    untrack(() => {
      if (!shown || !page || page.made !== shown.made) return;
      const end = codeEnd(page.list.pairing);
      if (end) finish(end);
    });
  });
  $effect(() => {
    if (shown && clock >= shown.deadline) untrack(() => finish('expired'));
  });

  // While a code is on screen and the tab is shown, the list is read every 15 s; a tick finds a read in flight and
  // lets it finish (it is given up after 15 s on its own). A tab shown again reads at once.
  let hidden = $state(typeof document !== 'undefined' && document.hidden);
  $effect(() => {
    const seen = () => {
      hidden = document.hidden;
      if (!hidden && untrack(() => shown) && !get(store).loading) void store.refresh();
    };
    document.addEventListener('visibilitychange', seen);
    return () => document.removeEventListener('visibilitychange', seen);
  });
  let polling = $derived(shown !== null && !hidden && gate === null);
  $effect(() => {
    if (!polling) return;
    const reading = setInterval(() => {
      if (!get(store).loading) void store.refresh();
    }, POLL_MS);
    return () => clearInterval(reading);
  });

  // A device revoked elsewhere takes its open question with it.
  $effect(() => {
    const ids = live.map((d) => d.id);
    untrack(() => {
      if (confirming && !ids.includes(confirming)) confirming = null;
    });
  });

  const byId = (id: string) => document.getElementById(id);

  async function pair(e: SubmitEvent) {
    e.preventDefault();
    if (busy) return;
    busy = 'pair';
    pairError = null;
    note = null;
    const had = shown !== null;
    try {
      const opened = await openPairing(name.trim());
      made += 1;
      clock = Date.now();
      shown = { code: opened.code, deadline: clock + opened.expires_in * 1000, made, replaced: had };
      ended = null;
      name = '';
    } catch (err) {
      pairError = problemText(err, 'pair');
      // With no answer, a new code may have been made and the one on screen replaced: it is not shown as working.
      if ((err as { status?: number } | null)?.status === undefined) shown = null;
    }
    busy = null;
    void store.refresh();
    await tick();
    if (shown) byId('dev-code')?.focus();
  }

  async function done() {
    shown = null;
    await tick();
    byId('dev-pair')?.focus();
  }

  async function askRevoke(id: string) {
    confirming = id;
    note = null;
    await tick();
    // The focus goes to the sentence, which is read first, never to Yes.
    byId(`dev-s.${id}`)?.focus();
  }

  async function cancel() {
    const id = confirming;
    confirming = null;
    await tick();
    if (id) byId(`dev-r.${id}`)?.focus();
  }

  async function revoke(d: Device) {
    if (busy) return;
    busy = d.id;
    confirming = null;
    note = null;
    let outcome: Note;
    try {
      const answer = await revokeDevice(d.id);
      outcome = { text: answer.revoked ? `Revoked ${d.name}.` : `${d.name} was already revoked.`, ok: true };
    } catch (err) {
      outcome = { text: problemText(err, 'revoke'), ok: false };
    }
    await store.refresh();
    busy = null;
    note = outcome;
    await tick();
    (byId(`dev-r.${d.id}`) ?? byId('dev-top'))?.focus();
  }

  const TONE = { ok: 'tone-ok', warn: 'tone-warn', neutral: 'tone-neutral' } as const;
</script>

<!-- Escape closes an open Revoke question, as Cancel does. -->
<svelte:window onkeydown={(e) => { if (e.key === 'Escape' && confirming && busy === null) void cancel(); }} />

<section class="card" aria-labelledby="dev-top">
  <h2 id="dev-top" tabindex="-1">Devices</h2>
  <p class="lead">{INTRO_TEXT}</p>

  {#if gate === 'forbidden'}
    <p class="notice" role="status">{FORBIDDEN_TEXT}</p>
  {:else if gate === 'unsupported'}
    <p class="notice" role="status">{UNSUPPORTED_TEXT}</p>
  {:else if list}
    {#if $store.error}
      <p class="note note-err" role="alert">Could not read the list again: {problemText($store.error, 'see')} What is shown is from the last read.</p>
    {/if}

    <h3>Pair a device</h3>
    <form onsubmit={pair}>
      <div class="field">
        <label for="dev-name">Name (optional)</label>
        <input id="dev-name" bind:value={name} maxlength={NAME_MAX} autocomplete="off" spellcheck="false"
          aria-describedby="dev-name-hint" disabled={busy !== null} />
        <p id="dev-name-hint" class="hint">{NAME_HINT}</p>
      </div>
      <div class="actions">
        <Button id="dev-pair" type="submit" size="sm" variant={shown ? 'outline' : 'default'} class="max-md:min-h-11 max-md:px-4"
          disabled={busy !== null}>
          {busy === 'pair' ? 'Making a code…' : shown ? 'Show a new code' : 'Show a code'}
        </Button>
      </div>
    </form>
    {#if shown}
      <div class="code-box" role="group" aria-label="Pairing code">
        <p class="code" id="dev-code" tabindex="-1">{shown.code}</p>
        <p class="valid">{validForText(shown.deadline - clock)}</p>
        <p class="hint">{CODE_HELP}</p>
        {#if shown.replaced}<p class="hint">{REPLACED_TEXT}</p>{/if}
        {#if tries}<p class="line tone-warn">{tries}</p>{/if}
        <div class="actions">
          <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" onclick={() => void done()}>Done</Button>
        </div>
      </div>
    {:else if ended}
      <p class="line {TONE[END_TEXT[ended].tone]}" role="status">{END_TEXT[ended].text}</p>
    {:else if serverLine}
      <p class="line {TONE[serverLine.tone]}">{serverLine.text}</p>
    {/if}
    {#if list.redeem.limited}<p class="line tone-warn" role="status">{LIMITED_TEXT}</p>{/if}
    {#if pairError}<p class="note note-err" role="alert">{pairError}</p>{/if}

    <h3>Paired devices</h3>
    {#if live.length === 0}
      <p class="hint">{EMPTY_TEXT}</p>
    {:else}
      <ul class="devices">
        {#each live as d (d.id)}
          <li class="device">
            <div class="device-head">
              <span class="device-name">{d.name}</span>
              <Button id="dev-r.{d.id}" variant="outline" size="sm"
                class="max-md:min-h-11 max-md:px-4 hover:border-[var(--err-text)] hover:text-[var(--err-text)]"
                disabled={busy !== null} aria-label="Revoke {d.name}" aria-expanded={confirming === d.id}
                onclick={() => void askRevoke(d.id)}>{busy === d.id ? 'Revoking…' : 'Revoke'}</Button>
            </div>
            <p class="device-line">{deviceLine(d)}</p>
            {#if confirming === d.id}
              <div class="confirm" id="dev-c.{d.id}" role="group" aria-label="Confirm revoking {d.name}" aria-describedby="dev-s.{d.id}">
                <p id="dev-s.{d.id}" tabindex="-1">{REVOKE_TEXT}</p>
                <div class="actions">
                  <Button variant="destructive" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={busy !== null}
                    onclick={() => void revoke(d)}>Yes, revoke</Button>
                  <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" onclick={() => void cancel()}>Cancel</Button>
                </div>
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if revoked.length}
      <ul class="devices revoked" aria-label="Revoked devices">
        {#each revoked as d (d.id)}
          <li class="device">
            <span class="device-name">{d.name}</span>
            <p class="device-line">{revokedLine(d)}</p>
          </li>
        {/each}
      </ul>
    {/if}
    {#if note}<p class="note" class:note-err={!note.ok} role={note.ok ? 'status' : 'alert'}>{note.text}</p>{/if}
  {:else if $store.error}
    <p class="note note-err" role="alert">Could not load the devices: {problemText($store.error, 'see')}</p>
  {:else}
    <p class="hint">Loading the devices…</p>
  {/if}
</section>

<style>
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem; margin-bottom: 1.25rem; min-width: 0; }
  h2 { font-size: 0.9375rem; font-weight: 650; margin: 0 0 0.375rem; }
  h2:focus { outline: none; }
  h3 { font-size: 0.8125rem; font-weight: 600; color: var(--muted); margin: 1rem 0 0.25rem; }
  .lead { font-size: 0.8125rem; color: var(--muted); margin: 0; max-width: 72ch; }
  .notice { padding: 0.875rem 1rem; border-radius: var(--card-radius-sm); background: var(--warn-bg);
    border: 1px solid var(--warn-border); font-size: 0.8125rem; margin: 0.75rem 0 0; }
  .hint { font-size: 0.75rem; color: var(--muted); margin: 0.25rem 0 0; }
  .line { font-size: 0.8125rem; margin: 0.5rem 0 0; overflow-wrap: anywhere; }
  .tone-ok { color: var(--save-ok); }
  .tone-warn { color: var(--warn-text); }
  .tone-neutral { color: var(--muted); }
  .note { font-size: 0.8125rem; color: var(--save-ok); margin: 0.5rem 0 0; overflow-wrap: anywhere; }
  .note-err { color: var(--save-err); }
  form { display: flex; flex-direction: column; gap: 0.5rem; }
  .field { display: flex; flex-direction: column; gap: 0.25rem; max-width: 24rem; }
  .field label { font-size: 0.75rem; font-weight: 600; color: var(--muted); }
  .field input { height: 2.75rem; padding: 0 0.625rem; background: var(--surface); border: 1px solid var(--field-border);
    border-radius: var(--radius-sm); color: var(--text); font: inherit; font-size: 0.875rem; min-width: 0; }
  .field input:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .field input:disabled { opacity: 0.6; }
  .field .hint { margin: 0; }
  .actions { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  /* The code, large enough to read across a desk and type on a phone. */
  .code-box { margin-top: 0.75rem; padding: 0.875rem 1rem; border-radius: var(--card-radius-sm); background: var(--wash);
    max-width: 30rem; }
  .code { font-family: var(--font-mono); font-size: 2rem; font-weight: 600; letter-spacing: 0.08em; line-height: 1.2;
    margin: 0; color: var(--text); user-select: all; overflow-wrap: anywhere; }
  .code:focus { outline: none; }
  .valid { font-size: 0.875rem; font-weight: 500; margin: 0.25rem 0 0; }
  .code-box .actions { margin-top: 0.625rem; }
  .devices { list-style: none; margin: 0; padding: 0; }
  .device { padding: 0.5rem 0; border-top: 1px solid var(--border); min-width: 0; }
  .device:first-child { border-top: 0; }
  .device-head { display: flex; align-items: center; justify-content: space-between; gap: 0.25rem 0.75rem; flex-wrap: wrap; }
  .device-name { font-size: 0.875rem; font-weight: 500; overflow-wrap: anywhere; min-width: 0; }
  .device-line { font-size: 0.75rem; color: var(--muted); margin: 0.125rem 0 0; }
  .revoked { margin-top: 0.5rem; }
  .revoked .device-name { color: var(--muted); font-weight: 400; }
  .confirm { margin-top: 0.5rem; padding: 0.75rem 0.875rem; border-radius: var(--card-radius-sm); background: var(--wash); font-size: 0.875rem; }
  .confirm p { margin: 0; }
  .confirm p:focus { outline: 2px solid var(--focus-ring); outline-offset: 2px; border-radius: var(--radius-sm); }
  .confirm .actions { margin-top: 0.5rem; }
  @media (max-width: 768px) {
    .code { font-size: 1.75rem; }
  }
</style>
