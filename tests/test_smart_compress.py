"""Tests for SmartCompress ingestion-time compression."""

import json
import pytest
from nous.api.smart_compress import (
    classify_content,
    is_crushable,
    ContentType,
    extract_preserved_lines,
    compress_string_array,
    compress_dict_array,
    smart_compress,
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


# --- Task 4: Dict Array Compression ---


class TestDictArrayCompression:
    def test_top_k_by_score_field(self):
        items = [{"id": i, "score": 1.0 - i * 0.05, "name": f"item_{i}"} for i in range(50)]
        result = compress_dict_array(items, max_k=10)
        assert len(result.kept) <= 10
        # Top items should be highest scored
        assert result.kept[0]["score"] >= result.kept[-1]["score"]

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


# --- Task 5: Entry Point ---


class TestSmartCompressEntryPoint:
    @pytest.mark.asyncio
    async def test_small_content_passes_through(self):
        settings = Settings(
            smart_compress_enabled=True,
            smart_compress_min_chars=500,
        )
        result = await smart_compress("bash", {}, "short output", settings)
        assert result == "short output"

    @pytest.mark.asyncio
    async def test_disabled_passes_through(self):
        settings = Settings(smart_compress_enabled=False)
        big = "\n".join(f"line {i}" for i in range(200))
        result = await smart_compress("bash", {}, big, settings)
        assert result == big

    @pytest.mark.asyncio
    async def test_error_results_pass_through(self):
        settings = Settings(smart_compress_enabled=True)
        big = "\n".join(f"line {i}" for i in range(200))
        result = await smart_compress("bash", {}, big, settings, is_error=True)
        assert result == big

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
        assert len(result) < len(text)
        assert "[SmartCompressed:" in result

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
        assert "[SmartCompressed:" in result
