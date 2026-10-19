"""The owner's sampled audits of completion checks the agent wrote itself (scorer v5).

``check_audit.CheckAuditSampler`` pushes a weekly companion card of a few finished subtasks whose own DAG node check
passed, and the ``audit.verdict`` action (``nous/a2ui/actions.py``) records the owner's answer in
``nous_system.check_audits`` (migration 090). The task scorer reads those rows (``task_score_gatherer``).

This module holds only names the app and the scoring job share, so the job can import it without the sampler.
"""

AUDIT_VERDICT_ACTION = "audit.verdict"  # the companion action that records a verdict
