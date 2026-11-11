import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { markupOf, scriptBlocks, styleBlocks } from './lib/testing/css';
import { sourceFiles } from './lib/testing/sources';

// The owner's rule: plain words and colour carry the meaning, no invented symbols or glyphs. No source file shows an
// arrow (U+2190 to U+21FF), geometric shape (U+25A0 to U+25FF), dingbat or miscellaneous symbol (U+2600 to U+27BF) or
// emoji (U+1F300 and up). Punctuation stays: · — – − ± ρ … “ ”. PR 7: every source file (lib/testing/sources.ts), not
// the seven screens of PR 4 and PR 5 and three helpers. Comments are not shown, so they are not read.

const SRC = __dirname;
const GLYPH = /[\u2190-\u21FF\u25A0-\u25FF\u2600-\u27BF]|[\u{1F300}-\u{1FAFF}]/u;

/** What a file shows or runs: a component's markup, script and style, or a module's code, comments removed. */
function shown(file: string): string {
  const src = readFileSync(join(SRC, file), 'utf-8');
  if (file.endsWith('.svelte')) return [markupOf(src), scriptBlocks(src), styleBlocks(src)].join('\n');
  return scriptBlocks(`<script>${src}</script>`);
}

const FILES = sourceFiles();

describe('no glyph icons in the dashboard', () => {
  it('the pattern catches arrows, shapes, symbols and emoji, and spares punctuation', () => {
    for (const bad of ['★ core', '▶', '▼', 'raw → current', 'Dimension ↔ Signal', '🙂', '✓', '⚠']) expect(bad, bad).toMatch(GLYPH);
    for (const ok of ['a · b', 'x — y', '1–2', '−1 to 1', '±5%', 'Spearman ρ', 'Loading…', '“not recorded”']) {
      expect(ok, ok).not.toMatch(GLYPH);
    }
  });

  it('the ranges are written as escapes, and each endpoint is in while its neighbour outside is out', () => {
    // A retyped literal endpoint (U+27B0 for U+27BF) would drop fifteen dingbats and still pass the samples above.
    expect(GLYPH.source).toMatch(/^[\x20-\x7e]+$/);
    for (const cp of [0x2190, 0x21ff, 0x25a0, 0x25ff, 0x2600, 0x27bf, 0x1f300, 0x1faff]) {
      expect(String.fromCodePoint(cp), cp.toString(16)).toMatch(GLYPH);
    }
    for (const cp of [0x218f, 0x2200, 0x259f, 0x27c0, 0x1f2ff, 0x1fb00]) {
      expect(String.fromCodePoint(cp), cp.toString(16)).not.toMatch(GLYPH);
    }
  });

  it('reads only what is shown: a glyph in a comment is not counted', () => {
    expect(scriptBlocks('<script>// "a" → "b"\nconst x = 1;</script>')).not.toMatch(GLYPH);
    expect(markupOf('<!-- ▶ --><p>Show</p>')).not.toMatch(GLYPH);
  });

  it('reads every source file, the seven screens and three helpers of PR 4 and PR 5 among them', () => {
    for (const f of ['views/Decisions.svelte', 'views/Rubric.svelte', 'views/Autonomy.svelte', 'views/Intentions.svelte',
      'views/Harness.svelte', 'views/Observability.svelte', 'views/Identity.svelte', 'lib/autonomy.ts', 'lib/harness.ts',
      'lib/intentions.ts', 'App.svelte', 'lib/ui/Nav.svelte']) {
      expect(FILES, f).toContain(f);
    }
  });

  it.each(FILES)('%s', (file) => {
    const hits = shown(file).split('\n').filter((l) => GLYPH.test(l)).map((l) => l.trim());
    expect(hits).toEqual([]);
  });
});
