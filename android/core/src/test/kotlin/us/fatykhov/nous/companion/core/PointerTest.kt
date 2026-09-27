package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class PointerTest {
    private fun j(s: String): JsonElement = Json.parseToJsonElement(s)

    @Test fun writesNested() {
        assertEquals(j("""{"a":{"b":2}}"""), Pointer.set(j("""{"a":{"b":1}}"""), "/a/b", JsonPrimitive(2)))
    }

    @Test fun createsMissingObjects() {
        assertEquals(j("""{"a":{"b":{"c":"deep"}}}"""), Pointer.set(j("{}"), "/a/b/c", JsonPrimitive("deep")))
    }

    @Test fun createsArraysForNumericTokens() {
        assertEquals(j("""{"items":[null,{"name":"second"}]}"""), Pointer.set(j("{}"), "/items/1/name", JsonPrimitive("second")))
    }

    @Test fun nullDeletesKey() {
        assertEquals(j("""{"a":1}"""), Pointer.set(j("""{"a":1,"b":2}"""), "/b", null))
    }

    @Test fun nullRemovesArrayElement() {
        assertEquals(j("""{"items":["x","z"]}"""), Pointer.set(j("""{"items":["x","y","z"]}"""), "/items/1", null))
    }

    @Test fun deletingMissingIsNoop() {
        assertEquals(j("""{"a":1}"""), Pointer.set(j("""{"a":1}"""), "/zzz", null))
    }

    @Test fun unescapesTilde1BeforeTilde0() {
        // "~01" must become "~1" (not "/"): ~1→/ first, then ~0→~.
        assertEquals(listOf("~1", "a/b"), Pointer.tokens("/~01/a~1b"))
    }

    @Test fun getMissingIsNull() {
        assertNull(Pointer.get(j("""{"a":[1]}"""), "/a/5"))
        assertNull(Pointer.get(j("""{"a":[1]}"""), "/b"))
    }

    @Test fun absoluteResolution() {
        assertEquals("/x", Pointer.absolute("/x", "/items/2"))
        assertEquals("/items/2/label", Pointer.absolute("label", "/items/2"))
        assertEquals("/label", Pointer.absolute("label", null))
    }
}
