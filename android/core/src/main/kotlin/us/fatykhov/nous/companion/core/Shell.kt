package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.text.BreakIterator

/** Shell rules from `Companion.svelte` (spec §3.2 R15): chip labels and close-all arming. */
object Shell {
    val KIND_LABELS = mapOf(
        "micro_app" to "app", "approval_gate" to "approval", "action_review" to "review",
        "heartbeat_findings" to "findings", "decision_sweep" to "decisions", "dag_monitor" to "DAG", "memory_graph" to "graph",
    )
    const val CHIP_MAX = 22
    const val CLOSE_ALL_ARM_MS = 4000L

    /** Surface ids are minted `nous:<origin>:<kind>:<hex>`. */
    fun kindOf(surfaceId: String): String = surfaceId.split(":").getOrNull(2) ?: ""

    /** Cut on grapheme clusters, on a word boundary when one is close to the limit. */
    fun shorten(title: String): String {
        val t = title.replace(Regex("\\s+"), " ").trim()
        val units = mutableListOf<String>()
        val it = BreakIterator.getCharacterInstance(); it.setText(t)
        var start = it.first(); var end = it.next()
        while (end != BreakIterator.DONE) { units.add(t.substring(start, end)); start = end; end = it.next() }
        if (units.size <= CHIP_MAX) return t
        val kept = units.subList(0, CHIP_MAX)
        val sp = kept.lastIndexOf(" ")
        val body = (if (sp >= CHIP_MAX - 8) kept.subList(0, sp) else kept).joinToString("")
        return body.replace(Regex("[\\s:,—-]+$"), "") + "…"
    }

    /** Only these functions may be evaluated for a chip label — an allowlist, so a new effectful function is unsafe by default. */
    private val PURE_TITLE_FNS = setOf("formatNumber", "formatCurrency", "formatDate", "pluralize", "length", "and", "or", "not", "regex", "numeric", "email")

    fun isPureTitle(v: JsonElement?, depth: Int = 0): Boolean {
        if (depth > 6) return false
        return when (v) {
            null, is JsonPrimitive -> true
            is JsonArray -> v.all { isPureTitle(it, depth + 1) }
            is JsonObject -> {
                val call = v["call"]?.stringOrNull
                if (call != null && call !in PURE_TITLE_FNS) return false
                v.values.all { isPureTitle(it, depth + 1) }
            }
            else -> true
        }
    }

    /** The AppHeader title reached THROUGH root (never by type scan: refine leaves stale orphans). */
    fun headerTitle(surface: SurfaceState, functions: Functions): String {
        val comps = surface.components
        val rootChildren = (comps["root"]?.get("children") as? JsonArray)?.mapNotNull { it.stringOrNull } ?: emptyList()
        val headerId = rootChildren.firstOrNull { comps[it]?.get("component")?.stringOrNull == "AppHeader" } ?: return ""
        val raw = comps[headerId]?.get("title") ?: return ""
        raw.stringOrNull?.let { return it.trim() }
        if (!isPureTitle(raw)) return ""
        return try { Functions.toDisplayString(functions.resolveDynamic(raw, EvalContext(surface.dataModel))).trim() } catch (_: Exception) { "" }
    }

    fun chipName(surface: SurfaceState, functions: Functions): String {
        if (kindOf(surface.surfaceId) != "micro_app") return ""
        return headerTitle(surface, functions).ifEmpty { surface.title }.trim()
    }

    fun chipLabel(surface: SurfaceState, functions: Functions): String {
        val kind = kindOf(surface.surfaceId)
        if (kind == "micro_app") chipName(surface, functions).takeIf { it.isNotEmpty() }?.let { return shorten(it) }
        return KIND_LABELS[kind] ?: kind.ifEmpty { surface.surfaceId.takeLast(4) }
    }

    fun chipTooltip(surface: SurfaceState, functions: Functions): String {
        val name = chipName(surface, functions).ifEmpty { surface.title }
        return if (name.isNotEmpty()) "$name — ${surface.surfaceId}" else surface.surfaceId
    }

    /**
     * Two-tap close-all with a 4 s auto-disarm; the confirmation is for the
     * set the user SAW (snapshot at arm time). Pure state machine.
     */
    class CloseAll(private val nowMs: () -> Long) {
        var armedIds: List<String> = emptyList(); private set
        private var armedAt: Long = -1
        val armed: Boolean get() = armedAt >= 0 && nowMs() - armedAt < CLOSE_ALL_ARM_MS
        /** Returns the ids to close on the second tap, or null when this tap only armed. */
        fun tap(liveMicroAppIds: List<String>): List<String>? {
            if (!armed) { armedIds = liveMicroAppIds; armedAt = nowMs(); return null }
            val ids = armedIds; armedIds = emptyList(); armedAt = -1
            return ids
        }
        fun disarm() { armedIds = emptyList(); armedAt = -1 }
    }
}
