package us.fatykhov.nous.companion.core

import us.fatykhov.nous.companion.core.Inline.Code
import us.fatykhov.nous.companion.core.Inline.Em
import us.fatykhov.nous.companion.core.Inline.Link
import us.fatykhov.nous.companion.core.Inline.Strong
import us.fatykhov.nous.companion.core.Inline.Text
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class MarkdownTest {
    private val fixture35 = "# Heading 1\n\nThis is **bold** text and *italic* text.\n\n- List item 1\n- List item 2\n\n[Link to Google](https://google.com)"

    @Test fun fixture35Ast() {
        assertEquals(
            listOf(
                Block.Heading(1, listOf(Text("Heading 1"))),
                Block.Paragraph(listOf(Text("This is "), Strong(listOf(Text("bold"))), Text(" text and "), Em(listOf(Text("italic"))), Text(" text."))),
                Block.ListBlock(false, listOf(listOf(Text("List item 1")), listOf(Text("List item 2")))),
                Block.Paragraph(listOf(Link("https://google.com", listOf(Text("Link to Google"))))),
            ),
            Markdown.parse(fixture35),
        )
        assertEquals(listOf(Block.Heading(3, listOf(Text("Markdown Rendering")))), Markdown.parse("### Markdown Rendering"))
    }

    @Test fun headings() {
        for (level in 1..6) assertEquals(listOf(Block.Heading(level, listOf(Text("T")))), Markdown.parse("#".repeat(level) + " T"))
        assertTrue(Markdown.parse("#NotAHeading")[0] is Block.Paragraph)
    }

    @Test fun paragraphs() {
        assertEquals(listOf(Block.Paragraph(listOf(Text("one\ntwo")))), Markdown.parse("one\ntwo"))
        assertEquals(2, Markdown.parse("one\n\ntwo").size)
        assertEquals(emptyList(), Markdown.parse(""))
        assertEquals(emptyList(), Markdown.parse("\n\n"))
    }

    @Test fun lists() {
        for (m in listOf("-", "*", "+")) assertEquals(listOf(Block.ListBlock(false, listOf(listOf(Text("a")), listOf(Text("b"))))), Markdown.parse("$m a\n$m b"))
        assertEquals(listOf(Block.ListBlock(true, listOf(listOf(Text("first")), listOf(Text("second"))))), Markdown.parse("1. first\n2) second"))
        assertEquals(listOf(Block.ListBlock(false, listOf(listOf(Text("has "), Strong(listOf(Text("bold"))))))), Markdown.parse("- has **bold**"))
        assertEquals(listOf("ListBlock", "Paragraph"), Markdown.parse("- a\nplain").map { it::class.simpleName })
    }

    @Test fun fences() {
        assertEquals(listOf(Block.CodeBlock("not **bold** here")), Markdown.parse("```\nnot **bold** here\n```"))
        assertEquals(listOf(Block.CodeBlock("x = 1", "python")), Markdown.parse("```python\nx = 1\n```"))
        assertEquals(listOf(Block.CodeBlock("dangling")), Markdown.parse("```\ndangling"))
    }

    @Test fun inline() {
        assertEquals(listOf(Text("use "), Code("a **b** c"), Text(" now")), Markdown.parseInline("use `a **b** c` now"))
        assertEquals(listOf(Strong(listOf(Text("bold and "), Code("code")))), Markdown.parseInline("**bold and `code`**"))
        assertEquals(listOf(Strong(listOf(Em(listOf(Text("both")))))), Markdown.parseInline("***both***"))
        val flat = Markdown.parseInline("**bold *and italic***").toString()
        assertTrue(flat.contains("bold") && flat.contains("and italic"))
        assertEquals(listOf(Text("2 * 3 = 6")), Markdown.parseInline("2 * 3 = 6"))
        assertEquals(listOf(Text("**unclosed")), Markdown.parseInline("**unclosed"))
        assertEquals(listOf(Text("`unclosed")), Markdown.parseInline("`unclosed"))
        assertEquals(listOf(Text("com_nous_nonce")), Markdown.parseInline("com_nous_nonce"))
        assertEquals(listOf(Text("[just brackets]")), Markdown.parseInline("[just brackets]"))
    }

    @Test fun linkAllowlist() {
        for (href in listOf("https://example.com", "http://example.com", "mailto:a@b.co"))
            assertEquals(listOf(Link(href, listOf(Text("go")))), Markdown.parseInline("[go]($href)"))
        assertEquals(listOf(Text("click")), Markdown.parseInline("[click](javascript:alert)"))
        for (href in listOf("data:text/html,<script>", "/local/path", "file:///etc/passwd"))
            assertFalse(Markdown.parseInline("[x]($href)").any { it is Link })
        assertEquals(listOf(Text("before "), Strong(listOf(Text("bold label"))), Text(" after")), Markdown.parseInline("before [**bold label**](javascript:x) after"))
        assertFalse(Markdown.parseInline("[x](  javascript:alert)").any { it is Link })
    }
}
