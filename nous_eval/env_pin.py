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


@contextlib.contextmanager
def hidden_env():
    """Temporarily remove NOUS_*/DB_* from `os.environ`."""
    saved = {k: v for k, v in os.environ.items()
             if k.startswith(_HIDDEN_PREFIXES)}
    for k in saved:
        del os.environ[k]
    try:
        yield
    finally:
        os.environ.update(saved)


def pinned_settings(**overrides: Any) -> Settings:
    """`Settings` built from code defaults + `overrides`, and nothing else.

    Read anything you need out of `os.environ` (API keys, an eval DB password)
    BEFORE calling, and pass it in as an override — inside the call the
    environment is not visible to `Settings`.
    """
    with hidden_env():
        return Settings(_env_file=None).model_copy(update=overrides)
