package us.fatykhov.nous.companion.data

import android.icu.text.NumberFormat
import android.icu.text.PluralRules
import android.icu.util.Currency
import android.icu.util.ULocale
import java.time.DayOfWeek
import java.time.Month
import java.time.format.TextStyle
import java.util.Locale
import us.fatykhov.nous.companion.core.Formatter

/** `:core.Formatter` on android.icu — the closest thing to V8's Intl (spec §3.2 formatting parity). */
object AndroidFormatter : Formatter {
    private val locale: ULocale get() = ULocale.getDefault()

    override fun number(v: Double, digits: Int?, grouping: Boolean): String {
        val nf = NumberFormat.getNumberInstance(locale)
        nf.isGroupingUsed = grouping
        nf.roundingMode = java.math.BigDecimal.ROUND_HALF_UP
        if (digits != null) { nf.minimumFractionDigits = digits; nf.maximumFractionDigits = digits }
        return nf.format(v)
    }

    override fun currency(v: Double, currency: String, digits: Int?, grouping: Boolean): String {
        val nf = NumberFormat.getCurrencyInstance(locale)
        nf.currency = Currency.getInstance(currency)   // throws on an unknown code → :core's fallback branch
        nf.isGroupingUsed = grouping
        nf.roundingMode = java.math.BigDecimal.ROUND_HALF_UP
        if (digits != null) { nf.minimumFractionDigits = digits; nf.maximumFractionDigits = digits }
        return nf.format(v)
    }

    override fun pluralCategory(n: Double): String = PluralRules.forLocale(locale).select(n)

    override fun monthName(month: Int, short: Boolean): String =
        Month.of(month).getDisplayName(if (short) TextStyle.SHORT else TextStyle.FULL, Locale.getDefault())

    override fun weekdayName(dow: Int, short: Boolean): String =
        DayOfWeek.of(dow).getDisplayName(if (short) TextStyle.SHORT else TextStyle.FULL, Locale.getDefault())
}
