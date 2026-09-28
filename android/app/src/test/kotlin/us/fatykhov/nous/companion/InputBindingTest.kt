package us.fatykhov.nous.companion

import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithContentDescription
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextReplacement
import androidx.test.core.app.ApplicationProvider
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import us.fatykhov.nous.companion.core.Pointer
import us.fatykhov.nous.companion.data.AppGraph
import us.fatykhov.nous.companion.ui.LocalSurfaceHost
import us.fatykhov.nous.companion.ui.NousThemed
import us.fatykhov.nous.companion.ui.Render
import us.fatykhov.nous.companion.ui.SurfaceHost
import us.fatykhov.nous.companion.ui.Themes
import java.io.File

/**
 * PR 4: two-way binding end to end through Compose — typing into a bound
 * TextField and tapping a CheckBox change the surface's data model LOCALLY
 * (no envelope, lastSeq unchanged), and a `checks` failure paints inline.
 * Uses the real conformance fixture 32 (advanced form validator).
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = TestApp::class, sdk = [34])
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class InputBindingTest {
    @get:Rule val rule = createComposeRule()
    private val root = File(System.getProperty("nous.repoRoot") ?: "../..").canonicalFile
    private val json = Json { ignoreUnknownKeys = true }

    @Test fun fixture32BindsAndValidates() {
        val doc = json.parseToJsonElement(File(root, "tests/fixtures/a2ui/examples/32_advanced-form-validator.json").readText()).jsonObject
        val graph = AppGraph(ApplicationProvider.getApplicationContext())
        doc["messages"]!!.jsonArray.forEachIndexed { i, m -> graph.store.apply((i + 1).toLong(), m.jsonObject) }
        val id = graph.store.surfaces.keys.first()
        val host = SurfaceHost(graph, id)
        rule.setContent { NousThemed(Themes.nousDefault) { CompositionLocalProvider(LocalSurfaceHost provides host) { Render("root", null, 0, emptyList()) } } }
        rule.waitForIdle()

        // The email field is bound to /formData/email and carries an `email` check.
        val emailId = graph.store.surfaces[id]!!.components.entries.first { (_, c) -> c["component"]?.jsonPrimitive?.content == "TextField" && (c["value"]?.jsonObject?.get("path")?.jsonPrimitive?.content == "/formData/email") }.key
        val seqBefore = graph.store.lastSeq
        rule.onNodeWithContentDescription("textfield:$emailId").performTextReplacement("not-an-email")
        rule.waitForIdle()
        assertEquals(JsonPrimitive("not-an-email"), Pointer.get(graph.store.surfaces[id]!!.dataModel, "/formData/email"))
        assertEquals("a local write is not an envelope", seqBefore, graph.store.lastSeq)
        assertTrue("email check should fail inline", rule.onAllNodesWithContentDescription("check-failure").fetchSemanticsNodes().isNotEmpty())

        rule.onNodeWithContentDescription("textfield:$emailId").performTextReplacement("a@b.co")
        rule.waitForIdle()
        assertEquals(JsonPrimitive("a@b.co"), Pointer.get(graph.store.surfaces[id]!!.dataModel, "/formData/email"))

        // The agree checkbox writes a real boolean.
        val agreeId = graph.store.surfaces[id]!!.components.entries.first { (_, c) -> c["component"]?.jsonPrimitive?.content == "CheckBox" }.key
        rule.onNodeWithContentDescription("checkbox:$agreeId").performClick()
        rule.waitForIdle()
        assertEquals(JsonPrimitive(true), Pointer.get(graph.store.surfaces[id]!!.dataModel, "/formData/agree"))
    }

    @Test fun choicePickerWritesArrayAndSliderSnaps() {
        val graph = AppGraph(ApplicationProvider.getApplicationContext())
        graph.store.apply(null, json.parseToJsonElement("""{"createSurface":{"surfaceId":"s","catalogId":"c","dataModel":{"where":[],"n":3},"components":[
            {"id":"root","component":"Column","children":["pick","sl"]},
            {"id":"pick","component":"ChoicePicker","label":"Where","variant":"mutuallyExclusive","options":[{"label":"Ballroom","value":"ballroom"},{"label":"Terrace","value":"terrace"}],"value":{"path":"/where"}},
            {"id":"sl","component":"Slider","label":"N","min":0,"max":10,"steps":5,"value":{"path":"/n"}}]}}""").jsonObject)
        val host = SurfaceHost(graph, "s")
        rule.setContent { NousThemed(Themes.nousDefault) { CompositionLocalProvider(LocalSurfaceHost provides host) { Render("root", null, 0, emptyList()) } } }
        rule.waitForIdle()
        rule.onNodeWithContentDescription("option:terrace").performClick()
        rule.waitForIdle()
        assertEquals(listOf("terrace"), Pointer.get(graph.store.surfaces["s"]!!.dataModel, "/where")!!.jsonArray.map { it.jsonPrimitive.content })
        rule.onNodeWithText("Ballroom").assertExists()
    }
}
