package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class WalkerTest {
    private val registry = setOf("Column", "Text", "Card", "Button")
    private val w = Walker(registry)
    private fun surface(components: String, model: String = "{}"): SurfaceState {
        val s = SurfaceStore()
        s.apply(null, Json.parseToJsonElement("""{"createSurface":{"surfaceId":"s","catalogId":"c","components":$components,"dataModel":$model}}""").jsonObject)
        return s.surfaces["s"]!!
    }

    @Test fun placeholdersNeverThrow() {
        val s = surface("""[{"id":"root","component":"Column","children":["root"]},{"id":"x","component":"Mystery"}]""")
        assertTrue(w.resolve(s, "root", null, 0, listOf("root")) is Node.Placeholder)
        assertTrue(w.resolve(s, "root", null, 65, emptyList()) is Node.Placeholder)
        assertTrue(w.resolve(s, "nope", null, 0, emptyList()) is Node.Dangling)
        assertTrue(w.resolve(null, "root", null, 0, emptyList()) is Node.Dangling)
        assertEquals(Node.Unknown("x", "Mystery"), w.resolve(s, "x", null, 0, emptyList()))
        val r = w.resolve(s, "root", null, 0, emptyList()) as Node.Render
        assertEquals(1, r.depth); assertEquals(listOf("root"), r.ancestors)
    }

    @Test fun cycleIsReportedBySweep() {
        val s = surface("""[{"id":"root","component":"Column","children":["a"]},{"id":"a","component":"Card","child":"root"}]""")
        val problems = w.sweep(s)
        assertEquals(1, problems.size); assertTrue(problems[0] is Node.Placeholder)
    }

    @Test fun templateChildrenExpandWithScopeAndOmitted() {
        val s = surface(
            """[{"id":"root","component":"Column","children":{"componentId":"row","path":"/items"}},{"id":"row","component":"Text","text":{"path":"label"}}]""",
            """{"items":[{"label":"a"},{"label":"b"},{"_truncated":true,"omitted":4}]}""",
        )
        val kids = w.children(s, s.components["root"]!!["children"], null)
        assertEquals(listOf(Child.Template("row", Scope("/items/0", 0)), Child.Template("row", Scope("/items/1", 1)), Child.Omitted("…and 4 more not shown (source over budget)")), kids)
        assertEquals(emptyList(), w.sweep(s))
    }

    @Test fun relativeTemplatePathResolvesAgainstScope() {
        val s = surface("""[{"id":"root","component":"Column","children":{"componentId":"t","path":"rows"}},{"id":"t","component":"Text"}]""", """{"outer":{"rows":[1,2]}}""")
        val kids = w.children(s, s.components["root"]!!["children"], Scope("/outer", 0))
        assertEquals(2, kids.size); assertEquals(Scope("/outer/rows/1", 1), (kids[1] as Child.Template).scope)
    }

    @Test fun staticChildrenAndTabs() {
        val s = surface("""[{"id":"root","component":"Column","children":["a","b"]},{"id":"a","component":"Text"},{"id":"b","component":"Text"}]""")
        assertEquals(listOf(Child.Static("a"), Child.Static("b")), w.children(s, s.components["root"]!!["children"], null))
        assertEquals(emptyList(), w.sweep(s))
    }
}
