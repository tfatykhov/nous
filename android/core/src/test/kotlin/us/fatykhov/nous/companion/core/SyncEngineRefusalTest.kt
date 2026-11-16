package us.fatykhov.nous.companion.core

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** How the sync loop answers the server's two device-proof refusals. */
class SyncEngineRefusalTest {
    private val unconfined = CoroutineScope(Dispatchers.Unconfined)
    private fun res(status: Int, body: String) = Http.Response(status, body, emptyMap())
    private val notPaired = res(403, """{"error":{"code":"OWNER_PROOF_REQUIRED","message":"This action needs the owner's sign-in. Use the web companion through the login page."}}""")
    private val rejected = res(403, """{"error":{"code":"DEVICE_TOKEN_REJECTED","message":"This device is no longer paired. Pair it again in the app's settings."}}""")
    private val index = res(200, """{"latest_seq":1,"surfaces":[{"surface_id":"s1"}]}""")
    private val snapshot = res(200, """{"version":"v1.0","createSurface":{"surfaceId":"s1","catalogId":"c","metadata":{"extensions":{"com_nous_nonce":"n1"}},"components":[{"id":"root","component":"Text","text":"s1"}]}}""")

    @Test fun a_refused_hydration_read_stops_the_loop_instead_of_backing_off() = runTest {
        val store = SurfaceStore()
        val http = ScriptedHttp { _, _ -> notPaired }
        val sleeps = mutableListOf<Long>()
        val e = SyncEngine(store, http, unconfined, sleep = { sleeps.add(it) })

        e.run()   // returns: the loop stopped

        assertEquals(listOf("GET /a2ui/surfaces"), http.log)
        assertTrue(sleeps.isEmpty(), "no backoff: a retry gets the same answer")
        assertEquals(Refusal.NOT_PAIRED, e.refused)
        assertEquals(Connection.ERROR, store.connection)
        assertEquals("not paired (HTTP 403)", e.lastError)
    }

    @Test fun a_refused_snapshot_read_stops_the_loop_too() = runTest {
        val http = ScriptedHttp { _, path -> if (path == "/a2ui/surfaces") index else notPaired }
        val e = SyncEngine(SurfaceStore(), http, unconfined, sleep = { error("no backoff") })
        e.run()
        assertEquals(listOf("GET /a2ui/surfaces", "GET /a2ui/surfaces/s1"), http.log)
        assertEquals(Refusal.NOT_PAIRED, e.refused)
    }

    @Test fun a_token_rejected_on_a_read_stops_the_loop_and_says_so() = runTest {
        val e = SyncEngine(SurfaceStore(), ScriptedHttp { _, _ -> rejected }, unconfined, sleep = { error("no backoff") })
        e.run()
        assertEquals(Refusal.TOKEN_REJECTED, e.refused)
        assertEquals("device token rejected (HTTP 403)", e.lastError)
    }

    @Test fun a_token_the_server_could_not_check_backs_off_and_keeps_going() = runTest {
        var reads = 0
        val self = arrayOfNulls<SyncEngine>(1)
        val http = ScriptedHttp { _, _ -> if (++reads >= 3) self[0]!!.stop(); notPaired }
        val sleeps = mutableListOf<Long>()
        self[0] = SyncEngine(SurfaceStore(), http, unconfined, sleep = { sleeps.add(it) }, tokenSent = { true })
        self[0]!!.run()
        assertEquals(3, reads)
        assertEquals(2, sleeps.size)
        assertNull(self[0]!!.refused)
        assertEquals(PairingWords.NOT_CHECKED, self[0]!!.lastError)
    }

    @Test fun any_other_refused_read_backs_off_as_before() = runTest {
        val e = SyncEngine(SurfaceStore(), ScriptedHttp { _, _ -> res(403, """{"error":"forbidden"}""") }, unconfined)
        assertFalse(e.cycle())
        assertNull(e.refused)
        assertTrue(e.lastError!!.contains("403"))
        assertEquals(1, e.attempt)
    }

    @Test fun a_refused_tap_does_not_stop_the_sync_loop() = runTest {
        val store = SurfaceStore()
        var streams = 0
        val taps = mutableListOf<ActionResult>()
        val self = arrayOfNulls<SyncEngine>(1)
        val http = object : Http by ScriptedHttp({ method, path ->
            when {
                method == "POST" -> notPaired
                path == "/a2ui/surfaces" -> index
                else -> snapshot
            }
        }) {
            override suspend fun stream(path: String, onOpen: () -> Unit, onChunk: (String) -> Unit) {
                onOpen()
                // A tap while the stream is open, refused for the proof.
                taps.add(self[0]!!.postAction("s1", "approval.choose", "b", JsonObject(emptyMap())))
                if (++streams >= 3) self[0]!!.stop()
            }
        }
        self[0] = SyncEngine(store, http, unconfined, sleep = { })
        self[0]!!.run()

        assertEquals(3, streams, "the loop went on after each refused tap")
        assertNull(self[0]!!.refused)
        assertTrue(taps.all { !it.ok && it.refusal == Refusal.NOT_PAIRED && it.message == PairingWords.TAP_NOT_PAIRED })
    }

    @Test fun postAction_shows_the_apps_own_sentence_for_each_code() = runTest {
        val store = SurfaceStore()
        var answer = notPaired
        var sent = false
        val e = SyncEngine(store, ScriptedHttp { _, _ -> answer }, unconfined, tokenSent = { sent })

        val unpaired = e.postAction("s1", "approval.choose", "b", JsonObject(emptyMap()))
        assertEquals(ActionResult(false, PairingWords.TAP_NOT_PAIRED, refusal = Refusal.NOT_PAIRED), unpaired)

        sent = true
        val unchecked = e.postAction("s1", "approval.choose", "b", JsonObject(emptyMap()))
        assertEquals(ActionResult(false, PairingWords.NOT_CHECKED, refusal = null), unchecked)

        answer = rejected; sent = false   // the transport forgot the token on this answer
        val gone = e.postAction("s1", "approval.choose", "b", JsonObject(emptyMap()))
        assertEquals(ActionResult(false, PairingWords.TOKEN_REJECTED, refusal = Refusal.TOKEN_REJECTED), gone)

        for (r in listOf(unpaired, unchecked, gone)) assertFalse(r.message.contains("sign-in") || r.message.contains("app's settings"), r.message)
        assertNull(e.refused)
    }

    @Test fun callAgentFunction_reads_the_code_from_the_call_envelope() = runTest {
        val e = SyncEngine(SurfaceStore(), ScriptedHttp { _, _ ->
            res(403, """{"version":"v1.0","agentFunctionResponse":{"functionCallId":"f","error":{"code":"OWNER_PROOF_REQUIRED","message":"server words"}}}""")
        }, unconfined)
        val r = e.callAgentFunction("s1", "app.refresh", JsonObject(emptyMap()))
        assertEquals(CallResult(false, null, PairingWords.TAP_NOT_PAIRED, refusal = Refusal.NOT_PAIRED), r)
        assertNull(e.refused)
    }

    @Test fun connect_after_pairing_runs_again() = runTest {
        val store = SurfaceStore()
        var paired = false
        val self = arrayOfNulls<SyncEngine>(1)
        val http = object : Http by ScriptedHttp({ _, path -> if (!paired) notPaired else if (path == "/a2ui/surfaces") res(200, """{"latest_seq":1,"surfaces":[]}""") else snapshot }) {
            override suspend fun stream(path: String, onOpen: () -> Unit, onChunk: (String) -> Unit) { onOpen(); self[0]!!.stop() }
        }
        self[0] = SyncEngine(store, http, unconfined, sleep = { })
        self[0]!!.run()
        assertEquals(Refusal.NOT_PAIRED, self[0]!!.refused)

        paired = true
        self[0]!!.connect()   // what the app does after a pairing

        assertNull(self[0]!!.refused)
        assertEquals(1, self[0]!!.reconnects, "the new run hydrated and opened the stream")
    }
}
