"""F116 PR 2: the self-initiated check's filters on their own (nous/heartbeat/pending_filter.py).

Transport type, the pure rules (no I/O, an injected clock), the filter with its batched read replaced, and the batched
read against real rows. The check with the filters wired in is tested in tests/test_heartbeat_stale_pending.py.
"""

from __future__ import annotations

from nous.heartbeat.schemas import CheckResult, SuppressionReport


def _add(report: SuppressionReport, n: int, *, shadow: bool, reason: str = "kind_rule") -> None:
    for i in range(n):
        report.add(reason, shadow=shadow, fingerprint=f"{i:016x}", kind="fact", item_id=f"id-{i}")


class TestSuppressionReport:
    def test_a_new_report_is_empty_and_false(self):
        report = SuppressionReport()
        assert not report
        assert (report.suppressed, report.shadow_suppressed, report.items, report.shadow_items, report.errors) == (
            {},
            {},
            [],
            [],
            0,
        )

    def test_add_counts_by_reason_and_keeps_the_item(self):
        report = SuppressionReport()
        report.add("age_fact", shadow=False, fingerprint="f" * 16, kind="fact", item_id="x")
        report.add("age_fact", shadow=False, fingerprint="e" * 16, kind="fact", item_id="y")
        report.add("kind_rule", shadow=True, fingerprint="d" * 16, kind="fact", item_id="z")

        assert report
        assert report.suppressed == {"age_fact": 2}
        assert report.shadow_suppressed == {"kind_rule": 1}
        assert report.items == [
            {"fingerprint": "f" * 16, "kind": "fact", "id": "x", "reason": "age_fact"},
            {"fingerprint": "e" * 16, "kind": "fact", "id": "y", "reason": "age_fact"},
        ]
        assert report.shadow_items == [{"fingerprint": "d" * 16, "kind": "fact", "id": "z", "reason": "kind_rule"}]

    def test_each_list_keeps_its_first_twenty_and_the_counts_stay_complete(self):
        report = SuppressionReport()
        _add(report, 30, shadow=False)
        _add(report, 25, shadow=True, reason="age_snapshot")

        assert report.suppressed == {"kind_rule": 30}
        assert report.shadow_suppressed == {"age_snapshot": 25}
        assert len(report.items) == 20 and len(report.shadow_items) == 20
        assert [i["id"] for i in report.items] == [f"id-{i}" for i in range(20)]

    def test_errors_alone_make_the_report_true(self):
        report = SuppressionReport()
        report.errors = 1
        assert report

    def test_merge_sums_counts_and_errors_and_caps_the_lists(self):
        tick = SuppressionReport()
        _add(tick, 15, shadow=False)
        tick.errors = 1
        run = SuppressionReport()
        _add(run, 10, shadow=False, reason="age_fact")
        _add(run, 3, shadow=True)
        run.errors = 2

        tick.merge(run)

        assert tick.suppressed == {"kind_rule": 15, "age_fact": 10}
        assert tick.shadow_suppressed == {"kind_rule": 3}
        assert tick.errors == 3
        assert len(tick.items) == 20
        assert [i["reason"] for i in tick.items[15:]] == ["age_fact"] * 5
        assert len(tick.shadow_items) == 3

    def test_merge_does_not_change_the_other_report(self):
        tick, run = SuppressionReport(), SuppressionReport()
        _add(run, 2, shadow=True)
        tick.merge(run)
        tick.add("age_fact", shadow=True, fingerprint="0" * 16, kind="fact", item_id="q")
        assert run.shadow_suppressed == {"kind_rule": 2}
        assert len(run.shadow_items) == 2


class TestCheckResult:
    def test_a_check_result_carries_no_report_by_default(self):
        assert CheckResult().suppression is None
        assert CheckResult(has_updates=True, findings=[]).suppression is None
