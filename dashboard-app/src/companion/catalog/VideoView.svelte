<script lang="ts">
  // Basic catalog Video. classifyVideoUrl (../video.ts) decides what `url`
  // may become — a <video>, an iframe embed built from a parsed id, or
  // nothing — and this view only renders that verdict. Every src comes from
  // the verdict, never from `url` itself, and the iframe attributes are fixed
  // here, never taken from input. A non-empty rejected url shows a quiet
  // placeholder that never echoes it; an empty url renders nothing (as Image).
  import { store } from '../store.svelte';
  import { flexGrow, resolveDynamic, toDisplayString } from '../functions';
  import { classifyVideoUrl, isSafePosterUrl } from '../video';
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

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  const url = $derived(toDisplayString(resolveDynamic(comp.url, ctx)));
  const poster = $derived(toDisplayString(resolveDynamic(comp.posterUrl, ctx)));
  const source = $derived(classifyVideoUrl(url));
  // undefined, not '': Svelte omits the attribute only for null/undefined.
  const posterSrc = $derived(isSafePosterUrl(poster) ? poster.trim() : undefined);
</script>

{#if url}
  {#if source.kind === 'video'}
    <!-- An agent-supplied clip has no caption track to point at. -->
    <!-- svelte-ignore a11y_media_has_caption -->
    <video
      class="vid"
      src={source.src}
      poster={posterSrc}
      controls
      playsinline
      preload="metadata"
      aria-label="Video"
      style:flex-grow={flexGrow(comp.weight)}
    ></video>
  {:else if source.kind === 'embed'}
    <div class="frame" style:flex-grow={flexGrow(comp.weight)}>
      <iframe
        src={source.src}
        title="Video"
        sandbox="allow-scripts allow-same-origin allow-presentation allow-popups allow-popups-to-escape-sandbox"
        allow="encrypted-media; picture-in-picture; fullscreen"
        allowfullscreen
        referrerpolicy="strict-origin-when-cross-origin"
        loading="lazy"
      ></iframe>
    </div>
  {:else}
    <span class="ph">Video unavailable</span>
  {/if}
{/if}

<style>
  .vid,
  .frame {
    display: block;
    width: 100%;
    max-width: 100%;
    border-radius: var(--radius-sm);
    background: var(--surface-hover);
    overflow: hidden;
  }
  .vid {
    max-height: 340px;
  }
  .frame {
    aspect-ratio: 16 / 9;
  }
  .frame iframe {
    display: block;
    width: 100%;
    height: 100%;
    border: 0;
  }
  .ph {
    color: var(--muted);
    font-size: 0.85em;
  }
</style>
