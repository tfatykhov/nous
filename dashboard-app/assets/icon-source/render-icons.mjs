// Renders the dashboard and companion PNG icons from nous-mark.svg with headless Microsoft Edge (Node standard library
// only; Pillow, through `py -I`, crops to the exact size and sets the colour type). Run from dashboard-app:
//   node assets/icon-source/render-icons.mjs
// Then embed each printed recipe in its PNG as provenance:
//   .claude/skills/impeccable/scripts/impeccable embed-prompt <png> --prompt "<recipe>"
// EDGE overrides the browser path. Nothing here is generated imagery: the mark is the Archivo Bold N, outlined.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = resolve(HERE, '..', '..');
const EDGE = process.env.EDGE ?? 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const MASTER = readFileSync(join(HERE, 'nous-mark.svg'), 'utf-8');
const N_PATH = /<path id="n"[^>]* d="([^"]+)"/.exec(MASTER)?.[1];
if (!N_PATH) throw new Error('nous-mark.svg has no <path id="n">');

const PLATE = '#12304f';
const INK = '#eef2f3';
/** The N's glyph box in font units (Archivo Bold, 1000 per em). */
const GLYPH = { x0: 76, x1: 678, h: 686 };
const FONT = 'https://fonts.gstatic.com/s/archivo/v25/k3k6o8UDI-1M0wlSV9XAw6lQkqWY8Q82sJaRE-NWIDdgffTT0zRp8A.ttf';

/** The mark on a 512 plate: corner radius `rx` (0 = full bleed) and the N `frac` of the plate high, centred. */
function compose(rx, frac, size) {
  const s = (frac * 512) / GLYPH.h;
  const tx = (512 - (GLYPH.x1 - GLYPH.x0) * s) / 2 - GLYPH.x0 * s;
  const ty = (512 - frac * 512) / 2 + frac * 512;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="${size}" height="${size}" style="display:block">` +
    `<rect width="512" height="512" rx="${rx}" fill="${PLATE}"/>` +
    `<path fill="${INK}" transform="translate(${tx.toFixed(2)} ${ty.toFixed(2)}) scale(${s.toFixed(5)} ${(-s).toFixed(5)})" d="${N_PATH}"/></svg>`;
}

/** any: the master (rounded plate, transparent corners, RGBA). maskable: full bleed, the N inside the 80% safe circle,
 * opaque RGBA. apple: full bleed, opaque RGB (iOS fills transparency with black and rounds the corners itself). */
const KINDS = {
  any: { rx: 96, frac: 0.56, mode: 'RGBA', note: 'rounded plate (96/512 corners), transparent corners, the N 56% of the side' },
  maskable: { rx: 0, frac: 0.40, mode: 'RGBA-opaque', note: 'full-bleed plate, the N 40% of the side inside the 80% safe circle' },
  'apple-touch': { rx: 0, frac: 0.50, mode: 'RGB', note: 'full-bleed opaque plate, the N 50% of the side' },
};
export const TARGETS = [
  { file: 'public/icons/companion-192.png', size: 192, kind: 'any' },
  { file: 'public/icons/companion-512.png', size: 512, kind: 'any' },
  { file: 'public/icons/companion-maskable-192.png', size: 192, kind: 'maskable' },
  { file: 'public/icons/companion-maskable-512.png', size: 512, kind: 'maskable' },
  { file: 'public/icons/apple-touch-icon.png', size: 180, kind: 'apple-touch' },
];

const PILLOW = `
import sys
from PIL import Image
src, out, size, mode = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
im = Image.open(src)
if im.size[0] < size or im.size[1] < size:
    sys.exit(f'{src} is {im.size}, smaller than {size}x{size}')
im = im.crop((0, 0, size, size))
if mode == 'RGB':
    im = im.convert('RGB')
else:
    im = im.convert('RGBA')
    if mode == 'RGBA-opaque':
        lo = im.getchannel('A').getextrema()[0]
        if lo != 255:
            sys.exit(f'{out}: a full-bleed icon must be opaque, min alpha {lo}')
im.save(out, optimize=True)
`;

function main() {
  const edgeVersion = execFileSync(EDGE, ['--version'], { encoding: 'utf-8' }).trim();
  const work = mkdtempSync(join(tmpdir(), 'nous-icons-'));
  for (const t of TARGETS) {
    const k = KINDS[t.kind];
    const html = join(work, `${t.size}-${t.kind}.html`);
    const shot = join(work, `${t.size}-${t.kind}.png`);
    writeFileSync(html, `<!doctype html><html style="background:transparent"><body style="margin:0;background:transparent">${compose(k.rx, k.frac, t.size)}</body></html>`);
    execFileSync(EDGE, ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--default-background-color=00000000',
      '--force-device-scale-factor=1', `--user-data-dir=${join(work, 'edge-profile')}`, `--window-size=${t.size},${t.size}`,
      `--screenshot=${shot}`, pathToFileURL(html).href], { stdio: 'ignore' });
    execFileSync('py', ['-I', '-c', PILLOW, shot, join(APP, t.file), String(t.size), k.mode], { stdio: 'inherit' });
    const recipe = `Rendered by headless ${edgeVersion} from dashboard-app/assets/icon-source/nous-mark.svg at ${t.size}x${t.size} ` +
      `(${t.kind}: ${k.note}; cropped and saved as ${k.mode === 'RGB' ? 'RGB' : 'RGBA'} with Pillow). Mark: the letter N in ` +
      `Archivo Bold (wght 700, wdth 100, SIL OFL 1.1, ${FONT}), outlined with fontTools, ${INK} on ${PLATE}. No generated ` +
      'imagery; brief: PRODUCT.md and .impeccable/surfaces/dashboard-app.md (own world: chart paper, Atlantic blue; no ' +
      'invented symbol).';
    console.log(`${t.file}\t${recipe}`);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
