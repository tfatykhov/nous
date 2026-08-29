<script lang="ts">
  // F092: ChildList renderer — static id array, or a template expanded over
  // a bound data-model array. Template items get a collection Scope so bare
  // paths inside resolve relative to the item (and @index works).
  import Renderer from '../Renderer.svelte';
  import { store } from '../store.svelte';
  import { absolute, type Scope } from '../pointer';
  import { getPointer } from '../pointer';

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
</script>

{#if Array.isArray(children)}
  {#each children as childId (childId)}
    <Renderer {surfaceId} componentId={childId} {scope} {depth} {ancestors} />
  {/each}
{:else if template}
  {#each templateItems as _, i (i)}
    <Renderer
      {surfaceId}
      componentId={template.componentId}
      scope={{ base: `${templateBase}/${i}`, index: i }}
      {depth}
      {ancestors}
    />
  {/each}
{/if}
