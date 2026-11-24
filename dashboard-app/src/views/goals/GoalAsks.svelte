<script lang="ts">
  import { untrack } from 'svelte';
  import { Button } from '../../lib/ui/button';
  import {
    ANSWER_EMPTY_TEXT, ANSWER_LIMIT_TEXT, ANSWER_MAX, ANSWER_RECORDED_TEXT, ASKS_GONE_TEXT, ASKS_LOST_GONE_TEXT,
    ASKS_READ_FAILED_TEXT, ASKS_TITLE, answerQuestion, askRefusal, decideProposal, decisionText, proposalExpiryText,
    proposalLead, questionExpiryText, waitingSentence, waitingWords,
    type AskOutcome, type Awaiting, type AwaitingProposal, type AwaitingQuestion,
  } from '../../lib/goals';

  // What a goal's sessions wait on the owner for (the goal's `awaiting`): each approval request with Approve and
  // Reject, each question with a field and Send, through the routes the Telegram bot calls. It is drawn from the
  // items as last seen, not from the newest read alone: an item that leaves the list while it is being answered, whose
  // answer was lost, or that has text typed for it stays where it was, with a sentence that says why it went. A read
  // that failed (`awaiting` null) changes nothing shown. Whether an item expired is the server's to say (a refusal,
  // or the item leaving the list): this browser's clock never disables a button. A line that stays in view while the
  // pane scrolls says what still waits once the card is out of view, and an always-present live region says it to a
  // screen reader. Every text an item carries is a text node. Ids are `goal-b.<ref>[.<short id>.<part>]`.
  let { goal, onChanged }: { goal: { ref: string; awaiting: Awaiting | null }; onChanged: () => Promise<void> } = $props();

  type Item = { kind: 'proposal'; item: AwaitingProposal } | { kind: 'question'; item: AwaitingQuestion };
  type Press = 'approve' | 'reject' | 'send';

  /** Every item seen since the pane opened, in the order first seen, each as last read. */
  let seen = $state<Item[]>([]);
  /** The ids of the last read that succeeded. */
  let listed = $state<Set<string>>(new Set());
  let busy = $state<Record<string, Press>>({});
  let outcomes = $state<Record<string, AskOutcome>>({});
  let drafts = $state<Record<string, string>>({});
  /** A question whose field was typed in (the empty-answer hint waits for that). */
  let typed = $state<Record<string, boolean>>({});
  /** A question with a Send that got no answer: its text may have been recorded, so it is never called "not sent". */
  let sendLost = $state<Record<string, boolean>>({});

  $effect.pre(() => {
    const read = goal.awaiting;
    if (read === null) return;
    untrack(() => {
      const now: Item[] = [
        ...read.proposals.map((item): Item => ({ kind: 'proposal', item })),
        ...read.questions.map((item): Item => ({ kind: 'question', item })),
      ];
      const at = new Map(seen.map((s, i) => [s.item.id, i]));
      const next = [...seen];
      for (const s of now) {
        const i = at.get(s.item.id);
        if (i === undefined) next.push(s);
        else next[i] = s;
      }
      seen = next;
      listed = new Set(now.map((s) => s.item.id));
    });
  });

  type Phase = 'open' | 'busy' | 'done' | 'lost' | 'gone';
  function phaseOf(id: string): Phase {
    if (busy[id] !== undefined) return 'busy';
    if (outcomes[id]?.final) return 'done';
    if (listed.has(id)) return 'open';
    return outcomes[id]?.lost ? 'lost' : 'gone';
  }
  const answerable = (id: string) => {
    const phase = phaseOf(id);
    return phase === 'open' || phase === 'busy';
  };

  let waiting = $derived(seen.filter((s) => answerable(s.item.id)));
  let proposalsWaiting = $derived(waiting.filter((s) => s.kind === 'proposal').length);
  let questionsWaiting = $derived(waiting.filter((s) => s.kind === 'question').length);
  let counted = $derived(waitingWords(proposalsWaiting, questionsWaiting));
  let sentence = $derived(waitingSentence(proposalsWaiting, questionsWaiting));
  let readFailed = $derived(goal.awaiting === null);

  const partId = (s: Item, part: string) => `goal-b.${goal.ref}.${s.item.short_id}.${part}`;

  async function decide(p: AwaitingProposal, approve: boolean) {
    if (busy[p.id] !== undefined) return;
    busy[p.id] = approve ? 'approve' : 'reject';
    delete outcomes[p.id];
    let outcome: AskOutcome;
    try {
      const answer = await decideProposal(p.id, approve);
      outcome = { text: decisionText(answer?.state, p.short_id), ok: true, final: true, lost: false };
    } catch (err) {
      outcome = askRefusal('proposal', err);
    }
    delete busy[p.id];
    outcomes[p.id] = outcome;
    if (!outcome.lost) void onChanged();
  }

  async function send(q: AwaitingQuestion) {
    const text = drafts[q.id] ?? '';
    if (busy[q.id] !== undefined || !text.trim()) return;
    busy[q.id] = 'send';
    delete outcomes[q.id];
    let outcome: AskOutcome;
    try {
      await answerQuestion(q.id, text);
      outcome = { text: ANSWER_RECORDED_TEXT, ok: true, final: true, lost: false };
      delete drafts[q.id];
    } catch (err) {
      outcome = askRefusal('question', err);
      if (outcome.lost) sendLost[q.id] = true;
    }
    delete busy[q.id];
    outcomes[q.id] = outcome;
    if (!outcome.lost) void onChanged();
  }

  /** What an item says under it once pressed, or once it left the list. */
  function lineOf(id: string): { text: string; ok: boolean } | null {
    const phase = phaseOf(id);
    if (phase === 'gone') return { text: ASKS_GONE_TEXT, ok: false };
    if (phase === 'lost') return { text: ASKS_LOST_GONE_TEXT, ok: false };
    return outcomes[id] && phase !== 'busy' ? outcomes[id] : null;
  }

  function answerHint(id: string): string {
    const text = drafts[id] ?? '';
    if (text.length >= ANSWER_MAX) return ANSWER_LIMIT_TEXT;
    return typed[id] && !text.trim() ? ANSWER_EMPTY_TEXT : '';
  }

  // The line in view: shown while something waits and the card is scrolled out of view.
  let card = $state<HTMLElement | null>(null);
  let cardInView = $state(true);
  $effect(() => {
    if (!card || typeof IntersectionObserver !== 'function') return;
    const watch = new IntersectionObserver((entries) => {
      for (const entry of entries) cardInView = entry.isIntersecting;
    });
    watch.observe(card);
    return () => watch.disconnect();
  });
  function showCard() {
    document.getElementById(`goal-b.${goal.ref}`)?.focus();
  }
</script>

<p class="sr-only" role="status" aria-live="polite">{sentence}</p>

{#if sentence && !cardInView}
  <div class="pin">
    <div class="pin-line">
      <span>{sentence}</span>
      <Button size="sm" variant="outline" class="max-md:min-h-11 max-md:px-4" onclick={showCard}>Show</Button>
    </div>
  </div>
{/if}

{#if seen.length || readFailed}
  <section class="card asks" aria-labelledby="goal-b.{goal.ref}" bind:this={card}>
    <div class="head">
      <h3 id="goal-b.{goal.ref}" tabindex="-1">{ASKS_TITLE}</h3>
      {#if counted}<span class="status-pill status-pill--warn">{counted}</span>{/if}
    </div>
    {#if readFailed}<p class="line warn" role="alert">{ASKS_READ_FAILED_TEXT}</p>{/if}
    {#if seen.length}
      <ul class="items">
        {#each seen as s (s.item.id)}
          {@const phase = phaseOf(s.item.id)}
          {@const pressed = busy[s.item.id]}
          {@const line = lineOf(s.item.id)}
          <li class="item" class:settled={phase === 'done' || phase === 'gone' || phase === 'lost'}>
            {#if s.kind === 'proposal'}
              {@const p = s.item}
              <p class="lead">{proposalLead(p.tool)}</p>
              <p class="label">Why</p>
              <p class="text">{p.why}</p>
              <p class="label">Call, exactly as it will run</p>
              <pre class="call">{p.arguments}</pre>
              {#if p.expires_at}<p class="hint">{proposalExpiryText(p.expires_at)}</p>{/if}
              {#if phase === 'open' || phase === 'busy'}
                <div class="actions">
                  <Button id={partId(s, 'approve')} size="sm" class="max-md:min-h-11 max-md:px-4" disabled={pressed !== undefined}
                    onclick={() => void decide(p, true)}>{pressed === 'approve' ? 'Approving…' : 'Approve'}</Button>
                  <Button id={partId(s, 'reject')} size="sm" variant="outline" class="max-md:min-h-11 max-md:px-4"
                    disabled={pressed !== undefined} onclick={() => void decide(p, false)}>{pressed === 'reject' ? 'Rejecting…' : 'Reject'}</Button>
                </div>
              {/if}
            {:else}
              {@const q = s.item}
              <p class="lead">Nous asks</p>
              <p class="text">{q.text}</p>
              <p class="hint">{questionExpiryText(q.expires_at)}</p>
              {#if phase === 'open' || phase === 'busy'}
                <div class="field">
                  <label for={partId(s, 'answer')}>Your answer</label>
                  <textarea id={partId(s, 'answer')} rows="3" maxlength={ANSWER_MAX} disabled={pressed !== undefined}
                    value={drafts[q.id] ?? ''}
                    oninput={(e) => { drafts[q.id] = e.currentTarget.value; typed[q.id] = true; }}></textarea>
                </div>
                {#if answerHint(q.id)}<p class="hint">{answerHint(q.id)}</p>{/if}
                <div class="actions">
                  <Button id={partId(s, 'send')} size="sm" class="max-md:min-h-11 max-md:px-4"
                    disabled={pressed !== undefined || !(drafts[q.id] ?? '').trim()} onclick={() => void send(q)}>
                    {pressed === 'send' ? 'Sending…' : 'Send'}
                  </Button>
                </div>
              {:else if (drafts[q.id] ?? '').trim()}
                <div class="field">
                  <label for={partId(s, 'answer')}>{sendLost[q.id] ? 'What you wrote' : 'What you wrote (not sent)'}</label>
                  <textarea id={partId(s, 'answer')} rows="3" readonly value={drafts[q.id]}></textarea>
                </div>
              {/if}
            {/if}
            {#if line}<p class="result" class:result-err={!line.ok} role={line.ok ? 'status' : 'alert'}>{line.text}</p>{/if}
          </li>
        {/each}
      </ul>
    {/if}
  </section>
{/if}

<style>
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem; margin-bottom: 1.25rem; min-width: 0; }
  .head { display: flex; align-items: center; gap: 0.375rem 0.625rem; flex-wrap: wrap; margin: 0 0 0.5rem; }
  h3 { font-size: 0.9375rem; font-weight: 650; margin: 0; scroll-margin-top: 4rem; }
  h3:focus { outline: none; }
  /* The line in view: no height of its own, so showing it moves nothing; it sticks to the top while the pane scrolls. */
  .pin { position: sticky; top: 0.75rem; z-index: 2; height: 0; }
  /* Below the phone's fixed top bar (App.svelte: 3.25rem tall). */
  @media (max-width: 768px) { .pin { top: 3.75rem; } }
  .pin-line { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem 0.75rem; flex-wrap: wrap;
    padding: 0.5rem 0.625rem 0.5rem 0.875rem; border-radius: var(--card-radius-sm); background: var(--warn-bg); color: var(--warn-text);
    box-shadow: var(--card-shadow); font-size: 0.8125rem; font-weight: 600; }
  .items { list-style: none; margin: 0; padding: 0; }
  .item { padding: 0.75rem 0; border-top: 1px solid var(--border); min-width: 0; }
  .item:first-child { border-top: 0; padding-top: 0.25rem; }
  .item:last-child { padding-bottom: 0; }
  .settled .lead, .settled .text, .settled .call { color: var(--muted); }
  .lead { font-size: 0.875rem; font-weight: 600; margin: 0; overflow-wrap: anywhere; }
  .label { font-size: 0.75rem; font-weight: 600; color: var(--muted); margin: 0.5rem 0 0; }
  .text { font-size: 0.8125rem; white-space: pre-wrap; overflow-wrap: anywhere; margin: 0.25rem 0 0; }
  .call { font-family: var(--font-mono); font-size: 0.75rem; white-space: pre-wrap; overflow-wrap: anywhere; margin: 0.25rem 0 0;
    padding: 0.5rem 0.625rem; border-radius: var(--radius-sm); background: var(--wash); }
  .hint { font-size: 0.75rem; color: var(--muted); margin: 0.375rem 0 0; }
  .line { font-size: 0.8125rem; margin: 0 0 0.5rem; }
  .warn { color: var(--warn-text); }
  .actions { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.625rem; }
  .field { display: flex; flex-direction: column; gap: 0.25rem; max-width: 36rem; margin-top: 0.625rem; }
  .field label { font-size: 0.75rem; font-weight: 600; color: var(--muted); }
  .field textarea { padding: 0.5rem 0.625rem; background: var(--surface); border: 1px solid var(--field-border);
    border-radius: var(--radius-sm); color: var(--text); font: inherit; font-size: 0.875rem; min-width: 0; resize: vertical; }
  .field textarea:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .field textarea[readonly] { background: var(--wash); }
  .result { font-size: 0.8125rem; color: var(--save-ok); margin: 0.5rem 0 0; overflow-wrap: anywhere; }
  .result-err { color: var(--warn-text); }
</style>
