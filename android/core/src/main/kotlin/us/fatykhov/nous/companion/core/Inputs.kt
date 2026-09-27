package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.booleanOrNull
import java.time.OffsetDateTime
import java.time.ZoneId

/**
 * Two-way binding for the basic-catalog inputs (PR 4), ported from the
 * seven `*View.svelte` adapters. Keystrokes update the surface data model
 * LOCALLY and never touch the network; state ships only on an action.
 *
 * The one write primitive: [Inputs.write] resolves the component's bound
 * path against the scope and replaces the surface's model through
 * `Pointer.set`. Unbound inputs (a literal `value`) are read-only.
 */
object Inputs {
    /** The absolute bound path of a `value` prop, or null when it is a literal / function call. */
    fun boundPath(comp: JsonObject, scope: Scope?): String? {
        val v = comp["value"] ?: return null
        if (!Functions.isDataBinding(v)) return null
        return Pointer.absolute((v as JsonObject)["path"]!!.stringOrNull!!, scope?.base)
    }

    /** Current value: the bound pointer when bound, else the resolved dynamic value. */
    fun read(store: SurfaceStore, functions: Functions, surfaceId: String, comp: JsonObject, scope: Scope?): JsonElement? {
        val s = store.surfaces[surfaceId] ?: return null
        val path = boundPath(comp, scope)
        return if (path != null) Pointer.get(s.dataModel, path)
        else try { functions.resolveDynamic(comp["value"], EvalContext(s.dataModel, scope)) } catch (_: Exception) { null }
    }

    /** Write through the binding. Returns false when the input is unbound or the surface is gone. */
    fun write(store: SurfaceStore, surfaceId: String, comp: JsonObject, scope: Scope?, value: JsonElement?): Boolean {
        val path = boundPath(comp, scope) ?: return false
        return store.patchLocal(surfaceId, path, value)
    }

    // ------------------------------------------------------------- TextField
    fun textValue(raw: JsonElement?): String = Functions.toDisplayString(raw)

    // -------------------------------------------------------------- CheckBox
    /** JS `Boolean(x)` on the raw model value. */
    fun checkValue(raw: JsonElement?): Boolean = Functions.toBool(raw)

    // ---------------------------------------------------------- ChoicePicker
    data class Option(val value: String, val label: String)

    fun options(comp: JsonObject, functions: Functions, ctx: EvalContext): List<Option> =
        ((comp["options"] as? JsonArray) ?: JsonArray(emptyList())).map { o ->
            val r = o as? JsonObject ?: JsonObject(emptyMap())
            Option(
                value = r["value"]?.let { Functions.toDisplayString(it) } ?: "",
                label = Functions.toDisplayString(try { functions.resolveDynamic(r["label"], ctx) } catch (_: Exception) { null }),
            )
        }

    /** The selection is ALWAYS a string array in the model, even for mutuallyExclusive. */
    fun selected(raw: JsonElement?): List<String> =
        (raw as? JsonArray)?.map { Functions.toDisplayString(it) } ?: emptyList()

    fun choose(current: List<String>, value: String, checked: Boolean, multiple: Boolean): List<String> =
        if (!multiple) listOf(value)
        else if (checked) current + value else current.filter { it != value }

    fun filtered(options: List<Option>, filter: String, filterable: Boolean): List<Option> {
        if (!filterable || filter.trim().isEmpty()) return options
        val needle = filter.trim().lowercase()
        return options.filter { it.label.lowercase().contains(needle) }
    }

    // ---------------------------------------------------------------- Slider
    data class SliderSpec(val min: Double, val max: Double, val step: Double?)

    /** `steps` is a COUNT OF DIVISIONS: the step size is (max−min)/steps; absent = continuous. */
    fun sliderSpec(comp: JsonObject): SliderSpec {
        val min = (comp["min"] as? JsonPrimitive)?.takeIf { !it.isString }?.doubleOrNull ?: 0.0
        val max = (comp["max"] as? JsonPrimitive)?.takeIf { !it.isString }?.doubleOrNull ?: 100.0
        val steps = (comp["steps"] as? JsonPrimitive)?.takeIf { !it.isString }?.doubleOrNull
        return SliderSpec(min, max, if (steps != null && steps > 0) (max - min) / steps else null)
    }

    fun sliderValue(raw: JsonElement?, spec: SliderSpec): Double {
        val v = Functions.jsNumberOf(raw)
        return if (v.isFinite()) v else spec.min
    }

    /** Snap a continuous position to the nearest step (the DOM `step` attribute does this). */
    fun sliderSnap(v: Double, spec: SliderSpec): Double {
        val step = spec.step ?: return v
        val n = Math.round((v - spec.min) / step)
        return (spec.min + n * step).coerceIn(minOf(spec.min, spec.max), maxOf(spec.min, spec.max))
    }

    // --------------------------------------------------------- DateTimeInput
    enum class DateKind { DATE, TIME, DATETIME }

    fun dateKind(comp: JsonObject): DateKind {
        val d = (comp["enableDate"] as? JsonPrimitive)?.booleanOrNull == true
        val t = (comp["enableTime"] as? JsonPrimitive)?.booleanOrNull == true
        return if (d && t) DateKind.DATETIME else if (t) DateKind.TIME else DateKind.DATE
    }

    private val ZONE = Regex("""[zZ]$|[+-]\d{2}:\d{2}$""")

    /**
     * Normalize an incoming ISO value to what the control shows (web `normalizeForControl`):
     * a zoned instant becomes the LOCAL wall clock; a zone-less value passes through
     * truncated. Writes stay zone-less local (the documented v1 policy).
     */
    fun normalizeForControl(raw: String, kind: DateKind, zone: ZoneId = ZoneId.systemDefault()): String {
        if (raw.isEmpty()) return ""
        val hasZone = ZONE.containsMatchIn(raw)
        fun dateTime(): String {
            if (!hasZone) return raw.take(16)
            val p = try { OffsetDateTime.parse(raw).atZoneSameInstant(zone) } catch (_: Exception) { return "" }
            return "%04d-%02d-%02dT%02d:%02d".format(p.year, p.monthValue, p.dayOfMonth, p.hour, p.minute)
        }
        return when (kind) {
            DateKind.TIME -> if (raw.contains('T')) dateTime().let { if (it.length >= 16) it.substring(11, 16) else "" } else raw.take(5)
            DateKind.DATE -> if (raw.contains('T')) dateTime().take(10) else raw.take(10)
            DateKind.DATETIME -> dateTime()
        }
    }

    // ------------------------------------------------------------------ Tabs
    data class Tab(val title: String, val child: String?)

    fun tabs(comp: JsonObject, functions: Functions, ctx: EvalContext): List<Tab> =
        ((comp["tabs"] as? JsonArray) ?: JsonArray(emptyList())).map { t ->
            val r = t as? JsonObject ?: JsonObject(emptyMap())
            Tab(Functions.toDisplayString(try { functions.resolveDynamic(r["title"], ctx) } catch (_: Exception) { null }), r["child"]?.stringOrNull)
        }

    /** Clamped, not assigned: updateComponents can shrink the list under a stale index. */
    fun activeTab(requested: Int, count: Int): Int = requested.coerceIn(0, maxOf(count - 1, 0))
}
