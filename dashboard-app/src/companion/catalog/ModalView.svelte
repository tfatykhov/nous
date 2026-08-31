<script lang="ts">
  // Basic catalog Modal — native <dialog> + showModal(), which gets focus
  // trapping, inertness of the background, the ::backdrop, and Esc-to-close
  // from the platform instead of from us.
  //
  // Two things are NOT free and are wired explicitly:
  //  - backdrop click: <dialog> does not close on it. The click lands on the
  //    dialog element itself (the backdrop is its pseudo-element), so a click
  //    whose target IS the dialog means "outside the content".
  //  - the trigger: an arbitrary child — in micro-apps a NON-interactive
  //    one (Text/Icon; Buttons cannot validate there, see compose.py), so
  //    the wrapper itself carries the interactive semantics: role="button",
  //    tabindex, Enter/Space. A real <button> wrapper is not used because a
  //    hypothetical interactive trigger child would then nest interactive
  //    elements (invalid HTML); with a role'd div that worst case degrades
  //    to a redundant tab stop instead.
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
  let triggerEl = $state<HTMLElement | null>(null);
  let isOpen = $state(false);

  // role="button" names itself from its contents, which works for a Text
  // trigger — but an Icon trigger renders an aria-hidden SVG and no text,
  // leaving a focusable button with NO accessible name (codex P2 on #626).
  // Fallback label only when contents provide none, so a Text trigger's
  // own words stay the name (aria-label would override them). The name
  // derives from the DOM, so watch the DOM: a data-bound Text trigger can
  // resolve from empty to populated via updateDataModel without comp
  // changing at all (codex round-3) — a reactive-deps-only effect would
  // keep the stale fallback label. aria-label is set on the wrapper
  // itself and attributes are not observed, so applying never re-fires
  // the observer.
  //
  // The same walk decides WHOSE keyboard semantics apply (codex round-4):
  // an INTERACTIVE trigger child (a Button outside micro-apps) already
  // gives keyboard access — its own key activation synthesizes a click
  // that bubbles to the wrapper's onclick — so the wrapper stamping
  // role/tabindex on top would nest two keyboard controls, and its
  // preventDefault on the bubbled keydown would suppress the child
  // button's action. Wrapper semantics apply only to non-interactive
  // triggers (Text/Icon), which is what micro-apps can express.
  let interactiveChild = $state(false);
  $effect(() => {
    const el = triggerEl;
    if (!el) return;
    const apply = () => {
      interactiveChild =
        el.querySelector('button, a[href], input, select, textarea, [tabindex]') != null;
      if (interactiveChild || el.textContent?.trim()) el.removeAttribute('aria-label');
      else el.setAttribute('aria-label', 'Show details');
    };
    apply();
    const mo = new MutationObserver(apply);
    mo.observe(el, { childList: true, subtree: true, characterData: true });
    return () => mo.disconnect();
  });

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

  function onTriggerKey(event: KeyboardEvent) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      open();
    }
  }
</script>

<!--
  The wrapper needs a real box (inline-flex, not display:contents) so it can
  take focus and paint a focus ring — a focusable no-box element draws no
  outline. Cost: a trigger child's own flex weight no longer reaches the
  parent flex container; keyboard reachability wins over that nicety
  (codex P2 on #626: Text/Icon triggers were pointer-only, making modal-only
  detail unreachable without a mouse).
-->
<!-- The compiler can't see the conditional role: when the attributes are
     stamped the element IS interactive, and when they aren't the child is. -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div
  bind:this={triggerEl}
  class="trigger"
  role={interactiveChild ? undefined : 'button'}
  tabindex={interactiveChild ? undefined : 0}
  aria-haspopup={interactiveChild ? undefined : 'dialog'}
  onclick={open}
  onkeydown={interactiveChild ? undefined : onTriggerKey}
>
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
    display: inline-flex;
    cursor: pointer;
    border-radius: var(--radius-sm);
  }
  .trigger:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
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
    background: var(--scrim);
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
