package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import java.math.BigDecimal
import java.math.RoundingMode
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.TextStyle
import java.util.Locale

/** Template scope for `{componentId, path}` children (`Scope` in pointer.ts). */
data class Scope(val base: String, val index: Int)

data class EvalContext(val dataModel: JsonElement, val scope: Scope? = null)

/** Local ValidationResult shape (no upstream schema): {valid, message?}. */
data class ValidationResult(val valid: Boolean, val message: String? = null)

/**
 * Locale-sensitive services the plain JVM cannot provide with V8 parity
 * (Intl.NumberFormat / PluralRules / month & weekday names). `:app` binds
 * android.icu; JVM tests use [JvmFormatter], a documented stand-in.
 */
interface Formatter {
    fun number(v: Double, digits: Int?, grouping: Boolean): String
    fun currency(v: Double, currency: String, digits: Int?, grouping: Boolean): String
    fun pluralCategory(n: Double): String
    fun monthName(month: Int, short: Boolean): String
    fun weekdayName(dow: Int, short: Boolean): String
}

object JvmFormatter : Formatter {
    private val en = Locale.US
    override fun number(v: Double, digits: Int?, grouping: Boolean): String {
        val nf = java.text.NumberFormat.getNumberInstance(en)
        nf.roundingMode = RoundingMode.HALF_UP   // Intl rounds half-expand; JVM defaults to half-even
        nf.isGroupingUsed = grouping
        if (digits != null) { nf.minimumFractionDigits = digits; nf.maximumFractionDigits = digits }
        else { nf.maximumFractionDigits = 3 }
        return nf.format(v)
    }
    override fun currency(v: Double, currency: String, digits: Int?, grouping: Boolean): String {
        val nf = java.text.NumberFormat.getCurrencyInstance(en)
        nf.roundingMode = RoundingMode.HALF_UP
        nf.currency = java.util.Currency.getInstance(currency)
        nf.isGroupingUsed = grouping
        if (digits != null) { nf.minimumFractionDigits = digits; nf.maximumFractionDigits = digits }
        return nf.format(v)
    }
    override fun pluralCategory(n: Double): String = if (n == 1.0) "one" else "other"
    override fun monthName(month: Int, short: Boolean) =
        java.time.Month.of(month).getDisplayName(if (short) TextStyle.SHORT else TextStyle.FULL, en)
    override fun weekdayName(dow: Int, short: Boolean) =
        java.time.DayOfWeek.of(dow).getDisplayName(if (short) TextStyle.SHORT else TextStyle.FULL, en)
}

/** Effects the function table can trigger; the app supplies the real one. */
fun interface UrlOpener { fun open(url: String) }

/**
 * Port of `functions.ts`: dynamic-value resolution and the basic-catalog
 * function table (spec §3.2 R13 + formatting parity rules).
 */
class Functions(
    private val fmt: Formatter = JvmFormatter,
    private val opener: UrlOpener = UrlOpener { },
    private val zone: ZoneId = ZoneId.of("UTC"),
) {
    companion object {
        fun isFunctionCall(v: JsonElement?): Boolean = v is JsonObject && v["call"]?.stringOrNull != null
        fun isDataBinding(v: JsonElement?): Boolean = v is JsonObject && v["path"]?.stringOrNull != null && !isFunctionCall(v)
        fun isTruncationMarker(v: JsonElement?): Boolean =
            v is JsonObject && (v["_truncated"] as? JsonPrimitive)?.booleanOrNull == true

        /** Real rows and the omitted count (null when no marker). */
        fun splitTruncation(rows: List<JsonElement>): Pair<List<JsonElement>, Int?> {
            var omitted: Int? = null
            val kept = rows.filter { r ->
                if (!isTruncationMarker(r)) true else {
                    val n = ((r as JsonObject)["omitted"] as? JsonPrimitive)?.doubleOrNull ?: 0.0
                    omitted = (omitted ?: 0) + (if (n > 0) n.toInt() else 0); false
                }
            }
            return kept to omitted
        }

        fun omittedNote(omitted: Int?): String = when {
            omitted == null -> ""
            omitted > 0 -> "…and $omitted more not shown (source over budget)"
            else -> "…more not shown (source over budget)"
        }

        fun isSafeUrl(url: String) = Markdown.isSafeUrl(url)

        /** A2UI display conversion: null → "", strings as-is, numbers JS-style, objects as JSON. */
        fun toDisplayString(v: JsonElement?): String = when {
            v == null || v is JsonNull -> ""
            v is JsonPrimitive && v.isString -> v.content
            v is JsonPrimitive -> v.booleanOrNull?.toString() ?: v.doubleOrNull?.let { jsNumber(it) } ?: v.content
            else -> v.toString()
        }

        /** JS `String(number)`: integral values print without ".0". */
        fun jsNumber(d: Double): String {
            if (d.isNaN()) return "NaN"
            if (d.isInfinite()) return if (d > 0) "Infinity" else "-Infinity"
            if (d == Math.floor(d) && Math.abs(d) < 1e21) return d.toLong().toString()
            return d.toString()
        }

        /** JS `Number(x)`: "" → 0, non-numeric string → NaN, bool → 0/1, null → 0. */
        fun jsNumberOf(v: JsonElement?): Double = when {
            v == null || v is JsonNull -> 0.0
            v is JsonPrimitive && v.isString -> { val s = v.content.trim(); if (s.isEmpty()) 0.0 else s.toDoubleOrNull() ?: Double.NaN }
            v is JsonPrimitive -> v.booleanOrNull?.let { if (it) 1.0 else 0.0 } ?: v.doubleOrNull ?: Double.NaN
            else -> Double.NaN
        }

        /** JS `Number.prototype.toFixed` on the double's exact binary value. */
        fun toFixed(d: Double, digits: Int): String =
            BigDecimal(d).setScale(digits, RoundingMode.HALF_UP).toPlainString()

        fun toBool(v: JsonElement?): Boolean = when {
            v == null || v is JsonNull -> false
            v is JsonObject && v.containsKey("valid") -> (v["valid"] as? JsonPrimitive)?.booleanOrNull == true
            v is JsonPrimitive && v.isString -> v.content.isNotEmpty()
            v is JsonPrimitive -> v.booleanOrNull ?: v.doubleOrNull?.let { it != 0.0 && !it.isNaN() } ?: true
            else -> true
        }

        private fun vr(ok: Boolean, msg: String) = JsonObject(
            mapOf("valid" to JsonPrimitive(ok)) + (if (ok) emptyMap() else mapOf("message" to JsonPrimitive(msg)))
        )

        fun clampDigits(d: Double?): Int? = if (d == null || d.isNaN() || d.isInfinite()) null else minOf(100, maxOf(0, d.toLong().toInt()))

        fun flexGrow(weight: JsonElement?): Double? = (weight as? JsonPrimitive)?.takeIf { !it.isString }?.doubleOrNull?.takeIf { it.isFinite() }
    }

    fun resolveDynamic(value: JsonElement?, ctx: EvalContext): JsonElement? {
        if (isDataBinding(value)) return Pointer.get(ctx.dataModel, Pointer.absolute((value as JsonObject)["path"]!!.stringOrNull!!, ctx.scope?.base))
        if (isFunctionCall(value)) {
            val o = value as JsonObject
            return callFunction(o["call"]!!.stringOrNull!!, (o["args"] as? JsonObject) ?: JsonObject(emptyMap()), ctx)
        }
        return value
    }

    private fun arg(args: JsonObject, key: String, ctx: EvalContext) = resolveDynamic(args[key], ctx)
    private fun num(args: JsonObject, key: String, ctx: EvalContext, default: Double): Double =
        if (args.containsKey(key)) jsNumberOf(arg(args, key, ctx)) else default

    fun callFunction(name: String, args: JsonObject, ctx: EvalContext): JsonElement? = when (name) {
        "@index" -> {
            val scope = ctx.scope ?: throw IllegalStateException("@index used outside a collection scope")
            val offset = jsNumberOf(arg(args, "offset", ctx) ?: JsonPrimitive(0))
            JsonPrimitive(scope.index + offset)
        }
        "required" -> {
            val v = arg(args, "value", ctx)
            val ok = !(v == null || v is JsonNull || (v is JsonPrimitive && v.isString && v.content == "") || (v is JsonArray && v.isEmpty()))
            vr(ok, "Required.")
        }
        "regex" -> {
            val v = toDisplayString(arg(args, "value", ctx))
            val pattern = toDisplayString(arg(args, "pattern", ctx) ?: JsonPrimitive(""))
            val ok = try { Regex(pattern).containsMatchIn(v) } catch (_: Exception) { false }
            vr(ok, "Invalid format.")
        }
        "length" -> {
            val v = toDisplayString(arg(args, "value", ctx))
            val ok = v.length >= num(args, "min", ctx, Double.NEGATIVE_INFINITY) && v.length <= num(args, "max", ctx, Double.POSITIVE_INFINITY)
            vr(ok, "Length out of range.")
        }
        "numeric" -> {
            val v = jsNumberOf(arg(args, "value", ctx))
            val ok = v.isFinite() && v >= num(args, "min", ctx, Double.NEGATIVE_INFINITY) && v <= num(args, "max", ctx, Double.POSITIVE_INFINITY)
            vr(ok, "Out of range.")
        }
        "email" -> vr(Regex("""^[^\s@]+@[^\s@]+\.[^\s@]+$""").matches(toDisplayString(arg(args, "value", ctx))), "Invalid email address.")
        "and" -> JsonPrimitive(((args["values"] as? JsonArray) ?: JsonArray(emptyList())).all { toBool(resolveDynamic(it, ctx)) })
        "or" -> JsonPrimitive(((args["values"] as? JsonArray) ?: JsonArray(emptyList())).any { toBool(resolveDynamic(it, ctx)) })
        "not" -> JsonPrimitive(!toBool(arg(args, "value", ctx)))
        "formatString" -> JsonPrimitive(formatString(toDisplayString(arg(args, "value", ctx)), ctx))
        "formatNumber" -> {
            val v = jsNumberOf(arg(args, "value", ctx))
            val digits = clampDigits(if (args.containsKey("decimals")) jsNumberOf(arg(args, "decimals", ctx)) else null)
            val grouping = if (args.containsKey("grouping")) toBool(arg(args, "grouping", ctx)) else true
            JsonPrimitive(try { fmt.number(v, digits, grouping) } catch (_: Exception) { jsNumber(v) })
        }
        "formatCurrency" -> {
            val v = jsNumberOf(arg(args, "value", ctx))
            val currency = toDisplayString(arg(args, "currency", ctx) ?: JsonPrimitive("USD"))
            val digits = clampDigits(if (args.containsKey("decimals")) jsNumberOf(arg(args, "decimals", ctx)) else null)
            val grouping = if (args.containsKey("grouping")) toBool(arg(args, "grouping", ctx)) else true
            JsonPrimitive(try { fmt.currency(v, currency, digits, grouping) } catch (_: Exception) { "$currency ${jsNumber(v)}" })
        }
        "formatDate" -> JsonPrimitive(formatDateCldr(arg(args, "value", ctx), toDisplayString(arg(args, "format", ctx) ?: JsonPrimitive("yyyy-MM-dd"))))
        "pluralize" -> {
            val count = jsNumberOf(arg(args, "value", ctx))
            val category = if (count.isFinite()) fmt.pluralCategory(count) else "other"
            val chosen = if (args.containsKey(category)) args[category] else args["other"]
            JsonPrimitive(toDisplayString(resolveDynamic(chosen, ctx)))
        }
        "openUrl" -> { val url = toDisplayString(arg(args, "url", ctx)); if (isSafeUrl(url)) opener.open(url); null }
        else -> null
    }

    // ---------------------------------------------------------- formatString

    fun formatString(template: String, ctx: EvalContext): String {
        val out = StringBuilder()
        var i = 0
        while (i < template.length) {
            if (template[i] == '\\' && template.startsWith("\${", i + 1)) { out.append("\${"); i += 3 }
            else if (template[i] == '$' && i + 1 < template.length && template[i + 1] == '{') {
                val (expr, next) = scanBraced(template, i + 2)
                out.append(toDisplayString(evalExpression(expr.trim(), ctx))); i = next
            } else { out.append(template[i]); i += 1 }
        }
        return out.toString()
    }

    private fun scanBraced(s: String, start: Int): Pair<String, Int> {
        var depth = 1; var i = start; var quote: Char? = null
        while (i < s.length) {
            val c = s[i]
            if (quote != null) { if (c == quote) quote = null }
            else if (c == '\'' || c == '"') quote = c
            else if (c == '{') depth += 1
            else if (c == '}') { depth -= 1; if (depth == 0) return s.substring(start, i) to i + 1 }
            i += 1
        }
        return s.substring(start) to i
    }

    private val FN = Regex("""^(@?[A-Za-z_]\w*)\((.*)\)$""", RegexOption.DOT_MATCHES_ALL)
    private val NUM = Regex("""^-?\d+(\.\d+)?$""")

    private fun evalExpression(expr: String, ctx: EvalContext): JsonElement? {
        if (expr == "") return JsonPrimitive("")
        if (expr.startsWith("\${") && expr.endsWith("}")) return evalExpression(expr.substring(2, expr.length - 1).trim(), ctx)
        if ((expr.startsWith("'") && expr.endsWith("'")) || (expr.startsWith("\"") && expr.endsWith("\""))) return JsonPrimitive(expr.substring(1, expr.length - 1))
        FN.find(expr)?.let { m ->
            val args = LinkedHashMap<String, JsonElement>()
            for ((k, v) in splitNamedArgs(m.groupValues[2])) evalExpression(v.trim(), ctx)?.let { args[k] = it } ?: run { args[k] = JsonNull }
            return callFunction(m.groupValues[1], JsonObject(args), ctx)
        }
        if (NUM.matches(expr)) return JsonPrimitive(expr.toDouble())
        if (expr == "true") return JsonPrimitive(true)
        if (expr == "false") return JsonPrimitive(false)
        return Pointer.get(ctx.dataModel, Pointer.absolute(expr, ctx.scope?.base))
    }

    private fun splitNamedArgs(src: String): List<Pair<String, String>> {
        val parts = mutableListOf<String>(); val cur = StringBuilder(); var depth = 0; var quote: Char? = null
        for (c in src) {
            if (quote != null) { if (c == quote) quote = null; cur.append(c) }
            else if (c == '\'' || c == '"') { quote = c; cur.append(c) }
            else if (c == '(' || c == '{') { depth += 1; cur.append(c) }
            else if (c == ')' || c == '}') { depth -= 1; cur.append(c) }
            else if (c == ',' && depth == 0) { parts.add(cur.toString()); cur.setLength(0) }
            else cur.append(c)
        }
        if (cur.isNotBlank()) parts.add(cur.toString())
        return parts.mapNotNull { p -> val idx = topLevelColon(p); if (idx == -1) null else p.substring(0, idx).trim() to p.substring(idx + 1) }
    }

    private fun topLevelColon(s: String): Int {
        var depth = 0; var quote: Char? = null
        for ((i, c) in s.withIndex()) {
            if (quote != null) { if (c == quote) quote = null }
            else if (c == '\'' || c == '"') quote = c
            else if (c == '(' || c == '{') depth += 1
            else if (c == ')' || c == '}') depth -= 1
            else if (c == ':' && depth == 0) return i
        }
        return -1
    }

    // ------------------------------------------------------------ formatDate

    /** Minimal CLDR pattern formatter. Date-only strings are CALENDAR dates (local, not UTC midnight). */
    fun formatDateCldr(value: JsonElement?, pattern: String): String {
        val raw = toDisplayString(value)
        val d: ZonedDateTime = Regex("""^(\d{4})-(\d{2})-(\d{2})$""").find(raw)?.let { m ->
            LocalDate.of(m.groupValues[1].toInt(), m.groupValues[2].toInt(), m.groupValues[3].toInt()).atStartOfDay(zone)
        } ?: run {
            try { OffsetDateTime.parse(raw).atZoneSameInstant(zone) } catch (_: Exception) {
                try { LocalDateTime.parse(raw).atZone(zone) } catch (_: Exception) { return "" }
            }
        }
        val h12 = ((d.hour + 11) % 12) + 1
        val tokens: List<Pair<String, () -> String>> = listOf(
            "yyyy" to { d.year.toString().padStart(4, '0') },
            "yy" to { (d.year % 100).toString().padStart(2, '0') },
            "MMMM" to { fmt.monthName(d.monthValue, false) },
            "MMM" to { fmt.monthName(d.monthValue, true) },
            "MM" to { d.monthValue.toString().padStart(2, '0') },
            "M" to { d.monthValue.toString() },
            "dd" to { d.dayOfMonth.toString().padStart(2, '0') },
            "d" to { d.dayOfMonth.toString() },
            "EEEE" to { fmt.weekdayName(d.dayOfWeek.value, false) },
            "EEE" to { fmt.weekdayName(d.dayOfWeek.value, true) },
            "E" to { fmt.weekdayName(d.dayOfWeek.value, true) },
            "HH" to { d.hour.toString().padStart(2, '0') },
            "H" to { d.hour.toString() },
            "hh" to { h12.toString().padStart(2, '0') },
            "h" to { h12.toString() },
            "mm" to { d.minute.toString().padStart(2, '0') },
            "ss" to { d.second.toString().padStart(2, '0') },
            "a" to { if (d.hour < 12) "AM" else "PM" },
        )
        val out = StringBuilder(); var i = 0
        while (i < pattern.length) {
            if (pattern[i] == '\'') {
                val end = pattern.indexOf('\'', i + 1)
                out.append(if (end == -1) pattern.substring(i + 1) else pattern.substring(i + 1, end))
                i = if (end == -1) pattern.length else end + 1; continue
            }
            val hit = tokens.firstOrNull { pattern.startsWith(it.first, i) }
            if (hit != null) { out.append(hit.second()); i += hit.first.length } else { out.append(pattern[i]); i += 1 }
        }
        return out.toString()
    }

    // ---------------------------------------------------------------- checks

    /** Evaluate `checks`; returns failures (empty = all pass). */
    fun runChecks(checks: JsonArray?, ctx: EvalContext): List<ValidationResult> {
        if (checks == null || checks.isEmpty()) return emptyList()
        val failures = mutableListOf<ValidationResult>()
        for (rule in checks) {
            val r = rule as? JsonObject ?: continue
            // A throwing condition is `{valid:false}` with NO message, so the rule's own message wins.
            val result = try { resolveDynamic(r["condition"], ctx) } catch (_: Exception) { JsonObject(mapOf("valid" to JsonPrimitive(false))) }
            val normalized = if (result is JsonObject && result.containsKey("valid"))
                ValidationResult((result["valid"] as? JsonPrimitive)?.booleanOrNull == true, result["message"]?.stringOrNull)
            else ValidationResult(toBool(result))
            if (!normalized.valid) failures.add(normalized.copy(message = normalized.message ?: r["message"]?.stringOrNull ?: "Invalid."))
        }
        return failures
    }
}
