import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';

// Dashboard redesign PR 7: the dark look is gone, and with it its palette (the near-black grounds, the violet accent and
// its glow, the grey inks) and its two fonts. None of them may come back in a source, a test, a public file or a page;
// a test that still names an old value to forbid it in a render is replaced by this one guard over the files.
// The one legitimate home is the companion's old look, kept on purpose (PR 6): the nous-dark theme, the legacy group's
// two fonts, and the tests that pin those values. ALLOWED names each place exactly; everything else in the same file is
// read like any other file.

const APP = resolve(__dirname, '..');

/** The old look: its hexes (any case, also with an alpha appended), the accent as rgba(), and the two fonts (also as a
 * Google Fonts family, `DM+Sans`). */
const OLD = [
  '#0a0a0f', '#12121a', '#181824', '#1e1e2e', '#7c6af7', '#4f46a8', '#a99df9', '#e2e2f0', '#8e8eab', '#6b6b8a', '#0b0b10',
  '#863bff', '#7e14ff',
];
const OLD_SOURCE = [...OLD, String.raw`rgba\(\s*124\s*,\s*106\s*,\s*247\b`, String.raw`DM[ +]Sans`, String.raw`JetBrains[ +]Mono`].join('|');
const OLD_PALETTE = new RegExp(OLD_SOURCE, 'i');
const OLD_ALL = new RegExp(OLD_SOURCE, 'gi');

/** A place where the old palette may stand: in `file`, inside each text `scope` matches, the text `values` matches is
 * allowed (and removed before the guard reads the file). */
type Allowance = { file: string; scope: RegExp; values: RegExp; reason: string };
const LEGACY_FONTS = /'(?:DM Sans|JetBrains Mono)'/g;
const ALLOWED: Allowance[] = [
  {
    file: 'src/companion/companion.css', scope: /\[data-theme='nous-dark'\]\s*\{[^}]*\}/g, values: OLD_ALL,
    reason: "the nous-dark theme block: the companion's previous default, kept as a named theme (PR 6)",
  },
  {
    file: 'src/companion/companion.css',
    scope: /\[data-theme='nous-dark'\],\s*\[data-theme='alpine-dusk'\],\s*\[data-theme='harbor'\],\s*\[data-theme='paper'\],\s*\[data-theme='signal'\],\s*\[data-theme='report'\]\s*\{[^}]*\}/g,
    values: LEGACY_FONTS,
    reason: "the legacy group's two fonts: nous-dark and the five named themes keep DM Sans and JetBrains Mono (only the fonts)",
  },
  {
    file: 'src/companion/companion.css', scope: /\[data-theme='signal'\]\s*\{[^}]*\}/g, values: LEGACY_FONTS,
    reason: "the signal theme's mono display face (only the fonts)",
  },
  {
    file: 'src/companion/companion.css', scope: /@import url\('[^']*'\)/g, values: /family=(?:DM\+Sans|JetBrains\+Mono):[^&']*/g,
    reason: 'the font import loads the two legacy faces those blocks name (only those two families)',
  },
  {
    file: 'src/companion/themes.test.ts',
    scope: /^\s*'--[\w-]+': (?:'[^']*'|"[^"]*"),?\s*$|^const JETBRAINS = "[^"]*";$|for \(const family of \[[^\]]*\]\)|^\s*it\('[^']*'/gm,
    values: OLD_ALL,
    reason: "the pins of those themes: the resolved token tables (a token per line), the JETBRAINS constant, the import's " +
      'family list and the titles that name the face',
  },
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

/** `text` with every allowance for `file` applied: inside each scope, the allowed values removed. */
function allowed(file: string, text: string): string {
  return ALLOWED.filter((a) => a.file === file).reduce((t, a) => t.replace(a.scope, (m) => m.replace(a.values, '')), text);
}
const read = (f: string) => readFileSync(join(APP, f), 'utf-8').replace(/\r\n/g, '\n');
const hit = (f: string) => OLD_PALETTE.test(allowed(f, read(f)));

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
      'src/companion/companion.css', 'src/companion/themes.test.ts', 'public/favicon.svg', 'public/companion.webmanifest']) {
      expect(files, f).toContain(f);
    }
    expect(files.some((f) => f.startsWith('public/lib/'))).toBe(false); // vendored Chart.js, d3, cytoscape
  });

  it('an allowance spares only its own values in its own scope', () => {
    const css = [
      "@import url('https://x/css2?family=Archivo:wght@400&family=DM+Sans:wght@400&display=swap');",
      "[data-theme='nous-dark'] { --bg: #0a0a0f; --font-ui: 'DM Sans'; }",
      "[data-theme='signal'] { --font-mono: 'JetBrains Mono'; }",
    ].join('\n');
    expect(OLD_PALETTE.test(allowed('src/companion/companion.css', css))).toBe(false);
    // The same values one block over, a hex in the fonts-only blocks, or a palette value in any other file, still fail.
    for (const bad of ["[data-theme='harbor'] { --bg: #0a0a0f; }", "[data-theme='signal'] { --bg: #0a0a0f; }",
      ":root { --font-ui: 'DM Sans'; }", "@import url('https://x/css2?family=Archivo&display=swap'); .x { color: #7c6af7 }"]) {
      expect(OLD_PALETTE.test(allowed('src/companion/companion.css', bad)), bad).toBe(true);
    }
    expect(OLD_PALETTE.test(allowed('src/companion/Companion.svelte', "[data-theme='nous-dark'] { --bg: #0a0a0f; }"))).toBe(true);
    // In the pins file only a pin line is spared: a palette value in a test body is not.
    expect(OLD_PALETTE.test(allowed('src/companion/themes.test.ts', "    '--bg': '#0a0a0f',"))).toBe(false);
    expect(OLD_PALETTE.test(allowed('src/companion/themes.test.ts', "    expect(x).toBe('#0a0a0f');"))).toBe(true);
  });

  it('no file holds the old palette outside its allowances', () => {
    const excluded = ['public/favicon.svg', 'src/oldPalette.test.ts'];
    expect(scanned().filter((f) => !excluded.includes(f)).filter(hit)).toEqual([]);
  });

  it.each(ALLOWED.map((a, i) => [i, a.file, a.reason] as const))('allowance %i (%s) still spares something: %s', (i) => {
    const a = ALLOWED[i];
    const scopes = read(a.file).match(a.scope) ?? [];
    expect(scopes.length, 'the scope matches nothing').toBeGreaterThan(0);
    expect(scopes.some((m) => (m.match(a.values) ?? []).some((v) => OLD_PALETTE.test(v))), 'it spares no old value: drop it').toBe(true);
  });

  it('the guard itself and the old mark are the only files left out (the mark until Task 8 re-draws it)', () => {
    expect(OLD_PALETTE.test(read('src/oldPalette.test.ts'))).toBe(true);
    expect(read('public/favicon.svg')).toMatch(/#863bff/i);
  });

  it('the stylesheet and the page the dark look lived in are clean (its other files are gone: flipResidue.test.ts)', () => {
    expect(existsSync(join(APP, 'src/app.css'))).toBe(true);
    expect(hit('src/app.css')).toBe(false);
    expect(hit('index.html')).toBe(false);
  });
});
