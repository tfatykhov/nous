package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonPrimitive
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/**
 * F092.4 activity indicator — the pure part, ported from `activity.ts`.
 * Header, footer and sections must agree on WHAT counts as working, so the
 * definitions live here once (spec §3.2 R9).
 */
enum class ActivityKind(val verb: String) {
    REFRESH("refreshing"), REFINE("rethinking layout"), ACT("agent working");
}

enum class HoldFor { STAMP, MODEL }

data class Activity(
    val kind: ActivityKind,
    /** The refine option's id, the agent action's id, or "refresh" — ids, not labels. */
    val id: String,
    /** Epoch ms; elapsed time is measured from here. */
    val startedAt: Long,
    /** Identity of the begin; a stamp-derived activity is 0 (nobody owns it). */
    val token: Long,
    val holdSince: Long? = null,
    val holdFor: HoldFor? = null,
    val stampSeenBefore: String? = null,
    /** The completion revision an ok refresh/refine response reported. */
    val seq: Long? = null,
    /** Outbox seqs applied to the surface since the record began. Exact membership. */
    val applied: MutableList<Long> = mutableListOf(),
)

data class PendingAction(val id: String, val label: String, val at: String, val staleMs: Long, val key: String)

object ActivityRules {
    const val DONE_FLASH_MS = 3000L
    const val HOLD_WAIT_MS = 10_000L
    /** F092.2 fallback only: used when a stamp predates the server's `timeout_s`. */
    const val PENDING_STALE_FALLBACK_MS = 5L * 60 * 1000

    /** Epoch ms of an ISO stamp, or null when it does not parse (JS Date.parse → NaN). */
    fun parseIsoMs(s: String?): Long? {
        if (s.isNullOrEmpty()) return null
        return try { OffsetDateTime.parse(s).toInstant().toEpochMilli() } catch (_: DateTimeParseException) { null }
    }

    /** `value.seq` when it is a positive integer, else null. */
    fun responseSeq(value: JsonElement?): Long? {
        val seq = (value as? JsonObject)?.get("seq") as? JsonPrimitive ?: return null
        if (seq.isString) return null
        val d = seq.doubleOrNull ?: return null
        if (d.isNaN() || d <= 0 || d != Math.floor(d)) return null
        return d.toLong()
    }

    /** Parse `/meta/pendingAction`. Anything malformed is "no pending action". */
    fun pendingActionOf(meta: JsonElement?): PendingAction? {
        val raw = (meta as? JsonObject)?.get("pendingAction") as? JsonObject ?: return null
        val id = raw["id"]?.let { it as? JsonPrimitive }?.takeIf { it.isString }?.content ?: return null
        val at = raw["at"]?.let { it as? JsonPrimitive }?.takeIf { it.isString }?.content ?: return null
        val label = raw["label"]?.let { it as? JsonPrimitive }?.takeIf { it.isString }?.content ?: id
        val timeoutS = raw["timeout_s"]?.let { it as? JsonPrimitive }?.takeIf { !it.isString }?.doubleOrNull
            ?.takeIf { it > 0 }
        val sub = raw["subtask_id"]?.let { it as? JsonPrimitive }?.takeIf { it.isString }?.content
        return PendingAction(
            id = id, label = label, at = at,
            staleMs = if (timeoutS != null) (timeoutS * 1000).toLong() else PENDING_STALE_FALLBACK_MS,
            key = if (!sub.isNullOrEmpty()) sub else "$id@$at",
        )
    }

    fun pendingIsFresh(p: PendingAction?, nowMs: Long): Boolean {
        if (p == null) return false
        val at = parseIsoMs(p.at) ?: return false
        return nowMs - at < p.staleMs
    }

    fun pendingActivity(meta: JsonElement?, nowMs: Long): Activity? {
        val p = pendingActionOf(meta)
        if (!pendingIsFresh(p, nowMs)) return null
        return Activity(ActivityKind.ACT, p!!.id, parseIsoMs(p.at)!!, token = 0)
    }

    /** Strict `>`: server stamps are second-precision; a same-second recompose is not a real turn. */
    fun recomposedAfter(composedAt: String, tappedAtMs: Long): Boolean {
        val c = parseIsoMs(composedAt) ?: return false
        return c > tappedAtMs
    }

    /** "4s", "1m 12s", "1h 03m" — a wait, not a lap time. */
    fun formatElapsed(ms: Long): String {
        val s = maxOf(0L, Math.floorDiv(ms, 1000L))
        if (s < 60) return "${s}s"
        val m = s / 60
        if (m < 60) return "${m}m ${(s % 60).toString().padStart(2, '0')}s"
        val h = m / 60
        return "${h}h ${(m % 60).toString().padStart(2, '0')}m"
    }
}

internal val JsonElement.stringOrNull: String?
    get() = (this as? JsonPrimitive)?.takeIf { it.isString }?.content
