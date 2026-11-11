import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { styleBlocks } from './lib/testing/css';

// Redesign PR 4 and PR 5: a converted screen must not move another screen through a global rule.
// - Identity's three :global(.detail-grid | .dl | .mono) blocks style other screens too (Consolidation, Heartbeat,
//   Ledger, DagView, GraphView and the model-call views use the global .mono and .dl). They stay byte for byte as on
//   origin/main (f4d735a1), so `font-family: monospace` there never becomes var(--font-mono) for everyone.
// - The other six files add no :global( rule, except the paper-only scoped form the redesign's titles use,
//   `:global(html[data-look="paper"] …) x`, which matches nothing under the dark look and only the file's own element.

const SRC = __dirname;
const IDENTITY_GLOBALS = `  :global(.detail-grid) {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.25rem 0.875rem;
    font-size: 0.8125rem;
    padding: 0.5rem 0;
  }

  :global(.dl) {
    color: var(--muted);
    font-weight: 600;
    white-space: nowrap;
    align-self: start;
  }

  :global(.mono) {
    font-family: monospace;
    font-size: 0.75rem;
  }`;

const OTHERS = ['views/Decisions.svelte', 'views/Rubric.svelte', 'views/Autonomy.svelte', 'views/Intentions.svelte',
  'views/Harness.svelte', 'views/Observability.svelte'];

/** Each `:global(` that is not the paper-only scoped form, and the line it is on. */
function leaks(css: string): string[] {
  return css.split('\n').filter((l) => /:global\(/.test(l) && !/^\s*:global\(html\[data-look="paper"\](?:\s+\[data-ground="paper"\])?\)\s+[^\s{,]/.test(l)).map((l) => l.trim());
}

describe('no global rule leaks from the converted dense screens', () => {
  it('Identity\'s three global blocks are byte for byte as on origin/main', () => {
    const src = readFileSync(join(SRC, 'views/Identity.svelte'), 'utf-8').replace(/\r\n/g, '\n');
    expect(src).toContain(IDENTITY_GLOBALS);
    expect(src.match(/:global\(/g)?.length, 'no other :global( in Identity').toBe(3);
  });

  it('the scoped paper form is allowed, any other :global( is a leak', () => {
    expect(leaks(':global(html[data-look="paper"] [data-ground="paper"]) h1 { font-size: 30px; }')).toEqual([]);
    expect(leaks(':global(html[data-look="paper"]) h1 { font-size: 30px; }')).toEqual([]);
    expect(leaks(':global(.mono) { font-family: monospace; }')).toEqual([':global(.mono) { font-family: monospace; }']);
    expect(leaks(':global(html[data-look="paper"] .x) { color: red; }')).toHaveLength(1); // the whole selector is global
  });

  it.each(OTHERS)('%s adds no global rule', (file) => {
    expect(leaks(styleBlocks(readFileSync(join(SRC, file), 'utf-8')))).toEqual([]);
  });
});
