// ScoreCard value classifier: decides whether a row value is a FIGURE (a
// preformatted scalar datum) or PROSE (descriptive text). Length is NOT a
// criterion in either direction.
//
// A value is a FIGURE when its entire content matches one of:
//   placeholder  : —, –, N/A, TBD, -
//   ISO date/datetime : 2026-09-02, 2026-09-02T15:40:43Z, 2026-09-02T15:40:43+00:00
//   localized date : 9/4/2026, 04/09/2026, 04.09.2026, 2026/09/04 15:40,
//               Sep 4, 2026, 4 Sept 2026, 4. Sept. 2026
//   time of day  : 14:30, 2:30 PM, ۱۴:۳۰, ٢:٣٠ م
//   ratio        : 12/30
//   compound duration: 1h 30m, 2d 3h, 45s
//   number with optional comparison marker (<, ≥, ~, ±), sign, currency
//               symbol or 3-letter code, thousands
//               separators, decimal, and a short trailing unit (kg, km, %, °…),
//               a compound unit (km/h) or a /rate suffix (0.8 /day, 3/wk)
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

// A clock time in any script's digits (fa "۱۴:۳۰", bn "২:৩০"), optionally
// followed by a day period — Latin "AM"/"p.m." or a locale's own one-to-two
// letter marker (ar "م" / "ص"). One definition, used standalone and as the
// optional tail of a date.
const DAY_PERIOD = String.raw`(?:\s?(?:[APap]\.?[Mm]\.?|\p{L}\p{M}*(?:\p{L}\p{M}*)?\.?))?`;
const CLOCK = String.raw`\p{Nd}{1,2}:\p{Nd}{2}(?::\p{Nd}{2})?${DAY_PERIOD}`;
const TIME_OF_DAY = new RegExp(String.raw`^${CLOCK}$`, 'u');

// A localized numeric date: three digit fields joined by one separator kind
// (en-US 9/4/2026, en-GB 04/09/2026, de 04.09.2026, ISO-ish 2026/09/04), with
// an optional time of day after a space or comma.
const TIME_SUFFIX = String.raw`(?:[ ,]+${CLOCK})?`;
const NUMERIC_DATE = new RegExp(
  String.raw`^\p{Nd}{1,4}([./-])\p{Nd}{1,2}\1\p{Nd}{1,4}${TIME_SUFFIX}$`,
  'u',
);

// A localized date with a month WORD (any script, optionally abbreviated with
// a period): en-US "Sep 4, 2026", en-GB "4 Sept 2026", de "4. Sept. 2026",
// fr "4 sept. 2026", plus the same optional time of day.
const MONTH = String.raw`\p{L}[\p{L}\p{M}]{2,11}\.?`;
const WORDY_DATE = new RegExp(
  String.raw`^(?:${MONTH} \p{Nd}{1,2},? \p{Nd}{4}|\p{Nd}{1,2}\.? ${MONTH} \p{Nd}{4})${TIME_SUFFIX}$`,
  'u',
);

const RATIO = /^\p{Nd}+\/\p{Nd}+$/u;

// One or more <digits><unit-char> groups, e.g. "1h 30m", "2d", "45s".
const DURATION = /^(\p{Nd}+\s?[dhms]\s?)+$/u;

// A sign (ASCII or Unicode minus) may sit on EITHER side of a currency
// prefix — Intl.NumberFormat emits "-$1,234.56", hand-written data "$-5" —
// but not on both: `-$-5` is not a figure.
const SIGN = String.raw`[+\-−]`;

// A currency symbol — ANY Unicode currency symbol (\p{Sc}: $, €, £, ¥, ₽,
// ₪, ₫, ₹, ฿, ₩, ¢, …), not a hand-picked subset — optionally COMPOUND as
// locale formatters emit it: R$ (BRL), CA$ / US$ / A$ / HK$ (disambiguated
// dollars), or symbol-first in French-style locales (fr-FR USD is "$US") —
// up to three capitals glued to one side of the symbol.
const SYMBOL = String.raw`(?:[A-Z]{1,3}\p{Sc}|\p{Sc}(?:[A-Z]{1,3})?)`;

// A threshold / approximation marker may lead a figure: "<5%", "≥95 bpm",
// "≤1.2 ms", "~42", "±0.3 kg". Optional whitespace after it ("< 5").
const COMPARE = String.raw`(?:[<>≤≥~≈±]\s?)?`;

// Percent-family glyphs as locales emit them: ASCII and full-width percent,
// Arabic percent U+066A, per-mille (‰, Arabic U+0609), per-myriad (‱, Arabic
// U+060A). tr-TR puts the percent sign BEFORE the number ("%12"), so the
// glyph is admitted as a prefix too.
const PERCENT = String.raw`[%％٪‰؉‱؊]`;

/** True when a unit string should be set tight against its number: the
 *  percent family (any locale's glyph), degree, prime and double prime.
 *  One definition for the classifier AND the card views, so a locale
 *  percent that classifies as a figure also renders without the gap. */
export function isTightUnit(unit: string): boolean {
  return new RegExp(String.raw`^(?:${PERCENT}|[°′″])`).test(unit);
}

// One optional unit suffix, shared by every numeric pattern so a unit
// accepted after "↑0.8" is also accepted after "0.8" (codex on #632):
//   a percent-family glyph or a special symbol (°, ′, ″) with an optional
//   short scale letter (°C, °F); a unit WORD in any script — abbreviated
//   (kg, bpm, ru "км", de "Mio.", ja "万") or spelled out as
//   `unitDisplay: "long"` emits it ("kilometers", "kilometers per hour": up
//   to three words) — optionally compounded with a slash (km/h) or ending in
//   an abbreviation period; a bare /rate (/day, /wk); or a SUFFIX currency
//   symbol as locale formatters emit it ("42 €", de-DE "1.234,56 €" — `\s`
//   also matches the no-break space Intl puts there). The figure/prose line
//   is therefore: a number followed by at most three words is a figure; a
//   longer tail is prose.
//   A bare `e`/`E` is NOT a unit: it is a dangling exponent marker ("1e",
//   "1e6e"), and letting the word branch swallow it would classify
//   malformed notation as a figure.
// A word is a letter followed by letters or COMBINING MARKS (\p{M}): in
// Devanagari, Bengali, Thai and friends the vowel signs are marks, not
// letters, so hi-IN "मेगाबाइट" would otherwise fail on its second character.
const WORD = String.raw`\p{L}[\p{L}\p{M}]{0,19}`;
const UNIT = String.raw`(\s?(?:${PERCENT}|[°′″])[A-Za-z]{0,2}|\s?(?![eE](?:$|\/))${WORD}(?:\/${WORD}|\.|(?:\s${WORD}){1,2})?|\s?\/${WORD}|\s?${SYMBOL})?`;
// With a prefix word present the suffix may carry at most two words, so a
// value never exceeds three words around its number.
const UNIT_AFTER_PREFIX = String.raw`(\s?(?:${PERCENT}|[°′″])[A-Za-z]{0,2}|\s?(?![eE](?:$|\/))${WORD}(?:\/${WORD}|\.|\s${WORD})?|\s?\/${WORD}|\s?${SYMBOL})?`;

// Digits with optional grouping and decimal separators, ending on a digit.
// A digit is any Unicode decimal digit (\p{Nd}, `u` flag) — ar-EG and fa-IR
// formatters emit ١٢٣ / ۱۲۳, and JS `\d` is ASCII-only. Grouping/decimal is
// whatever a locale formatter emits: comma, period, apostrophe (de-CH
// 1'234.56), the Arabic separators U+066C / U+066B (١٬٢٣٤٫٥٦), or a space —
// fr-FR uses U+202F, ru-RU U+00A0, hand-typed data a plain space — all of
// which `\s` matches. Ending on a digit is what lets UNIT's own optional
// whitespace claim the gap before a suffix. An optional exponent (1e6,
// 1.2e-6, 1E+09) follows the mantissa.
const DIGITS = String.raw`\p{Nd}(?:[\p{Nd}.,'’\s\u066b\u066c]*\p{Nd})?(?:[eE][+\-−]?\p{Nd}+)?`;

// What sits where the number goes: digits, or what a formatter emits for a
// non-finite metric — "∞" / "-∞" / "$∞" / "∞%", and "NaN" / "$NaN" / "NaN%"
// for an indeterminate one — with the same affixes as digits.
const MANTISSA = String.raw`(?:${DIGITS}|∞|NaN)`;

// Three shapes for every numeric core: the core itself; a formatted RANGE of
// two cores joined by a dash (Intl.NumberFormat.formatRange: "3–5", "$3 – $5",
// "10%–20%"); and the accounting style that wraps a negative in parentheses
// ("($1,234.56)", "(1 234,56 $US)").
const shapes = (core: string) =>
  String.raw`^(?:${core}(?:\s?[–—-]\s?${core})?|\(${core}\))$`;

// Leading affixes, then the mantissa, then the unit. The sign sits either
// BEFORE the prefix affixes (tr-TR "-%12", Intl "-$1,234.56") or AFTER a
// currency symbol ("$-5"), never on both sides ("-$-5" is not a figure).
// Covers: 42, -12.5%, %12, -%12, $1,234.56, -$1,234.56, $-5, 42 €,
// ($1,234.56), ∞, 65.0 bpm, 0.8 /day.
// Some locales put a unit WORD before the number (ja "時速 12.3 キロメートル",
// zh/ko similar), so one leading word is admitted ahead of the affixes; the
// "at most three words around a number" line still bounds the whole value.
const AFFIXES = String.raw`${COMPARE}(?:${SIGN}?(?:${PERCENT}\s?)?(?:${SYMBOL}\s?)?|(?:${PERCENT}\s?)?${SYMBOL}\s?${SIGN}?)${MANTISSA}`;
const NUMBER_CORE = String.raw`(?:${AFFIXES}${UNIT}|${WORD}\s${AFFIXES}${UNIT_AFTER_PREFIX})`;
const NUMBER = new RegExp(shapes(NUMBER_CORE), 'u');

// 3-letter ISO currency code (space optional) with a sign on either side.
// Covers: EUR 1,234,567.89, USD100, -EUR 5, EUR -5, (EUR 5), EUR 5 /mo.
const CURRENCY_CODE_CORE = String.raw`${COMPARE}(?:${SIGN}?[A-Z]{3}\s?|[A-Z]{3}\s?${SIGN}?)${MANTISSA}${UNIT}`;
const CURRENCY_CODE = new RegExp(shapes(CURRENCY_CODE_CORE), 'u');

// Directional delta marker (↑ ↓ ▲ ▼ or Unicode minus −) followed by a number
// and an optional unit. Covers: ↓0.03, ↑6 %, ↑0.8 /day, ▲3. ASCII +/- are
// already handled by NUMBER; this pattern covers the arrow chars and Unicode
// minus that NUMBER's [+-] class cannot match.
const DIRECTIONAL = new RegExp(String.raw`^[↑↓▲▼−]\s?${MANTISSA}${UNIT}$`, 'u');

const PATTERNS = [
  PLACEHOLDER,
  ISO_DATE,
  NUMERIC_DATE,
  WORDY_DATE,
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
// Invisible bidirectional formatting controls that RTL-locale formatters
// embed (ar-EG emits U+061C before a minus, U+200F before a positive value):
// ALM, LRM/RLM, the embedding/override controls and the isolate controls.
// They carry no figure/prose information, so they are dropped before the
// anchored patterns see the string.
const BIDI_MARKS = /[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g;

export function isFigureValue(value: string): boolean {
  const v = value.replace(BIDI_MARKS, '').trim();
  return v === '' || PATTERNS.some((re) => re.test(v));
}
