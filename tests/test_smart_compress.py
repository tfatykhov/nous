"""Tests for SmartCompress ingestion-time compression."""

import json
import re

import pytest
from nous.api.smart_compress import (
    add_cache_key,
    classify_content,
    is_crushable,
    ContentType,
    extract_preserved_lines,
    final_marker_line,
    CompressResult,
    compress_string_array,
    compress_dict_array,
    smart_compress,
    SmartCompressResult,
)
from nous.config import Settings


# --- Task 1: Classification + Crushability ---


class TestContentTypeClassification:
    def test_small_content_classified_as_small(self):
        assert classify_content("short output") == ContentType.SMALL

    def test_json_array_classified(self):
        data = json.dumps([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}] * 50)
        assert classify_content(data) == ContentType.DICT_ARRAY

    def test_newline_separated_classified_as_string_array(self):
        text = "\n".join(f"src/file_{i}.py:10: match found here" for i in range(30))
        assert classify_content(text) == ContentType.STRING_ARRAY

    def test_log_format_detected(self):
        lines = "\n".join(
            f"2026-03-07T12:00:{i:02d}Z INFO processing item {i}"
            for i in range(20)
        )
        assert classify_content(lines) == ContentType.LOG_FORMAT

    def test_large_text_few_lines_classified_as_raw(self):
        # Few long lines = RAW_TEXT (not STRING_ARRAY which requires >10 lines)
        text = ("x" * 200 + "\n") * 5
        assert classify_content(text) == ContentType.RAW_TEXT


class TestCrushabilityCheck:
    def test_high_uniqueness_no_errors_not_crushable(self):
        # Every line unique, no errors, no scores — skip compression
        lines = "\n".join(f"unique line {i} with data {i * 7}" for i in range(50))
        assert is_crushable(lines) is False

    def test_error_lines_make_crushable(self):
        lines = "\n".join(f"line {i} with some extra padding content here" for i in range(50))
        lines += "\nERROR: something failed"
        assert is_crushable(lines) is True

    def test_duplicate_lines_make_crushable(self):
        lines = "\n".join(["repeated line"] * 40 + [f"unique {i}" for i in range(10)])
        assert is_crushable(lines) is True

    def test_score_field_makes_crushable(self):
        data = json.dumps([{"id": i, "score": 0.9 - i * 0.01, "description": f"item number {i} with details"} for i in range(30)])
        assert is_crushable(data) is True

    def test_small_content_not_crushable(self):
        assert is_crushable("short") is False


# --- Task 2: Error Preservation ---


class TestErrorPreservation:
    def test_error_lines_extracted(self):
        lines = [
            "line 1",
            "line 2",
            "ERROR: connection refused",
            "line 4",
            "Traceback (most recent call last):",
            "line 6",
        ]
        preserved = extract_preserved_lines(lines)
        assert "ERROR: connection refused" in preserved
        assert "Traceback (most recent call last):" in preserved
        assert "line 1" not in preserved

    def test_case_insensitive_match(self):
        lines = ["ok", "FATAL error occurred", "ok"]
        preserved = extract_preserved_lines(lines)
        assert len(preserved) == 1

    def test_no_errors_returns_empty(self):
        lines = ["all good", "no problems", "fine"]
        preserved = extract_preserved_lines(lines)
        assert len(preserved) == 0


# --- Task 3: String Array Compression ---


class TestStringArrayCompression:
    def test_preserves_error_lines(self):
        lines = [f"src/file_{i}.py:10: match" for i in range(100)]
        lines[47] = "ERROR: compilation failed in module X"
        result = compress_string_array(lines, max_k=20)
        assert "ERROR: compilation failed in module X" in result.preserved

    def test_respects_max_k(self):
        lines = [f"src/file_{i}.py:10: match" for i in range(200)]
        result = compress_string_array(lines, max_k=30)
        assert len(result.kept) <= 30 + len(result.preserved)

    def test_includes_tail_lines(self):
        lines = [f"line {i}" for i in range(100)]
        result = compress_string_array(lines, max_k=20)
        # Last few lines should be in kept
        assert any("line 99" in ln for ln in result.kept)

    def test_marker_included(self):
        lines = [f"line {i}" for i in range(100)]
        result = compress_string_array(lines, max_k=20)
        assert "[SmartCompressed:" in result.marker

    def test_small_input_unchanged(self):
        lines = ["line 1", "line 2", "line 3"]
        result = compress_string_array(lines, max_k=50)
        assert len(result.kept) == 3  # All kept, under max_k

    def test_to_text_assembles_output(self):
        lines = [f"line {i}" for i in range(100)]
        result = compress_string_array(lines, max_k=15)
        text = result.to_text()
        assert "[SmartCompressed:" in text
        assert len(text.split("\n")) < 100


# --- Order preservation, gap markers, no dedup (2026-10-28) ---

_GAP_RE = re.compile(r"^\[… result lines (\d+)–(\d+) omitted \((\d+) lines\) …\]$")


def _numbered_chunk(n: int = 60) -> list[str]:
    """A prompt read with `sed -n 1,{n}p FILE | cat -n`: "failed" on line 37
    and a URL on line 52. At 60 lines it used to come back as
    [37, 1, 52, 2, ...] with line 59 dropped."""
    steps = []
    for i in range(1, n + 1):
        if i % 6 == 0:
            text = ""
        elif i == 37:
            text = "If a fetch failed, log it in the BLOCKED LEDGER and move on."
        elif i == 52:
            text = "See https://example.com/style-guide for the house style."
        else:
            text = f"Step {i}: do the thing described for this step and keep going."
        steps.append(f"{i:6}\t{text}")
    return steps


def _walk(body: list[str], original: list[str]) -> list[tuple[int, int]]:
    """Check that `body` is `original` in its original order, with every omitted
    run replaced by ONE gap line naming its exact 1-based range. Returns the gaps.

    This is the whole contract in one walk: no reordering, no duplicate, no
    silent drop, no adjacent gaps, a gap's range and count are exact, and no
    gap hides a single line (the gap line would cost as much as the line).
    """
    cursor = 0  # 0-based index of the next original line expected
    gaps: list[tuple[int, int]] = []
    previous_was_gap = False
    for line in body:
        m = _GAP_RE.match(line)
        if m:
            a, b, n = (int(g) for g in m.groups())
            assert not previous_was_gap, f"two gap lines in a row at {a}"
            assert a == cursor + 1, f"gap starts at {a}, expected {cursor + 1}"
            assert b > a and n == b - a + 1, line
            gaps.append((a, b))
            cursor = b
            previous_was_gap = True
        else:
            assert cursor < len(original), f"extra line {line!r}"
            assert line == original[cursor], (
                f"line {cursor + 1}: expected {original[cursor]!r}, got {line!r}"
            )
            cursor += 1
            previous_was_gap = False
    assert cursor == len(original), f"stopped at line {cursor} of {len(original)}"
    return gaps


def _cut(result: CompressResult) -> list[str]:
    """The body of a result that took the cut path; fails on a passthrough."""
    assert result.marker, "expected a cut, got a passthrough"
    return result.to_text().split("\n")[:-1]


def _body(text: str) -> list[str]:
    """The compressed body: everything before the final marker (and exit line)."""
    lines = text.split("\n")
    if lines[-1].startswith("Exit code: "):
        lines = lines[:-1]
    assert lines[-1].startswith("[SmartCompressed:"), lines[-1]
    return lines[:-1]


class TestOrderPreservingCompression:
    """Kept lines come out in their original order, each omitted run is one
    gap line with its exact range, and nothing is deduplicated."""

    def test_numbered_chunk_keeps_original_order(self):
        chunk = _numbered_chunk()
        result = compress_string_array(chunk, max_k=20)
        body = _cut(result)

        gaps = _walk(body, chunk)
        assert gaps, "a cut must have happened at max_k=20"
        numbers = [int(ln.split("\t")[0]) for ln in body if not _GAP_RE.match(ln)]
        assert numbers == sorted(numbers)
        assert 37 in numbers  # the error-word line, in place
        assert numbers[0] == 1 and numbers[-1] == 60  # head and tail

    @pytest.mark.asyncio
    async def test_numbered_chunk_through_smart_compress_keeps_order(self):
        settings = Settings(
            _env_file=None, smart_compress_enabled=True, smart_compress_min_chars=100,
            smart_compress_max_k=20,
        )
        chunk = _numbered_chunk()
        raw = "\n".join(chunk) + "\nExit code: 0"

        result = await smart_compress("bash", {"command": "sed -n 1,60p f | cat -n"}, raw, settings)

        assert result.was_compressed is True
        lines = result.text.split("\n")
        assert lines[-1] == "Exit code: 0"
        _walk(_body(result.text), chunk)

    def test_gap_markers_name_exact_ranges(self):
        lines = [f"row {i}" for i in range(1, 101)]
        lines[49] = "ERROR: row 50 failed"
        result = compress_string_array(lines, max_k=10)

        # K=10 non-error lines: head 3 + tail 1 + mid 6 (ties by position),
        # plus the error line in place.
        assert _cut(result) == [
            "row 1", "row 2", "row 3", "row 4", "row 5", "row 6", "row 7", "row 8", "row 9",
            "[… result lines 10–49 omitted (40 lines) …]",
            "ERROR: row 50 failed",
            "[… result lines 51–99 omitted (49 lines) …]",
            "row 100",
        ]

    def test_mixed_scores_obey_the_walk(self):
        """Lines of every score class (error, URL, path, digit, plain, blank)
        interleaved: the walk holds whatever the ranking picks."""
        kinds = [
            lambda i: f"ERROR: step {i} failed",
            lambda i: f"see https://example.com/{i}",
            lambda i: "edit /src/module.py",
            lambda i: f"value {i}",
            lambda i: "plain words only",
            lambda i: "",
        ]
        lines = [kinds[(i * 7) % len(kinds)](i) for i in range(300)]
        result = compress_string_array(lines, max_k=30)
        body = _cut(result)

        gaps = _walk(body, lines)
        assert gaps
        for line in body:
            assert not line.startswith("[SmartCompressed:")

    def test_repeated_rows_are_not_deduplicated(self):
        lines = ["src/module.py:10: match"] * 80 + [f"src/file_{i}.py:10: match" for i in range(120)]
        result = compress_string_array(lines, max_k=10)
        body = _cut(result)

        _walk(body, lines)
        # Head 3 + mid 6 fall on the repeated rows: all nine are emitted.
        assert body.count("src/module.py:10: match") == 9
        assert len(result.kept) == 10

    def test_kept_and_preserved_are_disjoint_even_for_repeated_error_lines(self):
        lines = ["ERROR: retry failed"] * 5 + [f"row {i}" for i in range(100)]
        result = compress_string_array(lines, max_k=10)
        body = _cut(result)

        _walk(body, lines)
        assert result.preserved == ["ERROR: retry failed"] * 5
        assert "ERROR: retry failed" not in result.kept
        assert body.count("ERROR: retry failed") == 5

    def test_at_most_max_k_lines_is_a_passthrough(self):
        """Repeated rows in a short result came back as 3 of 10 rows with no
        marker. At most max_k lines: the input, unchanged."""
        table = ["id | status", "---|---"] + ["7 | ok"] * 5 + ["8 | failed"] + ["9 | ok"] * 3
        exactly_max_k = ["dup"] * 20 + [f"line {i}" for i in range(29)] + ["ERROR: x"]
        assert len(exactly_max_k) == 50
        for lines in (table, exactly_max_k):
            result = compress_string_array(lines, max_k=50)
            assert result.marker == ""
            assert result.to_text() == "\n".join(lines)

    @pytest.mark.asyncio
    async def test_short_table_passes_through_smart_compress_unchanged(self):
        settings = Settings(_env_file=None, smart_compress_enabled=True, smart_compress_min_chars=100)
        table = "\n".join(
            ["id | status", "---|---"] + ["7 | ok"] * 5 + ["8 | failed"] + ["9 | ok"] * 3
        ) + "\n" + "x" * 400 + "\nExit code: 0"
        assert is_crushable(table, min_chars=100)

        result = await smart_compress("bash", {"command": "psql"}, table, settings)

        assert result.was_compressed is False
        assert result.text == table

    def test_float_noise_does_not_make_a_cliff(self):
        """A URL line (0.8) among digit lines (0.5): 0.8 - 0.5 is
        0.30000000000000004 in floats, which tripped the 0.3 elbow, gave K=1
        and mid_count=-1, and `[:-1]` dropped one arbitrary line (line 59 of
        the 60-line read). The same numbered shape at 200 lines, so the cut
        path is taken: the cut is to the budget (K=50), never K=1 or K=10."""
        chunk = _numbered_chunk(200)
        result = compress_string_array(chunk, max_k=50, elbow_threshold=0.3)

        _walk(_cut(result), chunk)  # in order, every gap >= 2 lines
        # K=50 non-error lines, plus any single line between two kept ones.
        assert 50 <= len(result.kept) < 60

    @pytest.mark.asyncio
    async def test_degenerate_k_through_smart_compress_drops_no_single_line(self):
        settings = Settings(_env_file=None, smart_compress_enabled=True, smart_compress_min_chars=100)
        chunk = _numbered_chunk(200)
        raw = "\n".join(chunk) + "\nExit code: 0"

        result = await smart_compress("bash", {}, raw, settings)

        assert result.was_compressed is True
        assert result.text.split("\n")[-1] == "Exit code: 0"
        _walk(_body(result.text), chunk)  # in order, every gap >= 2 lines

    @pytest.mark.asyncio
    async def test_numbered_60_line_read_at_max_k_50_passes_through_whole(self):
        """The original symptom, with the size gate open: the 60-line read
        would lose about 8 lines (13% of its chars) to one gap, which is not
        worth a cut, so it comes back byte for byte."""
        settings = Settings(_env_file=None, smart_compress_enabled=True, smart_compress_min_chars=100)
        assert settings.smart_compress_max_k == 50
        raw = "\n".join(_numbered_chunk()) + "\nExit code: 0"

        result = await smart_compress("bash", {"command": "sed -n 1,60p f | cat -n"}, raw, settings)

        assert result.was_compressed is False
        assert result.text == raw

    @staticmethod
    def _every_third_line_an_error(n: int) -> list[str]:
        return [
            f"step {i}: {'error: retry failed' if i % 3 == 0 else 'ok'}" for i in range(n)
        ]

    def test_error_dense_output_passes_through(self):
        """Every 3rd line has an error word: every error line is kept in place,
        so each pair between them becomes a gap line, and the 'compressed'
        body was 97% of the input's chars (227 lines, 74 of them gap lines).
        Not worth it: the input comes back unchanged."""
        lines = self._every_third_line_an_error(300)
        result = compress_string_array(lines, max_k=50)

        assert result.marker == ""
        assert result.to_text() == "\n".join(lines)

    @pytest.mark.asyncio
    async def test_error_dense_output_passes_through_smart_compress(self):
        settings = Settings(_env_file=None, smart_compress_enabled=True, smart_compress_min_chars=100)
        raw = "\n".join(self._every_third_line_an_error(300)) + "\nExit code: 1"
        assert is_crushable(raw, min_chars=100)

        result = await smart_compress("bash", {}, raw, settings)

        assert result.was_compressed is False
        assert result.text == raw

    def test_pytest_shape_with_some_failures_still_compresses(self):
        """The complement: 600 pytest -v lines, 1 in 10 FAILED, still saves
        most of its chars, in order."""
        lines = [
            f"tests/test_mod_{i // 50}.py::test_case_{i} {'FAILED' if i % 10 == 0 else 'PASSED'} [{i * 100 // 600:3d}%]"
            for i in range(600)
        ]
        result = compress_string_array(lines, max_k=50)
        body = _cut(result)

        _walk(body, lines)
        assert len(result.preserved) == 60
        assert len("\n".join(body)) < 0.5 * len("\n".join(lines))

    def test_real_cliff_below_head_plus_tail_uses_the_floor(self):
        """A genuine cliff after the first line (one URL, 0.8, then plain
        lines, 0.3) gives K=1 < head + tail + 1. K is floored, so the cut is
        to a real budget and mid_count is never negative."""
        lines = ["see https://example.com/a"] + ["plain words only here"] * 99
        result = compress_string_array(lines, max_k=50)
        body = _cut(result)

        _walk(body, lines)
        assert len(result.kept) == 10
        assert body.count("plain words only here") == 9

    def test_a_single_omitted_line_is_kept_not_replaced_by_a_gap(self):
        """51 equal lines at max_k=50: the budget would omit exactly one line.
        A gap line would cost as much as that line, so it is kept, and with
        nothing left to omit the input comes back unchanged."""
        lines = [f"row {i}" for i in range(51)]
        result = compress_string_array(lines, max_k=50)
        assert result.marker == ""
        assert result.to_text() == "\n".join(lines)

    def test_tiny_max_k_never_goes_negative(self):
        lines = [f"row {i}" for i in range(20)]
        for max_k in (1, 2, 3):
            result = compress_string_array(lines, max_k=max_k)
            _walk(_cut(result), lines)


class TestElbowThresholdOnLogFormat:
    """The log-format branch passes `smart_compress_elbow_threshold` through,
    as the string-array branch does. (Raw text is at most 10 lines, never
    more than max_k at the default, so the threshold cannot show there.)"""

    @staticmethod
    def _log() -> list[str]:
        """200 timestamped lines (digits: 0.5), every 10th with a URL (0.8),
        one error line. 0.8 - 0.5 is a cliff at 0.2 but not at 0.3."""
        lines = []
        for i in range(200):
            ts = f"2026-03-07T12:{i // 60:02d}:{i % 60:02d}Z"
            if i == 120:
                lines.append(f"{ts} ERROR upload of batch {i} failed")
            elif i % 10 == 5:
                lines.append(f"{ts} INFO fetched https://example.com/batch/{i}")
            else:
                lines.append(f"{ts} INFO processed batch {i} in 12 ms")
        return lines

    @pytest.mark.asyncio
    async def test_a_non_default_threshold_changes_the_cut(self):
        lines = self._log()
        raw = "\n".join(lines)
        assert classify_content(raw, min_chars=100) == ContentType.LOG_FORMAT

        markers = {}
        for threshold in (0.3, 0.2):
            settings = Settings(
                _env_file=None, smart_compress_enabled=True, smart_compress_min_chars=100,
                smart_compress_elbow_threshold=threshold,
            )
            result = await smart_compress("bash", {}, raw, settings)

            assert result.was_compressed is True
            expected = compress_string_array(lines, max_k=50, elbow_threshold=threshold)
            assert _body(result.text) == expected.body
            markers[threshold] = result.text.split("\n")[-1]

        # The default keeps K=50 lines; at 0.2 the URL cliff gives K=20.
        assert markers[0.3].startswith("[SmartCompressed: 200→51 lines,")
        assert markers[0.2].startswith("[SmartCompressed: 200→21 lines,")


class TestMinCharsDefault:
    """Short results are left alone: below 8000 chars SmartCompress saved
    little (bash, 72 h in prod: 113 results up to 2 KB saved 19.6 K chars in
    all, 87 of them by dedup alone) and every cut costs a re-read."""

    def test_default_is_8000(self):
        assert Settings(_env_file=None).smart_compress_min_chars == 8000

    @pytest.mark.asyncio
    async def test_numbered_60_line_read_passes_through_at_defaults(self):
        """A 60-line prompt read with `sed -n 1,60p FILE | cat -n` (about
        5 KB) arrives untouched."""
        settings = Settings(_env_file=None)
        raw = "\n".join(_numbered_chunk()) + "\nExit code: 0"
        assert 500 <= len(raw) < 8000

        result = await smart_compress("bash", {}, raw, settings)

        assert result.was_compressed is False
        assert result.text == raw

    @pytest.mark.asyncio
    async def test_the_floor_is_the_only_difference(self):
        """Same shape either side of the floor: below it passes through,
        above it is compressed."""
        settings = Settings(_env_file=None)

        def rows(n: int) -> str:  # 77 chars + newline per row
            return "\n".join(
                f"{i:05d} | {'failed' if i % 10 == 0 else 'passed'} | " + "x" * 60 for i in range(n)
            )

        below, above = rows(100), rows(103)  # 7,799 and 8,033 chars
        assert len(below) < 8000 <= len(above)
        for text in (below, above):
            assert is_crushable(text, min_chars=500)

        assert (await smart_compress("bash", {}, below, settings)).was_compressed is False
        assert (await smart_compress("bash", {}, above, settings)).was_compressed is True


class TestCompactionReadsTheMarker:
    """compaction.py parses the original size from the LAST marker line. Run
    its real parser on a real compressed result."""

    @pytest.mark.asyncio
    async def test_compaction_parser_reads_original_chars(self):
        from nous.api.compaction import ConversationCompactor

        settings = Settings(
            _env_file=None, smart_compress_enabled=True, smart_compress_min_chars=100,
            smart_compress_max_k=10,
        )
        lines = [f"row {i}" for i in range(1, 301)]
        lines[149] = "ERROR: row 150 failed"
        raw = "\n".join(lines) + "\nExit code: 1"

        result = await smart_compress("bash", {}, raw, settings)

        assert result.was_compressed is True
        out = result.text.split("\n")
        assert out[-1] == "Exit code: 1"
        assert out[-2].startswith("[SmartCompressed:")
        assert sum(1 for ln in out if ln.startswith("[SmartCompressed:")) == 1
        assert ConversationCompactor._SMARTCOMPRESS_CHARS_RE.findall(result.text) == [str(len(raw))]
        compactor = ConversationCompactor(settings)
        assert compactor._original_result_size(result.text) == len(raw)


# --- The cache_retrieve key in the final marker (2026-10-28) ---

_KEY = "3f2a9c0b1d4e5f60718293a4b5c6d7e8"


def _keyed(marker: str, key: str) -> str:
    """`marker` with ", cache_retrieve key <key>" before ", N chars original]"."""
    head, sep, tail = marker.rpartition(", ")
    assert sep and re.fullmatch(r"\d+ chars original\]", tail), marker
    return f"{head}, cache_retrieve key {key}, {tail}"


class TestAddCacheKey:
    """add_cache_key names the key in the final marker and nowhere else."""

    _SETTINGS = dict(_env_file=None, smart_compress_enabled=True, smart_compress_min_chars=100, smart_compress_max_k=20)

    @pytest.mark.asyncio
    async def test_with_the_exit_line(self):
        raw = "\n".join(_numbered_chunk()) + "\nExit code: 0"
        result = await smart_compress("bash", {}, raw, Settings(**self._SETTINGS))
        assert result.was_compressed is True
        before = result.text.split("\n")

        after = add_cache_key(result.text, _KEY).split("\n")

        assert after[-1] == "Exit code: 0"
        assert after[-2] == _keyed(before[-2], _KEY)
        assert after[-2].endswith(f", cache_retrieve key {_KEY}, {len(raw)} chars original]")
        assert after[:-2] == before[:-2]

    @pytest.mark.asyncio
    async def test_without_the_exit_line(self):
        raw = "\n".join(_numbered_chunk())
        result = await smart_compress("bash", {}, raw, Settings(**self._SETTINGS))
        assert result.was_compressed is True
        before = result.text.split("\n")

        after = add_cache_key(result.text, _KEY).split("\n")

        assert after[-1] == _keyed(before[-1], _KEY)
        assert after[:-1] == before[:-1]

    @pytest.mark.asyncio
    async def test_the_items_marker_of_a_json_result(self):
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        raw = json.dumps(_ranked_items(200), indent=2)
        result = await smart_compress("web_search", {}, raw, settings)
        assert result.was_compressed is True
        before = result.text.split("\n")

        after = add_cache_key(result.text, _KEY).split("\n")

        assert after[-1] == _keyed(before[-1], _KEY)
        assert after[-1].startswith("[SmartCompressed: 200→50 items")

    @pytest.mark.parametrize(
        "text",
        [
            "plain output\nwith no marker at all",
            "plain output\nExit code: 0",
            "Exit code: 0",
            "",
            # A marker without the original size is not one smart_compress() emits.
            "row 1\n[SmartCompressed: 60→20 lines, 1 error/outlier preserved, order kept, gaps marked]",
            # The final line must BE the marker, not mention it.
            "row 1\nsee [SmartCompressed: 60→20 lines, gaps marked, 5000 chars original]",
        ],
    )
    def test_no_final_marker_returns_the_text_unchanged(self, text):
        assert add_cache_key(text, _KEY) == text
        assert final_marker_line(text) is None  # the one locator compaction shares

    def test_a_marker_quoted_in_the_body_is_left_alone(self):
        quoted = "[SmartCompressed: 9→2 lines, 0 error/outlier preserved, order kept, gaps marked, 999 chars original]"
        final = (
            "[SmartCompressed: 60→20 lines, 1 error/outlier preserved, order kept, gaps marked, 5000 chars original]"
        )
        text = "\n".join(["row 1", quoted, "row 3", final, "Exit code: 0"])

        out = add_cache_key(text, _KEY).split("\n")

        assert out == ["row 1", quoted, "row 3", _keyed(final, _KEY), "Exit code: 0"]
        # A quoted marker as the body's last line, with no real marker after
        # it, is never reached: the line before the exit line is "row 3".
        assert add_cache_key("\n".join([quoted, "row 3", "Exit code: 0"]), _KEY) == "\n".join(
            [quoted, "row 3", "Exit code: 0"]
        )

    @pytest.mark.parametrize(
        "key",
        [_KEY, "0123456789abcdef0123456789012345", "12345678901234567890123456789012"],
        ids=["hex", "ends-in-digits", "all-digits"],
    )
    @pytest.mark.asyncio
    async def test_compaction_parser_reads_original_chars_from_a_keyed_marker(self, key):
        """compaction's lazy `[^\\]\\n]*?(\\d+) chars original` must still read
        the size, not the key's trailing digits."""
        from nous.api.compaction import ConversationCompactor

        settings = Settings(**{**self._SETTINGS, "smart_compress_max_k": 10})
        lines = [f"row {i}" for i in range(1, 301)]
        lines[149] = "ERROR: row 150 failed"
        raw = "\n".join(lines) + "\nExit code: 1"
        result = await smart_compress("bash", {}, raw, settings)
        assert result.was_compressed is True

        keyed = add_cache_key(result.text, key)

        assert f"cache_retrieve key {key}" in keyed
        assert ConversationCompactor._SMARTCOMPRESS_CHARS_RE.findall(keyed) == [str(len(raw))]
        assert ConversationCompactor(settings)._original_result_size(keyed) == len(raw)


# --- Task 4: Dict Array Compression ---


def _ranked_items(n: int, failed_every: int | None = None) -> list[dict]:
    """A ranked JSON array shaped like search results; with `failed_every`,
    every such item's snippet holds an error word."""
    return [
        {
            "id": i,
            "score": round(1.0 - i * 0.004, 3),
            "title": f"result {i}",
            "snippet": (
                "the deploy failed on retry " if failed_every and i % failed_every == 0
                else "the deploy finished normally "
            ) * 8,
        }
        for i in range(n)
    ]


class TestDictArrayCompression:
    def test_top_k_by_score_field(self):
        items = [{"id": i, "score": 1.0 - i * 0.05, "name": f"item_{i}"} for i in range(50)]
        result = compress_dict_array(items, max_k=10)
        # Ruling (2026-10-28): the top 10 by score, in their original order.
        assert result.kept == items[:10]

    def test_detects_score_field(self):
        items = [{"id": i, "relevance": 0.9 - i * 0.1} for i in range(20)]
        result = compress_dict_array(items, max_k=3)
        assert len(result.kept) <= 3

    def test_no_score_field_keeps_all_under_k(self):
        items = [{"id": i, "name": f"n{i}"} for i in range(5)]
        result = compress_dict_array(items, max_k=10)
        assert len(result.kept) == 5

    def test_error_items_preserved(self):
        items = [
            {"id": 1, "score": 0.1, "status": "ok"},
            {"id": 2, "score": 0.05, "error": "connection timeout"},
            {"id": 3, "score": 0.9, "status": "ok"},
        ]
        result = compress_dict_array(items, max_k=1)
        # Error item should be preserved even though only top 1 by score
        item_ids = {item["id"] for item in result.kept + result.preserved}
        assert 2 in item_ids  # Error item
        assert 3 in item_ids  # Top by score

    def test_marker_present(self):
        items = [{"id": i, "score": 1.0 - i * 0.1} for i in range(20)]
        result = compress_dict_array(items, max_k=5)
        assert "[SmartCompressed:" in result.marker

    def test_nothing_cut_with_error_items_has_no_marker(self):
        """More than max_k items, but at most max_k without an error word:
        every item is kept, so nothing is cut and there is no marker."""
        items = _ranked_items(60, failed_every=5)
        assert sum(1 for it in items if "failed" in it["snippet"]) == 12
        result = compress_dict_array(items, max_k=50)
        assert result.marker == ""
        assert result.kept == items

    def test_repeated_items_are_not_deduplicated(self):
        """Every kept item is emitted, however many times it repeats."""
        items = [{"id": 1, "status": "ok"}] * 20 + [{"id": i, "status": "ok"} for i in range(2, 80)]
        items[30] = {"id": 30, "status": "failed"}
        result = compress_dict_array(items, max_k=10)

        emitted = json.loads(result.to_text().split("\n")[0])
        # Ruling (2026-10-28): in original order, so the error item (index 30)
        # comes after the ten kept copies (indices 0-9), not first.
        assert emitted == [{"id": 1, "status": "ok"}] * 10 + [{"id": 30, "status": "failed"}]


class TestDictArrayKeepsOrder:
    """The selection is unchanged (error items, plus the top max_k others by
    score or the first max_k), but every selected item is emitted in its
    original array position, and the marker says 'order kept'."""

    @staticmethod
    def _shuffled_scores(n: int) -> list[dict]:
        # Scores not monotone in position: (i * 37) % n is a permutation of 0..n-1.
        return [{"id": i, "score": round(((i * 37) % n) / n, 3)} for i in range(n)]

    def test_top_k_by_score_is_emitted_in_original_order(self):
        items = self._shuffled_scores(60)
        top = sorted(items, key=lambda it: it["score"], reverse=True)[:20]
        assert [it["id"] for it in top] != sorted(it["id"] for it in top)  # a real test

        result = compress_dict_array(items, max_k=20)

        emitted = json.loads(result.to_text().split("\n")[0])
        assert [it["id"] for it in emitted] == sorted(it["id"] for it in top)
        assert emitted == result.body == result.kept

    def test_error_items_stay_in_place(self):
        items = self._shuffled_scores(60)
        items[7]["status"] = "failed"
        items[58]["status"] = "error: timeout"

        result = compress_dict_array(items, max_k=50)

        ids = [it["id"] for it in result.body]
        assert ids == sorted(ids)
        assert {7, 58} <= set(ids)
        assert len(ids) == 52
        assert result.marker == "[SmartCompressed: 60→52 items, K=50 by score, order kept, 2 outliers]"

    def test_no_score_field_keeps_the_first_max_k_and_a_late_error_in_place(self):
        items = [{"id": i, "status": "ok"} for i in range(80)]
        items[3]["status"] = "failed"
        items[75]["status"] = "failed"

        result = compress_dict_array(items, max_k=10)

        # Indices 0-10 minus the error at 3 are the first 10 others.
        assert [it["id"] for it in result.body] == list(range(11)) + [75]
        assert result.marker == "[SmartCompressed: 80→12 items, first 10, order kept, 2 outliers]"

    def test_a_null_score_ranks_as_zero_and_is_cut_when_it_ranks_out(self):
        """`"score": null` raised TypeError in the sort, and smart_compress()
        passed the whole array through. It now ranks as 0."""
        items = [{"id": i, "score": round(1.0 - i * 0.01, 2)} for i in range(60)]
        items[30]["score"] = None

        result = compress_dict_array(items, max_k=50)

        ids = [it["id"] for it in result.body]
        assert ids == [i for i in range(51) if i != 30]  # every other score is above 0
        assert result.marker.startswith("[SmartCompressed: 60→50 items, K=50 by score, order kept")

    def test_a_null_score_that_ranks_in_is_kept_in_place(self):
        items = [{"id": i, "score": round(-i * 0.01, 2)} for i in range(60)]  # 0.0 down to -0.59
        items[55]["score"] = None  # ranks as 0: ties item 0, ahead of every negative score

        result = compress_dict_array(items, max_k=50)

        assert [it["id"] for it in result.body] == list(range(49)) + [55]

    @pytest.mark.asyncio
    async def test_a_null_score_no_longer_forces_a_passthrough(self):
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        items = _ranked_items(200)
        items[10]["score"] = None  # would rank in at 0.96
        raw = json.dumps(items, indent=2)

        result = await smart_compress("web_search", {}, raw, settings)

        assert result.was_compressed is True
        ids = [it["id"] for it in json.loads(result.text.split("\n")[0])]
        assert ids == [i for i in range(51) if i != 10]  # as 0 it ranks out

    @pytest.mark.asyncio
    async def test_through_smart_compress_with_the_original_size(self):
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        items = _ranked_items(200, failed_every=40)
        # Reverse so the best scores sit at the end of the array.
        items.reverse()
        raw = json.dumps(items, indent=2)

        result = await smart_compress("web_search", {}, raw, settings)

        assert result.was_compressed is True
        body, marker = result.text.split("\n")
        ids = [it["id"] for it in json.loads(body)]
        assert ids == sorted(ids, reverse=True)  # the array's own order
        assert {0, 40, 80, 120, 160} <= set(ids)  # the error items
        assert marker == (
            f"[SmartCompressed: 200→55 items, K=50 by score, order kept, 5 outliers, {len(raw)} chars original]"
        )


# --- Task 5: Entry Point ---


class TestSmartCompressEntryPoint:
    @pytest.mark.asyncio
    async def test_small_content_passes_through(self):
        settings = Settings(
            smart_compress_enabled=True,
            smart_compress_min_chars=500,
        )
        result = await smart_compress("bash", {}, "short output", settings)
        assert result.text == "short output"
        assert result.was_compressed is False

    @pytest.mark.asyncio
    async def test_disabled_passes_through(self):
        settings = Settings(smart_compress_enabled=False)
        big = "\n".join(f"line {i}" for i in range(200))
        result = await smart_compress("bash", {}, big, settings)
        assert result.text == big
        assert result.was_compressed is False

    @pytest.mark.asyncio
    async def test_error_results_pass_through(self):
        settings = Settings(smart_compress_enabled=True)
        big = "\n".join(f"line {i}" for i in range(200))
        result = await smart_compress("bash", {}, big, settings, is_error=True)
        assert result.text == big

    @pytest.mark.asyncio
    async def test_grep_output_compressed(self):
        settings = Settings(
            smart_compress_enabled=True,
            smart_compress_min_chars=100,
            smart_compress_max_k=10,
        )
        # Include duplicates + an error to pass crushability gate
        lines = ["src/module.py:10: match"] * 80 + [f"src/file_{i}.py:10: match" for i in range(120)]
        lines.append("ERROR: build failed")
        text = "\n".join(lines)
        result = await smart_compress("bash", {}, text, settings)
        assert len(result.text) < len(text)
        assert "[SmartCompressed:" in result.text
        assert result.was_compressed is True
        # Ruling (2026-10-28): bash is cached; re-running a command does not
        # reliably reproduce the omitted lines.
        assert result.original_text == text

    @pytest.mark.asyncio
    async def test_json_array_compressed(self):
        settings = Settings(
            smart_compress_enabled=True,
            smart_compress_min_chars=100,
            smart_compress_max_k=5,
        )
        items = [{"id": i, "score": 1.0 - i * 0.02, "data": f"item_{i}"} for i in range(50)]
        text = json.dumps(items)
        result = await smart_compress("web_search", {}, text, settings)
        assert "[SmartCompressed:" in result.text
        assert result.was_compressed is True
        assert result.original_text == text  # web_search is non-re-fetchable

    @pytest.mark.parametrize(
        ("tool", "indent", "tail"),
        [("web_search", 2, ""), ("bash", None, "\nExit code: 0")],
    )
    @pytest.mark.asyncio
    async def test_json_array_with_nothing_cut_passes_through(self, tool, indent, tail):
        """At most max_k items: nothing is cut, so the original bytes come back
        and nothing is cached. Re-serialising it used to count as compression:
        indentation lost, a stray newline before the exit line, and for
        web_search a tool_cache row for a result nothing was cut from."""
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        items = [
            {"id": i, "score": round(1.0 - i * 0.02, 2), "title": f"result {i}", "snippet": "text " * 40}
            for i in range(40)
        ]
        raw = json.dumps(items, indent=indent) + tail
        assert len(items) <= settings.smart_compress_max_k
        assert len(raw) >= settings.smart_compress_min_chars
        assert is_crushable(raw[: len(raw) - len(tail)], min_chars=settings.smart_compress_min_chars)

        result = await smart_compress(tool, {}, raw, settings)

        assert result.was_compressed is False
        assert result.text == raw
        assert result.original_text is None

    @pytest.mark.asyncio
    async def test_json_array_with_error_items_and_nothing_cut_passes_through(self):
        """60 items, 12 with an error word, max_k 50: all 48 others fit, so
        nothing is cut. It used to be re-serialised (indentation lost), the
        error items moved to the front, marked '60→60 items' and cached."""
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        raw = json.dumps(_ranked_items(60, failed_every=5), indent=2)
        assert len(raw) >= settings.smart_compress_min_chars

        result = await smart_compress("web_search", {}, raw, settings)

        assert result.was_compressed is False
        assert result.text == raw
        assert result.original_text is None

    @pytest.mark.asyncio
    async def test_run_list_with_failures_and_nothing_cut_passes_through(self):
        """The same with no score field: `gh run list --json`-like rows, 12 of
        60 failed, through bash with its exit line."""
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        runs = [
            {
                "databaseId": 1000 + i,
                "name": "Tests",
                "conclusion": "failure" if i % 5 == 0 else "success",
                "status": "completed",
                "headBranch": f"feat/x-{i}",
                "message": "ci: run the tests " + ("after a failed push" if i % 5 == 0 else "on a clean push"),
            }
            for i in range(60)
        ]
        raw = json.dumps(runs, indent=2) + "\nExit code: 0"
        assert len(raw) >= settings.smart_compress_min_chars

        result = await smart_compress("bash", {"command": "gh run list --json ..."}, raw, settings)

        assert result.was_compressed is False
        assert result.text == raw

    @pytest.mark.asyncio
    async def test_json_array_cut_that_saves_little_passes_through(self):
        """51 ranked items at max_k 50: one item cut, and compact JSON for the
        rest, kept about 91% of the chars. Not worth a cut (the string path's
        rule): the original bytes come back and nothing is cached."""
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        raw = json.dumps(_ranked_items(51), indent=2)
        assert len(raw) >= settings.smart_compress_min_chars
        assert compress_dict_array(json.loads(raw), max_k=50).marker  # the compressor does cut

        result = await smart_compress("web_search", {}, raw, settings)

        assert result.was_compressed is False
        assert result.text == raw
        assert result.original_text is None

    @pytest.mark.asyncio
    async def test_large_ranked_json_array_still_compresses_and_is_cached(self):
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        raw = json.dumps(_ranked_items(200), indent=2)

        result = await smart_compress("web_search", {}, raw, settings)

        assert result.was_compressed is True
        out = result.text.split("\n")
        assert out[-1].startswith("[SmartCompressed: 200→50 items")
        assert len(json.loads(out[0])) <= 50
        assert result.original_text == raw

    @pytest.mark.asyncio
    async def test_bash_json_with_exit_line_keeps_dict_array_path(self):
        """#179 review: bash_tool now appends 'Exit code: N' to every result.
        The status line must be detached before classification so bash JSON
        output still takes the DICT_ARRAY path (valid JSON out), and
        re-attached as the final line."""
        settings = Settings(
            smart_compress_enabled=True,
            smart_compress_min_chars=100,
            smart_compress_max_k=5,
        )
        items = [{"id": i, "score": 1.0 - i * 0.02, "data": f"item_{i}"} for i in range(50)]
        text = json.dumps(items) + "\nExit code: 0"
        result = await smart_compress("bash", {}, text, settings)
        assert result.was_compressed is True
        lines = result.text.split("\n")
        assert lines[-1] == "Exit code: 0"          # re-attached, authoritative tail
        assert lines[-2].startswith("[SmartCompressed:")
        parsed = json.loads(lines[0])               # DICT_ARRAY path → valid JSON
        assert isinstance(parsed, list) and len(parsed) >= 5

    @pytest.mark.asyncio
    async def test_quoted_exit_line_kept_in_place_and_trailing_exit_line_last(self):
        """#179 review: a quoted 'Exit code: 1' early in the output must not
        displace the authoritative trailing status line from the tail. Since
        2026-10-28 nothing is deduplicated: the quoted line stays in place AND
        the detached status line is re-attached as the final line."""
        settings = Settings(
            smart_compress_enabled=True,
            smart_compress_min_chars=100,
            smart_compress_max_k=10,
        )
        lines = ["replaying saved log:", "Exit code: 1"]
        lines += ["src/module.py:10: match"] * 80
        lines += [f"src/file_{i}.py:10: match" for i in range(120)]
        text = "\n".join(lines) + "\nExit code: 1"
        result = await smart_compress("bash", {}, text, settings)
        assert result.was_compressed is True
        out = result.text.split("\n")
        assert out[-1] == "Exit code: 1"
        assert out[-2].startswith("[SmartCompressed:")
        assert out[1] == "Exit code: 1"  # the quoted line, in its original place
        assert out.count("Exit code: 1") == 2
        _walk(_body(result.text), lines)

    @pytest.mark.asyncio
    async def test_marker_records_original_chars(self):
        """#179: the marker must record the pre-compression size so the
        compaction pruner's bulk threshold survives ingestion compression."""
        settings = Settings(
            smart_compress_enabled=True,
            smart_compress_min_chars=100,
            smart_compress_max_k=10,
        )
        lines = ["src/module.py:10: match"] * 200
        text = "\n".join(lines)
        result = await smart_compress("bash", {}, text, settings)
        assert result.was_compressed is True
        assert f"{len(text)} chars original]" in result.text

    @pytest.mark.asyncio
    async def test_non_refetchable_preserves_original(self):
        settings = Settings(
            smart_compress_enabled=True,
            smart_compress_min_chars=100,
            smart_compress_max_k=5,
        )
        items = [{"id": i, "score": 1.0 - i * 0.02, "data": f"item_{i}"} for i in range(50)]
        text = json.dumps(items)
        result = await smart_compress("web_fetch", {"url": "https://example.com"}, text, settings)
        assert result.original_text == text
        assert result.item_count == 50

    @pytest.mark.asyncio
    async def test_dag_manage_preserves_original_when_compressed(self):
        """codex P2 round 6 FINDING 10: dag_manage's node_result advertises
        a lossless recovery path (FINDING 9) -- but the gap is exactly
        where an agent needs it most: a failed node's long, error-laden
        result. Built from the REAL gate conditions smart_compress checks,
        not invented prose or a mock -- `is_crushable` requires at least
        `smart_compress_min_chars` (8000 by default since 2026-10-28)
        chars AND an `_ERROR_PATTERNS` match (has_errors short-circuits
        the uniqueness check entirely); `compress_string_array` only
        drops content once there are MORE than `max_k` (default 50)
        newline-separated lines. Must fail against the pre-fix
        implementation: `dag_manage` was absent from
        `NON_REFETCHABLE_TOOLS`, so `original_text` stayed None even
        though compression genuinely ran.

        No genuine end-to-end harness (dispatch -> smart_compress ->
        cache_compressed_result -> DB -> cache_retrieve) exists in this
        test suite: test_tool_cache.py has no DB-backed round-trip tests
        at all, and the runner.py tool loop that wires smart_compress and
        tool_cache together has no test harness either. This calls the
        real smart_compress() directly with real gate-triggering content,
        mirroring this file's own established pattern for exactly this
        class of assertion (test_non_refetchable_preserves_original
        above, for web_fetch).

        #724 made dag_manage exempt by default, so node_result now passes
        through whole. This pins the cache for an operator who compresses
        it again (the `[]` kill switch).
        """
        settings = Settings(_env_file=None, smart_compress_enabled=True, smart_compress_exempt_tools=[])
        lines = [f"line {i}: processing step {i} completed" for i in range(250)]
        lines.append("Traceback (most recent call last):")
        lines.append("RuntimeError: failed to complete the fix-stage retry")
        text = "\n".join(lines)
        # Confirm the gate conditions actually hold before trusting the
        # result -- this is not incidental content, it MUST trip both gates.
        assert len(text) >= settings.smart_compress_min_chars
        assert len(lines) > settings.smart_compress_max_k
        assert is_crushable(text, min_chars=settings.smart_compress_min_chars)

        result = await smart_compress(
            "dag_manage", {"action": "node_result", "node_name": "fix1"}, text, settings,
        )

        assert result.was_compressed is True  # confirms the gap is real, not hypothetical
        assert result.text != text  # the model-facing text IS compressed
        assert result.original_text == text  # but the original survives, recoverable


# --- Per-tool exemption: already-ranked output must not be re-ranked ---


class TestExemptTools:
    """`recall_deep` / `recall_recent` output is the curated result of RRF +
    relevance floors + MMR + CE rerank + graph scoring. SmartCompress ranks by
    ORIGINAL POSITION (30% head + 15% tail) and by `_score_line`, which scores a
    line on digits / URLs / file paths / error words and never reads the
    retrieval score — a second, cruder ranker overriding a careful one.

    Measured on prod-shaped blocks: graph/spreading rows survived at 43.1%
    against 92.0% for top-10 heart rows.
    """

    def _recall_block(self) -> str:
        """A block that provably trips every gate, so a pass-through result
        can only be the exemption and not an incidental miss."""
        lines = [
            f"- [fact] memory item {i} recorded during session {i // 5}"
            for i in range(200)
        ]
        lines.append("- [fact] the deploy failed on the first attempt")
        return "\n".join(lines)

    def test_default_exempts_both_recall_tools(self):
        s = Settings(_env_file=None)
        assert "recall_deep" in s.smart_compress_exempt_tools
        assert "recall_recent" in s.smart_compress_exempt_tools

    @pytest.mark.parametrize("tool", ["recall_deep", "recall_recent"])
    @pytest.mark.asyncio
    async def test_recall_output_passes_through_untouched(self, tool):
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        text = self._recall_block()
        # The gates MUST hold, or this test would pass for the wrong reason.
        assert len(text) >= settings.smart_compress_min_chars
        assert is_crushable(text, min_chars=settings.smart_compress_min_chars)

        result = await smart_compress(tool, {"query": "anything"}, text, settings)

        assert result.was_compressed is False
        assert result.text == text, "exempt output must be byte-identical"
        assert result.original_text is None, "nothing cached: nothing was cut"

    @pytest.mark.asyncio
    async def test_identical_text_is_still_compressed_for_a_non_exempt_tool(self):
        """The complement. Without it, the assertions above would also pass if
        the block simply failed a gate — this pins that the ONLY difference is
        the tool name."""
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        text = self._recall_block()

        result = await smart_compress("bash", {}, text, settings)

        assert result.was_compressed is True
        assert result.text != text

    @pytest.mark.asyncio
    async def test_empty_list_is_a_kill_switch(self):
        """`[]` restores today's behaviour for every tool, so the change is
        revertible by config rather than by deploy."""
        settings = Settings(
            _env_file=None, smart_compress_enabled=True,
            smart_compress_exempt_tools=[],
        )
        text = self._recall_block()

        result = await smart_compress("recall_deep", {}, text, settings)

        assert result.was_compressed is True
        assert result.text != text

    @pytest.mark.asyncio
    async def test_exemption_is_per_tool_not_global(self):
        """An operator exempting one tool must not disable compression."""
        settings = Settings(
            _env_file=None, smart_compress_enabled=True,
            smart_compress_exempt_tools=["recall_deep"],
        )
        text = self._recall_block()

        assert (await smart_compress("recall_deep", {}, text, settings)).text == text
        assert (await smart_compress("recall_recent", {}, text, settings)).text != text


class TestCacheRetrieveNeverCompressed:
    """cache_retrieve returns originals; cutting one again would undo it. The
    rule is in code, so even the `[]` kill switch does not reach it."""

    @staticmethod
    def _retrieved() -> str:
        """A retrieved range that trips every gate: long, an error word, many lines."""
        lines = [f"{i:6}\tstep {i}: copied src/module_{i}.py to build/" for i in range(1, 301)]
        lines[149] = "   150\tERROR: step 150 failed"
        return "\n".join(["[result lines 1–300 of 301]", *lines])

    @pytest.mark.parametrize("exempt", [[], ["recall_deep"]], ids=["kill-switch", "list-without-it"])
    @pytest.mark.asyncio
    async def test_passes_through_whatever_the_exempt_list(self, exempt):
        settings = Settings(_env_file=None, smart_compress_enabled=True, smart_compress_exempt_tools=exempt)
        text = self._retrieved()
        assert "cache_retrieve" not in settings.smart_compress_exempt_tools
        # The gates MUST hold, or this would pass for the wrong reason.
        assert len(text) >= settings.smart_compress_min_chars
        assert is_crushable(text, min_chars=settings.smart_compress_min_chars)
        assert (await smart_compress("bash", {}, text, settings)).was_compressed is True

        result = await smart_compress("cache_retrieve", {"hash_key": "0" * 32, "lines": "1-300"}, text, settings)

        assert result.was_compressed is False
        assert result.text == text
        assert result.original_text is None


class TestReadFileExempt:
    """2026-10-07: a 303-line report read with read_file was classified
    STRING_ARRAY (it contains "failed") and cut to ~63 non-contiguous lines;
    read_file is re-fetchable, so no original was cached. read_file now pages
    contiguously itself and SmartCompress must pass it through."""

    @staticmethod
    def _prose_report() -> str:
        lines = [
            f"Section {i // 20}, line {i}: the run {'failed' if i % 7 == 0 else 'passed'} "
            f"on criterion {i % 13}; the effort rating for this item is under review."
            for i in range(300)
        ]
        return "\n".join(lines) + "\n"

    def test_default_exempts_read_file(self):
        assert "read_file" in Settings(_env_file=None).smart_compress_exempt_tools

    @pytest.mark.asyncio
    async def test_read_file_prose_passes_through(self):
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        text = self._prose_report()
        assert is_crushable(text, min_chars=settings.smart_compress_min_chars)

        result = await smart_compress("read_file", {"path": "report.md"}, text, settings)

        assert result.was_compressed is False
        assert result.text == text

    @pytest.mark.asyncio
    async def test_same_text_still_compressed_for_bash(self):
        """Compression for other tools is unchanged."""
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        text = self._prose_report()

        result = await smart_compress("bash", {}, text, settings)

        assert result.was_compressed is True


class TestDbQueryExempt:
    """db_query (#816) bounds its own result (200 rows, 500-char cells,
    100,000 chars) and says when rows were cut. A query result is rows the
    model asked for; sampling them, or dropping repeated rows, changes the
    answer."""

    @staticmethod
    def _query_result() -> str:
        header = "id | status | detail"
        rows = [
            f"{i} | {'failed' if i % 9 == 0 else 'ok'} | " + "row detail text " * 4
            for i in range(200)
        ]
        return "\n".join([header, *rows, "(200 rows)"])

    def test_default_exempts_db_query(self):
        assert "db_query" in Settings(_env_file=None).smart_compress_exempt_tools

    @pytest.mark.asyncio
    async def test_query_result_passes_through(self):
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        text = self._query_result()
        assert len(text) >= settings.smart_compress_min_chars
        assert is_crushable(text, min_chars=settings.smart_compress_min_chars)

        result = await smart_compress("db_query", {"sql": "SELECT 1"}, text, settings)

        assert result.was_compressed is False
        assert result.text == text

    @pytest.mark.asyncio
    async def test_same_text_still_compressed_for_bash(self):
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        result = await smart_compress("bash", {}, self._query_result(), settings)
        assert result.was_compressed is True


class TestDagManageExempt:
    """#724: a DAG summary over 4096 units is cut for Telegram with a marker
    saying `dag_manage status <id>` shows the stored summary. A failed DAG's
    status carries error words, so SmartCompress sampled it in a chat turn
    (about 47 of 120 summary lines kept) and the marker was false exactly
    where the reader acts on it."""

    @staticmethod
    def _failed_status() -> str:
        lines = [
            "DAG: nightly-migration (deadbeef)",
            "Status: failed",
            "Nodes (2):",
            "  [X] migrate (subtask, w0) — failed | error: step 3 failed: lock timeout",
            "  [-] verify (check, w1) — cancelled",
            "Summary:",
        ]
        lines += [f"Paragraph {i}: migration step {i} {'failed' if i % 9 == 0 else 'finished'}." for i in range(240)]
        return "\n".join(lines)

    def test_default_exempts_dag_manage(self):
        assert "dag_manage" in Settings(_env_file=None).smart_compress_exempt_tools

    @pytest.mark.asyncio
    async def test_failed_dag_status_passes_through(self):
        settings = Settings(_env_file=None, smart_compress_enabled=True)
        text = self._failed_status()
        assert is_crushable(text, min_chars=settings.smart_compress_min_chars)

        result = await smart_compress("dag_manage", {"action": "status", "dag_id": "deadbeef"}, text, settings)

        assert result.was_compressed is False
        assert result.text == text
