import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

// A page's title names it the way the menu does, so clicking "Guards" does not land on a page called "Harness".
const SRC = resolve(__dirname, '..');
const TITLES: [string, string][] = [
  ['views/DagView.svelte', 'Workflows'],
  ['views/Rubric.svelte', 'Scoring'],
  ['views/Harness.svelte', 'Guards'],
];

describe('page titles match the menu', () => {
  it.each(TITLES)('%s is titled %s', (file, title) => {
    const h1s = [...readFileSync(join(SRC, file), 'utf-8').matchAll(/<h1\b[^>]*>([^<]*)<\/h1>/g)].map((m) => m[1].trim());
    expect(h1s).toEqual([title]);
  });
});
