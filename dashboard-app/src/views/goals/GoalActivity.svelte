<script lang="ts">
  import { untrack } from 'svelte';
  import { fmtUtc } from '../../lib/harness';
  import { routeHref } from '../../lib/router';
  import type { ApiAnswerError } from '../../lib/api';
  import {
    ACTIVITY_ENDED_TITLE, ACTIVITY_FAILED_TEXT, ACTIVITY_LOADING_TEXT, ACTIVITY_LOG_OFF_TEXT, ACTIVITY_STALE_TEXT, ACTIVITY_TITLE,
    WORK_LINKS_TEXT, activityOn, callsText, endedText, getGoalActivity, jobsText, lastCallText, linkSeparator, noCallYetText,
    outcomeWord, phaseText, quietText, startedText, timed, workKindWord, workLinks, type GoalActivity,
  } from '../../lib/goals';

  // What a goal's session is doing now (GET /goals/{ref}/activity): the current task or workflow, the last tool call,
  // the counts, the newest calls in words and links to the session on the Work screen. It reads when it is shown,
  // when the goal shown, its state, its open session or its last change differ, and, while a session is open, when
  // the page's own poll lands (`tick`, its last good read), and on that tick too while its last read failed, so a
  // failed read is tried again with no session open: no timer of its own, so a hidden tab or another page reads
  // nothing. One read at a time: a tick while a read is on its way is dropped; a change of the goal is read once that
  // read lands. A failed read keeps the last answer and says so. Every name is a text node.
  let {
    goal,
    now,
    tick = null,
  }: {
    goal: { ref: string; state: string; open_session: number | null; updated_at: string | null };
    now: number;
    tick?: number | null;
  } = $props();

  // Each value in its own derived: the page hands over a new goal object on every poll, and an unchanged value does
  // not run the read again.
  const ref = $derived(goal.ref);
  const goalState = $derived(goal.state);
  const openSession = $derived(goal.open_session);
  const updatedAt = $derived(goal.updated_at);

  let answer = $state<GoalActivity | null>(null);
  let failed = $state(false);
  const pollTick = $derived(openSession !== null || failed ? tick : null);
  /** The server says the panel or goals are off, or the goal is gone: nothing is shown. */
  let off = $state(false);

  /** The goal shown now ('' once the panel is gone); read after every await instead of the props. */
  let shown = '';
  let reading: AbortController | null = null;
  /** What the last read was started for (the goal's key and the page's tick then), and whether the goal changed since. */
  let readFor = '';
  let readTick: number | null = null;
  let changedSince = false;

  async function read(forRef: string, key: string) {
    const ac = new AbortController();
    reading = ac;
    readFor = key;
    readTick = untrack(() => tick);
    changedSince = false;
    try {
      const got = await timed((signal) => getGoalActivity(forRef, signal), ac.signal);
      if (ac.signal.aborted || forRef !== shown) return;
      off = !activityOn(got);
      if (activityOn(got)) answer = got;
      failed = false;
    } catch (err) {
      if (ac.signal.aborted || forRef !== shown) return;
      if ((err as ApiAnswerError | null)?.status === 404) off = true;
      else failed = true;
    } finally {
      if (reading === ac) {
        reading = null;
        if (changedSince && forRef === shown) void read(forRef, keyOf());
      }
    }
  }

  /** The goal's state, open session and last change, as one value. */
  const keyOf = () => `${goalState}|${openSession}|${updatedAt}`;

  $effect(() => {
    const r = ref;
    const key = keyOf();
    const t = pollTick;
    untrack(() => {
      if (r !== shown) {
        reading?.abort();
        reading = null;
        shown = r;
        answer = null;
        failed = false;
        off = false;
      } else if (reading) {
        if (key !== readFor) changedSince = true;
        return;
      } else if (key === readFor && (t === null || t === readTick)) {
        return; // nothing new since the last read (a read that just failed lets the tick through, not a read)
      }
      void read(r, key);
    });
  });
  $effect(() => () => {
    shown = '';
    reading?.abort();
    reading = null;
  });

  let session = $derived(answer?.session ?? null);
  let ended = $derived(session !== null && !session.open);
  let lastTool = $derived(answer?.recent[0]?.tool ?? null);
  /** After a long quiet, this says when the last call was, in place of the last-call line. */
  let quiet = $derived(answer?.last_call_at && !ended ? quietText(answer.last_call_at, lastTool, now) : '');
  let jobsLine = $derived(answer?.jobs && !ended ? jobsText(answer.jobs, now) : '');
  let links = $derived(workLinks(answer?.work_sessions ?? []));
</script>

{#if !off}
  <section class="card" aria-labelledby="goal-v.{ref}">
    <h3 id="goal-v.{ref}">{ended ? ACTIVITY_ENDED_TITLE : ACTIVITY_TITLE}</h3>
    {#if answer === null}
      <p class="line" role={failed ? 'alert' : 'status'}>{failed ? ACTIVITY_FAILED_TEXT : ACTIVITY_LOADING_TEXT}</p>
    {:else}
      {#if session !== null && ended}
        <p class="lead">{endedText(session, answer.calls, now)}</p>
      {:else}
        <p class="lead">{phaseText(answer)}</p>
        {#if answer.current?.status === 'running' && answer.current.started_at}
          <p class="line">{startedText(answer.current.kind, answer.current.started_at, now)}</p>
        {/if}
        {#if session !== null && answer.log_enabled}
          {#if answer.last_call_at}
            <p class="line">{quiet || lastCallText(answer.last_call_at, lastTool, now)}</p>
          {:else}
            <p class="line">{noCallYetText(session.started_at, now)}</p>
          {/if}
          {#if answer.calls && answer.calls.total > 0}<p class="line">{callsText(answer.calls)}</p>{/if}
          {#if jobsLine}<p class="line">{jobsLine}</p>{/if}
          {#if answer.recent.length}
            <ul class="calls" aria-label="Newest tool calls">
              {#each answer.recent as call, i (i)}
                <li class:failed={call.outcome === 'error'}>
                  <span class="when">{fmtUtc(call.at)}</span>
                  <span class="tool">{call.tool}</span>
                  <span class="outcome">{outcomeWord(call.outcome)}</span>
                  <span class="in">{workKindWord(call.in)}</span>
                </li>
              {/each}
            </ul>
          {/if}
        {:else if session !== null}
          <p class="hint">{ACTIVITY_LOG_OFF_TEXT}</p>
        {/if}
      {/if}
      {#if links.length}
        <p class="line links">{WORK_LINKS_TEXT}{' '}{#each links as link, i (link.id)}{linkSeparator(i, links.length)}<a
            href={routeHref('work', ['s', link.id])}>{link.label}</a>{/each}.</p>
      {/if}
      {#if failed}<p class="line warn" role="alert">{ACTIVITY_STALE_TEXT}</p>{/if}
    {/if}
  </section>
{/if}

<style>
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem; margin-bottom: 1.25rem; min-width: 0; }
  h3 { font-size: 0.9375rem; font-weight: 650; margin: 0 0 0.5rem; }
  .lead { font-size: 0.875rem; font-weight: 500; margin: 0; overflow-wrap: anywhere; }
  .line { font-size: 0.8125rem; margin: 0.25rem 0 0; overflow-wrap: anywhere; }
  .hint { font-size: 0.75rem; color: var(--muted); margin: 0.25rem 0 0; }
  .warn { color: var(--warn-text); }
  /* The rows share the list's columns (subgrid): the tool column is as wide as the widest name, up to 14rem, so the
     outcome and the kind sit just after it; a longer name wraps in its column. */
  .calls { list-style: none; margin: 0.5rem 0 0; padding: 0; display: grid; column-gap: 0.75rem;
    grid-template-columns: 8rem fit-content(14rem) max-content max-content minmax(0, 1fr); }
  .calls > li { display: grid; grid-column: 1 / -1; grid-template-columns: subgrid; align-items: baseline;
    padding: 0.25rem 0; border-top: 1px solid var(--border); font-size: 0.8125rem; min-width: 0; }
  .calls > li:first-child { border-top: 0; }
  .when, .in { font-size: 0.75rem; color: var(--muted); }
  .tool { font-family: var(--font-mono); font-size: 0.75rem; overflow-wrap: anywhere; min-width: 0; }
  .failed .outcome { color: var(--warn-text); font-weight: 600; }
  .links a { color: var(--accent-text); text-decoration: none; border-radius: var(--radius-sm); }
  .links a:hover { text-decoration: underline; }
  .links a:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  @media (max-width: 768px) {
    /* A row is the tool's name on a line of its own, then the time, the outcome and the kind, wrapping as they need. */
    .calls { display: block; }
    .calls > li { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.125rem 0.75rem; }
    .calls > li > .tool { order: -1; flex-basis: 100%; }
    /* The links stay words in their sentence and wrap like words; the padding widens the tap area without changing
       the line's height. */
    .links a { display: inline; padding-block: 0.75rem; }
  }
</style>
