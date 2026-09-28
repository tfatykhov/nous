package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class GraphsAndShellTest {
    private fun j(s: String) = Json.parseToJsonElement(s)

    @Test fun dagDedupesAndLaysOutByLongestPath() {
        val nodes = Graphs.dagNodes(j("""[{"name":"a","status":"completed"},{"name":"b"},{"name":"c","status":"running"},{"name":"a"}]"""))
        assertEquals(listOf("a", "b", "c"), nodes.map { it.name })
        val edges = Graphs.dagEdges(j("""[{"from":"a","to":"b"},{"from":"b","to":"c"},{"from":"a","to":"c"},{"from":"a","to":"b"},{"from":"x","to":"a"}]"""), nodes)
        assertEquals(3, edges.size)
        val l = Graphs.dagLayout(nodes, edges)
        assertEquals(60.0, l.pos["a"]!!.x); assertEquals(210.0, l.pos["b"]!!.x); assertEquals(360.0, l.pos["c"]!!.x)
        assertEquals(40.0 + 64.0 / 3, l.pos["b"]!!.y)   // odd column wave stagger
        assertEquals(3 * 150.0 + 40, l.width)
        // a cycle must not hang
        val cyc = Graphs.dagLayout(nodes, listOf(DagEdge("a", "b"), DagEdge("b", "a")))
        assertTrue(cyc.pos.size == 3)
        assertEquals("ok", Graphs.dagStatusToken("completed")); assertEquals("muted", Graphs.dagStatusToken(null))
        assertEquals("abcdefghijklmno…", Graphs.dagShort("abcdefghijklmnopq"))
    }

    @Test fun radialLayoutCentresFocus() {
        val nodes = Graphs.graphNodes(j("""[{"id":"f","type":"fact","label":"Focus"},{"id":"n1"},{"id":"n2","type":"decision"},{"id":"n1"}]"""))
        assertEquals(3, nodes.size)
        val focus = Graphs.focusId(null, nodes); assertEquals("f", focus)
        val pos = Graphs.radialLayout(nodes, focus)
        assertEquals(Pt(220.0, 180.0), pos["f"])
        assertEquals(220.0, pos["n1"]!!.x, 1e-9); assertEquals(180.0 - 128.0, pos["n1"]!!.y, 1e-9)   // first at -90°
        val edges = Graphs.graphEdges(j("""[{"source":"f","target":"n1","relation":"r","weight":0.8},{"source":"f","target":"n1","relation":"r"},{"source":"","target":"n1"}]"""))
        assertEquals(1, edges.size); assertEquals(2.0, Graphs.edgeStroke(0.8)); assertEquals(1.25, Graphs.edgeStroke(null))
        assertEquals("node-decision", Graphs.graphTypeToken("decision"))
        assertEquals("abcdefgh", Graphs.graphShort(null, "abcdefghijkl"))
    }

    @Test fun kindAndLabels() {
        assertEquals("approval_gate", Shell.kindOf("nous:heartbeat:approval_gate:ab12cd"))
        assertEquals("", Shell.kindOf("junk"))
        assertEquals("short", Shell.shorten("  short  "))
        // 22 graphemes kept = "Crypto Note Six Months"; the last space (index 15) is within 8 of the cap, so cut there.
        assertEquals("Crypto Note Six…", Shell.shorten("Crypto Note Six Months Forward View"))
        assertEquals("abcdefghijklmnopqrstuv…", Shell.shorten("abcdefghijklmnopqrstuvwxyz"))   // no boundary → hard cut
        // grapheme cut: a flag is two code points, one cluster
        val flags = "🇺🇸".repeat(30)
        assertEquals(22, Shell.shorten(flags).dropLast(1).codePointCount(0, Shell.shorten(flags).dropLast(1).length) / 2)
    }

    @Test fun pureTitlesOnly() {
        assertTrue(Shell.isPureTitle(j("""{"call":"formatNumber","args":{"value":1}}""")))
        assertTrue(!Shell.isPureTitle(j("""{"call":"openUrl","args":{"url":"x"}}""")))
        assertTrue(!Shell.isPureTitle(j("""{"call":"formatString","args":{"value":"x"}}""")))   // template can smuggle a call
        assertTrue(!Shell.isPureTitle(j("""{"call":"and","args":{"values":[{"call":"openUrl"}]}}""")))
    }

    @Test fun chipLabelUsesHeaderThroughRoot() {
        val store = SurfaceStore()
        store.apply(null, j("""{"createSurface":{"surfaceId":"nous:agent:micro_app:1","catalogId":"c","metadata":{"extensions":{"com_nous_title":"Record Title Long"}},
          "components":[{"id":"root","component":"Column","children":["hdr"]},{"id":"hdr","component":"AppHeader","title":"Crypto Note","composedAt":"x"},{"id":"stale","component":"AppHeader","title":"Old"}],"dataModel":{}}}""").jsonObject)
        val f = Functions()
        assertEquals("Crypto Note", Shell.chipLabel(store.surfaces["nous:agent:micro_app:1"]!!, f))
        store.apply(null, j("""{"createSurface":{"surfaceId":"nous:heartbeat:dag_monitor:2","catalogId":"c","components":[],"dataModel":{}}}""").jsonObject)
        assertEquals("DAG", Shell.chipLabel(store.surfaces["nous:heartbeat:dag_monitor:2"]!!, f))
    }

    @Test fun closeAllArmsThenFires() {
        var now = 0L
        val c = Shell.CloseAll { now }
        assertNull(c.tap(listOf("a", "b")))          // arm
        assertTrue(c.armed)
        now += 4_001                                  // auto-disarm
        assertNull(c.tap(listOf("a", "b", "c")))      // re-arm with the CURRENT set
        now += 100
        assertEquals(listOf("a", "b", "c"), c.tap(listOf("a", "b", "c", "d")))   // fires with the snapshot the user saw
    }
}
