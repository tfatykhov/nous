<script lang="ts">
  import MemoryWriteHealth from '../MemoryWriteHealth.svelte';
  import Admission from '../Admission.svelte';

  // F108 Memory health, Writes tab: what the write paths stored (Memory write health), then what admission turned
  // away (Admission). Both drop their own title under the screen's; each keeps its controls and staleness badge.
</script>

<div class="writes">
  <div><MemoryWriteHealth embedded /></div>
  <div><Admission embedded /></div>
</div>

<style>
  .writes { display: flex; flex-direction: column; gap: 2rem; }
</style>
