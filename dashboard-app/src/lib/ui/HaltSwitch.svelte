<script lang="ts">
  // Goals PR 0c: the Halt and Resume control for background work, always in view. Two placements, both on the rail:
  // the foot of the desktop sidebar (the state, then every button) and, with `compact`, the mobile header beside the
  // menu button (one control: the button that can be pressed, or the state word while none can). The words are in
  // lib/halt.ts; what they mean in full is said by the banner above the routed view (App.svelte).
  import { Button } from '$lib/ui/button';
  import { haltNow, haltState, resumeNow } from '$lib/stores/halt';
  import type { HaltButton } from '$lib/halt';

  let { compact = false }: { compact?: boolean } = $props();

  let shown = $derived(
    compact ? $haltState.buttons.filter((b, i) => i === 0 && !b.disabled) : $haltState.buttons,
  );
  let pill = $derived(!compact || shown.length === 0 ? $haltState.pill : null);

  function press(b: HaltButton) {
    void (b.kind === 'halt' ? haltNow() : resumeNow());
  }
</script>

<div class="halt-switch" class:halt-switch--compact={compact} role="group" aria-label="Background work">
  {#if pill}
    <span class="status-pill status-pill--{pill.tone}">{pill.text}</span>
  {/if}
  {#each shown as b (b.kind)}
    <!-- Halt is the destructive pill (the error tint). Resume is the rail's filled action: the paper pill with ink,
         as the current nav item (an Atlantic fill would vanish on the Atlantic rail). -->
    <Button
      variant={b.kind === 'halt' ? 'destructive' : 'default'}
      size="lg"
      class={[
        'h-auto py-1 whitespace-normal leading-tight',
        compact ? 'min-h-11 shrink px-4' : 'min-h-9 w-full px-3',
        b.kind === 'resume' && 'bg-[var(--accent-glow)] text-[var(--accent-text)] hover:bg-[var(--accent-glow)]/90',
      ].filter(Boolean).join(' ')}
      disabled={b.disabled}
      onclick={() => press(b)}
    >{b.label}</Button>
  {/each}
</div>

<style>
  .halt-switch {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.5rem;
  }

  .halt-switch--compact {
    flex-direction: row;
    align-items: center;
    margin-left: auto;
    min-width: 0;
  }
</style>
