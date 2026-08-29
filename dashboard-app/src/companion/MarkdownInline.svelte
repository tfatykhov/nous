<script lang="ts">
  // F092: recursive renderer for the markdown-lite inline AST. Every node
  // becomes an ordinary Svelte element — there is no {@html} on this path, so
  // agent-authored text can never introduce markup. The href on a link node
  // was already scheme-checked by the parser (http/https/mailto only), which
  // is why nothing here has to second-guess it.
  //
  // Recursion is via self-import (svelte:self is deprecated in Svelte 5).
  //
  // The markup below is deliberately packed onto one line per branch with NO
  // whitespace between the block tags: Text.svelte renders paragraphs with
  // `white-space: pre-wrap`, so a newline-and-indent between two inline nodes
  // would render as a VISIBLE gap in the middle of a sentence.
  import Self from './MarkdownInline.svelte';
  import type { InlineNode } from './markdown';

  let { nodes }: { nodes: InlineNode[] } = $props();
</script>

<!-- prettier-ignore -->
{#each nodes as node, i (i)}{#if node.type === 'text'}{node.value}{:else if node.type === 'strong'}<strong><Self nodes={node.children} /></strong>{:else if node.type === 'em'}<em><Self nodes={node.children} /></em>{:else if node.type === 'code'}<code>{node.value}</code>{:else if node.type === 'link'}<a href={node.href} target="_blank" rel="noopener noreferrer"><Self nodes={node.children} /></a>{/if}{/each}

<style>
  code {
    font-family: var(--font-mono);
    font-size: 0.9em;
    background: var(--surface-hover);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.05em 0.3em;
  }
  a {
    color: var(--accent);
  }
  a:hover {
    text-decoration: underline;
  }
</style>
