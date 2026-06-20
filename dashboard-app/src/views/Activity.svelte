<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { ActivityData, ActivityEvent } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';

  // Load-once: interval so large it never auto-fires. Refresh button handles reload.
  const store = usePoll(
    makePollStore<ActivityData>(
      (signal) => apiGet<ActivityData>('/dashboard/activity?hours=168', { signal }),
      0, // fetch-once (manual refresh only)
    ),
  );

  // ── Helpers ──────────────────────────────────────────────────────────────

  function formatTimeAgo(dateStr: string | null): string {
    if (!dateStr) return 'Never';
    const diffMs = Date.now() - new Date(dateStr).getTime();
    const diffMins = Math.floor(diffMs / 60_000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);
    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    return new Date(dateStr).toLocaleDateString();
  }

  function formatDateTime(dateStr: string | null): string {
    if (!dateStr) return '—';
    return new Date(dateStr).toLocaleString();
  }

  function eventSummary(event: ActivityEvent): string {
    const d = (event.data ?? {}) as Record<string, unknown>;
    const str = (k: string): string => String(d[k] ?? '');
    const num = (k: string): number => Number(d[k] ?? 0);
    switch (event.type) {
      case 'censor_triggered':
        return `Censor ${str('censor_id')} fired — matched: ${str('matched_text')}`;
      case 'sleep_completed':
        return `Sleep cycle completed. ${num('facts_created')} facts, ${num('procedures_created')} procedures.`;
      case 'schedule_fired':
        return `Schedule ${str('task') || str('schedule_id')} fired.`;
      case 'subtask_completed':
        return `Subtask completed: ${str('title') || str('subtask_id')}`;
      case 'subtask_failed':
        return `Subtask failed: ${str('title') || str('subtask_id')}${d['error'] ? ' — ' + str('error') : ''}`;
      case 'censor_created':
        return `New censor created: ${str('trigger_pattern')} (${d['auto'] ? 'auto' : 'manual'})`;
      case 'censor_escalated':
        return `Censor escalated: ${str('trigger_pattern')} — ${str('reason')}`;
      default:
        return JSON.stringify(event.data).slice(0, 120);
    }
  }

  function typeLabel(type: string): string {
    return type.replace(/_/g, ' ');
  }
</script>

<header class="view-head">
  <div>
    <h1>System Activity</h1>
    <p class="subtitle">Events, censors, schedules, and sleep cycles</p>
  </div>
  <div class="head-right">
    <button class="refresh-btn" onclick={() => void store.refresh()} disabled={$store.loading}>
      {$store.loading ? 'Loading…' : 'Refresh'}
    </button>
    <StaleBadge state={$store} />
  </div>
</header>

{#if $store.data}
  {@const d = $store.data}
  {@const cs = d.censor_stats}
  {@const ss = d.schedule_stats}
  {@const sl = d.sleep_stats}

  <!-- ── Stat cards ──────────────────────────────────────────────────────── -->
  <StatGrid stats={[
    { label: 'Censor Activations (7d)',  value: cs.total_activations_7d },
    { label: 'Auto-created Censors',     value: cs.auto_created },
    { label: 'Manual Censors',           value: cs.manual_created },
    { label: 'False Positives (7d)',     value: cs.false_positives_7d },
    { label: 'Active Schedules',         value: ss.active },
    { label: 'Schedule Fires (7d)',      value: ss.fires_7d },
    { label: 'Last Sleep',               value: formatTimeAgo(sl.last_sleep) },
    { label: 'Sleep Facts Created',      value: sl.facts_created },
  ]} />

  <!-- ── Two-column grid ──────────────────────────────────────────────────── -->
  <div class="activity-grid">

    <!-- Left: Event Timeline -->
    <section class="chart-card">
      <h2>Event Timeline</h2>
      {#if d.events.length > 0}
        <div class="timeline">
          {#each d.events as event}
            <div class="timeline-item">
              <div class="event-header">
                <span class="event-badge">{typeLabel(event.type)}</span>
                <span class="event-time">{formatDateTime(event.created_at)}</span>
              </div>
              <div class="event-summary">{eventSummary(event)}</div>
            </div>
          {/each}
        </div>
      {:else}
        <p class="empty">No events in the last 7 days.</p>
      {/if}
    </section>

    <!-- Right: Side panels -->
    <div class="side-panels">

      <!-- Most Active Censors -->
      <section class="chart-card">
        <h2>Most Active Censors</h2>
        {#if cs.top_censors.length > 0}
          <ul class="item-list">
            {#each cs.top_censors as c}
              <li class="item-row">
                <span class="item-label" title={c.trigger_pattern ?? ''}>
                  {c.trigger_pattern ?? c.id}
                </span>
                <span class="badge">{c.activations}</span>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="empty">No censor activations.</p>
        {/if}
      </section>

      <!-- Upcoming Schedules -->
      <section class="chart-card">
        <h2>Upcoming Schedules</h2>
        {#if ss.next_fires.length > 0}
          <ul class="item-list">
            {#each ss.next_fires as s}
              <li class="schedule-item">
                <div class="schedule-task">{s.task ?? s.id}</div>
                <div class="schedule-time">Next: {formatDateTime(s.next_fire_at)}</div>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="empty">No upcoming schedules.</p>
        {/if}
      </section>

      <!-- Sleep Activity -->
      <section class="chart-card">
        <h2>Sleep Activity</h2>
        <dl class="detail-grid">
          <dt>Last Sleep</dt>
          <dd>{sl.last_sleep ? formatDateTime(sl.last_sleep) : 'Never'}</dd>
          <dt>Total Sleeps</dt>
          <dd>{sl.total_sleeps}</dd>
          <dt>Facts Created</dt>
          <dd>{sl.facts_created}</dd>
          <dt>Procedures</dt>
          <dd>{sl.procedures_created}</dd>
          <dt>Censors Retired</dt>
          <dd>{sl.censors_retired}</dd>
        </dl>
      </section>

    </div>
  </div>

{:else if $store.error}
  <p class="status-msg error">
    Failed to load activity data —
    <button class="retry-link" onclick={() => void store.refresh()}>retry</button>
  </p>
{:else}
  <p class="status-msg">Loading…</p>
{/if}

<style>
  .view-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.25rem;
  }

  h1 {
    font-size: 1.4rem;
    font-weight: 700;
    color: var(--text);
    margin: 0 0 0.125rem;
  }

  .subtitle {
    font-size: 0.8125rem;
    color: var(--muted);
    margin: 0;
  }

  h2 {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text);
    margin: 0 0 0.75rem;
  }

  .head-right {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .refresh-btn {
    font-size: 0.8125rem;
    padding: 0.25rem 0.75rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
  }
  .refresh-btn:hover:not(:disabled) {
    background: var(--surface-hover);
  }
  .refresh-btn:disabled {
    opacity: 0.5;
    cursor: default;
  }

  /* ── Two-column grid ─────────────────────────────────────────── */
  .activity-grid {
    display: grid;
    grid-template-columns: 1fr 340px;
    gap: 1rem;
    margin-top: 1rem;
    align-items: start;
  }

  @media (max-width: 900px) {
    .activity-grid {
      grid-template-columns: 1fr;
    }
  }

  .side-panels {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  /* ── Shared card ─────────────────────────────────────────────── */
  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem;
  }

  /* ── Event Timeline ──────────────────────────────────────────── */
  .timeline {
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  .timeline-item {
    padding: 0.75rem 0;
    border-bottom: 1px solid var(--border);
  }
  .timeline-item:last-child {
    border-bottom: none;
  }

  .event-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    margin-bottom: 0.25rem;
  }

  .event-badge {
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--accent);
    background: rgba(124, 106, 247, 0.12);
    border-radius: 4px;
    padding: 0.125rem 0.375rem;
    white-space: nowrap;
  }

  .event-time {
    font-size: 0.6875rem;
    color: var(--muted);
    white-space: nowrap;
  }

  .event-summary {
    font-size: 0.8125rem;
    color: var(--text);
    line-height: 1.45;
    word-break: break-word;
  }

  /* ── Item list (censors, schedules) ──────────────────────────── */
  ul.item-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  .item-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--border);
    font-size: 0.8125rem;
  }
  .item-row:last-child {
    border-bottom: none;
  }

  .item-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 200px;
    color: var(--text);
  }

  .badge {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--accent);
    background: rgba(124, 106, 247, 0.12);
    border-radius: 4px;
    padding: 0.125rem 0.375rem;
    white-space: nowrap;
    flex-shrink: 0;
  }

  /* ── Schedule items ──────────────────────────────────────────── */
  .schedule-item {
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--border);
    font-size: 0.8125rem;
  }
  .schedule-item:last-child {
    border-bottom: none;
  }

  .schedule-task {
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .schedule-time {
    font-size: 0.75rem;
    color: var(--muted);
    margin-top: 0.125rem;
  }

  /* ── Sleep detail grid ───────────────────────────────────────── */
  .detail-grid {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.375rem 1rem;
    font-size: 0.8125rem;
    margin: 0;
  }

  dt {
    color: var(--muted);
    font-weight: 500;
  }

  dd {
    color: var(--text);
    margin: 0;
  }

  /* ── State messages ──────────────────────────────────────────── */
  .empty {
    color: var(--muted);
    font-size: 0.875rem;
    padding: 1rem 0;
    text-align: center;
    margin: 0;
  }

  .status-msg {
    color: var(--muted);
    font-size: 0.9375rem;
    padding: 3rem 2rem;
    text-align: center;
  }

  .status-msg.error {
    color: var(--red, #ef4444);
  }

  .retry-link {
    background: none;
    border: none;
    color: var(--accent);
    cursor: pointer;
    font-size: inherit;
    padding: 0;
    text-decoration: underline;
  }
</style>
