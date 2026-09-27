package us.fatykhov.nous.companion.core

/**
 * Markdown-lite for the basic catalog's Text component — a port of
 * `markdown.ts`. Emphasis is `*…*` only (never `_…_`: snake_case survives);
 * links are parsed because conformance fixture 35 renders one, and gated
 * on the same scheme allowlist `openUrl` uses (spec R13).
 */
sealed interface Inline {
    data class Text(val value: String) : Inline
    data class Strong(val children: List<Inline>) : Inline
    data class Em(val children: List<Inline>) : Inline
    data class Code(val value: String) : Inline
    data class Link(val href: String, val children: List<Inline>) : Inline
}

sealed interface Block {
    data class Heading(val level: Int, val children: List<Inline>) : Block
    data class Paragraph(val children: List<Inline>) : Block
    data class ListBlock(val ordered: Boolean, val items: List<List<Inline>>) : Block
    data class CodeBlock(val value: String, val lang: String? = null) : Block
}

object Markdown {
    private val UNORDERED = Regex("""^\s*[-*+]\s+(.*)$""")
    private val ORDERED = Regex("""^\s*\d+[.)]\s+(.*)$""")
    private val HEADING = Regex("""^(#{1,6})\s+(.*)$""")
    private val FENCE = Regex("""^\s*```(.*)$""")
    private val LINK = Regex("""^\[([^\]]*)\]\(([^)]*)\)""")

    fun isSafeUrl(url: String): Boolean = Regex("""^(https?:|mailto:)""", RegexOption.IGNORE_CASE).containsMatchIn(url.trim())

    fun parseInline(src: String): List<Inline> {
        val out = mutableListOf<Inline>()
        val buffer = StringBuilder()
        fun flush() { if (buffer.isNotEmpty()) { out.add(Inline.Text(buffer.toString())); buffer.setLength(0) } }
        var i = 0
        while (i < src.length) {
            val rest = src.substring(i)
            if (rest[0] == '`') {
                val end = rest.indexOf('`', 1)
                if (end > 0) { flush(); out.add(Inline.Code(rest.substring(1, end))); i += end + 1; continue }
            }
            if (rest[0] == '[') {
                val m = LINK.find(rest)
                if (m != null) {
                    flush()
                    val label = parseInline(m.groupValues[1])
                    val href = m.groupValues[2].trim()
                    if (isSafeUrl(href)) out.add(Inline.Link(href, label)) else out.addAll(label)
                    i += m.value.length; continue
                }
            }
            if (rest.startsWith("***")) {
                val end = rest.indexOf("***", 3)
                if (end > 3) { flush(); out.add(Inline.Strong(listOf(Inline.Em(parseInline(rest.substring(3, end)))))); i += end + 3; continue }
            }
            if (rest.startsWith("**")) {
                val end = rest.indexOf("**", 2)
                if (end > 2) { flush(); out.add(Inline.Strong(parseInline(rest.substring(2, end)))); i += end + 2; continue }
            }
            if (rest[0] == '*') {
                val end = rest.indexOf('*', 1)
                if (end > 1) { flush(); out.add(Inline.Em(parseInline(rest.substring(1, end)))); i += end + 1; continue }
            }
            buffer.append(src[i]); i += 1
        }
        flush()
        return out
    }

    fun parse(src: String): List<Block> {
        val lines = src.split(Regex("\r?\n"))
        val blocks = mutableListOf<Block>()
        val paragraph = mutableListOf<String>()
        fun flushParagraph() {
            if (paragraph.isEmpty()) return
            blocks.add(Block.Paragraph(parseInline(paragraph.joinToString("\n"))))
            paragraph.clear()
        }
        var i = 0
        while (i < lines.size) {
            val line = lines[i]
            val fence = FENCE.find(line)
            if (fence != null) {
                flushParagraph()
                val body = mutableListOf<String>()
                i += 1
                while (i < lines.size && !FENCE.containsMatchIn(lines[i])) { body.add(lines[i]); i += 1 }
                i += 1
                val lang = fence.groupValues[1].trim()
                blocks.add(Block.CodeBlock(body.joinToString("\n"), lang.ifEmpty { null }))
                continue
            }
            val heading = HEADING.find(line)
            if (heading != null) {
                flushParagraph()
                blocks.add(Block.Heading(heading.groupValues[1].length, parseInline(heading.groupValues[2].trim())))
                i += 1; continue
            }
            if (UNORDERED.matches(line) || ORDERED.matches(line)) {
                flushParagraph()
                val ordered = !UNORDERED.matches(line)
                val items = mutableListOf<List<Inline>>()
                while (i < lines.size) {
                    val m = (if (ordered) ORDERED else UNORDERED).find(lines[i]) ?: break
                    items.add(parseInline(m.groupValues[1])); i += 1
                }
                blocks.add(Block.ListBlock(ordered, items))
                continue
            }
            if (line.isBlank()) { flushParagraph(); i += 1; continue }
            paragraph.add(line); i += 1
        }
        flushParagraph()
        return blocks
    }
}
