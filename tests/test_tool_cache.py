"""Tests for ReversibleCache — Postgres-backed tool result cache."""

import json
import re
import uuid

import pytest

from nous.api.smart_compress import add_cache_key, smart_compress
from nous.api.tool_cache import (
    cache_compressed_result,
    compute_hash_key,
    _MAX_RETRIEVE_CHARS,
    _keyword_filter,
    _render_cached,
    NON_REFETCHABLE_TOOLS,
    retrieve_cached_result,
)
from nous.api.tools import CACHE_RETRIEVE_TOOL_DEF
from nous.config import Settings


class TestHashKey:
    def test_deterministic(self):
        content = "some tool output"
        h1 = compute_hash_key(content)
        h2 = compute_hash_key(content)
        assert h1 == h2

    def test_32_chars(self):
        # CR-7: widened 16 -> 32 hex chars (128-bit) to make collisions negligible.
        h = compute_hash_key("test content")
        assert len(h) == 32

    def test_different_content_different_hash(self):
        h1 = compute_hash_key("content A")
        h2 = compute_hash_key("content B")
        assert h1 != h2

    def test_hex_chars_only(self):
        h = compute_hash_key("any content here")
        assert all(c in "0123456789abcdef" for c in h)


class TestNonRefetchableTools:
    def test_web_search_included(self):
        assert "web_search" in NON_REFETCHABLE_TOOLS

    def test_web_fetch_included(self):
        assert "web_fetch" in NON_REFETCHABLE_TOOLS

    def test_bash_included(self):
        """Ruling (2026-10-28): bash was treated as re-fetchable, so a cut
        bash result kept no original. Re-running a command is not
        guaranteed to reproduce its output and may be slow or have side
        effects, so the omitted lines must come from the cache."""
        assert "bash" in NON_REFETCHABLE_TOOLS

    def test_read_file_not_included(self):
        assert "read_file" not in NON_REFETCHABLE_TOOLS

    def test_dag_manage_included(self):
        """codex P2 round 6 FINDING 10: dag_manage's node_result is
        deterministic (calling it again reproduces the same content), but
        that is not the criterion this set encodes -- the criterion is
        whether calling the tool again RECOVERS THE ORIGINAL. It does not:
        SmartCompress re-runs on every dispatch and re-compresses the same
        input identically, so without caching, a compressed node_result
        has no recovery path at all.
        """
        assert "dag_manage" in NON_REFETCHABLE_TOOLS


class TestKeywordFilter:
    def test_json_array_filter(self):
        items = [
            {"title": "Python tutorial", "url": "https://python.org"},
            {"title": "Rust guide", "url": "https://rust-lang.org"},
            {"title": "Python async", "url": "https://docs.python.org"},
        ]
        content = json.dumps(items)
        result = _keyword_filter(content, "python")
        parsed = json.loads(result)
        assert len(parsed) == 2
        assert all("python" in json.dumps(item).lower() for item in parsed)

    def test_json_no_match(self):
        items = [{"title": "A"}, {"title": "B"}]
        content = json.dumps(items)
        result = _keyword_filter(content, "zzz_nonexistent")
        assert "No items matching" in result

    def test_text_line_filter(self):
        content = "line with python\nline with rust\nline with python again"
        result = _keyword_filter(content, "python")
        lines = result.split("\n")
        assert len(lines) == 2
        assert all("python" in ln.lower() for ln in lines)

    def test_text_no_match(self):
        content = "line 1\nline 2\nline 3"
        result = _keyword_filter(content, "zzz_nonexistent")
        assert "No lines matching" in result

    def test_max_20_json_items(self):
        items = [{"title": f"python item {i}"} for i in range(30)]
        content = json.dumps(items)
        result = _keyword_filter(content, "python")
        parsed = json.loads(result)
        assert len(parsed) == 20

    def test_max_50_text_lines(self):
        content = "\n".join(f"match line {i}" for i in range(100))
        result = _keyword_filter(content, "match")
        assert len(result.split("\n")) == 50


class TestCacheRetrieveToolDef:
    def test_tool_def_has_name(self):
        assert CACHE_RETRIEVE_TOOL_DEF["name"] == "cache_retrieve"

    def test_tool_def_has_description(self):
        assert "SmartCompressed" in CACHE_RETRIEVE_TOOL_DEF["description"]

    def test_tool_def_requires_hash_key(self):
        schema = CACHE_RETRIEVE_TOOL_DEF["input_schema"]
        assert "hash_key" in schema["required"]

    def test_tool_def_query_optional(self):
        schema = CACHE_RETRIEVE_TOOL_DEF["input_schema"]
        assert "query" in schema["properties"]
        assert "query" not in schema["required"]

    def test_tool_def_lines_optional(self):
        schema = CACHE_RETRIEVE_TOOL_DEF["input_schema"]
        assert schema["properties"]["lines"]["type"] == "string"
        assert "lines" not in schema["required"]

    def test_tool_def_says_bash_lines_and_the_marker_key(self):
        props = CACHE_RETRIEVE_TOOL_DEF["input_schema"]["properties"]
        assert "bash" in CACHE_RETRIEVE_TOOL_DEF["description"]
        assert 'lines="A-B"' in CACHE_RETRIEVE_TOOL_DEF["description"]
        assert "result lines A–B omitted" in props["lines"]["description"]
        assert "cache_retrieve key" in props["hash_key"]["description"]


def _rows(n: int) -> str:
    """An n-line result: "row 1" .. "row n", "failed" on every 7th row."""
    return "\n".join(f"row {i}" + (" failed" if i % 7 == 0 else "") for i in range(1, n + 1))


class TestLineRanges:
    """cache_retrieve's `lines`: 1-based, inclusive, over content.split("\\n")."""

    CONTENT = _rows(60)
    LINES = CONTENT.split("\n")

    @pytest.mark.parametrize("spec", ["10-20", "10–20", " 10 - 20 ", "10 – 20"])
    def test_a_range_returns_those_lines_under_a_header(self, spec):
        out = _render_cached(self.CONTENT, lines=spec)
        assert out == "\n".join(["[result lines 10–20 of 60]", *self.LINES[9:20]])

    def test_a_single_line(self):
        assert _render_cached(self.CONTENT, lines="7") == "[result lines 7–7 of 60]\nrow 7 failed"
        assert _render_cached(self.CONTENT, lines="7-7") == "[result lines 7–7 of 60]\nrow 7 failed"

    def test_the_first_and_the_last_line(self):
        assert _render_cached(self.CONTENT, lines="1") == "[result lines 1–1 of 60]\nrow 1"
        assert _render_cached(self.CONTENT, lines="60") == "[result lines 60–60 of 60]\nrow 60"

    def test_an_end_past_the_last_line_is_clipped(self):
        out = _render_cached(self.CONTENT, lines="55-999")
        assert out == "\n".join(["[result lines 55–60 of 60]", *self.LINES[54:]])

    @pytest.mark.parametrize(
        ("spec", "why"),
        [
            ("61", "only 60 lines"),
            ("70-80", "only 60 lines"),
            ("20-10", "first line is after the last"),
            ("0-5", "start at 1"),
            ("0", "start at 1"),
            ("abc", "not a line range"),
            ("3-", "not a line range"),
            ("-5", "not a line range"),
            ("1,5", "not a line range"),
            ("1-5-9", "not a line range"),
            ("2.5", "not a line range"),
        ],
    )
    def test_a_range_that_names_no_lines_gets_a_plain_message(self, spec, why):
        out = _render_cached(self.CONTENT, lines=spec)
        assert why in out
        assert "60 lines" in out
        assert "row " not in out

    def test_the_range_is_taken_first_then_filtered(self):
        # "failed" is on rows 7, 14, ..., 56: only 14 and 21 are in 10-25.
        out = _render_cached(self.CONTENT, lines="10-25", query="failed")
        assert out == "[result lines 10–25 of 60]\nrow 14 failed\nrow 21 failed"

    def test_a_filtered_range_with_no_match_says_so(self):
        out = _render_cached(self.CONTENT, lines="1-6", query="failed")
        assert out.split("\n")[0] == "[result lines 1–6 of 60]"
        assert "No lines matching 'failed' in cached 6 lines." in out

    def test_query_alone_is_unchanged(self):
        assert _render_cached(self.CONTENT, query="failed") == _keyword_filter(self.CONTENT, "failed")

    def test_no_arguments_return_the_whole_original(self):
        assert _render_cached(self.CONTENT) == self.CONTENT


_FOOTER_RE = re.compile(
    r'^\[… result lines (\d+)–(\d+) not shown \((\d+) lines\); call cache_retrieve with lines="(\d+)-(\d+)" …\]$'
)


class TestRetrieveCap:
    """One retrieval returns at most _MAX_RETRIEVE_CHARS (footer aside)."""

    @staticmethod
    def _wide(n: int) -> list[str]:
        return [f"{i:5} " + "x" * 94 for i in range(1, n + 1)]  # 100 chars per line

    def test_the_cap_is_30000(self):
        assert _MAX_RETRIEVE_CHARS == 30_000

    def test_a_result_at_the_cap_is_whole(self):
        line = "y" * 99
        content = "\n".join([line] * 400)[:_MAX_RETRIEVE_CHARS]
        assert len(content) == _MAX_RETRIEVE_CHARS
        assert _render_cached(content) == content
        # One char more and the last (partial) line no longer fits.
        out = _render_cached(content + "y").split("\n")
        assert out[-1] == '[… result lines 301–301 not shown (1 lines); call cache_retrieve with lines="301-301" …]'

    def test_a_full_result_over_the_cap_ends_with_the_next_range(self):
        lines = self._wide(1000)  # 100,999 chars
        out = _render_cached("\n".join(lines)).split("\n")

        shown, footer = out[:-1], out[-1]
        m = _FOOTER_RE.match(footer)
        assert m, footer
        k, b, n, k2, b2 = (int(g) for g in m.groups())
        assert shown == lines[: k - 1], "whole lines only, in order"
        assert len("\n".join(shown)) <= _MAX_RETRIEVE_CHARS
        assert len("\n".join(lines[:k])) > _MAX_RETRIEVE_CHARS, "line K would not have fit"
        assert (k, b, n, k2, b2) == (k, 1000, 1000 - k + 1, k, 1000)

    def test_following_the_footers_rebuilds_the_original(self):
        lines = self._wide(1000)
        content = "\n".join(lines)
        rebuilt = _render_cached(content).split("\n")
        calls = 1
        while (m := _FOOTER_RE.match(rebuilt[-1])) is not None:
            rebuilt.pop()
            header, *page = _render_cached(content, lines=f"{m.group(4)}-{m.group(5)}").split("\n")
            assert header == f"[result lines {m.group(4)}–{m.group(5)} of 1000]"
            assert len("\n".join([header, *page])) <= _MAX_RETRIEVE_CHARS + 200  # one footer at most
            rebuilt.extend(page)
            calls += 1
        assert rebuilt == lines
        assert calls == 4  # 100,999 chars in pages of at most 30,000

    def test_a_ranged_result_over_the_cap_names_the_rest_of_its_range(self):
        lines = self._wide(1000)
        out = _render_cached("\n".join(lines), lines="101-900").split("\n")

        assert out[0] == "[result lines 101–900 of 1000]"
        m = _FOOTER_RE.match(out[-1])
        assert m, out[-1]
        k = int(m.group(1))
        assert out[1:-1] == lines[100 : k - 1]
        assert len("\n".join(out[:-1])) <= _MAX_RETRIEVE_CHARS
        assert out[-1] == (
            f'[… result lines {k}–900 not shown ({900 - k + 1} lines); call cache_retrieve with lines="{k}-900" …]'
        )

    def test_a_single_line_over_the_cap_is_cut_at_the_cap(self):
        long = "z" * 50_000
        content = "\n".join(["row 1", "row 2", long, "row 4", "row 5"])

        # The full result stops before it, whole lines only.
        assert _render_cached(content).split("\n") == [
            "row 1", "row 2",
            '[… result lines 3–5 not shown (3 lines); call cache_retrieve with lines="3-5" …]',
        ]

        # Asked for, it is cut at the cap and the footer says so, then names the rest.
        out = _render_cached(content, lines="3-5").split("\n")
        header = "[result lines 3–5 of 5]"
        room = _MAX_RETRIEVE_CHARS - len(header) - 1
        assert out[0] == header
        assert out[1] == "z" * room
        assert len("\n".join(out[:2])) == _MAX_RETRIEVE_CHARS
        assert out[2] == (
            f"[… result line 3 cut at {room} of 50000 chars; cache_retrieve cannot page within a line; "
            'result lines 4–5 not shown (2 lines); call cache_retrieve with lines="4-5" …]'
        )

        # Alone, the footer has nothing more to name.
        out = _render_cached(content, lines="3").split("\n")
        assert out[2] == f"[… result line 3 cut at {room} of 50000 chars; cache_retrieve cannot page within a line …]"

    def test_a_one_line_original_over_the_cap(self):
        content = "j" * 40_000  # a one-line JSON blob, say
        out = _render_cached(content).split("\n")
        assert out == [
            "j" * _MAX_RETRIEVE_CHARS,
            "[… result line 1 cut at 30000 of 40000 chars; cache_retrieve cannot page within a line …]",
        ]

    def test_query_output_keeps_its_line_limit_and_gets_the_cap(self):
        content = "\n".join(f"match {i} " + "q" * 990 for i in range(100))  # ~1,000 chars per line
        out = _render_cached(content, query="match")

        shown, footer = out.rsplit("\n", 1)
        assert len(shown) == _MAX_RETRIEVE_CHARS
        full = _keyword_filter(content, "match")
        assert len(full.split("\n")) == 50  # the existing 50-line limit still applies first
        assert shown == full[:_MAX_RETRIEVE_CHARS]
        assert footer == f"[… query result cut at 30000 of {len(full)} chars; narrow the query …]"

    def test_query_output_keeps_its_item_limit_and_gets_the_cap(self):
        items = [{"title": f"match {i}", "body": "b" * 3000} for i in range(30)]
        out = _render_cached(json.dumps(items), query="match")

        shown, footer = out.rsplit("\n", 1)
        full = _keyword_filter(json.dumps(items), "match")
        assert len(json.loads(full)) == 20  # the existing 20-item limit still applies first
        assert shown == full[:_MAX_RETRIEVE_CHARS]
        assert footer.startswith("[… query result cut at 30000 of ")

    def test_a_ranged_query_gets_the_cap(self):
        content = "\n".join(f"match {i} " + "q" * 990 for i in range(100))
        out = _render_cached(content, lines="1-80", query="match")
        assert out.startswith("[result lines 1–80 of 100]\n")
        assert out.rsplit("\n", 1)[1].startswith("[… query result cut at 30000 of ")


class TestFrameToolAccess:
    def test_cache_retrieve_in_conversation_frame(self):
        from nous.api.runner import FRAME_TOOLS
        assert "cache_retrieve" in FRAME_TOOLS["conversation"]

    def test_cache_retrieve_in_question_frame(self):
        from nous.api.runner import FRAME_TOOLS
        assert "cache_retrieve" in FRAME_TOOLS["question"]

    def test_cache_retrieve_in_decision_frame(self):
        from nous.api.runner import FRAME_TOOLS
        assert "cache_retrieve" in FRAME_TOOLS["decision"]

    def test_cache_retrieve_in_debug_frame(self):
        from nous.api.runner import FRAME_TOOLS
        assert "cache_retrieve" in FRAME_TOOLS["debug"]

    def test_cache_retrieve_in_task_via_wildcard(self):
        from nous.api.runner import FRAME_TOOLS
        assert "*" in FRAME_TOOLS["task"]  # wildcard covers all


# --- Every cut can be undone (real Postgres) ---

_GAP_RE = re.compile(r"^\[… result lines (\d+)–(\d+) omitted \((\d+) lines\) …\]$")


def _ids() -> tuple[str, str]:
    """A fresh agent and session per test, so no test sees another's rows."""
    tag = uuid.uuid4().hex[:12]
    return f"test-cache-{tag}", f"s-{tag}"


@pytest.mark.postgres_only
class TestRecoverEveryCut:
    """The property that matters: a bash result is cut, cached, and every gap
    marker's range brings back exactly the lines it stands for."""

    @staticmethod
    def _bash_output() -> list[str]:
        lines = []
        for i in range(1, 241):
            if i % 40 == 0:
                text = f"ERROR: step {i} failed"
            elif i % 9 == 0:
                text = ""
            else:
                text = f"step {i}: copied src/module_{i}.py to build/"
            lines.append(f"{i:6}\t{text}")
        return lines

    @pytest.mark.asyncio
    async def test_every_gap_marker_range_returns_exactly_the_omitted_lines(self, session):
        settings = Settings(
            _env_file=None, smart_compress_enabled=True, smart_compress_min_chars=100, smart_compress_max_k=20,
        )
        chunk = self._bash_output()
        raw = "\n".join(chunk) + "\nExit code: 0"
        result = await smart_compress("bash", {"command": "cat -n build.log"}, raw, settings)
        assert result.was_compressed is True, "the output must be cut for this test to mean anything"
        assert result.original_text == raw

        agent_id, session_id = _ids()
        key = await cache_compressed_result(
            session, agent_id, session_id, "bash", {"command": "cat -n build.log"},
            result.original_text, result.item_count,
        )
        text = add_cache_key(result.text, key)
        out = text.split("\n")
        assert out[-1] == "Exit code: 0"
        assert f", cache_retrieve key {key}, {len(raw)} chars original]" in out[-2]

        total = len(raw.split("\n"))
        rebuilt: list[str] = []
        gaps = 0
        for line in out[:-2]:
            m = _GAP_RE.match(line)
            if not m:
                rebuilt.append(line)
                continue
            gaps += 1
            a, b, n = (int(g) for g in m.groups())
            got = await retrieve_cached_result(session, session_id, key, lines=f"{a}-{b}")
            header, *omitted = got.split("\n")
            assert header == f"[result lines {a}–{b} of {total}]"
            assert omitted == chunk[a - 1 : b] and len(omitted) == n
            rebuilt.extend(omitted)
        assert gaps >= 2
        assert rebuilt == chunk, "the kept lines plus every retrieved gap must be the original, in order"

        # The range exactly as the marker prints it, en dash and all.
        first_gap = next(ln for ln in out if _GAP_RE.match(ln))
        printed = re.search(r"result lines (\d+–\d+) omitted", first_gap).group(1)
        a, b = (int(x) for x in printed.split("–"))
        got = await retrieve_cached_result(session, session_id, key, lines=printed)
        assert got == "\n".join([f"[result lines {a}–{b} of {total}]", *chunk[a - 1 : b]])

    @pytest.mark.asyncio
    async def test_a_range_then_a_query_through_the_cache(self, session):
        agent_id, session_id = _ids()
        content = _rows(60)
        key = await cache_compressed_result(session, agent_id, session_id, "bash", {}, content)

        got = await retrieve_cached_result(session, session_id, key, query="failed", lines="10-25")

        assert got == "[result lines 10–25 of 60]\nrow 14 failed\nrow 21 failed"
        assert await retrieve_cached_result(session, session_id, "0" * 32, lines="1-5") is None
