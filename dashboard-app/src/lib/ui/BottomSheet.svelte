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
    <DrawerContent aria-label={title}>
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
  .bottom-sheet-body {
    padding: 0 1rem 1.5rem;
    overflow-y: auto;
    flex: 1;
  }
</style>
