import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { markupOf, scriptBlocks } from './lib/testing/css';
import { sourceFiles } from './lib/testing/sources';

// Codex round 1: the header consolidation (c5dc5700, c335b91a) left one "updated" badge per screen. A screen
// whose content comes from more than one poll store must have that badge speak for all of them (combinePolls: the
// oldest update, and an error naming what failed), and its Refresh must refresh every one of them.

const read = (f: string) => readFileSync(join(__dirname, f), 'utf-8');

/** The poll stores a component reads: its own `const x = usePoll(` and the shared attentionPoll if it reads it. */
function storesOf(src: string): string[] {
  const own = [...scriptBlocks(src).matchAll(/const (\w+) = usePoll\(/g)].map((m) => m[1]);
  return /\$attentionPoll\b/.test(markupOf(src) + scriptBlocks(src)) ? [...own, 'attentionPoll'] : own;
}

const VIEWS = sourceFiles().filter((f) => f.endsWith('.svelte') && /<StaleBadge\b/.test(read(f)));
const MULTI = VIEWS.filter((f) => storesOf(read(f)).length > 1);

describe('one badge speaks for every store', () => {
  it('finds the screens that read more than one store', () => {
    expect(MULTI.sort()).toEqual(['views/Consolidation.svelte', 'views/Harness.svelte', 'views/Observability.svelte', 'views/Overview.svelte',
      'views/Subtasks.svelte', 'views/health/GraphTab.svelte', 'views/health/GrowthTab.svelte']);
  });

  it.each(VIEWS)('%s: one badge', (f) => {
    expect((markupOf(read(f)).match(/<StaleBadge\b/g) ?? []).length).toBe(1);
  });

  it.each(MULTI)('%s: the badge combines every store, and Refresh refreshes every one', (f) => {
    const src = read(f);
    const stores = storesOf(src);
    const expr = markupOf(src).match(/<StaleBadge state=\{([\s\S]*?)\} \/>/)?.[1] ?? '';
    // the badge's state: combinePolls(...) itself, or a name the script derives from it
    const badge = /^\w+$/.test(expr) ? (scriptBlocks(src).match(new RegExp(String.raw`const ${expr} = \$derived\((combinePolls\(\{[^\n]*\}\))\);`))?.[1] ?? '') : expr;
    expect(badge).toMatch(/^combinePolls\(\{/);
    for (const s of stores) expect(badge, s).toContain(`$${s}`);
    const fn = markupOf(src).match(/<Button\b[^>]*onclick=\{(?:\(\) => void )?(\w+)(?:\(\))?\}[^>]*>\s*\{[^}]*'Refresh'/)?.[1] ?? '';
    expect(fn, 'a named page Refresh').not.toBe('');
    // The whole function, whatever the checkout's line endings: a declaration or a block-bodied arrow runs to the
    // closing brace at its own indent; an expression-bodied arrow is its one line.
    const script = scriptBlocks(src).replace(/\r\n/g, '\n');
    const body =
      script.match(new RegExp(String.raw`\n( *)(?:function ${fn}\(\)|const ${fn} = \(\) =>) *\{[\s\S]*?\n\1\}`))?.[0] ??
      script.match(new RegExp(String.raw`const ${fn} = \(\) =>[^\n]*`))?.[0] ??
      '';
    expect(body, `the body of ${fn}`).not.toBe('');
    for (const s of stores) expect(body, `${fn} refreshes ${s}`).toContain(`${s}.refresh()`);
  });
});
