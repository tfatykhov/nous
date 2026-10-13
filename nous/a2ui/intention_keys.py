"""F099 Phase 3a: the reserved dedup keys of the owner's intention cards.

A leaf module (standard library only, like ``nous/dag/approval.py``), so ``nous.a2ui.service`` can import the prefix
without a cycle. A card under this prefix is built by the server for the owner and answers the row its key names:
``SurfaceService.push_built`` refuses the prefix unless the caller passes ``reserved_key_ok=True``, and every
``intention.*`` action handler takes its subject from ``parse_key(surface.dedup_key)``, never from the client.
"""

from __future__ import annotations

from uuid import UUID

INTENTION_DEDUP_PREFIX = "intention:"
KIND_PROPOSAL, KIND_QUESTION, KIND_ACTIVE = "proposal", "question", "active"
ACTIVE_KEY = f"{INTENTION_DEDUP_PREFIX}{KIND_ACTIVE}"


def proposal_key(proposal_id: UUID) -> str:
    return f"{INTENTION_DEDUP_PREFIX}{KIND_PROPOSAL}:{proposal_id}"


def question_key(question_id: UUID) -> str:
    return f"{INTENTION_DEDUP_PREFIX}{KIND_QUESTION}:{question_id}"


def parse_key(dedup_key: object) -> tuple[str, UUID | None] | None:
    """``(kind, id)`` of one of our keys, ``("active", None)`` for the active-intentions card, else None: anything
    that is not exactly a key this module builds (a malformed or non-canonical id is not ours)."""
    if not isinstance(dedup_key, str) or not dedup_key.startswith(INTENTION_DEDUP_PREFIX):
        return None
    rest = dedup_key[len(INTENTION_DEDUP_PREFIX) :]
    if rest == KIND_ACTIVE:
        return (KIND_ACTIVE, None)
    kind, sep, raw = rest.partition(":")
    if not sep or kind not in (KIND_PROPOSAL, KIND_QUESTION):
        return None
    try:
        parsed = UUID(raw)
    except ValueError:
        return None
    return (kind, parsed) if str(parsed) == raw else None
