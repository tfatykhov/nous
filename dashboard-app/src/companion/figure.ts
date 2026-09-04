// ScoreCard value classifier: decides whether a row value is a FIGURE (a
// preformatted scalar datum) or PROSE (descriptive text). Length is NOT a
// criterion in either direction.
//
// A value is a FIGURE when its entire content matches one of:
//   placeholder  : —, –, N/A, TBD, -
//   ISO date/datetime : 2026-09-02, 2026-09-02T15:40:43Z, 2026-09-02T15:40:43+00:00
//   time of day  : 14:30, 2:30 PM
//   ratio        : 12/30
//   compound duration: 1h 30m, 2d 3h, 45s
//   number with optional sign, currency symbol or 3-letter code, thousands
//               separators, decimal, and a short trailing unit (kg, km, %, °…)
//   directional delta: ↓0.03, ↑6 %, ↑0.8 /day, ▲3 — a leading arrow (↑↓▲▼)
//               or Unicode minus (−) followed by a bare number, a special
//               unit symbol, an alpha unit, or a /rate suffix
//
// ISO datetimes are figures, not prose: they are preformatted scalar datums
// (a moment in time), not descriptive sentences. The old 16-char heuristic
// tripped on them, forcing prose mode on timestamp evidence rows.

const PLACEHOLDER = /^([—–−-]{1,3}|[Nn]\/[Aa]|TBD)$/;

// Zone suffix: `Z` or an offset (`+00:00`, `-04:00`, `+0200`) — aware-datetime
// serializers emit the offset form, not `Z`.
const ISO_DATE =
  /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?([.,]\d+)?(Z|[+-]\d{2}:?\d{2})?)?$/;

const TIME_OF_DAY = /^\d{1,2}:\d{2}(:\d{2})?(\s?[APap][Mm])?$/;

const RATIO = /^\d+\/\d+$/;

// One or more <digits><unit-char> groups, e.g. "1h 30m", "2d", "45s".
const DURATION = /^(\d+\s?[dhms]\s?)+$/;

// Optional currency symbol, optional sign, digits with optional thousands
// separators and decimal, optional short trailing unit (≤5 alpha chars or a
// special symbol). Covers: 42, -12.5%, $1,234.56, 65.0 bpm.
const NUMBER = /^[€$£¥₹฿₩]?[+-]?\d[\d,.]*([%°‰′″]|\s?[A-Za-z]{1,5})?$/;

// 3-letter ISO currency code (space optional) followed by a number.
// Covers: EUR 1,234,567.89, USD100.
const CURRENCY_CODE = /^[A-Z]{3}\s?[+-]?\d[\d,.]*([%°‰′″]|\s?[A-Za-z]{1,5})?$/;

// Directional delta marker (↑ ↓ ▲ ▼ or Unicode minus −) followed by a number
// and an optional unit or /rate suffix. Covers: ↓0.03, ↑6 %, ↑0.8 /day, ▲3.
// ASCII +/- are already handled by NUMBER; this pattern covers the arrow chars
// and Unicode minus that NUMBER's [+-] class cannot match.
const DIRECTIONAL =
  /^[↑↓▲▼−]\s?\d[\d,.]*(\s?[%°‰′″]|\s?[A-Za-z]{1,5}|\s?\/[A-Za-z]{1,10})?$/;

const PATTERNS = [
  PLACEHOLDER,
  ISO_DATE,
  TIME_OF_DAY,
  RATIO,
  DURATION,
  NUMBER,
  CURRENCY_CODE,
  DIRECTIONAL,
];

/** Returns true when the value is figure-shaped (number, date, time, ratio,
 *  placeholder).  An empty string is treated as a figure so it does not
 *  force stacked mode on a card that has no real prose.
 *
 *  Explicit producer hints (`format: 'figure' | 'prose'` on a row or on the
 *  ScoreCard component) override this inference and are applied by the
 *  caller, not here. */
export function isFigureValue(value: string): boolean {
  const v = value.trim();
  return v === '' || PATTERNS.some((re) => re.test(v));
}
