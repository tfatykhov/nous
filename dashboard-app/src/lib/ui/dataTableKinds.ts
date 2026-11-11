// A DataTable column's kind, read off its values (finish review fix 3): a number column is right-aligned in tabular
// figures, a time column never wraps. A column may also declare its kind.

export type ColumnKind = 'num' | 'time' | 'text';

/** A count, a decimal, a grouped number, a percentage, a size or duration with its unit, or a fraction "3 / 5". */
const NUMBER = /^[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(?:%|ms|s|m|h|d|k|K|M|B|x)?$|^\d+\s*\/\s*\d+$/;
/** fmtUtc's "Nov 10 09:31 UTC" (optionally "· 3h ago"), or a relative time. */
const TIME = /^(?:[A-Z][a-z]{2} \d{1,2} \d{2}:\d{2} UTC(?: · .+)?|\d+[smhd] ago|just now)$/;

export function columnKind(values: unknown[]): ColumnKind {
  const present = values.filter((v) => v !== null && v !== undefined && v !== '');
  if (present.length === 0) return 'text';
  if (present.every((v) => (typeof v === 'number' && Number.isFinite(v)) || (typeof v === 'string' && NUMBER.test(v.trim())))) {
    return 'num';
  }
  if (present.every((v) => typeof v === 'string' && TIME.test(v.trim()))) return 'time';
  return 'text';
}
