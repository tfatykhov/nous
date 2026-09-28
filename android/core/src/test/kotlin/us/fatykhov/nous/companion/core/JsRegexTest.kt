package us.fatykhov.nous.companion.core

import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class JsRegexTest {
    @Test
    fun whitespaceIsTheJsSetOutsideAndInsideAClass() {
        val outside = JsRegex.compile("""^a\sb$""")
        val inside = JsRegex.compile("""^a[\s,]b$""")
        for (ws in listOf(" ", "\t", " ", " ", " ", "　", "﻿", " ")) {
            assertTrue(outside.matches("a${ws}b"), "outside: U+%04X".format(ws[0].code))
            assertTrue(inside.matches("a${ws}b"), "inside: U+%04X".format(ws[0].code))
        }
        assertTrue(inside.matches("a,b"))
        assertFalse(outside.matches("a_b"))
    }

    @Test
    fun digitAndWordAreAsciiLikeJs() {
        assertTrue(JsRegex.compile("""^\d+$""").matches("2026"))
        assertFalse(JsRegex.compile("""^\d+$""").matches("٢٠٢٦"), "Arabic-Indic digits are not JS \\d")
        assertTrue(JsRegex.compile("""^[\d.]+$""").matches("1.5"))
        assertFalse(JsRegex.compile("""^[\d.]+$""").matches("१.५"))
        assertTrue(JsRegex.compile("""^\w+$""").matches("snake_case9"))
        assertFalse(JsRegex.compile("""^\w+$""").matches("café"))
        assertTrue(JsRegex.compile("""^\D$""").matches("x"))
        assertTrue(JsRegex.compile("""^\S$""").matches("x"))
        assertFalse(JsRegex.compile("""^\S$""").matches(" "))
    }

    @Test
    fun escapesAndBracketsSurviveTranslation() {
        assertEquals("""^\[([^\]]*)\]\(([^)]*)\)""", JsRegex.translate("""^\[([^\]]*)\]\(([^)]*)\)"""))
        assertEquals("""\\[0-9]""", JsRegex.translate("""\\\d"""))   // an escaped backslash then \d
        assertEquals("""\p{Nd}{1,2}""", JsRegex.translate("""\p{Nd}{1,2}"""))
        assertTrue(JsRegex.compile("""^\[([^\]]*)\]\(([^)]*)\)""").matches("[label](https://x)"))
    }

    @Test
    fun figureStillReadsNbspFigures() {
        assertTrue(Figure.isFigureValue("1 234,5 kg"))
        assertTrue(Figure.isFigureValue("12:30 pm"))
        assertFalse(Figure.isFigureValue("Twelve thirty in the afternoon, roughly"))
    }

    /** Ratchet: every regex in `:core` main goes through JsRegex, so the platform ambiguity cannot come back one site at a time. */
    @Test
    fun everyCoreRegexGoesThroughJsRegex() {
        val root = File("src/main/kotlin")
        assertTrue(root.isDirectory, "run from android/core: ${root.absolutePath}")
        val bare = Regex("""(?<![A-Za-z.])(Regex\(|toRegex\(|Pattern\.compile\(|UNICODE_CHARACTER_CLASS)""")
        val offenders = root.walkTopDown().filter { it.extension == "kt" && it.name != "JsRegex.kt" }.flatMap { f ->
            f.readLines().withIndex().filter { (_, line) -> bare.containsMatchIn(line) && !line.trim().startsWith("*") && !line.trim().startsWith("//") }
                .map { (i, line) -> "${f.name}:${i + 1}: ${line.trim()}" }
        }.toList()
        assertEquals(emptyList(), offenders)
    }
}
