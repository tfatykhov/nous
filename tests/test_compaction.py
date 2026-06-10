"""Tests for Spec 008.1 Phase 1: Tool Output Pruning + Token Estimation."""

from nous.api.compaction import ConversationCompactor, TokenEstimator
from nous.config import Settings

# ------------------------------------------------------------------
# TokenEstimator Tests
# ------------------------------------------------------------------


class TestTokenEstimator:
    def test_initial_estimate_chars_div_4(self):
        est = TokenEstimator()
        assert est.estimate("a" * 100) == 25  # 100 * 0.25

    def test_estimate_minimum_1(self):
        est = TokenEstimator()
        assert est.estimate("") == 1
        assert est.estimate("a") == 1

    def test_estimate_non_string(self):
        est = TokenEstimator()
        result = est.estimate(["some", "list"])
        assert result >= 1

    def test_estimate_messages(self):
        est = TokenEstimator()
        messages = [
            {"role": "user", "content": "a" * 100},
            {"role": "assistant", "content": "b" * 200},
        ]
        # 100*0.25 + 4 + 200*0.25 + 4 = 25 + 4 + 50 + 4 = 83
        assert est.estimate_messages(messages) == 83

    def test_calibrate_shifts_ratio(self):
        est = TokenEstimator()
        assert est.ratio == 0.25
        # If actual tokens are higher than estimated, ratio should increase
        est.calibrate(input_chars=1000, actual_tokens=500)
        # observed = 500/1000 = 0.5
        # new ratio = 0.1 * 0.5 + 0.9 * 0.25 = 0.05 + 0.225 = 0.275
        assert abs(est.ratio - 0.275) < 0.001
        assert est.samples == 1

    def test_calibrate_ignores_zero(self):
        est = TokenEstimator()
        est.calibrate(0, 100)
        est.calibrate(100, 0)
        est.calibrate(0, 0)
        assert est.ratio == 0.25
        assert est.samples == 0

    def test_calibrate_converges(self):
        est = TokenEstimator()
        # Feed consistent signal: 0.5 tokens per char
        for _ in range(50):
            est.calibrate(1000, 500)
        assert abs(est.ratio - 0.5) < 0.01


# ------------------------------------------------------------------
# Tool Result Identification Tests
# ------------------------------------------------------------------


class TestIsToolResultMessage:
    def test_positive_tool_result(self):
        msg = {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "output"}
            ],
        }
        assert ConversationCompactor.is_tool_result_message(msg) is True

    def test_negative_string_content(self):
        msg = {"role": "user", "content": "hello"}
        assert ConversationCompactor.is_tool_result_message(msg) is False

    def test_negative_assistant_role(self):
        msg = {
            "role": "assistant",
            "content": [{"type": "tool_result", "content": "x"}],
        }
        assert ConversationCompactor.is_tool_result_message(msg) is False

    def test_negative_empty_list(self):
        msg = {"role": "user", "content": []}
        assert ConversationCompactor.is_tool_result_message(msg) is False

    def test_negative_wrong_type(self):
        msg = {
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
        }
        assert ConversationCompactor.is_tool_result_message(msg) is False

    def test_negative_missing_content(self):
        msg = {"role": "user"}
        assert ConversationCompactor.is_tool_result_message(msg) is False


# ------------------------------------------------------------------
# Tool Result Pruning Tests
# ------------------------------------------------------------------


def _make_tool_result(content: str, tool_id: str = "t1") -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": content}
        ],
    }


def _make_assistant(text: str = "ok") -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
    }


def _make_user(text: str = "hello") -> dict:
    return {"role": "user", "content": text}


def _make_settings(**overrides) -> Settings:
    defaults = {
        "ANTHROPIC_API_KEY": "test",
        "NOUS_TOOL_PRUNING_ENABLED": "true",
        "NOUS_TOOL_SOFT_TRIM_CHARS": "100",
        "NOUS_TOOL_SOFT_TRIM_HEAD": "20",
        "NOUS_TOOL_SOFT_TRIM_TAIL": "20",
        "NOUS_TOOL_HARD_CLEAR_AFTER": "12",
        "NOUS_TOOL_METADATA_DEGRADE_AFTER": "8",
        "NOUS_KEEP_LAST_TOOL_RESULTS": "2",
    }
    defaults.update(overrides)
    return Settings(**defaults)


class TestPruneToolResults:
    def test_empty_messages_noop(self):
        compactor = ConversationCompactor(_make_settings())
        messages: list = []
        compactor.prune_tool_results(messages)
        assert messages == []

    def test_no_tool_results_noop(self):
        compactor = ConversationCompactor(_make_settings())
        messages = [_make_user("hi"), _make_assistant("hello")]
        original = [m.copy() for m in messages]
        compactor.prune_tool_results(messages)
        assert len(messages) == len(original)

    def test_all_protected_noop(self):
        """When all tool results are within protection zone, nothing is pruned."""
        compactor = ConversationCompactor(_make_settings())
        messages = [
            _make_assistant(),
            _make_tool_result("short output", "t1"),
            _make_assistant(),
            _make_tool_result("short output", "t2"),
        ]
        compactor.prune_tool_results(messages)
        # Both are in last 2 (keep_last_tool_results=2), so no pruning
        assert messages[1]["content"][0]["content"] == "short output"
        assert messages[3]["content"][0]["content"] == "short output"

    def test_soft_trim_large_result(self):
        """Results exceeding soft_trim_chars get head+tail trimmed."""
        compactor = ConversationCompactor(_make_settings())
        large_content = "x" * 200  # > 100 threshold
        messages = [
            _make_assistant(),
            _make_tool_result(large_content, "t1"),  # old, not protected
            _make_assistant(),
            _make_tool_result("small", "t2"),  # protected
            _make_assistant(),
            _make_tool_result("small", "t3"),  # protected
        ]
        compactor.prune_tool_results(messages)
        trimmed = messages[1]["content"][0]["content"]
        assert "--- trimmed" in trimmed
        assert trimmed.startswith("x" * 20)  # head
        assert trimmed.endswith("x" * 20)  # tail

    def test_soft_trim_preserves_small(self):
        """Results under threshold are not trimmed."""
        compactor = ConversationCompactor(_make_settings())
        messages = [
            _make_assistant(),
            _make_tool_result("small", "t1"),  # under threshold, not protected
            _make_assistant(),
            _make_tool_result("small2", "t2"),
            _make_assistant(),
            _make_tool_result("small3", "t3"),  # protected
            _make_assistant(),
            _make_tool_result("small4", "t4"),  # protected
        ]
        compactor.prune_tool_results(messages)
        assert messages[1]["content"][0]["content"] == "small"

    def test_hard_clear_old_results(self):
        """Results older than clear_age get replaced with placeholder.

        Uses list_files (aggressive profile: clear_age=8, degrade_age=4)
        so that 10 tool results produce hard-clears at the oldest positions.
        """
        compactor = ConversationCompactor(_make_settings())
        # Create 10 tool results with list_files (aggressive: clear_age=8)
        # keep_last=2, so ages are 10,9,...,1. Ages >= 8 -> hard-cleared (first 3).
        messages = []
        for i in range(10):
            messages.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"t{i}", "name": "list_files", "input": {"path": "."}}],
            })
            messages.append(_make_tool_result(f"result_{i}", f"t{i}"))

        compactor.prune_tool_results(messages)

        # First 3 results (age 10, 9, 8 >= clear_age=8) should be hard-cleared
        assert "cleared" in messages[1]["content"][0]["content"]
        assert "cleared" in messages[3]["content"][0]["content"]
        assert "cleared" in messages[5]["content"][0]["content"]

        # Result at age 7 (< clear_age=8, >= degrade_age=4) would be degraded,
        # but content "result_3" is small (<200 chars) so kept as-is
        assert "result_3" in messages[7]["content"][0]["content"]

        # Last 2 (protected)
        assert "result_8" in messages[17]["content"][0]["content"]
        assert "result_9" in messages[19]["content"][0]["content"]

    def test_never_modify_assistant_blocks(self):
        """Assistant messages (including thinking blocks) are never modified."""
        compactor = ConversationCompactor(_make_settings())
        assistant_msg = {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "x" * 500},
                {"type": "text", "text": "response"},
                {"type": "tool_use", "id": "t1", "name": "bash", "input": {}},
            ],
        }
        messages = [
            assistant_msg,
            _make_tool_result("result", "t1"),
        ]
        compactor.prune_tool_results(messages)
        # Assistant message unchanged
        assert messages[0]["content"][0]["thinking"] == "x" * 500

    def test_never_modify_user_text(self):
        """Regular user text messages are never modified."""
        compactor = ConversationCompactor(_make_settings())
        messages = [
            _make_user("x" * 500),  # Large user text
            _make_assistant(),
            _make_tool_result("result", "t1"),
        ]
        compactor.prune_tool_results(messages)
        assert messages[0]["content"] == "x" * 500

    def test_skip_image_results(self):
        """Tool results containing images are not pruned."""
        compactor = ConversationCompactor(_make_settings())
        image_result = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [
                        {"type": "image", "source": {"data": "base64..."}}
                    ],
                }
            ],
        }
        messages = [
            _make_assistant(),
            image_result,
            _make_assistant(),
            _make_tool_result("text", "t2"),
            _make_assistant(),
            _make_tool_result("text", "t3"),
        ]
        compactor.prune_tool_results(messages)
        # Image result should be untouched
        assert messages[1]["content"][0]["content"][0]["type"] == "image"

    def test_pruning_disabled(self):
        """When tool_pruning_enabled=False, nothing happens."""
        settings = _make_settings(NOUS_TOOL_PRUNING_ENABLED="false")
        compactor = ConversationCompactor(settings)
        large = "x" * 200
        messages = [
            _make_assistant(),
            _make_tool_result(large, "t1"),
            _make_assistant(),
            _make_tool_result("small", "t2"),
            _make_assistant(),
            _make_tool_result("small", "t3"),
        ]
        compactor.prune_tool_results(messages)
        assert messages[1]["content"][0]["content"] == large


# ------------------------------------------------------------------
# #179: Bulk-result pruning tests
# ------------------------------------------------------------------


def _make_named_pair(name: str, content: str, tid: str) -> list[dict]:
    """assistant tool_use + matching tool_result message pair."""
    return [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": {"code": "sweep()"}}],
        },
        _make_tool_result(content, tid),
    ]


def _bulk_settings(**overrides) -> Settings:
    """Settings with a small bulk threshold (500) above soft-trim (100)."""
    defaults = {"NOUS_TOOL_BULK_RESULT_CHARS": "500"}
    defaults.update(overrides)
    return _make_settings(**defaults)


class TestBulkResultPruning:
    """#179: oversized results escalate to the (1, 2, 4) bulk profile with
    anti-replay stub text, so a completed sweep can't dominate context and
    trigger a replay loop."""

    def _sweep_conversation(self, bulk_content: str) -> list[dict]:
        """run_python bulk result at age 5, then 4 small results after it
        (keep_last=2 → ages 5,4,3 unprotected; the bulk result is oldest)."""
        messages: list[dict] = []
        messages += _make_named_pair("run_python", bulk_content, "t0")
        for i in range(1, 5):
            messages += _make_named_pair("run_python", f"small_{i}", f"t{i}")
        return messages

    def test_bulk_result_hard_cleared_at_bulk_age(self):
        """A bulk run_python result at age 5 is hard-cleared (bulk clear=4)
        even though run_python's standard profile wouldn't clear until 12."""
        compactor = ConversationCompactor(_bulk_settings())
        messages = self._sweep_conversation("x" * 600)
        compactor.prune_tool_results(messages)
        cleared = messages[1]["content"][0]["content"]
        assert "Bulk tool output cleared" in cleared
        # codex round 4: facts only — "already ran" + tool-reported status,
        # not a semantic COMPLETED claim.
        assert "already ran" in cleared
        assert "tool reported success" in cleared
        assert "do NOT re-run" in cleared

    def test_non_bulk_result_keeps_standard_ages(self):
        """The same conversation with a sub-threshold result is NOT cleared —
        run_python's standard profile (3,8,12) applies."""
        compactor = ConversationCompactor(_bulk_settings())
        messages = self._sweep_conversation("x" * 400)  # < 500 bulk threshold
        compactor.prune_tool_results(messages)
        text = messages[1]["content"][0]["content"]
        assert "cleared" not in text
        # age 5 >= soft-trim only (oversized vs 100): trimmed, not stubbed
        assert "--- trimmed" in text

    def test_bulk_detection_survives_soft_trim(self):
        """A previously soft-trimmed bulk result still counts as bulk via the
        original-size marker, and is cleared with the anti-replay stub."""
        trimmed = (
            "xxxx\n\n--- trimmed (kept 20 head + 20 tail of 99999 chars) ---\n\nyyyy"
        )
        compactor = ConversationCompactor(_bulk_settings())
        messages = self._sweep_conversation(trimmed)
        compactor.prune_tool_results(messages)
        assert "Bulk tool output cleared" in messages[1]["content"][0]["content"]

    def test_bulk_degrade_carries_hint(self):
        """At degrade age (bulk: 2-3) the metadata stub carries the
        anti-replay hint so bulkness persists to the clear tier."""
        compactor = ConversationCompactor(_bulk_settings())
        # bulk result at age 3: 3 pairs total, keep_last=2 → only the first
        # is unprotected at age 3 → degrade tier (>=2, <4).
        messages: list[dict] = []
        messages += _make_named_pair("run_python", "x" * 600, "t0")
        for i in range(1, 3):
            messages += _make_named_pair("run_python", f"small_{i}", f"t{i}")
        compactor.prune_tool_results(messages)
        degraded = messages[1]["content"][0]["content"]
        assert "do NOT re-run" in degraded
        # codex round 7: the reported outcome must survive at the degrade
        # stage so checkpoint summaries can mirror it.
        assert "tool reported success" in degraded
        # And a second pass at clear age still recognizes it as bulk.
        messages += _make_named_pair("run_python", "small_3", "t3")
        compactor.prune_tool_results(messages)
        assert "Bulk tool output cleared" in messages[1]["content"][0]["content"]

    def test_bulk_disabled_when_zero(self):
        """NOUS_TOOL_BULK_RESULT_CHARS=0 disables bulk escalation."""
        compactor = ConversationCompactor(
            _bulk_settings(NOUS_TOOL_BULK_RESULT_CHARS="0")
        )
        messages = self._sweep_conversation("x" * 600)
        compactor.prune_tool_results(messages)
        text = messages[1]["content"][0]["content"]
        assert "Bulk tool output cleared" not in text
        assert "--- trimmed" in text  # plain soft-trim only

    def test_conservative_tool_exempt_from_bulk(self):
        """codex round 11: web_fetch/web_search (conservative) are
        pure-retrieval — re-fetching is cheap and benign, so a large
        result keeps conservative ages and never gets a do-NOT-re-run
        stub."""
        compactor = ConversationCompactor(_bulk_settings())
        bulk_content = "see https://example.com/report plus " + "x" * 600
        messages: list[dict] = []
        messages += _make_named_pair("web_fetch", bulk_content, "t0")
        for i in range(1, 5):
            messages += _make_named_pair("web_fetch", f"small_{i}", f"t{i}")
        compactor.prune_tool_results(messages)
        text = messages[1]["content"][0]["content"]
        # age 5 < conservative degrade(10)/clear(15): soft-trim only.
        assert "Bulk tool output cleared" not in text
        assert "do NOT re-run" not in text
        assert "--- trimmed" in text

    def test_bulk_sibling_item_keeps_own_profile(self):
        """codex P1 round 2: a small sibling result in the SAME message as a
        bulk result (parallel tool calls) keeps its own tool profile — it is
        not dragged onto bulk ages."""
        compactor = ConversationCompactor(_bulk_settings())
        messages: list[dict] = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t0a", "name": "run_python", "input": {}},
                    {"type": "tool_use", "id": "t0b", "name": "web_fetch", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t0a", "content": "x" * 600},
                    {"type": "tool_result", "tool_use_id": "t0b", "content": "small fetch"},
                ],
            },
        ]
        for i in range(1, 5):
            messages += _make_named_pair("run_python", f"small_{i}", f"t{i}")
        compactor.prune_tool_results(messages)
        bulk_item, sibling = messages[1]["content"]
        assert "Bulk tool output cleared" in bulk_item["content"]
        # web_fetch is 'conservative' (5, 10, 15): untouched at age 5.
        assert sibling["content"] == "small fetch"

    def test_bulk_error_result_not_marked_completed(self):
        """codex P1 round 2: a failed bulk call must not get a COMPLETED /
        do-not-re-run stub — that would suppress a legitimate retry."""
        compactor = ConversationCompactor(_bulk_settings())
        messages = self._sweep_conversation("Traceback: " + "x" * 600)
        messages[1]["content"][0]["is_error"] = True
        compactor.prune_tool_results(messages)
        stub = messages[1]["content"][0]["content"]
        assert "FAILED" in stub
        assert "tool reported success" not in stub
        assert "do NOT re-run" not in stub

    def test_conservative_facts_extracted_before_degrade(self):
        """codex P1 round 2: on the incremental lifecycle (degrade first,
        clear later), conservative facts are captured at degrade time —
        not lost by the time clear sees only the stub."""
        compactor = ConversationCompactor(_bulk_settings())
        content = "see https://example.com/report plus " + "x" * 300
        messages: list[dict] = []
        messages += _make_named_pair("web_fetch", content, "t0")
        for i in range(1, 10):
            messages += _make_named_pair("web_fetch", f"small_{i}", f"t{i}")
        # Pass 1: item at age 10 → conservative degrade tier (5, 10, 15).
        # Facts extracted NOW, before the metadata stub destroys them.
        extracted = compactor.prune_tool_results(messages)
        assert any("example.com" in f for f in extracted)
        assert " | first: " in messages[1]["content"][0]["content"]
        # Pass 2: age 15 → clear tier. Stub yields nothing new; no crash.
        for i in range(10, 15):
            messages += _make_named_pair("web_fetch", f"small_{i}", f"t{i}")
        extracted2 = compactor.prune_tool_results(messages)
        assert "cleared" in messages[1]["content"][0]["content"]
        assert not any("example.com" in f for f in extracted2)

    def test_smartcompressed_result_is_bulk(self):
        """codex round 9: SmartCompress (F020) shrinks a sweep at ingestion,
        so the pruner may never see a 50KB+ result — the [SmartCompressed:]
        marker itself means 'sweep-shaped repetitive output' and must
        trigger bulk handling (original is preserved in tool_cache)."""
        compactor = ConversationCompactor(_bulk_settings())
        digest = (
            "result line 1\nresult line 2\n"
            "[SmartCompressed: 5000→58 lines, 3 error/outlier preserved]"
        )
        messages = self._sweep_conversation(digest)
        compactor.prune_tool_results(messages)
        stub = messages[1]["content"][0]["content"]
        assert "Bulk tool output cleared" in stub
        assert "do NOT re-run" in stub

    def test_smartcompressed_bash_failure_detected(self):
        """codex round 9: SmartCompress appends its marker AFTER the
        preserved 'Exit code: N' line — the line-anchored regex must still
        detect the failure (bash results only)."""
        compactor = ConversationCompactor(_bulk_settings())
        digest = (
            "sweep output\nExit code: 1\n"
            "[SmartCompressed: 5000→58 lines, 1 error/outlier preserved]"
        )
        messages: list[dict] = []
        messages += _make_named_pair("bash", digest, "t0")
        for i in range(1, 5):
            messages += _make_named_pair("bash", f"small_{i}", f"t{i}")
        compactor.prune_tool_results(messages)
        stub = messages[1]["content"][0]["content"]
        assert "FAILED" in stub
        assert "tool reported success" not in stub

    def test_non_bash_exit_code_mention_not_failure(self):
        """codex round 10: 'Exit code: 1' inside non-bash content (e.g. a
        fetched page quoting a shell session) must NOT mark the bulk result
        as failed — the marker is bash_tool-specific."""
        compactor = ConversationCompactor(_bulk_settings())
        content = "x" * 600 + "\nthe build log ended with\nExit code: 1"
        messages: list[dict] = []
        messages += _make_named_pair("run_python", content, "t0")
        for i in range(1, 5):
            messages += _make_named_pair("run_python", f"small_{i}", f"t{i}")
        compactor.prune_tool_results(messages)
        stub = messages[1]["content"][0]["content"]
        assert "tool reported success" in stub
        assert "FAILED" not in stub

    def test_bulk_bash_nonzero_exit_marked_failed(self):
        """codex round 8: bash reports failure IN the text ('Exit code: N'
        appended last) and never sets is_error — the FAILED stub must fire
        from the content marker."""
        compactor = ConversationCompactor(_bulk_settings())
        messages: list[dict] = []
        messages += _make_named_pair("bash", "x" * 600 + "\nExit code: 1", "t0")
        for i in range(1, 5):
            messages += _make_named_pair("bash", f"small_{i}", f"t{i}")
        compactor.prune_tool_results(messages)
        stub = messages[1]["content"][0]["content"]
        assert "FAILED" in stub
        assert "tool reported success" not in stub

    def test_unlisted_tool_exempt_from_bulk(self):
        """codex round 12: escalation is a positive allowlist — an unlisted
        (retrieval-ish or future) tool never bulk-escalates, even with a
        huge result."""
        compactor = ConversationCompactor(_bulk_settings())
        messages: list[dict] = []
        messages += _make_named_pair("recall_recent", "x" * 600, "t0")
        for i in range(1, 5):
            messages += _make_named_pair("recall_recent", f"small_{i}", f"t{i}")
        compactor.prune_tool_results(messages)
        text = messages[1]["content"][0]["content"]
        assert "Bulk tool output cleared" not in text
        assert "do NOT re-run" not in text

    def test_preserve_profile_exempt_from_bulk(self):
        """codex round 7: a large read_file result keeps its 'preserve'
        profile — deliberate reference content is not a sweep, and
        re-reading a file is cheap and legitimate."""
        compactor = ConversationCompactor(_bulk_settings())
        messages: list[dict] = []
        messages += _make_named_pair("read_file", "x" * 600, "t0")
        for i in range(1, 5):
            messages += _make_named_pair("read_file", f"small_{i}", f"t{i}")
        compactor.prune_tool_results(messages)
        text = messages[1]["content"][0]["content"]
        assert "Bulk tool output cleared" not in text
        assert "do NOT re-run" not in text
        # preserve (8, 999, 20): at age 5 only the ageless soft-trim applies.
        assert "--- trimmed" in text

    def test_repetitive_ops_rule_in_both_prompts(self):
        """codex P2: the #179 compression rule must be in BOTH the checkpoint
        and the update summarization prompts — updates run on every
        compaction after the first."""
        from nous.api.compaction import CHECKPOINT_SYSTEM_PROMPT, UPDATE_SYSTEM_PROMPT

        for prompt in (CHECKPOINT_SYSTEM_PROMPT, UPDATE_SYSTEM_PROMPT):
            assert "REPETITIVE OPERATIONS RULE" in prompt
            # codex rounds 3+6: the rule must mirror the surviving tool
            # results' wording — never mandate COMPLETED, never upgrade
            # "tool reported success" into a completion claim.
            assert "exactly as the surviving" in prompt
            assert "do not upgrade" in prompt.lower()
            assert "never describe a failed one as successful" in prompt


# ------------------------------------------------------------------
# Extract Text Tests
# ------------------------------------------------------------------


class TestExtractText:
    def test_single_text_block(self):
        content = [{"type": "text", "text": "hello"}]
        assert ConversationCompactor.extract_text(content) == "hello"

    def test_multiple_text_blocks(self):
        content = [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]
        assert ConversationCompactor.extract_text(content) == "hello world"

    def test_no_text_blocks(self):
        content = [{"type": "tool_use", "name": "bash"}]
        assert ConversationCompactor.extract_text(content) == ""


# ------------------------------------------------------------------
# Find Cut Point Tests (Phase 2 stubs, basic validation)
# ------------------------------------------------------------------


class TestFindCutPoint:
    def test_returns_zero_when_fits(self):
        compactor = ConversationCompactor(_make_settings())
        messages = [
            {"role": "user", "content": "short"},
            {"role": "assistant", "content": "reply"},
        ]
        assert compactor.find_cut_point(messages, keep_recent_tokens=10000) == 0

    def test_should_compact_disabled(self):
        compactor = ConversationCompactor(_make_settings(NOUS_COMPACTION_ENABLED="false"))
        # compaction explicitly disabled
        assert compactor.should_compact(5000, 200000) is False

    def test_snaps_to_user_boundary(self):
        compactor = ConversationCompactor(_make_settings())
        messages = [
            {"role": "user", "content": "a" * 1000},
            {"role": "assistant", "content": "b" * 1000},
            {"role": "user", "content": "c" * 1000},
            {"role": "assistant", "content": "d" * 1000},
        ]
        # With keep_recent_tokens very small, should cut early but at user boundary
        cut = compactor.find_cut_point(messages, keep_recent_tokens=300)
        assert cut > 0, "Should need to cut with only 300 token budget"
        assert messages[cut]["role"] == "user"
