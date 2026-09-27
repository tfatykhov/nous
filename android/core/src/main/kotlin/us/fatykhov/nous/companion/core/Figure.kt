package us.fatykhov.nous.companion.core

/**
 * ScoreCard value classifier (port of `figure.ts`): is a row value a FIGURE
 * (preformatted scalar datum) or PROSE? Length is never a criterion. The
 * regexes are copied verbatim; Java's `\p{L}`/`\p{Nd}`/`\p{Sc}`/`\p{M}` match
 * JS `u`-mode semantics for these classes.
 */
object Figure {
    /** JS `u`-mode parity: `\s` must match NBSP/NNBSP and `\p{…}` classes are Unicode-aware. */
    private fun ure(pattern: String): Regex =
        java.util.regex.Pattern.compile(pattern, java.util.regex.Pattern.UNICODE_CHARACTER_CLASS).toRegex()
    /** The two web patterns compiled WITHOUT the `u` flag: `\d` must stay ASCII there (a JS invariant). */
    private fun ascii(pattern: String): Regex = java.util.regex.Pattern.compile(pattern).toRegex()

    private const val PLACEHOLDER = """^([—–−-]{1,3}|[Nn]/[Aa]|TBD)$"""
    private fun ranged(core: String) = """$core(?:\s?[–—-]\s?$core)?"""
    private const val ISO_DATE_CORE = """\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?([.,]\d+)?(Z|[+-]\d{2}:?\d{2})?)?"""
    private val ISO_DATE = ascii("^${ranged(ISO_DATE_CORE)}$")
    private const val PERIOD_WORD = """(?:[APap]\.?[Mm]\.?|\p{L}\p{M}*(?:\p{L}\p{M}*)?\.?)"""
    private const val DAY_PERIOD = """(?:\s?$PERIOD_WORD)?"""
    private const val CLOCK = """(?:$PERIOD_WORD\s?)?\p{Nd}{1,2}:\p{Nd}{2}(?::\p{Nd}{2})?$DAY_PERIOD"""
    private val TIME_OF_DAY = ure("^${ranged(CLOCK)}$")
    private const val TIME_SUFFIX = """(?:[ ,]+$CLOCK)?"""
    private const val NUMERIC_DATE_CORE = """\p{Nd}{1,4}(?:[./-])\p{Nd}{1,2}(?:[./-])\p{Nd}{1,4}$TIME_SUFFIX"""
    private val NUMERIC_DATE = ure("^${ranged(NUMERIC_DATE_CORE)}$")
    private const val MONTH = """\p{L}(?:[\p{L}\p{M}]|\.(?=[\p{L}\p{M}]))(?:[\p{L}\p{M}]|\.(?=[\p{L}\p{M}]))*[.॰]?"""
    private const val WORDY_DATE_CORE = """(?:$MONTH \p{Nd}{1,2},? \p{Nd}{4}|\p{Nd}{1,2}\.? $MONTH,? \p{Nd}{4})$TIME_SUFFIX"""
    private val WORDY_DATE = ure("^${ranged(WORDY_DATE_CORE)}$")
    private const val DASH = """\s?[–—-]\s?"""
    private val COLLAPSED_DATE_RANGE = ure(
        """^(?:\p{Nd}{1,2}(?:[./-]\p{Nd}{1,2})?[./-]?$DASH$NUMERIC_DATE_CORE|\p{Nd}{1,2}\.?$DASH\p{Nd}{1,2}\.? $MONTH \p{Nd}{4}|$MONTH \p{Nd}{1,2}$DASH\p{Nd}{1,2},? \p{Nd}{4})$TIME_SUFFIX$""",
    )
    private val RATIO = ure("""^\p{Nd}+/\p{Nd}+$""")
    private val DURATION = ure("""^(\p{Nd}+\s?[dhms]\s?)+$""")
    private const val SIGN = """[+\-−]"""
    private const val SYMBOL = """(?:[A-Z]{1,3}\p{Sc}|\p{Sc}(?:[A-Z]{1,3})?)"""
    private const val COMPARE = """(?:[<>≤≥~≈±]\s?)?"""
    private const val PERCENT = """[%％٪‰؉‱؊]"""
    private const val WORD = """\p{L}[\p{L}\p{M}]{0,19}"""
    private const val CONNECTOR = """(?:per|pro|par|por|al|all|na|в|на|за)"""
    private const val UNIT = """(\s?(?:$PERCENT|[°′″])(?:\p{L}\p{M}*){0,2}|\s?(?![eE](?:$|/))$WORD\.?(?:/$WORD\.?|\s$CONNECTOR\s$WORD\.?)?|\s?/$WORD\.?|\s?$SYMBOL)?"""
    private const val UNIT_AFTER_PREFIX = """(\s?(?:$PERCENT|[°′″])(?:\p{L}\p{M}*){0,2}|\s?(?![eE](?:$|/))$WORD\.?(?:/$WORD\.?)?|\s?/$WORD\.?|\s?$SYMBOL)?"""
    // Grouping/decimal separators incl. Arabic U+066B / U+066C; `\s` matches NBSP/NNBSP under UNICODE_CHARACTER_CLASS (JS parity).
    private const val DIGITS = """\p{Nd}(?:[\p{Nd}.,'’\s٫٬]*\p{Nd})?(?:[eE][+\-−]?\p{Nd}+)?"""
    private const val MANTISSA = """(?:$DIGITS|∞|NaN)"""
    private fun shaped(core: String) = """(?:${ranged(core)}|\($core\))"""
    private fun shapes(core: String) = "^${shaped(core)}$"
    private const val AFFIXES = """$COMPARE(?:$SIGN?(?:$PERCENT\s?)?(?:$SYMBOL\s?)?|(?:$PERCENT\s?)?$SYMBOL\s?$SIGN?)$MANTISSA"""
    private const val NUMBER_CORE = """(?:$AFFIXES$UNIT|$WORD\s$AFFIXES$UNIT_AFTER_PREFIX)"""
    private val NUMBER = ure(shapes(NUMBER_CORE))
    private const val CURRENCY_CODE_CORE = """$COMPARE(?:$SIGN?[A-Z]{3}\s?|[A-Z]{3}\s?$SIGN?)$MANTISSA$UNIT"""
    private val CURRENCY_CODE = ure(shapes(CURRENCY_CODE_CORE))
    private val DIRECTIONAL = ure("""^[↑↓▲▼]\s?(?:${shaped(NUMBER_CORE)}|${shaped(CURRENCY_CODE_CORE)})$""")
    private val PATTERNS = listOf(
        ascii(PLACEHOLDER), ISO_DATE, NUMERIC_DATE, WORDY_DATE, COLLAPSED_DATE_RANGE,
        TIME_OF_DAY, RATIO, DURATION, NUMBER, CURRENCY_CODE, DIRECTIONAL,
    )
    // ALM, LRM/RLM, embedding/override controls, isolate controls (written as escapes: they are invisible).
    private val BIDI = ure("[؜‎‏‪-‮⁦-⁩]")

    fun isTightUnit(unit: String): Boolean = ure("""^(?:$PERCENT|[°′″])""").containsMatchIn(unit)

    fun isFigureValue(value: String): Boolean {
        val v = value.replace(BIDI, "").trim()
        return v == "" || PATTERNS.any { it.matches(v) }
    }
}
