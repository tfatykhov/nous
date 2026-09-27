package us.fatykhov.nous.companion.core

import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class FreshnessTest {
    private val t0 = Instant.parse("2026-08-29T13:34:00Z").toEpochMilli()
    private val stamp = "2026-08-29T13:34:00Z"

    @Test fun justNowWithinFirstMinute() {
        val f = FreshnessFormatter.format(stamp, t0 + 30_000, 3600.0)
        assertEquals("composed 13:34 UTC · just now", f.label)
        assertFalse(f.stale)
    }

    @Test fun minutesAndHours() {
        assertTrue(FreshnessFormatter.format(stamp, t0 + 5 * 60_000, 3600.0).label.contains("5m ago"))
        assertTrue(FreshnessFormatter.format(stamp, t0 + 2 * 3600_000, 7201.0).label.contains("2h ago"))
    }

    @Test fun staleExactlyAtThreshold() {
        assertFalse(FreshnessFormatter.format(stamp, t0 + 3599_000, 3600.0).stale)
        assertTrue(FreshnessFormatter.format(stamp, t0 + 3600_000, 3600.0).stale)
    }

    @Test fun unparseableIsStale() {
        val f = FreshnessFormatter.format("garbage", t0)
        assertTrue(f.stale)
        assertTrue(f.label.contains("unknown"))
    }

    @Test fun futureStampClampsToZero() {
        val f = FreshnessFormatter.format(stamp, t0 - 60_000, 3600.0)
        assertTrue(f.label.contains("just now"))
        assertFalse(f.stale)
    }

    @Test fun acceptsProductionOffsetForm() {
        // compose.py emits +00:00, not Z (spec R10).
        val f = FreshnessFormatter.format("2026-08-29T13:34:00+00:00", t0 + 1000, 3600.0)
        assertEquals("composed 13:34 UTC · just now", f.label)
    }
}
