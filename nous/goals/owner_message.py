"""F114: the goal's own messages to the owner (a done claim, a pause the goal driver made).

A message is one owner-facing REPORT row on the owner's channel, written with ``continuation.insert_inbox_row``
directly: not through ``insert_report``, so the goals' report hook never moves it to a goal's journal, and with no
intention, so a cancel of a session's root never withdraws it. Its source id is the caller's key (one message per
claim, per pause row), and the insert does nothing when that key is already written, so a retry or a second driver
never sends it twice. Its words are the harness's fixed sentences and the goal's title, the owner-approved charter's,
on one line, quoted and bounded: nothing a chain wrote, and no command line (a claimed message reaches the next chat
turn).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain import continuation
from nous.goals.quote import _visible, neutralize
from nous.goals.store import clip, format_short_id

logger = logging.getLogger(__name__)

MESSAGE_TITLE_CHARS = 120  # the goal's title, as a message quotes it


def quoted_title(goal: Any) -> str:
    """The goal's title on one line, delimiters escaped, double quotes made single, at most ``MESSAGE_TITLE_CHARS``,
    in double quotes."""
    one_line = " ".join(_visible(str(goal.title)).split())
    return '"' + clip(neutralize(one_line).replace('"', "'"), MESSAGE_TITLE_CHARS) + '"'


def label(goal: Any) -> str:
    return format_short_id(goal.short_id)


async def tell_owner(
    s: AsyncSession, agent_id: str, settings: Any, *, goal: Any, source_id: UUID, title: str, body: str
) -> bool:
    """Write one message to the owner about ``goal`` in the caller's transaction (it commits). True when the row was
    written; False when the goal has nowhere to tell the owner, or ``source_id`` was already written."""
    channel = continuation.owner_channel(settings, goal.origin_channel)
    if channel is None:
        logger.warning("F114: goal %s has no channel to tell the owner on", goal.id)
        return False
    written = await continuation.insert_inbox_row(
        s,
        agent_id,
        source_kind=continuation.SOURCE_INTENTION_REPORT,
        source_id=source_id,
        msg_type=continuation.MSG_REPORT,
        title=title,
        body=body,
        channel=channel,
        correlation_id=str(source_id),
        push_after=await continuation.owner_push_after(s, agent_id, settings, channel=channel),
    )
    return written is not None
