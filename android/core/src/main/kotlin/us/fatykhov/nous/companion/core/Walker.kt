package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/**
 * The adjacency-list walker's DECISIONS (Renderer.svelte + Children.svelte),
 * separated from drawing so `:app` renders what this resolves and the
 * fixture sweep can walk a tree with no UI (spec §3.2 R11, R12).
 */
sealed interface Node {
    /** Cycle or depth overflow — the web renders "⟳". */
    data class Placeholder(val componentId: String, val reason: String) : Node
    /** Dangling id (surface or component not loaded yet) — "…". */
    data class Dangling(val componentId: String) : Node
    /** Registered adapter absent — "[Name]" on the web, the §8.2 fallback card here. */
    data class Unknown(val componentId: String, val component: String) : Node
    data class Render(
        val componentId: String,
        val component: String,
        val props: JsonObject,
        val scope: Scope?,
        val depth: Int,
        val ancestors: List<String>,
    ) : Node
}

/** One expanded child slot of a ChildList. */
sealed interface Child {
    data class Static(val componentId: String) : Child
    data class Template(val componentId: String, val scope: Scope) : Child
    data class Omitted(val note: String) : Child
}

class Walker(private val registry: Set<String>) {
    companion object { const val MAX_DEPTH = 64 }

    fun resolve(surface: SurfaceState?, componentId: String, scope: Scope?, depth: Int, ancestors: List<String>): Node {
        if (depth > MAX_DEPTH || componentId in ancestors) return Node.Placeholder(componentId, "cycle or depth limit")
        val comp = surface?.components?.get(componentId) ?: return Node.Dangling(componentId)
        val name = comp["component"]?.stringOrNull ?: return Node.Dangling(componentId)
        if (name !in registry) return Node.Unknown(componentId, name)
        return Node.Render(componentId, name, comp, scope, depth + 1, ancestors + componentId)
    }

    /** Expand a `children` prop: a static id array, or `{componentId, path}` over a bound array. */
    fun children(surface: SurfaceState, children: JsonElement?, scope: Scope?): List<Child> {
        if (children is JsonArray) return children.mapNotNull { it.stringOrNull }.map { Child.Static(it) }
        val t = children as? JsonObject ?: return emptyList()
        val cid = t["componentId"]?.stringOrNull ?: return emptyList()
        val path = t["path"]?.stringOrNull ?: return emptyList()
        val base = Pointer.absolute(path, scope?.base)
        val items = Pointer.get(surface.dataModel, base) as? JsonArray ?: return emptyList()
        var omitted: Int? = null
        val out = mutableListOf<Child>()
        items.forEachIndexed { i, item ->
            if (Functions.isTruncationMarker(item)) {
                val n = ((item as JsonObject)["omitted"] as? kotlinx.serialization.json.JsonPrimitive)?.let { kotlinx.serialization.json.JsonPrimitive(it.content).let { p -> p.content.toDoubleOrNull() } } ?: 0.0
                omitted = (omitted ?: 0) + (if (n > 0) n.toInt() else 0)
            } else out.add(Child.Template(cid, Scope("$base/$i", i)))
        }
        omitted?.let { out.add(Child.Omitted(Functions.omittedNote(it))) }
        return out
    }

    /** Depth-first sweep of a whole surface; returns every non-Render node found (fixture test §10.3). */
    fun sweep(surface: SurfaceState, rootId: String = "root"): List<Node> {
        val problems = mutableListOf<Node>()
        fun visit(id: String, scope: Scope?, depth: Int, ancestors: List<String>) {
            when (val n = resolve(surface, id, scope, depth, ancestors)) {
                is Node.Render -> for (key in CHILD_KEYS) childrenOf(surface, n, key).forEach { c ->
                    when (c) {
                        is Child.Static -> visit(c.componentId, n.scope, n.depth, n.ancestors)
                        is Child.Template -> visit(c.componentId, c.scope, n.depth, n.ancestors)
                        is Child.Omitted -> {}
                    }
                }
                else -> problems.add(n)
            }
        }
        visit(rootId, null, 0, emptyList())
        return problems
    }

    private fun childrenOf(surface: SurfaceState, n: Node.Render, key: String): List<Child> {
        val v = n.props[key] ?: return emptyList()
        return when (key) {
            "children" -> children(surface, v, n.scope)
            "tabs" -> (v as? JsonArray)?.mapNotNull { (it as? JsonObject)?.get("child")?.stringOrNull }?.map { Child.Static(it) } ?: emptyList()
            else -> v.stringOrNull?.let { listOf(Child.Static(it)) } ?: emptyList()
        }
    }

    /** Every prop key that names a child in either catalog (mirrors grammar `_children_of`). */
    val CHILD_KEYS = listOf("children", "child", "trigger", "content", "tabs")
}
