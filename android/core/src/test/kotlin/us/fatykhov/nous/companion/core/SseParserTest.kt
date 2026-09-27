package us.fatykhov.nous.companion.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class SseParserTest {
    @Test fun parsesEnvelopeFrame() {
        val ev = SseParser().feed("id: 4821\nevent: a2ui\ndata: {\"version\":\"v1.0\"}\n\n")
        assertEquals(listOf(SseEvent(4821, "a2ui", "{\"version\":\"v1.0\"}")), ev)
    }

    @Test fun controlFrameHasNoId() {
        val ev = SseParser().feed("event: control\ndata: {\"type\":\"resync\"}\n\n")
        assertEquals(1, ev.size)
        assertNull(ev[0].id)
        assertEquals("control", ev[0].event)
    }

    @Test fun keepaliveCommentIsIgnored() {
        assertEquals(emptyList(), SseParser().feed(": keepalive\n\n"))
    }

    @Test fun handlesChunkBoundaries() {
        val p = SseParser()
        assertEquals(emptyList(), p.feed("id: 7\nev"))
        assertEquals(emptyList(), p.feed("ent: a2ui\ndata: {}\n"))
        assertEquals(listOf(SseEvent(7, "a2ui", "{}")), p.feed("\n"))
    }

    @Test fun resetsBetweenEvents() {
        val p = SseParser()
        p.feed("id: 1\nevent: a2ui\ndata: a\n\n")
        val second = p.feed("event: control\ndata: b\n\n")
        assertNull(second[0].id)
    }
}
