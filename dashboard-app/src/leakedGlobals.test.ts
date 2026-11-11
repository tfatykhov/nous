import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { styleBlocks } from './lib/testing/css';

// Redesign PR 4 and PR 5: a converted screen must not move another screen through a global rule.
// - Identity's three :global(.detail-grid | .dl | .mono) blocks style other screens too (Consolidation, Heartbeat,
//   Ledger, DagView, GraphView and the model-call views use the global .mono and .dl). They stay byte for byte as on
//   origin/main (f4d735a1), so `font-family: monospace` there never becomes var(--font-mono) for everyone.
// - The other six files add no :global( rule, except the anchor the redesign's titles use, `:global(html:root
//   .page-ground) x` or `:global(html:root) x`, which only raises the rank of the file's own element (PR 7: the same
//   specificity as the look-scoped form it replaced).

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

/** The anchor prefix, followed by the file's own selector (not a brace, a comma or another pseudo). */
const PAPER_SCOPE = /:global\(html:root(?:\s+\.page-ground)?\)(?=\s+[^\s{,:])/g;

/** What makes a rule global: `:global`, and the forms that never say it (`:root`, `:host`, a `-global-` keyframes name),
 * which Svelte leaves unscoped (PR 5 re-review R1). */
const LEAK = /:global\b|:root\b|:host\b|@keyframes\s+-global-/;

/** Each line with a global form left once every allowed paper prefix is removed: a parenthesised `:global` behind the
 * prefix, a second selector on the line, Svelte 5's block form `:global { … }` (PR 5 review I3), or one of the forms
 * above. */
function leaks(css: string): string[] {
  return css.split('\n').filter((l) => LEAK.test(l.replace(PAPER_SCOPE, ''))).map((l) => l.trim());
}

describe('no global rule leaks from the converted dense screens', () => {
  it('Identity\'s three global blocks are byte for byte as on origin/main', () => {
    const src = readFileSync(join(SRC, 'views/Identity.svelte'), 'utf-8').replace(/\r\n/g, '\n');
    expect(src).toContain(IDENTITY_GLOBALS);
    // No other global rule besides the three pinned blocks (its paper title, anchored in PR 5, folded in PR 7 T6c).
    expect(leaks(styleBlocks(src)), 'no other :global( in Identity').toEqual([':global(.detail-grid) {', ':global(.dl) {', ':global(.mono) {']);
    // And a count, which catches what a line-based reading can miss: the three blocks, nothing else.
    expect(styleBlocks(src).match(/:global\b/g), 'the :global count in Identity').toHaveLength(3);
  });

  it('the anchor form is allowed, any other :global( is a leak', () => {
    expect(leaks(':global(html:root .page-ground) h1 { font-size: 30px; }')).toEqual([]);
    expect(leaks(':global(html:root) h1 { font-size: 30px; }')).toEqual([]);
    expect(leaks(':global(.mono) { font-family: monospace; }')).toEqual([':global(.mono) { font-family: monospace; }']);
    expect(leaks(':global(html:root .x) { color: red; }')).toHaveLength(1); // the whole selector is global
    expect(leaks(':global(html:root .page-ground .x) { color: red; }')).toHaveLength(1);
    expect(leaks(':global(html:root .page-ground) { color: red; }')).toHaveLength(1); // the region itself, no own element
    // PR 5 review I3: a global behind the allowed prefix, a second selector on the same line, and Svelte 5's block form.
    expect(leaks(':global(html:root .page-ground) :global(.mono) { font-family: var(--font-mono); }')).toHaveLength(1);
    expect(leaks(':global(html:root) h1, :global(.dl) { color: red; }')).toHaveLength(1);
    expect(leaks(':global { .dl { color: var(--text); } }')).toHaveLength(1);
    // PR 5 re-review R1: global rules that never say :global. Svelte leaves :root and :host unscoped, and a -global-
    // keyframes name is global; a plain @keyframes is scoped to the file.
    expect(leaks(':root { --muted: red; }')).toHaveLength(1);
    expect(leaks(':host { color: red; }')).toHaveLength(1);
    expect(leaks('@keyframes -global-spin { to { transform: rotate(1turn); } }')).toHaveLength(1);
    expect(leaks('@keyframes spin { to { transform: rotate(1turn); } }')).toEqual([]);
  });

  it.each(OTHERS)('%s adds no global rule', (file) => {
    expect(leaks(styleBlocks(readFileSync(join(SRC, file), 'utf-8')))).toEqual([]);
  });
});
