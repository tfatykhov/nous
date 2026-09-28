package us.fatykhov.nous.companion

import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.getOrNull
import androidx.test.core.app.ApplicationProvider
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import us.fatykhov.nous.companion.data.AppGraph
import us.fatykhov.nous.companion.ui.LocalSurfaceHost
import us.fatykhov.nous.companion.ui.NousThemed
import us.fatykhov.nous.companion.ui.Render
import us.fatykhov.nous.companion.ui.SurfaceHost
import us.fatykhov.nous.companion.ui.Tags
import us.fatykhov.nous.companion.ui.Themes
import java.io.File

/**
 * PR 5: the F096 report app — the one whole-app fixture CI keeps byte-
 * identical to the Python builder — renders with NO fallback and no
 * placeholder, every record present, no series "not a series" state, and
 * the `report` theme applied.
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = TestApp::class, sdk = [34])
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class ReportAppTest {
    @get:Rule val rule = createComposeRule()
    private val root = File(System.getProperty("nous.repoRoot") ?: "../..").canonicalFile

    private fun nodesTagged(prefix: String) = rule.onAllNodes(SemanticsMatcher(prefix) { n ->
        (n.config.getOrNull(SemanticsProperties.ContentDescription) ?: emptyList()).any { it.startsWith(prefix) }
    }, useUnmergedTree = true).fetchSemanticsNodes()

    @Test fun f096ReportAppRendersCompletely() {
        val doc = Json.parseToJsonElement(File(root, "dashboard-app/src/companion/catalog/__fixtures__/f096-report-app.json").readText()).jsonObject
        val graph = AppGraph(ApplicationProvider.getApplicationContext())
        graph.store.apply(null, buildJsonObject {
            put("createSurface", buildJsonObject {
                put("surfaceId", "nous:agent:micro_app:f096"); put("catalogId", "nous-core")
                put("metadata", buildJsonObject { put("extensions", buildJsonObject { put("com_nous_theme", "report") }) })
                put("components", doc["components"]!!); put("dataModel", doc["dataModel"]!!)
            })
        })
        val host = SurfaceHost(graph, "nous:agent:micro_app:f096")
        assertEquals("report", Themes.byId(host.surface!!.theme).id)
        rule.setContent { NousThemed(Themes.byId(host.surface!!.theme)) { CompositionLocalProvider(LocalSurfaceHost provides host) { Render("root", null, 0, emptyList()) } } }
        rule.waitForIdle()
        assertEquals("fallback cards", 0, nodesTagged(Tags.FALLBACK).size)
        assertEquals("placeholders", 0, nodesTagged(Tags.PLACEHOLDER).size)
        assertEquals("bad series states", 0, nodesTagged("series-state").size)
        assertTrue("stamp rendered", nodesTagged("stamp").isNotEmpty())
        assertTrue("sparklines rendered", nodesTagged("sparkline").isNotEmpty())
        // Every literal Section title in the fixture reaches the screen.
        for (c in doc["components"]!!.let { it as kotlinx.serialization.json.JsonArray }) {
            val o = c.jsonObject
            val kind = (o["component"] as? JsonPrimitive)?.content
            if (kind == "Section") { val title = (o["title"] as? JsonPrimitive)?.content ?: continue
                assertTrue("section '$title' rendered", rule.onAllNodesWithText(title, substring = true, useUnmergedTree = true).fetchSemanticsNodes().isNotEmpty()) }
        }
    }
}
