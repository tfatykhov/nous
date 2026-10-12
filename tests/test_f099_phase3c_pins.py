"""F099 Phase 3c-7: Ledger.svelte's context filter lists exactly the ContextKinds (contract 5.13)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from nous.api.execution_context import ContextKind

LEDGER = Path(__file__).resolve().parent.parent / "dashboard-app" / "src" / "views" / "Ledger.svelte"


def _contexts() -> list[str]:
    source = LEDGER.read_text(encoding="utf-8")
    block = re.search(r"const CONTEXTS = \[(.*?)\];", source, re.S)
    assert block, "Ledger.svelte no longer declares CONTEXTS as an array literal"
    return re.findall(r"'([a-z_]+)'", block.group(1))


def test_ledger_contexts_equal_the_context_kinds():  # PIN
    """Mutation: drop 'continuation' from CONTEXTS (or add a kind to ContextKind) and this fails."""
    assert _contexts() == list(get_args(ContextKind))
