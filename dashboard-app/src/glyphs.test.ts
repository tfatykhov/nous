import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { PAPER_ROUTES } from './lib/look';
import { markupOf, scriptBlocks, styleBlocks } from './lib/testing/css';

// The owner's rule: plain words and colour carry the meaning, no invented symbols or glyphs. On the screens PR 4 and
// PR 5 convert, no arrow (U+2190 to U+21FF), geometric shape (U+25A0 to U+25FF), dingbat or miscellaneous symbol
// (U+2600 to U+27BF) or emoji (U+1F300 and up) is shown. Punctuation stays: · — – − ± ρ … “ ”.
// A screen joins the list when its route turns paper (it is then in HEX_GUARDED); the three helper modules are always
// in it. Comments are not shown, so they are not read.

const SRC = __dirname;
const GLYPH = /[\u2190-\u21FF\u25A0-\u25FF\u2600-\u27BF]|[\u{1F300}-\u{1FAFF}]/u;

/** PR 4 and PR 5's routes and the view each renders. */
const SCREENS: Record<string, string> = {
  decisions: 'views/Decisions.svelte',
  rubric: 'views/Rubric.svelte',
  autonomy: 'views/Autonomy.svelte',
  intentions: 'views/Intentions.svelte',
  harness: 'views/Harness.svelte',
  observability: 'views/Observability.svelte',
  identity: 'views/Identity.svelte',
};
const HELPERS = ['lib/autonomy.ts', 'lib/harness.ts', 'lib/intentions.ts'];

function guardList(name: string): string[] {
  const src = readFileSync(join(SRC, 'hexGuard.test.ts'), 'utf-8');
  const body = src.match(new RegExp(`export const ${name}[^=]*=\\s*\\[([\\s\\S]*?)\\];`))?.[1] ?? '';
  return [...body.matchAll(/'([^']+)'/g)].map((m) => m[1]);
}

/** What a file shows or runs: a component's markup, script and style, or a module's code, comments removed. */
function shown(file: string): string {
  const src = readFileSync(join(SRC, file), 'utf-8');
  if (file.endsWith('.svelte')) return [markupOf(src), scriptBlocks(src), styleBlocks(src)].join('\n');
  return scriptBlocks(`<script>${src}</script>`);
}

const converted = Object.values(SCREENS).filter((f) => guardList('HEX_GUARDED').includes(f));
const FILES = [...HELPERS, ...converted];

describe('no glyph icons on the Decisions, Autonomy and System screens', () => {
  it('the pattern catches arrows, shapes, symbols and emoji, and spares punctuation', () => {
    for (const bad of ['★ core', '▶', '▼', 'raw → current', 'Dimension ↔ Signal', '🙂', '✓', '⚠']) expect(bad, bad).toMatch(GLYPH);
    for (const ok of ['a · b', 'x — y', '1–2', '−1 to 1', '±5%', 'Spearman ρ', 'Loading…', '“not recorded”']) {
      expect(ok, ok).not.toMatch(GLYPH);
    }
  });

  it('reads only what is shown: a glyph in a comment is not counted', () => {
    expect(scriptBlocks('<script>// "a" → "b"\nconst x = 1;</script>')).not.toMatch(GLYPH);
    expect(markupOf('<!-- ▶ --><p>Show</p>')).not.toMatch(GLYPH);
  });

  it('every converted screen of the seven is in the list (a screen joins when its route turns paper)', () => {
    for (const [route, file] of Object.entries(SCREENS)) {
      if ((PAPER_ROUTES as readonly string[]).includes(route)) expect(FILES, route).toContain(file);
    }
    expect(FILES.length).toBeGreaterThanOrEqual(HELPERS.length);
  });

  it.each(FILES)('%s', (file) => {
    const hits = shown(file).split('\n').filter((l) => GLYPH.test(l)).map((l) => l.trim());
    expect(hits).toEqual([]);
  });
});
