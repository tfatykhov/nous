<script lang="ts">
  import { usePoll } from '../poll';
  import { makePollStore } from '../stores/registry';
  import { goalCommandsNow } from '../goals';

  // While a goal's tasks may run any command, every desktop screen says so above its content: one line per goal, in
  // the server's words (GET /goals/commands). It cannot be dismissed, and a read that fails keeps what was last read
  // (nothing before the first answer). Not shown on a phone, where the goal's own page says it.
  const poll = makePollStore(goalCommandsNow, 30_000);
  usePoll(poll);
  let active = $derived($poll.data?.enabled && $poll.data.allowed ? $poll.data.active : []);
</script>

{#if active.length}
  <div class="commands-banner" role="status" aria-label="Goals that may run any command">
    {#each active as goal (goal.ref)}<p>{goal.says}</p>{/each}
  </div>
{/if}

<style>
  .commands-banner {
    margin: 0 0 1rem;
    padding: 0.625rem 0.875rem;
    border: 1px solid var(--warn-border);
    border-radius: var(--card-radius-sm);
    background: var(--warn-bg);
    color: var(--warn-text);
    font-size: 0.875rem;
    font-weight: 600;
  }
  .commands-banner p { margin: 0; overflow-wrap: anywhere; }
  .commands-banner p + p { margin-top: 0.25rem; }
  @media (max-width: 768px) {
    .commands-banner { display: none; }
  }
</style>
