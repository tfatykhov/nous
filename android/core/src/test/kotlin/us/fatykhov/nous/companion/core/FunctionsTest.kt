package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import us.fatykhov.nous.companion.core.Functions.Companion.toDisplayString
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class FunctionsTest {
    private val f = Functions()
    private fun j(s: String): JsonElement = Json.parseToJsonElement(s)
    private fun o(s: String) = j(s).jsonObject
    private fun ctx(model: String, scope: Scope? = null) = EvalContext(j(model), scope)
    private fun valid(r: JsonElement?) = (r as JsonObject)["valid"]!!.jsonPrimitive.booleanOrNull == true
    private fun call(name: String, args: String, c: EvalContext = ctx("{}")) = f.callFunction(name, o(args), c)

    @Test fun typeGuards() {
        assertTrue(Functions.isDataBinding(o("""{"path":"/a"}""")))
        assertFalse(Functions.isDataBinding(o("""{"call":"required"}""")))
        assertFalse(Functions.isDataBinding(JsonPrimitive("literal")))
        assertTrue(Functions.isFunctionCall(o("""{"call":"required"}""")))
        assertFalse(Functions.isFunctionCall(o("""{"path":"/a"}""")))
    }

    @Test fun displayString() {
        assertEquals("", toDisplayString(null)); assertEquals("", toDisplayString(JsonNull))
        assertEquals("hi", toDisplayString(JsonPrimitive("hi")))
        assertEquals("42", toDisplayString(JsonPrimitive(42))); assertEquals("42", toDisplayString(JsonPrimitive(42.0)))
        assertEquals("false", toDisplayString(JsonPrimitive(false)))
        assertEquals("""{"a":1}""", toDisplayString(o("""{"a":1}""")))
        assertEquals("""[1,"x"]""", toDisplayString(j("""[1,"x"]""")))
        assertEquals("3.5", toDisplayString(JsonPrimitive(3.5)))
    }

    @Test fun resolveDynamic() {
        val c = ctx("""{"a":{"b":"deep"}}""")
        assertEquals(JsonPrimitive("plain"), f.resolveDynamic(JsonPrimitive("plain"), c))
        assertEquals(JsonPrimitive("deep"), f.resolveDynamic(o("""{"path":"/a/b"}"""), c))
        assertTrue(valid(f.resolveDynamic(o("""{"call":"required","args":{"value":"x"}}"""), c)))
        val scoped = ctx("""{"rows":[{"n":"first"},{"n":"second"}]}""", Scope("/rows/1", 1))
        assertEquals(JsonPrimitive("second"), f.resolveDynamic(o("""{"path":"n"}"""), scoped))
    }

    @Test fun validators() {
        for (v in listOf("null", "\"\"", "[]")) assertFalse(valid(call("required", """{"value":$v}""")), v)
        assertFalse(valid(f.callFunction("required", JsonObject(emptyMap()), ctx("{}"))))
        for (v in listOf("\"x\"", "0", "false", "[\"a\"]")) assertTrue(valid(call("required", """{"value":$v}""")), v)
        assertTrue(valid(call("regex", """{"value":"94103","pattern":"^[0-9]{5}$"}""")))
        assertFalse(valid(call("regex", """{"value":"9410","pattern":"^[0-9]{5}$"}""")))
        assertFalse(valid(call("regex", """{"value":"941030","pattern":"^[0-9]{5}$"}""")))
        assertFalse(valid(call("regex", """{"value":"x","pattern":"(["}""")))
        assertTrue(valid(call("length", """{"value":"abc","min":2}""")))
        assertFalse(valid(call("length", """{"value":"a","min":2}""")))
        assertFalse(valid(call("length", """{"value":"abcd","max":3}""")))
        assertTrue(valid(call("length", """{"value":"abc","min":2,"max":3}""")))
        assertTrue(valid(call("length", """{"value":""}""")))
        assertTrue(valid(call("numeric", """{"value":5,"min":1,"max":10}""")))
        assertFalse(valid(call("numeric", """{"value":0,"min":1}""")))
        assertFalse(valid(call("numeric", """{"value":11,"max":10}""")))
        assertFalse(valid(call("numeric", """{"value":"abc"}""")))
        assertTrue(valid(call("numeric", """{"value":""}""")), "Number('') === 0 parity")
        assertTrue(valid(call("email", """{"value":"a@b.co"}""")))
        assertFalse(valid(call("email", """{"value":"a@b"}""")))
        assertFalse(valid(call("email", """{"value":"nope"}""")))
        assertFalse(valid(call("email", """{"value":""}""")))
    }

    private val guard = """{"call":"and","args":{"values":[{"path":"/formData/agree"},{"call":"or","args":{"values":[{"call":"required","args":{"value":{"path":"/formData/email"}}},{"call":"required","args":{"value":{"path":"/formData/phone"}}}]}},{"call":"required","args":{"value":{"path":"/formData/zip"}}}]}}"""

    @Test fun andOrNot() {
        assertEquals(JsonPrimitive(true), f.resolveDynamic(o(guard), ctx("""{"formData":{"email":"","phone":"5551234567","zip":"94103","agree":true}}""")))
        assertEquals(JsonPrimitive(false), f.resolveDynamic(o(guard), ctx("""{"formData":{"email":"","phone":"5551234567","zip":"94103","agree":false}}""")))
        assertEquals(JsonPrimitive(false), f.resolveDynamic(o(guard), ctx("""{"formData":{"email":"","phone":"","zip":"94103","agree":true}}""")))
        assertEquals(JsonPrimitive(true), call("and", "{}")); assertEquals(JsonPrimitive(false), call("or", "{}"))
        assertEquals(JsonPrimitive(false), call("not", """{"value":true}"""))
        assertEquals(JsonPrimitive(true), call("not", """{"value":{"valid":false}}"""))
        assertEquals(JsonPrimitive(true), call("not", """{"value":{"call":"required","args":{"value":""}}}"""))
    }

    @Test fun atIndex() {
        val c = ctx("{}", Scope("/rows/3", 3))
        assertEquals(3.0, (call("@index", "{}", c) as JsonPrimitive).content.toDouble())
        assertEquals(4.0, (call("@index", """{"offset":1}""", c) as JsonPrimitive).content.toDouble())
        assertEquals(2.0, (call("@index", """{"offset":-1}""", c) as JsonPrimitive).content.toDouble())
        assertEquals(10.0, (call("@index", """{"offset":{"path":"/off"}}""", ctx("""{"off":10}""", Scope("/rows/0", 0))) as JsonPrimitive).content.toDouble())
        assertFailsWith<IllegalStateException> { call("@index", "{}") }
    }

    @Test fun formatString() {
        assertEquals("just text", f.formatString("just text", ctx("{}")))
        assertEquals("Hi Ada!", f.formatString("Hi \${/user/name}!", ctx("""{"user":{"name":"Ada"}}""")))
        assertEquals("Hi Grace", f.formatString("Hi \${name}", ctx("""{"employees":[{"name":"Ada"},{"name":"Grace"}]}""", Scope("/employees/1", 1))))
        assertEquals("cost: \${100}", f.formatString("cost: \\\${100}", ctx("{}")))
        assertEquals("[]", f.formatString("[\${/nope}]", ctx("{}")))
        assertEquals("#1", f.formatString("#\${@index(offset: 1)}", ctx("""{"rows":[1,2]}""", Scope("/rows/0", 0))))
        val now = ctx("""{"now":"2025-12-15T12:00:00"}""")
        assertEquals("Hello! Today is Monday, December 15.", f.formatString("Hello! Today is \${formatDate(value: \${/now}, format: 'EEEE, MMMM d')}.", now))
        assertEquals(JsonPrimitive("Today is December 15, 2025"), f.resolveDynamic(o("""{"call":"formatString","args":{"value":"Today is ${'$'}{formatDate(value: ${'$'}{/now}, format: 'MMMM d, yyyy')}"}}"""), now))
        assertEquals("1 then 2", f.formatString("\${/a} then \${/b}", ctx("""{"a":1,"b":2}""")))
        assertEquals(JsonPrimitive("Hi Ada!"), call("formatString", """{"value":{"path":"/tmpl"}}""", ctx("""{"tmpl":"Hi ${'$'}{/name}!","name":"Ada"}""")))
    }

    @Test fun formattingHelpers() {
        assertEquals(JsonPrimitive("3.14"), call("formatNumber", """{"value":3.14159,"decimals":2}"""))
        assertEquals(JsonPrimitive("1235"), call("formatNumber", """{"value":1234.5,"decimals":0,"grouping":false}"""))
        assertTrue((call("formatNumber", """{"value":1234,"decimals":0}""") as JsonPrimitive).content.contains(","))
        assertTrue((call("formatCurrency", """{"value":5,"currency":"USD"}""") as JsonPrimitive).content.contains("5"))
        assertEquals(JsonPrimitive("NOTACODE 5"), call("formatCurrency", """{"value":5,"currency":"NOTACODE"}"""))
        assertEquals(JsonPrimitive("item"), call("pluralize", """{"value":1,"one":"item","other":"items"}"""))
        assertEquals(JsonPrimitive("items"), call("pluralize", """{"value":0,"one":"item","other":"items"}"""))
        assertEquals(JsonPrimitive("items"), call("pluralize", """{"value":2,"one":"item","other":"items"}"""))
        assertNull(call("noSuchFunction", "{}"))
    }

    @Test fun formatDateCldr() {
        val fixed = JsonPrimitive("2025-12-15T14:05:09")
        for ((p, e) in listOf("yyyy" to "2025", "MM" to "12", "MMM" to "Dec", "MMMM" to "December", "d" to "15", "dd" to "15", "EEE" to "Mon", "EEEE" to "Monday", "HH" to "14", "mm" to "05", "ss" to "09", "yy" to "25", "hh" to "02", "h" to "2", "H" to "14", "a" to "PM"))
            assertEquals(e, f.formatDateCldr(fixed, p), p)
        assertEquals("2025-12-15 14:05", f.formatDateCldr(fixed, "yyyy-MM-dd HH:mm"))
        assertEquals("Monday, December 15", f.formatDateCldr(fixed, "EEEE, MMMM d"))
        assertEquals("2025-12-15", f.formatDateCldr(JsonPrimitive("2025-12-15"), "yyyy-MM-dd"))
        assertEquals("15", f.formatDateCldr(JsonPrimitive("2025-12-15"), "d"))
        assertEquals("", f.formatDateCldr(JsonPrimitive("not a date"), "yyyy"))
        assertEquals("", f.formatDateCldr(null, "yyyy"))
        assertEquals("at 14", f.formatDateCldr(fixed, "'at' HH"))
    }

    @Test fun runChecks() {
        val c = ctx("""{"formData":{"email":"","agree":false}}""")
        assertEquals(emptyList(), f.runChecks(null, c)); assertEquals(emptyList(), f.runChecks(JsonArray(emptyList()), c))
        val r1 = f.runChecks(j("""[{"condition":{"call":"email","args":{"value":{"path":"/formData/email"}}},"message":"Invalid email format"}]""") as JsonArray, c)
        assertEquals(listOf(ValidationResult(false, "Invalid email address.")), r1)
        val r2 = f.runChecks(j("""[{"condition":{"call":"and","args":{"values":[{"path":"/formData/agree"}]}},"message":"You must agree to terms."}]""") as JsonArray, c)
        assertEquals(listOf(ValidationResult(false, "You must agree to terms.")), r2)
        assertEquals("Invalid.", f.runChecks(j("""[{"condition":false}]""") as JsonArray, c)[0].message)
        assertEquals(listOf(ValidationResult(false, "boom")), f.runChecks(j("""[{"condition":{"call":"@index"},"message":"boom"}]""") as JsonArray, c))
        val r5 = f.runChecks(j("""[{"condition":true},{"condition":false,"message":"second"},{"condition":{"valid":false,"message":"third"}}]""") as JsonArray, c)
        assertEquals(listOf("second", "third"), r5.map { it.message })
    }

    @Test fun truncation() {
        val (rows, omitted) = Functions.splitTruncation((j("""[{"a":1},{"_truncated":true,"omitted":3},{"a":2}]""") as JsonArray).toList())
        assertEquals(2, rows.size); assertEquals(3, omitted)
        assertEquals("…and 3 more not shown (source over budget)", Functions.omittedNote(3))
        assertEquals("…more not shown (source over budget)", Functions.omittedNote(0))
        assertEquals("", Functions.omittedNote(null))
    }

    @Test fun jsSemantics() {
        assertEquals("1.00", Functions.toFixed(1.005, 2))   // V8: 1.005 is 1.00499999...
        assertEquals("2.5", Functions.toFixed(2.5, 1)); assertEquals("3", Functions.jsNumber(3.0))
        assertEquals(0.0, Functions.jsNumberOf(JsonPrimitive(""))); assertTrue(Functions.jsNumberOf(JsonPrimitive("abc")).isNaN())
        assertEquals(1.0, Functions.jsNumberOf(JsonPrimitive(true)))
        // JS Number() grammar, not Java's: suffixes and hex-floats are NaN; 0x/0o/0b prefixes parse.
        assertTrue(Functions.jsNumberOfString("12d").isNaN()); assertTrue(Functions.jsNumberOfString("12f").isNaN())
        assertTrue(Functions.jsNumberOfString("0x1.8p3").isNaN())
        assertEquals(26.0, Functions.jsNumberOfString("0x1A")); assertEquals(8.0, Functions.jsNumberOfString("0o10")); assertEquals(5.0, Functions.jsNumberOfString("0b101"))
        assertEquals(1.5e3, Functions.jsNumberOfString(" 1.5e3 ")); assertEquals(0.5, Functions.jsNumberOfString(".5"))
        assertEquals(Double.POSITIVE_INFINITY, Functions.jsNumberOfString("Infinity"))
        assertEquals("NaN", Functions.toFixed(Double.NaN, 2)); assertEquals("Infinity", Functions.toFixed(Double.POSITIVE_INFINITY, 1))
    }
}
