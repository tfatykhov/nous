<script lang="ts">
  // Basic catalog Image. `fit` maps 1:1 onto CSS object-fit (only scaleDown
  // needs renaming); `variant` is a size hint, mapped to a box per the
  // catalog's enum. `description` is accessibility text — when the agent
  // omits it the image is treated as decorative (alt="") rather than being
  // given a junk label.
  import { store } from '../store.svelte';
  import { flexGrow, resolveDynamic, toDisplayString } from '../functions';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  let {
    surfaceId,
    comp,
    scope = null,
  }: {
    surfaceId: string;
    comp: A2uiComponent;
    scope?: Scope | null;
    depth?: number;
    ancestors?: readonly string[];
  } = $props();

  const OBJECT_FIT: Record<string, string> = {
    contain: 'contain',
    cover: 'cover',
    fill: 'fill',
    none: 'none',
    scaleDown: 'scale-down',
  };

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  const url = $derived(toDisplayString(resolveDynamic(comp.url, ctx)));
  const description = $derived(toDisplayString(resolveDynamic(comp.description, ctx)));
  const fit = $derived(OBJECT_FIT[comp.fit as string] ?? 'fill');
  const variant = $derived(typeof comp.variant === 'string' ? comp.variant : 'mediumFeature');
</script>

{#if url}
  <img
    class="img {variant}"
    src={url}
    alt={description}
    style:object-fit={fit}
    style:flex-grow={flexGrow(comp.weight)}
    loading="lazy"
  />
{/if}

<style>
  .img {
    display: block;
    max-width: 100%;
    border-radius: var(--radius-sm);
    background: var(--surface-hover);
  }
  .img.icon {
    width: 24px;
    height: 24px;
  }
  .img.avatar {
    width: 40px;
    height: 40px;
    border-radius: 999px;
  }
  .img.smallFeature {
    width: 96px;
    height: 96px;
  }
  .img.mediumFeature {
    width: 100%;
    max-height: 220px;
  }
  .img.largeFeature {
    width: 100%;
    max-height: 340px;
  }
  .img.header {
    width: 100%;
    height: 160px;
    border-radius: var(--radius);
  }
</style>
