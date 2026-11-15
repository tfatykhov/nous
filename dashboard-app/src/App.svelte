<script lang="ts">
  import { currentRoute, initRouter } from '$lib/router';
  import Nav from '$lib/ui/Nav.svelte';
  import DagView from './views/DagView.svelte';
  import Heartbeat from './views/Heartbeat.svelte';
  import Observability from './views/Observability.svelte';
  import Retrieval from './views/Retrieval.svelte';
  import Work from './views/Work.svelte';
  import ModelCalls from './views/ModelCalls.svelte';
  import Subtasks from './views/Subtasks.svelte';
  import GraphView from './views/GraphView.svelte';
  import Overview from './views/Overview.svelte';
  import Browser from './views/Browser.svelte';
  import Decisions from './views/Decisions.svelte';
  import Health from './views/Health.svelte';
  import Rubric from './views/Rubric.svelte';
  import Consolidation from './views/Consolidation.svelte';
  import Identity from './views/Identity.svelte';
  import Harness from './views/Harness.svelte';
  import Intentions from './views/Intentions.svelte';
  import Autonomy from './views/Autonomy.svelte';
  import Integrations from './views/Integrations.svelte';
  import { usePoll } from '$lib/poll';
  import { attentionPoll, attentionCounts } from '$lib/stores/attention';
  import HaltSwitch from '$lib/ui/HaltSwitch.svelte';
  import { haltPoll, haltState } from '$lib/stores/halt';

  // ── Router ────────────────────────────────────────────────────
  initRouter();

  // Harness dashboard §4.6: one attention poll feeds every nav badge and the
  // mobile menu dot (the badges sit inside a closed drawer on a phone).
  usePoll(attentionPoll);
  // Goals PR 0c: one halt poll feeds the Halt control (the rail's foot, the mobile header) and the banner.
  usePoll(haltPoll);
  let needsYou = $derived($attentionCounts.questions + $attentionCounts.sends);

  // ── Mobile drawer state ───────────────────────────────────────
  let drawerOpen = $state(false);
  let innerWidth = $state(0);

  // Close drawer automatically when viewport widens past mobile breakpoint
  $effect(() => {
    if (innerWidth > 768) drawerOpen = false;
  });

  let hamburgerBtn: HTMLButtonElement | null = $state(null);
  let drawerEl: HTMLDivElement | null = $state(null);

  function openDrawer() {
    drawerOpen = true;
    // Move focus into the drawer on the next tick, after it's visible
    setTimeout(() => {
      const first = drawerEl?.querySelector<HTMLElement>(
        'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      first?.focus();
    }, 50);
  }

  function closeDrawer() {
    drawerOpen = false;
    hamburgerBtn?.focus();
  }

  function handleDrawerKeydown(e: KeyboardEvent) {
    if (!drawerOpen) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      closeDrawer();
      return;
    }
    // Focus trap: intercept Tab/Shift+Tab inside the drawer
    if (e.key === 'Tab' && drawerEl) {
      const focusable = Array.from(
        drawerEl.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey) {
        if (document.activeElement === first) {
          e.preventDefault();
          last.focus();
        }
      } else {
        if (document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }
  }

</script>

<svelte:window bind:innerWidth />

<!-- Skip-to-content for keyboard users -->
<a href="#main-content" class="skip-link">Skip to content</a>

<!-- ── Desktop sidebar ───────────────────────────────────────── -->
<!-- The navigation is the rail (class rail): the Atlantic-blue sidebar. -->
<aside class="sidebar rail" aria-label="Site navigation">
  <div class="sidebar-header">
    <span class="sidebar-title">Nous</span>
  </div>
  <Nav currentRoute={$currentRoute} navLabel="Site navigation" />
  <div class="sidebar-foot">
    <HaltSwitch />
  </div>
</aside>

<!-- ── Mobile header ─────────────────────────────────────────── -->
<header class="mobile-header rail">
  <button
    bind:this={hamburgerBtn}
    class="hamburger"
    aria-label={needsYou > 0 ? `Open navigation menu, ${needsYou} item${needsYou === 1 ? '' : 's'} need you` : 'Open navigation menu'}
    aria-expanded={drawerOpen}
    aria-controls="mobile-drawer"
    onclick={openDrawer}
  >
    <svg viewBox="0 0 20 20" fill="currentColor" width="20" height="20" aria-hidden="true">
      <path fill-rule="evenodd" d="M3 5a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 5a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 5a1 1 0 011-1h6a1 1 0 110 2H4a1 1 0 01-1-1z" clip-rule="evenodd"/>
    </svg>
    {#if needsYou > 0}<span class="hamburger-dot" aria-hidden="true"></span>{/if}
  </button>
  <span class="mobile-title">Nous</span>
  <!-- Outside the drawer, which is inert while closed: the Halt control stays in reach on a phone. -->
  <HaltSwitch compact />
</header>

<!-- ── Mobile off-canvas drawer ─────────────────────────────── -->
<!-- Overlay -->
{#if drawerOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div
    class="drawer-overlay"
    aria-hidden="true"
    onclick={closeDrawer}
  ></div>
{/if}

<div
  id="mobile-drawer"
  bind:this={drawerEl}
  class="drawer rail"
  class:drawer-open={drawerOpen}
  role="dialog"
  aria-modal="true"
  aria-label="Navigation menu"
  aria-hidden={!drawerOpen}
  inert={!drawerOpen ? true : undefined}
  onkeydown={handleDrawerKeydown}
>
  <div class="drawer-header">
    <span class="sidebar-title">Nous</span>
    <button
      class="drawer-close"
      aria-label="Close navigation menu"
      onclick={closeDrawer}
    >
      <svg viewBox="0 0 20 20" fill="currentColor" width="18" height="18" aria-hidden="true">
        <path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd"/>
      </svg>
    </button>
  </div>
  <Nav currentRoute={$currentRoute} navLabel="Mobile navigation" onnavigate={closeDrawer} />
</div>

<!-- ── Main content ───────────────────────────────────────────── -->
<!-- The page ground (class page-ground): chart paper, on every route. -->
<main id="main-content" class="main-content page-ground">
  <!-- While halted, or while the control has something to say, a card above every screen says it in full. -->
  {#if $haltState.lines.length > 0}
    <section class="halt-banner" aria-label="Background work">
      {#if $haltState.pill}
        <span class="status-pill status-pill--{$haltState.pill.tone}">{$haltState.pill.text}</span>
      {/if}
      {#each $haltState.lines as line (line)}
        <p>{line}</p>
      {/each}
      {#if $haltState.caption}
        <p class="halt-caption">{$haltState.caption}</p>
      {/if}
    </section>
  {/if}
  <!--
    Route switch: {#if} (not {#key}) so each view's component state
    (scroll, loaded data) survives tab switching.
  -->
  {#if $currentRoute === 'overview'}
    <Overview />
  {:else if $currentRoute === 'graph'}
    <GraphView />
  {:else if $currentRoute === 'browser'}
    <Browser />
  {:else if $currentRoute === 'decisions'}
    <Decisions />
  {:else if $currentRoute === 'heartbeat'}
    <Heartbeat />
  {:else if $currentRoute === 'observability'}
    <Observability />
  {:else if $currentRoute === 'retrieval'}
    <Retrieval />
  {:else if $currentRoute === 'health'}
    <Health />
  {:else if $currentRoute === 'rubric'}
    <Rubric />
  {:else if $currentRoute === 'work'}
    <Work />
  {:else if $currentRoute === 'model-calls'}
    <ModelCalls />
  {:else if $currentRoute === 'consolidation'}
    <Consolidation />
  {:else if $currentRoute === 'dag'}
    <DagView />
  {:else if $currentRoute === 'subtasks'}
    <Subtasks />
  {:else if $currentRoute === 'identity'}
    <Identity />
  {:else if $currentRoute === 'harness'}
    <Harness />
  {:else if $currentRoute === 'intentions'}
    <Intentions />
  {:else if $currentRoute === 'autonomy'}
    <Autonomy />
  {:else if $currentRoute === 'integrations'}
    <Integrations />
  {/if}
</main>

<style>
  /* ── Skip link ─────────────────────────────────────────────── */
  .skip-link {
    position: absolute;
    left: -9999px;
    top: 1rem;
    z-index: 9999;
    padding: 0.5rem 1rem;
    background: var(--accent);
    color: var(--on-accent);
    border-radius: var(--radius-sm);
    font-size: 0.875rem;
    font-weight: 600;
    text-decoration: none;
  }
  .skip-link:focus {
    left: 1rem;
  }

  /* ── Layout ────────────────────────────────────────────────── */
  :global(#app) {
    display: flex;
    min-height: 100dvh;
  }

  /* ── Desktop sidebar ───────────────────────────────────────── */
  .sidebar {
    position: fixed;
    inset: 0 auto 0 0;
    width: var(--sidebar-width);
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    z-index: 100;
  }

  .sidebar-header {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    padding: 1.25rem 1.25rem 1rem;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }

  .sidebar-title {
    font-size: var(--brand-size);
    font-weight: 700;
    color: var(--text);
    letter-spacing: -0.01em;
  }

  /* The Halt control: pinned to the foot of the rail, and still in view when the rail scrolls. */
  .sidebar-foot {
    margin-top: auto;
    position: sticky;
    bottom: 0;
    padding: 0.75rem;
    border-top: 1px solid var(--border);
    background: var(--surface);
    flex-shrink: 0;
  }

  /* ── Halt banner (a card on the paper, above the routed view) ─ */
  .halt-banner {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.375rem;
    margin-bottom: 1rem;
    padding: 1rem 1.25rem;
    background: var(--surface);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
    font-size: 0.875rem;
    color: var(--text);
  }
  .halt-caption {
    font-size: 0.75rem;
    color: var(--muted);
  }

  /* ── Mobile header (hidden on desktop) ──────────────────────── */
  .mobile-header {
    display: none;
  }

  /* ── Mobile drawer ─────────────────────────────────────────── */
  .drawer-overlay {
    display: none;
  }

  .drawer {
    display: none;
  }

  /* ── Main content ──────────────────────────────────────────── */
  .main-content {
    margin-left: var(--sidebar-width);
    flex: 1;
    /* A flex item: without this a wide table or a chart canvas would widen it, and the page, at phone width. */
    min-width: 0;
    min-height: 100dvh;
    padding: 1.5rem;
    /* Safe area for notched devices */
    padding-bottom: max(1.5rem, env(safe-area-inset-bottom));
  }

  /* ── Mobile breakpoint ─────────────────────────────────────── */
  @media (max-width: 768px) {
    .sidebar {
      display: none;
    }

    .mobile-header {
      display: flex;
      position: fixed;
      inset: 0 0 auto 0;
      height: 3.25rem;
      align-items: center;
      gap: 0.75rem;
      padding: 0 1rem;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      z-index: 200;
    }

    .mobile-title {
      font-size: var(--brand-size);
      font-weight: 700;
      color: var(--text);
    }

    .hamburger {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 2.75rem; /* a 44px tap target (final step) */
      height: 2.75rem;
      background: none;
      border: none;
      border-radius: var(--radius-sm);
      color: var(--text);
      cursor: pointer;
      padding: 0;
      position: relative;
    }
    .hamburger-dot {
      position: absolute;
      top: 0.5rem; /* the button grew 4px a side: the dot stays on the icon */
      right: 0.5rem;
      width: 0.5rem;
      height: 0.5rem;
      border-radius: 50%;
      background: var(--waiting);
      box-shadow: 0 0 0 2px var(--surface);
    }
    .hamburger:hover {
      background: var(--surface-hover);
    }

    /* Overlay */
    .drawer-overlay {
      display: block;
      position: fixed;
      inset: 0;
      background: var(--overlay-bg);
      z-index: 299;
    }

    /* Drawer panel */
    .drawer {
      display: flex;
      flex-direction: column;
      position: fixed;
      inset: 0 auto 0 0;
      width: min(280px, 85vw);
      background: var(--surface);
      border-right: 1px solid var(--border);
      z-index: 300;
      transform: translateX(-100%);
      transition: transform var(--transition);
      overflow-y: auto;
      /* Safe area for notched phones */
      padding-bottom: env(safe-area-inset-bottom);
    }

    .drawer.drawer-open {
      transform: translateX(0);
    }

    .drawer-header {
      display: flex;
      align-items: center;
      gap: 0.625rem;
      padding: 0.75rem 1rem; /* with the 44px close, the header stays 68px tall */
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }

    .drawer-close {
      margin-left: auto;
      display: flex;
      align-items: center;
      justify-content: center;
      width: 2.75rem; /* a 44px tap target (final step) */
      height: 2.75rem;
      background: none;
      border: none;
      border-radius: var(--radius-sm);
      color: var(--muted);
      cursor: pointer;
      padding: 0;
    }
    .drawer-close:hover {
      background: var(--surface-hover);
      color: var(--text);
    }

    .main-content {
      margin-left: 0;
      padding-top: calc(3.25rem + 1rem); /* mobile header height + gap */
    }
  }
</style>
