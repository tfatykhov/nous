<script lang="ts">
  import { apiGet, apiSend } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { MemoryWriteHealthData, MwhRuleId } from '../lib/types/api';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import FilterBar from '../lib/ui/FilterBar.svelte';
  import { fmtWhen } from '../lib/harness';
  import { dayRows, fmtValue, ruleTiles, stateTone, RULE_LABEL, SOURCE_COLUMNS, STATE_LABEL } from '../lib/memoryWriteHealth';

  /** A rule's tone as a status pill (app.css .status-pill). */
  const PILL: Record<string, string> = { bad: 'bad', warn: 'warn', ok: 'ok', muted: 'neutral' };

  // GET /dashboard/memory-write-health: the daily write-health rollup and its rules. These findings never go to
  // Telegram: this page (and the companion card) is where they show. Acknowledge records who accepted a rule's
  // evidence (a deliberate restore or delete) and restarts that rule's baseline.
  // Embedded in Memory health's Writes tab: that screen's header is the title, so this view drops its h1
  // (its subtitle, window filter and staleness badge stay).
  let { embedded = false }: { embedded?: boolean } = $props();
  let days = $state('30');
  const store = usePoll(
    makePollStore<MemoryWriteHealthData>(
      (signal) => apiGet<MemoryWriteHealthData>(`/dashboard/memory-write-health?days=${days}`, { signal }),
      60_000,
    ),
  );
  let first = true;
  $effect(() => {
    void days;
    if (first) { first = false; return; }
    void store.refresh();
  });

  let notes = $state<Record<string, string>>({});
  let busy = $state<MwhRuleId | null>(null);
  let ackError = $state('');

  async function acknowledge(rule: MwhRuleId) {
    busy = rule;
    ackError = '';
    try {
      await apiSend('/dashboard/memory-write-health/acknowledge', { rule, note: notes[rule] ?? '' }, 'POST');
      notes[rule] = '';
      await store.refresh();
    } catch (e) {
      ackError = `Could not acknowledge ${rule}: ${(e as Error).message}`;
    } finally {
      busy = null;
    }
  }

  const SOURCE_SHORT: Record<string, string> = {
    enumerative_extractor: 'Enumerative', user_direct: 'User direct', reflection: 'Reflection',
    sleep_reflection: 'Sleep refl.', contradiction_resolution: 'Contradiction', cluster_consolidation: 'Cluster',
    episode_summarizer: 'Episode sum.', other: 'Other',
  };
  const n = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v.toLocaleString());
</script>

<header class="view-head">
  <div>
    {#if !embedded}<h1>Memory write health</h1>{/if}
    <p class="subtitle">What the memory write paths stored each day, and the rules that watch them. Shown here and on the companion only, never on Telegram.</p>
  </div>
  <div class="head-right">
    <FilterBar label="Window" required bind:value={days}
      options={[{ value: '7', label: '7 d' }, { value: '30', label: '30 d' }, { value: '90', label: '90 d' }]} />
    <StaleBadge state={$store} />
  </div>
</header>

{#if $store.data}
  {@const d = $store.data}
  {#if !d.enabled}
    <p class="state-msg" role="status">Memory write health is off (NOUS_MEMORY_WRITE_HEALTH_ENABLED).</p>
  {:else if d.checks.length === 0}
    <p class="state-msg" role="status">No rollup yet: the hourly check writes its first rows within an hour of start.</p>
  {:else}
    {@const tiles = ruleTiles(d.checks)}
    {@const tripped = tiles.filter((t) => t.state === 'tripped').length}
    <section class="strip verdict-{tripped ? 'bad' : 'ok'}" aria-labelledby="mwh-verdict">
      <h2 id="mwh-verdict" class="verdict-title">
        {tripped ? `${tripped} rule${tripped === 1 ? '' : 's'} tripped: you decide` : 'No rule tripped'}
      </h2>
      <span class="small">Nothing here changes memory. Acknowledge only records that you accept what a rule saw.</span>
    </section>

    {#if ackError}<p class="ack-error" role="alert">{ackError}</p>{/if}

    <div class="tiles-grid">
      {#each tiles as t (t.id)}
        <section class="tile tone-{stateTone(t.state)}" aria-labelledby="rule-{t.id}">
          <div class="tile-head">
            <h2 id="rule-{t.id}"><span class="rule-id">{t.id}</span> {t.label}</h2>
            <span class="status-pill status-pill--{PILL[stateTone(t.state)]}">{STATE_LABEL[t.state]}</span>
          </div>
          <div class="value">{fmtValue(t.id, t.value)} <span class="small muted">/ threshold {fmtValue(t.id, t.threshold)}</span></div>
          <p class="small muted">{t.hint}</p>
          {#each t.summaries as s (s)}<p class="summary">{s}</p>{/each}
          {#if t.acknowledged}
            <p class="small muted">Acknowledged up to {t.acknowledged.day} by {t.acknowledged.actor ?? '—'}</p>
          {/if}
          {#if t.state === 'tripped'}
            <div class="ack">
              <label class="small" for="note-{t.id}">Note (optional)</label>
              <input id="note-{t.id}" type="text" maxlength="500" bind:value={notes[t.id]} placeholder="why this was deliberate" />
              <button type="button" disabled={busy !== null} onclick={() => acknowledge(t.id)}>
                {busy === t.id ? 'Acknowledging…' : `Acknowledge ${t.id}`}
              </button>
            </div>
          {/if}
        </section>
      {/each}
    </div>

    {#if d.live}
      {@const l = d.live}
      <section class="panel" aria-labelledby="hd-title">
        <div class="panel-head">
          <h2 id="hd-title">Hard-delete signals</h2>
          <span class="small muted">Observed {fmtWhen(l.observed_at)} · ledger since {l.ledger_oldest ? l.ledger_oldest.slice(0, 10) : '—'}</span>
        </div>
        <dl class="tiles">
          <div><dt>Facts (all rows)</dt><dd>{n(l.facts_total)}</dd></div>
          <div><dt>Episodes (all rows)</dt><dd>{n(l.episodes_total)}</dd></div>
          <div><dt>Fact ids gone</dt><dd>{n(l.vanished_fact_ids)}</dd></div>
          <div><dt>Episode ids gone</dt><dd>{n(l.vanished_episode_ids)}</dd></div>
          <div><dt>Dangling edges</dt><dd>{n(l.dangling_edges)}</dd></div>
          <div><dt>Open episodes &gt; 24 h</dt><dd>{n(l.open_episodes_over_24h)}</dd></div>
        </dl>
        <p class="small muted">Ids gone counts every id the ledger recorded with no row, including those gone before the monitor started (its baseline). H1 counts only what rose after the baseline or the last acknowledgement.</p>
        {#if l.profile_card}
          <p class="small">Profile card: {l.profile_card.entries} lines, {l.profile_card.tokens} tokens, {Math.round(l.profile_card.changed_frac * 100)}% changed at the last rewrite.</p>
        {/if}
      </section>
    {/if}

    <section class="panel" aria-labelledby="days-title">
      <div class="panel-head">
        <h2 id="days-title">Day by day</h2>
        <span class="small muted">UTC days · tier-1 columns count non-enumerative facts</span>
      </div>
      <div class="table-wrap">
        <table class="stats">
          <thead>
            <tr>
              <th scope="col">Day</th><th scope="col">Facts</th>
              {#each SOURCE_COLUMNS as s (s)}<th scope="col" title={s}>{SOURCE_SHORT[s]}</th>{/each}
              <th scope="col">Person</th><th scope="col">Pref.</th><th scope="col">Rule</th>
              <th scope="col">Deactivated</th><th scope="col">Merged + superseded</th><th scope="col">Tier-1 stale</th>
              <th scope="col">Rejected</th><th scope="col">Episodes started</th><th scope="col">Ended</th><th scope="col">Card rewrites</th>
            </tr>
          </thead>
          <tbody>
            {#each dayRows(d.days) as r (r.day)}
              <tr>
                <th scope="row">{r.day}</th><td>{r.facts}</td>
                {#each SOURCE_COLUMNS as s (s)}<td>{r.bySource[s] ?? 0}</td>{/each}
                <td>{r.person}</td><td>{r.preference}</td><td>{r.rule}</td>
                <td>{r.deactivated}</td><td>{r.merged}</td><td class:hot={r.tier1Stale > 0}>{r.tier1Stale}</td>
                <td>{r.rejected}</td><td>{r.started}</td><td>{r.ended}</td><td>{r.cardRewrites}</td>
              </tr>
            {:else}
              <tr><td colspan="99" class="empty">No days in this window.</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel" aria-labelledby="acks-title">
      <div class="panel-head">
        <h2 id="acks-title">Acknowledgements</h2>
        <span class="small muted">Latest first · "system" is the baseline the monitor took when it started</span>
      </div>
      {#if d.acknowledgements.length === 0}
        <p class="empty">None yet.</p>
      {:else}
        <div class="table-wrap">
          <table class="stats acks">
            <thead><tr><th scope="col">Rule</th><th scope="col">Up to</th><th scope="col">By</th><th scope="col">When</th><th scope="col">Note</th></tr></thead>
            <tbody>
              {#each d.acknowledgements as a, i (i)}
                <tr>
                  <th scope="row">{a.rule} {RULE_LABEL[a.rule] ?? ''}</th><td>{a.day}</td><td>{a.actor}</td>
                  <td>{fmtWhen(a.at)}</td><td class="note">{a.note}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>
  {/if}
{:else if $store.error}
  <p class="state-msg error">Failed to load memory write health — retrying…</p>
{:else}
  <p class="state-msg">Loading…</p>
{/if}

<style>
  .view-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.25rem; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  h1 { font-size: 30px; font-weight: 650; margin: 0 0 0.25rem; letter-spacing: -0.022em; line-height: 1.15; }
  h2 { font-size: 0.9375rem; font-weight: 600; margin: 0; }
  /* Anchored on purpose: it out-ranks .verdict-title's 700, so the verdict reads 650 like every heading. */
  :global(html:root .page-ground) h2 { font-weight: 650; }
  .subtitle { font-size: 0.8125rem; color: var(--muted); margin: 0; }
  .strip { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem 2rem; flex-wrap: wrap;
    padding: 0.875rem 1.125rem; border-radius: var(--card-radius); border: 1px solid; margin-bottom: 1rem; }
  /* Status by token: the alphas of the old literals are the mix percentages (dark identical). */
  .verdict-ok { color: var(--ok-text); border-color: color-mix(in srgb, var(--ok-text) 35%, transparent);
    background: color-mix(in srgb, var(--ok-text) 8%, transparent); }
  .verdict-bad { color: var(--err-text); border-color: color-mix(in srgb, var(--tone-bad) 50%, transparent);
    background: color-mix(in srgb, var(--tone-bad) 10%, transparent); }
  .verdict-title { font-size: 0.9375rem; font-weight: 700; }
  .strip .small { color: var(--text); }
  .ack-error { color: var(--err-text); font-size: 0.8125rem; margin: 0 0 1rem; }
  .tiles-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(250px, 100%), 1fr)); gap: 1rem; }
  .tile { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow); padding: 1rem 1.125rem;
    display: flex; flex-direction: column; gap: 0.5rem; min-width: 0; }
  .tile.tone-bad { border-color: color-mix(in srgb, var(--tone-bad) 60%, transparent); }
  .tile.tone-warn { border-color: color-mix(in srgb, var(--warn-text) 50%, transparent); }
  .tile p { margin: 0; }
  .tile-head { display: flex; justify-content: space-between; align-items: baseline; gap: 0.5rem; }
  .rule-id { font-family: var(--font-mono); font-size: 0.75rem; color: var(--muted); }
  .value { font-size: 1.5rem; font-weight: 700; }
  .summary { font-size: 0.8125rem; overflow-wrap: anywhere; }
  .ack { display: flex; flex-direction: column; gap: 0.375rem; margin-top: 0.25rem; }
  .ack input { padding: 0.375rem 0.5rem; border: 1px solid var(--border); border-radius: var(--ctl-radius); background: var(--bg);
    color: var(--text); font-size: 0.8125rem; min-width: 0; }
  .ack button { align-self: flex-start; padding: 0.375rem 0.875rem; border-radius: var(--ctl-radius); border: 1px solid var(--action);
    background: var(--action); color: var(--on-accent); font-size: 0.8125rem; font-weight: 600; cursor: pointer; }
  .ack button:disabled { opacity: 0.6; cursor: default; }
  /* A rule's state is app.css's .status-pill (one tinted pill on every screen). */
  .panel { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.25rem; min-width: 0; margin-top: 1.25rem; }
  .panel p { margin: 0.5rem 0 0; }
  .panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 0.5rem 1rem; flex-wrap: wrap; margin-bottom: 0.875rem; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.75rem; margin: 0; }
  .tiles dt { font-size: 0.6875rem; font-weight: 600; color: var(--muted); text-transform: none; letter-spacing: 0; }
  .tiles dd { margin: 0.125rem 0 0; font-size: 1.375rem; font-weight: 700; }
  /* contain: a wide table scrolls in its own box instead of widening the page (main is a flex item). */
  .table-wrap { overflow-x: auto; contain: inline-size; }
  .stats { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
  .stats th, .stats td { padding: 0.375rem 0.5rem; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
  .stats th:first-child, .stats td:first-child { text-align: left; }
  .stats thead th { font-size: 0.6875rem; font-weight: 600; color: var(--muted); }
  .stats td.hot { color: var(--err-text); font-weight: 700; }
  .acks td, .acks th { text-align: left; }
  .acks td.note { white-space: normal; overflow-wrap: anywhere; min-width: 12rem; }
  .empty { color: var(--muted); font-size: 0.875rem; text-align: center; padding: 1rem 0; margin: 0; }
  .small { font-size: 0.75rem; }
  .muted { color: var(--muted); }
  .state-msg { margin-top: 2rem; text-align: center; color: var(--muted); font-size: 0.875rem; }
  .state-msg.error { color: var(--err-text); }
</style>
