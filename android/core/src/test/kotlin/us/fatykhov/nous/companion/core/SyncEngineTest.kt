package us.fatykhov.nous.companion.core

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

class SyncEngineTest {
    private fun envelope(id: String, nonce: String = "nonce-$id", priority: Int = 0) =
        """{"version":"v1.0","createSurface":{"surfaceId":"$id","catalogId":"c","sendDataModel":true,"metadata":{"extensions":{"com_nous_nonce":"$nonce","com_nous_priority":$priority}},"components":[{"id":"root","component":"Text","text":"$id"}],"dataModel":{"formData":{"choice":"yes"}}}}"""

    private val resyncFrame = "event: control\ndata: {\"type\":\"resync\"}\n\n"

    /** Scripted HTTP: GET responses by path; the stream delivers `frames` then ends (EOF). */
    open class FakeHttp(private val gets: Map<String, Http.Response>, private val frames: List<String> = emptyList()) : Http {
        val calls = mutableListOf<String>(); val posts = mutableListOf<Pair<String, String>>(); var streams = 0
        var postResponse = Http.Response(200, """{"ok":true,"message":"done","resolved":true}""", emptyMap())
        override suspend fun get(path: String): Http.Response { calls.add(path); return gets[path] ?: Http.Response(404, "{}", emptyMap()) }
        override suspend fun postJson(path: String, body: String): Http.Response { posts.add(path to body); return postResponse }
        override suspend fun stream(path: String, onOpen: () -> Unit, onChunk: (String) -> Unit) { calls.add(path); streams += 1; onOpen(); frames.forEach(onChunk) }
    }
    private fun ok(body: String, vararg h: Pair<String, String>) = Http.Response(200, body, h.toMap())
    private val unconfined = CoroutineScope(Dispatchers.Unconfined)

    @Test fun prunesZombieAndHydratesBeforeStream() = runTest {
        val store = SurfaceStore()
        store.apply(null, Json.parseToJsonElement(envelope("zombie")).jsonObject)
        val http = FakeHttp(mapOf(
            "/a2ui/surfaces" to ok("""{"latest_seq":5,"surfaces":[{"surface_id":"live1"}]}"""),
            "/a2ui/surfaces/live1" to ok(envelope("live1"), "X-A2UI-Upto-Seq" to "4"),
        ))
        SyncEngine(store, http, unconfined, nowMs = { 1_000 }).cycle()
        assertNull(store.surfaces["zombie"]); assertNotNull(store.surfaces["live1"])
        assertEquals(listOf("/a2ui/surfaces", "/a2ui/surfaces/live1", "/a2ui/stream?since=5"), http.calls)
        assertEquals(5, store.lastSeq)
        // the header watermark suppresses seq 4 for live1
        store.apply(4, Json.parseToJsonElement("""{"updateDataModel":{"surfaceId":"live1","path":"/x","value":1}}""").jsonObject)
        assertNull((store.surfaces["live1"]!!.dataModel as JsonObject)["x"])
    }

    @Test fun snapshot404AbortsCycleIntoBackoff() = runTest {
        val store = SurfaceStore()
        val http = FakeHttp(mapOf(
            "/a2ui/surfaces" to ok("""{"latest_seq":9,"surfaces":[{"surface_id":"a"},{"surface_id":"b"}]}"""),
            "/a2ui/surfaces/a" to ok(envelope("a")),
        ))
        val e = SyncEngine(store, http, unconfined)
        assertFalse(e.cycle())
        assertEquals(Connection.ERROR, store.connection); assertEquals(1, e.attempt); assertEquals(0, http.streams)
        assertTrue(e.lastError!!.contains("404"))
    }

    @Test fun streamEventsApplyAndEofRehydrates() = runTest {
        val store = SurfaceStore()
        val http = FakeHttp(
            mapOf("/a2ui/surfaces" to ok("""{"latest_seq":1,"surfaces":[]}""")),
            frames = listOf("id: 2\nevent: a2ui\ndata: ${envelope("s2")}\n\n", ": keepalive\n\n"),
        )
        val e = SyncEngine(store, http, unconfined)
        assertFalse(e.cycle())                     // EOF → backoff, never resume (R2)
        assertNotNull(store.surfaces["s2"]); assertEquals(2, store.lastSeq)
        assertEquals(Connection.ERROR, store.connection); assertEquals(1, e.reconnects)
    }

    @Test fun resyncControlKeepsInFlightActivityAndRerunsImmediately() = runTest {
        val store = SurfaceStore()
        val http = FakeHttp(
            mapOf("/a2ui/surfaces" to ok("""{"latest_seq":1,"surfaces":[{"surface_id":"a"}]}"""), "/a2ui/surfaces/a" to ok(envelope("a"))),
            frames = listOf(resyncFrame),
        )
        val e = SyncEngine(store, http, unconfined)
        store.beginActivity("a", ActivityKind.REFRESH, "refresh")
        assertTrue(e.cycle())                      // resync → rerun with no backoff
        assertNotNull(store.activity["a"])         // R5: resync preserves in-flight records
        assertTrue(store.surfaces.isEmpty())       // ...but drops surfaces until the next hydration
        assertEquals(1, e.reconnects)
    }

    @Test fun runLoopsWithoutRecursionAndStops() = runTest {
        // A stream that ends instantly on every cycle must not grow the stack
        // (the recursive scheduleNext did: StackOverflowError).
        val store = SurfaceStore()
        var cycles = 0
        val self = arrayOfNulls<SyncEngine>(1)
        val http = object : FakeHttp(mapOf("/a2ui/surfaces" to ok("""{"latest_seq":1,"surfaces":[]}""")), frames = listOf(resyncFrame)) {
            override suspend fun get(path: String): Http.Response { if (++cycles >= 500) self[0]!!.stop(); return super.get(path) }
        }
        self[0] = SyncEngine(store, http, unconfined, sleep = { })
        self[0]!!.run()
        assertTrue(cycles >= 500)
    }

    @Test fun actionBodyShapeAndNeverThrows() = runTest {
        val store = SurfaceStore(); store.apply(null, Json.parseToJsonElement(envelope("s1", "abc")).jsonObject)
        val http = FakeHttp(emptyMap())
        val e = SyncEngine(store, http, unconfined, nowMs = { 0 })
        val r = e.postAction("s1", "approval.choose", "btn", Json.parseToJsonElement("""{"optionId":"x"}""").jsonObject)
        assertTrue(r.ok); assertEquals("done", r.message); assertEquals(true, r.resolved)
        val body = Json.parseToJsonElement(http.posts[0].second).jsonObject
        val action = body["action"]!!.jsonObject
        assertEquals("abc", action["metadata"]!!.jsonObject["extensions"]!!.jsonObject["com_nous_nonce"]!!.jsonPrimitive.content)
        assertEquals("1970-01-01T00:00:00.000Z", action["timestamp"]!!.jsonPrimitive.content)   // JS toISOString shape
        assertEquals("yes", body["a2uiRendererDataModel"]!!.jsonObject["surfaces"]!!.jsonObject["s1"]!!.jsonObject["formData"]!!.jsonObject["choice"]!!.jsonPrimitive.content)
        http.postResponse = Http.Response(403, """{"error":{"code":"NONCE_MISMATCH","message":"stale"}}""", emptyMap())
        val rej = e.postAction("s1", "x", "b", JsonObject(emptyMap())); assertFalse(rej.ok); assertEquals("stale", rej.message)
    }

    @Test fun surfaceIdIsEncodedLikeEncodeURIComponent() {
        assertEquals("a%20b%2Fc'(d)~", encodeURIComponent("a b/c'(d)~"))
        assertEquals("nous%3Aagent%3Amicro_app%3A1", encodeURIComponent("nous:agent:micro_app:1"))
    }

    @Test fun callNonceAtTopLevel() = runTest {
        val store = SurfaceStore(); store.apply(null, Json.parseToJsonElement(envelope("s1", "n1")).jsonObject)
        val http = FakeHttp(emptyMap())
        http.postResponse = Http.Response(200, """{"version":"v1.0","agentFunctionResponse":{"functionCallId":"fc","value":{"seq":42}}}""", emptyMap())
        val e = SyncEngine(store, http, unconfined)
        val r = e.callAgentFunction("s1", "app.refresh", JsonObject(emptyMap()))
        assertTrue(r.ok); assertEquals(42L, ActivityRules.responseSeq(r.value))
        val body = Json.parseToJsonElement(http.posts[0].second).jsonObject
        assertEquals("n1", body["metadata"]!!.jsonObject["extensions"]!!.jsonObject["com_nous_nonce"]!!.jsonPrimitive.content)
        assertNull(body["callAgentFunction"]!!.jsonObject["metadata"])
        http.postResponse = Http.Response(200, """{"agentFunctionResponse":{"error":{"code":"X","message":"boom"}}}""", emptyMap())
        val err = e.callAgentFunction("s1", "app.refine", JsonObject(emptyMap())); assertFalse(err.ok); assertEquals("boom", err.message)
    }
}
