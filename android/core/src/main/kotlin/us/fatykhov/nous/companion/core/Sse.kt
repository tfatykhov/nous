package us.fatykhov.nous.companion.core

/**
 * Minimal SSE frame parser for `/a2ui/stream` (nous/a2ui/transport.py).
 *
 * Server frames are exactly:
 * ```
 * id: <seq>\n event: a2ui\n data: <json>\n\n       // envelope; seq present
 * event: control\n data: {"type":"resync"}\n\n     // NO id line, ever
 * : keepalive\n\n                                  // comment; ignore
 * ```
 * The app never sends `Last-Event-ID` (spec R2) so the parsed id is used
 * only for store dedupe, never for resume.
 */
data class SseEvent(val id: Long?, val event: String, val data: String)

class SseParser {
    private val buf = StringBuilder()
    private var id: Long? = null
    private var event: String? = null
    private val data = StringBuilder()
    private var hasData = false

    /** Feed raw text; returns every complete event it terminates. */
    fun feed(chunk: String): List<SseEvent> {
        val out = mutableListOf<SseEvent>()
        buf.append(chunk)
        while (true) {
            val nl = buf.indexOf("\n")
            if (nl < 0) break
            var line = buf.substring(0, nl)
            buf.delete(0, nl + 1)
            if (line.endsWith("\r")) line = line.dropLast(1)
            if (line.isEmpty()) {
                dispatch(out)
                continue
            }
            if (line.startsWith(":")) continue
            val colon = line.indexOf(':')
            val field = if (colon < 0) line else line.substring(0, colon)
            var value = if (colon < 0) "" else line.substring(colon + 1)
            if (value.startsWith(" ")) value = value.substring(1)
            when (field) {
                "id" -> id = value.toLongOrNull()
                "event" -> event = value
                "data" -> { if (hasData) data.append('\n'); data.append(value); hasData = true }
                // "retry" and unknown fields are ignored: reconnect is ours (R2).
            }
        }
        return out
    }

    private fun dispatch(out: MutableList<SseEvent>) {
        if (hasData) out.add(SseEvent(id, event ?: "message", data.toString()))
        id = null; event = null; data.setLength(0); hasData = false
    }
}
