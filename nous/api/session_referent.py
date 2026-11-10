"""F110 v2: each channel's last assistant reply, kept in process across the session roll.

Telegram starts a new session after 30 minutes idle, and a new session's conversation is
empty, so a short "yes" there cannot see the offer it answers. The runner records the tail
of every interactive reply under its channel and, on a new session's first turn, hands the
tail and its age to ``pre_turn``. Pure: the caller passes the clock (``time.monotonic()``),
so a restart starts empty, which is today's behaviour.
"""

from __future__ import annotations


class ChannelLastReply:
    """Channel -> (session_id, monotonic time of the reply, tail of the reply)."""

    def __init__(self, *, max_age_s: float, max_chars: int) -> None:
        self._max_age_s = max_age_s
        self._max_chars = max_chars
        self._replies: dict[str, tuple[str, float, str]] = {}

    def record(self, channel: str, session_id: str, text: str, now: float) -> None:
        """Keep the last ``max_chars`` of ``text`` as the channel's last reply, then drop expired entries.

        A blank reply (a turn that ended before any text) leaves the previous one in place.
        """
        if not text.strip():
            return
        self._replies[channel] = (session_id, now, text[-self._max_chars :])
        self.prune(now)

    def lookup(self, channel: str, session_id: str, now: float) -> tuple[str, float] | None:
        """The channel's last reply and its age in seconds, when it came from another session
        no more than ``max_age_s`` ago; else None."""
        entry = self._replies.get(channel)
        if entry is None:
            return None
        stored_session, at, tail = entry
        age = now - at
        if stored_session == session_id or age > self._max_age_s:
            return None
        return tail, age

    def prune(self, now: float) -> None:
        """Drop the entries older than ``max_age_s``."""
        for channel in [c for c, (_s, at, _t) in self._replies.items() if now - at > self._max_age_s]:
            del self._replies[channel]
