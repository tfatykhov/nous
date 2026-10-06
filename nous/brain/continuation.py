"""F099 Phase 2: the continuation store (data and routing; the runner follows).

Phase 2b puts the data and the routing here: the inbox primitives, the
same-transaction move of an intention to ``result_ready``, owner-facing rows,
and the startup rollback. The runner, the claim, proposals and cancel are
later PRs and fill this module in. Callers use the module
(``continuation.record_result(...)``), not its names, so one monkeypatch reaches
every writer.
"""

from __future__ import annotations

# Flipped to True by PR-2e, in the commit that wires the runner into main.py.
# While it is False, main.py forces NOUS_CONTINUATION_ENABLED off: with the flag
# on and no runner, a continue result is written NULL-keyed and nothing claims it.
CONTINUATION_RUNNER_READY: bool = False
