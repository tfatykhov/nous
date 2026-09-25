"""Harness Phase 2b: an idempotency key names the logical send, not its wording."""

import uuid

from nous.api.execution_context import ExecutionContext
from nous.api.idempotency import idempotency_key, is_keyed_tool

DAG = uuid.uuid4()
EMAIL = {"to": "Bob@X.io, alice@x.io", "subject": " Premarket ", "body": "b"}


def _node(**kw):
    return ExecutionContext(kind="dag_node", dag_id=DAG, dag_node_name="send", **kw)


def test_only_sends_are_keyed():
    assert is_keyed_tool("send_email") and is_keyed_tool("send_file")
    assert not is_keyed_tool("bash") and not is_keyed_tool("write_file")
    assert not is_keyed_tool("brand_new_tool")


def test_a_dag_node_key_survives_relaunch_and_rewording():
    first = idempotency_key(_node(subtask_id=uuid.uuid4(), session_id="subtask-aaaa"), "send_email", EMAIL)
    again = idempotency_key(_node(subtask_id=uuid.uuid4(), session_id="subtask-bbbb"), "send_email",
                            dict(EMAIL, subject="Premarket brief (retry)", body="reworded"))
    assert first == again and first.startswith(f"dag:{DAG}:send:")


def test_recipients_are_canonical():
    reordered = dict(EMAIL, to=["ALICE@x.io", "bob@x.io"])
    assert idempotency_key(_node(), "send_email", EMAIL) == idempotency_key(_node(), "send_email", reordered)


def test_different_recipients_are_different_sends():
    assert idempotency_key(_node(), "send_email", EMAIL) != idempotency_key(
        _node(), "send_email", dict(EMAIL, to="carol@x.io"))
    assert idempotency_key(_node(), "send_email", EMAIL) != idempotency_key(
        _node(), "send_email", dict(EMAIL, cc="dave@x.io"))


def test_a_label_makes_an_intentional_second_message():
    assert idempotency_key(_node(), "send_email", EMAIL) != idempotency_key(
        _node(), "send_email", dict(EMAIL, send_label="followup"))


def test_the_summary_turn_and_its_children_share_a_scope():
    session = f"dag-summary-{DAG.hex}-g1"
    summary = ExecutionContext(kind="dag_summary", dag_id=DAG, session_id=session)
    child = ExecutionContext(kind="subtask", subtask_id=uuid.uuid4(), parent_session_id=session)
    assert idempotency_key(summary, "send_email", EMAIL) == idempotency_key(child, "send_email", EMAIL)


def test_a_new_delivery_generation_is_a_new_announcement():
    g1 = ExecutionContext(kind="dag_summary", dag_id=DAG, session_id=f"dag-summary-{DAG.hex}-g1")
    g2 = ExecutionContext(kind="dag_summary", dag_id=DAG, session_id=f"dag-summary-{DAG.hex}-g2")
    assert idempotency_key(g1, "send_email", EMAIL) != idempotency_key(g2, "send_email", EMAIL)


def test_a_callback_retry_shares_its_run_scope():
    a = ExecutionContext(kind="heartbeat_callback", check_name="c", run_id="r1", session_id="hb-1")
    b = ExecutionContext(kind="heartbeat_callback", check_name="c", run_id="r1", session_id="hb-2")
    assert idempotency_key(a, "send_email", EMAIL) == idempotency_key(b, "send_email", EMAIL)
    c = ExecutionContext(kind="heartbeat_callback", check_name="c", run_id="r2", session_id="hb-3")
    assert idempotency_key(a, "send_email", EMAIL) != idempotency_key(c, "send_email", EMAIL)


def test_a_plain_subtask_is_keyed_by_its_row():
    sid = uuid.uuid4()
    for kind in ("subtask", "scheduled", "agent_action"):
        assert idempotency_key(ExecutionContext(kind=kind, subtask_id=sid), "send_email", EMAIL).startswith(
            f"subtask:{sid}:")


def test_foreground_checks_and_background_are_unkeyed():
    for kind in ("interactive", "mcp", "heartbeat_triage", "heartbeat_check", "background"):
        assert idempotency_key(ExecutionContext(kind=kind), "send_email", EMAIL) is None


def test_a_context_without_its_scope_ids_is_unkeyed():
    assert idempotency_key(ExecutionContext(kind="dag_node"), "send_email", EMAIL) is None
    assert idempotency_key(ExecutionContext(kind="subtask"), "send_email", EMAIL) is None
    assert idempotency_key(ExecutionContext(kind="heartbeat_callback", check_name="c"), "send_email", EMAIL) is None


def test_send_file_key_uses_the_resolved_chat_and_file_name():
    implicit = idempotency_key(_node(), "send_file", {"file_path": "/tmp/r.png"}, default_chat_id="123")
    explicit = idempotency_key(_node(), "send_file", {"file_path": "/tmp/r.png", "chat_id": "123"},
                               default_chat_id="123")
    regenerated = idempotency_key(_node(), "send_file", {"file_path": "/tmp/r.png", "caption": "new"},
                                  default_chat_id="123")
    assert implicit == explicit == regenerated
    other_chat = idempotency_key(_node(), "send_file", {"file_path": "/tmp/r.png", "chat_id": "999"},
                                 default_chat_id="123")
    assert other_chat != implicit


# --- after the verify-by-execution review ----------------------------------------


def test_the_key_reads_recipients_exactly_as_the_send_does():
    """One definition: the handler's normalization. A list element holding a
    comma is two recipients to the handler, so it is two to the key too."""
    from nous.api.email_tools import _normalize_recipients
    from nous.api.idempotency import normalize_recipients

    assert _normalize_recipients is normalize_recipients
    as_list = idempotency_key(_node(), "send_email", dict(EMAIL, to=["tim@example.com, alice@example.com"]))
    as_string = idempotency_key(_node(), "send_email", dict(EMAIL, to="tim@example.com, alice@example.com"))
    split = idempotency_key(_node(), "send_email", dict(EMAIL, to=["tim@example.com", "alice@example.com"]))
    assert as_list == as_string == split
    assert idempotency_key(_node(), "send_email", dict(EMAIL, cc=["b@x.io, c@x.io"])) == idempotency_key(
        _node(), "send_email", dict(EMAIL, cc="b@x.io, c@x.io"))


def test_a_semicolon_is_not_a_separator_to_either():
    from nous.api.idempotency import normalize_recipients

    assert normalize_recipients("a@x.io;b@x.io") == ["a@x.io;b@x.io"]


def test_fields_are_encoded_unambiguously():
    """Codex r2: joining fields with a delimiter let two different sends share
    material when a field contains the delimiter."""
    a = idempotency_key(_node(), "send_file", {"file_path": "/tmp/report|draft.pdf", "send_label": "final"},
                        default_chat_id="123")
    b = idempotency_key(_node(), "send_file", {"file_path": "/tmp/report", "send_label": "draft.pdf|final"},
                        default_chat_id="123")
    assert a != b
    c = idempotency_key(_node(), "send_email", dict(EMAIL, to="a@x.io", send_label="x|y"))
    d = idempotency_key(_node(), "send_email", dict(EMAIL, to="a@x.io|x", send_label="y"))
    assert c != d
