"""F099 Phase 2d-1: propose_action stages a call for the owner and runs nothing."""

from __future__ import annotations

import json
import uuid

import pytest
from f099_support import (
    CONT,
    SEND_EMAIL_ARGS,
    SEND_EMAIL_SCHEMA,
    claimed,
    env_factory,  # noqa: F401
    make_root,
    proposal_row,
    record,
    register_send_email,
    runner_env,  # noqa: F401
    stage,
    use,
)
from sqlalchemy import select, text
from test_tool_classes import _registered_names

from nous.api import tool_policy
from nous.api.execution_context import ExecutionContext
from nous.api.runner import TERMINAL_EXTRA_TOOLS
from nous.api.tool_classes import tool_class
from nous.api.tools import ToolDispatcher
from nous.brain import continuation
from nous.handlers.continuation_runner import (
    PROPOSE_ACTION_SCHEMA,
    ArrivalState,
    ContinuationRunner,
    make_propose_action_executor,
    make_resolve_intention_executor,
)
from nous.storage.models import IntentionProposal


async def _noop(**kwargs):
    return {"content": [{"type": "text", "text": "ok"}]}


def _ctx(**over) -> ExecutionContext:
    base = {
        "kind": "continuation",
        "session_id": "intent-x",
        "authority": "internal_only",
        "intention_id": uuid.uuid4(),
        "root_intention_id": uuid.uuid4(),
        "claim_token": uuid.uuid4(),
    }
    return ExecutionContext(**{**base, **over})


def _dispatcher(*names: str) -> ToolDispatcher:
    dispatcher = ToolDispatcher()
    for name in names:
        dispatcher.register(name, _noop, SEND_EMAIL_SCHEMA if name == "send_email" else {"type": "object"})
    return dispatcher


def _propose(
    *,
    spawn_blocked: bool = False,
    stage_error: Exception | None = None,
    proposal_id=None,
    lineage_shell: bool = False,
    workspace_dir: str = "/tmp/nous-workspace",
):
    """The executor over a fake store: ``(executor, state, staged calls)``."""
    staged: list[tuple[str, dict, str]] = []

    async def fake_stage(tool, arguments, rationale):
        if stage_error is not None:
            raise stage_error
        staged.append((tool, arguments, rationale))
        return proposal_id or uuid.uuid4()

    dispatcher = _dispatcher(
        "send_email",
        "bash",
        "schedule_task",
        "write_file",
        "web_fetch",
        "spawn_task",
        "dag_create",
        "spawn_sync",
        "heartbeat_check_create",
    )
    state = ArrivalState()
    executor = make_propose_action_executor(
        state,
        ctx=_ctx(spawn_blocked=spawn_blocked),
        dispatcher=dispatcher,
        stage=fake_stage,
        lineage_shell=lineage_shell,
        workspace_dir=workspace_dir,
    )
    return executor, state, staged


# ---- the tool's place in the tables --------------------------------------------------------------------------


def test_propose_action_is_a_classified_internal_only_extra_tool_and_not_terminal():
    assert tool_class("propose_action").side_effect == "write"
    assert "propose_action" in tool_policy.INTERNAL_ONLY_EXTRA_TOOLS
    assert "propose_action" not in TERMINAL_EXTRA_TOOLS  # PIN: a terminal name would end the loop
    assert "propose_action" not in _registered_names()  # PIN: a per-turn extra tool, never registered
    assert PROPOSE_ACTION_SCHEMA["name"] == "propose_action"
    assert PROPOSE_ACTION_SCHEMA["input_schema"]["required"] == ["tool", "arguments", "rationale"]
    for ctx in (_ctx(), _ctx(kind="subtask")):
        assert tool_policy.internal_only_allowed("propose_action", ctx=ctx) is False


# ---- validate_call -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"to": "a@example.com", "subject": "s"}, "missing required argument 'body'"),
        ({"to": ["a@example.com"], "subject": "s", "body": "b"}, "to must be string"),
        ({"to": "a@example.com", "subject": "s", "body": "b", "_session_id": "x"}, "reserved"),
        ("not an object", "JSON object"),
    ],
)
def test_validate_call_names_what_is_wrong(args, needle):
    problems = _dispatcher("send_email").validate_call("send_email", args)
    assert any(needle in problem for problem in problems), problems


def test_validate_call_accepts_a_well_formed_call_and_rejects_an_unknown_tool():
    dispatcher = _dispatcher("send_email")
    assert dispatcher.validate_call("send_email", dict(SEND_EMAIL_ARGS)) == []
    assert dispatcher.validate_call("nope", {}) == ["nope is not a registered tool"]


# ---- render_arguments ----------------------------------------------------------------------------------------


def test_render_arguments_is_indented_json_in_the_mappings_key_order():
    shown = continuation.render_arguments({"to": "a@example.com", "body": "Hi"})
    assert shown == '{\n  "to": "a@example.com",\n  "body": "Hi"\n}'


def test_render_arguments_shows_bidi_and_zero_width_characters_as_escapes():
    shown = continuation.render_arguments({"body": "pay \u202eevil\u202c\u200b now \x85"})
    assert "\u202e" not in shown and "\u200b" not in shown and "\x85" not in shown
    assert "\\u202e" in shown and "\\u200b" in shown and "\\u0085" in shown


def test_render_arguments_escapes_every_invisible_or_format_character():
    """A category predicate, not a list: the Arabic letter mark (a bidi control), the tag block (invisible text
    a downstream reader still sees), a variation selector. Above U+FFFF the escape has eight hex digits."""
    shown = continuation.render_arguments({"cmd": "rm a\u061cb", "tag": "x\U000e0041y", "vs": "z\ufe0f"})
    assert "\u061c" not in shown and "\U000e0041" not in shown and "\ufe0f" not in shown
    assert "\\u061c" in shown and "\\U000e0041" in shown and "\\ufe0f" in shown
    assert (
        continuation.render_arguments({"t": "caf\u00e9 \U0001f600"}) == '{\n  "t": "caf\u00e9 \U0001f600"\n}'
    )  # visible text stays


# ---- the executor --------------------------------------------------------------------------------------------


async def test_a_valid_proposal_is_staged_and_remembered_for_the_turn():
    executor, state, staged = _propose()
    text, is_error = await executor(tool="send_email", arguments=dict(SEND_EMAIL_ARGS), rationale="They asked.")
    assert is_error is False and "resolve_intention" in text and "ask" in text
    assert staged == [("send_email", SEND_EMAIL_ARGS, "They asked.")]  # exactly the arguments the model sent
    assert len(state.proposals) == 1 and state.proposals[0].hex[:8] in text


@pytest.mark.parametrize("tool", ["bash", "schedule_task"])
async def test_a_denylisted_local_tool_may_be_proposed(tool):
    executor, state, staged = _propose()
    _text, is_error = await executor(tool=tool, arguments={"command": "ls"}, rationale="They asked.")
    assert is_error is False and staged and state.proposals


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "gh pr merge 719",
        "curl -X POST https://example.com",
        "rm -rf /tmp/x",
        "{R} launch nous p; curl -X POST https://example.com",
    ],
)
async def test_with_the_lineage_shell_a_bash_command_the_allowlist_refuses_may_still_be_proposed(command, tmp_path):
    executor, state, staged = _propose(lineage_shell=True, workspace_dir=str(tmp_path))
    command = command.format(R=f"{tmp_path}/claude-jobs/runner.sh")
    _text, is_error = await executor(tool="bash", arguments={"command": command}, rationale="They asked.")
    assert is_error is False and staged and state.proposals


@pytest.mark.parametrize("command", ["ls", "gh pr view 719", "{R} status"])
async def test_with_the_lineage_shell_an_allowed_bash_command_is_not_a_proposal(command, tmp_path):
    executor, state, staged = _propose(lineage_shell=True, workspace_dir=str(tmp_path))
    command = command.format(R=f"{tmp_path}/claude-jobs/runner.sh")
    text, is_error = await executor(tool="bash", arguments={"command": command}, rationale="r")
    assert is_error is True and "call it yourself" in text
    assert staged == [] and state.proposals == []


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"tool": "no_such_tool", "arguments": {}, "rationale": "r"}, "not a registered tool"),
        ({"tool": "write_file", "arguments": {"path": "a"}, "rationale": "r"}, "call it yourself"),
        ({"tool": "web_fetch", "arguments": {"url": "https://example.com"}, "rationale": "r"}, "call it yourself"),
        ({"tool": "send_email", "arguments": "x", "rationale": "r"}, "arguments must be"),
        ({"tool": "send_email", "arguments": dict(SEND_EMAIL_ARGS), "rationale": "  "}, "rationale is required"),
        ({"tool": "", "arguments": {}, "rationale": "r"}, "tool is required"),
        ({"tool": "send_email", "arguments": {"to": "a@example.com"}, "rationale": "r"}, "missing required"),
    ],
)
async def test_a_proposal_that_cannot_be_staged_is_an_error_the_model_can_read(kwargs, needle):
    executor, state, staged = _propose()
    text, is_error = await executor(**kwargs)
    assert is_error is True and needle in text, text
    assert staged == [] and state.proposals == []


async def test_a_model_sent_underscore_argument_is_refused_at_staging():
    """Dispatch drops a `_`-prefixed argument silently at run time: the owner would approve a call that runs
    differently from the one shown."""
    executor, _state, staged = _propose()
    args = {**SEND_EMAIL_ARGS, "_session_id": "intent-x"}
    text, is_error = await executor(tool="send_email", arguments=args, rationale="r")
    assert is_error is True and "reserved" in text and staged == []


@pytest.mark.parametrize("spawn_blocked", [False, True])
@pytest.mark.parametrize("tool", ["spawn_task", "dag_create", "spawn_sync", "heartbeat_check_create"])
async def test_spawn_tools_cannot_be_proposed(tool, spawn_blocked):
    """A turn at its depth or spawn limit has them removed; proposing one would route around the limit.
    spawn_sync is an inline model run under the approving request, and an approved heartbeat check is not
    origin-aware, so nothing would make it internal_only. schedule_task stays proposable (see above)."""
    executor, _state, staged = _propose(spawn_blocked=spawn_blocked)
    text, is_error = await executor(tool=tool, arguments={}, rationale="r")
    assert is_error is True and "cannot be proposed" in text and staged == []


async def test_a_refusal_from_the_store_is_returned_as_an_error():
    executor, state, _staged = _propose(stage_error=continuation.ProposalRefused("too many proposals"))
    text, is_error = await executor(tool="send_email", arguments=dict(SEND_EMAIL_ARGS), rationale="r")
    assert (text, is_error) == ("Error: too many proposals", True) and state.proposals == []


async def test_the_same_proposal_staged_twice_is_remembered_once():
    """The store answers the same id for the same call (see the real-store test below): the turn remembers it once."""
    fixed = uuid.uuid4()
    executor, state, _staged = _propose(proposal_id=fixed)
    for _ in range(2):
        _text, is_error = await executor(tool="send_email", arguments=dict(SEND_EMAIL_ARGS), rationale="r")
        assert is_error is False
    assert state.proposals == [fixed]


async def test_a_turn_that_proposed_may_only_ask():
    """The existing resolve_intention executor enforces it from ArrivalState.proposals; pinned against the
    real tool, because 2d is what makes the list non-empty."""
    executor, state, _staged = _propose()
    await executor(tool="send_email", arguments=dict(SEND_EMAIL_ARGS), rationale="r")

    async def limits():
        return continuation.RootLimits(0, 0, 0, 0, 0, False, None)

    async def open_work():
        return True

    resolve = make_resolve_intention_executor(state, limits_of=limits, open_work_of=open_work)
    text, is_error = await resolve(decision="continue", note="n", progress=True, confidence=0.5)
    assert is_error is True and "ask" in text and state.resolution is None
    text, is_error = await resolve(decision="ask", note="May I?", progress=False, confidence=0.5)
    assert (text, is_error) == ("Recorded.", False)


# ---- the store -----------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_staged_proposal_carries_the_claim_token_and_nothing_else(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    root, got = await claimed(env)
    proposal_id = await stage(env, got)
    row = await proposal_row(env, proposal_id)
    assert row.state == "staged" and row.claim_token == got.claim_token
    assert (row.intention_id, row.root_id) == (got.deepest.id, root.id)
    assert row.arguments == SEND_EMAIL_ARGS and row.tool == "send_email"
    assert (row.arrival_id, row.deadline, row.decided_at, row.executed_at, row.ledger_key) == (None,) * 5


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("arguments", "rationale", "needle"),
    [
        ({**SEND_EMAIL_ARGS, "body": "x" * 3000}, "r", "the owner reads"),
        pytest.param({**SEND_EMAIL_ARGS, "body": "\U0001f600" * 1500}, "r", "the owner reads", id="astral-call"),
        pytest.param(SEND_EMAIL_ARGS, "\U0001f600" * 600, "rationale is too long", id="astral-rationale"),
        (SEND_EMAIL_ARGS, "y" * 1001, "rationale is too long"),
        (SEND_EMAIL_ARGS, "   ", "rationale is required"),
        ({**SEND_EMAIL_ARGS, "body": "a\x00b"}, "r", "arguments may not contain a NUL"),
        ({**SEND_EMAIL_ARGS, "cc\x00": "x"}, "r", "arguments may not contain a NUL"),
        ({**SEND_EMAIL_ARGS, "nested": ["ok", {"k": "a\x00b"}]}, "r", "arguments may not contain a NUL"),
        (SEND_EMAIL_ARGS, "why\x00", "rationale may not contain a NUL"),
    ],
)
async def test_a_call_the_owner_cannot_read_in_full_is_not_staged(env_factory, arguments, rationale, needle):  # noqa: F811
    """What the owner approves must fit one message whole: an oversize call is refused, never clipped. A NUL
    character (which jsonb and text refuse) is a refusal the model reads, not a database error that fails the turn."""
    env = await env_factory(**CONT)
    _root, got = await claimed(env)
    with pytest.raises(continuation.ProposalRefused, match=needle):
        await stage(env, got, arguments=arguments, rationale=rationale)
    async with env.db.session() as s:
        assert (
            await s.execute(select(IntentionProposal).where(IntentionProposal.agent_id == env.agent))
        ).first() is None


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("arguments", "rationale", "needle"),
    [
        pytest.param(
            {**SEND_EMAIL_ARGS, "body": "a\ud800b"}, "r", "arguments may not contain a lone surrogate", id="value"
        ),
        pytest.param({**SEND_EMAIL_ARGS, "cc\udc00": "x"}, "r", "arguments may not contain a lone surrogate", id="key"),
        pytest.param(
            {**SEND_EMAIL_ARGS, "nested": ["ok", {"k": "\udfff"}]},
            "r",
            "arguments may not contain a lone surrogate",
            id="nested",
        ),
        pytest.param(SEND_EMAIL_ARGS, "why\ud83d", "rationale may not contain a lone surrogate", id="rationale"),
    ],
)
async def test_a_lone_surrogate_is_a_refusal_the_model_reads(env_factory, arguments, rationale, needle):  # noqa: F811
    """Addendum (2d-2): an unpaired UTF-16 half (category Cs) cannot be stored (jsonb and the UTF-8 wire refuse it), so
    it is refused at staging the way a NUL is, instead of failing the whole turn with a database error."""
    env = await env_factory(**CONT)
    _root, got = await claimed(env)
    with pytest.raises(continuation.ProposalRefused, match=needle):
        await stage(env, got, arguments=arguments, rationale=rationale)
    async with env.db.session() as s:
        assert (
            await s.execute(select(IntentionProposal).where(IntentionProposal.agent_id == env.agent))
        ).first() is None


@pytest.mark.postgres_only
async def test_staging_on_a_claim_that_is_no_longer_live_is_refused(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _root, got = await claimed(env)
    async with env.db.session() as s:
        assert await continuation.release_claim(s, env.agent, got) == 1  # the lease was released under the turn
        await s.commit()
    with pytest.raises(continuation.ProposalRefused, match="no longer live"):
        await stage(env, got)


@pytest.mark.postgres_only
async def test_the_same_call_twice_is_one_row_and_a_sixth_is_refused(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _root, got = await claimed(env)
    first = await stage(env, got)
    assert await stage(env, got) == first  # idempotent: the model retried its own call
    for n in range(continuation.MAX_PROPOSALS_PER_ARRIVAL - 1):
        await stage(env, got, arguments={**SEND_EMAIL_ARGS, "subject": f"Snow {n}"})
    with pytest.raises(continuation.ProposalRefused, match="at most"):
        await stage(env, got, arguments={**SEND_EMAIL_ARGS, "subject": "one too many"})


# ---- lead addendum 2 (2d-6 review I1): the cap measures the call as jsonb stores it -----------------------------


@pytest.mark.postgres_only
async def test_the_call_cap_measures_the_arguments_as_jsonb_stores_them(env_factory):  # noqa: F811
    """jsonb writes ``1e300`` as 301 digits, and the push renders the stored mapping: 90 such values fit the cap
    as Python renders them and would reach the owner as a 28k message Telegram refuses, so the proposal would
    expire unseen. The cap measures the stored form, so the call is refused at staging, where the model reads it."""
    env = await env_factory(**CONT)
    _root, got = await claimed(env)
    arguments = {**SEND_EMAIL_ARGS, **{f"k{n}": 1e300 for n in range(90)}}
    assert continuation.utf16_units(continuation.render_arguments(arguments)) <= continuation.PROPOSAL_ARGS_MAX_CHARS
    with pytest.raises(continuation.ProposalRefused, match="the owner reads"):
        await stage(env, got, arguments=arguments)
    async with env.db.session() as s:
        assert (
            await s.execute(select(IntentionProposal).where(IntentionProposal.agent_id == env.agent))
        ).first() is None


@pytest.mark.postgres_only
async def test_a_staged_call_is_stored_as_its_jsonb_round_trip_and_dedupes_on_it(env_factory):  # noqa: F811
    """What is measured is what is stored and what runs: the stored arguments render exactly as jsonb renders
    them, and the model retrying the same call is still one row (the dedupe compares the stored form)."""
    env = await env_factory(**CONT)
    _root, got = await claimed(env)
    arguments = {**SEND_EMAIL_ARGS, "big": 1e300, "small": 2.5, "whole": 1.5e3}
    first = await stage(env, got, arguments=arguments)
    async with env.db.session() as s:
        trip = (
            await s.execute(text("SELECT CAST(CAST(:j AS jsonb) AS text)"), {"j": json.dumps(arguments)})
        ).scalar_one()
    stored = (await proposal_row(env, first)).arguments
    assert continuation.render_arguments(stored) == continuation.render_arguments(json.loads(trip))
    assert await stage(env, got, arguments=arguments) == first


@pytest.mark.postgres_only
async def test_a_non_finite_number_is_a_refusal_the_model_reads(env_factory):  # noqa: F811
    """jsonb has no NaN or Infinity: refused as not plain JSON, never a database error that fails the turn."""
    env = await env_factory(**CONT)
    _root, got = await claimed(env)
    with pytest.raises(continuation.ProposalRefused, match="plain JSON"):
        await stage(env, got, arguments={**SEND_EMAIL_ARGS, "n": float("nan")})


@pytest.mark.postgres_only
async def test_expire_staged_touches_only_this_claims_staged_rows(env_factory):  # noqa: F811
    env = await env_factory(**CONT)
    _root, got = await claimed(env)
    mine = await stage(env, got)
    _other_root, other = await claimed(env)
    theirs = await stage(env, other)
    async with env.db.session() as s:
        assert await continuation.expire_staged(s, env.agent, claim_token=got.claim_token) == 1
        assert await continuation.expire_staged(s, env.agent, claim_token=got.claim_token) == 0  # idempotent
        await s.commit()
    assert (await proposal_row(env, mine)).state == "expired"
    assert (await proposal_row(env, theirs)).state == "staged"


# ---- through a real turn -------------------------------------------------------------------------------------


def _cont(env, *, dispatcher=True) -> ContinuationRunner:
    env.cont = ContinuationRunner(
        database=env.db,
        settings=env.settings,
        runner=env.runner,
        heart=env.heart,
        brain=env.brain,
        bus=env.bus,
        dispatcher=env.dispatcher if dispatcher else None,
    )
    return env.cont


def _ask(note="May I email the report?"):
    return use("resolve_intention", decision="ask", note=note, progress=False, confidence=0.7)


@pytest.mark.postgres_only
async def test_propose_action_is_offered_runs_nothing_and_does_not_end_the_turn(runner_env):  # noqa: F811
    env = await runner_env(
        [use("propose_action", tool="send_email", arguments=SEND_EMAIL_ARGS, rationale="The owner wants it.")],
        [_ask()],
    )
    sent = register_send_email(env)
    root = await make_root(env)
    await record(env, root)
    done = await _cont(env).run_arrival(root.id)
    assert done is not None and len(env.model.calls) == 2  # propose_action did not end the loop
    offered = {tool["name"] for tool in env.model.calls[0]["tools"]}
    assert {"propose_action", "resolve_intention"} <= offered and "send_email" not in offered
    assert sent == []  # nothing ran
    async with env.db.session() as s:
        (row,) = (await s.execute(select(IntentionProposal).where(IntentionProposal.agent_id == env.agent))).scalars()
    assert (row.tool, row.arguments, row.rationale) == ("send_email", SEND_EMAIL_ARGS, "The owner wants it.")
    assert row.claim_token is not None


@pytest.mark.postgres_only
async def test_a_runner_without_a_dispatcher_does_not_offer_propose_action(runner_env):  # noqa: F811
    """It cannot validate a call without the dispatcher, so it does not offer the tool (conflict C16)."""
    env = await runner_env([_ask("Shall I?")])
    root = await make_root(env)
    await record(env, root)
    await _cont(env, dispatcher=False).run_arrival(root.id)
    assert "propose_action" not in {tool["name"] for tool in env.model.calls[0]["tools"]}
