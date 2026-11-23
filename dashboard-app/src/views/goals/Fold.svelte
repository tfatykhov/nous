<script lang="ts">
  import type { Snippet } from 'svelte';
  import { readFolds, writeFold, type FoldId } from '../../lib/goals';

  // One folding section of a goal's page. Its heading is one button: the title, a short summary in words when there
  // is one, and Show or Hide. The panel stays in the page while hidden, so what it holds keeps its state and its
  // reads. The viewer's choice is kept in this browser for every goal; with no storage the default holds and the
  // section still folds. Ids: `goal-f.<ref>.<id>` (the button), `goal-l` (its title), `goal-b` (the panel).
  let {
    id,
    refOf,
    title,
    summary = '',
    defaultOpen,
    children,
  }: {
    id: FoldId;
    refOf: string;
    title: string;
    summary?: string;
    defaultOpen: boolean;
    children: Snippet;
  } = $props();

  const initial = () => readFolds()[id] ?? defaultOpen;
  let open = $state(initial());

  function toggle() {
    open = !open;
    writeFold(id, open);
  }
</script>

<section class="card" aria-labelledby="goal-l.{refOf}.{id}">
  <h3>
    <button type="button" class="toggle" id="goal-f.{refOf}.{id}" aria-expanded={open} aria-controls="goal-b.{refOf}.{id}"
      onclick={toggle}>
      <span class="title" id="goal-l.{refOf}.{id}">{title}</span>
      {#if summary}<span class="summary">{summary}</span>{/if}
      <span class="act">{open ? 'Hide' : 'Show'}</span>
    </button>
  </h3>
  <div class="panel" id="goal-b.{refOf}.{id}" hidden={!open}>
    {@render children()}
  </div>
</section>

<style>
  .card { background: var(--surface); border: var(--card-border); border-radius: var(--card-radius); box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem; margin-bottom: 1.25rem; min-width: 0; }
  h3 { font-size: 0.9375rem; font-weight: 650; margin: 0; }
  /* The whole heading row is the button: the title, the summary beside it, and the word at the right. Its padding gives
     the focus outline room around the words; the negative margin keeps the words where they were. */
  .toggle { display: flex; align-items: baseline; flex-wrap: wrap; gap: 0.25rem 0.75rem; width: calc(100% + 1rem);
    padding: 0.25rem 0.5rem; margin: -0.25rem -0.5rem; border: 0;
    background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; border-radius: var(--radius-sm); }
  .summary { font-size: 0.8125rem; font-weight: 400; color: var(--muted); }
  .act { margin-left: auto; font-size: 0.8125rem; font-weight: 500; color: var(--muted); transition: color var(--transition); }
  .toggle:hover .act { color: var(--text); }
  .toggle:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .panel { margin-top: 0.5rem; }
  @media (prefers-reduced-motion: reduce) {
    .act { transition: none; }
  }
  @media (max-width: 768px) {
    .toggle { min-height: 2.75rem; align-items: center; }
  }
</style>
