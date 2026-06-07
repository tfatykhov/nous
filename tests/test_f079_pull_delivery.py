"""F079 P1 — pull-path delivery: richer recall_deep procedure bodies + awareness cue.

The body-formatting logic is a pure helper (no DB), unit-tested here. The flag-gated
awareness section + live recall_deep behavior are validated on the eval instance.
"""

from nous.cognitive.context import SECTION_TIERS
from nous.heart.heart import _format_procedure_recall_summary
from nous.heart.schemas import ProcedureSummary


def _proc(**kw) -> ProcedureSummary:
    base = dict(
        id="00000000-0000-0000-0000-000000000001",
        name="send-email",
        domain="comms",
        description="Send email via the guarded tool",
        activation_count=5,
        effectiveness=0.9,
        score=0.7,
    )
    base.update(kw)
    return ProcedureSummary(**base)


class TestRecallSummary:
    def test_off_is_byte_identical_legacy(self):
        p = _proc(implementation_notes=["step one", "step two"])
        assert _format_procedure_recall_summary(p, False, 240) == "send-email: Send email via the guarded tool"

    def test_off_no_description(self):
        p = _proc(description=None)
        assert _format_procedure_recall_summary(p, False, 240) == "send-email"

    def test_on_appends_body(self):
        p = _proc(implementation_notes=["use the send_email tool", "verify recipient"])
        out = _format_procedure_recall_summary(p, True, 240)
        assert out.startswith("send-email: Send email via the guarded tool | ")
        assert "use the send_email tool" in out and "verify recipient" in out

    def test_on_collapses_newlines_to_one_line(self):
        # codex-relevant: verbatim multiline fields must NOT produce multiple lines
        # (would be shredded by SmartCompress).
        p = _proc(implementation_notes=["line a\n- embedded bullet\nline b"])
        out = _format_procedure_recall_summary(p, True, 240)
        assert "\n" not in out
        assert "line a - embedded bullet line b" in out

    def test_on_caps_length_with_ellipsis(self):
        p = _proc(implementation_notes=["x" * 1000])
        out = _format_procedure_recall_summary(p, True, 50)
        body = out.split(" | ", 1)[1]
        assert len(body) <= 51  # 50 + the ellipsis char
        assert body.endswith("…")

    def test_on_empty_body_no_separator(self):
        p = _proc(implementation_notes=[], core_patterns=[])
        assert _format_procedure_recall_summary(p, True, 240) == "send-email: Send email via the guarded tool"

    def test_on_includes_core_patterns_after_notes(self):
        p = _proc(implementation_notes=["note1"], core_patterns=["pattern1"])
        out = _format_procedure_recall_summary(p, True, 240)
        assert out.index("note1") < out.index("pattern1")


class TestAwarenessTier:
    def test_awareness_section_is_static_tier(self):
        # Static => cached, never busts (the caching invariant of the design).
        assert SECTION_TIERS.get("Procedure Awareness") == "static"
