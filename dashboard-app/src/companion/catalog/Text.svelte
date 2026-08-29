<script lang="ts">
  // Basic catalog Text, rendered through the markdown-lite AST (headings,
  // paragraphs, lists, fenced code, and inline strong/em/code/link). The
  // parser — not this component — enforces the link scheme allowlist, so
  // nothing here has to be trusted with a raw href. No {@html} anywhere.
  import MarkdownInline from '../MarkdownInline.svelte';
  import { store } from '../store.svelte';
  import { flexGrow, resolveDynamic, toDisplayString } from '../functions';
  import { parseMarkdown } from '../markdown';
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
  const raw = $derived(toDisplayString(resolveDynamic(comp.text, ctx)));
  const blocks = $derived(parseMarkdown(raw));
  const variant = $derived(typeof comp.variant === 'string' ? comp.variant : 'body');
</script>

<div class="md" class:caption={variant === 'caption'} style:flex-grow={flexGrow(comp.weight)}>
  {#each blocks as block, i (i)}
    {#if block.type === 'heading'}
      {#if block.level === 1}
        <h1><MarkdownInline nodes={block.children} /></h1>
      {:else if block.level === 2}
        <h2><MarkdownInline nodes={block.children} /></h2>
      {:else if block.level === 3}
        <h3><MarkdownInline nodes={block.children} /></h3>
      {:else}
        <h4><MarkdownInline nodes={block.children} /></h4>
      {/if}
    {:else if block.type === 'paragraph'}
      <p><MarkdownInline nodes={block.children} /></p>
    {:else if block.type === 'list'}
      {#if block.ordered}
        <ol>
          {#each block.items as item, j (j)}
            <li><MarkdownInline nodes={item} /></li>
          {/each}
        </ol>
      {:else}
        <ul>
          {#each block.items as item, j (j)}
            <li><MarkdownInline nodes={item} /></li>
          {/each}
        </ul>
      {/if}
    {:else if block.type === 'codeblock'}
      <pre><code>{block.value}</code></pre>
    {/if}
  {/each}
</div>

<style>
  .md {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    min-width: 0;
  }
  .md.caption {
    color: var(--muted);
    font-size: 0.82rem;
  }
  h1,
  h2,
  h3,
  h4 {
    margin: 0;
    line-height: 1.3;
  }
  h1 {
    font-size: 1.35rem;
  }
  h2 {
    font-size: 1.15rem;
  }
  h3 {
    font-size: 1rem;
  }
  h4 {
    font-size: 0.92rem;
    color: var(--muted);
  }
  p {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  ul,
  ol {
    margin: 0;
    padding-left: 1.2rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  li {
    overflow-wrap: anywhere;
  }
  pre {
    margin: 0;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.6rem 0.7rem;
    overflow-x: auto;
  }
  pre code {
    font-family: var(--font-mono);
    font-size: 0.85rem;
  }
</style>
