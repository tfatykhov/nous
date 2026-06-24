<script lang="ts">
  import type { Snippet } from 'svelte';
  import {
    Drawer,
    DrawerContent,
    DrawerHeader,
    DrawerTitle,
    DrawerOverlay,
    DrawerPortal,
  } from '$lib/ui/drawer';

  let {
    open = $bindable(false),
    title,
    children,
  }: {
    open?: boolean;
    title: string;
    children?: Snippet;
  } = $props();
</script>

<Drawer bind:open direction="bottom">
  <DrawerPortal>
    <DrawerOverlay />
    <DrawerContent aria-label={title} class="nous-sheet">
      <!-- Drag handle is rendered inside DrawerContent by shadcn; header adds context -->
      <DrawerHeader>
        <DrawerTitle>{title}</DrawerTitle>
      </DrawerHeader>
      <div class="bottom-sheet-body">
        {#if children}
          {@render children()}
        {/if}
      </div>
    </DrawerContent>
  </DrawerPortal>
</Drawer>

<style>
  /* shadcn's bg-popover/border tokens are unmapped in our Tailwind v4 @theme,
     so DrawerContent ships with no background — give the sheet an explicit
     solid surface + border. Unlayered, so it beats the (empty) bg-popover utility. */
  :global(.nous-sheet) {
    background: var(--surface);
    border-color: var(--border);
  }

  /* The drawer is fixed inset-x-0 (full viewport width), so on desktop its left
     half slides under the fixed sidebar and clips the detail content. Offset the
     left edge past the sidebar. Mobile (<769px) keeps full width — the sidebar is
     an overlay drawer there, not a fixed rail. */
  @media (min-width: 769px) {
    :global(.nous-sheet) {
      left: var(--sidebar-width);
    }
  }

  .bottom-sheet-body {
    padding: 0 1rem 1.5rem;
    overflow-y: auto;
    flex: 1;
  }
</style>
