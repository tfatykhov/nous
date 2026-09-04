import { describe, it, expect } from 'vitest';
import { isFigureValue, isTightUnit } from './figure';

// Required cases from the Codex P2 finding on PR #632, plus coverage for
// patterns that a 16-char cutoff could not distinguish.

describe('isFigureValue', () => {
  // ── prose ──────────────────────────────────────────────────────────────────

  it('"payment pending" is prose (short text, no numeric structure)', () => {
    // 15 chars — the old heuristic would have classified this as a figure
    expect(isFigureValue('payment pending')).toBe(false);
  });

  it('"deposit paid · EUR811.44 balance at check-in" is prose (multi-part sentence)', () => {
    expect(isFigureValue('deposit paid · EUR811.44 balance at check-in')).toBe(false);
  });

  it('free-form text with embedded numbers is still prose', () => {
    expect(isFigureValue('approved 3 of 5 requests')).toBe(false);
    expect(isFigureValue('down 4% from last week')).toBe(false);
  });

  // ── figures ────────────────────────────────────────────────────────────────

  it('"EUR 1,234,567.89" is a figure (3-letter currency code + number)', () => {
    // 17 chars — the old heuristic would have forced prose mode
    expect(isFigureValue('EUR 1,234,567.89')).toBe(true);
  });

  it('"-12.5%" is a figure (signed percentage)', () => {
    expect(isFigureValue('-12.5%')).toBe(true);
  });

  it('"2026-09-02T15:40:43Z" is a figure (ISO datetime is a scalar datum, not prose)', () => {
    // A timestamp is a preformatted moment in time — right-aligned tabular
    // treatment is correct; it should not force prose mode on the whole card.
    expect(isFigureValue('2026-09-02T15:40:43Z')).toBe(true);
  });

  it('ISO datetime with a zone OFFSET is a figure (aware-datetime serializers emit +00:00, not Z)', () => {
    expect(isFigureValue('2026-09-02T15:40:43+00:00')).toBe(true);
    expect(isFigureValue('2026-09-02T15:40:43-04:00')).toBe(true);
    expect(isFigureValue('2026-09-02T15:40:43.250+0200')).toBe(true);
    // A bare trailing sign is not an offset.
    expect(isFigureValue('2026-09-02T15:40:43+')).toBe(false);
  });

  it('rates and compound units are figures with or without a direction marker', () => {
    // The unit suffix is one shared definition: whatever "↑0.8 /day" accepts,
    // "0.8 /day" accepts too (codex on #632).
    expect(isFigureValue('0.8 /day')).toBe(true);
    expect(isFigureValue('3/wk')).toBe(true);
    expect(isFigureValue('12 km/h')).toBe(true);
    expect(isFigureValue('EUR 5 /mo')).toBe(true);
    expect(isFigureValue('↑0.8 /day')).toBe(true);
    // A slash followed by prose is not a rate.
    expect(isFigureValue('0.8 / day of rest')).toBe(false);
  });

  it('degree units with a scale letter are figures', () => {
    expect(isFigureValue('16 °C')).toBe(true);
    expect(isFigureValue('98.6°F')).toBe(true);
    expect(isFigureValue('−2 °C')).toBe(true);
    expect(isFigureValue('16 °Celsius')).toBe(false);
  });

  it('a sign on either side of a currency prefix is a figure, on both sides is not', () => {
    // Intl.NumberFormat puts the sign before the symbol; hand-written data after it.
    expect(isFigureValue('-$1,234.56')).toBe(true);
    expect(isFigureValue('−$3')).toBe(true);
    expect(isFigureValue('$-5')).toBe(true);
    expect(isFigureValue('-EUR 5')).toBe(true);
    expect(isFigureValue('EUR -5')).toBe(true);
    expect(isFigureValue('-$-5')).toBe(false);
  });

  it('suffix currency symbols as locale formatters emit them are figures', () => {
    expect(isFigureValue('42 €')).toBe(true);
    expect(isFigureValue('1.234,56 €')).toBe(true);
    expect(isFigureValue('1.234,56 €')).toBe(true); // de-DE Intl uses a no-break space
    expect(isFigureValue('-42 €')).toBe(true);
    expect(isFigureValue('€ 42')).toBe(true);
    expect(isFigureValue('42 € extra')).toBe(false);
  });

  it('localized grouping separators inside the number are figures', () => {
    expect(isFigureValue('1 234,56 €')).toBe(true); // fr-FR narrow no-break space
    expect(isFigureValue('1 234,56 €')).toBe(true); // ru-RU no-break space
    expect(isFigureValue("1'234.56")).toBe(true); // de-CH apostrophe
    expect(isFigureValue('1 234')).toBe(true);
    expect(isFigureValue('1 234 people')).toBe(false);
  });

  it('non-Latin decimal digits and their locale separators are figures', () => {
    expect(isFigureValue('١٬٢٣٤٫٥٦')).toBe(true); // ar-EG Intl output
    expect(isFigureValue('۱۲۳')).toBe(true); // fa-IR digits
    expect(isFigureValue('↑١٢ km')).toBe(true);
    // Units are short letters in any script, so a 3-letter Arabic word reads
    // as a unit (exactly as 'km' does); prose is a longer word.
    expect(isFigureValue('١٢ كيلومترات')).toBe(false);
  });

  it('threshold and approximation markers before a number are figures', () => {
    expect(isFigureValue('<5%')).toBe(true);
    expect(isFigureValue('≥95 bpm')).toBe(true);
    expect(isFigureValue('≤1.2 ms')).toBe(true);
    expect(isFigureValue('> $1,000')).toBe(true);
    expect(isFigureValue('~42')).toBe(true);
    expect(isFigureValue('±0.3 kg')).toBe(true);
    expect(isFigureValue('< five')).toBe(false);
  });

  it('compound currency affixes from locale formatters are figures', () => {
    expect(isFigureValue('R$1,234.56')).toBe(true); // en-US BRL
    expect(isFigureValue('CA$1,234.56')).toBe(true); // en-US CAD
    expect(isFigureValue('US$ 1.234,56')).toBe(true); // pt-BR USD
    expect(isFigureValue('-R$5')).toBe(true);
    expect(isFigureValue('42 US$')).toBe(true);
    expect(isFigureValue('ABCD$5')).toBe(false);
  });

  it('scientific notation is a figure', () => {
    expect(isFigureValue('1e6')).toBe(true);
    expect(isFigureValue('1.2e-6')).toBe(true);
    expect(isFigureValue('1E+09')).toBe(true);
    expect(isFigureValue('3e8 m/s')).toBe(true);
    // A dangling exponent marker is not a unit: the alpha-unit branch
    // refuses a bare e/E, so malformed notation stays prose.
    expect(isFigureValue('1e')).toBe(false);
    expect(isFigureValue('1e6e')).toBe(false);
    expect(isFigureValue('1 E')).toBe(false);
    expect(isFigureValue('e6')).toBe(false);
    expect(isFigureValue('1e+')).toBe(false);
  });

  it('symbol-first compound currency affixes and RTL bidi marks are handled', () => {
    expect(isFigureValue('1 234,56 $US')).toBe(true); // fr-FR USD
    expect(isFigureValue('$US 5')).toBe(true);
    expect(isFigureValue('؜-١٬٢٣٤٫٥٦')).toBe(true); // ar-EG negative: ALM before the minus
    expect(isFigureValue('‏١٬٢٣٤٫٥٦ US$')).toBe(true); // ar-EG USD: RLM prefix
    expect(isFigureValue('‏مرحبا')).toBe(false);
  });

  it('locale percent glyphs, accounting negatives and non-Latin units are figures', () => {
    expect(isFigureValue('%12')).toBe(true); // tr-TR percent prefix
    expect(isFigureValue('١٢٪')).toBe(true); // ar-EG percent U+066A
    expect(isFigureValue('12 ‰')).toBe(true);
    expect(isFigureValue('($1,234.56)')).toBe(true); // currencySign: accounting
    expect(isFigureValue('(US$1,234.56)')).toBe(true);
    expect(isFigureValue('(1 234,56 $US)')).toBe(true);
    expect(isFigureValue('(EUR 5)')).toBe(true);
    expect(isFigureValue('12 км')).toBe(true); // ru-RU unit style
    expect(isFigureValue('1,2 Mio.')).toBe(true); // de compact notation
    expect(isFigureValue('1.2万')).toBe(true); // ja compact notation
    expect(isFigureValue('(12')).toBe(false);
    expect(isFigureValue('12)')).toBe(false);
    expect(isFigureValue('12 километров')).toBe(false); // a word, not a unit
  });

  it('signed prefix percents and formatted infinity are figures', () => {
    expect(isFigureValue('-%12')).toBe(true); // tr-TR negative percent
    expect(isFigureValue('+%12')).toBe(true); // signDisplay: always
    expect(isFigureValue('-%-12')).toBe(false);
    expect(isFigureValue('∞')).toBe(true);
    expect(isFigureValue('-∞')).toBe(true);
    expect(isFigureValue('$∞')).toBe(true);
    expect(isFigureValue('∞%')).toBe(true);
    expect(isFigureValue('↑∞')).toBe(true);
    expect(isFigureValue('infinity')).toBe(false);
  });

  it('localized numeric dates are figures', () => {
    expect(isFigureValue('9/4/2026')).toBe(true); // en-US
    expect(isFigureValue('04/09/2026')).toBe(true); // en-GB
    expect(isFigureValue('04.09.2026')).toBe(true); // de
    expect(isFigureValue('2026/09/04 15:40')).toBe(true);
    expect(isFigureValue('9/4/2026, 3:40 PM')).toBe(true);
    expect(isFigureValue('9/4/2026/1')).toBe(false);
    expect(isFigureValue('9/4-2026')).toBe(false); // mixed separators
  });

  it('dates with a month word and formatted NaN are figures', () => {
    expect(isFigureValue('Sep 4, 2026')).toBe(true); // en-US medium
    expect(isFigureValue('4 Sept 2026')).toBe(true); // en-GB
    expect(isFigureValue('4. Sept. 2026')).toBe(true); // de
    expect(isFigureValue('4 sept. 2026')).toBe(true); // fr
    expect(isFigureValue('September 4, 2026, 3:40 PM')).toBe(true);
    expect(isFigureValue('Sep 4 2026 and more')).toBe(false);
    expect(isFigureValue('NaN')).toBe(true);
    expect(isFigureValue('$NaN')).toBe(true);
    expect(isFigureValue('NaN%')).toBe(true);
    // 'NaNs' is NaN with unit 's' by the unit rule; prose is a sentence.
    expect(isFigureValue('not a number')).toBe(false);
  });

  it('formatted ranges and any Unicode currency symbol are figures', () => {
    expect(isFigureValue('3–5')).toBe(true); // Intl formatRange
    expect(isFigureValue('3-5')).toBe(true);
    expect(isFigureValue('$3 – $5')).toBe(true);
    expect(isFigureValue('10%–20%')).toBe(true);
    expect(isFigureValue('EUR 3–EUR 5')).toBe(true);
    expect(isFigureValue('3–')).toBe(false);
    expect(isFigureValue('3–5–7')).toBe(false);
    expect(isFigureValue('-1\u00a0234,56 ₽')).toBe(true); // ru-RU
    expect(isFigureValue('-1,234.56 ₪')).toBe(true); // he-IL
    expect(isFigureValue('-1.235 ₫')).toBe(true); // vi-VN
    expect(isFigureValue('¢99')).toBe(true);
  });

  it('isTightUnit shares the percent family with the classifier', () => {
    for (const u of ['%', '％', '٪', '‰', '°C', '′']) expect(isTightUnit(u)).toBe(true);
    for (const u of ['kg', 'bpm', '€', '']) expect(isTightUnit(u)).toBe(false);
  });

  it('"—" is a figure (em-dash placeholder)', () => {
    expect(isFigureValue('—')).toBe(true);
  });

  // ── additional figure patterns ─────────────────────────────────────────────

  it('plain integers and decimals are figures', () => {
    expect(isFigureValue('42')).toBe(true);
    expect(isFigureValue('3.14')).toBe(true);
    expect(isFigureValue('-7')).toBe(true);
    expect(isFigureValue('+1.5')).toBe(true);
  });

  it('number with short unit is a figure', () => {
    expect(isFigureValue('65.0 bpm')).toBe(true);
    expect(isFigureValue('3 kg')).toBe(true);
    expect(isFigureValue('5 km')).toBe(true);
    expect(isFigureValue('12 h')).toBe(true);
    expect(isFigureValue('98.6°')).toBe(true);
  });

  it('currency-symbol-prefixed number is a figure', () => {
    expect(isFigureValue('$1,234.56')).toBe(true);
    expect(isFigureValue('€42')).toBe(true);
    expect(isFigureValue('£100')).toBe(true);
  });

  it('ratio is a figure', () => {
    expect(isFigureValue('12/30')).toBe(true);
    expect(isFigureValue('3/4')).toBe(true);
  });

  it('ISO date (date-only) is a figure', () => {
    expect(isFigureValue('2026-09-02')).toBe(true);
  });

  it('time of day is a figure', () => {
    expect(isFigureValue('14:30')).toBe(true);
    expect(isFigureValue('2:30 PM')).toBe(true);
    expect(isFigureValue('08:05:00')).toBe(true);
  });

  it('compact duration is a figure', () => {
    expect(isFigureValue('45s')).toBe(true);
    expect(isFigureValue('1h 30m')).toBe(true);
    expect(isFigureValue('2d')).toBe(true);
  });

  it('dash placeholders are figures', () => {
    expect(isFigureValue('-')).toBe(true);
    expect(isFigureValue('–')).toBe(true);
    expect(isFigureValue('—')).toBe(true);
    expect(isFigureValue('N/A')).toBe(true);
    expect(isFigureValue('n/a')).toBe(true);
  });

  it('empty string does not force prose mode', () => {
    expect(isFigureValue('')).toBe(true);
    expect(isFigureValue('   ')).toBe(true);
  });

  // ── length is not the deciding factor ─────────────────────────────────────

  it('length does not determine the result in either direction', () => {
    // Short (15 chars) but prose
    expect(isFigureValue('payment pending')).toBe(false);
    // Long (17 chars) but figure
    expect(isFigureValue('EUR 1,234,567.89')).toBe(true);
    // Long ISO datetime — figure, not prose
    expect(isFigureValue('2026-09-02T15:40:43Z')).toBe(true);
  });

  // ── directional delta figures (P2-1 Codex finding on PR #632) ─────────────
  // Every value from the f096-report-app fixture that carries a leading arrow.

  it('fixture values ↓0.03, ↑6 %, ↑0.02 are figures', () => {
    expect(isFigureValue('↓0.03')).toBe(true);
    expect(isFigureValue('↑6 %')).toBe(true);
    expect(isFigureValue('↑0.02')).toBe(true);
  });

  it('fixture value ↑0.8 /day is a figure (rate suffix)', () => {
    expect(isFigureValue('↑0.8 /day')).toBe(true);
  });

  it('fixture value ↑3 is a figure (bare integer delta)', () => {
    expect(isFigureValue('↑3')).toBe(true);
  });

  it('all four arrow glyphs and Unicode minus are accepted', () => {
    expect(isFigureValue('↑1.5')).toBe(true);
    expect(isFigureValue('↓1.5')).toBe(true);
    expect(isFigureValue('▲1.5')).toBe(true);
    expect(isFigureValue('▼1.5')).toBe(true);
    expect(isFigureValue('−1.5')).toBe(true); // U+2212 Unicode minus, not ASCII -
  });

  it('directional marker with /rate suffix variants', () => {
    expect(isFigureValue('↑0.8 /day')).toBe(true);
    expect(isFigureValue('↓1.2 /week')).toBe(true);
    expect(isFigureValue('▲3 /month')).toBe(true);
  });

  it('arrow-prefixed values that are still prose are not classified as figures', () => {
    // A full sentence with an embedded arrow is prose, not a delta figure.
    expect(isFigureValue('↑ improving trend over last 30 days')).toBe(false);
    // Two numbers after the arrow — cannot be a single delta
    expect(isFigureValue('↑6 and ↓3')).toBe(false);
  });
});
