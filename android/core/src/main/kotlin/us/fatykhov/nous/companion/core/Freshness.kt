package us.fatykhov.nous.companion.core

import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeParseException

/** Port of `freshness.ts` — the micro-app AppHeader stamp (spec §3.2 R10). */
data class Freshness(val label: String, val stale: Boolean)

object FreshnessFormatter {
    const val DEFAULT_STALE_AFTER_S = 3600.0

    /**
     * [composedAtIso] is parsed with OffsetDateTime so both `Z` and `+00:00`
     * (what production `compose.py` emits) are accepted; JS `Date.parse` is
     * the reference and accepts both.
     */
    fun format(composedAtIso: String, nowMs: Long, staleAfterS: Double = DEFAULT_STALE_AFTER_S): Freshness {
        val composed = try {
            OffsetDateTime.parse(composedAtIso).withOffsetSameInstant(ZoneOffset.UTC)
        } catch (_: DateTimeParseException) {
            return Freshness("composed at unknown time", stale = true)
        }
        val composedMs = composed.toInstant().toEpochMilli()
        val ageS = maxOf(0.0, (nowMs - composedMs) / 1000.0)
        val threshold = if (staleAfterS > 0) staleAfterS else DEFAULT_STALE_AFTER_S
        val stale = ageS >= threshold
        val ago = when {
            ageS < 60 -> "just now"
            ageS < 3600 -> "${(ageS / 60).toInt()}m ago"
            ageS < 86400 -> "${(ageS / 3600).toInt()}h ago"
            else -> "${(ageS / 86400).toInt()}d ago"
        }
        val hh = composed.hour.toString().padStart(2, '0')
        val mm = composed.minute.toString().padStart(2, '0')
        return Freshness("composed $hh:$mm UTC · $ago", stale)
    }
}
