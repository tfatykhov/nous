"""F099 Phase 2c and 2d: the owner push (spec 4.5.8).

Owner-facing rows (a REPORT, a QUESTION, a PROPOSAL) are written to the inbox at once, so chat shows them at any
hour. Telegram is the push: each row carries ``push_after`` (now, or the end of the quiet hours, set by
``continuation.push_after_for`` when the row was written), and this sweep sends the rows that are due, once each.

2d: a PROPOSAL goes out with Approve and Reject buttons, a QUESTION with ``force_reply`` (a reply to the message
is the answer). Both are sent as HTML in which every model-authored string is escaped and inside ``<pre>``: a
proposal's arguments and rationale, and a question, are model output that an injected result may have shaped, and
Telegram turns ``/command`` text in a plain message into a tappable command and parses links and mentions, but
parses no entity inside ``pre``. Before the HTML escape, the characters ``continuation.render_arguments`` shows
as escapes (bidi, zero-width, control) are shown as escapes in the rationale, the note and the question too
(``continuation.render_text``). A REPORT is sent the same way (2d final review m4): the model learns its own
proposal's short id in the turn, so a report it shapes could otherwise carry a tappable ``/approve <id>``.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select, update

from nous.brain import continuation
from nous.owner_actions import ACTION_APPROVE, ACTION_REJECT, callback_data
from nous.storage.models import IntentionArrival, IntentionProposal, ResultInbox

logger = logging.getLogger(__name__)

PUSH_BATCH = 20
TELEGRAM_TEXT_MAX = continuation.RAW_PUSH_CHARS  # 3900: one constant for the raw pushes
PUSHED_KINDS = (continuation.MSG_REPORT, continuation.MSG_QUESTION, continuation.MSG_PROPOSAL)
# Telegram's force_reply: the owner's client opens a reply to this message, and the reply is the answer.
QUESTION_MARKUP: dict[str, Any] = {"force_reply": True, "input_field_placeholder": "Your answer", "selective": False}

# What one send came to. A refusal is final for its row (the bot is blocked, the chat is gone): the row is
# stamped unsent, so it cannot hold the rows behind it. Any other failure leaves every row due.
SENT, TRANSIENT, REFUSED = "sent", "transient", "refused"
REFUSED_STATUSES = frozenset({400, 403})  # 401 and 404 are a wrong token: a fault for every row, never refused


def _esc(value: str) -> str:
    return html.escape(value, quote=False)


def _pre(value: str) -> str:
    return f"<pre>{_esc(value)}</pre>"


def proposal_keyboard(proposal_id: UUID) -> dict[str, Any]:
    """The inline keyboard of a PROPOSAL message: Approve and Reject, each carrying the proposal's id."""
    return {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": callback_data(proposal_id, ACTION_APPROVE)},
                {"text": "Reject", "callback_data": callback_data(proposal_id, ACTION_REJECT)},
            ]
        ]
    }


def render_proposal_html(proposal: Any, note: str | None) -> str:
    """The Telegram text of a PROPOSAL, in HTML. Outside ``<pre>``: fixed words, the proposal's 8-hex short id, the
    tool name (a registered name, escaped anyway) and the expiry time. Inside, escaped: the rationale
    (``render_text``), the call exactly as ``continuation.render_arguments`` renders it, and the arrival's note
    (``proposal_note``: one line, at most ``PROPOSAL_NOTE_MAX_CHARS`` UTF-16 units). Nothing else is cut: the
    stage-time caps, which count UTF-16 units of what is shown as Telegram does, make the longest message fit one
    Telegram message whole."""
    context = continuation.proposal_note(note)
    lines = [
        f"<b>Approval needed</b> <code>{proposal.id.hex[:8]}</code>",
        f"Tool: <code>{_esc(proposal.tool)}</code>",
        "<b>Why</b>",
        _pre(continuation.render_text(proposal.rationale)),
        "<b>Call, exactly as it will run</b>",
        _pre(continuation.render_arguments(proposal.arguments)),
    ]
    if context:
        lines += ["<b>Nous says</b>", _pre(context)]
    if proposal.deadline is not None:
        lines.append(
            f"Expires {proposal.deadline.astimezone(UTC):%Y-%m-%d %H:%M} UTC. If you do nothing, it is rejected."
        )
    return "\n".join(lines)


def render_question_html(title: str, body: str) -> str:
    """The Telegram text of a QUESTION, in HTML: the title and the question are model-authored, so both are
    shown with ``render_text``'s escapes and HTML-escaped inside ``<pre>``."""
    quoted = continuation.render_text(f"{title}\n\n{body}")
    return f"<b>Question</b>\n{_pre(quoted)}\nReply to this message to answer it."


def render_report_html(title: str, body: str) -> str:
    """The Telegram text of a REPORT, in HTML: the title (it names the intent) and the body are model-authored, so
    both are shown with ``render_text``'s escapes and HTML-escaped inside ``<pre>``, and nothing sits outside it."""
    return _pre(continuation.render_text(f"{title}\n\n{body}"))


class OwnerPublisher:
    """Sends the owner-facing rows that are due to Telegram, once each (see the module docstring)."""

    def __init__(self, *, database: Any, settings: Any, http_client: Any = None) -> None:
        self._db = database
        self._settings = settings
        self._http = http_client
        self._lock = asyncio.Lock()  # one sweep at a time: the stamp is the idempotence, this spares the duplicate send

    async def push_due(self, limit: int = PUSH_BATCH, *, now: datetime | None = None) -> int:
        """Send up to ``limit`` due rows; the number sent. Inert without the flag or a bot token."""
        settings = self._settings
        token = getattr(settings, "telegram_bot_token", "") or ""
        if not continuation.enabled(settings) or not token:
            return 0
        async with self._lock:
            now = now or datetime.now(UTC)
            since = now - timedelta(hours=settings.result_inbox_max_age_hours)
            async with self._db.session() as session:
                rows = (
                    (
                        await session.execute(
                            select(ResultInbox)
                            .where(
                                ResultInbox.agent_id == settings.agent_id,
                                ResultInbox.source_kind == continuation.SOURCE_INTENTION_REPORT,
                                ResultInbox.msg_type.in_(PUSHED_KINDS),
                                ResultInbox.pushed_at.is_(None),
                                ResultInbox.push_after.is_not(None),
                                ResultInbox.push_after <= now,
                                ResultInbox.channel.like("telegram:%"),
                                ResultInbox.created_at > since,
                            )
                            .order_by(ResultInbox.push_after, ResultInbox.id)
                            .limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )
                proposals, notes = await self._proposals_of(session, rows)
            pushed = 0
            for row in rows:
                proposal = proposals.get(row.proposal_id) if row.proposal_id is not None else None
                note = notes.get(proposal.arrival_id) if proposal is not None else None
                result, message_id = await self._send(token, row, proposal, note)
                if result == TRANSIENT:
                    break  # an outage is not hammered: the rows stay due for the next sweep
                # Sent, or refused for good: stamped either way (a refusal with no message id), never sent again.
                async with self._db.session() as session:
                    stamped = (
                        await session.execute(
                            update(ResultInbox)
                            .where(ResultInbox.id == row.id, ResultInbox.pushed_at.is_(None))
                            .values(pushed_at=datetime.now(UTC), push_message_id=message_id)
                            .returning(ResultInbox.id)
                            .execution_options(synchronize_session=False)
                        )
                    ).scalar_one_or_none()
                    await session.commit()
                if stamped is not None and result == SENT:
                    pushed += 1
            return pushed

    @staticmethod
    async def _proposals_of(session: Any, rows: list[ResultInbox]) -> tuple[dict[UUID, Any], dict[UUID, str | None]]:
        """The proposals the PROPOSAL rows show, and the notes of their arrivals (the "Nous says" context)."""
        proposal_ids = [r.proposal_id for r in rows if r.msg_type == continuation.MSG_PROPOSAL and r.proposal_id]
        if not proposal_ids:
            return {}, {}
        proposals = {
            p.id: p
            for p in (
                await session.execute(select(IntentionProposal).where(IntentionProposal.id.in_(proposal_ids)))
            ).scalars()
        }
        arrival_ids = {p.arrival_id for p in proposals.values() if p.arrival_id is not None}
        notes: dict[UUID, str | None] = {}
        if arrival_ids:
            notes = dict(
                (
                    await session.execute(
                        select(IntentionArrival.id, IntentionArrival.note).where(IntentionArrival.id.in_(arrival_ids))
                    )
                ).all()
            )
        return proposals, notes

    def _payload(self, row: ResultInbox, chat_id: str, proposal: Any, note: str | None) -> dict[str, Any] | None:
        """The sendMessage body of ``row``, or None when there is nothing to send (a PROPOSAL whose proposal is
        gone or no longer pending: its buttons would approve nothing)."""
        if row.msg_type == continuation.MSG_PROPOSAL:
            if proposal is None or proposal.state != continuation.PROPOSAL_PENDING:
                return None
            return {
                "chat_id": chat_id,
                "text": render_proposal_html(proposal, note),
                "parse_mode": "HTML",
                "reply_markup": proposal_keyboard(proposal.id),
            }
        # The body gets the room the title leaves, so its [truncated] marker survives the Telegram cut.
        if row.msg_type == continuation.MSG_QUESTION:
            # Counted as Telegram counts (UTF-16 units of the escaped text, like the stage-time caps). 60 covers the
            # 48 fixed characters of render_question_html (its header and its closing line), counted after parsing.
            title = continuation.render_text(row.title)
            room = max(100, TELEGRAM_TEXT_MAX - continuation.utf16_units(title) - 60)
            body = continuation.clip_body(row.body, self._settings, limit=room)
            body = continuation.clip_shown(body, room, marker="\n[truncated]")
            return {
                "chat_id": chat_id,
                "text": render_question_html(title, body),
                "parse_mode": "HTML",
                "reply_markup": QUESTION_MARKUP,
            }
        # A REPORT, counted as the QUESTION is: 2 covers the blank line between the title and the body.
        title = continuation.render_text(row.title)
        room = max(100, TELEGRAM_TEXT_MAX - continuation.utf16_units(title) - 2)
        body = continuation.clip_body(row.body, self._settings, limit=room)  # carry-over 7, C20: the store's one clip
        body = continuation.clip_shown(body, room, marker="\n[truncated]")
        return {"chat_id": chat_id, "text": render_report_html(title, body), "parse_mode": "HTML"}

    async def _send(
        self, token: str, row: ResultInbox, proposal: Any = None, note: str | None = None
    ) -> tuple[str, int | None]:
        """One sendMessage. ``(result, message_id)``: SENT; REFUSED for HTTP 400 or 403, for a row with no chat id,
        or for a PROPOSAL with nothing left to approve (never sent); TRANSIENT for an exception or any other failed
        status (401, 404, 429, 5xx...). The URL carries the bot token, so nothing here logs it, the response, or a
        traceback: a failure names the row and the exception class or the status only."""
        chat_id = row.channel.split(":", 1)[1]
        if not chat_id:
            logger.warning("F099: row %s has no Telegram chat id; it is not pushed", row.id.hex[:8])
            return REFUSED, None
        payload = self._payload(row, chat_id, proposal, note)
        if payload is None:
            logger.info("F099: proposal row %s has nothing left to approve; it is not pushed", row.id.hex[:8])
            return REFUSED, None
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            if self._http is not None:
                response = await self._http.post(url, json=payload, timeout=10)
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, json=payload, timeout=10)
        except Exception as exc:
            logger.warning("F099: the Telegram push of row %s failed (%s)", row.id.hex[:8], type(exc).__name__)
            return TRANSIENT, None
        status = response.status_code
        if status in REFUSED_STATUSES:
            logger.warning(
                "F099: the Telegram push of row %s was refused (HTTP %s); it is not retried", row.id.hex[:8], status
            )
            return REFUSED, None
        if status >= 400:
            logger.warning("F099: the Telegram push of row %s failed (HTTP %s); it stays due", row.id.hex[:8], status)
            return TRANSIENT, None
        try:
            return SENT, int(response.json()["result"]["message_id"])
        except Exception:
            return SENT, None  # sent, but the id could not be read: still stamped, so it is not sent twice
