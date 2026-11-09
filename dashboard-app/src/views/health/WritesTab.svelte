<script lang="ts">
  import MemoryWriteHealth from '../MemoryWriteHealth.svelte';
  import Admission from '../Admission.svelte';

  // F108 Memory health, Writes tab: what the write paths stored (Memory write health), then what admission turned
  // away (Admission). Both drop their own title under the screen's; each keeps its controls and staleness badge. A
  // section heading marks where the admission page starts.
</script>

<div class="writes">
  <div><MemoryWriteHealth embedded /></div>
  <section aria-labelledby="writes-admission">
    <h2 id="writes-admission" class="section-title">Admission</h2>
    <Admission embedded />
  </section>
</div>

<style>
  .writes { display: flex; flex-direction: column; gap: 2rem; }
  .section-title {
    font-size: 1.125rem;
    font-weight: 700;
    margin: 0 0 0.5rem;
    padding-top: 1.25rem;
    border-top: 1px solid var(--border);
  }
</style>
