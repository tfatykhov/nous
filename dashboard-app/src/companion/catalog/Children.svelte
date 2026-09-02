<script lang="ts">
  // F092: ChildList renderer — static id array, or a template expanded over
  // a bound data-model array. Template items get a collection Scope so bare
  // paths inside resolve relative to the item (and @index works).
  import Renderer from '../Renderer.svelte';
  import { store } from '../store.svelte';
  import { absolute, type Scope } from '../pointer';
  import { getPointer } from '../pointer';
  import { isTruncationMarker, omittedNote } from '../functions';

  let {
    surfaceId,
    children,
    scope = null,
    depth,
    ancestors,
  }: {
    surfaceId: string;
    children: unknown;
    scope?: Scope | null;
    depth: number;
    ancestors: readonly string[];
  } = $props();

  const surface = $derived(store.surfaces[surfaceId]);
  const template = $derived(
    typeof children === 'object' && children !== null && !Array.isArray(children)
      ? (children as { componentId: string; path: string })
      : null,
  );
  const templateBase = $derived(template ? absolute(template.path, scope) : '');
  const templateItems = $derived.by(() => {
    if (!template || !surface) return [];
    const value = getPointer(surface.dataModel, templateBase);
    return Array.isArray(value) ? value : [];
  });
  // The server's char-budget marker is the LAST array entry, not a record:
  // expanding the template over it would render a blank card and hide the
  // omitted count (codex P2 on #630). Item indices are preserved for the
  // real records so their scopes still point at the right array slot.
  const omitted = $derived.by(() => {
    let n: number | null = null;
    for (const item of templateItems) {
      if (isTruncationMarker(item)) {
        n = (n ?? 0) + (typeof item.omitted === 'number' && item.omitted > 0 ? item.omitted : 0);
      }
    }
    return n;
  });
</script>

{#if Array.isArray(children)}
  {#each children as childId (childId)}
    <Renderer {surfaceId} componentId={childId} {scope} {depth} {ancestors} />
  {/each}
{:else if template}
  {#each templateItems as item, i (i)}
    {#if !isTruncationMarker(item)}
      <Renderer
        {surfaceId}
        componentId={template.componentId}
        scope={{ base: `${templateBase}/${i}`, index: i }}
        {depth}
        {ancestors}
      />
    {/if}
  {/each}
  {#if omitted !== null}
    <div class="omitted">{omittedNote(omitted)}</div>
  {/if}
{/if}

<style>
  .omitted {
    color: var(--muted);
    font-size: 0.78rem;
    font-style: italic;
  }
</style>
