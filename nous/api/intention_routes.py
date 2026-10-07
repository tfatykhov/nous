"""F099 Phase 2d: the owner's deterministic actions over REST (spec 4.4 items 3 to 6, contract section 4.10).

Approve, reject and answer are one function each on the continuation runner (``decide_proposal``,
``answer_question``); these routes only resolve an id, call it, and map the result to a status. The Telegram bot
calls these routes, and the A2UI cards of Phase 3 will call the same two runner functions: no owner action has any
semantics of its own here. No agent tool reaches them. They have the existing no-auth LAN posture of ``rest.py``
(spec section 9): there is no in-app authentication, and the gate is the network and the bot's owner-chat check.

The id of a route is looked up BEFORE the runner is needed: an id that names nothing is a 404 whatever is wired,
and a row with no runner is a 503, so a deployment with continuation off (no rows, no runner) answers 404.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nous.brain import continuation
from nous.owner_actions import ANSWER_REFUSALS, DECISION_REFUSALS  # the one refusal vocabulary (fixed sentences)

logger = logging.getLogger(__name__)

ACTOR_MAX_CHARS = 100
ANSWER_MAX_CHARS = 8000  # the owner's text; record_answer clips it again to the inbox body cap
LIST_LIMIT_MAX = 100
NOT_RUNNING = "continuation is not running"


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


async def _object_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _actor(body: dict[str, Any]) -> str:
    """Who decided, as data: printable characters only, clipped; ``rest`` when blank."""
    raw = str(body.get("actor") or "")
    cleaned = "".join(ch for ch in raw if ch.isprintable())[:ACTOR_MAX_CHARS].strip()
    return cleaned or "rest"


def _is_int(value: Any) -> bool:
    """A JSON integer that fits a signed int64: a larger one would fail asyncpg's BIGINT bind (a 500, not a 400)."""
    return isinstance(value, int) and not isinstance(value, bool) and -(1 << 63) <= value < 1 << 63


def build_intention_routes(*, database: Any, settings: Any, continuation_runner: Any) -> list[Route]:
    """The four owner-action routes. ``continuation_runner`` may be None, or a proxy that is falsy until the
    component exists: it is read per request."""
    agent_id = settings.agent_id

    async def list_proposals(request: Request) -> JSONResponse:
        """GET /intentions/proposals?state=pending&limit=20"""
        state = request.query_params.get("state", "pending")
        raw_limit = request.query_params.get("limit", "20")
        # isascii: str.isdigit() also accepts Unicode digits such as a superscript two, which int() rejects.
        if not (raw_limit.isascii() and raw_limit.isdigit()) or not 1 <= int(raw_limit) <= LIST_LIMIT_MAX:
            return _error(400, f"limit must be a whole number from 1 to {LIST_LIMIT_MAX}")
        try:
            async with database.session() as session:
                views = await continuation.list_proposals(session, agent_id, state=state, limit=int(raw_limit))
        except ValueError:
            return _error(400, "state must be a proposal state, 'open' or 'all'")
        return JSONResponse({"proposals": views})

    async def decide(request: Request) -> JSONResponse:
        """POST /intentions/proposals/{id}/decide

        The 200 body carries the call's raw ``result`` and ``error``: a tool's output, which a model or an injected
        result may have shaped. Any surface that renders them (the Phase 3 A2UI cards) must escape them; the bot
        never renders them."""
        body = await _object_body(request)
        if body is None:
            return _error(400, "the body must be a JSON object")
        decision = body.get("decision")
        if decision not in ("approve", "reject"):
            return _error(400, "decision must be 'approve' or 'reject'")
        shape = continuation.normalize_id(request.path_params["id"])
        if shape is None:
            return _error(400, "id must be 8 to 32 hex characters")
        try:
            async with database.session() as session:
                proposal_id = await continuation.find_proposal_id(session, agent_id, shape)
        except continuation.AmbiguousId:
            return _error(400, "that id matches more than one proposal: use more characters")
        if proposal_id is None:
            return _error(404, "no such proposal")
        if not continuation_runner:
            return _error(503, NOT_RUNNING)
        try:
            outcome = await continuation_runner.decide_proposal(
                proposal_id, approve=decision == "approve", actor=_actor(body)
            )
        except continuation.ProposalNotFound:
            return _error(404, "no such proposal")
        except Exception:
            logger.exception("F099: deciding proposal %s failed", proposal_id.hex[:8])
            return _error(500, "the decision could not be processed")
        if outcome.refusal is not None:
            return _error(
                409,
                DECISION_REFUSALS.get(outcome.refusal, DECISION_REFUSALS[continuation.REFUSE_STATE]),
                state=outcome.state,
                refusal=outcome.refusal,
            )
        return JSONResponse(
            {
                "proposal_id": str(outcome.proposal_id),
                "short_id": continuation.short_id(outcome.proposal_id),
                "state": outcome.state,
                "result": outcome.result,
                "error": outcome.error,
                "changed": outcome.changed,
                "woke": outcome.woke_arrival,
            }
        )

    async def _record(question_id: UUID, text: str, actor: str) -> JSONResponse:
        if not continuation_runner:
            return _error(503, NOT_RUNNING)
        try:
            recorded = await continuation_runner.answer_question(question_id, text=text, actor=actor)
        except continuation.QuestionNotFound:
            return _error(404, "no such question")
        except continuation.AnswerRefused as refused:
            return _error(
                409, ANSWER_REFUSALS.get(refused.reason, "The answer was not recorded."), reason=refused.reason
            )
        except Exception:
            logger.exception("F099: answering question %s failed", question_id.hex[:8])
            return _error(500, "the answer could not be processed")
        return JSONResponse(
            {
                "question_id": str(recorded.question_id),
                "arrival_id": str(recorded.arrival_id),
                "woke": recorded.woke_arrival,
            }
        )

    def _text_of(body: dict[str, Any]) -> str | None:
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > ANSWER_MAX_CHARS:
            return None
        # Postgres refuses a NUL and the UTF-8 encode a lone surrogate: refused here, not a 500 from the store.
        if continuation._has_nul(text) or continuation._has_surrogate(text):
            return None
        return text

    async def answer(request: Request) -> JSONResponse:
        """POST /intentions/questions/{id}/answer"""
        body = await _object_body(request)
        if body is None:
            return _error(400, "the body must be a JSON object")
        text = _text_of(body)
        if text is None:
            return _error(400, f"text is required (at most {ANSWER_MAX_CHARS} characters)")
        shape = continuation.normalize_id(request.path_params["id"])
        if shape is None:
            return _error(400, "id must be 8 to 32 hex characters")
        try:
            async with database.session() as session:
                question_id = await continuation.find_question_id(session, agent_id, shape)
        except continuation.AmbiguousId:
            return _error(400, "that id matches more than one question: use more characters")
        if question_id is None:
            return _error(404, "no such question")
        return await _record(question_id, text, _actor(body))

    async def answer_by_message(request: Request) -> JSONResponse:
        """POST /intentions/questions/answer: a Telegram reply, resolved by the message it replies to."""
        body = await _object_body(request)
        if body is None:
            return _error(400, "the body must be a JSON object")
        text = _text_of(body)
        if text is None or not _is_int(body.get("chat_id")) or not _is_int(body.get("message_id")):
            return _error(400, "chat_id and message_id (integers) and text are required")
        async with database.session() as session:
            question_id = await continuation.find_question_id_by_message(
                session, agent_id, chat_id=body["chat_id"], message_id=body["message_id"]
            )
        if question_id is None:
            return _error(404, "no question was sent as that message")
        return await _record(question_id, text, _actor(body))

    # Literal paths first: a later /intentions/{root_id} (2e) must not shadow them.
    return [
        Route("/intentions/proposals", list_proposals),
        Route("/intentions/proposals/{id}/decide", decide, methods=["POST"]),
        Route("/intentions/questions/answer", answer_by_message, methods=["POST"]),
        Route("/intentions/questions/{id}/answer", answer, methods=["POST"]),
    ]
