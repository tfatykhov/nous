"""Construct `Settings` from explicit values only — no .env, no process env.

An eval probe whose result depends on which directory it was launched from is
not an instrument. This was measured, not theorised: the same script over the
same qrels and the same corpus reported a positive control moving 23/57 from a
checkout carrying prod's `.env`, and 0/57 from a worktree without it.

`Settings(_env_file=None)` is NOT sufficient, and that is the whole reason this
module exists. It disables only pydantic-settings' dotenv source;
`EnvSettingsSource` still reads the process environment, so an exported override
survives a call that looks pinned:

    $ NOUS_SPREADING_ACTIVATION_DECAY=0.99 python -c \
        "from nous.config import Settings; \
         print(Settings(_env_file=None).spreading_activation_decay)"
    0.99

Removing the whole `NOUS_`/`DB_` prefix, rather than allowlisting known
retrieval flags, is deliberate: an allowlist has to be extended for every new
flag, and the one nobody remembers to add is exactly the one that silently
varies the measurement.
"""
from __future__ import annotations

import contextlib
import os
from typing import Any

from nous.config import Settings

# Both prefixes Settings reads: NOUS_* (env_prefix) and the unprefixed DB_*
# connection vars, which are shared with docker-compose.
_HIDDEN_PREFIXES = ("NOUS_", "DB_")

# Flags that are `true` in prod and `false` by code default — i.e. exactly the
# ones an ambient-.env run silently flips. A probe that claims to reproduce a
# production code path MUST select a shape explicitly; the two available shapes
# are this one and bare code defaults, and they are not interchangeable.
# Measured 2026-08-24: the difference moved a baseline MRR by 79%.
PROD_SHAPE: dict[str, Any] = {
    # NOT optional. The code default is `text-embedding-3-small`, prod-shaped
    # corpora are embedded with `-large`, and querying 1536-dim vectors written
    # by one model with vectors from the other returns plausible-looking
    # nonsense rather than an error.
    "embedding_model": "text-embedding-3-large",
    # Prod runs `NOUS_RRF_K=30`; the code default is 60. Omitting it made a
    # "PROD SHAPE" run fuse ranks with a constant prod does not use.
    "rrf_k": 30,
    "episode_chunks_enabled": True,
    "chunk_hybrid_search_enabled": True,
    "episode_chunk_recall_limit": 30,
    "heart_graph_all_types_enabled": True,
    "graph_neighbor_seed_score_enabled": True,
    "graph_adjacency_boost_enabled": True,
    "graph_inferred_edge_penalty": 1.0,
    "keyed_fact_leg_enabled": True,
    "exemplar_mode_enabled": True,
}

def eval_off() -> dict[str, Any]:
    """The harness's OWN disable list, not a copy of it.

    `nous_eval.retrieval_runner._EVAL_DISABLE_FIELDS` is what every real eval
    path applies. A probe maintaining its own parallel list drifts silently —
    and a probe that claims to reproduce the harness while disabling a
    different set of handlers is measuring a different system, which is the
    exact failure this module exists to prevent. Imported lazily to keep
    `env_pin` free of a cycle.
    """
    from nous_eval.retrieval_runner import _EVAL_DISABLE_FIELDS

    return dict(_EVAL_DISABLE_FIELDS)


@contextlib.contextmanager
def hidden_env():
    """Temporarily remove NOUS_*/DB_* from `os.environ`.

    Matching is CASE-INSENSITIVE: `Settings` inherits pydantic-settings'
    `case_sensitive=False`, so on a case-sensitive filesystem an exported
    `nous_spreading_activation_decay` is still consumed while sailing past a
    `startswith("NOUS_")` filter.
    """
    saved = {k: v for k, v in os.environ.items()
             if k.upper().startswith(_HIDDEN_PREFIXES)}
    for k in saved:
        del os.environ[k]
    try:
        yield
    finally:
        os.environ.update(saved)


# The env vars `nous.heart.search._resolver_settings()` reads LIVE at query
# time, mapped to the `Settings` fields they shadow. It builds its own bare
# `Settings()`, so a pinned Settings object cannot reach it — the only channel
# is the environment, which is why the pin has to PUBLISH these rather than
# merely hide them. Hiding alone silently swaps in code defaults: prod runs
# `NOUS_RRF_K=30` against a default of 60, and that difference moved a measured
# baseline MRR from 0.1620 to 0.0950.
_RESOLVER_VARS = {
    "NOUS_RRF_K": "rrf_k",
    "NOUS_VECTOR_WEIGHT": "vector_weight",
    "NOUS_HYBRID_SEARCH_KEYWORD_ENABLED": "hybrid_search_keyword_enabled",
}


@contextlib.contextmanager
def pinned_runtime(settings: Settings):
    """Hide ambient config, then publish `settings`' fusion params to the env.

    Wrap the WHOLE probe in this — not just `Settings` construction. Rank
    fusion is resolved per query from `os.environ`, so a probe that only pins
    its `Settings` object still runs someone else's RRF constant.
    """
    with hidden_env():
        for var, field in _RESOLVER_VARS.items():
            value = getattr(settings, field)
            os.environ[var] = (
                str(value).lower() if isinstance(value, bool) else str(value)
            )
        yield


def pinned_settings(**overrides: Any) -> Settings:
    """`Settings` built from code defaults + `overrides`, and nothing else.

    Read anything you need out of `os.environ` (API keys, an eval DB password)
    BEFORE calling, and pass it in as an override — inside the call the
    environment is not visible to `Settings`.

    NOT SUFFICIENT ON ITS OWN. Pinning the `Settings` OBJECT does not pin the
    run: `nous.heart.search._resolver_settings()` builds a fresh bare
    `Settings()` at QUERY time, from three call sites in hybrid search, keyed on
    a fingerprint it reads straight out of `os.environ` —

        NOUS_RRF_K, NOUS_VECTOR_WEIGHT, NOUS_HYBRID_SEARCH_KEYWORD_ENABLED

    — so rank fusion still varies by shell even when every `Settings` field the
    caller passes is pinned. Hold `hidden_env()` around the WHOLE probe, not
    just around construction. Both diagnostic scripts do.
    """
    with hidden_env():
        return Settings(_env_file=None).model_copy(update=overrides)
