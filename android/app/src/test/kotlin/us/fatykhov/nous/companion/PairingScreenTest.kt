package us.fatykhov.nous.companion

import android.app.Application
import android.content.Context
import android.content.pm.ApplicationInfo
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.hasSetTextAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.test.core.app.ApplicationProvider
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.GraphicsMode
import us.fatykhov.nous.companion.core.Connection
import us.fatykhov.nous.companion.core.DeviceToken
import us.fatykhov.nous.companion.core.Paired
import us.fatykhov.nous.companion.core.PairingWords
import us.fatykhov.nous.companion.data.AppGraph
import us.fatykhov.nous.companion.push.PushManager
import us.fatykhov.nous.companion.ui.CompanionApp
import us.fatykhov.nous.companion.ui.LocalOpenSettings
import us.fatykhov.nous.companion.ui.LocalSurfaceHost
import us.fatykhov.nous.companion.ui.NousThemed
import us.fatykhov.nous.companion.ui.Render
import us.fatykhov.nous.companion.ui.Route
import us.fatykhov.nous.companion.ui.SettingsScreen
import us.fatykhov.nous.companion.ui.SurfaceHost
import us.fatykhov.nous.companion.ui.Themes
import java.net.InetAddress
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.CopyOnWriteArrayList

/** The pairing screens against a scripted server: pair, unpair, the inbox when not paired, a refused tap. */
@RunWith(RobolectricTestRunner::class)
@Config(application = TestApp::class, sdk = [34])
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class PairingScreenTest {
    @get:Rule val rule = createComposeRule()
    private val ctx: Context = ApplicationProvider.getApplicationContext()
    private val seen = CopyOnWriteArrayList<RecordedRequest>()
    private var answers: (RecordedRequest) -> MockResponse = { MockResponse.Builder().code(404).build() }
    private val server = MockWebServer().apply {
        dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse { seen += request; return answers(request) }
        }
        start(InetAddress.getByName("127.0.0.1"), 0)
    }
    private val base = "http://localhost:${server.port}"
    private var graph: AppGraph? = null

    @After fun close() { graph?.engine?.stop(); server.close() }

    private fun json(code: Int, body: String) = MockResponse.Builder().code(code).body(body).build()
    private val notPaired = """{"error":{"code":"OWNER_PROOF_REQUIRED","message":"m"}}"""

    /** A debuggable app at a local Nous, so plain http to `localhost` may carry the token. */
    private fun graph(box: FakeBox = FakeBox()): AppGraph {
        ctx.applicationInfo.flags = ctx.applicationInfo.flags or ApplicationInfo.FLAG_DEBUGGABLE
        return AppGraph(ctx, box).also { it.credential.clear(); it.settings.baseUrl = base; graph = it }
    }

    private fun waitForText(text: String) = rule.waitUntil(5_000) { rule.onAllNodes(hasText(text)).fetchSemanticsNodes().isNotEmpty() }
    private fun path(r: RecordedRequest) = r.url.encodedPath

    @Test fun settings_pairs_with_a_typed_code_and_unpairs() {
        answers = { r ->
            when (path(r)) {
                "/a2ui/devices/redeem" -> json(200, """{"device_id":"d-1","name":"Pixel","token":"$SAMPLE_TOKEN","public_base_url":"https://web.nous.example"}""")
                "/a2ui/devices/unpair" -> json(200, """{"revoked":true}""")
                "/a2ui/surfaces" -> json(200, """{"latest_seq":0,"surfaces":[]}""")
                else -> json(200, "{}")
            }
        }
        val box = FakeBox()
        val g = graph(box)
        rule.setContent { NousThemed(Themes.nousDefault) { SettingsScreen(g) { } } }
        rule.onNodeWithText("Not paired").assertExists()
        rule.onNodeWithText("Open web companion").assertExists()

        rule.onNode(hasSetTextAction()).performTextInput("k7qm-2xrd")
        rule.onNodeWithText("Pair this device").performClick()
        waitForText("Paired as")

        rule.onNodeWithText("Pixel").assertExists()
        rule.onNodeWithText(PairingWords.paired("Pixel")).assertExists()
        rule.onNodeWithText("Open web companion").assertExists()
        assertNotNull(g.credential.pairedFor(base))
        assertEquals("a working key is not replaced", 0, box.resets)
        val redeem = seen.first { path(it) == "/a2ui/devices/redeem" }
        val sent = Json.parseToJsonElement(redeem.body!!.utf8()).jsonObject
        assertEquals("\"K7QM2XRD\"", sent["code"].toString())
        assertNull(redeem.headers[DeviceToken.HEADER])

        seen.clear()
        rule.onNodeWithText("Unpair").performClick()
        waitForText(PairingWords.UNPAIRED)
        rule.onNodeWithText("Not paired").assertExists()
        assertNull(g.credential.pairedFor(base))
        val order = seen.filter { it.method != "GET" }.map { "${it.method} ${path(it)} ${it.headers[DeviceToken.HEADER] == SAMPLE_TOKEN}" }
        assertEquals(listOf("DELETE /a2ui/push/tokens/${g.settings.installationId} true", "POST /a2ui/devices/unpair true"), order.take(2))
    }

    // ------------------------------------------- leaving Settings mid-way

    /** Runs the main looper, where the app's own scope resumes, until [done]; at most 5 s. */
    private fun idleMainUntil(what: String, done: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 5_000
        while (!done() && System.currentTimeMillis() < deadline) { shadowOf(android.os.Looper.getMainLooper()).idle(); Thread.sleep(10) }
        assertTrue(what, done())
    }

    /** Settings that the test can take away (Back, a rotation) and bring back. */
    private fun settingsThatCanLeave(g: AppGraph): androidx.compose.runtime.MutableState<Boolean> {
        val shown = mutableStateOf(true)
        rule.setContent { NousThemed(Themes.nousDefault) { if (shown.value) SettingsScreen(g) { } } }
        return shown
    }

    @Test fun pairing_finishes_when_settings_is_left_while_the_server_answers() {
        val arrived = CountDownLatch(1); val release = CountDownLatch(1)
        answers = { r ->
            when (path(r)) {
                "/a2ui/devices/redeem" -> { arrived.countDown(); release.await(10, TimeUnit.SECONDS); json(200, """{"device_id":"d-1","name":"Pixel","token":"$SAMPLE_TOKEN"}""") }
                "/a2ui/surfaces" -> json(200, """{"latest_seq":0,"surfaces":[]}""")
                else -> json(200, "{}")
            }
        }
        val g = graph()
        val shown = settingsThatCanLeave(g)
        rule.onNode(hasSetTextAction()).performTextInput("K7QM2XRD")
        rule.onNodeWithText("Pair this device").performClick()
        idleMainUntil("the redeem is in flight") { arrived.count == 0L }
        rule.runOnIdle { shown.value = false }   // Settings goes away; its scope is cancelled
        rule.waitForIdle()
        release.countDown()
        idleMainUntil("the token is kept") { g.credential.pairedFor(base) != null }
        rule.runOnIdle { shown.value = true }    // and comes back
        waitForText(PairingWords.paired("Pixel"))
        rule.onNodeWithText("Paired as").assertExists()
    }

    @Test fun unpair_finishes_when_settings_is_left_while_the_server_answers() {
        val arrived = CountDownLatch(1); val release = CountDownLatch(1)
        answers = { r ->
            when (path(r)) {
                "/a2ui/devices/unpair" -> { arrived.countDown(); release.await(10, TimeUnit.SECONDS); json(200, """{"revoked":true}""") }
                "/a2ui/surfaces" -> json(200, """{"latest_seq":0,"surfaces":[]}""")
                else -> json(200, "{}")
            }
        }
        val g = graph()
        assertTrue(g.credential.save(Paired(SAMPLE_TOKEN, "d-1", "Pixel", null), base))
        val shown = settingsThatCanLeave(g)
        rule.onNodeWithText("Unpair").performClick()
        idleMainUntil("the unpair is in flight") { arrived.count == 0L }
        rule.runOnIdle { shown.value = false }
        rule.waitForIdle()
        release.countDown()
        idleMainUntil("the token is forgotten") { g.credential.pairedFor(base) == null && !g.pairing.state.busy }
        rule.runOnIdle { shown.value = true }
        waitForText(PairingWords.UNPAIRED)
        rule.onNodeWithText("Not paired").assertExists()
        assertEquals(listOf("DELETE", "POST"), seen.filter { it.method != "GET" }.map { it.method }.take(2))
    }

    @Test fun each_refused_code_shows_its_sentence() {
        answers = { r -> if (path(r) == "/a2ui/devices/redeem") json(404, """{"error":"that code is not valid"}""") else json(404, "Not Found") }
        val g = graph()
        rule.setContent { NousThemed(Themes.nousDefault) { SettingsScreen(g) { } } }
        rule.onNode(hasSetTextAction()).performTextInput("K7QM2XRD")
        rule.onNodeWithText("Pair this device").performClick()
        waitForText("That code is not valid or has run out. Show a new one in the dashboard.")
        assertNull(g.credential.pairedFor(base))
    }

    private val redeemed = """{"device_id":"d-1","name":"Pixel","token":"$SAMPLE_TOKEN"}"""

    private fun pairIn(g: AppGraph) {
        rule.setContent { NousThemed(Themes.nousDefault) { SettingsScreen(g) { } } }
        rule.onNode(hasSetTextAction()).performTextInput("K7QM2XRD")
        rule.onNodeWithText("Pair this device").performClick()
    }

    @Test fun a_key_that_stopped_working_is_made_again_once_and_pairing_goes_on() {
        answers = { r -> if (path(r) == "/a2ui/devices/redeem") json(200, redeemed) else json(200, "{}") }
        val box = FakeBox(failSeal = true, resetFixes = true)
        val g = graph(box)
        pairIn(g)
        waitForText(PairingWords.paired("Pixel"))
        assertEquals(1, box.resets)
        assertNotNull(g.credential.pairedFor(base))
    }

    @Test fun a_key_that_cannot_be_made_refuses_to_pair_and_sends_nothing() {
        answers = { r -> if (path(r) == "/a2ui/devices/redeem") json(200, redeemed) else json(200, "{}") }
        val box = FakeBox(failSeal = true)
        val g = graph(box)
        pairIn(g)
        waitForText(PairingWords.NO_KEY)
        assertEquals(1, box.resets)
        assertTrue(seen.none { path(it) == "/a2ui/devices/redeem" })
        rule.onNodeWithText("Not paired").assertExists()
    }

    @Test fun a_token_that_cannot_be_stored_after_the_redeem_says_to_revoke_it() {
        answers = { r -> if (path(r) == "/a2ui/devices/redeem") json(200, redeemed) else json(200, "{}") }
        val g = graph(FakeBox(failSealFrom = 2))   // the trial seal works, the real one does not
        pairIn(g)
        waitForText(PairingWords.NOT_STORED)
        rule.onNodeWithText("Not paired").assertExists()
    }

    @Test fun an_inbox_refused_for_the_proof_says_not_paired_and_offers_settings() {
        answers = { json(403, notPaired) }
        val g = graph()
        var route: Route = Route.Inbox
        rule.setContent { CompanionApp(g, route) { route = it } }
        g.store.connection = Connection.CONNECTING
        rule.runOnIdle { g.engine.connect() }
        waitForText(PairingWords.INBOX_NOT_PAIRED_TITLE)
        rule.onNodeWithText("Not paired").assertExists()   // the pill
        val reads = seen.size
        rule.onNodeWithText("Open Settings").performClick()
        assertEquals(Route.Settings, route)
        rule.waitForIdle()
        assertEquals("the loop stopped instead of retrying", reads, seen.size)
    }

    @Test fun a_card_opened_from_a_notification_while_not_paired_says_so_and_keeps_the_notification() {
        answers = { json(403, notPaired) }
        val g = graph()
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as android.app.NotificationManager
        nm.notify("s1", PushManager.NOTIF_ID, android.app.Notification.Builder(ctx, PushManager.CHANNEL_UPDATES).setContentTitle("t").build())
        rule.setContent { CompanionApp(g, Route.Surface("s1")) { } }
        rule.runOnIdle { g.engine.connect() }
        waitForText(PairingWords.INBOX_NOT_PAIRED_TITLE)
        assertTrue(rule.onAllNodes(hasText("This card is no longer live")).fetchSemanticsNodes().isEmpty())
        rule.onNodeWithText("Open Settings").assertExists()
        assertEquals(1, org.robolectric.Shadows.shadowOf(nm).size())
    }

    // ------------------------------------------------------------- web links

    /** Taps "Open web companion" in Settings, then "Open in web" on a card the app cannot render; the URLs they opened. */
    private fun webLinksOpen(g: AppGraph): List<String> {
        val card = """{"version":"v1.0","createSurface":{"surfaceId":"s1","catalogId":"c","metadata":{"extensions":{"com_nous_nonce":"n1"}},"components":[{"id":"root","component":"NoSuchComponent"}]}}"""
        g.store.apply(null, Json.parseToJsonElement(card).jsonObject)
        val onCard = mutableStateOf(false)
        rule.setContent {
            NousThemed(Themes.nousDefault) {
                if (!onCard.value) SettingsScreen(g) { }
                else CompositionLocalProvider(LocalSurfaceHost provides SurfaceHost(g, "s1")) { Render("root", null, 0, emptyList()) }
            }
        }
        val app = shadowOf(ctx as Application)
        rule.onNodeWithText("Open web companion").performScrollTo().performClick()
        rule.waitForIdle()
        val fromSettings = app.nextStartedActivity?.dataString
        rule.runOnIdle { onCard.value = true }
        rule.onNodeWithText("Open in web").performClick()
        rule.waitForIdle()
        return listOf(fromSettings, app.nextStartedActivity?.dataString).map { it ?: "nothing opened" }
    }

    @Test fun web_links_open_the_configured_address_while_not_paired() {
        assertEquals(listOf("$base/companion", "$base/companion/a/s1"), webLinksOpen(graph()))
    }

    @Test fun web_links_open_the_public_base_url_once_paired() {
        val g = graph()
        assertTrue(g.credential.save(Paired(SAMPLE_TOKEN, "d-1", "Pixel", "https://web.nous.example"), base))
        assertEquals(listOf("https://web.nous.example/companion", "https://web.nous.example/companion/a/s1"), webLinksOpen(g))
    }

    @Test fun web_links_open_the_configured_address_when_the_pairing_gave_no_public_base_url() {
        val g = graph()
        assertTrue(g.credential.save(Paired(SAMPLE_TOKEN, "d-1", "Pixel", null), base))
        assertEquals(listOf("$base/companion", "$base/companion/a/s1"), webLinksOpen(g))
    }

    @Test fun a_refused_tap_says_pair_and_offers_settings() {
        answers = { json(403, notPaired) }
        val g = graph()
        val envelope = """{"version":"v1.0","createSurface":{"surfaceId":"s1","catalogId":"c","metadata":{"extensions":{"com_nous_nonce":"n1"}},"components":[
            {"id":"root","component":"Button","child":"label","action":{"event":{"name":"approval.choose","context":{}}}},
            {"id":"label","component":"Text","text":"Approve"}]}}"""
        g.store.apply(null, Json.parseToJsonElement(envelope).jsonObject)
        g.store.connection = Connection.LIVE
        var openedSettings = 0
        rule.setContent {
            NousThemed(Themes.nousDefault) {
                CompositionLocalProvider(LocalSurfaceHost provides SurfaceHost(g, "s1"), LocalOpenSettings provides { openedSettings++ }) {
                    Render("root", null, 0, emptyList())
                }
            }
        }
        rule.onNodeWithText("Approve").performClick()
        waitForText(PairingWords.TAP_NOT_PAIRED)
        rule.onNodeWithText("Open Settings").performClick()
        assertEquals(1, openedSettings)
        assertEquals(Connection.LIVE, g.store.connection)
    }
}
