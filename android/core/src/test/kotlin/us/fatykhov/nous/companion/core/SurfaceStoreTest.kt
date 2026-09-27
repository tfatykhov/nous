package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

class SurfaceStoreTest {
    private fun j(s: String) = Json.parseToJsonElement(s).jsonObject
    private fun create(id: String = "s1", extras: String = ""): JsonObject = j(
        """{"version":"v1.0","createSurface":{"surfaceId":"$id","catalogId":"https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json",
        "components":[{"id":"root","component":"Card","child":"body"},{"id":"body","component":"Text","text":"hello"}],
        "dataModel":{"formData":{"email":""}}$extras}}"""
    )
    private fun model(store: SurfaceStore, id: String) = store.surfaces[id]!!.dataModel.jsonObject

    @Test fun createIndexesComponentsAndSeedsModel() {
        val s = SurfaceStore(); s.apply(1, create())
        val st = s.surfaces["s1"]!!
        assertEquals(listOf("root", "body"), st.components.keys.toList())
        assertEquals("Card", st.components["root"]!!["component"]!!.jsonPrimitive.content)
        assertEquals(j("""{"formData":{"email":""}}"""), st.dataModel)
        assertTrue(st.catalogId.contains("basic/catalog.json"))
    }

    @Test fun createToleratesAbsentComponentsAndModel() {
        val s = SurfaceStore()
        s.apply(1, j("""{"version":"v1.0","createSurface":{"surfaceId":"bare","catalogId":"c","sendDataModel":true}}"""))
        assertEquals(emptyMap(), s.surfaces["bare"]!!.components)
        assertEquals(JsonObject(emptyMap()), s.surfaces["bare"]!!.dataModel)
    }

    @Test fun extensionsAndDefaults() {
        val s = SurfaceStore()
        s.apply(1, create("s1", ""","metadata":{"extensions":{"com_nous_nonce":"abc123","com_nous_priority":2,"com_nous_theme":"report","com_nous_title":"T"}}"""))
        assertEquals("abc123", s.surfaces["s1"]!!.nonce); assertEquals(2, s.surfaces["s1"]!!.priority)
        assertEquals("report", s.surfaces["s1"]!!.theme); assertEquals("T", s.surfaces["s1"]!!.title)
        s.apply(2, create("s2"))
        assertEquals("", s.surfaces["s2"]!!.nonce); assertEquals(0, s.surfaces["s2"]!!.priority)
    }

    @Test fun updateComponentsUpsertsById() {
        val s = SurfaceStore(); s.apply(1, create())
        s.apply(2, j("""{"updateComponents":{"surfaceId":"s1","components":[{"id":"body","component":"Text","text":"changed"},{"id":"extra","component":"Divider"}]}}"""))
        assertEquals("changed", s.surfaces["s1"]!!.components["body"]!!["text"]!!.jsonPrimitive.content)
        assertEquals("Divider", s.surfaces["s1"]!!.components["extra"]!!["component"]!!.jsonPrimitive.content)
        assertEquals("Card", s.surfaces["s1"]!!.components["root"]!!["component"]!!.jsonPrimitive.content)
    }

    @Test fun ignoresUnknownSurface() {
        val s = SurfaceStore()
        s.apply(1, j("""{"updateComponents":{"surfaceId":"ghost","components":[]}}"""))
        s.apply(2, j("""{"updateDataModel":{"surfaceId":"ghost","path":"/a","value":1}}"""))
        assertNull(s.surfaces["ghost"])
    }

    @Test fun updateDataModelVariants() {
        val s = SurfaceStore(); s.apply(1, create())
        s.apply(2, j("""{"updateDataModel":{"surfaceId":"s1","path":"/formData/email","value":"a@b.co"}}"""))
        assertEquals(j("""{"formData":{"email":"a@b.co"}}"""), model(s, "s1"))
        s.apply(3, j("""{"updateDataModel":{"surfaceId":"s1","path":"/formData/email","value":null}}"""))
        assertEquals(j("""{"formData":{}}"""), model(s, "s1"))
        s.apply(4, j("""{"updateDataModel":{"surfaceId":"s1","value":{"now":"2025-12-15"}}}"""))
        assertEquals(j("""{"now":"2025-12-15"}"""), model(s, "s1"))
        s.apply(5, j("""{"updateDataModel":{"surfaceId":"s1","path":"/","value":{"fresh":true}}}"""))
        assertEquals(j("""{"fresh":true}"""), model(s, "s1"))
    }

    @Test fun deleteRemoves() {
        val s = SurfaceStore(); s.apply(1, create()); s.apply(2, j("""{"deleteSurface":{"surfaceId":"s1"}}"""))
        assertNull(s.surfaces["s1"])
    }

    @Test fun sameSeqAppliesOnce() {
        val s = SurfaceStore(); s.apply(1, create())
        s.apply(2, j("""{"updateDataModel":{"surfaceId":"s1","path":"/count","value":5}}"""))
        s.apply(2, j("""{"updateDataModel":{"surfaceId":"s1","path":"/count","value":99}}"""))
        assertEquals("5", model(s, "s1")["count"]!!.jsonPrimitive.content); assertEquals(2, s.lastSeq)
    }

    @Test fun unseenLowerSeqAppliesExactlyOnce() {
        val s = SurfaceStore(); s.apply(5, create())
        s.apply(3, j("""{"updateDataModel":{"surfaceId":"s1","path":"/count","value":1}}"""))
        assertEquals("1", model(s, "s1")["count"]!!.jsonPrimitive.content)
        s.apply(3, j("""{"updateDataModel":{"surfaceId":"s1","path":"/count","value":99}}"""))
        assertEquals("1", model(s, "s1")["count"]!!.jsonPrimitive.content); assertEquals(5, s.lastSeq)
    }

    @Test fun nullSeqAlwaysAppliesNeverMovesWatermark() {
        val s = SurfaceStore(); s.apply(7, create()); s.apply(null, create("s2"))
        assertNotNull(s.surfaces["s2"]); assertEquals(7, s.lastSeq)
    }

    @Test fun pruneResetOrdered() {
        val s = SurfaceStore()
        s.apply(null, create("keep")); s.apply(null, create("zombie")); s.pruneAbsent(setOf("keep"))
        assertNotNull(s.surfaces["keep"]); assertNull(s.surfaces["zombie"])
        s.pruneAbsent(emptySet()); assertTrue(s.surfaces.isEmpty())
        s.apply(9, create()); s.reset(); assertTrue(s.surfaces.isEmpty()); assertEquals(0, s.lastSeq)
        s.apply(null, create("low", ""","metadata":{"extensions":{"com_nous_priority":0}}"""))
        s.apply(null, create("high", ""","metadata":{"extensions":{"com_nous_priority":2}}"""))
        s.apply(null, create("mid", ""","metadata":{"extensions":{"com_nous_priority":1}}"""))
        assertEquals(listOf("high", "mid", "low"), s.ordered().map { it.surfaceId })
    }

    @Test fun surfaceUptoSuppressesWithoutMovingCursor() {
        val s = SurfaceStore(); s.apply(null, create("snap")); s.setSurfaceUpto("snap", 12)
        assertEquals(0, s.lastSeq)
        s.apply(12, j("""{"updateDataModel":{"surfaceId":"snap","path":"/x","value":"stale"}}"""))
        assertNull(model(s, "snap")["x"])
        s.apply(11, create("missed")); assertNotNull(s.surfaces["missed"])
    }

    // --- R9 holds (activity parity) ---

    @Test fun modelHoldEndsOnExactSeqNotHigher() {
        var now = 1_000L
        val s = SurfaceStore { now }; s.apply(null, create())
        val tok = s.beginActivity("s1", ActivityKind.REFRESH, "refresh")
        assertTrue(s.holdForModel("s1", tok, 42))
        s.apply(50, j("""{"updateDataModel":{"surfaceId":"s1","path":"/a","value":1}}"""))  // higher seq: not arrival
        assertNotNull(s.activity["s1"])
        s.apply(42, j("""{"updateDataModel":{"surfaceId":"s1","path":"/meta/composedAt","value":"2026-09-04T10:00:00Z"}}"""))
        assertNull(s.activity["s1"]); assertEquals(1_000L, s.doneAt["s1"])
    }

    @Test fun modelHoldEndsWhenWatermarkCovers() {
        val s = SurfaceStore { 5L }; s.apply(null, create())
        val tok = s.beginActivity("s1", ActivityKind.REFINE, "opt")
        assertTrue(s.holdForModel("s1", tok, 42)); s.setSurfaceUpto("s1", 42)
        assertNull(s.activity["s1"]); assertEquals(5L, s.doneAt["s1"])
    }

    @Test fun holdWithoutSeqNeverSucceeds() {
        val s = SurfaceStore(); s.apply(null, create())
        val tok = s.beginActivity("s1", ActivityKind.REFRESH, "refresh")
        assertTrue(s.holdForModel("s1", tok, null))
        s.apply(99, j("""{"updateDataModel":{"surfaceId":"s1","path":"/a","value":1}}"""))
        assertNotNull(s.activity["s1"])
    }

    @Test fun streamBeatingResponseEndsImmediately() {
        val s = SurfaceStore(); s.apply(null, create())
        val tok = s.beginActivity("s1", ActivityKind.REFRESH, "refresh")
        s.apply(42, j("""{"updateDataModel":{"surfaceId":"s1","path":"/a","value":1}}"""))
        assertFalse(s.holdForModel("s1", tok, 42)); assertNull(s.activity["s1"])
    }

    @Test fun stampHoldEndsOnNewStampIdentity() {
        val s = SurfaceStore(); s.apply(null, create())
        val tok = s.beginActivity("s1", ActivityKind.ACT, "rebalance")
        assertTrue(s.holdForStamp("s1", tok))
        s.apply(10, j("""{"updateDataModel":{"surfaceId":"s1","path":"/meta/pendingAction","value":{"id":"rebalance","at":"2026-09-04T10:00:00Z","subtask_id":"sub-1","timeout_s":300}}}"""))
        assertNull(s.activity["s1"]); assertEquals("sub-1", s.stampSeen["s1"])
    }

    @Test fun stampSeenSinceBeginEndsWithoutHold() {
        val s = SurfaceStore(); s.apply(null, create())
        val tok = s.beginActivity("s1", ActivityKind.ACT, "x")
        s.apply(10, j("""{"updateDataModel":{"surfaceId":"s1","path":"/meta/pendingAction","value":{"id":"x","at":"2026-09-04T10:00:00Z","subtask_id":"sub-9"}}}"""))
        assertFalse(s.holdForStamp("s1", tok)); assertNull(s.activity["s1"])
    }

    @Test fun staleTokenDoesNotRelease() {
        val s = SurfaceStore(); s.apply(null, create())
        val t1 = s.beginActivity("s1", ActivityKind.REFRESH, "refresh")
        val t2 = s.beginActivity("s1", ActivityKind.REFRESH, "refresh")
        s.endActivityIf("s1", t1, true); assertNotNull(s.activity["s1"])
        s.endActivityIf("s1", t2, true); assertNull(s.activity["s1"])
    }

    @Test fun resyncKeepsInFlightActivity() {
        val s = SurfaceStore(); s.apply(null, create())
        s.beginActivity("s1", ActivityKind.REFRESH, "refresh"); s.resync()
        assertTrue(s.surfaces.isEmpty()); assertNotNull(s.activity["s1"])
        s.reset(); assertNull(s.activity["s1"])
    }

    @Test fun deleteEndsActivityKeepsTapped() {
        val s = SurfaceStore(); s.apply(null, create())
        s.apply(1, j("""{"updateDataModel":{"surfaceId":"s1","path":"/meta/pendingAction","value":{"id":"x","at":"2026-09-04T10:00:00Z"}}}"""))
        s.beginActivity("s1", ActivityKind.ACT, "x")
        s.apply(2, j("""{"deleteSurface":{"surfaceId":"s1"}}"""))
        assertNull(s.activity["s1"]); assertNotNull(s.tappedAt["s1"])
    }

    @Test fun recomposeAfterTapMarksDone() {
        val s = SurfaceStore { 77L }; s.apply(null, create())
        s.apply(1, j("""{"updateDataModel":{"surfaceId":"s1","path":"/meta/pendingAction","value":{"id":"x","at":"2026-09-04T10:00:00Z"}}}"""))
        // Recompose delivered as delete + create with a later composedAt and no stamp.
        s.apply(2, j("""{"deleteSurface":{"surfaceId":"s1"}}"""))
        s.apply(3, j("""{"createSurface":{"surfaceId":"s1","catalogId":"c","components":[],"dataModel":{"meta":{"composedAt":"2026-09-04T10:00:41Z"}}}}"""))
        assertEquals(77L, s.doneAt["s1"]); assertNull(s.tappedAt["s1"])
    }
}
