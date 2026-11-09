"""Execution ledger detail capture (migration 086): the redacted call input and output."""

import base64
import hashlib
import json
import os
import random
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from nous.api.execution_context import ExecutionContext
from nous.api.harness_dashboard import INPUT_PREVIEW_CHARS, get_execution_data, get_execution_detail
from nous.cognitive import ledger_store
from nous.cognitive.ledger_store import (
    DETAIL_OUTPUT_CHARS,
    SECRET_MASK,
    LedgerStore,
    detail_text,
)
from nous.storage.models import ExecutionLedgerEntry

CTX = ExecutionContext(kind="interactive")
MODES = {
    "persist": True,
    "retention_days": 90,
    "detail": True,
    "detail_retention_days": 30,
    "offered_set": "warn",
    "context_policy": "warn",
    "claim_verification": "enforce",
    "action_gating": "off",
    "events_persisted": True,
}


@pytest.fixture
def agent():
    return f"ledger-detail-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def store(db, agent):
    return LedgerStore(db, agent, detail_enabled=True)


@pytest.fixture
def plain_store(db, agent):
    return LedgerStore(db, agent)


@pytest.fixture
def own_secret(monkeypatch):
    """A secret-named env var with a unique value; the lazy cache is rebuilt around it."""
    value = "zq" + uuid.uuid4().hex
    monkeypatch.setenv("NOUS_TEST_FAKE_TOKEN", value)
    ledger_store._own_secrets.cache_clear()
    yield value
    ledger_store._own_secrets.cache_clear()


async def _row(db, entry_id):
    async with db.session() as s:
        return (await s.execute(select(ExecutionLedgerEntry).where(ExecutionLedgerEntry.id == entry_id))).scalar_one()


async def _run(
    store,
    command="touch /tmp/ledger-detail-ok",
    output="made it\n[exit code: 0]",
    tool="bash",
    tool_input=None,
    status="success",
):
    entry = await store.open_entry(context=CTX, tool_name=tool, tool_input=tool_input or {"command": command}, turn=1)
    await store.close_entry(entry, status=status, result_summary=output, output_of=tool)
    return entry


async def _age(db, entry_id, days):
    async with db.session() as s:
        await s.execute(
            update(ExecutionLedgerEntry)
            .where(ExecutionLedgerEntry.id == entry_id)
            .values(created_at=datetime.now(UTC) - timedelta(days=days))
        )
        await s.commit()


# ---- the store ----


@pytest.mark.asyncio
async def test_with_the_flag_off_nothing_is_stored_and_the_summary_says_so(plain_store, db):
    """PIN: the default behaviour."""
    entry = await _run(plain_store, output="made it\n[exit code: 0]")
    row = await _row(db, entry)
    assert row.input_text is None and row.output_text is None
    assert row.result_summary.endswith("chars of output, not stored")


@pytest.mark.asyncio
async def test_with_the_flag_on_a_bash_command_and_its_output_are_stored(store, db):
    entry = await _run(store, command="touch /tmp/ledger-detail-ok", output="made it\n[exit code: 0]")
    row = await _row(db, entry)
    assert row.input_text == "touch /tmp/ledger-detail-ok"
    assert row.output_text == "made it\n[exit code: 0]"
    assert "chars of output" in row.result_summary and "not stored" not in row.result_summary
    # the hashed key_args stay as they were
    assert row.key_args["command_len"] == str(len("touch /tmp/ledger-detail-ok"))


@pytest.mark.asyncio
async def test_run_python_stores_the_code_and_other_tools_their_sorted_json(store, db):
    py = await _run(store, tool="run_python", tool_input={"code": "open('f', 'w').write('1')\n"})
    assert (await _row(db, py)).input_text == "open('f', 'w').write('1')\n"
    wf = await _run(store, tool="write_file", tool_input={"path": "/tmp/b", "content": "é", "a": 1})
    assert (await _row(db, wf)).input_text == json.dumps(
        {"path": "/tmp/b", "content": "é", "a": 1}, ensure_ascii=False, indent=2, sort_keys=True
    )


@pytest.mark.asyncio
async def test_an_own_secret_value_is_masked_in_input_and_output(store, db, own_secret):
    entry = await _run(store, command=f"echo {own_secret} > f", output=f"wrote {own_secret} ok")
    row = await _row(db, entry)
    assert own_secret not in row.input_text and own_secret not in row.output_text
    assert SECRET_MASK in row.input_text and SECRET_MASK in row.output_text


@pytest.mark.asyncio
async def test_a_labeled_password_is_masked_by_the_pattern_redaction(store, db):
    secret = "hunter" + "2" * 10
    entry = await _run(store, command=f"touch f && curl -d password={secret} x", output=f"password={secret}")
    row = await _row(db, entry)
    assert secret not in row.input_text and secret not in row.output_text


def test_a_long_text_keeps_head_and_tail_and_masks_a_secret_across_the_cut(own_secret):
    half = DETAIL_OUTPUT_CHARS // 2
    filler = "lorem ipsum dolor sit amet\n"
    lead = ("HEAD-" + filler * 400)[: half - 10]
    text = lead + own_secret + (filler * 4000) + "-TAIL"
    assert len(text) > 100_000 and lead.__len__() + 10 == half
    out = detail_text(text, DETAIL_OUTPUT_CHARS)
    assert out.startswith("HEAD-") and out.endswith("-TAIL")
    assert "\n…[middle cut]…\n" in out
    assert DETAIL_OUTPUT_CHARS - 100 < len(out) < DETAIL_OUTPUT_CHARS + 100
    assert own_secret not in out and own_secret[:12] not in out and own_secret[-12:] not in out


@pytest.mark.asyncio
async def test_a_blocked_row_keeps_what_it_tried_to_run_and_no_output(store, db):
    await store.record_blocked(
        context=CTX, tool_name="bash", tool_input={"command": "touch /tmp/refused"}, turn=1, refused_by="action_gate"
    )
    async with db.session() as s:
        row = (
            await s.execute(select(ExecutionLedgerEntry).where(ExecutionLedgerEntry.agent_id == store._agent_id))
        ).scalar_one()
    assert row.status == "blocked" and row.input_text == "touch /tmp/refused" and row.output_text is None


@pytest.mark.asyncio
async def test_a_harness_note_close_stores_no_output(store, db):
    entry = await store.open_entry(context=CTX, tool_name="bash", tool_input={"command": "touch f"}, turn=1)
    await store.close_entry(entry, status="unknown", result_summary="cancelled mid-call")
    row = await _row(db, entry)
    assert row.output_text is None and row.result_summary == "cancelled mid-call"
    assert row.input_text == "touch f"


# ---- retention ----


@pytest.mark.asyncio
async def test_detail_retention_clears_the_texts_and_keeps_the_row(store, db):
    old = await _run(store)
    fresh = await _run(store)
    pending = await store.open_entry(context=CTX, tool_name="bash", tool_input={"command": "touch p"}, turn=1)
    await _age(db, old, 40)
    await _age(db, pending, 40)
    assert await store.prune(retention_days=90, detail_retention_days=30) == 0
    row = await _row(db, old)
    assert row.input_text is None and row.output_text is None and row.status == "success"
    assert (await _row(db, fresh)).input_text is not None
    assert (await _row(db, pending)).input_text == "touch p"


@pytest.mark.asyncio
async def test_a_tombstoned_keyed_row_loses_its_texts(store, db):
    entry = await store.open_entry(
        context=CTX,
        tool_name="send_email",
        tool_input={"to": "a@example.com", "subject": "s", "body": "b"},
        turn=1,
        idempotency_key="k-detail-1",
    )
    await store.close_entry(entry, status="success", result_summary="sent", output_of="send_email")
    assert (await _row(db, entry)).output_text == "sent"
    await _age(db, entry, 100)
    await store.prune(retention_days=90)
    row = await _row(db, entry)
    assert row.idempotency_key == "k-detail-1" and row.key_args == {}
    assert row.input_text is None and row.output_text is None


@pytest.mark.asyncio
async def test_zero_row_retention_with_a_detail_window_deletes_nothing(store, db):
    entry = await _run(store)
    await _age(db, entry, 400)
    assert await store.prune(retention_days=0, detail_retention_days=30) == 0
    row = await _row(db, entry)
    assert row.status == "success" and row.input_text is None


# ---- the dashboard ----


async def _exec(db, agent, **kw):
    async with db.session() as session:
        return await get_execution_data(session, agent, modes=MODES, **kw)


@pytest.mark.asyncio
async def test_a_list_row_carries_a_preview_and_flags_never_the_full_texts(store, db, agent):
    await _run(store, command="touch f # " + "see the log. " * 100, output="o" * 3000)
    row = (await _exec(db, agent))["rows"][0]
    assert len(row["input_preview"]) == INPUT_PREVIEW_CHARS and row["input_preview"].startswith("touch f")
    assert row["has_input"] is True and row["has_output"] is True
    assert "input_text" not in row and "output_text" not in row


@pytest.mark.asyncio
async def test_a_row_without_detail_has_no_preview(plain_store, db, agent):
    await _run(plain_store)
    row = (await _exec(db, agent))["rows"][0]
    assert row["input_preview"] is None and row["has_input"] is False and row["has_output"] is False


@pytest.mark.asyncio
async def test_the_detail_returns_both_texts_for_this_agent_only(store, db, agent):
    entry = await _run(store, command="touch f", output="the output")
    async with db.session() as session:
        got = await get_execution_detail(session, agent, str(entry))
        other = await get_execution_detail(session, agent + "-other", str(entry))
        missing = await get_execution_detail(session, agent, str(uuid.uuid4()))
        with pytest.raises(ValueError):
            await get_execution_detail(session, agent, "not-a-uuid")
    assert got == {
        "id": str(entry),
        "tool_name": "bash",
        "status": "success",
        "input_text": "touch f",
        "output_text": "the output",
    }
    assert other is None and missing is None


@pytest.mark.asyncio
async def test_search_matches_text_found_only_in_the_input_or_output(store, db, agent):
    await _run(store, command="touch /tmp/zebrafile", output="plain")
    await _run(store, command="touch /tmp/other", output="giraffe reply")
    assert [r["input_preview"] for r in (await _exec(db, agent, q="ZEBRAFILE"))["rows"]] == ["touch /tmp/zebrafile"]
    assert [r["input_preview"] for r in (await _exec(db, agent, q="giraffe"))["rows"]] == ["touch /tmp/other"]
    assert (await _exec(db, agent, q="nomatchanywhere"))["rows"] == []


# ---- review fixes ----


def _set_secret(monkeypatch, value, name="NOUS_TEST_FAKE_TOKEN"):
    monkeypatch.setenv(name, value)
    ledger_store._own_secrets.cache_clear()


@pytest.fixture(autouse=True)
def _clear_secret_cache():
    yield
    ledger_store._own_secrets.cache_clear()


def _longest_shared_run(secret: str, text: str, run: int = 8) -> str | None:
    """A run of ``run`` characters of ``secret`` that survives in ``text``, else None."""
    for i in range(len(secret) - run + 1):
        if secret[i : i + run] in text:
            return secret[i : i + run]
    return None


@pytest.mark.asyncio
async def test_an_own_secret_with_quote_backslash_and_newline_is_masked_in_every_spelling(store, db, monkeypatch):
    secret = 'pw"with\\slash\nline-' + uuid.uuid4().hex
    _set_secret(monkeypatch, secret)
    escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
    entry = await _run(
        store,
        tool="write_file",
        tool_input={"path": "/tmp/p", "content": secret},
        output=f"saw {secret} and {escaped} and {secret!r}",
    )
    row = await _row(db, entry)
    for text in (row.input_text, row.output_text):
        assert secret not in text and escaped not in text and repr(secret)[1:-1] not in text
        assert _longest_shared_run(uuid.UUID(secret[-32:]).hex, text) is None
    assert SECRET_MASK in row.input_text


def test_a_secret_longer_than_the_scan_pad_does_not_leak_at_the_cut(monkeypatch):
    secret = "".join(uuid.uuid4().hex for _ in range(10))[:300]
    _set_secret(monkeypatch, secret)
    half = DETAIL_OUTPUT_CHARS // 2
    filler = "lorem ipsum dolor sit amet\n"
    text = (filler * 400)[: half - 20] + secret + filler * 4000
    out = detail_text(text, DETAIL_OUTPUT_CHARS)
    assert _longest_shared_run(secret, out) is None


def test_masking_that_shrinks_the_head_does_not_let_raw_window_edge_text_in(monkeypatch):
    secret = "".join(uuid.uuid4().hex for _ in range(2))[:60]
    _set_secret(monkeypatch, secret)
    half = DETAIL_OUTPUT_CHARS // 2
    pad = ledger_store._DETAIL_SCAN_PAD
    filler = "lorem ipsum dolor sit amet\n"
    early = (filler + secret + "\n") * 40  # each mask shrinks the head
    at_edge = half + pad - 30
    text = early + (filler * 400)[: at_edge - len(early)] + secret + filler * 4000
    assert len(text) > DETAIL_OUTPUT_CHARS
    out = detail_text(text, DETAIL_OUTPUT_CHARS)
    assert _longest_shared_run(secret, out) is None
    assert out.count(SECRET_MASK) >= 40
    # nothing masked: the bound is unchanged
    plain = detail_text(filler * 2000, DETAIL_OUTPUT_CHARS)
    assert DETAIL_OUTPUT_CHARS - len(filler) < len(plain) - len("\n…[middle cut]…\n") <= DETAIL_OUTPUT_CHARS


@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.asyncio
async def test_an_error_excerpt_masks_an_own_secret(db, agent, monkeypatch, detail):
    secret = "zq" + uuid.uuid4().hex
    _set_secret(monkeypatch, secret)
    store = LedgerStore(db, agent, detail_enabled=detail)
    entry = await _run(
        store,
        tool="run_python",
        tool_input={"code": "raise ValueError(SECRET)"},
        output=f"Traceback (most recent call last):\nValueError: {secret}",
        status="error",
    )
    row = await _row(db, entry)
    assert secret not in row.result_summary and "error excerpt" in row.result_summary


def test_a_service_account_variable_is_collected_as_a_secret(monkeypatch):
    value = "eyJ" + uuid.uuid4().hex * 4
    _set_secret(monkeypatch, value, name="GOOGLE_SERVICE_ACCOUNT_JSON")
    assert value in ledger_store._own_secrets()


# ---- a secret inside a JSON string (F108, the structure is redacted before it is serialised) ----------------------

_NESTED = '{"password":"hunter2"}'  # JSON inside a string: serialised, its quotes are escaped


def test_redact_structure_masks_json_inside_a_string_and_keeps_the_shape():
    value = {"password": "hunter2", "items": [_NESTED, ("curl -d " + _NESTED,)], "n": 3, "flag": None}

    out = ledger_store.redact_structure(value)

    assert "hunter2" not in json.dumps(out["items"])
    # a string alone carries no key: the serialised text's own redaction masks a credential-named key
    assert out["password"] == "hunter2"
    assert "hunter2" not in ledger_store.redact_output(json.dumps(out))
    assert out["n"] == 3 and out["flag"] is None and list(out) == ["password", "items", "n", "flag"]
    assert value["items"][0] == _NESTED  # a copy: the input is untouched
    # with a cap, each string is redacted and bounded as detail_text does
    long = "x" * 5000 + _NESTED
    assert ledger_store.redact_structure([long], 100) == [detail_text(long, 100)]


def test_a_secret_inside_a_json_string_is_masked_in_the_stored_input():
    stored = ledger_store.detail_input("write_file", {"path": "/tmp/a", "content": _NESTED})

    assert "hunter2" not in stored and json.loads(stored)["path"] == "/tmp/a"


# ---- a text near the cap keeps its head and tail (prod: the scan pad is 3148, the longest own secret) ----------------

_PROD_PAD = 3148
_CUT = "\n…[middle cut]…\n"
_LENGTHS = pytest.mark.parametrize("n", [501, 600, 1500, 3000, 3600, 6000, 20000])


@pytest.fixture
def prod_pad(monkeypatch):
    """The scan pad prod measured: it grows with the longest own secret (a service-account JSON)."""
    monkeypatch.setattr(ledger_store, "redaction_scan_pad", lambda: _PROD_PAD)


def _sample(n: int) -> str:
    """``n`` characters of prose that redaction leaves alone, from ``HEAD-`` to ``-TAIL``."""
    return ("HEAD-" + "lorem ipsum dolor sit amet\n" * (n // 27 + 1))[: n - 5] + "-TAIL"


@_LENGTHS
def test_a_text_over_the_cap_keeps_its_head_and_tail_at_the_prod_pad(prod_pad, n):
    """Under the scan bound the windows cover the whole text: dropping the pad there emptied both sides."""
    text = _sample(n)
    out = detail_text(text, 500)
    assert out != _CUT and out.count(_CUT) == 1
    head, tail = out.split(_CUT)
    assert len(head) == 250 and len(tail) == 250
    assert text.startswith(head) and text.endswith(tail)
    assert len(out) <= 500 + len(_CUT)


@_LENGTHS
@pytest.mark.parametrize("where", ["start", "head_cut", "head_far_edge", "tail_far_edge", "tail_cut", "end"])
def test_an_own_secret_is_masked_wherever_it_sits_at_the_prod_pad(prod_pad, own_secret, n, where):
    filler = _sample(n - len(own_secret))
    at = {
        "start": 0,
        "head_cut": 250 - len(own_secret) // 2,  # straddles the head's cut
        "head_far_edge": 250 + _PROD_PAD - len(own_secret) // 2,  # straddles the head's second scan edge
        "tail_far_edge": len(filler) - 250 - _PROD_PAD + len(own_secret) // 2,
        "tail_cut": len(filler) - 250 + len(own_secret) // 2,  # straddles the tail's cut
        "end": len(filler),
    }[where]
    at = min(max(at, 0), len(filler))  # a far edge past a short text is its start or end
    text = filler[:at] + own_secret + filler[at:]
    out = detail_text(text, 500)
    assert _longest_shared_run(own_secret, out) is None
    assert len(out) <= 500 + len(_CUT)
    assert out.strip() and out != _CUT
    if _CUT in out:  # masking can bring a text just over the cap back under it, kept whole
        head, tail = out.split(_CUT)
        assert head and tail
    else:
        assert out == text.replace(own_secret, SECRET_MASK)


def test_redact_structure_keeps_a_mid_length_string_at_the_prod_pad(prod_pad):
    long = _sample(1500)
    [out] = ledger_store.redact_structure([long], 500)
    assert out == detail_text(long, 500)
    assert out.startswith("HEAD-") and out.endswith("-TAIL") and len(out) == 500 + len(_CUT)


def test_a_secret_across_the_second_scans_edge_is_masked_when_the_third_scan_runs(prod_pad, own_secret):
    """A blob at the start masks to 15 characters, so the first two scans agree on less than half the cap and the
    third scan runs. The secret straddles the second scan's edge (``half + pad``): only the third sees it whole, so
    the two differ from its start and none of it is kept."""
    blob = base64.b64encode(random.Random(7).randbytes(2475)).decode()  # one 3,300-character run, masked
    assert "+" in blob[:250]  # not a path: masked in every window
    lead = blob + " " + "the quick brown fox jumps over the lazy dog\n" * 600
    at = 250 + _PROD_PAD - len(own_secret) // 2
    text = lead[:at] + own_secret + lead[at:]
    out = detail_text(text, 500)
    head = out.split(_CUT)[0]
    assert _longest_shared_run(own_secret, out) is None
    assert head.startswith("[REDACTED_BLOB] the quick") and len(head) < 250


@pytest.mark.parametrize("lead", ["", "X"], ids=["at_the_edge", "one_char_in"])
def test_a_long_text_that_opens_and_closes_with_a_long_own_secret_keeps_both_sides(monkeypatch, lead):
    """An own secret longer than half the cap runs across the cut, so the first scans disagree from its start; the
    side is settled one pad further in, where the secret is masked whole."""
    secret = " ".join(f"qxzjk{i}" for i in range(500))  # ~4000 chars: the scan pad grows to its length
    _set_secret(monkeypatch, secret)
    prose = "the quick brown fox jumps over the lazy dog\n" * 1000
    text = lead + secret + prose + secret + lead[::-1]
    out = detail_text(text, 500)
    head, tail = out.split(_CUT)
    assert _longest_shared_run(secret, out, 12) is None
    assert head.startswith(lead + SECRET_MASK + "the quick") and len(head) == 250
    assert tail.endswith("lazy dog\n" + SECRET_MASK + lead[::-1]) and len(tail) == 250


@pytest.mark.parametrize("pad", [None, _PROD_PAD], ids=["default_pad", "prod_pad"])
def test_a_long_text_dense_with_masks_keeps_both_sides(monkeypatch, pad):
    """``git log --format=%H``: each hash masks to fewer characters, so a window shrinks below the pad. Dropping the
    pad in redacted characters then emptied both sides; it is dropped in raw characters."""
    if pad is not None:
        monkeypatch.setattr(ledger_store, "redaction_scan_pad", lambda: pad)
    hashes = "".join(hashlib.sha1(str(i).encode()).hexdigest() + "\n" for i in range(2000))
    out = detail_text(hashes, 500)
    head, tail = out.split(_CUT)
    assert "[REDACTED_HEX]" in head and "[REDACTED_HEX]" in tail
    assert re.search(r"[0-9a-f]{4}", out) is None  # no part of a hash the cut split
    assert len(out) <= 500 + len(_CUT)


# ---- a periodic own secret, repeated (Codex on PR #874) -------------------------------------------------------------
# str.replace masks non-overlapping matches from the left, so where a scan starts decides the alignment at which it
# masks a run of a self-overlapping secret: a window that starts inside the run can leave a raw stretch of it, at the
# window's end or wherever the run ends.

# name: (secret, its period)
PERIODIC = {
    "period_1": ("z" * 16, 1),
    "period_2": ("qx" * 10, 2),
    "period_7": ("k3y!x9Q" * 4, 7),
    "period_8": ("s3cr3t! " * 8, 8),
}
_PROSE = "the quick brown fox jumps over the lazy dog\n"


def periodic_texts(secret: str, period: int) -> list[str]:
    """Texts that hold ``secret`` only in whole occurrences: runs of it (alone, at the end, in the middle,
    continued by whole periods past a whole occurrence) and separated copies, at lengths from under 500 to past
    3 * (500 + 2 * 3148), each at several alignments."""
    texts = []
    for target in (300, 700, 2000, 6000, 7500, 12000, 24000):
        k = max(1, target // len(secret))
        for shift in (0, 1, 3):
            lead = _PROSE[: (shift * 11) % len(_PROSE)]
            run = secret * (k + shift)
            texts += [
                run,
                lead + run,
                _PROSE * 3 + run + "p" * shift + " " + _PROSE * 3,
                lead + run + secret[: period * (1 + shift)] + " " + _PROSE,  # a run continued by whole periods
                (secret + " ") * (k // 2 + shift),
            ]
    return texts


def test_codex_case_a_repeated_periodic_password_leaves_no_raw_run(monkeypatch):
    """Codex's reproduction: the tail's windows start inside the run and mask it 6 characters off the whole text's
    alignment, which kept a raw 56-character stretch of the password."""
    password = "s3cr3t! " * 8
    _set_secret(monkeypatch, password, name="NOUS_TEST_FAKE_PASSWORD")
    monkeypatch.setattr(ledger_store, "redaction_scan_pad", lambda: 256)
    out = detail_text(password * 100, 500)
    assert _longest_shared_run(password, out, 6) is None, out[-80:]


@pytest.mark.parametrize("pad", [256, _PROD_PAD])
@pytest.mark.parametrize("name", list(PERIODIC))
def test_a_periodic_own_secret_leaves_no_fragment_at_any_length(monkeypatch, pad, name):
    secret, period = PERIODIC[name]
    _set_secret(monkeypatch, secret)
    monkeypatch.setattr(ledger_store, "redaction_scan_pad", lambda: pad)
    for text in periodic_texts(secret, period):
        for cap in (100, 500):
            out = detail_text(text, cap)
            assert _longest_shared_run(secret, out, 6) is None, (len(text), cap, out)


def test_own_secret_spans_cover_every_occurrence(monkeypatch):
    """The linear walk against brute force: every occurrence of every spelling, overlapping ones included, merged
    where they overlap or touch. Secrets and texts over two or three letters, where self-overlap and second periods
    (the Fibonacci word 'abaababaabaab' has periods 5 and 8) are common."""
    rng = random.Random(874)
    for _ in range(400):
        for key in [k for k in os.environ if k.startswith("NOUS_TEST_SPAN_")]:
            monkeypatch.delenv(key)
        alphabet = rng.choice(["ab", "abc", "aab"])
        values = []
        for j in range(rng.randint(1, 3)):
            base = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 5)))
            values.append(rng.choice([(base * 20)[: rng.randint(8, 30)], "abaababaabaab"]))
            monkeypatch.setenv(f"NOUS_TEST_SPAN_{j}_TOKEN", values[-1])
        ledger_store._own_secrets.cache_clear()
        text = "".join(rng.choice(alphabet + "x") for _ in range(rng.randint(0, 300)))
        planted = rng.choice(values)  # only this test's values: never a real secret of the machine
        text = text[:40] + planted * rng.randint(1, 5) + planted[: rng.randint(0, len(planted))] + text[40:]
        # the walk masks every own secret, so the brute force counts every one; the message names only this test's
        secrets = ledger_store._own_secrets()
        every = sorted((i, i + len(s)) for s in secrets for i in range(len(text)) if text.startswith(s, i))
        want: list[tuple[int, int]] = []
        for start, end in every:
            if want and start <= want[-1][1]:
                want[-1] = (want[-1][0], max(want[-1][1], end))
            else:
                want.append((start, end))
        assert ledger_store._own_secret_spans(text) == want, (values, text)


def test_a_run_continued_past_a_whole_occurrence_is_masked_in_the_whole_text(monkeypatch):
    """Whole-text redaction too: str.replace masked three copies and left the next 24 characters raw, though they
    end an occurrence that overlaps the third copy."""
    password = "s3cr3t! " * 8
    _set_secret(monkeypatch, password)
    assert ledger_store.redact_output("x " + password * 3 + password[:24] + " y") == f"x {SECRET_MASK} y"
