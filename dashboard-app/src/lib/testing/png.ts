// A PNG reader for the icon tests (test-only; not imported by the app): the chunks, the header, the tEXt entries and,
// for an 8-bit non-interlaced RGB or RGBA image, the pixels.
import { inflateSync } from 'node:zlib';

export type Png = {
  width: number;
  height: number;
  /** 2 = RGB, 6 = RGBA. */
  colorType: number;
  bitDepth: number;
  interlace: number;
  chunks: string[];
  /** tEXt keyword -> text. */
  text: Record<string, string>;
  /** [r, g, b, a] at (x, y); a is 255 for an RGB image. */
  pixel: (x: number, y: number) => [number, number, number, number];
};

const SIGNATURE = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);

export function readPng(buf: Buffer): Png {
  if (!buf.subarray(0, 8).equals(SIGNATURE)) throw new Error('not a PNG');
  const chunks: string[] = [];
  const text: Record<string, string> = {};
  const idat: Buffer[] = [];
  let header: Buffer | null = null;
  for (let i = 8; i < buf.length; ) {
    const len = buf.readUInt32BE(i);
    const type = buf.toString('latin1', i + 4, i + 8);
    const data = buf.subarray(i + 8, i + 8 + len);
    chunks.push(type);
    if (type === 'IHDR') header = data;
    else if (type === 'IDAT') idat.push(data);
    else if (type === 'tEXt') {
      const nul = data.indexOf(0);
      text[data.toString('latin1', 0, nul)] = data.toString('latin1', nul + 1);
    }
    i += 12 + len;
  }
  if (!header) throw new Error('no IHDR');
  const width = header.readUInt32BE(0);
  const height = header.readUInt32BE(4);
  const bitDepth = header[8];
  const colorType = header[9];
  const interlace = header[12];
  let rows: Buffer | null = null;
  const channels = colorType === 6 ? 4 : colorType === 2 ? 3 : 0;
  const pixel = (x: number, y: number): [number, number, number, number] => {
    if (!channels || bitDepth !== 8 || interlace !== 0) throw new Error(`pixels of colour type ${colorType}, depth ${bitDepth}, interlace ${interlace} are not read`);
    rows ??= unfilter(inflateSync(Buffer.concat(idat)), width, height, channels);
    const o = (y * width + x) * channels;
    return [rows[o], rows[o + 1], rows[o + 2], channels === 4 ? rows[o + 3] : 255];
  };
  return { width, height, colorType, bitDepth, interlace, chunks, text, pixel };
}

/** The raw scanlines (each a filter byte and the row) back to plain pixel bytes (PNG filters 0 to 4). */
function unfilter(raw: Buffer, width: number, height: number, bpp: number): Buffer {
  const stride = width * bpp;
  const out = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)];
    const line = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? out[y * stride + x - bpp] : 0;
      const b = y > 0 ? out[(y - 1) * stride + x] : 0;
      const c = x >= bpp && y > 0 ? out[(y - 1) * stride + x - bpp] : 0;
      let v = line[x];
      if (filter === 1) v += a;
      else if (filter === 2) v += b;
      else if (filter === 3) v += (a + b) >> 1;
      else if (filter === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      } else if (filter !== 0) throw new Error(`unknown PNG filter ${filter}`);
      out[y * stride + x] = v & 0xff;
    }
  }
  return out;
}
