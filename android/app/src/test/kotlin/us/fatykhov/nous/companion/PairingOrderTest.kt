package us.fatykhov.nous.companion

import android.content.Context
import android.content.pm.ApplicationInfo
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.hasSetTextAction
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.test.core.app.ApplicationProvider
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import us.fatykhov.nous.companion.core.DeviceToken
import us.fatykhov.nous.companion.core.Paired
import us.fatykhov.nous.companion.core.PairingWords
import us.fatykhov.nous.companion.data.AppGraph
import us.fatykhov.nous.companion.ui.NousThemed
import us.fatykhov.nous.companion.ui.SettingsScreen
import us.fatykhov.nous.companion.ui.Themes
import java.net.InetAddress
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Pair, Unpair and Disconnect run one at a time in the order asked, and work
 * that finishes in the background does not start the sync engine. The
 * server's answer is held on a latch so the next request lands mid-way.
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = TestApp::class, sdk = [34])
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class PairingOrderTest {
    @get:Rule val rule = createComposeRule()
    private val ctx: Context = ApplicationProvider.getApplicationContext()
    private val seen = CopyOnWriteArrayList<RecordedRequest>()
    private val arrived = CountDownLatch(1)
    private val release = CountDownLatch(1)
    /** The request whose answer waits for [release]. */
    private var heldPath = ""
    private val server = MockWebServer().apply {
        dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                seen += request
                val path = request.url.encodedPath
                if (path == heldPath || (heldPath == "DELETE" && request.method == "DELETE")) { arrived.countDown(); release.await(10, TimeUnit.SECONDS) }
                return when {
                    path == "/a2ui/devices/redeem" -> json(200, """{"device_id":"d-1","name":"Pixel","token":"$SAMPLE_TOKEN"}""")
                    path == "/a2ui/devices/unpair" -> json(200, """{"revoked":true}""")
                    path == "/a2ui/surfaces" -> json(200, """{"latest_seq":0,"surfaces":[]}""")
                    else -> json(200, "{}")
                }
            }
        }
        start(InetAddress.getByName("127.0.0.1"), 0)
    }
    private val base = "http://localhost:${server.port}"
    private var graph: AppGraph? = null
    private val owner = object : LifecycleOwner { override val lifecycle: Lifecycle get() = error("not used") }

    @After fun close() { graph?.engine?.stop(); server.close() }

    private fun json(code: Int, body: String) = MockResponse.Builder().code(code).body(body).build()

    private fun graph(paired: Boolean = false): AppGraph {
        ctx.applicationInfo.flags = ctx.applicationInfo.flags or ApplicationInfo.FLAG_DEBUGGABLE
        return AppGraph(ctx, FakeBox()).also {
            it.credential.clear(); it.settings.baseUrl = base; graph = it
            it.lifecycle.inForeground = true
            if (paired) assertTrue(it.credential.save(Paired(SAMPLE_TOKEN, "d-1", "Pixel", null), base))
        }
    }

    /** Runs the main looper, where the app's own scope resumes, until [done]; at most 5 s. */
    private fun idleMainUntil(what: String, done: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 5_000
        while (!done() && System.currentTimeMillis() < deadline) { shadowOf(android.os.Looper.getMainLooper()).idle(); Thread.sleep(10) }
        assertTrue(what, done())
    }

    /** Lets the main looper run for a while, for checks that something does NOT happen. */
    private fun idleMainFor(ms: Long) {
        val end = System.currentTimeMillis() + ms
        while (System.currentTimeMillis() < end) { shadowOf(android.os.Looper.getMainLooper()).idle(); Thread.sleep(10) }
    }

    private fun calls(method: String, path: String) = seen.filter { it.method == method && it.url.encodedPath == path }
    private fun settings(g: AppGraph, onDisconnect: () -> Unit = {}) = rule.setContent { NousThemed(Themes.nousDefault) { SettingsScreen(g, onDisconnect) { } } }

    private fun startPairing() {
        rule.onNode(hasSetTextAction()).performTextInput("K7QM2XRD")
        rule.onNodeWithText("Pair this device").performClick()
        idleMainUntil("the redeem is in flight") { arrived.count == 0L }
    }

    // ------------------------------------------------------------- one at a time

    @Test fun disconnect_asked_mid_redeem_waits_and_then_removes_what_the_pairing_produced() {
        heldPath = "/a2ui/devices/redeem"
        val g = graph()
        var disconnected = 0
        settings(g) { disconnected++ }
        startPairing()
        rule.onNodeWithText("Disconnect this phone").performScrollTo().assertIsNotEnabled()   // the screen offers no race
        rule.runOnIdle { g.pairing.disconnect { disconnected++ } }                           // a request that arrives anyway waits
        release.countDown()
        idleMainUntil("both finished") { !g.pairing.state.busy }

        assertEquals(1, disconnected)
        assertNull(g.credential.pairedFor(base))
        assertFalse(g.settings.configured)
        val unpair = calls("POST", "/a2ui/devices/unpair")
        assertEquals("the device the pairing produced is revoked", 1, unpair.size)
        assertEquals(SAMPLE_TOKEN, unpair.single().headers[DeviceToken.HEADER])
        val after = seen.size
        idleMainFor(300)
        assertEquals("the engine stays stopped", after, seen.size)
    }

    @Test fun unpair_asked_mid_redeem_runs_after_it_and_revokes_the_new_device() {
        heldPath = "/a2ui/devices/redeem"
        val g = graph()
        settings(g)
        startPairing()
        rule.runOnIdle { g.pairing.unpair() }   // Settings shows no Unpair until paired; a request that arrives anyway waits
        release.countDown()
        idleMainUntil("both finished") { !g.pairing.state.busy }

        assertNull(g.credential.pairedFor(base))
        assertTrue(g.settings.configured)
        val order = seen.map { "${it.method} ${it.url.encodedPath}" }.filter { !it.startsWith("GET") }
        assertEquals(listOf("POST /a2ui/devices/redeem", "DELETE /a2ui/push/tokens/${g.settings.installationId}", "POST /a2ui/devices/unpair"), order)
        assertEquals(SAMPLE_TOKEN, calls("POST", "/a2ui/devices/unpair").single().headers[DeviceToken.HEADER])
        assertEquals(PairingWords.UNPAIRED, g.pairing.state.note)
    }

    @Test fun pair_asked_mid_disconnect_does_nothing_after_it() {
        heldPath = "DELETE"   // the disconnect's first call to the server
        val g = graph(paired = true)
        settings(g)
        rule.onNodeWithText("Disconnect this phone").performScrollTo().performClick()
        idleMainUntil("the disconnect is in flight") { arrived.count == 0L }
        rule.onNodeWithText("Unpair").performScrollTo().assertIsNotEnabled()
        rule.runOnIdle { g.pairing.pair("K7QM2XRD") }   // a request that arrives anyway waits, then finds nothing configured
        release.countDown()
        idleMainUntil("both finished") { !g.pairing.state.busy }

        assertTrue(calls("POST", "/a2ui/devices/redeem").isEmpty())
        assertNull(g.credential.pairedFor(base))
        assertFalse(g.settings.configured)
    }

    // -------------------------------------------------- answered in the background

    @Test fun a_pairing_answered_in_the_background_keeps_the_token_and_the_next_foreground_connects() {
        heldPath = "/a2ui/devices/redeem"
        val g = graph()
        settings(g)
        startPairing()
        rule.runOnIdle { g.lifecycle.onStop(owner) }   // the app goes to the background
        release.countDown()
        idleMainUntil("the pairing finished") { !g.pairing.state.busy }
        assertNotNull("the token is kept", g.credential.pairedFor(base))
        idleMainFor(300)
        assertTrue("the engine stays stopped", calls("GET", "/a2ui/surfaces").isEmpty())

        rule.runOnIdle { g.lifecycle.onStart(owner) }   // back in the foreground
        idleMainUntil("the next foreground connects") { calls("GET", "/a2ui/surfaces").isNotEmpty() }
        assertEquals(SAMPLE_TOKEN, calls("GET", "/a2ui/surfaces").first().headers[DeviceToken.HEADER])
    }

    @Test fun an_unpair_answered_in_the_background_forgets_the_token_and_the_next_foreground_connects() {
        heldPath = "/a2ui/devices/unpair"
        val g = graph(paired = true)
        settings(g)
        rule.onNodeWithText("Unpair").performClick()
        idleMainUntil("the unpair is in flight") { arrived.count == 0L }
        rule.runOnIdle { g.lifecycle.onStop(owner) }
        release.countDown()
        idleMainUntil("the unpair finished") { !g.pairing.state.busy }
        assertNull("the token is forgotten", g.credential.pairedFor(base))
        idleMainFor(300)
        assertTrue("the engine stays stopped", calls("GET", "/a2ui/surfaces").isEmpty())

        rule.runOnIdle { g.lifecycle.onStart(owner) }
        idleMainUntil("the next foreground connects") { calls("GET", "/a2ui/surfaces").isNotEmpty() }
        assertNull(calls("GET", "/a2ui/surfaces").first().headers[DeviceToken.HEADER])
    }
}
