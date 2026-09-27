package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonObject

/** One live surface as the client holds it (mirror of `SurfaceState`). */
data class SurfaceState(
    val surfaceId: String,
    val catalogId: String,
    val components: Map<String, JsonObject>,
    val dataModel: JsonElement,
    val nonce: String,
    val priority: Int,
    /** F093 theme id; empty = nous-default. */
    val theme: String,
    val title: String,
)

enum class Connection { CONNECTING, LIVE, RESYNCING, ERROR }

/**
 * Port of `store.svelte.ts` (spec §3.2 R3, R4, R5, R7, R9). Pure Kotlin:
 * observers subscribe through [onChange]; the clock is injected so tests
 * hand-feed time instead of faking timers.
 *
 * Not thread-safe — the app confines it to the main dispatcher.
 */
class SurfaceStore(private val nowMs: () -> Long = System::currentTimeMillis) {
    private val _surfaces = LinkedHashMap<String, SurfaceState>()
    val surfaces: Map<String, SurfaceState> get() = _surfaces
    var connection: Connection = Connection.CONNECTING
        set(v) { field = v; changed() }

    val activity = HashMap<String, Activity>()
    val doneAt = HashMap<String, Long>()
    val tappedAt = HashMap<String, Long>()
    val stampSeen = HashMap<String, String>()

    /** Highest seq seen — the resume point, NOT the dedupe test. */
    var lastSeq: Long = 0; private set
    private var seenFloor = 0L
    private var seen = HashSet<Long>()
    private val surfaceUpto = HashMap<String, Long>()
    private var activitySeq = 0L

    private val listeners = java.util.concurrent.CopyOnWriteArrayList<() -> Unit>()
    fun onChange(l: () -> Unit) { listeners += l }
    fun removeListener(l: () -> Unit) { listeners -= l }
    private fun changed() { listeners.forEach { it() } }

    private fun markSeen(seq: Long): Boolean {
        if (seq <= seenFloor || seq in seen) return false
        seen.add(seq)
        // Advance the floor ONLY through the contiguous prefix.
        while (seen.contains(seenFloor + 1)) { seenFloor += 1; seen.remove(seenFloor) }
        return true
    }

    /** Record a snapshot watermark. Deliberately does NOT raise [lastSeq]. */
    fun setSurfaceUpto(surfaceId: String, uptoSeq: Long) {
        surfaceUpto[surfaceId] = maxOf(surfaceUpto[surfaceId] ?: 0L, uptoSeq)
        settleModelHold(surfaceId)
    }

    private fun targetOf(env: JsonObject): String? {
        for (k in listOf("createSurface", "updateComponents", "updateDataModel", "deleteSurface")) {
            val id = (env[k] as? JsonObject)?.get("surfaceId")?.stringOrNull
            if (id != null) return id
        }
        return null
    }

    /** Apply one envelope; seq=null bypasses dedupe (snapshot hydration). */
    fun apply(seq: Long?, envelope: JsonObject) {
        if (seq != null) {
            if (!markSeen(seq)) return
            lastSeq = maxOf(lastSeq, seq)
            val target = targetOf(envelope)
            if (target != null && seq <= (surfaceUpto[target] ?: 0L)) return
        }
        (envelope["createSurface"] as? JsonObject)?.let { cs ->
            val id = cs["surfaceId"]?.stringOrNull ?: return
            val ext = ((cs["metadata"] as? JsonObject)?.get("extensions") as? JsonObject) ?: JsonObject(emptyMap())
            val comps = LinkedHashMap<String, JsonObject>()
            (cs["components"] as? JsonArray)?.forEach { c ->
                val o = c as? JsonObject ?: return@forEach
                o["id"]?.stringOrNull?.let { comps[it] = o }
            }
            _surfaces[id] = SurfaceState(
                surfaceId = id,
                catalogId = cs["catalogId"]?.stringOrNull ?: "",
                components = comps,
                dataModel = cs["dataModel"] ?: JsonObject(emptyMap()),
                nonce = ext["com_nous_nonce"]?.let { jsToString(it) } ?: "",
                priority = (ext["com_nous_priority"] as? JsonPrimitive)?.doubleOrNull?.toInt() ?: 0,
                theme = ext["com_nous_theme"]?.let { jsToString(it) } ?: "",
                title = ext["com_nous_title"]?.let { jsToString(it) } ?: "",
            )
            observe(id, seq)
            changed(); return
        }
        (envelope["updateComponents"] as? JsonObject)?.let { uc ->
            val id = uc["surfaceId"]?.stringOrNull ?: return
            val s = _surfaces[id] ?: return
            val merged = LinkedHashMap(s.components)
            (uc["components"] as? JsonArray)?.forEach { c ->
                val o = c as? JsonObject ?: return@forEach
                o["id"]?.stringOrNull?.let { merged[it] = o }
            }
            _surfaces[id] = s.copy(components = merged)
            changed(); return
        }
        (envelope["updateDataModel"] as? JsonObject)?.let { ud ->
            val id = ud["surfaceId"]?.stringOrNull ?: return
            val s = _surfaces[id] ?: return
            val path = ud["path"]?.stringOrNull
            val value = ud["value"]?.let { if (it is JsonNull) null else it }
            val model = if (path == null || path.isEmpty() || path == "/") (value ?: JsonObject(emptyMap()))
                else (Pointer.set(s.dataModel, path, value) ?: JsonObject(emptyMap()))
            _surfaces[id] = s.copy(dataModel = model)
            observe(id, seq)
            changed(); return
        }
        (envelope["deleteSurface"] as? JsonObject)?.let { ds ->
            val id = ds["surfaceId"]?.stringOrNull ?: return
            _surfaces.remove(id)
            // Nothing in flight any more; tappedAt/doneAt stay for the replacement's create.
            activity.remove(id)
            changed()
        }
    }

    /**
     * A LOCAL two-way-binding write (an input's keystroke): patches the model
     * in place with no seq, no observe(), no network — exactly the web's
     * `setPointer(surface.dataModel, …)` from an adapter. Returns false when
     * the surface is not held.
     */
    fun patchLocal(surfaceId: String, path: String, value: JsonElement?): Boolean {
        val s = _surfaces[surfaceId] ?: return false
        _surfaces[surfaceId] = s.copy(dataModel = Pointer.set(s.dataModel, path, value) ?: JsonObject(emptyMap()))
        changed()
        return true
    }

    private fun observe(surfaceId: String, seq: Long?) {
        val meta = (_surfaces[surfaceId]?.dataModel as? JsonObject)?.get("meta") as? JsonObject
        val record = activity[surfaceId]
        if (seq != null) record?.applied?.add(seq)
        val p = ActivityRules.pendingActionOf(meta)
        if (p != null) {
            if (stampSeen[surfaceId] != p.key) {
                stampSeen[surfaceId] = p.key
                if (record?.holdFor == HoldFor.STAMP) endActivity(surfaceId, false)
            }
            ActivityRules.parseIsoMs(p.at)?.let { tappedAt[surfaceId] = it }
        } else {
            val tapped = tappedAt.remove(surfaceId)
            if (tapped != null) {
                val composedAt = meta?.get("composedAt")?.stringOrNull
                if (composedAt != null && ActivityRules.recomposedAfter(composedAt, tapped)) doneAt[surfaceId] = nowMs()
            }
        }
        settleModelHold(surfaceId)
    }

    private fun settleModelHold(surfaceId: String) {
        val r = activity[surfaceId] ?: return
        if (r.holdFor == HoldFor.MODEL && modelArrived(surfaceId, r)) endActivity(surfaceId, true)
    }

    /** Hydration-first reconnect: drop local surfaces the index no longer lists. */
    fun pruneAbsent(liveIds: Set<String>) {
        for (id in _surfaces.keys.toList()) if (id !in liveIds) {
            _surfaces.remove(id); surfaceUpto.remove(id); activity.remove(id)
        }
        changed()
    }

    /** Everything at/below the hydration watermark counts as delivered. */
    fun setDeliveredFloor(seq: Long) {
        if (seq <= seenFloor) return
        seenFloor = seq
        lastSeq = maxOf(lastSeq, seq)
        seen = seen.filterTo(HashSet()) { it > seq }
    }

    fun beginActivity(surfaceId: String, kind: ActivityKind, id: String): Long {
        val token = ++activitySeq
        activity[surfaceId] = Activity(kind, id, nowMs(), token, stampSeenBefore = stampSeen[surfaceId])
        doneAt.remove(surfaceId)
        changed()
        return token
    }

    fun endActivityIf(surfaceId: String, token: Long, ok: Boolean) {
        if (activity[surfaceId]?.token == token) endActivity(surfaceId, ok)
    }

    fun holdForStamp(surfaceId: String, token: Long): Boolean {
        val a = activity[surfaceId]
        if (a?.token != token) return false
        if (stampSeen[surfaceId] != a.stampSeenBefore) { endActivity(surfaceId, false); return false }
        activity[surfaceId] = a.copy(holdSince = nowMs(), holdFor = HoldFor.STAMP)
        return true
    }

    fun holdForModel(surfaceId: String, token: Long, seq: Long?): Boolean {
        var a = activity[surfaceId]
        if (a?.token != token) return false
        a = a.copy(seq = seq); activity[surfaceId] = a
        if (modelArrived(surfaceId, a)) { endActivity(surfaceId, true); return false }
        activity[surfaceId] = a.copy(holdSince = nowMs(), holdFor = HoldFor.MODEL)
        return true
    }

    private fun modelArrived(surfaceId: String, a: Activity): Boolean {
        if (_surfaces[surfaceId] == null || a.seq == null) return false
        return a.seq <= (surfaceUpto[surfaceId] ?: 0L) || a.applied.contains(a.seq)
    }

    fun endActivity(surfaceId: String, ok: Boolean) {
        activity.remove(surfaceId)
        if (ok) doneAt[surfaceId] = nowMs()
        changed()
    }

    fun markDone(surfaceId: String) { doneAt[surfaceId] = nowMs(); changed() }

    /** Everything, including transient activity — a fresh start. */
    fun reset() {
        resync(); activity.clear(); doneAt.clear(); tappedAt.clear(); stampSeen.clear(); changed()
    }

    /** control:resync / reconnect: surfaces and cursors restart; in-flight activity is KEPT (R5). */
    fun resync() {
        _surfaces.clear(); lastSeq = 0; seenFloor = 0; seen = HashSet(); surfaceUpto.clear(); changed()
    }

    /** Feed order: priority desc, then insertion order (stable sort). */
    fun ordered(): List<SurfaceState> = _surfaces.values.sortedByDescending { it.priority }

    private fun jsToString(e: JsonElement): String = when (e) {
        is JsonNull -> ""          // TS `?? ''` on an explicit null
        is JsonPrimitive -> e.content   // String(x) for a number gives its JS repr; nonce/theme/title are strings in practice
        else -> e.toString()
    }
}
