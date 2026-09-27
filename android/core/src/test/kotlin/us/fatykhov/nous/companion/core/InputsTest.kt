package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.time.ZoneId
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class InputsTest {
    private fun j(s: String) = Json.parseToJsonElement(s)
    private val f = Functions()
    private fun store(model: String): SurfaceStore {
        val s = SurfaceStore()
        s.apply(null, j("""{"createSurface":{"surfaceId":"s","catalogId":"c","components":[],"dataModel":$model}}""").jsonObject)
        return s
    }

    @Test fun boundWriteIsLocalAndUnboundIsReadOnly() {
        val s = store("""{"formData":{"email":""}}""")
        val bound = j("""{"id":"e","component":"TextField","value":{"path":"/formData/email"}}""").jsonObject
        assertEquals("/formData/email", Inputs.boundPath(bound, null))
        assertTrue(Inputs.write(s, "s", bound, null, JsonPrimitive("a@b.co")))
        assertEquals("a@b.co", Inputs.textValue(Inputs.read(s, f, "s", bound, null)))
        assertEquals(0, s.lastSeq)   // a local patch is not a delivered envelope
        val literal = j("""{"id":"l","component":"TextField","value":"fixed"}""").jsonObject
        assertNull(Inputs.boundPath(literal, null))
        assertFalse(Inputs.write(s, "s", literal, null, JsonPrimitive("x")))
        assertEquals("fixed", Inputs.textValue(Inputs.read(s, f, "s", literal, null)))
    }

    @Test fun relativeBindingResolvesAgainstScope() {
        val s = store("""{"rows":[{"n":"a"},{"n":"b"}]}""")
        val comp = j("""{"id":"t","component":"TextField","value":{"path":"n"}}""").jsonObject
        assertTrue(Inputs.write(s, "s", comp, Scope("/rows/1", 1), JsonPrimitive("B")))
        assertEquals("B", (Pointer.get(s.surfaces["s"]!!.dataModel, "/rows/1/n") as JsonPrimitive).content)
    }

    @Test fun checkboxWritesRealBooleans() {
        val s = store("""{"agree":false}""")
        val comp = j("""{"id":"c","component":"CheckBox","value":{"path":"/agree"}}""").jsonObject
        assertFalse(Inputs.checkValue(Inputs.read(s, f, "s", comp, null)))
        Inputs.write(s, "s", comp, null, JsonPrimitive(true))
        assertEquals(JsonPrimitive(true), Pointer.get(s.surfaces["s"]!!.dataModel, "/agree"))
        assertTrue(Inputs.checkValue(JsonPrimitive("yes"))); assertFalse(Inputs.checkValue(JsonPrimitive("")))
    }

    @Test fun choicePickerAlwaysWritesAnArray() {
        assertEquals(listOf("b"), Inputs.choose(listOf("a"), "b", true, multiple = false))
        assertEquals(listOf("a", "b"), Inputs.choose(listOf("a"), "b", true, multiple = true))
        assertEquals(listOf("a"), Inputs.choose(listOf("a", "b"), "b", false, multiple = true))
        assertEquals(emptyList(), Inputs.selected(JsonPrimitive("a")))   // a scalar is not a selection
        assertEquals(listOf("1", "x"), Inputs.selected(j("""[1,"x"]""") as JsonArray))
        val opts = Inputs.options(j("""{"options":[{"value":"a","label":"Alpha"},{"value":"b","label":{"path":"/lbl"}}]}""").jsonObject, f, EvalContext(j("""{"lbl":"Beta"}""")))
        assertEquals(listOf(Inputs.Option("a", "Alpha"), Inputs.Option("b", "Beta")), opts)
        assertEquals(listOf(opts[1]), Inputs.filtered(opts, " bet ", filterable = true))
        assertEquals(opts, Inputs.filtered(opts, "bet", filterable = false))
    }

    @Test fun sliderStepsAreDivisionsAndWritesAreNumbers() {
        val spec = Inputs.sliderSpec(j("""{"min":0,"max":10,"steps":4}""").jsonObject)
        assertEquals(Inputs.SliderSpec(0.0, 10.0, 2.5), spec)
        assertNull(Inputs.sliderSpec(j("""{"min":0,"max":10}""").jsonObject).step)
        assertEquals(5.0, Inputs.sliderSnap(5.9, spec)); assertEquals(10.0, Inputs.sliderSnap(11.0, spec))
        assertEquals(0.0, Inputs.sliderValue(JsonPrimitive("abc"), spec))   // non-finite → min
        assertEquals(7.0, Inputs.sliderValue(JsonPrimitive("7"), spec))
        assertEquals(Inputs.SliderSpec(0.0, 100.0, null), Inputs.sliderSpec(j("{}").jsonObject))
    }

    @Test fun dateTimeNormalizesZonedInstantsToLocalWallClock() {
        val ny = ZoneId.of("America/New_York")
        assertEquals("2025-12-15T12:00", Inputs.normalizeForControl("2025-12-15T17:00:00Z", Inputs.DateKind.DATETIME, ny))
        assertEquals("2025-12-15", Inputs.normalizeForControl("2025-12-15T17:00:00Z", Inputs.DateKind.DATE, ny))
        assertEquals("12:00", Inputs.normalizeForControl("2025-12-15T17:00:00Z", Inputs.DateKind.TIME, ny))
        assertEquals("2026-08-29T14:30", Inputs.normalizeForControl("2026-08-29T14:30:00", Inputs.DateKind.DATETIME, ny))   // zone-less passes through
        assertEquals("14:30", Inputs.normalizeForControl("14:30:00", Inputs.DateKind.TIME, ny))
        assertEquals("", Inputs.normalizeForControl("garbage-with-zoneZ", Inputs.DateKind.DATETIME, ny))
        assertEquals(Inputs.DateKind.DATETIME, Inputs.dateKind(j("""{"enableDate":true,"enableTime":true}""").jsonObject))
        assertEquals(Inputs.DateKind.DATE, Inputs.dateKind(j("{}").jsonObject))   // both false → date, never nothing
    }

    @Test fun tabsClampAndResolveTitles() {
        val tabs = Inputs.tabs(j("""{"tabs":[{"title":"A","child":"a"},{"title":{"path":"/t"},"child":"b"},{"title":"C"}]}""").jsonObject, f, EvalContext(j("""{"t":"Bee"}""")))
        assertEquals(listOf(Inputs.Tab("A", "a"), Inputs.Tab("Bee", "b"), Inputs.Tab("C", null)), tabs)
        assertEquals(1, Inputs.activeTab(5, 2)); assertEquals(0, Inputs.activeTab(-3, 2)); assertEquals(0, Inputs.activeTab(2, 0))
    }
}
