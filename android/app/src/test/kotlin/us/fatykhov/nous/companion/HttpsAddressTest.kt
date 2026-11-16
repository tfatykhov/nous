package us.fatykhov.nous.companion

import android.content.Context
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import us.fatykhov.nous.companion.core.Connection
import us.fatykhov.nous.companion.data.AppGraph
import us.fatykhov.nous.companion.ui.CompanionApp
import us.fatykhov.nous.companion.ui.NEEDS_HTTPS
import us.fatykhov.nous.companion.ui.NousThemed
import us.fatykhov.nous.companion.ui.Route
import us.fatykhov.nous.companion.ui.SettingsScreen
import us.fatykhov.nous.companion.ui.Themes

/** A plain http address on a build that will not use clear text is named as the problem, in the inbox and in Settings. */
@RunWith(RobolectricTestRunner::class)
@Config(application = TestApp::class, sdk = [34])
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class HttpsAddressTest {
    @get:Rule val rule = createComposeRule()
    private val ctx: Context = ApplicationProvider.getApplicationContext()

    private fun graph(address: String, cleartext: Boolean) =
        AppGraph(ctx, FakeBox(), cleartextPermitted = { cleartext }).also { it.settings.baseUrl = address }

    private fun shows(text: String) = rule.onAllNodes(hasText(text)).fetchSemanticsNodes().isNotEmpty()

    @Test fun an_http_address_without_clear_text_says_to_use_the_https_address_in_the_inbox() {
        val g = graph("http://nous.example:8383", cleartext = false)
        g.store.connection = Connection.ERROR   // what the refused clear-text call leaves
        var route: Route = Route.Inbox
        rule.setContent { CompanionApp(g, Route.Inbox) { route = it } }
        assertTrue(shows(NEEDS_HTTPS))
        assertFalse("not the tailnet advice", shows("Can't reach Nous"))
        rule.onNodeWithText("Open Settings").performClick()
        assertEquals(Route.Settings, route)
    }

    @Test fun an_http_address_without_clear_text_says_so_in_settings() {
        val g = graph("http://nous.example:8383", cleartext = false)
        rule.setContent { NousThemed(Themes.nousDefault) { SettingsScreen(g) { } } }
        assertTrue(shows(NEEDS_HTTPS))
    }

    @Test fun an_https_address_or_a_build_that_allows_clear_text_is_not_told_so() {
        for ((address, cleartext) in listOf("https://nous.example" to false, "http://10.0.2.2:8000" to true)) {
            val g = graph(address, cleartext)
            assertFalse(address, g.needsHttps())
        }
        val g = graph("https://nous.example", cleartext = false)
        g.store.connection = Connection.ERROR
        rule.setContent { CompanionApp(g, Route.Inbox) { } }
        assertFalse(shows(NEEDS_HTTPS))
        assertTrue(shows("Can't reach Nous"))
    }
}
