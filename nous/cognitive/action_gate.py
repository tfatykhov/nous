"""F026 Execution Integrity — Phase E: Action Gating.

Tiered gate that approves or blocks tool calls before dispatch:
  Tier 1 (read-only)       → always approved
  Tier 2 (local write)     → duplicate / consistency check
  Tier 3 (external/irrev.) → LLM full gate with 5 s timeout, fail-open
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from nous.cognitive.execution_ledger import classify_side_effect

if TYPE_CHECKING:
    from nous.cognitive.execution_ledger import ExecutionLedger
    from nous.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """Result of an ActionGate check."""

    approved: bool
    reason: str
    suggestion: str | None = None

    @classmethod
    def from_json(cls, text: str) -> GateResult:
        """Parse a GateResult from an LLM response string.

        Expects JSON with keys ``approved`` (bool) and ``reason`` (str).
        Optionally includes ``suggestion`` (str).  On any parse failure the
        gate fails **open** (approved=True) so a bad model response never
        silently blocks legitimate work.
        """
        try:
            # Strip markdown fences if present
            stripped = text.strip()
            if stripped.startswith("```"):
                lines = stripped.splitlines()
                # Drop opening and closing fence lines
                inner = [l for l in lines[1:] if not l.startswith("```")]
                stripped = "\n".join(inner).strip()

            data = json.loads(stripped)
            approved = bool(data.get("approved", True))
            reason = str(data.get("reason", "gate-response"))
            suggestion = str(data["suggestion"]) if "suggestion" in data else None
            return cls(approved=approved, reason=reason, suggestion=suggestion)
        except Exception as exc:  # noqa: BLE001
            logger.warning("GateResult.from_json parse error (%s) — failing open", exc)
            return cls(approved=True, reason=f"gate-parse-error-fail-open: {exc}")


class ActionGate:
    """Tiered pre-dispatch gate for tool calls.

    Parameters
    ----------
    settings:
        Nous settings (used for feature flags / model name).
    call_gate_model:
        Optional async callable ``(prompt: str) -> str`` used for Tier 3
        full-gate checks.  When *None*, Tier 3 always fails open.
    """

    def __init__(
        self,
        settings: Settings,
        call_gate_model: Callable[..., object] | None = None,
    ) -> None:
        self._settings = settings
        self._call_gate_model = call_gate_model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check(
        self,
        tool_name: str,
        tool_input: dict,
        ledger: ExecutionLedger,
        user_message: str = "",
    ) -> GateResult:
        """Gate a proposed tool call.

        Returns a :class:`GateResult` indicating whether the call should
        proceed.  Never raises — all exceptions are caught and the gate fails
        open so infrastructure errors never silently kill legitimate work.
        """
        try:
            side_effect = classify_side_effect(tool_name, tool_input)

            if side_effect == "none":
                # Tier 1: read-only — always approved
                return GateResult(approved=True, reason="read-only")

            if side_effect == "write":
                # Tier 2: local write — duplicate check
                return self._consistency_check(tool_name, tool_input, ledger)

            if side_effect in ("external", "irreversible"):
                # Tier 3: high-stakes — LLM gate with timeout
                return await self._full_gate(tool_name, tool_input, ledger, user_message)

            # Unknown classification — fail open
            return GateResult(approved=True, reason=f"unknown-side-effect:{side_effect}")

        except Exception as exc:  # noqa: BLE001
            logger.warning("ActionGate.check error — failing open: %s", exc)
            return GateResult(approved=True, reason=f"gate-check-error-fail-open: {exc}")

    # ------------------------------------------------------------------
    # Tier 2: consistency / duplicate check
    # ------------------------------------------------------------------

    def _consistency_check(
        self,
        tool_name: str,
        tool_input: dict,
        ledger: ExecutionLedger,
    ) -> GateResult:
        """Block duplicate successful writes with identical arguments."""
        # Summarize new args the same way the ledger summarizes recorded args
        new_key_args = ledger._summarize_args(tool_name, tool_input)  # noqa: SLF001

        recent = [
            a
            for a in ledger.actions[-20:]
            if a.tool_name == tool_name and a.status == "success"
        ]

        for prior in recent:
            if self._args_similar(prior.key_args, new_key_args):
                return GateResult(
                    approved=False,
                    reason=(
                        f"Duplicate: {tool_name} already succeeded with the same"
                        f" arguments on turn {prior.turn}"
                    ),
                    suggestion=(
                        "If you intend a different outcome, adjust the arguments"
                        " or confirm the first result was incorrect."
                    ),
                )

        return GateResult(approved=True, reason="consistency-pass")

    # ------------------------------------------------------------------
    # Tier 3: full LLM gate
    # ------------------------------------------------------------------

    async def _full_gate(
        self,
        tool_name: str,
        tool_input: dict,
        ledger: ExecutionLedger,
        user_message: str,
    ) -> GateResult:
        """Run an LLM gate check with a 5-second timeout, failing open."""
        if not self._call_gate_model:
            return GateResult(approved=True, reason="no-gate-model")

        prompt = self._build_gate_prompt(tool_name, tool_input, ledger, user_message)

        try:
            response = await asyncio.wait_for(
                self._call_gate_model(prompt),  # type: ignore[arg-type]
                timeout=5.0,
            )
            return GateResult.from_json(str(response))
        except TimeoutError:
            logger.warning(
                "ActionGate: gate model call timed out for %s — failing open", tool_name
            )
            return GateResult(approved=True, reason="gate-timeout-fail-open")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ActionGate: gate model call failed for %s (%s) — failing open",
                tool_name,
                exc,
            )
            return GateResult(approved=True, reason=f"gate-error-fail-open: {exc}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _args_similar(
        self,
        prior_args: dict[str, str],
        new_args: dict[str, str],
    ) -> bool:
        """Return True if ANY shared key has matching values.

        Comparison is case-insensitive with whitespace stripped.  Keys that
        represent file paths (``path``, ``file``, ``command``) receive
        additional normalisation: trailing slashes are removed and leading
        ``./`` is stripped so ``./foo.py`` and ``foo.py`` are treated as
        identical.
        """
        PATH_KEYS = {"path", "file", "command"}

        for key in prior_args:
            if key not in new_args:
                continue

            prior_val = prior_args[key].strip().lower()
            new_val = new_args[key].strip().lower()

            if key in PATH_KEYS:
                prior_val = prior_val.rstrip("/").replace("./", "")
                new_val = new_val.rstrip("/").replace("./", "")

            if prior_val == new_val:
                return True

        return False

    def _build_gate_prompt(
        self,
        tool_name: str,
        tool_input: dict,
        ledger: ExecutionLedger,
        user_message: str,
    ) -> str:
        """Build the LLM prompt for a Tier 3 gate check."""
        safe_args = self._safe_args(tool_input)
        args_text = json.dumps(safe_args, ensure_ascii=False)[:500]
        user_text = user_message[:500]
        ledger_text = ledger.system_prompt_section()

        return (
            "You are a safety gate reviewing a proposed tool action.\n"
            "Answer ONLY with valid JSON: {\"approved\": true/false, \"reason\": \"...\"}.\n"
            "Do NOT add any other text.\n\n"
            f"USER REQUEST:\n{user_text}\n\n"
            f"PROPOSED ACTION:\n  tool: {tool_name}\n  args: {args_text}\n\n"
            f"EXECUTION HISTORY:\n{ledger_text}\n\n"
            "Approve if:\n"
            "  - The action clearly aligns with the user's request.\n"
            "  - It has not already been completed successfully.\n"
            "  - There are no obvious red flags (destructive, unintended, out-of-scope).\n"
            "Block if the action is a duplicate, misaligned, or potentially harmful.\n"
            "When in doubt, APPROVE (fail open)."
        )

    def _safe_args(self, tool_input: dict) -> dict:
        """Return a sanitised copy of tool_input safe to include in prompts.

        Removes the ``content`` key (may be large/sensitive) and truncates
        all remaining values to 200 characters.
        """
        result: dict[str, str] = {}
        for k, v in tool_input.items():
            if k == "content":
                continue
            result[k] = str(v)[:200]
        return result
