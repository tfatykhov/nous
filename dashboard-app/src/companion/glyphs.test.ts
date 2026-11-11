/// <reference types="node" />
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, relative } from 'node:path';
import { markupOf, scriptBlocks } from '../lib/testing/css';

// No invented symbols or glyphs (the owner's rule): what a companion component prints is words. The dashboard's own
// glyph guard walks dashboard views only. Typography stays: the ellipsis, the em and en dash, and the middle dot used
// as a separator.
const ALLOWED = new Set(['…', '—', '–', '·']);
// figure.ts parses agent-written figures, whose arrows and signs are data, not something a component prints.
const EXCLUDED = new Set(['figure.ts']);

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return e.name === 'testing' || e.name === '__fixtures__' ? [] : sources(p);
    if (EXCLUDED.has(e.name)) return [];
    return e.name.endsWith('.svelte') || (e.name.endsWith('.ts') && !e.name.endsWith('.test.ts')) ? [p] : [];
  });
}

/** What can reach the screen: a component's markup and script, a module's code; comments removed. */
function printable(file: string): string {
  const src = readFileSync(file, 'utf-8');
  if (file.endsWith('.svelte')) return markupOf(src) + '\n' + scriptBlocks(src);
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|\s)\/\/.*$/gm, '$1');
}

const FILES = sources(__dirname);

describe('the companion prints words, not glyphs (review I6)', () => {
  it('the scan reads the components and modules', () => {
    const rel = FILES.map((f) => relative(__dirname, f).replace(/\\/g, '/'));
    expect(rel).toContain('catalog/DeltaListView.svelte');
    expect(rel).toContain('Renderer.svelte');
    expect(rel).not.toContain('figure.ts');
  });

  it.each(FILES.map((f) => [relative(__dirname, f).replace(/\\/g, '/'), f]))('%s', (_, file) => {
    const odd = [...printable(file)].filter((ch) => ch.charCodeAt(0) > 126 && !ALLOWED.has(ch));
    expect(odd).toEqual([]);
  });

  it('the files that draw an <svg> are the charts, the graphs, Icon and the footer spinner: a new inline icon fails here until it is listed', () => {
    const drawing = FILES.filter((f) => f.endsWith('.svelte') && markupOf(readFileSync(f, 'utf-8')).includes('<svg'))
      .map((f) => relative(__dirname, f).replace(/\\/g, '/'))
      .sort();
    expect(drawing).toEqual([
      'catalog/AppFooterView.svelte',
      'catalog/DagGraphView.svelte',
      'catalog/IconView.svelte',
      'catalog/LineChartView.svelte',
      'catalog/MemoryGraphView.svelte',
      'catalog/SparkSvg.svelte',
    ]);
  });
});
