import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { deflateSync } from 'node:zlib';
import { join, resolve } from 'node:path';
import { readPng } from './lib/testing/png';

// Dashboard redesign PR 7 Task 8: the favicon and the companion's icons are the Archivo Bold N, #eef2f3 on Atlantic blue
// #12304f, drawn once in assets/icon-source/nous-mark.svg and rendered by render-icons.mjs with headless Edge. Each PNG
// carries its recipe as provenance (impeccable embed-prompt: a tEXt chunk, keyword impeccable:prompt).

const APP = resolve(__dirname, '..');
const PUB = join(APP, 'public');
const PLATE: [number, number, number] = [0x12, 0x30, 0x4f];
const near = (p: number[], c: number[], tol = 2) => c.every((v, i) => Math.abs(p[i] - v) <= tol);
const light = (p: number[]) => p[3] > 200 && p[0] > 160 && p[1] > 160 && p[2] > 160;

const TARGETS = [
  { file: 'icons/companion-192.png', size: 192, kind: 'any' },
  { file: 'icons/companion-512.png', size: 512, kind: 'any' },
  { file: 'icons/companion-maskable-192.png', size: 192, kind: 'maskable' },
  { file: 'icons/companion-maskable-512.png', size: 512, kind: 'maskable' },
  { file: 'icons/apple-touch-icon.png', size: 180, kind: 'apple-touch' },
] as const;
const png = (file: string) => readPng(readFileSync(join(PUB, file)));

describe('the PNG reader', () => {
  it('reads the header, a tEXt entry and the pixels through the None, Sub and Up filters', () => {
    const ihdr = Buffer.alloc(13);
    ihdr.writeUInt32BE(2, 0); ihdr.writeUInt32BE(3, 4); ihdr[8] = 8; ihdr[9] = 6;
    // Three rows of two RGBA pixels: filter 0 (raw), 1 (Sub: each byte minus the one a pixel left), 2 (Up).
    const raw = Buffer.from([0, 10, 20, 30, 255, 40, 50, 60, 255, 1, 5, 6, 7, 255, 1, 1, 1, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0]);
    const chunk = (type: string, data: Buffer) => {
      const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
      return Buffer.concat([len, Buffer.from(type, 'latin1'), data, Buffer.alloc(4)]);
    };
    const file = Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', ihdr),
      chunk('tEXt', Buffer.from('impeccable:prompt\0a recipe', 'latin1')), chunk('IDAT', deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
    const p = readPng(file);
    expect([p.width, p.height, p.colorType, p.chunks]).toEqual([2, 3, 6, ['IHDR', 'tEXt', 'IDAT', 'IEND']]);
    expect(p.text['impeccable:prompt']).toBe('a recipe');
    expect(p.pixel(1, 0)).toEqual([40, 50, 60, 255]);
    expect(p.pixel(0, 1)).toEqual([5, 6, 7, 255]);
    expect(p.pixel(1, 1)).toEqual([6, 7, 8, 255]);
    expect(p.pixel(1, 2)).toEqual([6, 7, 8, 255]);
  });
});

describe('the rendered icons', () => {
  it.each(TARGETS)('$file: $size px, its colour type, and its recipe embedded', ({ file, size, kind }) => {
    const p = png(file);
    expect([p.width, p.height]).toEqual([size, size]);
    expect(p.colorType, 'RGB for the apple icon (iOS fills transparency with black), RGBA for the others').toBe(kind === 'apple-touch' ? 2 : 6);
    const recipe = p.text['impeccable:prompt'] ?? '';
    for (const s of ['nous-mark.svg', 'Archivo Bold', `${size}x${size}`, `(${kind}:`, '#eef2f3 on #12304f', 'No generated imagery']) {
      expect(recipe, s).toContain(s);
    }
  });

  it.each(TARGETS.filter((t) => t.kind !== 'any'))('$file is full bleed: opaque Atlantic blue in all four corners', ({ file, size }) => {
    const p = png(file);
    for (const [x, y] of [[0, 0], [size - 1, 0], [0, size - 1], [size - 1, size - 1]]) {
      expect(near(p.pixel(x, y), [...PLATE, 255]), `${x},${y}`).toBe(true);
    }
  });

  it.each(TARGETS.filter((t) => t.kind === 'maskable'))('$file keeps every pixel of the N inside the 80% safe circle', ({ file, size }) => {
    const p = png(file);
    let ink = 0;
    let far = 0;
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        if (!light(p.pixel(x, y))) continue;
        ink++;
        if (Math.hypot(x + 0.5 - size / 2, y + 0.5 - size / 2) > 0.4 * size) far++;
      }
    }
    expect(ink, 'the N is drawn').toBeGreaterThan(size * size * 0.03);
    expect(far).toBe(0);
  });

  it.each(TARGETS.filter((t) => t.kind === 'any'))('$file has transparent corners, an opaque plate and the N at the centre', ({ file, size }) => {
    const p = png(file);
    for (const [x, y] of [[0, 0], [size - 1, 0], [0, size - 1], [size - 1, size - 1]]) expect(p.pixel(x, y)[3], `${x},${y}`).toBe(0);
    expect(near(p.pixel(Math.round(size * 0.12), Math.round(size / 2)), [...PLATE, 255]), 'the plate, centre left').toBe(true);
    expect(light(p.pixel(Math.round(size * 0.3), Math.round(size / 2))), "the N's left stem").toBe(true);
  });
});

describe('the sources and the references', () => {
  const mark = readFileSync(join(APP, 'assets/icon-source/nous-mark.svg'), 'utf-8');
  const favicon = readFileSync(join(PUB, 'favicon.svg'), 'utf-8');

  it('the master and the favicon are the N on Atlantic blue, with their provenance, and the old mark is gone', () => {
    for (const svg of [mark, favicon]) {
      expect(svg).toContain('fill="#12304f"');
      expect(svg).toContain('fill="#eef2f3"');
      expect(svg).toMatch(/<desc>Provenance\.[^<]*Archivo Bold[^<]*SIL Open Font License 1\.1[^<]*<\/desc>/);
      expect(svg).not.toMatch(/filter=|<ellipse|<mask/); // the old violet mark's blur layers (its hexes: oldPalette.test.ts)
    }
    const n = /<path id="n"[^>]* d="([^"]+)"/.exec(mark)?.[1];
    expect(n, 'the master names its glyph path').toBeTruthy();
    expect(favicon).toContain(`d="${n}"`); // the same outline
    expect(existsSync(join(PUB, 'icons.svg')), 'the unreferenced scaffold sprite is deleted').toBe(false);
  });

  it('every manifest icon exists, at the size it declares, and maskable means the full-bleed file', () => {
    const manifest = JSON.parse(readFileSync(join(PUB, 'companion.webmanifest'), 'utf-8')) as {
      icons: { src: string; sizes: string; purpose?: string }[];
    };
    expect(manifest.icons).toHaveLength(4);
    for (const icon of manifest.icons) {
      expect(icon.src.startsWith('/dashboard/v2/'), icon.src).toBe(true);
      const file = icon.src.slice('/dashboard/v2/'.length);
      const p = png(file);
      expect(`${p.width}x${p.height}`, icon.src).toBe(icon.sizes);
      expect(file.includes('maskable'), icon.src).toBe(icon.purpose === 'maskable');
    }
  });

  it.each(['index.html', 'companion.html'])('%s links the favicon and the apple-touch icon under /dashboard/v2/, and both exist', (page) => {
    const html = readFileSync(join(APP, page), 'utf-8');
    const href = (rel: string) => new RegExp(`<link rel="${rel}"[^>]*href="([^"]+)"`).exec(html)?.[1];
    expect(href('icon')).toBe('/dashboard/v2/favicon.svg');
    expect(href('apple-touch-icon')).toBe('/dashboard/v2/icons/apple-touch-icon.png');
    for (const h of [href('icon')!, href('apple-touch-icon')!]) expect(existsSync(join(PUB, h.slice('/dashboard/v2/'.length))), h).toBe(true);
  });
});
