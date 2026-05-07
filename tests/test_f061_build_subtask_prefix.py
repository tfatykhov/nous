"""F061 PR-2: tests for build_subtask_prefix — legacy and hardened modes."""

from __future__ import annotations

import pytest

from nous.api.tools import (
    _F061_DEFAULT_BOUNDARIES,
    _F061_DEFAULT_SUCCESS_CRITERIA,
    _F061_FRAME_OUTPUT_FORMATS,
    build_subtask_prefix,
)


class TestLegacyMode:
    """When hardening_enabled=False, emit the pre-F061 text byte-identical."""

    _LEGACY_BASE = (
        "You are executing a background subtask.\n"
        "Deliver a clear, complete result. Do not ask questions."
    )

    def test_legacy_no_frame(self):
        s = build_subtask_prefix("Do X", frame_type=None, hardening_enabled=False)
        assert s == f"{self._LEGACY_BASE}\n\nTask: Do X"

    def test_legacy_with_known_frame(self):
        # Use 'debug' — present in FRAME_TOOLS so legacy renders the Frame line.
        # 'research' is intentionally NOT in FRAME_TOOLS today.
        s = build_subtask_prefix(
            "Do Y", frame_type="debug", hardening_enabled=False,
        )
        assert "Frame: debug" in s
        assert "Task: Do Y" in s
        # No F061 sections in legacy mode
        assert "Objective" not in s
        assert "submit_final_report" not in s

    def test_legacy_research_frame_dropped(self):
        # Pre-F061 behavior: 'research' is not in FRAME_TOOLS so the Frame
        # block was silently dropped. PR-1 must not change this.
        s = build_subtask_prefix(
            "Do Z", frame_type="research", hardening_enabled=False,
        )
        assert "Frame:" not in s

    def test_legacy_unknown_frame_dropped(self):
        s = build_subtask_prefix(
            "Do Z", frame_type="not_a_frame", hardening_enabled=False,
        )
        assert "Frame:" not in s

    def test_default_is_legacy_mode(self):
        """hardening_enabled defaults to False — legacy callers unchanged."""
        s = build_subtask_prefix("Do X")
        assert "submit_final_report" not in s
        assert "# Objective" not in s


class TestHardenedMode:
    """When hardening_enabled=True, emit the four-part brief + termination."""

    def test_hardened_full_template(self):
        s = build_subtask_prefix(
            "Research X",
            frame_type="research",
            hardening_enabled=True,
        )
        assert "# Objective" in s
        assert "Research X" in s
        assert "# Output format" in s
        assert "# Success criteria" in s
        assert "# Boundaries" in s
        assert "# Frame" in s
        assert "# Termination" in s
        assert "submit_final_report" in s
        # Frame name appears in the Frame block
        assert "research — apply research" in s

    def test_hardened_uses_frame_default_output_format(self):
        s = build_subtask_prefix(
            "Research X",
            frame_type="research",
            hardening_enabled=True,
        )
        assert _F061_FRAME_OUTPUT_FORMATS["research"] in s

    def test_hardened_user_output_format_wins_over_default(self):
        s = build_subtask_prefix(
            "Research X",
            frame_type="research",
            output_format="MY CUSTOM FORMAT",
            hardening_enabled=True,
        )
        assert "MY CUSTOM FORMAT" in s
        # Frame default must NOT also appear
        assert _F061_FRAME_OUTPUT_FORMATS["research"] not in s

    def test_hardened_user_success_criteria_wins(self):
        s = build_subtask_prefix(
            "task",
            frame_type=None,
            success_criteria="MY CRITERIA",
            hardening_enabled=True,
        )
        assert "MY CRITERIA" in s
        assert _F061_DEFAULT_SUCCESS_CRITERIA not in s

    def test_hardened_user_boundaries_wins(self):
        s = build_subtask_prefix(
            "task",
            frame_type=None,
            boundaries="MY BOUNDARIES",
            hardening_enabled=True,
        )
        assert "MY BOUNDARIES" in s
        assert _F061_DEFAULT_BOUNDARIES not in s

    def test_hardened_unknown_frame_renders_block_with_freeform_format(self):
        # Hardened mode is informational about ANY frame name (incl. 'research'
        # which is not in FRAME_TOOLS). The output_format falls back to free-form.
        s = build_subtask_prefix(
            "task",
            frame_type="not_a_frame",
            hardening_enabled=True,
        )
        assert "# Frame" in s
        assert "not_a_frame" in s
        assert "Free-form summary" in s

    def test_hardened_no_frame_omits_frame_block(self):
        s = build_subtask_prefix(
            "task",
            frame_type=None,
            hardening_enabled=True,
        )
        assert "# Frame" not in s

    @pytest.mark.parametrize(
        "frame", ["task", "research", "decision", "debug", "conversation"],
    )
    def test_hardened_each_frame_produces_nonempty_default(self, frame):
        s = build_subtask_prefix("t", frame_type=frame, hardening_enabled=True)
        assert _F061_FRAME_OUTPUT_FORMATS[frame] in s

    def test_hardened_prompt_size_is_reasonable(self):
        """Caching budget guard — hardened prompt < 4000 chars per frame."""
        for frame in _F061_FRAME_OUTPUT_FORMATS:
            s = build_subtask_prefix(
                "Some task description here.",
                frame_type=frame,
                hardening_enabled=True,
            )
            assert len(s) < 4000, f"frame={frame} prompt is {len(s)} chars"
