package us.fatykhov.nous.companion

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.async
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import us.fatykhov.nous.companion.core.DeviceToken
import us.fatykhov.nous.companion.core.Paired
import us.fatykhov.nous.companion.core.Pairing
import us.fatykhov.nous.companion.core.SurfaceStore
import us.fatykhov.nous.companion.core.SyncEngine
import us.fatykhov.nous.companion.data.DeviceCredential
import us.fatykhov.nous.companion.data.DeviceTokenInterceptor
import us.fatykhov.nous.companion.data.OkHttpTransport
import us.fatykhov.nous.companion.data.Settings
import us.fatykhov.nous.companion.push.registrationOutcome
import java.io.File
import java.net.InetAddress
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * The device token on the wire, against real sockets: sent only to the
 * paired origin and only where the policy allows; never after a redirect;
 * forgotten when the server rejects it.
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = TestApp::class, sdk = [34])
class DeviceTokenTransportTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()
    private val servers = mutableListOf<MockWebServer>()
    private val localhostCert = HeldCertificate.Builder().addSubjectAlternativeName("localhost").build()
    private val serverCerts = HandshakeCertificates.Builder().heldCertificate(localhostCert).build()
    private val clientCerts = HandshakeCertificates.Builder().addTrustedCertificate(localhostCert.certificate).build()
    private val trusting = OkHttpClient.Builder().sslSocketFactory(clientCerts.sslSocketFactory(), clientCerts.trustManager).build()
    private val rejected = """{"error":{"code":"DEVICE_TOKEN_REJECTED","message":"This device is no longer paired. Pair it again in the app's settings."}}"""

    @After fun close() { servers.forEach { it.close() } }

    private fun server(https: Boolean = false): MockWebServer = MockWebServer().also {
        if (https) it.useHttps(serverCerts.sslSocketFactory())
        it.start(InetAddress.getByName("127.0.0.1"), 0)
        servers += it
    }

    private fun MockWebServer.answer(code: Int, body: String = "{}", location: String? = null) =
        enqueue(MockResponse.Builder().code(code).body(body).apply { if (location != null) addHeader("Location", location) }.build())

    private fun MockWebServer.next(): RecordedRequest = takeRequest(5, TimeUnit.SECONDS) ?: error("no request arrived")
    private val RecordedRequest.token: String? get() = headers[DeviceToken.HEADER]

    private fun credential(pairedAt: String?): DeviceCredential =
        DeviceCredential(File(ctx.noBackupFilesDir, "device.properties"), FakeBox()).also {
            it.clear()
            if (pairedAt != null) assertTrue(it.save(Paired(SAMPLE_TOKEN, "d-1", "Pixel", null), pairedAt))
        }

    private fun transport(baseUrl: String, credential: DeviceCredential, debuggable: Boolean): OkHttpTransport {
        val settings = Settings(ctx).apply { this.baseUrl = baseUrl }
        return OkHttpTransport(settings, credential, debuggable, base = trusting)
    }

    // ------------------------------------------------------------------ the header

    @Test fun the_header_goes_on_every_kind_of_call_to_the_paired_origin() = runBlocking {
        val s = server()
        val base = "http://localhost:${s.port}"
        val http = transport(base, credential(base), debuggable = true)
        repeat(5) { s.answer(200) }
        s.answer(403)   // the stream: refused, so it returns at once
        http.get("/a2ui/surfaces")
        http.postJson("/a2ui/action", "{}")
        http.postJson("/a2ui/call", "{}")
        http.putJson("/a2ui/push/tokens", "{}")
        http.delete("/a2ui/push/tokens/i")
        http.stream("/a2ui/stream?since=0", onOpen = { error("not opened") }, onChunk = { })
        repeat(6) { assertEquals(SAMPLE_TOKEN, s.next().token) }
    }

    @Test fun the_header_goes_over_https() = runBlocking {
        val s = server(https = true)
        val base = "https://localhost:${s.port}"
        s.answer(200)
        transport(base, credential(base), debuggable = false).get("/a2ui/surfaces")
        assertEquals(SAMPLE_TOKEN, s.next().token)
    }

    @Test fun no_header_when_unpaired() = runBlocking {
        val s = server()
        s.answer(200)
        transport("http://localhost:${s.port}", credential(null), debuggable = true).get("/a2ui/surfaces")
        assertNull(s.next().token)
    }

    @Test fun no_header_when_the_origin_differs() = runBlocking {
        val s = server()
        s.answer(200)
        transport("http://localhost:${s.port}", credential("http://localhost:${s.port + 1}"), debuggable = true).get("/a2ui/surfaces")
        assertNull(s.next().token)
    }

    @Test fun no_header_over_plain_http_on_a_release_build() = runBlocking {
        val s = server()
        val base = "http://localhost:${s.port}"
        s.answer(200)
        transport(base, credential(base), debuggable = false).get("/a2ui/surfaces")
        assertNull(s.next().token)
    }

    @Test fun no_header_over_plain_http_to_a_host_that_is_not_the_emulators() = runBlocking {
        val s = server()
        val base = "http://127.0.0.1:${s.port}"
        s.answer(200)
        transport(base, credential(base), debuggable = true).get("/a2ui/surfaces")
        assertNull(s.next().token)
    }

    @Test fun changing_the_address_does_not_carry_the_token_to_the_new_one() = runBlocking {
        val old = server(); val new = server()
        val cred = credential("http://localhost:${old.port}")
        new.answer(200)
        transport("http://localhost:${new.port}", cred, debuggable = true).get("/a2ui/surfaces")
        assertNull(new.next().token)
        assertEquals(0, old.requestCount)
    }

    // ---------------------------------------------------------------- redirects

    @Test fun the_app_follows_no_redirect_to_another_host() = runBlocking {
        val a = server(); val b = server()
        val base = "http://localhost:${a.port}"
        a.answer(302, location = "http://127.0.0.1:${b.port}/a2ui/surfaces")
        val res = transport(base, credential(base), debuggable = true).get("/a2ui/surfaces")
        assertEquals(302, res.status)
        assertEquals(0, b.requestCount)
    }

    @Test fun the_app_follows_no_redirect_from_https_to_http() = runBlocking {
        val a = server(https = true); val b = server()
        val base = "https://localhost:${a.port}"
        a.answer(302, location = "http://localhost:${b.port}/a2ui/surfaces")
        val res = transport(base, credential(base), debuggable = true).get("/a2ui/surfaces")
        assertEquals(302, res.status)
        assertEquals(0, b.requestCount)
    }

    /** The second lock on its own: a client that does follow redirects, with the same interceptor. */
    private fun following(cred: DeviceCredential) =
        trusting.newBuilder().addNetworkInterceptor(DeviceTokenInterceptor(cred, debuggable = true)).build()

    @Test fun the_interceptor_keeps_the_header_off_a_redirect_to_another_host() {
        val a = server(); val b = server()
        val base = "http://localhost:${a.port}"
        a.answer(302, location = "http://127.0.0.1:${b.port}/next")
        b.answer(200)
        following(credential(base)).newCall(Request.Builder().url("$base/a2ui/surfaces").build()).execute().close()
        assertEquals(SAMPLE_TOKEN, a.next().token)
        assertNull(b.next().token)
    }

    @Test fun the_interceptor_keeps_the_header_off_a_redirect_from_https_to_http() {
        val a = server(https = true); val b = server()
        val base = "https://localhost:${a.port}"
        a.answer(302, location = "http://localhost:${b.port}/next")
        b.answer(200)
        following(credential(base)).newCall(Request.Builder().url("$base/a2ui/surfaces").build()).execute().close()
        assertEquals(SAMPLE_TOKEN, a.next().token)
        assertNull(b.next().token)
    }

    @Test fun the_interceptor_keeps_the_header_off_a_redirect_within_the_paired_origin() {
        val a = server()
        val base = "http://localhost:${a.port}"
        a.answer(302, location = "$base/elsewhere")
        a.answer(200)
        following(credential(base)).newCall(Request.Builder().url("$base/a2ui/surfaces").build()).execute().close()
        assertEquals(SAMPLE_TOKEN, a.next().token)
        assertNull(a.next().token)
    }

    // ------------------------------------------------------------ the answers

    @Test fun a_token_rejected_answer_forgets_the_token() = runBlocking {
        val s = server()
        val base = "http://localhost:${s.port}"
        val cred = credential(base)
        val http = transport(base, cred, debuggable = true)
        s.answer(403, rejected)
        s.answer(200)
        assertEquals(403, http.postJson("/a2ui/action", "{}").status)
        assertNull(cred.pairedFor(base))
        assertFalse(File(ctx.noBackupFilesDir, "device.properties").exists())
        http.get("/a2ui/surfaces")
        assertEquals(SAMPLE_TOKEN, s.next().token)
        assertNull(s.next().token)
    }

    @Test fun a_rejection_of_an_older_token_arriving_after_a_newer_one_was_saved_keeps_the_newer_one() = runBlocking {
        val arrived = CountDownLatch(1); val release = CountDownLatch(1)
        val s = MockWebServer().apply {
            dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    arrived.countDown(); release.await(10, TimeUnit.SECONDS)
                    return MockResponse.Builder().code(403).body(rejected).build()
                }
            }
            start(InetAddress.getByName("127.0.0.1"), 0)
        }
        servers += s
        val base = "http://localhost:${s.port}"
        val cred = credential(base)
        val http = transport(base, cred, debuggable = true)
        val call = async(Dispatchers.IO) { http.postJson("/a2ui/action", "{}") }   // carries the old token
        assertTrue("the old token's request is in flight", arrived.await(5, TimeUnit.SECONDS))
        val newer = "nd1_" + "N".repeat(43)
        assertTrue(cred.save(Paired(newer, "d-2", "Pixel", null), base))   // paired again meanwhile
        release.countDown()
        assertEquals(403, call.await().status)
        assertEquals("the rejection was for the old token", newer, cred.tokenFor("http", "localhost", s.port))
    }

    @Test fun a_device_token_header_already_on_a_request_is_removed_unless_the_paired_token_replaces_it() {
        val planted = "nd1_" + "P".repeat(43)
        fun sendWithPlanted(cred: DeviceCredential, s: MockWebServer): String? {
            s.answer(200)
            trusting.newBuilder().addNetworkInterceptor(DeviceTokenInterceptor(cred, debuggable = true)).followRedirects(false).build()
                .newCall(Request.Builder().url("http://localhost:${s.port}/a2ui/surfaces").header(DeviceToken.HEADER, planted).build()).execute().close()
            return s.next().token
        }
        val s = server()
        assertNull("paired elsewhere", sendWithPlanted(credential("http://localhost:${s.port + 1}"), s))
        assertNull("not paired", sendWithPlanted(credential(null), s))
        assertEquals("the paired origin gets the real token", SAMPLE_TOKEN, sendWithPlanted(credential("http://localhost:${s.port}"), s))
    }

    @Test fun owner_proof_required_keeps_the_token() = runBlocking {
        val s = server()
        val base = "http://localhost:${s.port}"
        val cred = credential(base)
        s.answer(403, """{"error":{"code":"OWNER_PROOF_REQUIRED","message":"m"}}""")
        transport(base, cred, debuggable = true).postJson("/a2ui/action", "{}")
        assertNotNull(cred.pairedFor(base))
    }

    @Test fun unpair_deletes_the_push_row_then_revokes_then_forgets_over_the_wire() = runBlocking {
        val s = server()
        val base = "http://localhost:${s.port}"
        val cred = credential(base)
        s.answer(200)
        s.answer(200, """{"revoked":true}""")
        val report = Pairing.unpair(transport(base, cred, debuggable = true), "inst-1", paired = true, forget = { cred.clear() })
        val first = s.next(); val second = s.next()
        assertEquals("DELETE", first.method); assertEquals("/a2ui/push/tokens/inst-1", first.url.encodedPath); assertEquals(SAMPLE_TOKEN, first.token)
        assertEquals("POST", second.method); assertEquals("/a2ui/devices/unpair", second.url.encodedPath); assertEquals(SAMPLE_TOKEN, second.token)
        assertTrue(report.pushDeleted && report.revoked)
        assertNull(cred.pairedFor(base))
    }

    // ------------------------------------------------------------- no leak

    @Test fun no_string_the_app_shows_or_keeps_holds_the_token() = runBlocking {
        val s = server()
        val base = "http://localhost:${s.port}"
        val cred = credential(base)
        val http = transport(base, cred, debuggable = true)
        val engine = SyncEngine(SurfaceStore(), http, CoroutineScope(Dispatchers.Unconfined), tokenSent = { cred.pairedFor(base) != null })
        s.answer(403, """{"error":{"code":"OWNER_PROOF_REQUIRED","message":"m"}}""")
        val tap = engine.postAction("s1", "approval.choose", "b", JsonObject(emptyMap()))
        s.answer(403, rejected)
        engine.connect()   // the read is rejected: the loop stops with its sentence
        val deadline = System.currentTimeMillis() + 5_000
        while (engine.refused == null && System.currentTimeMillis() < deadline) Thread.sleep(20)
        engine.stop()
        assertNotNull(engine.refused)
        val dead = transport("http://localhost:1", cred, debuggable = true)
        val failure = runCatching { dead.get("/a2ui/surfaces") }.exceptionOrNull()
        val shown = listOf(tap.message, tap.toString(), engine.lastError, registrationOutcome(403, rejected).second,
            failure?.message, failure?.toString(), cred.pairedFor(base)?.toString(),
            Paired(SAMPLE_TOKEN, "d", "n", null).toString())
        for (line in shown) assertFalse(line, (line ?: "").contains(SAMPLE_TOKEN) || (line ?: "").contains("nd1_"))
    }

    @Test fun the_app_writes_no_log_lines() {
        // The app has no logger: no line it could write can hold the token. A new
        // logging call fails here, so whoever adds one decides what it may print.
        val calls = Regex("""\b(android\.util\.Log|Log\.(v|d|i|w|e|wtf)\(|println\(|printStackTrace\(|Timber\.)""")
        // `:core` ships inside the app, so its sources count too.
        val found = listOf(File("src/main/kotlin"), File("../core/src/main/kotlin")).onEach { assertTrue(it.path, it.isDirectory) }
            .flatMap { dir -> dir.walkTopDown().filter { it.isFile && it.extension == "kt" }.toList() }
            .flatMap { f -> f.readLines().mapIndexedNotNull { i, l -> if (calls.containsMatchIn(l)) "${f.name}:${i + 1}: ${l.trim()}" else null } }
            .toList()
        assertTrue(found.joinToString("\n"), found.isEmpty())
    }
}
