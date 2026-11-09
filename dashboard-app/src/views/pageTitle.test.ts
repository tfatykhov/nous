import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';

// The F108 screens share one page-title style, `.page-h1` in app.css, instead of a copy of the rule in each file.
const SRC = resolve(__dirname, '..');
const RULE = /font-size:\s*1\.75rem;\s*font-weight:\s*700;\s*letter-spacing:\s*-0\.02em;\s*margin:\s*0\.25rem 0;?/;
const SCREENS = [
  'views/Work.svelte', 'views/work/SessionList.svelte', 'views/work/Patterns.svelte',
  'views/ModelCalls.svelte', 'views/modelcalls/CallList.svelte', 'views/Health.svelte',
];

function svelteFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? svelteFiles(join(dir, e.name)) : e.name.endsWith('.svelte') ? [join(dir, e.name)] : []);
}

describe('page title style', () => {
  it('is defined once, in app.css', () => {
    const css = readFileSync(join(SRC, 'app.css'), 'utf-8');
    const block = css.match(/\.page-h1\s*\{([^}]*)\}/);
    expect(block).not.toBeNull();
    expect(block![1].replace(/\s+/g, ' ').trim()).toMatch(RULE);
  });

  it('no component keeps its own copy of the rule', () => {
    const copies = svelteFiles(SRC).filter((f) => RULE.test(readFileSync(f, 'utf-8'))).map((f) => relative(SRC, f));
    expect(copies).toEqual([]);
  });

  it('every page title of the F108 screens uses the shared class', () => {
    for (const f of SCREENS) {
      const src = readFileSync(join(SRC, f), 'utf-8');
      const h1s = src.match(/<h1\b[^>]*>/g) ?? [];
      expect(h1s.length, f).toBeGreaterThan(0);
      for (const tag of h1s) expect(tag, f).toContain('class="page-h1"');
      expect(src, f).not.toMatch(/^\s*h1\s*\{/m);
    }
  });
});
