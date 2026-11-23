<script lang="ts">
  import { onMount } from 'svelte';
  import { apiGet } from '../lib/api';
  import { Badge } from '../lib/ui/badge';
  import { Button } from '../lib/ui/button';
  import {
    ABOUT_PATH, CARDS, DB_NOT_READ, FAILED, LABELS, LINKS, LOADING, MODELS_HEADING, MODEL_ROWS, NEW_TAB,
    NOT_AVAILABLE, SUBTITLE, TRY_AGAIN, buildPieces, databaseUnread, runningSince, schemaLine, type AboutAnswer,
  } from '../lib/about';

  // What this Nous is and where it runs. The page reads once on mount, and again only when the owner asks after a
  // failed read.
  let answer = $state<AboutAnswer | null>(null);
  let loading = $state(true);
  let failed = $state(false);

  async function load() {
    loading = true;
    failed = false;
    try {
      answer = await apiGet<AboutAnswer>(ABOUT_PATH);
    } catch {
      failed = true;
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    void load();
  });

  const orMissing = (value: string | null) => value ?? NOT_AVAILABLE;
</script>

{#snippet row(label: string, value: string, mono = false)}
  <div class="row">
    <dt>{label}</dt>
    <dd class:mono>{value}</dd>
  </div>
{/snippet}

<div class="about">
  <header class="head">
    <h1 class="page-h1">About</h1>
    <p class="subtitle">{SUBTITLE}</p>
  </header>

  {#if answer}
    {@const a = answer}
    {@const pieces = buildPieces(a.build)}
    <section class="card" aria-labelledby="about-nous">
      <h2 id="about-nous">{CARDS.nous}</h2>
      <dl>
        {@render row(LABELS.version, a.version)}
        {#if pieces}
          <div class="row">
            <dt>{LABELS.build}</dt>
            <dd>{#each pieces as p, i (i)}{#if p.mono}<span class="mono">{p.text}</span>{:else}{p.text}{/if}{/each}</dd>
          </div>
        {/if}
        {@render row(LABELS.runningSince, runningSince(a.started_at, a.uptime_seconds))}
      </dl>
    </section>

    <section class="card" aria-labelledby="about-agent">
      <h2 id="about-agent">{CARDS.agent}</h2>
      <dl>
        {@render row(LABELS.name, a.agent.name)}
        {@render row(LABELS.id, a.agent.id, true)}
      </dl>
      <h3>{MODELS_HEADING}</h3>
      <dl>
        {#each MODEL_ROWS as [key, label] (key)}
          {@render row(label, a.models[key], true)}
        {/each}
      </dl>
    </section>

    <section class="card" aria-labelledby="about-running-on">
      <h2 id="about-running-on">{CARDS.runningOn}</h2>
      {#if databaseUnread(a)}
        <p class="unread"><Badge variant="warn">{DB_NOT_READ}</Badge></p>
      {/if}
      <dl>
        {@render row(LABELS.python, a.python)}
        {@render row(LABELS.postgres, orMissing(a.database.server))}
        {@render row(LABELS.pgvector, orMissing(a.database.pgvector))}
        {@render row(LABELS.schema, schemaLine(a.schema))}
      </dl>
    </section>

    <section class="card" aria-labelledby="about-links">
      <h2 id="about-links">{CARDS.links}</h2>
      <ul class="links">
        {#each LINKS as link (link.href)}
          <li>
            <a href={link.href} target="_blank" rel="noopener noreferrer">{link.label}</a>
            <span class="note">{NEW_TAB}</span>
          </li>
        {/each}
      </ul>
    </section>
  {:else if loading}
    <p class="state" role="status">{LOADING}</p>
  {:else if failed}
    <div class="state failed" role="alert">
      <p>{FAILED}</p>
      <Button variant="outline" class="max-md:min-h-11 max-md:px-4" onclick={() => void load()}>{TRY_AGAIN}</Button>
    </div>
  {/if}
</div>

<style>
  .about { display: flex; flex-direction: column; gap: 1rem; max-width: 44rem; }
  .head { margin-bottom: 0.25rem; }
  .subtitle { margin: 0; font-size: 0.8125rem; color: var(--muted); }

  .card {
    padding: 1rem 1.25rem;
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
  }
  h2 { margin: 0 0 0.75rem; font-size: 1.05rem; font-weight: 600; color: var(--text); }
  h3 { margin: 1rem 0 0.5rem; font-size: 0.8125rem; font-weight: 600; color: var(--muted); }

  dl { margin: 0; display: flex; flex-direction: column; }
  .row {
    display: grid;
    grid-template-columns: 10rem minmax(0, 1fr);
    gap: 1rem;
    padding: 0.5rem 0;
    border-top: 1px solid var(--border);
  }
  .row:first-child { border-top: 0; }
  dt { font-size: 0.8125rem; color: var(--muted); }
  dd { margin: 0; font-size: 0.875rem; color: var(--text); overflow-wrap: anywhere; }
  .mono { font-family: var(--font-mono); font-size: 0.875rem; }

  .unread { margin: 0 0 0.5rem; }

  .links { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 0.25rem; }
  .links li { display: flex; flex-wrap: wrap; align-items: center; gap: 0 0.75rem; }
  .links a { display: inline-flex; align-items: center; min-height: 2rem; color: var(--accent-text); font-weight: 500; }
  .note { font-size: 0.8125rem; color: var(--muted); }

  .state { margin: 0; color: var(--muted); font-size: 0.9375rem; }
  .failed { display: flex; flex-direction: column; align-items: flex-start; gap: 0.75rem; }
  .failed p { margin: 0; color: var(--text); }

  @media (max-width: 640px) {
    .row { grid-template-columns: minmax(0, 1fr); gap: 0.125rem; }
  }
  @media (max-width: 768px) {
    .links a { min-height: 2.75rem; }
  }
</style>
