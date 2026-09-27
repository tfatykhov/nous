package us.fatykhov.nous.companion

import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.getOrNull
import androidx.test.core.app.ApplicationProvider
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import us.fatykhov.nous.companion.core.SurfaceStore
import us.fatykhov.nous.companion.data.AppGraph
import us.fatykhov.nous.companion.ui.LocalSurfaceHost
import us.fatykhov.nous.companion.ui.NousThemed
import us.fatykhov.nous.companion.ui.Render
import us.fatykhov.nous.companion.ui.SurfaceHost
import us.fatykhov.nous.companion.ui.Tags
import us.fatykhov.nous.companion.ui.Themes
import us.fatykhov.nous.companion.ui.catalog.Registry
import java.io.File

/**
 * Spec §10.3/§10.4: the conformance examples that use ONLY ported components
 * must render with no fallback and no placeholder node, and their literal
 * strings must reach the semantics tree (the expected text is derived from
 * the fixture, not written by the renderer's author).
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = TestApp::class, sdk = [34])
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class RenderFixturesTest {
    @get:Rule val rule = createComposeRule()

    private val root = File(System.getProperty("nous.repoRoot") ?: "../..").canonicalFile
    private val json = Json { ignoreUnknownKeys = true }

    private fun fallbackNodes() = rule.onAllNodes(SemanticsMatcher("fallback/placeholder") { n ->
        val descs = n.config.getOrNull(SemanticsProperties.ContentDescription) ?: emptyList()
        descs.any { d -> d.startsWith(Tags.FALLBACK) || d.startsWith(Tags.PLACEHOLDER) }
    }, useUnmergedTree = true)

    @Test fun portedOnlyExamplesRenderCleanly() {
        val files = File(root, "tests/fixtures/a2ui/examples").listFiles { f -> f.extension == "json" }!!.sortedBy { it.name }
        var checked = 0
        for (f in files) {
            val doc = json.parseToJsonElement(f.readText()).jsonObject
            val store = SurfaceStore()
            doc["messages"]!!.jsonArray.forEachIndexed { i, m -> store.apply((i + 1).toLong(), m.jsonObject) }
            val id = store.surfaces.keys.first()
            val comps = store.surfaces[id]!!.components.values.mapNotNull { it["component"]?.let { c -> (c as? kotlinx.serialization.json.JsonPrimitive)?.content } }
            if (!comps.all { it in Registry.names }) continue   // PR 4/5 vocabulary: skipped, not hidden
            checked += 1
            val graph = AppGraph(ApplicationProvider.getApplicationContext())
            graph.store.apply(null, json.parseToJsonElement(f.readText()).jsonObject["messages"]!!.jsonArray[0].jsonObject)
            doc["messages"]!!.jsonArray.forEachIndexed { i, m -> graph.store.apply((i + 1).toLong(), m.jsonObject) }
            val host = SurfaceHost(graph, id)
            rule.setContent { NousThemed(Themes.nousDefault) { CompositionLocalProvider(LocalSurfaceHost provides host) { Render("root", null, 0, emptyList()) } } }
            rule.waitForIdle()
            assertEquals("${f.name}: fallback/placeholder nodes", 0, fallbackNodes().fetchSemanticsNodes().size)
            // Every literal Text `text` prop in the fixture must be visible (first line, markdown stripped of the heading marker).
            for (c in store.surfaces[id]!!.components.values) {
                if ((c["component"] as? kotlinx.serialization.json.JsonPrimitive)?.content != "Text") continue
                val lit = (c["text"] as? kotlinx.serialization.json.JsonPrimitive)?.takeIf { it.isString }?.content ?: continue
                val probe = lit.lineSequence().first().removePrefix("#").trimStart('#').trim().takeIf { it.isNotEmpty() && !it.contains('$') && !it.contains('*') && !it.contains('`') && !it.contains('[') } ?: continue
                // At least one: a fixture may legitimately repeat a literal (00_incremental has four "Book now" buttons).
                assertTrue("${f.name}: text '$probe' not rendered",
                    rule.onAllNodesWithText(probe, substring = true, useUnmergedTree = true).fetchSemanticsNodes().isNotEmpty())
            }
        }
        assertTrue("no fixture used only ported components — the sweep proved nothing", checked >= 10)
    }
}
