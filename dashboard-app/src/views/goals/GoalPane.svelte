<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { Button } from '../../lib/ui/button';
  import { fmtUtc } from '../../lib/harness';
  import {
    ACTION_TEXT, DRIVER_OFF_TEXT, HELD_TEXT, NO_CHANNEL_TEXT, PENDING_TEXT, PILL, RESUME_BLOCKED_TEXT, budgetProblem,
    deadlineProblem, typedNumber,
    actorWord, asksFirst, confirmText, deadlineText, formatNumber, formatTokens, fromOwner, goalAction, intervalText,
    journalKindWord, nextSessionText, proposerWord, refusalText, resumeLabel, sessionOutcomeWord, spanText, spentPercent,
    stateWord, type Action, type GoalDetail, type GoalOptions, type PlanStep,
  } from '../../lib/goals';

  // One goal: its state and the owner's actions, then its charter, limits, spend, plan, sessions and journal, and the
  // note box. The buttons are exactly the server's `actions`; an action that spends tokens or ends something says so
  // in a sentence and waits for a Yes. Every text a session wrote is a text node. Ids of this goal's controls are
  // `goal-<letter>.<ref>[.<action>]`: a ref has no ".", so none can take another's.
  let {
    goal,
    options,
    halted,
    now,
    journalAll,
    onChanged,
    onMore,
  }: {
    goal: GoalDetail;
    options: GoalOptions;
    halted: boolean;
    now: number;
    journalAll: boolean;
    onChanged: () => Promise<void>;
    onMore: () => void;
  } = $props();

  type Note = { text: string; ok: boolean; expect?: string };
  let busy = $state<Action | null>(null);
  let result = $state<Note | null>(null);
  let confirming = $state<Action | null>(null);
  let total = $state('');
  let day = $state('');
  let days = $state('');
  let noteText = $state('');
  let noteResult = $state<Note | null>(null);

  let driverRunning = $derived(options.driver_running === true);
  let word = $derived(stateWord(goal, driverRunning));
  let buttons = $derived(goal.actions.filter((a) => a !== 'note'));
  let openSteps = $derived(goal.plan.filter((s) => !closed(s)));
  let closedSteps = $derived(goal.plan.filter(closed));

  // A result that speaks of a state is dropped once the goal shows another.
  $effect(() => {
    const state = goal.state;
    untrack(() => {
      if (result?.expect && result.expect !== state) result = null;
    });
  });

  /** A change whose answer was lost, and the goal as it was before it: once a later read shows the goal's state or
   * newest journal row changed, the change landed, and its usual result line replaces "No answer came back". */
  type Lost = { action: Action; state: string; top: number; text?: string };
  let lost = $state<Lost | null>(null);
  const newestRow = () => goal.journal[0]?.seq ?? 0;
  $effect(() => {
    const was = lost;
    const state = goal.state;
    const top = newestRow();
    if (!was || (state === was.state && top === was.top)) return;
    untrack(() => {
      lost = null;
      if (was.action === 'note') {
        noteResult = { text: ACTION_TEXT.note.done, ok: true };
        if (noteText === was.text) noteText = '';
      } else {
        result = { text: ACTION_TEXT[was.action].done, ok: true, expect: state };
      }
    });
  });
  const answerLost = (err: unknown) => (err as { status?: number } | null)?.status === undefined;

  function closed(step: PlanStep): boolean {
    return step.closed_session !== undefined || step.status === 'done' || step.status === 'closed';
  }

  const byId = (id: string) => document.getElementById(id);
  const buttonId = (action: Action) => `goal-a.${goal.ref}.${action}`;

  // The budget and deadline forms: the same ranges as the create form (the server's); Yes only for a valid change.
  let newTotal = $derived(typedNumber(total));
  let newDay = $derived(typedNumber(day));
  let newDays = $derived(typedNumber(days));
  let budgetWrong = $derived(budgetProblem(newTotal, newDay, goal, options));
  let deadlineWrong = $derived(deadlineProblem(newDays, options));
  let budgetReady = $derived(budgetWrong === null);
  let deadlineReady = $derived(deadlineWrong === null);
  const sentenceId = () => `goal-s.${goal.ref}`;

  async function press(action: Action) {
    if (busy) return;
    if (action === 'budget') {
      total = String(goal.total_budget);
      day = String(goal.day_budget);
    }
    if (action === 'deadline') days = '';
    if (!asksFirst(action)) return run(action);
    confirming = action;
    await tick();
    // The focus never lands on Yes: on the form's first field, else on the sentence, which is read first.
    (byId(`goal-c.${goal.ref}`)?.querySelector<HTMLElement>('input') ?? byId(sentenceId()))?.focus();
  }

  async function no() {
    const action = confirming;
    confirming = null;
    await tick();
    if (action) byId(buttonId(action))?.focus();
  }

  /** One change: every control of the goal is disabled until it and the reload after it have landed; its result is
   * shown then, beside controls that can be used again. */
  async function run(action: Action, body?: object) {
    if (busy) return;
    // The goal's ids are read now: after the awaits below, this pane may show another answer or none.
    const ref = goal.ref;
    const opener = `goal-a.${ref}.${action}`;
    const before = { state: goal.state, top: newestRow() };
    busy = action;
    confirming = null;
    result = null;
    lost = null;
    let outcome: Note;
    let unanswered = false;
    try {
      const changed = await goalAction(ref, action, body);
      outcome = { text: ACTION_TEXT[action].done, ok: true, expect: changed.state };
    } catch (err) {
      outcome = { text: refusalText(err), ok: false };
      unanswered = answerLost(err);
    }
    await onChanged();
    busy = null;
    result = outcome;
    if (unanswered) lost = { action, ...before };
    await tick();
    (byId(opener) ?? byId(`goal-h.${ref}`))?.focus();
  }

  function yes(action: Action) {
    if (action === 'budget') {
      const body: { total?: number; day?: number } = {};
      if (newTotal !== goal.total_budget) body.total = newTotal;
      if (newDay !== goal.day_budget) body.day = newDay;
      return run('budget', body);
    }
    if (action === 'deadline') return run('deadline', { days: newDays });
    return run(action);
  }

  async function addNote(e: SubmitEvent) {
    e.preventDefault();
    if (busy || !noteText.trim()) return;
    busy = 'note';
    noteResult = null;
    lost = null;
    const ref = goal.ref;
    const sent = noteText;
    const before = { state: goal.state, top: newestRow() };
    let outcome: Note;
    let unanswered = false;
    try {
      await goalAction(ref, 'note', { text: sent });
      noteText = '';
      outcome = { text: ACTION_TEXT.note.done, ok: true };
    } catch (err) {
      outcome = { text: refusalText(err), ok: false };
      unanswered = answerLost(err);
    }
    await onChanged();
    busy = null;
    noteResult = outcome;
    if (unanswered) lost = { action: 'note', text: sent, ...before };
  }

  const YES: Record<Action, string> = {
    approve: 'Yes, approve',
    'run-now': 'Yes, run a session now',
    resume: 'Yes, resume',
    'confirm-done': 'Yes, it is done',
    pause: 'Yes, pause',
    stop: 'Yes, stop now',
    budget: 'Yes, change the budget',
    deadline: 'Yes, extend the deadline',
    note: 'Yes, add the note',
    cancel: 'Yes, cancel the goal',
  };

  /** The action that moves a goal forward is drawn filled, the rest outlined. Waiting for the owner, that is Confirm
   * done, not "Not done yet". */
  const primary = (action: Action) =>
    action === 'approve' || action === 'confirm-done' || (action === 'resume' && goal.pause_reason !== 'waiting_for_owner');
  const label = (action: Action) => (action === 'resume' ? resumeLabel(goal) : ACTION_TEXT[action].label);
  const sessionLength = (start: string | null, end: string | null) =>
    start && end ? spanText(new Date(end).getTime() - new Date(start).getTime()) : start ? 'running' : '';
</script>

<!-- Escape closes an open confirmation, as No does. -->
<svelte:window onkeydown={(e) => { if (e.key === 'Escape' && confirming && busy === null) void no(); }} />

{#snippet line(n: Note | null)}
  {#if n}<p class="result" class:result-err={!n.ok} role={n.ok ? 'status' : 'alert'}>{n.text}</p>{/if}
{/snippet}

{#snippet refs(evidence: string[] | undefined)}
  {#if evidence?.length}
    <ul class="refs" aria-label="Evidence">{#each evidence as ref, i (i)}<li><code>{ref}</code></li>{/each}</ul>
  {/if}
{/snippet}

<section class="goal" aria-labelledby="goal-h.{goal.ref}">
  <div class="card">
    <div class="goal-head">
      <h2 id="goal-h.{goal.ref}" tabindex="-1">{goal.title}</h2>
      <span class="ref">{goal.ref}</span>
    </div>
    <div class="state">
      <span class="status-pill {PILL[word.tone]}">{word.text}</span>
      {#if halted && goal.state === 'active'}<span class="status-pill status-pill--warn">{HELD_TEXT}</span>{/if}
      {#if nextSessionText(goal, now, driverRunning)}<span class="when">{nextSessionText(goal, now, driverRunning)}</span>{/if}
    </div>
    {#if goal.pause_sentence}<p class="reason">{goal.pause_sentence}</p>{/if}
    {#if goal.pending_request}<p class="reason">{PENDING_TEXT[goal.pending_request]}</p>{/if}
    {#if !driverRunning && (goal.state === 'draft' || goal.state === 'active')}<p class="reason">{DRIVER_OFF_TEXT}</p>{/if}
    {#if goal.state === 'draft' && !goal.actions.includes('approve')}<p class="reason">{NO_CHANNEL_TEXT}</p>{/if}

    {#if buttons.length || goal.resume_blocked}
      <div class="actions">
        {#each buttons as action (action)}
          <Button id={buttonId(action)} data-action={action} size="sm"
            variant={primary(action) ? 'default' : 'outline'}
            class="max-md:min-h-11 max-md:px-4 {action === 'cancel' ? 'hover:border-[var(--err-text)] hover:text-[var(--err-text)]' : ''}"
            disabled={busy !== null} aria-expanded={asksFirst(action) ? confirming === action : undefined}
            onclick={() => void press(action)}>
            {busy === action ? 'Working…' : label(action)}
          </Button>
        {/each}
        {#if goal.state === 'paused' && goal.resume_blocked}
          <Button id={buttonId('resume')} data-action="resume-blocked" size="sm" variant="outline" class="max-md:min-h-11 max-md:px-4"
            disabled aria-describedby="goal-r.{goal.ref}">{label('resume')}</Button>
          <span class="hint" id="goal-r.{goal.ref}">{RESUME_BLOCKED_TEXT[goal.resume_blocked]}</span>
        {/if}
      </div>
    {/if}

    {#if confirming}
      {@const action = confirming}
      <div class="confirm" id="goal-c.{goal.ref}" role="group" aria-label="Confirm: {label(action)}" aria-describedby={sentenceId()}>
        {#if action === 'budget'}
          <div class="fields">
            <div class="field">
              <label for="goal-t.{goal.ref}">Total budget (tokens)</label>
              <input id="goal-t.{goal.ref}" inputmode="numeric" autocomplete="off" bind:value={total} disabled={busy !== null} />
            </div>
            <div class="field">
              <label for="goal-d.{goal.ref}">Day budget (tokens)</label>
              <input id="goal-d.{goal.ref}" inputmode="numeric" autocomplete="off" bind:value={day} disabled={busy !== null} />
            </div>
          </div>
          <p id={sentenceId()} tabindex="-1" class:wrong={!budgetReady}>{budgetReady ? confirmText('budget', goal, { driverRunning, total: newTotal, day: newDay }) : budgetWrong}</p>
        {:else if action === 'deadline'}
          <div class="field">
            <label for="goal-e.{goal.ref}">More days</label>
            <input id="goal-e.{goal.ref}" inputmode="numeric" autocomplete="off" bind:value={days} disabled={busy !== null} />
          </div>
          <p id={sentenceId()} tabindex="-1" class:wrong={!deadlineReady}>{deadlineReady ? confirmText('deadline', goal, { driverRunning, days: newDays, now }) : deadlineWrong}</p>
        {:else}
          <p id={sentenceId()} tabindex="-1">{confirmText(action, goal, { driverRunning })}</p>
        {/if}
        <div class="actions">
          <Button size="sm" class="max-md:min-h-11 max-md:px-4" variant={ACTION_TEXT[action].ends ? 'destructive' : 'default'}
            disabled={busy !== null || (action === 'budget' && !budgetReady) || (action === 'deadline' && !deadlineReady)}
            onclick={() => void yes(action)}>{action === 'resume' && goal.pause_reason === 'waiting_for_owner' ? 'Yes, keep working' : YES[action]}</Button>
          <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" onclick={() => void no()}>No</Button>
        </div>
      </div>
    {/if}
    {@render line(result)}
  </div>

  <div class="card">
    <h3>Charter</h3>
    <dl class="facts">
      <dt>Objective</dt><dd class="text">{goal.objective}</dd>
      <dt>What done looks like</dt><dd class="text">{goal.done_when}</dd>
      <dt>Questions and reports go to</dt>
      <dd>{goal.origin_channel ?? (options.has_default_channel ? 'The default chat' : 'Nowhere: no channel is set')}</dd>
      <dt>Proposed by</dt><dd>{proposerWord(goal.proposed_by)}</dd>
    </dl>
  </div>

  <div class="card">
    <h3>Limits</h3>
    <dl class="facts">
      <dt>Total budget</dt><dd>{formatTokens(goal.total_budget)}</dd>
      <dt>Day budget</dt><dd>{formatTokens(goal.day_budget)}</dd>
      <dt>Deadline</dt><dd>{goal.deadline_at ? `${fmtUtc(goal.deadline_at)}. ${deadlineText(goal, now)}` : `${goal.deadline_days} days from approval`}</dd>
      <dt>Sessions per day</dt><dd>{goal.sessions_per_day}</dd>
      <dt>Shortest gap between sessions</dt><dd>{intervalText(goal.min_interval_seconds)}</dd>
      <dt>Job launches per day</dt><dd>{goal.jobs_per_day}</dd>
      <dt>Quiet hours</dt><dd>{goal.quiet_hours_ok ? 'Sessions may start in quiet hours' : 'No session starts in quiet hours'}</dd>
      <dt>Stall limit</dt><dd>{goal.stall_limit} sessions</dd>
      <dt>Mode</dt><dd>{goal.mode === 'contained' ? 'Contained' : goal.mode}</dd>
    </dl>
  </div>

  <div class="card">
    <h3>Spend</h3>
    <div class="spend">
      <span>Today</span>
      <span class="meter" aria-hidden="true"><span class="fill" class:full={goal.spent_today >= goal.day_budget} style:width="{spentPercent(goal.spent_today, goal.day_budget)}%"></span></span>
      <span>{formatNumber(goal.spent_today)} of {formatTokens(goal.day_budget)}</span>
      <span>In all</span>
      <span class="meter" aria-hidden="true"><span class="fill" class:full={goal.spent_total >= goal.total_budget} style:width="{spentPercent(goal.spent_total, goal.total_budget)}%"></span></span>
      <span>{formatNumber(goal.spent_total)} of {formatTokens(goal.total_budget)}</span>
    </div>
    <p class="hint">{goal.spend_note}</p>
  </div>

  <div class="card">
    <h3>Plan</h3>
    {#if goal.plan.length === 0}
      <p class="hint">No plan yet. A session writes one.</p>
    {:else}
      <ul class="steps">
        {#each openSteps as step, i (i)}
          <li><span class="step-state">{step.status ?? 'open'}</span> <span class="text">{step.text ?? ''}</span></li>
        {/each}
        {#each closedSteps as step, i (i)}
          <li class="step-closed">
            <span class="step-state">{step.status ?? 'closed'}</span> <span class="text">{step.text ?? ''}</span>
            {#if step.closed_session !== undefined}<span class="hint">Closed in session {step.closed_session}.</span>{/if}
            {@render refs(step.evidence)}
          </li>
        {/each}
      </ul>
    {/if}
  </div>

  <div class="card">
    <h3>Sessions</h3>
    {#if goal.sessions_total > goal.sessions.length}<p class="hint">Showing the newest {goal.sessions.length} of {goal.sessions_total}.</p>{/if}
    {#if goal.sessions.length === 0}
      <p class="hint">No session has run yet.</p>
    {:else}
      <ul class="sessions">
        {#each goal.sessions as s (s.n)}
          <li>
            <span class="session-n">Session {s.n}</span>
            <span>{fmtUtc(s.started_at)}{sessionLength(s.started_at, s.ended_at) ? `, ${sessionLength(s.started_at, s.ended_at)}` : ''}</span>
            <span>{sessionOutcomeWord(s.outcome)}</span>
            <span>{formatTokens(s.spent)}, {s.steps_closed} step{s.steps_closed === 1 ? '' : 's'} closed</span>
            {#if s.summary}
              <details class="summary"><summary>Summary</summary><p class="text">{s.summary}</p></details>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  </div>

  <div class="card">
    <h3>Journal</h3>
    {#if goal.journal.length === 0}
      <p class="hint">Nothing written yet.</p>
    {:else}
      <ul class="journal">
        {#each goal.journal as row (row.seq)}
          <li class:own={fromOwner(row)}>
            <span class="journal-head">
              <span class="kind">{journalKindWord(row.kind)}</span>
              <span>{actorWord(row.actor)}{row.session_n !== null ? `, session ${row.session_n}` : ''}</span>
              <span class="when">{fmtUtc(row.created_at)}</span>
            </span>
            <p class="text">{row.text}</p>
            {@render refs(row.evidence)}
          </li>
        {/each}
      </ul>
      {#if goal.journal.length >= (journalAll ? 200 : 50)}<p class="hint">Showing the newest {goal.journal.length} entries.</p>{/if}
      {#if !journalAll && goal.journal.length >= 50}
        <div class="actions">
          <Button variant="outline" size="sm" class="max-md:min-h-11 max-md:px-4" onclick={onMore}>Show more</Button>
        </div>
      {/if}
    {/if}
  </div>

  {#if goal.actions.includes('note')}
    <div class="card">
      <form onsubmit={addNote}>
        <div class="field">
          <label for="goal-n.{goal.ref}">A note for the next session</label>
          <textarea id="goal-n.{goal.ref}" rows="3" maxlength={options.bounds?.note ?? 4000} bind:value={noteText} disabled={busy !== null}></textarea>
        </div>
        <div class="actions">
          <Button type="submit" size="sm" variant="outline" class="max-md:min-h-11 max-md:px-4" disabled={busy !== null || !noteText.trim()}>
            {busy === 'note' ? 'Adding…' : 'Add note'}
          </Button>
        </div>
        {@render line(noteResult)}
      </form>
    </div>
  {/if}
</section>

<style>
  .goal { min-width: 0; }
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem; margin-bottom: 1.25rem; min-width: 0; }
  .goal-head { display: flex; align-items: baseline; gap: 0.5rem 0.75rem; flex-wrap: wrap; }
  h2 { font-size: 1rem; font-weight: 650; margin: 0; overflow-wrap: anywhere; }
  h2:focus { outline: none; }
  h3 { font-size: 0.9375rem; font-weight: 650; margin: 0 0 0.5rem; }
  .ref { font-family: var(--font-mono); font-size: 0.8125rem; color: var(--muted); }
  .state { display: flex; align-items: center; gap: 0.375rem 0.5rem; flex-wrap: wrap; margin-top: 0.5rem; }
  .when { font-size: 0.75rem; color: var(--muted); }
  .reason { font-size: 0.8125rem; color: var(--muted); margin: 0.5rem 0 0; overflow-wrap: anywhere; }
  .hint { font-size: 0.75rem; color: var(--muted); margin: 0.25rem 0 0; }
  .actions { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.75rem; }
  .confirm { margin-top: 0.75rem; padding: 0.75rem 0.875rem; border-radius: var(--card-radius-sm); background: var(--wash); font-size: 0.875rem; }
  .confirm p { margin: 0.5rem 0 0; }
  .confirm p:focus { outline: 2px solid var(--focus-ring); outline-offset: 2px; border-radius: var(--radius-sm); }
  .confirm p.wrong { color: var(--warn-text); }
  .confirm .actions { margin-top: 0.5rem; }
  .fields { display: flex; gap: 0.5rem 0.75rem; flex-wrap: wrap; }
  .field { display: flex; flex-direction: column; gap: 0.25rem; max-width: 36rem; flex: 1 1 12rem; }
  .field label { font-size: 0.75rem; font-weight: 600; color: var(--muted); }
  .field input, .field textarea { padding: 0.5rem 0.625rem; background: var(--surface); border: 1px solid var(--field-border);
    border-radius: var(--radius-sm); color: var(--text); font: inherit; font-size: 0.875rem; min-width: 0; }
  .field input { height: 2.75rem; }
  .field textarea { resize: vertical; }
  .field input:focus-visible, .field textarea:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .result { font-size: 0.8125rem; color: var(--save-ok); margin: 0.5rem 0 0; overflow-wrap: anywhere; }
  .result-err { color: var(--warn-text); }
  .facts { display: grid; grid-template-columns: minmax(8rem, 14rem) minmax(0, 1fr); gap: 0.375rem 1rem; margin: 0; font-size: 0.8125rem; }
  .facts dt { color: var(--muted); }
  .facts dd { margin: 0; min-width: 0; }
  .text { white-space: pre-line; overflow-wrap: anywhere; }
  .spend { display: grid; grid-template-columns: 3.5rem minmax(6rem, 1fr) auto; align-items: center; gap: 0.375rem 0.75rem; font-size: 0.8125rem; }
  .meter { display: block; height: 0.5rem; border-radius: 999px; background: var(--track); overflow: hidden; }
  .fill { display: block; height: 100%; border-radius: 999px; background: var(--action); }
  .fill.full { background: var(--warn-text); }
  .steps, .sessions, .journal, .refs { list-style: none; margin: 0; padding: 0; }
  .steps > li, .sessions > li, .journal > li { padding: 0.5rem 0; border-top: 1px solid var(--border); font-size: 0.8125rem; min-width: 0; }
  .steps > li:first-child, .sessions > li:first-child, .journal > li:first-child { border-top: 0; }
  .step-state { font-size: 0.75rem; color: var(--muted); }
  .step-closed .text { color: var(--muted); }
  .refs { display: flex; flex-wrap: wrap; gap: 0.25rem 0.5rem; margin-top: 0.25rem; }
  .refs code { font-family: var(--font-mono); font-size: 0.75rem; overflow-wrap: anywhere; }
  .sessions > li { display: grid; grid-template-columns: 5.5rem repeat(3, minmax(0, 1fr)); gap: 0.25rem 0.75rem; align-items: baseline; }
  .session-n { font-weight: 600; }
  .summary { grid-column: 1 / -1; }
  .summary summary { cursor: pointer; color: var(--muted); font-size: 0.75rem; border-radius: var(--radius-sm); }
  .summary p { margin: 0.25rem 0 0; }
  .journal > li.own { background: var(--info-bg); border-radius: var(--radius-sm); padding: 0.5rem 0.625rem; border-top-color: transparent; }
  .journal-head { display: flex; flex-wrap: wrap; gap: 0.25rem 0.75rem; font-size: 0.75rem; color: var(--muted); }
  .kind { font-weight: 600; color: var(--text); }
  .journal p { margin: 0.25rem 0 0; }
  form { display: flex; flex-direction: column; gap: 0.5rem; }
  form .actions { margin-top: 0; }
  @media (max-width: 768px) {
    .facts { grid-template-columns: minmax(0, 1fr); gap: 0.125rem; }
    .facts dd { margin-bottom: 0.375rem; }
    .sessions > li { grid-template-columns: minmax(0, 1fr); }
    .summary summary { line-height: 2.75rem; }
  }
</style>
