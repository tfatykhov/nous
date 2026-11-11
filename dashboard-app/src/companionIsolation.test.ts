import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';

// The companion (/dashboard/v2/companion.html) is a separate app with its own stylesheet. The dashboard redesign is
// staged behind a look switch that the companion must never see: no app.css, no look module or boot script, no
// data-look. Its default theme is the paper world (PR 6), so companion.css, and only companion.css, names the paper
// faces (Archivo, Red Hat Mono): a component reads them through --font-ui and --font-mono.
const APP = resolve(__dirname, '..');
const COMPANION = join(__dirname, 'companion');

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return files(p);
    return /\.(ts|svelte|css)$/.test(e.name) && !e.name.endsWith('.test.ts') ? [p] : [];
  });
}

const FORBIDDEN: [string, RegExp][] = [
  ['an app.css import', /@import\s+(url\()?['"][^'"]*app\.css|import\s+['"][^'"]*app\.css|from\s+['"][^'"]*app\.css/],
  ['the look module', /['"][^'"]*\/look(\.ts)?['"]/],
  ['the boot script', /look-boot/],
  ['data-look', /data-look|dataset\.look/],
  ['the look key', /nous\.look/],
];

/** The paper faces: named in companion.css, nowhere else. */
const PAPER_FACES = /Archivo|Red[ +]Hat[ +]Mono/;

describe('the companion is untouched by the redesign', () => {
  const sources = [join(APP, 'companion.html'), ...files(COMPANION)];

  it('the import pattern catches the ways a file could pull app.css in, and spares a comment about it', () => {
    const pattern = FORBIDDEN[0][1];
    for (const bad of ["@import '../app.css';", '@import url("./app.css");', "import '../app.css';", "import x from '$lib/../app.css';"]) {
      expect(bad).toMatch(pattern);
    }
    expect('Deliberately does NOT import app.css: the companion needs no').not.toMatch(pattern);
  });

  it('reads the companion page and its sources', () => {
    const rel = sources.map((f) => relative(APP, f).replace(/\\/g, '/'));
    expect(rel).toContain('companion.html');
    expect(rel).toContain('src/companion/companion.css');
    expect(rel.length).toBeGreaterThan(10);
  });

  it.each(FORBIDDEN)('no file holds %s', (_, pattern) => {
    const hits = sources.filter((f) => pattern.test(readFileSync(f, 'utf-8'))).map((f) => relative(APP, f));
    expect(hits).toEqual([]);
  });

  it('no companion file but companion.css names Archivo or Red Hat Mono', () => {
    const css = join(COMPANION, 'companion.css');
    expect(PAPER_FACES.test(readFileSync(css, 'utf-8'))).toBe(true);
    const hits = sources.filter((f) => f !== css && PAPER_FACES.test(readFileSync(f, 'utf-8'))).map((f) => relative(APP, f));
    expect(hits).toEqual([]);
  });

  it('companion.css still says it does not import app.css', () => {
    const css = readFileSync(join(COMPANION, 'companion.css'), 'utf-8');
    expect(css.trimStart().startsWith('/* Nous Companion')).toBe(true);
    expect(css.slice(0, 300)).toContain('Deliberately does NOT import app.css');
  });
});
