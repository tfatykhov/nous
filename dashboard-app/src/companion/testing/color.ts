// Test-only colour maths for the companion's theme tests. Not imported by the app.
//
// WCAG 2.x relative luminance and contrast, OKLab distance, and colour-vision-deficiency simulation. One copy, so the
// contrast tests cannot drift apart (theme.test.ts once weighted blue by 0.4152 instead of 0.0722).

/** [r, g, b, a] of a hex (3, 4, 6 or 8 digits) or an `rgb()`/`rgba()` colour; r, g, b in 0..255, a in 0..1. */
export function parseColor(c: string): [number, number, number, number] {
  const s = c.trim().toLowerCase();
  const hex = /^#([0-9a-f]{3,8})$/.exec(s);
  if (hex) {
    let h = hex[1];
    if (h.length === 3 || h.length === 4) h = [...h].map((x) => x + x).join('');
    if (h.length !== 6 && h.length !== 8) throw new Error(`not a colour: ${c}`);
    const n = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
    const a = h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1;
    return [n[0], n[1], n[2], a];
  }
  const rgb = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/.exec(s);
  if (rgb) {
    const alpha = rgb[4] === undefined ? 1 : rgb[4].endsWith('%') ? parseFloat(rgb[4]) / 100 : parseFloat(rgb[4]);
    return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3]), alpha];
  }
  throw new Error(`not a colour: ${c}`);
}

/** `#rrggbb` of an opaque colour (rounded). */
export function toHex([r, g, b]: number[]): string {
  return '#' + [r, g, b].map((v) => Math.round(Math.min(255, Math.max(0, v))).toString(16).padStart(2, '0')).join('');
}

/** A colour with alpha painted over an opaque backdrop (source-over, in gamma-encoded sRGB, as browsers paint). */
export function over(fg: string, bg: string): string {
  const [r, g, b, a] = parseColor(fg);
  const [br, bg2, bb, ba] = parseColor(bg);
  if (ba !== 1) throw new Error(`backdrop must be opaque: ${bg}`);
  return toHex([r * a + br * (1 - a), g * a + bg2 * (1 - a), b * a + bb * (1 - a)]);
}

/** `color-mix(in srgb, a p%, b)` painted over an opaque backdrop. `b` may be `transparent`: the mix is then `a` at
 * alpha p (premultiplied interpolation), which is what the companion's tinted pills and badges use. */
export function mix(a: string, pct: number, b: string, backdrop: string): string {
  const p = pct / 100;
  const ca = parseColor(a);
  const cb = b === 'transparent' ? [0, 0, 0, 0] : parseColor(b);
  const alpha = ca[3] * p + cb[3] * (1 - p);
  if (alpha === 0) return over(backdrop, backdrop);
  const ch = [0, 1, 2].map((i) => (ca[i] * ca[3] * p + cb[i] * cb[3] * (1 - p)) / alpha);
  return over(`rgba(${ch[0]}, ${ch[1]}, ${ch[2]}, ${alpha})`, backdrop);
}

function linear(v: number): number {
  const s = v / 255;
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

/** WCAG relative luminance of an opaque colour (coefficients 0.2126, 0.7152, 0.0722). */
export function hexLuminance(c: string): number {
  const [r, g, b, a] = parseColor(c);
  if (a !== 1) throw new Error(`luminance needs an opaque colour (composite it first): ${c}`);
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

/** WCAG contrast ratio of two opaque colours. */
export function contrast(a: string, b: string): number {
  const [l1, l2] = [hexLuminance(a), hexLuminance(b)].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
}

function oklabLinear([r, g, b]: number[]): [number, number, number] {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}

/** OKLab (Björn Ottosson) of an opaque sRGB colour. */
export function oklab(c: string): [number, number, number] {
  return oklabLinear(parseColor(c).slice(0, 3).map(linear));
}

/** Colour-vision deficiency (Machado, Oliveira and Fernandes 2009, severity 1), applied to linear sRGB.
 * A copy of the matrices in src/tokens.test.ts (the dashboard's), so both apps measure with the same numbers. */
export const CVD: Record<'protan' | 'deutan', number[][]> = {
  protan: [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
  deutan: [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.01182, 0.04294, 0.968881]],
};

/** OKLab distance (times 100) between two opaque colours, for normal vision or under a simulated deficiency. */
export function cvdDistance(a: string, b: string, cvd?: 'protan' | 'deutan'): number {
  const see = (c: string) => {
    const lin = parseColor(c).slice(0, 3).map(linear);
    if (!cvd) return oklabLinear(lin);
    return oklabLinear(CVD[cvd].map((row) => Math.min(1, Math.max(0, row[0] * lin[0] + row[1] * lin[1] + row[2] * lin[2]))));
  };
  const [x, y] = [see(a), see(b)];
  return 100 * Math.hypot(x[0] - y[0], x[1] - y[1], x[2] - y[2]);
}
