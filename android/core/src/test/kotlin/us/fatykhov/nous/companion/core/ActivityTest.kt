package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import us.fatykhov.nous.companion.core.ActivityRules as A
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class ActivityTest {
    private fun j(s: String) = Json.parseToJsonElement(s)
    private val at = A.parseIsoMs("2026-09-04T10:00:00Z")!!

    @Test fun formatElapsed() {
        assertEquals("0s", A.formatElapsed(0))
        assertEquals("4s", A.formatElapsed(4_200))
        assertEquals("1m 12s", A.formatElapsed(72_000))
        assertEquals("1m 05s", A.formatElapsed(65_000))
        assertEquals("1h 03m", A.formatElapsed(3_780_000))
        assertEquals("0s", A.formatElapsed(-5))
    }

    @Test fun pendingActionParsesAndDerivesStaleWindow() {
        val p = A.pendingActionOf(j("""{"pendingAction":{"id":"a","label":"Act","at":"2026-09-04T10:00:00Z","timeout_s":60}}"""))
        assertEquals(PendingAction("a", "Act", "2026-09-04T10:00:00Z", 60_000, "a@2026-09-04T10:00:00Z"), p)
    }

    @Test fun stampIdentityIsSubtaskId() {
        val a = A.pendingActionOf(j("""{"pendingAction":{"id":"x","at":"2026-09-04T10:00:00Z","subtask_id":"sub-1"}}"""))
        val b = A.pendingActionOf(j("""{"pendingAction":{"id":"x","at":"2026-09-04T10:00:00Z","subtask_id":"sub-2"}}"""))
        assertEquals("sub-1", a?.key); assertEquals("sub-2", b?.key); assertNotEquals(a?.key, b?.key)
    }

    @Test fun fallbacksForOldStamps() {
        val p = A.pendingActionOf(j("""{"pendingAction":{"id":"a","at":"2026-09-04T10:00:00Z"}}"""))
        assertEquals("a", p?.label)
        assertEquals(A.PENDING_STALE_FALLBACK_MS, p?.staleMs)
    }

    @Test fun malformedIsNoPending() {
        assertNull(A.pendingActionOf(null))
        assertNull(A.pendingActionOf(j("{}")))
        assertNull(A.pendingActionOf(j("""{"pendingAction":"x"}""")))
        assertNull(A.pendingActionOf(j("""{"pendingAction":{"id":"a"}}""")))
        assertNull(A.pendingActionOf(j("""{"pendingAction":{"at":"now"}}""")))
    }

    private val meta = j("""{"pendingAction":{"id":"rebalance","label":"Rebalance","at":"2026-09-04T10:00:00Z","timeout_s":300}}""")

    @Test fun freshInsideWindowStalePastIt() {
        val p = A.pendingActionOf(meta)
        assertTrue(A.pendingIsFresh(p, at + 10_000))
        assertFalse(A.pendingIsFresh(p, at + 300_000))
        assertFalse(A.pendingIsFresh(null, at))
    }

    @Test fun unparsableStampNeverFresh() {
        assertFalse(A.pendingIsFresh(A.pendingActionOf(j("""{"pendingAction":{"id":"a","at":"not a date"}}""")), at))
    }

    @Test fun freshStampIsActActivity() {
        assertEquals(Activity(ActivityKind.ACT, "rebalance", at, 0), A.pendingActivity(meta, at + 5_000))
        assertNull(A.pendingActivity(meta, at + 600_000))
    }

    @Test fun recomposedAfterIsStrict() {
        assertTrue(A.recomposedAfter("2026-09-04T10:00:41Z", at))
        assertFalse(A.recomposedAfter("2026-09-04T10:00:00Z", at))
        assertFalse(A.recomposedAfter("2026-09-04T09:00:00Z", at))
        assertFalse(A.recomposedAfter("", at))
        assertFalse(A.recomposedAfter("soon", at))
    }

    @Test fun responseSeq() {
        assertEquals(42L, A.responseSeq(j("""{"refreshed":["meta"],"seq":42}""")))
        assertEquals(7L, A.responseSeq(j("""{"refined":"blockers","seq":7}""")))
        assertNull(A.responseSeq(j("""{"refreshed":["meta"]}""")))
        assertNull(A.responseSeq(j("""{"seq":"42"}""")))
        assertNull(A.responseSeq(j("""{"seq":0}""")))
        assertNull(A.responseSeq(j("""{"seq":4.5}""")))
        assertNull(A.responseSeq(null))
        assertNull(A.responseSeq(JsonPrimitive("seq")))
    }
}
