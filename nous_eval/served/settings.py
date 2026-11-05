"""The Settings one arm runs with, and what a run records about them.

Layers, later wins:

1. code defaults (``Settings(_env_file=None)`` with every NOUS_*/DB_* variable hidden);
2. each ``--base`` file in order: a ``.json`` snapshot (``{"settings": {field: value}}``, as an
   allowlisted reader inside the prod container writes it) or a dotenv file of ``NOUS_*`` flags
   (a duplicate name: the last line wins);
3. the eval disables (``nous_eval.retrieval_runner._EVAL_DISABLE_FIELDS``: event bus, sleep,
   extraction and other writers), except query expansion, which keeps the base's value;
4. the arm's ``--set NOUS_NAME=VALUE`` overrides (one variable per arm);
5. the database connection and agent id of the run.

Layers only collect requests (an env string, or a snapshot value); Settings parses and validates
once, on the merged configuration, so a cross-field validator sees base and arm together, and a
validator that changed a requested value stops the run.

Names that look like credentials, endpoints or connection fields are never taken from a layer
(``NEVER_APPLY``) and never recorded. The record holds, for every applied field and a fixed list
of retrieval knobs, the effective value and the layer it came from.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NEVER_APPLY = re.compile(
    r"^db_|^agent_id$|api_key|_key$|(^|_)token$|secret|passw|credential|service_account|webhook|chat_id|email"
    r"|cookie|_urls?$|_hosts?$|_dsn$",
    re.IGNORECASE,
)

# Always recorded, whatever the layers name: the knobs every served-precision arm depends on.
RECORD_ALWAYS = (
    "embedding_model", "embedding_dimensions", "vector_weight", "rrf_k", "hybrid_search_keyword_enabled",
    "hybrid_keyword_or_fallback_enabled", "cross_encoder_enabled", "cross_encoder_model",
    "cross_encoder_max_candidates", "cross_encoder_text_limit", "query_expansion_enabled", "date_leg_enabled",
    "date_leg_inline_rrf", "keyed_fact_leg_enabled", "spreading_activation_enabled", "graph_recall_enabled",
    "episode_chunks_enabled", "recall_include_parent_episodes", "residual_activation_enabled", "mmr_enabled",
    "relevance_floor_enabled", "context_query_mode", "context_budget_overrides",
    "context_episode_untagged_diversity_cap", "profile_card_enabled",
)  # fmt: skip


@dataclass
class ResolvedSettings:
    """A run's Settings plus the provenance a run record needs."""

    settings: Any
    sources: dict[str, str]
    arm_overrides: dict[str, Any]
    ignored: dict[str, list[str]] = field(default_factory=dict)
    base_files: list[dict[str, str]] = field(default_factory=list)

    def record(self) -> dict[str, Any]:
        """Effective value and source of every applied field plus RECORD_ALWAYS; never a dump."""
        fields = type(self.settings).model_fields
        names = sorted((set(self.sources) | set(RECORD_ALWAYS)) & set(fields))
        return {
            n: {"value": _jsonable(getattr(self.settings, n)), "source": self.sources.get(n, "default")}
            for n in names
            if not NEVER_APPLY.search(n)
        }


def _jsonable(v: Any) -> Any:
    if isinstance(v, (set, frozenset, tuple)):
        return sorted(v, key=str) if isinstance(v, (set, frozenset)) else list(v)
    if isinstance(v, (str, int, float, bool, type(None), list, dict)):
        return v
    return str(v)


def env_name_to_field() -> dict[str, str]:
    """``NOUS_CONTEXT_QUERY_MODE`` -> ``context_query_mode``, aliases included."""
    from nous_eval.beam.arms import env_name_to_field as _map

    return _map()


# A requested value: ("env", NOUS_NAME, string) as a dotenv or --set gives it, or ("init", value)
# as a JSON snapshot or the eval disables give it. Layers only collect requests; Settings parses
# and validates once.
Request = tuple


def _init_key(name: str) -> str:
    """The keyword Settings(...) accepts for a field: its alias when it has one (an aliased field
    passed by name is silently ignored, extra='ignore')."""
    from nous.config import Settings

    alias = Settings.model_fields[name].validation_alias
    return alias if isinstance(alias, str) else name


@functools.cache
def _field_parsed() -> type:
    """Settings with its cross-field (model) validators replaced by no-ops: every field is read and
    field-validated exactly as Settings reads it, and nothing is adjusted for another field."""
    from nous.config import Settings

    validators = Settings.__pydantic_decorators__.model_validators
    if any(d.info.mode != "after" for d in validators.values()):
        raise RuntimeError("Settings has a before/wrap model validator; _field_parsed cannot neutralize it")
    return type("_FieldParsed", (Settings,), {name: (lambda self: self) for name in validators})


def _build(cls: type, requests: dict[str, Request]) -> Any:
    """One construction of ``cls`` from the merged requests, every NOUS_*/DB_* variable hidden."""
    from nous_eval.env_pin import hidden_env

    env = {r[1]: r[2] for r in requests.values() if r[0] == "env"}
    init = {_init_key(f): r[1] for f, r in requests.items() if r[0] == "init"}
    with hidden_env():
        os.environ.update(env)
        return cls(_env_file=None, **init)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_base_layer(path: Path) -> tuple[dict[str, Request], list[str]]:
    """(field -> request, ignored names) of one base file; nothing is parsed here."""
    from nous.config import Settings

    fields = Settings.model_fields
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("settings", data)
        keep = {k: ("init", v) for k, v in raw.items() if k in fields and not NEVER_APPLY.search(k)}
        return keep, sorted(k for k in raw if k not in keep)
    from dotenv import dotenv_values

    known = env_name_to_field()
    out: dict[str, Request] = {}
    ignored = []
    for name, value in dotenv_values(path).items():  # a repeated name: the last value wins
        f = known.get(name.upper())
        if value is None or f is None or NEVER_APPLY.search(f):
            ignored.append(name)
        else:
            out[f] = ("env", name.upper(), value)
    return out, sorted(set(ignored))


def override_requests(pairs: list[str]) -> dict[str, Request]:
    """``["NOUS_X=1", ...]`` -> field -> request. Unknown and never-applied names are errors."""
    known = env_name_to_field()
    out: dict[str, Request] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        name = name.strip().upper()
        if not sep or not name:
            raise SystemExit(f"--set expects NOUS_NAME=VALUE, got {pair!r}")
        f = known.get(name)
        if f is None:
            raise SystemExit(f"--set {name}: Settings has no such variable")
        if NEVER_APPLY.search(f):
            raise SystemExit(f"--set {name}: connection, credential and agent settings are not arm settings")
        out[f] = ("env", name, value)
    return out


def parse_overrides(pairs: list[str]) -> dict[str, Any]:
    """``["NOUS_X=1", ...]`` -> typed ``{field: value}`` as requested (no cross-field validation)."""
    requests = override_requests(pairs)
    parsed = _build(_field_parsed(), requests)
    return {f: getattr(parsed, f) for f in requests}


def resolve(
    base_paths: list[Path],
    set_pairs: list[str],
    *,
    db: dict[str, Any],
    agent_id: str,
) -> ResolvedSettings:
    """Apply the layers (module docstring): collect each layer's raw requests (a later layer wins
    per field), then parse and validate once, on the merged configuration, so a cross-field
    validator sees the base and the arm together. Every requested value must survive: a
    validator that changed one stops the run, naming it."""
    from nous.config import Settings
    from nous_eval.retrieval_runner import _EVAL_DISABLE_FIELDS

    requests: dict[str, Request] = {}
    sources: dict[str, str] = {}
    ignored: dict[str, list[str]] = {}
    base_files = []
    for p in base_paths:
        reqs, ign = load_base_layer(p)
        requests.update(reqs)
        sources.update(dict.fromkeys(reqs, f"base:{p.name}"))
        ignored[p.name] = ign
        base_files.append({"name": p.name, "sha256": _sha256(p)})
    for name, value in _EVAL_DISABLE_FIELDS:
        if name != "query_expansion_enabled":
            requests[name] = ("init", value)
            sources[name] = "eval"
    arm = override_requests(set_pairs)
    requests.update(arm)
    sources.update(dict.fromkeys(arm, "arm"))
    merged = _build(Settings, requests)
    asked = _build(_field_parsed(), requests)  # the same requests, no cross-field adjustment
    for f in requests:  # pin the run: the object must hold what each layer asked for
        if getattr(merged, f) != getattr(asked, f):
            raise SystemExit(
                f"setting {f}: {sources[f]} asked for {getattr(asked, f)!r}, the merged Settings hold "
                f"{getattr(merged, f)!r} (a validator changed it; set its prerequisite too)"
            )
    s = merged.model_copy(update={**db, "agent_id": agent_id})
    return ResolvedSettings(s, sources, {f: getattr(s, f) for f in arm}, ignored, base_files)
