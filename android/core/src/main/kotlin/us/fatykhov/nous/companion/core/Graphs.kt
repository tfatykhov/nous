package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/**
 * The graph layouts EXTRACTED from `DagGraphView.svelte` and
 * `MemoryGraphView.svelte` (spec §8.5: they had no module and no tests on
 * the web). Geometry only; `:app` draws it.
 */
data class Pt(val x: Double, val y: Double)

data class DagNode(val name: String, val status: String?, val nodeType: String?)
data class DagEdge(val from: String, val to: String)
data class DagLayout(val pos: Map<String, Pt>, val width: Double, val height: Double)

data class GNode(val id: String, val type: String?, val label: String?)
data class GEdge(val source: String, val target: String, val relation: String?, val weight: Double?)

object Graphs {
    // ------------------------------------------------------------- DagGraph
    fun dagNodes(raw: JsonElement?): List<DagNode> {
        val arr = raw as? JsonArray ?: return emptyList()
        val seen = HashSet<String>(); val out = mutableListOf<DagNode>()
        for (e in arr) {
            val o = e as? JsonObject ?: continue
            val name = o["name"]?.stringOrNull ?: continue
            if (!seen.add(name)) continue
            out.add(DagNode(name, o["status"]?.stringOrNull, o["node_type"]?.stringOrNull))
        }
        return out
    }

    fun dagEdges(raw: JsonElement?, nodes: List<DagNode>): List<DagEdge> {
        val arr = raw as? JsonArray ?: return emptyList()
        val names = nodes.map { it.name }.toSet()
        val seen = HashSet<String>(); val out = mutableListOf<DagEdge>()
        for (e in arr) {
            val o = e as? JsonObject ?: continue
            val from = o["from"]?.stringOrNull ?: continue
            val to = o["to"]?.stringOrNull ?: continue
            if (from !in names || to !in names) continue
            if (!seen.add("$from→$to")) continue
            out.add(DagEdge(from, to))
        }
        return out
    }

    /** Longest-path depth via bounded relaxation (a cycle just stops relaxing), wave-staggered columns. */
    fun dagLayout(nodes: List<DagNode>, edges: List<DagEdge>): DagLayout {
        val depths = LinkedHashMap<String, Int>(); nodes.forEach { depths[it.name] = 0 }
        for (pass in nodes.indices) {
            var changed = false
            for (e in edges) {
                val want = (depths[e.from] ?: 0) + 1
                if (want > (depths[e.to] ?: 0) && want <= nodes.size) { depths[e.to] = want; changed = true }
            }
            if (!changed) break
        }
        val columns = LinkedHashMap<Int, MutableList<DagNode>>()
        for (n in nodes) columns.getOrPut(depths[n.name] ?: 0) { mutableListOf() }.add(n)
        val nCols = maxOf(columns.size, 1)
        val maxRows = maxOf(columns.values.maxOfOrNull { it.size } ?: 1, 1)
        val colW = 150.0; val rowH = 64.0
        val width = nCols * colW + 40; val height = maxRows * rowH + 30
        val pos = LinkedHashMap<String, Pt>()
        for ((d, col) in columns) col.forEachIndexed { i, n ->
            val y = 40 + i * rowH + (if (d % 2 == 1) rowH / 3 else 0.0)
            pos[n.name] = Pt(60 + d * colW, y)
        }
        return DagLayout(pos, width, height)
    }

    fun dagShort(name: String): String = if (name.length > 16) name.substring(0, 15) + "…" else name

    /** Status → semantic token key (`ok`/`accent`/`crit`/`muted`); the theme maps it to a colour. */
    fun dagStatusToken(status: String?): String = when (status) {
        "completed" -> "ok"; "running" -> "accent"; "failed" -> "crit"; else -> "muted"
    }

    // ---------------------------------------------------------- MemoryGraph
    const val GRAPH_W = 440.0
    const val GRAPH_H = 360.0

    fun graphNodes(raw: JsonElement?): List<GNode> {
        val arr = raw as? JsonArray ?: return emptyList()
        val seen = HashSet<String>(); val out = mutableListOf<GNode>()
        for (e in arr) {
            val o = e as? JsonObject ?: continue
            val id = o["id"]?.stringOrNull ?: continue
            if (!seen.add(id)) continue
            out.add(GNode(id, o["type"]?.stringOrNull, o["label"]?.stringOrNull))
        }
        return out
    }

    fun graphEdges(raw: JsonElement?): List<GEdge> {
        val arr = raw as? JsonArray ?: return emptyList()
        val seen = HashSet<String>(); val out = mutableListOf<GEdge>()
        for (e in arr) {
            val o = e as? JsonObject ?: continue
            val s = o["source"]?.stringOrNull?.takeIf { it.isNotEmpty() } ?: continue
            val t = o["target"]?.stringOrNull?.takeIf { it.isNotEmpty() } ?: continue
            val rel = o["relation"]?.stringOrNull
            if (!seen.add("$s→$t:${rel ?: ""}")) continue
            out.add(GEdge(s, t, rel, (o["weight"] as? JsonPrimitive)?.takeIf { !it.isString }?.doubleOrNull))
        }
        return out
    }

    fun focusId(focusNodeId: String?, nodes: List<GNode>): String =
        if (!focusNodeId.isNullOrEmpty()) focusNodeId else nodes.firstOrNull()?.id ?: ""

    /** Radial: focus centred, others on one ring (two interleaved radii past 9). */
    fun radialLayout(nodes: List<GNode>, focus: String): Map<String, Pt> {
        val map = LinkedHashMap<String, Pt>()
        val others = nodes.filter { it.id != focus }
        map[focus] = Pt(GRAPH_W / 2, GRAPH_H / 2)
        val base = minOf(GRAPH_W, GRAPH_H) / 2 - 52
        others.forEachIndexed { i, n ->
            val r = if (others.size > 9 && i % 2 == 1) base * 0.58 else base
            val angle = (2 * PI * i) / maxOf(others.size, 1) - PI / 2
            map[n.id] = Pt(GRAPH_W / 2 + r * cos(angle), GRAPH_H / 2 + r * sin(angle))
        }
        return map
    }

    fun graphShort(label: String?, id: String): String {
        val text = if (!label.isNullOrEmpty()) label else id.take(8)
        return if (text.length > 18) text.substring(0, 17) + "…" else text
    }

    fun graphTypeToken(type: String?): String = when (type) {
        "fact" -> "accent"; "decision" -> "node-decision"; "episode" -> "ok"; "procedure" -> "warn"; else -> "muted"
    }

    fun edgeStroke(weight: Double?): Double = maxOf(1.0, (weight ?: 0.5) * 2.5)
}
