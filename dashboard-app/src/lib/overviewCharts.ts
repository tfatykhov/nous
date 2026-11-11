// The Overview charts' colours, read from the ground's tokens. Chart.js draws on a canvas and cannot read var(), so
// the colours are resolved against the chart's own element when the chart is built, and again when the look changes:
// the dark look gets today's colours exactly, the paper look the paper palette. Token names follow the PR 2/3 plan
// (status hues for outcomes, `--series-7` for pending, `--series-0` for marks that were violet: not the accent, whose
// paper value is the question frame's blue).
// PR 2's `lib/chartColors.ts` is meant to generalise this for every chart.

/** Outcome label (lower case) to its token; any other label is pending's. Success and failure are marks that stay apart
 * from partial and from each other under red-green colour blindness (redesign PR 4 review I2); Decisions shares the map. */
const OUTCOME_TOKENS: Record<string, string> = {
  success: '--oc-success',
  partial: '--yellow',
  failure: '--err-text',
  pending: '--series-7',
};

/** A custom property's value on `el` (inherited from its ground), trimmed; '' when unset. */
export function readToken(el: Element, name: string): string {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

/** `#7c6af7` or `rgb(…)` at `alpha`, as `rgba(…)`; anything else is returned unchanged. */
export function withAlpha(color: string, alpha: number): string {
  const hex = color.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (hex) {
    const h = hex[1].length === 3 ? [...hex[1]].map((x) => x + x).join('') : hex[1];
    return `rgba(${parseInt(h.slice(0, 2), 16)}, ${parseInt(h.slice(2, 4), 16)}, ${parseInt(h.slice(4, 6), 16)}, ${alpha})`;
  }
  const rgb = color.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)/i);
  if (rgb) return `rgba(${rgb[1]}, ${rgb[2]}, ${rgb[3]}, ${alpha})`;
  return color;
}

export interface ChartPalette {
  /** The doughnut segment of a decision outcome. */
  outcome: (label: string) => string;
  /** The edge-type bars: fill and border. */
  bar: string;
  barBorder: string;
}

/** The palette from a token reader (`(name) => readToken(el, name)` in the app). */
export function chartPalette(read: (name: string) => string): ChartPalette {
  const violet = read('--series-0');
  return {
    outcome: (label) => {
      const k = label.toLowerCase();
      return read(Object.hasOwn(OUTCOME_TOKENS, k) ? OUTCOME_TOKENS[k] : OUTCOME_TOKENS.pending);
    },
    bar: withAlpha(violet, 0.5),
    barBorder: violet,
  };
}
