<script lang="ts">
  import type { A2uiComponent } from '../store.svelte';

  let { comp }: { comp: A2uiComponent; surfaceId?: string } = $props();
</script>

<hr class="divider" class:vertical={comp.axis === 'vertical'} />

<style>
  .divider {
    border: none;
    border-top: 1px solid var(--border);
    margin: 0.25rem 0;
    width: 100%;
  }
  .divider.vertical {
    border-top: none;
    border-left: 1px solid var(--border);
    height: 1.5rem;
    width: 0;
    margin: 0 0.25rem;
  }
</style>
