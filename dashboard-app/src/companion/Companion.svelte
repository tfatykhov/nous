<script lang="ts">
  // F092: companion shell — surface feed + focused deep-link view.
  //
  // Routing is ~25 own lines, NOT a reuse of src/lib/router.ts: that router
  // validates against a closed ROUTES union and would send arbitrary
  // surface ids to 'overview'. The `initialized` guard is copied from it —
  // it stops hashchange listeners stacking across tests and HMR.
  import { onMount } from 'svelte';
  import Renderer from './Renderer.svelte';
  import { store } from './store.svelte';
  import { transport } from './transport';

  type View = { view: 'feed' } | { view: 'surface'; id: string };

  function parseHash(): View {
    const h = location.hash;
    const m = /^#\/s\/(.+)$/.exec(h);
    if (m) return { view: 'surface', id: decodeURIComponent(m[1]) };
    return { view: 'feed' };
  }

  let route = $state<View>(parseHash());
  let initialized = false;

  onMount(() => {
    route = parseHash();
    if (!initialized) {
      initialized = true;
      window.addEventListener('hashchange', () => {
        route = parseHash();
      });
    }
    void transport.connect();
    return () => transport.stop();
  });

  const feed = $derived(store.ordered());
  const focused = $derived(route.view === 'surface' ? store.surfaces[route.id] : null);
</script>

<div class="shell">
  <header>
    <a class="brand" href="#/">Nous <span>Companion</span></a>
    <span class="conn {store.connection}">{store.connection}</span>
  </header>

  <main>
    {#if route.view === 'surface'}
      {#if focused}
        <section class="surface" aria-label={focused.surfaceId}>
          <Renderer surfaceId={focused.surfaceId} componentId="root" />
        </section>
      {:else}
        <p class="empty">
          Surface not found — it may have resolved or expired.
          <a href="#/">Back to feed</a>
        </p>
      {/if}
    {:else if feed.length === 0}
      <p class="empty">
        Nothing needs you right now. Surfaces Nous pushes — escalations,
        action reviews, triage — appear here.
      </p>
    {:else}
      {#each feed as surface (surface.surfaceId)}
        <section class="surface" aria-label={surface.surfaceId}>
          <a class="permalink" href={'#/s/' + encodeURIComponent(surface.surfaceId)}>⧉</a>
          <Renderer surfaceId={surface.surfaceId} componentId="root" />
        </section>
      {/each}
    {/if}
  </main>
</div>

<style>
  .shell {
    max-width: 720px;
    margin: 0 auto;
    padding: 1rem 1rem calc(2rem + env(safe-area-inset-bottom));
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.25rem 0;
  }
  .brand {
    color: var(--text);
    text-decoration: none;
    font-weight: 600;
    font-size: 1.05rem;
  }
  .brand span {
    color: var(--accent);
  }
  .conn {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.15rem 0.6rem;
    border-radius: 999px;
    border: 1px solid var(--border);
    color: var(--muted);
  }
  .conn.live {
    color: var(--green);
    border-color: var(--green);
  }
  .conn.error,
  .conn.resyncing {
    color: var(--yellow);
    border-color: var(--yellow);
  }
  main {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  .surface {
    position: relative;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.1rem;
  }
  .permalink {
    position: absolute;
    top: 0.6rem;
    right: 0.8rem;
    color: var(--muted);
    text-decoration: none;
    font-size: 0.85rem;
  }
  .permalink:hover {
    color: var(--accent);
  }
  .empty {
    color: var(--muted);
    text-align: center;
    padding: 3rem 1rem;
  }
  .empty a {
    color: var(--accent);
  }
</style>
