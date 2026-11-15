<script lang="ts">
  import { Button } from '../../lib/ui/button';
  import {
    CHANNEL_HINT, DRAFT_NOTE, LIMIT_LABEL, PRESET_LABEL, PRESET_NAMES, createBody, createGoal, createProblems, formatNumber, limitFields,
    presetLine, refusalText, type CreateForm, type GoalOptions, type NumberLimit,
  } from '../../lib/goals';

  // The form that makes a draft (POST /goals). A preset's numbers come from GET /goals/options, never from here;
  // "Customise" starts from the chosen preset. The rules of a custom limit are checked before anything is sent, and
  // the server's own sentence is shown when it refuses. What was typed stays until the draft is made.
  let { options, onCreated }: { options: GoalOptions; onCreated: (ref: string) => void } = $props();

  const presets = $derived(PRESET_NAMES.filter((name) => options.presets?.[name]));
  const startLimits = () => options.presets?.small;
  let form = $state<CreateForm>({
    title: '',
    objective: '',
    done_when: '',
    preset: 'small',
    custom: false,
    limits: limitFields(startLimits() ?? { total_budget: 0, day_budget: 0, deadline_days: 0, sessions_per_day: 0, min_interval_seconds: 0, jobs_per_day: 0, quiet_hours_ok: false }),
    quiet_hours_ok: startLimits()?.quiet_hours_ok ?? false,
    origin_channel: '',
  });
  let tried = $state(false);
  let sending = $state(false);
  let refused = $state<string | null>(null);
  let problems = $derived(tried ? createProblems(form, options) : []);
  const FIELDS: NumberLimit[] = ['total_budget', 'day_budget', 'deadline_days', 'sessions_per_day', 'min_interval_seconds', 'jobs_per_day'];
  const rangeOf = (field: NumberLimit) => options.ranges?.find((r) => r.field === field);

  /** Choosing a size, or opening "Customise", starts the limits from that preset. */
  function usePreset(name: string) {
    form.preset = name;
    const limits = options.presets?.[name];
    if (!limits) return;
    form.limits = limitFields(limits);
    form.quiet_hours_ok = limits.quiet_hours_ok;
  }

  /** Open the custom limits, starting from the chosen size's numbers, or go back to the size. */
  function toggleCustom() {
    if (!form.custom) usePreset(form.preset);
    form.custom = !form.custom;
  }

  async function submit(e: SubmitEvent) {
    e.preventDefault();
    tried = true;
    refused = null;
    if (sending || createProblems(form, options).length) return;
    sending = true;
    try {
      const made = await createGoal(createBody(form));
      onCreated(made.ref);
    } catch (err) {
      refused = refusalText(err);
    } finally {
      sending = false;
    }
  }
</script>

<section class="card" aria-labelledby="goal-new-h">
  <h2 id="goal-new-h">New goal</h2>
  <p class="lead">{DRAFT_NOTE}</p>
  <form onsubmit={submit} novalidate>
    <div class="field">
      <label for="goal-new-title">Title</label>
      <input id="goal-new-title" bind:value={form.title} autocomplete="off" disabled={sending} />
    </div>
    <div class="field">
      <label for="goal-new-objective">Objective</label>
      <textarea id="goal-new-objective" rows="3" bind:value={form.objective} disabled={sending}></textarea>
    </div>
    <div class="field">
      <label for="goal-new-done">What done looks like</label>
      <textarea id="goal-new-done" rows="2" bind:value={form.done_when} disabled={sending}></textarea>
    </div>

    <fieldset class="sizes" disabled={sending || form.custom}>
      <legend>Size</legend>
      <div class="cards">
        {#each presets as name (name)}
          <label class="size" class:chosen={form.preset === name && !form.custom}>
            <input type="radio" name="goal-new-preset" id="goal-new-preset-{name}" value={name} checked={form.preset === name}
              onchange={() => usePreset(name)} />
            <span class="size-name">{PRESET_LABEL[name]}</span>
            <span class="hint">{presetLine(options.presets![name])}</span>
          </label>
        {/each}
      </div>
    </fieldset>

    <div class="custom">
      <button type="button" class="disclose" aria-expanded={form.custom} aria-controls="goal-new-limits" disabled={sending}
        onclick={toggleCustom}>{form.custom ? 'Use a size instead' : 'Customise the limits'}</button>
      {#if form.custom}
        <div class="limits" id="goal-new-limits">
          {#each FIELDS as field (field)}
            {@const range = rangeOf(field)}
            <div class="field">
              <label for="goal-new-{field}">{LIMIT_LABEL[field]}</label>
              <input id="goal-new-{field}" inputmode="numeric" autocomplete="off" bind:value={form.limits[field]} disabled={sending}
                aria-describedby="goal-new-{field}-hint" />
              {#if range}<span class="hint" id="goal-new-{field}-hint">{formatNumber(range.low)} to {formatNumber(range.high)}</span>{/if}
            </div>
          {/each}
          <label class="check">
            <input id="goal-new-quiet" type="checkbox" bind:checked={form.quiet_hours_ok} disabled={sending} />
            Sessions may start in quiet hours
          </label>
        </div>
      {/if}
    </div>

    <div class="field">
      <label for="goal-new-channel">Channel{options.has_default_channel ? ' (optional)' : ''}</label>
      <input id="goal-new-channel" bind:value={form.origin_channel} autocomplete="off" spellcheck="false" disabled={sending}
        placeholder="telegram:<chat id>" aria-describedby="goal-new-channel-hint" />
      <span class="hint" id="goal-new-channel-hint">{CHANNEL_HINT}{options.has_default_channel ? ' Left empty, they go to the default chat.' : ''}</span>
    </div>

    {#if problems.length}
      <ul class="problems" role="alert">{#each problems as p (p)}<li>{p}</li>{/each}</ul>
    {/if}
    <div class="actions">
      <Button type="submit" id="goal-new-submit" size="sm" class="max-md:min-h-11 max-md:px-4" disabled={sending}>
        {sending ? 'Creating…' : 'Create draft'}
      </Button>
    </div>
    {#if refused}<p class="result-err" role="alert">{refused}</p>{/if}
  </form>
</section>

<style>
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem; margin-bottom: 1.25rem; min-width: 0; }
  h2 { font-size: 1rem; font-weight: 650; margin: 0; }
  .lead { font-size: 0.8125rem; color: var(--muted); margin: 0.25rem 0 0.875rem; }
  form { display: flex; flex-direction: column; gap: 0.875rem; }
  .field { display: flex; flex-direction: column; gap: 0.25rem; max-width: 36rem; }
  .field label, legend { font-size: 0.75rem; font-weight: 600; color: var(--muted); padding: 0; }
  .field input, .field textarea { padding: 0.5rem 0.625rem; background: var(--surface); border: 1px solid var(--field-border);
    border-radius: var(--radius-sm); color: var(--text); font: inherit; font-size: 0.875rem; min-width: 0; }
  .field input { height: 2.75rem; }
  .field textarea { resize: vertical; }
  .field input:focus-visible, .field textarea:focus-visible, .size:focus-within {
    outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .hint { font-size: 0.75rem; color: var(--muted); }
  .sizes { border: 0; margin: 0; padding: 0; min-width: 0; }
  .sizes:disabled .size { opacity: 0.6; }
  .cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.5rem; margin-top: 0.25rem; }
  .size { position: relative; display: flex; flex-direction: column; gap: 0.125rem; padding: 0.625rem 0.75rem; border: 1px solid var(--border);
    border-radius: var(--card-radius-sm); cursor: pointer; transition: background var(--transition), border-color var(--transition); }
  .size:hover { background: var(--wash); }
  .size.chosen { border-color: var(--action); background: var(--wash); }
  .size input { position: absolute; opacity: 0; pointer-events: none; }
  .size-name { font-size: 0.875rem; font-weight: 600; }
  .disclose { background: none; border: 0; padding: 0; font: inherit; font-size: 0.8125rem; color: var(--accent-text); cursor: pointer;
    border-radius: var(--radius-sm); }
  .disclose:hover { text-decoration: underline; }
  .disclose:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .limits { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.75rem; margin-top: 0.625rem; }
  .check { display: inline-flex; align-items: center; gap: 0.5rem; font-size: 0.875rem; cursor: pointer; grid-column: 1 / -1; }
  .check input { width: 1rem; height: 1rem; }
  .problems { margin: 0; padding-left: 1.25rem; list-style: disc; font-size: 0.8125rem; color: var(--warn-text); }
  .actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  .result-err { font-size: 0.8125rem; color: var(--warn-text); margin: 0; overflow-wrap: anywhere; }
  @media (max-width: 768px) {
    .cards, .limits { grid-template-columns: minmax(0, 1fr); }
    .size, .check, .disclose { min-height: 2.75rem; }
  }
</style>
