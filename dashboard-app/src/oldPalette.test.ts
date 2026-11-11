import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';

// Dashboard redesign PR 7: the dark look is gone, and with it its palette (the near-black grounds, the violet accent and
// its glow, the grey inks) and its two fonts. None of them may come back in a source, a test, a public file or a page;
// a test that still names an old value to forbid it in a render is replaced by this one guard over the files.

const APP = resolve(__dirname, '..');

/** The old look: its hexes (any case, also with an alpha appended), the accent as rgba(), and the two fonts (also as a
 * Google Fonts family, `DM+Sans`). */
const OLD = [
  '#0a0a0f', '#12121a', '#181824', '#1e1e2e', '#7c6af7', '#4f46a8', '#a99df9', '#e2e2f0', '#8e8eab', '#6b6b8a', '#0b0b10',
  '#863bff', '#7e14ff',
];
const OLD_PALETTE = new RegExp([...OLD, String.raw`rgba\(\s*124\s*,\s*106\s*,\s*247\b`, String.raw`DM[ +]Sans`, String.raw`JetBrains[ +]Mono`].join('|'), 'i');

/** Not read until the work that replaces them lands; each with its reason. A group that no longer holds the old palette
 * fails (no stale exclusion). */
const EXCLUDED: { match: RegExp; reason: string }[] = [
  {
    match: /^(?:src\/companion\/|companion\.html$|public\/companion[.-])/,
    reason: 'the companion: PR 6 re-themes it (its nous-dark and legacy themes keep the old values on purpose); after the ' +
      'rebase onto PR 6 this becomes the narrow allowance of companion.css and the companion tests that assert those themes',
  },
  { match: /^public\/favicon\.svg$/, reason: 'the old violet mark (#863bff); PR 7 Task 8 re-draws it after PR 6 lands' },
  { match: /^src\/oldPalette\.test\.ts$/, reason: 'this guard spells the patterns it forbids' },
];

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? walk(join(dir, e.name)) : [join(dir, e.name)]));
}

/** src (sources, tests, styles), public (not the vendored public/lib) and the two pages; text files only. */
function scanned(): string[] {
  const pub = walk(join(APP, 'public')).filter((f) => !relative(join(APP, 'public'), f).replace(/\\/g, '/').startsWith('lib/'));
  return [...walk(join(APP, 'src')), ...pub, join(APP, 'index.html'), join(APP, 'companion.html')]
    .map((f) => relative(APP, f).replace(/\\/g, '/'))
    .filter((f) => !/\.(png|ico|jpe?g|webp|woff2?)$/i.test(f))
    .sort();
}

const hit = (f: string) => OLD_PALETTE.test(readFileSync(join(APP, f), 'utf-8'));

describe('no old palette anywhere in the dashboard', () => {
  it('the pattern matches each old value in its own sample, any case, and spares the new palette', () => {
    for (const h of OLD) {
      for (const s of [`color: ${h};`, `color: ${h.toUpperCase()};`, `'${h}33'`]) expect(s, s).toMatch(OLD_PALETTE);
    }
    for (const s of ['rgba(124, 106, 247, 0.15)', 'rgba(124,106,247,.5)', "font-family: 'DM Sans'", 'family=DM+Sans:wght@400',
      "'JetBrains Mono', monospace", 'family=JetBrains+Mono']) {
      expect(s, s).toMatch(OLD_PALETTE);
    }
    for (const s of ['#12304f', '#eef2f3', '#2346c8', 'rgba(20, 32, 43, 0.35)', "'Archivo'", "'Red Hat Mono'", 'rgba(124, 106, 24, 1)']) {
      expect(s, s).not.toMatch(OLD_PALETTE);
    }
  });

  it('reads the sources, the tests, the stylesheet, the public files and both pages', () => {
    const files = scanned();
    expect(files.length).toBeGreaterThan(200);
    for (const f of ['src/app.css', 'src/App.svelte', 'src/tokens.test.ts', 'src/lib/chartColors.ts', 'index.html', 'companion.html',
      'public/favicon.svg', 'public/icons.svg']) {
      expect(files, f).toContain(f);
    }
    expect(files.some((f) => f.startsWith('public/lib/'))).toBe(false); // vendored Chart.js, d3, cytoscape
  });

  it('no file holds the old palette', () => {
    expect(scanned().filter((f) => !EXCLUDED.some((x) => x.match.test(f))).filter(hit)).toEqual([]);
  });

  it.each(EXCLUDED.map((x) => [x.match.source, x.reason] as const))('the exclusion %s still holds the old palette (%s)', (src) => {
    const x = EXCLUDED.find((e) => e.match.source === src)!;
    const files = scanned().filter((f) => x.match.test(f));
    expect(files.length, 'the exclusion names no file').toBeGreaterThan(0);
    expect(files.some(hit), 'no excluded file holds the old palette any more: drop the exclusion').toBe(true);
  });

  it('the stylesheet and the page the dark look lived in are clean (its other files are gone: flipResidue.test.ts)', () => {
    expect(existsSync(join(APP, 'src/app.css'))).toBe(true);
    expect(hit('src/app.css')).toBe(false);
    expect(hit('index.html')).toBe(false);
  });
});
