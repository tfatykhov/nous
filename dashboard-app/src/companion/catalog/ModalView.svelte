<script lang="ts">
  // Basic catalog Modal — native <dialog> + showModal(), which gets focus
  // trapping, inertness of the background, the ::backdrop, and Esc-to-close
  // from the platform instead of from us.
  //
  // Two things are NOT free and are wired explicitly:
  //  - backdrop click: <dialog> does not close on it. The click lands on the
  //    dialog element itself (the backdrop is its pseudo-element), so a click
  //    whose target IS the dialog means "outside the content".
  //  - the trigger: it is an arbitrary child, usually a Button, so it cannot
  //    be wrapped in another <button> (nested interactive elements are
  //    invalid HTML). A plain wrapper catches the click as it bubbles.
  //
  // Open state is renderer-local: the catalog gives Modal no bound value.
  import Renderer from '../Renderer.svelte';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  let {
    surfaceId,
    comp,
    scope = null,
    depth = 0,
    ancestors = [],
  }: {
    surfaceId: string;
    comp: A2uiComponent;
    scope?: Scope | null;
    depth?: number;
    ancestors?: readonly string[];
  } = $props();

  let dialog = $state<HTMLDialogElement | null>(null);
  let isOpen = $state(false);

  function open() {
    isOpen = true;
    // Optional call: jsdom implements <dialog> without showModal.
    dialog?.showModal?.();
  }

  function close() {
    dialog?.close?.();
    isOpen = false;
  }

  function onDialogClick(event: MouseEvent) {
    if (event.target === dialog) close();
  }
</script>

<!--
  No style:flex-grow here, unlike every other adapter: `display: contents`
  means this wrapper generates no box, so a flex-grow on it would be dead
  code. The trigger child is the real flex item and honours its own weight.
-->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="trigger" onclick={open}>
  {#if typeof comp.trigger === 'string'}
    <Renderer {surfaceId} componentId={comp.trigger} {scope} {depth} {ancestors} />
  {/if}
</div>

<dialog bind:this={dialog} onclick={onDialogClick} onclose={() => (isOpen = false)}>
  <div class="content">
    <button type="button" class="close" aria-label="Close dialog" onclick={close}>✕</button>
    {#if isOpen && typeof comp.content === 'string'}
      <Renderer {surfaceId} componentId={comp.content} {scope} {depth} {ancestors} />
    {/if}
  </div>
</dialog>

<style>
  .trigger {
    display: contents;
  }
  dialog {
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 0;
    max-width: min(560px, calc(100vw - 2rem));
    width: 100%;
  }
  dialog::backdrop {
    background: rgba(10, 10, 15, 0.72);
  }
  .content {
    position: relative;
    padding: 1.2rem 1.1rem 1.1rem;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }
  .close {
    position: absolute;
    top: 0.5rem;
    right: 0.6rem;
    font: inherit;
    color: var(--muted);
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.15rem 0.35rem;
    border-radius: var(--radius-sm);
  }
  .close:hover {
    color: var(--text);
    background: var(--surface-hover);
  }
</style>
