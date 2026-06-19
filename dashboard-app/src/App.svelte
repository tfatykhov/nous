<script lang="ts">
  import { currentRoute, initRouter } from '$lib/router';
  import Nav from '$lib/ui/Nav.svelte';
  import Placeholder from '$lib/ui/Placeholder.svelte';
  import Cache from './views/Cache.svelte';
  import Subtasks from './views/Subtasks.svelte';

  // ── Router ────────────────────────────────────────────────────
  initRouter();

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
<aside class="sidebar" aria-label="Site navigation">
  <div class="sidebar-header">
    <span class="sidebar-logo" aria-hidden="true">&#x1D6B9;</span>
    <span class="sidebar-title">Nous</span>
  </div>
  <Nav currentRoute={$currentRoute} navLabel="Site navigation" />
</aside>

<!-- ── Mobile header ─────────────────────────────────────────── -->
<header class="mobile-header">
  <button
    bind:this={hamburgerBtn}
    class="hamburger"
    aria-label="Open navigation menu"
    aria-expanded={drawerOpen}
    aria-controls="mobile-drawer"
    onclick={openDrawer}
  >
    <svg viewBox="0 0 20 20" fill="currentColor" width="20" height="20" aria-hidden="true">
      <path fill-rule="evenodd" d="M3 5a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 5a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 5a1 1 0 011-1h6a1 1 0 110 2H4a1 1 0 01-1-1z" clip-rule="evenodd"/>
    </svg>
  </button>
  <span class="mobile-title">Nous</span>
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
  class="drawer"
  class:drawer-open={drawerOpen}
  role="dialog"
  aria-modal="true"
  aria-label="Navigation menu"
  aria-hidden={!drawerOpen}
  inert={!drawerOpen ? true : undefined}
  onkeydown={handleDrawerKeydown}
>
  <div class="drawer-header">
    <span class="sidebar-logo" aria-hidden="true">&#x1D6B9;</span>
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
<main id="main-content" class="main-content">
  <!--
    Route switch: {#if} (not {#key}) so each view's component state
    (scroll, loaded data) survives tab switching.
    When a view is migrated, replace <Placeholder> with the real component:
      {#if $currentRoute === 'overview'}<OverviewView />{/if}
  -->
  {#if $currentRoute === 'overview'}
    <Placeholder route={$currentRoute} />
  {:else if $currentRoute === 'graph'}
    <Placeholder route={$currentRoute} />
  {:else if $currentRoute === 'browser'}
    <Placeholder route={$currentRoute} />
  {:else if $currentRoute === 'decisions'}
    <Placeholder route={$currentRoute} />
  {:else if $currentRoute === 'activity'}
    <Placeholder route={$currentRoute} />
  {:else if $currentRoute === 'heartbeat'}
    <Placeholder route={$currentRoute} />
  {:else if $currentRoute === 'observability'}
    <Placeholder route={$currentRoute} />
  {:else if $currentRoute === 'health'}
    <Placeholder route={$currentRoute} />
  {:else if $currentRoute === 'admission'}
    <Placeholder route={$currentRoute} />
  {:else if $currentRoute === 'rubric'}
    <Placeholder route={$currentRoute} />
  {:else if $currentRoute === 'execution'}
    <Placeholder route={$currentRoute} />
  {:else if $currentRoute === 'cache'}
    <Cache />
  {:else if $currentRoute === 'density'}
    <Placeholder route={$currentRoute} />
  {:else if $currentRoute === 'dag'}
    <Placeholder route={$currentRoute} />
  {:else if $currentRoute === 'subtasks'}
    <Subtasks />
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
    color: #fff;
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

  .sidebar-logo {
    font-size: 1.375rem;
    line-height: 1;
    color: var(--accent);
  }

  .sidebar-title {
    font-size: 1rem;
    font-weight: 700;
    color: var(--text);
    letter-spacing: -0.01em;
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
      font-size: 1rem;
      font-weight: 700;
      color: var(--text);
    }

    .hamburger {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 2.25rem;
      height: 2.25rem;
      background: none;
      border: none;
      border-radius: var(--radius-sm);
      color: var(--text);
      cursor: pointer;
      padding: 0;
    }
    .hamburger:hover {
      background: var(--surface-hover);
    }

    /* Overlay */
    .drawer-overlay {
      display: block;
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.6);
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
      transition: transform 0.25s ease;
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
      padding: 1.125rem 1rem;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }

    .drawer-close {
      margin-left: auto;
      display: flex;
      align-items: center;
      justify-content: center;
      width: 2rem;
      height: 2rem;
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
