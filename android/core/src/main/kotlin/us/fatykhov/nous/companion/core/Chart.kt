package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneOffset

/** Port of `chart.ts` — pure geometry, DOM-free (F094 §2, spec §8.5). */
data class ReadSeries(
    val ok: Boolean,
    val points: List<JsonObject>,
    val unit: String,
    val reason: String? = null,
    val downsampledFrom: Int? = null,
    val focusFrom: String? = null,
    val shape: String? = null,
)

enum class Tone(val key: String) { NEUTRAL("neutral"), OK("ok"), WARN("warn"), CRIT("crit") }
enum class Degenerate { EMPTY, SINGLE, FLAT, OK }
data class Domain(val min: Double, val max: Double, val zeroBreak: Boolean)
data class IV(val i: Int, val v: Double)

object Chart {
    fun readSeries(value: JsonElement?): ReadSeries {
        val o = value as? JsonObject
        if (o != null && o["kind"]?.stringOrNull == "series" && o["points"] is JsonArray) {
            val meta = o["meta"] as? JsonObject
            val points = (o["points"] as JsonArray).map { (it as? JsonObject) ?: JsonObject(emptyMap()) }
            return ReadSeries(
                ok = true, points = points,
                unit = o["unit"]?.stringOrNull ?: "",
                reason = meta?.get("reason")?.stringOrNull,
                downsampledFrom = (meta?.get("downsampled_from") as? JsonPrimitive)?.takeIf { !it.isString }?.doubleOrNull?.toInt(),
                focusFrom = meta?.get("focus_from")?.stringOrNull,
            )
        }
        val shape = when {
            value is JsonArray -> "array"
            value == null || value is kotlinx.serialization.json.JsonNull -> "nothing"
            value is JsonObject -> "object"
            value is JsonPrimitive && value.isString -> "string"
            value is JsonPrimitive && value.booleanOrNull != null -> "boolean"
            else -> "number"
        }
        return ReadSeries(false, emptyList(), "", shape = shape)
    }

    fun normalizeTone(raw: JsonElement?): Tone = Tone.entries.firstOrNull { it.key == raw?.stringOrNull } ?: Tone.NEUTRAL

    /** Nth series slot from the renderer-owned ramp (1-based clamp to 4). */
    fun seriesSlot(index: Int): Int = minOf(4, maxOf(1, index + 1))

    private fun finiteAt(p: JsonObject, key: String): Double? =
        (p[key] as? JsonPrimitive)?.takeIf { !it.isString }?.doubleOrNull?.takeIf { it.isFinite() }

    fun seriesValues(points: List<JsonObject>, key: String = "v"): List<IV> =
        points.mapIndexedNotNull { i, p -> finiteAt(p, key)?.let { IV(i, it) } }

    fun countDropped(points: List<JsonObject>, keys: List<String>): Int =
        points.count { p -> keys.all { finiteAt(p, it) == null } }

    fun classify(values: List<Double>): Degenerate = when {
        values.isEmpty() -> Degenerate.EMPTY
        values.size == 1 -> Degenerate.SINGLE
        values.all { it == values[0] } -> Degenerate.FLAT
        else -> Degenerate.OK
    }

    fun yDomain(values: List<Double>, zeroBase: Boolean): Domain {
        if (values.isEmpty()) return Domain(0.0, 1.0, false)
        val min = values.min(); val max = values.max()
        if (zeroBase) return Domain(minOf(0.0, min), maxOf(0.0, max), false)
        if (min == max) { val pad = if (Math.abs(min) > 0) Math.abs(min) * 0.1 else 1.0; return Domain(min - pad, max + pad, false) }
        val span = max - min; val pad = span * 0.08
        val pMin = min - pad; val pMax = max + pad
        return Domain(pMin, pMax, pMin > 0 || pMax < 0)
    }

    fun yScale(domain: Domain, height: Double, pad: Double): (Double) -> Double {
        val span = (domain.max - domain.min).let { if (it == 0.0) 1.0 else it }
        val usable = height - 2 * pad
        return { v -> pad + usable * (1 - (v - domain.min) / span) }
    }

    fun xScale(count: Int, width: Double, pad: Double): (Int) -> Double {
        val usable = width - 2 * pad
        return { i -> if (count <= 1) width / 2 else pad + (usable * i) / (count - 1) }
    }

    /** Gap-aware polyline segments; a dropped reading breaks the line. */
    fun lineSegments(finite: List<IV>, x: (Int) -> Double, y: (Double) -> Double): List<String> {
        val segs = mutableListOf<String>(); var cur = mutableListOf<String>(); var prev = -2
        for ((i, v) in finite) {
            if (i != prev + 1 && cur.isNotEmpty()) { segs.add(cur.joinToString(" ")); cur = mutableListOf() }
            cur.add("${Functions.toFixed(x(i), 1)},${Functions.toFixed(y(v), 1)}"); prev = i
        }
        if (cur.isNotEmpty()) segs.add(cur.joinToString(" "))
        return segs
    }

    /** Compact numeric tick label (renderer-owned; JS toFixed semantics). */
    fun formatTick(v: Double): String {
        val abs = Math.abs(v)
        if (abs >= 1e6) return Functions.toFixed(v / 1e6, 1).removeSuffix(".0") + "M"
        if (abs >= 1e3) return Functions.toFixed(v / 1e3, 1).removeSuffix(".0") + "k"
        if (abs >= 100 || v == Math.floor(v)) return Functions.jsNumber(Math.round(v).toDouble())
        return Functions.toFixed(v, if (abs >= 10) 1 else 2)
    }

    fun ticks(domain: Domain, count: Int = 3): List<Double> {
        val span = domain.max - domain.min
        return (0 until count).map { k -> domain.min + (span * k) / (count - 1) }
    }

    fun trendWindow(n: Int): Int = maxOf(3, Math.round(n / 8.0).toInt())

    /** Trailing rolling mean PER RUN of consecutive indices — a gap resets the window. */
    fun rollingMean(finite: List<IV>, window: Int): List<IV> {
        val w = maxOf(1, window)
        val out = mutableListOf<IV>(); var run = mutableListOf<IV>()
        fun flush() {
            for (k in run.indices) {
                val lo = maxOf(0, k - w + 1)
                var sum = 0.0; for (j in lo..k) sum += run[j].v
                out.add(IV(run[k].i, sum / (k - lo + 1)))
            }
            run = mutableListOf()
        }
        var prev = -2
        for (p in finite) { if (p.i != prev + 1 && run.isNotEmpty()) flush(); run.add(p); prev = p.i }
        flush()
        return out
    }

    private val NAIVE = Regex("""^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$""")

    /** `Date.parse` with the producer's convention: a naive datetime is UTC. Null = NaN. */
    fun parseInstant(t: String): Long? {
        val s = t.trim()
        val iso = if (NAIVE.matches(s)) s.replace(' ', 'T') + "Z" else s
        try { return OffsetDateTime.parse(iso).toInstant().toEpochMilli() } catch (_: Exception) {}
        try { return LocalDate.parse(iso).atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli() } catch (_: Exception) {}
        return null
    }

    fun focusStartIndex(points: List<JsonObject>, focusFrom: String?): Int? {
        if (focusFrom.isNullOrEmpty()) return null
        val target = parseInstant(focusFrom)
        val idx = points.indexOfFirst { p ->
            val t = p["t"]?.stringOrNull ?: return@indexOfFirst false
            val ts = parseInstant(t)
            if (target != null && ts != null) ts >= target else t >= focusFrom
        }
        return if (idx == -1) null else idx
    }
}
