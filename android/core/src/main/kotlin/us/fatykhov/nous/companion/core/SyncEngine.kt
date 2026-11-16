package us.fatykhov.nous.companion.core

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import kotlin.math.min
import kotlin.math.pow

/** JS `encodeURIComponent`: everything but `A-Za-z0-9-_.!~*'()` is percent-encoded; a space is %20, never `+`. */
fun encodeURIComponent(s: String): String = java.net.URLEncoder.encode(s, "UTF-8")
    .replace("+", "%20").replace("%21", "!").replace("%27", "'").replace("%28", "(").replace("%29", ")").replace("%7E", "~")

/** What the engine needs from HTTP; `:app` binds OkHttp, tests bind fakes. */
interface Http {
    class Response(val status: Int, val body: String, val headers: Map<String, String>) {
        val ok get() = status in 200..299
        fun header(name: String) = headers.entries.firstOrNull { it.key.equals(name, true) }?.value
    }
    suspend fun get(path: String): Response
    suspend fun postJson(path: String, body: String): Response
    /** PUT with a JSON body — the token registration upsert (`/a2ui/push/tokens`). */
    suspend fun putJson(path: String, body: String): Response
    /** DELETE — the only way to deregister a push installation (spec §6.3). */
    suspend fun delete(path: String): Response
    /**
     * Open the SSE stream and deliver raw text chunks until EOF/error. The
     * implementation must never send `Last-Event-ID` (spec R2) and must
     * return (not throw) on any failure; the engine reruns the cycle.
     */
    suspend fun stream(path: String, onOpen: () -> Unit, onChunk: (String) -> Unit)
}

/** [refusal] is set when the server refused for the device proof and pairing is the remedy. */
data class ActionResult(val ok: Boolean, val message: String, val resolved: Boolean? = null, val refusal: Refusal? = null)
data class CallResult(val ok: Boolean, val value: kotlinx.serialization.json.JsonElement?, val message: String, val refusal: Refusal? = null)

/**
 * Port of `transport.ts` (spec §3.2 R1, R2, R5, R8): the hydration-first
 * cycle, never auto-resume, exponential backoff capped at 30 s.
 *
 * [tokenSent] says whether calls to the configured address carry a device
 * token. A hydration read refused for the device proof stops the loop and
 * sets [refused] (a phone that is not paired gets the same answer on every
 * retry); a refused tap or call answers the app's own sentence and the loop
 * goes on.
 */
class SyncEngine(
    private val store: SurfaceStore,
    private val http: Http,
    private val scope: CoroutineScope,
    private val backoffMs: Long = 2000,
    private val nowMs: () -> Long = System::currentTimeMillis,
    private val sleep: suspend (Long) -> Unit = { delay(it) },
    private val tokenSent: () -> Boolean = { false },
) {
    private var job: Job? = null
    private var stopped = false
    var attempt = 0; private set
    var reconnects = 0; private set
    var lastError: String? = null; private set
    /** Why the loop stopped for the device proof; null while it runs. */
    var refused: Refusal? = null; private set

    fun connect() { stopped = false; refused = null; job?.cancel(); job = scope.launch { run() } }
    fun stop() { stopped = true; job?.cancel(); job = null }

    /**
     * The connection loop: a cycle, then either an immediate rerun (server
     * resync) or a backoff sleep. A LOOP, not recursion — a stream that ends
     * instantly must not grow the stack.
     */
    internal suspend fun run() {
        while (!stopped && scope.isActive) {
            val immediate = cycle()
            if (stopped) return
            if (!immediate) sleep(min(backoffMs * 2.0.pow(min(attempt, 4)).toLong(), 30_000L))
        }
    }

    /** R1. One full hydration then a stream tail. Returns true when the next cycle should run at once (resync). */
    internal suspend fun cycle(): Boolean {
        if (stopped) return false
        store.connection = if (attempt == 0) Connection.CONNECTING else Connection.RESYNCING
        try {
            val index = http.get("/a2ui/surfaces")
            if (!index.ok) refusedRead(index)?.let { return it }
            if (!index.ok) throw IllegalStateException("HTTP ${index.status} for /a2ui/surfaces")
            val idx = Json.parseToJsonElement(index.body).jsonObject
            val surfaces = (idx["surfaces"] as? JsonArray) ?: JsonArray(emptyList())
            val ids = surfaces.mapNotNull { (it as? JsonObject)?.get("surface_id")?.stringOrNull }
            store.pruneAbsent(ids.toSet())
            for (id in ids) {
                val res = http.get("/a2ui/surfaces/" + encodeURIComponent(id))
                if (!res.ok) refusedRead(res)?.let { return it }
                if (!res.ok) throw IllegalStateException("HTTP ${res.status} for surface $id")   // a 404 aborts the whole cycle
                store.apply(null, Json.parseToJsonElement(res.body).jsonObject)
                val upto = res.header("X-A2UI-Upto-Seq")?.toLongOrNull() ?: 0L
                if (upto > 0) store.setSurfaceUpto(id, upto)
            }
            store.setDeliveredFloor(idx["latest_seq"]?.jsonPrimitive?.longOrNull ?: 0L)
            // LIVE is published from the stream's onOpen, not here. The web
            // sets 'live' right after CONSTRUCTING its EventSource (transport.ts),
            // which is before the socket opens — so a hydration that succeeds
            // while the stream endpoint is unreachable presents stale snapshots
            // as current with actions enabled, and resets the backoff counter
            // every cycle so the retry never grows past its first tier. The
            // OkHttp transport has a real open (the 200), so this port uses it;
            // the same fix is owed to the web (codex round 1 on #659).
            val parser = SseParser()
            var resync = false
            var opened = false
            http.stream("/a2ui/stream?since=${store.lastSeq}", onOpen = {
                opened = true
                store.connection = Connection.LIVE
                attempt = 0
            }) { chunk ->
                for (ev in parser.feed(chunk)) when (ev.event) {
                    "a2ui" -> store.apply(ev.id, Json.parseToJsonElement(ev.data).jsonObject)
                    "control" -> if ((Json.parseToJsonElement(ev.data) as? JsonObject)?.get("type")?.stringOrNull == "resync") resync = true
                }
            }
            // Stream ended (EOF, error, or resync request). R2/R5: never resume; rehydrate.
            if (opened) reconnects += 1
            if (resync) { store.resync(); return true }
            lastError = if (opened) "stream ended" else "stream did not open"
            store.connection = Connection.ERROR; attempt += 1
            return false
        } catch (e: kotlinx.coroutines.CancellationException) { throw e }
        catch (e: Exception) {
            // Class name too: a Kotlin `!!` NPE carries no message, and "none" hides the failure.
            lastError = e.javaClass.simpleName + (e.message?.let { ": $it" } ?: "")
            store.connection = Connection.ERROR
            attempt += 1
            return false
        }
    }

    /**
     * A hydration read refused for the device proof, or null when [res] is not
     * such a refusal. Not paired, or a token the server rejects: the loop
     * stops (a retry gets the same answer) until [connect] runs again. A
     * token the server could not check: the normal backoff.
     */
    private fun refusedRead(res: Http.Response): Boolean? {
        val r = Refusal.of(res.status, res.body) ?: return null
        if (r == Refusal.NOT_PAIRED && tokenSent()) {
            lastError = PairingWords.NOT_CHECKED
            store.connection = Connection.ERROR; attempt += 1
            return false
        }
        refused = r; stopped = true
        // Short, for Diagnostics: the screens say it in full from [refused].
        lastError = if (r == Refusal.TOKEN_REJECTED) "device token rejected (HTTP 403)" else "not paired (HTTP 403)"
        store.connection = Connection.ERROR
        return false
    }

    /** The sentence for a refused tap or call, and the refusal when pairing is the remedy. */
    private fun refusedTap(r: Refusal): Pair<String, Refusal?> = when {
        r == Refusal.TOKEN_REJECTED -> PairingWords.TOKEN_REJECTED to r
        tokenSent() -> PairingWords.NOT_CHECKED to null
        else -> PairingWords.TAP_NOT_PAIRED to r
    }

    /** R8: a rejection resolves, never throws. Nonce inside `action`; the model rides along. */
    suspend fun postAction(surfaceId: String, name: String, sourceComponentId: String, context: JsonObject): ActionResult {
        val s = store.surfaces[surfaceId]
        val body = buildJsonObject {
            put("version", "v1.0")
            put("action", buildJsonObject {
                put("name", name); put("surfaceId", surfaceId); put("sourceComponentId", sourceComponentId)
                // JS toISOString() always carries 3 fraction digits; Instant.toString() varies.
                put("timestamp", java.time.format.DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss.SSS'Z'")
                    .withZone(java.time.ZoneOffset.UTC).format(java.time.Instant.ofEpochMilli(nowMs())))
                put("context", context)
                put("metadata", buildJsonObject { put("extensions", buildJsonObject { put("com_nous_nonce", s?.nonce ?: "") }) })
            })
            put("a2uiRendererDataModel", buildJsonObject {
                put("version", "v1.0")
                put("surfaces", buildJsonObject { put(surfaceId, s?.dataModel ?: JsonObject(emptyMap())) })
            })
        }
        return try {
            val res = http.postJson("/a2ui/action", body.toString())
            val p = runCatching { Json.parseToJsonElement(res.body).jsonObject }.getOrNull()
            val refusal = if (res.ok) null else Refusal.of(res.status, res.body)
            if (refusal != null) refusedTap(refusal).let { (words, r) -> ActionResult(false, words, refusal = r) }
            else if (!res.ok) ActionResult(false, ((p?.get("error") as? JsonObject)?.get("message")?.stringOrNull) ?: "HTTP ${res.status}")
            else ActionResult(true, p?.get("message")?.stringOrNull ?: "", (p?.get("resolved") as? JsonPrimitive)?.let { it.content.toBooleanStrictOrNull() })
        } catch (e: Exception) { ActionResult(false, e.message ?: "network error") }
    }

    /** Nonce at the TOP level for /a2ui/call (spec §3.1). */
    suspend fun callAgentFunction(surfaceId: String, call: String, args: JsonObject): CallResult {
        val s = store.surfaces[surfaceId]
        val body = buildJsonObject {
            put("version", "v1.0")
            put("callAgentFunction", buildJsonObject {
                put("surfaceId", surfaceId)
                put("functionCallId", "fc-${nowMs()}-${java.util.UUID.randomUUID().toString().take(6)}")
                put("callFunction", buildJsonObject { put("call", call); put("args", args) })
            })
            put("metadata", buildJsonObject { put("extensions", buildJsonObject { put("com_nous_nonce", s?.nonce ?: "") }) })
        }
        return try {
            val res = http.postJson("/a2ui/call", body.toString())
            val p = runCatching { Json.parseToJsonElement(res.body).jsonObject }.getOrNull()
            val afr = p?.get("agentFunctionResponse") as? JsonObject
            val err = afr?.get("error") as? JsonObject
            val refusal = if (res.ok) null else Refusal.of(res.status, res.body)
            if (refusal != null) refusedTap(refusal).let { (words, r) -> CallResult(false, null, words, refusal = r) }
            else if (!res.ok || err != null) CallResult(false, null, err?.get("message")?.stringOrNull ?: "HTTP ${res.status}")
            else CallResult(true, afr?.get("value"), "")
        } catch (e: Exception) { CallResult(false, null, e.message ?: "network error") }
    }
}
