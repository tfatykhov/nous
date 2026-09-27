package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class ChartTest {
    private fun pts(vararg vs: Double?): List<JsonObject> = vs.mapIndexed { i, v ->
        val t = "2026-08-" + (i + 1).toString().padStart(2, '0')
        JsonObject(mapOf("t" to JsonPrimitive(t)) + (if (v != null) mapOf("v" to JsonPrimitive(v)) else mapOf("v" to kotlinx.serialization.json.JsonNull)))
    }
    private fun o(s: String) = Json.parseToJsonElement(s).jsonObject

    @Test fun toneAndSeries() {
        assertEquals(Tone.OK, Chart.normalizeTone(JsonPrimitive("ok"))); assertEquals(Tone.CRIT, Chart.normalizeTone(JsonPrimitive("crit")))
        assertEquals(Tone.NEUTRAL, Chart.normalizeTone(JsonPrimitive("rainbow"))); assertEquals(Tone.NEUTRAL, Chart.normalizeTone(null))
        assertEquals(1, Chart.seriesSlot(0)); assertEquals(4, Chart.seriesSlot(3)); assertEquals(4, Chart.seriesSlot(9))
    }

    @Test fun finiteExtractionAndGaps() {
        assertEquals(listOf(IV(0, 1.0), IV(2, 3.0)), Chart.seriesValues(pts(1.0, null, 3.0)))
        val s = Chart.readSeries(o("""{"kind":"series","points":[{"t":"a","v":1},null,{"t":"c","v":3}]}"""))
        assertTrue(s.ok); assertEquals(JsonObject(emptyMap()), s.points[1])
        assertEquals(listOf(IV(0, 1.0), IV(2, 3.0)), Chart.seriesValues(s.points))
        assertEquals(1, Chart.countDropped(s.points, listOf("v")))
        // strings are not finite numbers
        assertEquals(emptyList(), Chart.seriesValues(listOf(o("""{"t":"a","v":"5"}"""))))
    }

    @Test fun classify() {
        assertEquals(Degenerate.EMPTY, Chart.classify(emptyList())); assertEquals(Degenerate.SINGLE, Chart.classify(listOf(5.0)))
        assertEquals(Degenerate.FLAT, Chart.classify(listOf(5.0, 5.0, 5.0))); assertEquals(Degenerate.OK, Chart.classify(listOf(1.0, 2.0, 3.0)))
    }

    @Test fun yDomain() {
        val d = Chart.yDomain(listOf(40.0, 62.0, 55.0), true); assertEquals(0.0, d.min); assertTrue(d.max >= 62); assertFalse(d.zeroBreak)
        val l = Chart.yDomain(listOf(58.0, 62.0, 60.0), false); assertTrue(l.min > 0); assertTrue(l.zeroBreak)
        val fl = Chart.yDomain(listOf(50.0, 50.0, 50.0), false); assertTrue(fl.min < 50); assertTrue(fl.max > 50)
        assertEquals(Domain(0.0, 10.0, false), Chart.yDomain(listOf(10.0, 10.0), true))
        assertEquals(Domain(-10.0, 0.0, false), Chart.yDomain(listOf(-10.0, -10.0), true))
        val e = Chart.yDomain(emptyList(), false); assertTrue(e.min.isFinite() && e.max.isFinite())
    }

    @Test fun scales() {
        val d = Chart.yDomain(listOf(0.0, 100.0), true); val y = Chart.yScale(d, 100.0, 10.0)
        assertEquals(10.0, y(d.max), 0.5); assertEquals(90.0, y(d.min), 0.5)
        val x = Chart.xScale(3, 100.0, 10.0); assertEquals(10.0, x(0)); assertEquals(90.0, x(2)); assertEquals(50.0, Chart.xScale(1, 100.0, 10.0)(0))
    }

    @Test fun lineSegments() {
        val segs = Chart.lineSegments(Chart.seriesValues(pts(1.0, null, 3.0, 4.0)), { it.toDouble() }, { it })
        assertEquals(listOf("0.0,1.0", "2.0,3.0 3.0,4.0"), segs)
        assertEquals(1, Chart.lineSegments(Chart.seriesValues(pts(1.0, 2.0, 3.0)), { it.toDouble() }, { it }).size)
    }

    @Test fun ticks() {
        assertEquals("1.5k", Chart.formatTick(1500.0)); assertEquals("2M", Chart.formatTick(2_000_000.0))
        assertEquals("62", Chart.formatTick(62.0)); assertEquals("3.14", Chart.formatTick(3.14159)); assertEquals("12.3", Chart.formatTick(12.345))
        assertEquals(listOf(0.0, 50.0, 100.0), Chart.ticks(Domain(0.0, 100.0, false), 3))
    }

    @Test fun sparklineAdditions() {
        assertEquals(3, Chart.trendWindow(10)); assertEquals(7, Chart.trendWindow(56)); assertEquals(25, Chart.trendWindow(200))
        val finite = Chart.seriesValues(pts(1.0, 3.0, null, 5.0, 7.0))
        assertEquals(listOf(IV(0, 1.0), IV(1, 2.0), IV(3, 5.0), IV(4, 6.0)), Chart.rollingMean(finite, 2))
        assertEquals(2, Chart.lineSegments(Chart.rollingMean(finite, 2), { it.toDouble() }, { it }).size)
        val id = Chart.seriesValues(pts(4.0, 8.0, 6.0)); assertEquals(id, Chart.rollingMean(id, 1))
        val points = pts(1.0, 2.0, 3.0, 4.0)
        assertEquals(2, Chart.focusStartIndex(points, "2026-08-03")); assertEquals(2, Chart.focusStartIndex(points, "2026-08-02T12:00:00Z"))
        assertNull(Chart.focusStartIndex(points, "2027-01-01")); assertNull(Chart.focusStartIndex(points, null))
        assertEquals(1, Chart.focusStartIndex(listOf(JsonObject(emptyMap())) + points, "2026-08-01"))
        val offsets = listOf(o("""{"t":"2026-09-01T00:30:00+01:00","v":1}"""), o("""{"t":"2026-09-01T00:00:00Z","v":2}"""), o("""{"t":"2026-09-01T02:00:00Z","v":3}"""))
        assertEquals(1, Chart.focusStartIndex(offsets, "2026-09-01T00:00:00Z")); assertEquals(0, Chart.focusStartIndex(offsets, "2026-08-31T23:00:00Z"))
        assertEquals(1, Chart.focusStartIndex(listOf(o("""{"t":"alpha","v":1}"""), o("""{"t":"beta","v":2}""")), "b"))
        val z = Chart.parseInstant("2026-09-01T00:30:00Z")
        assertEquals(z, Chart.parseInstant("2026-09-01T00:30:00")); assertEquals(z, Chart.parseInstant("2026-09-01 00:30:00"))
        assertEquals(Chart.parseInstant("2026-08-31T23:30:00Z"), Chart.parseInstant("2026-09-01T00:30:00+01:00"))
        assertEquals(Chart.parseInstant("2026-09-01T00:00:00Z"), Chart.parseInstant("2026-09-01"))
        assertEquals(1, Chart.focusStartIndex(listOf(o("""{"t":"2026-09-01T00:30:00","v":1}"""), o("""{"t":"2026-09-01T01:30:00","v":2}""")), "2026-09-01T01:00:00Z"))
        assertEquals("2026-08-02", Chart.readSeries(o("""{"kind":"series","points":[],"unit":"","meta":{"focus_from":"2026-08-02"}}""")).focusFrom)
        assertNull(Chart.readSeries(o("""{"kind":"series","points":[],"meta":{"focus_from":42}}""")).focusFrom)
        assertNull(Chart.readSeries(Json.parseToJsonElement("[1,2]")).focusFrom)
        assertEquals("array", Chart.readSeries(Json.parseToJsonElement("[1,2]")).shape)
        assertEquals("nothing", Chart.readSeries(null).shape)
    }
}
