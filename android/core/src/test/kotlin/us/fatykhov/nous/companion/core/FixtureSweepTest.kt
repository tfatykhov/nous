package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * Spec §10.3: every upstream conformance example (43) and the F096 whole
 * app must replay through the store and walk with no placeholder or
 * dangling node. Every catalog component the manifest marks `ported` must
 * resolve to a Render node; an `unsupported` one must resolve to Unknown
 * (the §8.2 fallback card), never to a crash.
 */
class FixtureSweepTest {
    private val json = Json { ignoreUnknownKeys = true }
    private val walker = Walker(Registry.ported)

    private fun replay(messages: JsonArray): SurfaceStore {
        val store = SurfaceStore()
        messages.forEachIndexed { i, m -> store.apply((i + 1).toLong(), m.jsonObject) }
        return store
    }

    private fun sweepAll(store: SurfaceStore): Map<String, List<Node>> =
        store.surfaces.mapValues { (_, s) -> walker.sweep(s, rootId = if ("root" in s.components) "root" else s.components.keys.first()) }

    @Test fun everyConformanceExampleWalks() {
        val files = RepoFiles.examples.listFiles { f -> f.extension == "json" }!!.sortedBy { it.name }
        assertEquals(43, files.size, "expected the 43 upstream examples")
        val problems = mutableListOf<String>()
        for (f in files) {
            val doc = json.parseToJsonElement(f.readText()).jsonObject
            val store = replay(doc["messages"]!!.jsonArray)
            assertTrue(store.surfaces.isNotEmpty(), "${f.name}: no surface after replay")
            for ((id, nodes) in sweepAll(store)) for (n in nodes) when (n) {
                is Node.Unknown -> if (n.component in Registry.ported) problems.add("${f.name}/$id: ported component ${n.component} not rendered")
                is Node.Dangling -> problems.add("${f.name}/$id: dangling ${n.componentId}")
                is Node.Placeholder -> problems.add("${f.name}/$id: ${n.reason} at ${n.componentId}")
                is Node.Render -> {}
            }
        }
        assertTrue(problems.isEmpty(), problems.joinToString("\n"))
    }

    @Test fun f096ReportAppWalks() {
        val doc = json.parseToJsonElement(RepoFiles.f096.readText()).jsonObject
        val env = buildJsonObject {
            put("createSurface", buildJsonObject {
                put("surfaceId", "f096"); put("catalogId", "nous-core")
                put("components", doc["components"]!!); put("dataModel", doc["dataModel"]!!)
            })
        }
        val store = SurfaceStore(); store.apply(null, env)
        val nodes = walker.sweep(store.surfaces["f096"]!!)
        val bad = nodes.filter { it !is Node.Unknown || it.component in Registry.ported }
        assertTrue(bad.isEmpty(), bad.joinToString("\n"))
        // The app declares 19 components; the sweep must reach every one of them (no unreachable islands).
        val reached = HashSet<String>()
        fun visit(id: String, scope: Scope?, depth: Int, anc: List<String>) {
            if (!reached.add(id)) return
            val s = store.surfaces["f096"]!!
            when (val n = walker.resolve(s, id, scope, depth, anc)) {
                is Node.Render, is Node.Unknown -> {
                    val comp = s.components[id] ?: return
                    for (key in walker.CHILD_KEYS) {
                        val v = comp[key] ?: continue
                        val kids = when (key) {
                            "children" -> walker.children(s, v, scope)
                            "tabs" -> (v as? JsonArray)?.mapNotNull { (it as? JsonObject)?.get("child")?.stringOrNull }?.map { Child.Static(it) } ?: emptyList()
                            else -> v.stringOrNull?.let { listOf(Child.Static(it)) } ?: emptyList()
                        }
                        for (k in kids) when (k) {
                            is Child.Static -> visit(k.componentId, scope, depth + 1, anc + id)
                            is Child.Template -> visit(k.componentId, k.scope, depth + 1, anc + id)
                            is Child.Omitted -> {}
                        }
                    }
                }
                else -> {}
            }
        }
        visit("root", null, 0, emptyList())
        assertEquals(store.surfaces["f096"]!!.components.keys, reached, "unreachable components")
    }
}
