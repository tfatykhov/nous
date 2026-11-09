import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';

// F108 Phase 2 T14: the six retired routes only redirect now. No source file may link to one: every link goes to
// the screen's new home. Template-built hrefs (the Nav's `#/{id}`) are checked by Nav.test.ts, not by this regex.
// The router itself is left out: REDIRECTS keys are bare route names, so the table never matches anyway.
const SRC = resolve(__dirname, '..');
const RETIRED = /#\/(activity|admission|memory-write-health|density|cache|execution)(?![A-Za-z0-9-])/;

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...sources(path));
    else if (/\.(svelte|ts)$/.test(entry.name) && !entry.name.endsWith('.test.ts')) out.push(path);
  }
  return out;
}

describe('retired routes', () => {
  const files = sources(SRC).filter((f) => relative(SRC, f).replace(/\\/g, '/') !== 'lib/router.ts');

  it('reads the whole source tree', () => {
    const rel = files.map((f) => relative(SRC, f).replace(/\\/g, '/'));
    expect(rel).toContain('App.svelte');
    expect(rel).toContain('lib/ui/Nav.svelte');
    expect(rel).toContain('views/work/Trace.svelte');
    expect(rel).not.toContain('lib/router.ts');
    expect(rel.some((f) => f.endsWith('.test.ts'))).toBe(false);
  });

  it('the pattern catches a retired link with a sub or a query, and spares the live routes', () => {
    for (const bad of ['href="#/cache"', "'#/execution'", '#/activity/x', '#/density?d=7', '`#/admission`', '#/memory-write-health']) {
      expect(bad).toMatch(RETIRED);
    }
    for (const ok of ['#/model-calls/cache', '#/work/changes', '#/health/writes', '#/health/graph', '#/observability', '#/cache-x']) {
      expect(ok).not.toMatch(RETIRED);
    }
  });

  it('no source file links to a retired route', () => {
    const hits: string[] = [];
    for (const f of files) {
      readFileSync(f, 'utf-8').split('\n').forEach((line, i) => {
        if (RETIRED.test(line)) hits.push(`${relative(SRC, f)}:${i + 1}: ${line.trim()}`);
      });
    }
    expect(hits).toEqual([]);
  });

  // The companion (src/companion/) is a separate app with its own hash routes; it is not checked here.
  it('dashboard links are built with routeHref, never a hand-written href="#/…"', () => {
    const hits: string[] = [];
    for (const f of files.filter((x) => !relative(SRC, x).replace(/\\/g, '/').startsWith('companion/'))) {
      readFileSync(f, 'utf-8').split('\n').forEach((line, i) => {
        if (/href=\{?\s*["'`]#\//.test(line)) hits.push(`${relative(SRC, f)}:${i + 1}: ${line.trim()}`);
      });
    }
    expect(hits).toEqual([]);
  });
});
