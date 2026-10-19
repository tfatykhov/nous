<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { AutonomyData, InventoryTool, ScoreStats, VerifiedShare } from '../lib/types/api';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import FilterBar from '../lib/ui/FilterBar.svelte';
  import DataTable from '../lib/ui/DataTable.svelte';
  import { fmtWhen, FLAG_GLOSS } from '../lib/harness';
  import {
    bars, count, coverageFrom, pct, score, scorerHealth, OUTCOMES, OUTCOME_LABEL, ROLLUP_LABEL, CONTEXT_LABEL, LEVEL_LABEL,
  } from '../lib/autonomy';

  // GET /dashboard/autonomy: how tasks score, where the owner's attention goes,
  // and what the agent does without asking. Read-only, like the endpoint.
  let days = $state('7');
  const store = usePoll(
    makePollStore<AutonomyData>(
      (signal) => apiGet<AutonomyData>(`/dashboard/autonomy?days=${days}`, { signal }),
      60_000,
    ),
  );
  let first = true;
  $effect(() => {
    void days;
    if (first) { first = false; return; }
    void store.refresh();
  });

  const OUTCOME_COLOR: Record<string, string> = {
    done_verified: 'var(--green)', done_checked: '#34d399', done_self: '#60a5fa', partial: '#f59e0b', failed: 'var(--red)', not_scored: 'var(--muted)',
  };
  const CONTEXT_COLOR: Record<string, string> = { owner_present: '#60a5fa', approved: '#34d399', autonomous: '#fb923c' };
  const CONTEXTS = ['owner_present', 'approved', 'autonomous'] as const;

  const outcomeBars = (byOutcome: Record<string, number>) => bars(OUTCOMES.map((o) => [o, byOutcome[o] ?? 0]));
  const verifiedRows = (d: AutonomyData): [string, VerifiedShare][] => [
    ['Provisional', d.task_scores.headline.verified_share.provisional],
    ['Settled', d.task_scores.headline.verified_share.settled],
  ];
  const statRows = (d: AutonomyData): [string, ScoreStats][] => [
    ['Provisional', d.task_scores.subtasks.provisional],
    ['Settled', d.task_scores.subtasks.settled],
  ];
  const successes = (t: InventoryTool) => t.by_context.owner_present + t.by_context.approved + t.by_context.autonomous;
  const share = (t: InventoryTool, k: (typeof CONTEXTS)[number]) => {
    const n = successes(t);
    return n ? Math.round((t.by_context[k] / n) * 100) : 0;
  };

  const toolCols = [
    { key: 'tool', label: 'Tool' },
    { key: 'runs', label: 'Runs' },
    { key: 'who', label: 'Who was there' },
    { key: 'level', label: 'Level' },
    { key: 'undo', label: 'Undoable' },
  ];
  const warnCols = [
    { key: 'context', label: 'Context' },
    { key: 'tool', label: 'Tool' },
    { key: 'violation', label: 'Flag' },
    { key: 'mode', label: 'Mode' },
    { key: 'count', label: 'Count' },
  ];
</script>

<header class="view-head">
  <div>
    <h1>Autonomy</h1>
    <p class="subtitle">How Nous's tasks score, where your attention goes, and what it does without asking</p>
  </div>
  <div class="head-right">
    <FilterBar label="Window" required bind:value={days}
      options={[{ value: '7', label: '7 d' }, { value: '30', label: '30 d' }, { value: '90', label: '90 d' }]} />
    <StaleBadge state={$store} />
  </div>
</header>

{#if $store.data}
  {@const d = $store.data}
  {@const ts = d.task_scores}
  {@const health = scorerHealth(ts.scorer, ts.scorer_version)}
  {@const h = ts.headline}
  {@const w = `${d.window_days} d`}
  {@const acting = d.inventory.tools.filter((t) => t.de_facto_level !== 'L0').length}

  <section class="strip verdict-{health.tone}" aria-labelledby="scorer-title">
    <div>
      <h2 id="scorer-title" class="verdict-title">{health.title}</h2>
      <div class="verdict-detail">{health.detail}</div>
    </div>
    <dl class="strip-meta">
      <div><dt>Scorer version</dt><dd>{ts.scorer_version}</dd></div>
      <div><dt>First score</dt><dd>{fmtWhen(ts.first_scored_at)}</dd></div>
      <div><dt>Latest score write</dt><dd>{fmtWhen(ts.last_scored_at)}</dd></div>
    </dl>
  </section>

  {#if ts.audits_without_action_row > 0}
    <p class="small" role="status">
      {ts.audits_without_action_row} spot-check answer{ts.audits_without_action_row === 1 ? '' : 's'} on file with no matching tap recorded on its card.
    </p>
  {/if}

  <div class="cards">
    <section class="card" aria-labelledby="h-verified">
      <h2 id="h-verified">Done, verified</h2>
      <div class="pair">
        {#each verifiedRows(d) as [label, vs] (label)}
          <div>
            <div class="big">{pct(vs.share)}</div>
            <div class="small muted">{label}: {vs.verified} of {vs.scored} scored subtasks</div>
          </div>
        {/each}
      </div>
      <p class="small muted">A subtask counts when the harness confirmed it or you graded it a success. Settled means finished more than 14 days ago, so a 7 d window has none.</p>
    </section>
    <section class="card" aria-labelledby="h-overrides">
      <h2 id="h-overrides">Your overrides</h2>
      <div class="big">{h.owner_overrides.total}</div>
      <div class="small muted">{h.owner_overrides.subtasks} on subtasks · {h.owner_overrides.rollups} on DAG runs and intentions</div>
      <p class="small muted">Times you rejected, undid or corrected its work in {w}. On a subtask, each one cancels a verified success; on a DAG run or intention it enters no score.</p>
    </section>
    <section class="card" aria-labelledby="h-penalties">
      <h2 id="h-penalties">Harness penalties</h2>
      <dl class="triple">
        <div><dt>Harness errors</dt><dd>{h.harness_penalties.harness_errors}</dd></div>
        <div><dt>Refusals</dt><dd>{h.harness_penalties.refusals}</dd></div>
        <div><dt>Unverified claims</dt><dd>{h.harness_penalties.unverified_claims}</dd></div>
      </dl>
      <p class="small muted">Over every unit in {w}, DAG runs and intentions included.</p>
    </section>
  </div>

  <section class="panel" aria-labelledby="subtasks-title">
    <div class="panel-head">
      <h2 id="subtasks-title">Subtasks</h2>
      <span class="small muted">
        {#if ts.subtasks.finished == null}
          Coverage is counted from the scorer's first run.
        {:else}
          {coverageFrom(ts.first_scored_at, d.window_days)}: {ts.subtasks.covered} of {ts.subtasks.finished} finished subtasks have a score row ({pct(ts.subtasks.coverage)})
        {/if}
      </span>
    </div>
    <div class="split">
      <div class="breakdown">
        <div class="label">By outcome</div>
        {#each outcomeBars(ts.subtasks.by_outcome) as b (b.key)}
          <div class="bar-row">
            <span class="bar-key">{OUTCOME_LABEL[b.key] ?? b.key}</span>
            <div class="bar"><div class="bar-fill" style:width="{b.pct}%" style:background={OUTCOME_COLOR[b.key]}></div></div>
            <span class="bar-count">{b.count}</span>
          </div>
        {/each}
      </div>
      <div class="table-wrap">
      <table class="stats">
        <caption class="label">Scores (−1 to 1)</caption>
        <thead><tr><th scope="col"></th><th scope="col">Count</th><th scope="col">Mean</th><th scope="col">p10</th><th scope="col">p50</th><th scope="col">p90</th><th scope="col">Overrides</th></tr></thead>
        <tbody>
          {#each statRows(d) as [label, s] (label)}
            <tr>
              <th scope="row">{label}</th><td>{s.count}</td><td>{score(s.mean)}</td><td>{score(s.p10)}</td>
              <td>{score(s.p50)}</td><td>{score(s.p90)}</td><td>{s.overrides}</td>
            </tr>
          {/each}
        </tbody>
      </table>
      </div>
    </div>
  </section>

  <section class="panel" aria-labelledby="rollups-title">
    <div class="panel-head">
      <h2 id="rollups-title">DAG runs and intentions</h2>
      <span class="small muted">Counted apart: never added into a subtask's score</span>
    </div>
    <div class="table-wrap">
      <table class="stats">
        <thead>
          <tr>
            <th scope="col"></th><th scope="col">Count</th>
            {#each OUTCOMES as o (o)}<th scope="col">{OUTCOME_LABEL[o]}</th>{/each}
            <th scope="col">Harness errors</th><th scope="col">Refusals</th><th scope="col">Unverified claims</th><th scope="col">Overrides</th>
          </tr>
        </thead>
        <tbody>
          {#each Object.entries(ts.rollups) as [kind, r] (kind)}
            <tr>
              <th scope="row">{ROLLUP_LABEL[kind] ?? kind}</th><td>{r.count}</td>
              {#each OUTCOMES as o (o)}<td>{r.by_outcome[o] ?? 0}</td>{/each}
              <td>{r.own_penalties.harness_errors}</td><td>{r.own_penalties.refusals}</td>
              <td>{r.own_penalties.unverified_claims}</td><td>{r.own_penalties.owner_overrides}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </section>

  <section class="panel" aria-labelledby="attention-title">
    <div class="panel-head">
      <h2 id="attention-title">Your attention</h2>
      <span class="small muted">Last {w}</span>
    </div>
    <dl class="tiles">
      <div><dt>Taps in the companion</dt><dd>{d.taps.total}</dd></div>
      <div><dt>On heartbeat cards</dt><dd>{d.triage_taps.heartbeat}</dd></div>
      <div><dt>On decision cards</dt><dd>{d.triage_taps.decision}</dd></div>
      <div><dt>Approval cards asked</dt><dd>{d.approval_cards_asked}</dd></div>
    </dl>
    <div class="split">
      <div>
        <div class="label">Taps by action</div>
        {#if d.taps.by_action.length === 0}
          <p class="empty">No taps in this window.</p>
        {:else}
          {#each bars(d.taps.by_action.map((a) => [a.action_name, a.count])) as b (b.key)}
            <div class="bar-row">
              <span class="bar-key mono" title={b.key}>{b.key}</span>
              <div class="bar"><div class="bar-fill" style:width="{b.pct}%" style:background="var(--accent)"></div></div>
              <span class="bar-count">{b.count}</span>
            </div>
          {/each}
        {/if}
      </div>
      <div>
        <div class="label">Decision grades</div>
        {#if d.decision_grades.length === 0}
          <p class="empty">No decisions graded in this window.</p>
        {:else}
          <div class="table-wrap">
          <table class="stats">
            <thead><tr><th scope="col">Reviewer</th><th scope="col">Outcome</th><th scope="col">Count</th></tr></thead>
            <tbody>
              {#each d.decision_grades as g (`${g.reviewer}|${g.outcome}`)}
                <tr><td class="mono">{g.reviewer}</td><td>{g.outcome ?? '—'}</td><td>{g.count}</td></tr>
              {/each}
            </tbody>
          </table>
          </div>
        {/if}
      </div>
    </div>
  </section>

  <section class="panel" aria-labelledby="inventory-title">
    <div class="panel-head">
      <h2 id="inventory-title">What it did, and who was there</h2>
      <span class="small muted">Last {d.inventory.days} d, whatever the window · side-effecting tools only</span>
    </div>
    {#if d.inventory.tools.length === 0}
      <p class="empty">No side-effecting tool ran in the last {d.inventory.days} days.</p>
    {:else}
      <p class="small">{acting} of {d.inventory.tools.length} tools ran at least once without you in the loop.</p>
      <div class="legend" aria-hidden="true">
        {#each CONTEXTS as k (k)}<span><i style:background={CONTEXT_COLOR[k]}></i>{CONTEXT_LABEL[k]}</span>{/each}
      </div>
      <DataTable columns={toolCols} rows={d.inventory.tools} mode="cards"
        rowKey={(t: InventoryTool) => t.tool_name} rowLabel={(t: InventoryTool) => t.tool_name}>
        {#snippet cell(t: InventoryTool, c: { key: string })}
          {#if c.key === 'tool'}
            <span class="mono">{t.tool_name}</span>
          {:else if c.key === 'runs'}
            <span><strong>{count(t.by_status.success)}</strong>&nbsp;<span class="small muted">of {count(t.total)}</span></span>
          {:else if c.key === 'who'}
            <div class="stack" role="img"
              aria-label={CONTEXTS.map((k) => `${CONTEXT_LABEL[k]} ${t.by_context[k]}`).join(', ')}>
              {#each CONTEXTS as k (k)}
                {#if t.by_context[k] > 0}<div style:width="{share(t, k)}%" style:background={CONTEXT_COLOR[k]}></div>{/if}
              {/each}
            </div>
          {:else if c.key === 'level'}
            {#if successes(t) === 0}
              <span class="small muted">no successful run</span>
            {:else}
              <span class="pill" class:pill-warn={t.de_facto_level !== 'L0'}>{LEVEL_LABEL[t.de_facto_level] ?? t.de_facto_level}</span>
            {/if}
          {:else if c.key === 'undo'}
            <span class="small" class:muted={!t.compensable}>{t.compensable ? 'yes' : 'no'}</span>
          {/if}
        {/snippet}
        {#snippet detail(t: InventoryTool)}
          <dl class="detail">
            {#each CONTEXTS as k (k)}<div><dt>{CONTEXT_LABEL[k]}</dt><dd>{t.by_context[k]}</dd></div>{/each}
            <div><dt>Failed</dt><dd>{t.by_status.error}</dd></div>
            <div><dt>Refused</dt><dd>{t.by_status.blocked}</dd></div>
            <div><dt>Side effects</dt><dd class="mono">{Object.entries(t.side_effects).map(([k, n]) => `${k} ${n}`).join(' · ') || '—'}</dd></div>
          </dl>
        {/snippet}
      </DataTable>
    {/if}
    <table class="stats censors">
      <caption class="label">Censors, last {d.inventory.censors.days} d</caption>
      <thead><tr><th scope="col">Written by</th><th scope="col">Created</th><th scope="col">Deactivated</th></tr></thead>
      <tbody>
        {#each Object.entries(d.inventory.censors.by_provenance) as [p, c] (p)}
          <tr><th scope="row">{p}</th><td>{c.created}</td><td>{c.deactivated}</td></tr>
        {/each}
      </tbody>
    </table>
  </section>

  <section class="panel" aria-labelledby="warn-title">
    <div class="panel-head">
      <h2 id="warn-title">Tool-policy flags</h2>
      <span class="small muted">Last {w} · top 50 by count · details in Harness</span>
    </div>
    <!-- Gate on the flag, never on the list: with persistence off the server sends [] for "not recorded". -->
    {#if !d.events_persisted}
      <p class="empty">Not measured — event persistence is off (<code>NOUS_F026_PERSISTENCE_ENABLED=false</code>).</p>
    {:else if d.warn_classes.length === 0}
      <p class="empty">Nothing flagged in this window.</p>
    {:else}
      <div class="table-wrap">
      <DataTable columns={warnCols} rows={d.warn_classes}
        rowKey={(r: AutonomyData['warn_classes'][number]) => JSON.stringify([r.mode, r.context_kind, r.violation, r.tool_name])}>
        {#snippet cell(r: AutonomyData['warn_classes'][number], c: { key: string })}
          {#if c.key === 'context'}
            <span class="small">{r.context_kind}</span>
          {:else if c.key === 'tool'}
            <span class="mono">{r.tool_name}</span>
          {:else if c.key === 'violation'}
            <span class="mono" title={FLAG_GLOSS[r.violation] ?? ''}>{r.violation}</span>
          {:else if c.key === 'mode'}
            <span class="small muted">{r.mode}</span>
          {:else if c.key === 'count'}
            <strong>{r.count}</strong>
          {/if}
        {/snippet}
      </DataTable>
      </div>
    {/if}
  </section>
{:else if $store.error}
  <p class="state-msg error">Failed to load autonomy data — retrying…</p>
{:else}
  <p class="state-msg">Loading…</p>
{/if}

<style>
  .view-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.25rem; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  h1 { font-size: 1.375rem; font-weight: 700; margin: 0 0 0.25rem; }
  h2 { font-size: 0.9375rem; font-weight: 600; margin: 0; }
  .subtitle { font-size: 0.8125rem; color: var(--muted); margin: 0; }
  .strip { display: flex; justify-content: space-between; align-items: center; gap: 1rem 2rem; flex-wrap: wrap;
    padding: 0.875rem 1.125rem; border-radius: 8px; border: 1px solid; margin-bottom: 1rem; }
  .verdict-warn { color: #f59e0b; border-color: rgba(245, 158, 11, 0.35); background: rgba(245, 158, 11, 0.08); }
  .verdict-ok { color: #10b981; border-color: rgba(16, 185, 129, 0.35); background: rgba(16, 185, 129, 0.08); }
  .verdict-muted { color: var(--muted); border-color: var(--border); background: transparent; }
  .verdict-title { font-size: 0.875rem; font-weight: 700; }
  .verdict-detail { font-size: 0.8125rem; color: var(--text); margin-top: 0.125rem; }
  .strip-meta { display: flex; gap: 1.5rem; flex-wrap: wrap; margin: 0; }
  .strip-meta dt, .tiles dt, .triple dt, .detail dt, .label { font-size: 0.6875rem; font-weight: 600; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.04em; }
  .strip-meta dd { margin: 0.125rem 0 0; font-size: 0.8125rem; color: var(--text); }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr)); gap: 1rem; }
  .card, .panel { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; min-width: 0; }
  .card { display: flex; flex-direction: column; gap: 0.75rem; }
  .card p { margin: 0; }
  .panel { margin-top: 1.25rem; }
  .panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 0.5rem 1rem; flex-wrap: wrap; margin-bottom: 0.875rem; }
  .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  .big { font-size: 2rem; font-weight: 700; line-height: 1.1; }
  .triple { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.75rem; margin: 0; }
  .triple dd, .tiles dd { margin: 0.125rem 0 0; font-size: 1.5rem; font-weight: 700; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.75rem; margin: 0 0 1rem; }
  .split > * { min-width: 0; }
  .split { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr)); gap: 1.25rem 2rem; align-items: start; }
  .breakdown { display: flex; flex-direction: column; gap: 0.5rem; }
  .bar-row { display: grid; grid-template-columns: minmax(0, 10rem) minmax(0, 1fr) 2.75rem; gap: 0.625rem; align-items: center; margin-top: 0.375rem; }
  .bar-key { font-size: 0.8125rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bar { height: 8px; border-radius: 4px; background: var(--border); overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; }
  .bar-count { font-size: 0.8125rem; font-weight: 600; text-align: right; }
  /* contain: a wide table scrolls in its own box instead of widening the page (main is a flex item). */
  .table-wrap { overflow-x: auto; contain: inline-size; }
  .stats { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
  .stats caption { text-align: left; padding-bottom: 0.375rem; }
  .stats th, .stats td { padding: 0.375rem 0.5rem; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
  .stats th:first-child, .stats td:first-child { text-align: left; }
  .stats thead th { font-size: 0.6875rem; font-weight: 600; color: var(--muted); }
  .censors { margin-top: 1.25rem; max-width: 28rem; }
  .legend { display: flex; gap: 1rem; flex-wrap: wrap; font-size: 0.75rem; color: var(--muted); margin: 0.5rem 0 0.75rem; }
  .legend i { display: inline-block; width: 0.625rem; height: 0.625rem; border-radius: 2px; margin-right: 0.375rem; vertical-align: -1px; }
  .stack { display: flex; width: 8rem; max-width: 100%; height: 8px; border-radius: 4px; background: var(--border); overflow: hidden; }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 0.6875rem; font-weight: 600;
    border: 1px solid var(--border); color: var(--muted); white-space: nowrap; }
  .pill-warn { color: #fb923c; border-color: rgba(251, 146, 60, 0.5); }
  .detail { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 0.5rem 1.5rem; margin: 0; padding: 0.5rem 0; }
  .detail dd { margin: 0.125rem 0 0; font-size: 0.8125rem; overflow-wrap: anywhere; }
  .mono, code { font-family: var(--font-mono); font-size: 0.75rem; }
  .empty { color: var(--muted); font-size: 0.875rem; text-align: center; padding: 1rem 0; margin: 0; }
  .small { font-size: 0.75rem; }
  .muted { color: var(--muted); }
  .state-msg { margin-top: 2rem; text-align: center; color: var(--muted); font-size: 0.875rem; }
  .state-msg.error { color: var(--red); }
  @media (max-width: 520px) {
    .triple { grid-template-columns: 1fr 1fr; }
    .bar-row { grid-template-columns: minmax(0, 7rem) minmax(0, 1fr) 2.5rem; }
  }
</style>
