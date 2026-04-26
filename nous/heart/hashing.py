"""Canonical input hashing — shared by F047 Phase 3 (planned) and F050 cache.

Stable digest semantics: ``sha256(NFKC-normalize -> lowercase -> strip)`` -> 32 bytes.
Returned as bytes for direct binding to ``BYTEA`` columns.

NFKC normalization (F050 plan v2 — devil P1) defends against:
  - NFC vs NFD cache misses (same visual char, different bytes)
  - ZWS / bidi / NBSP slipping past ``.strip()`` and creating unbounded
    cache rows for visually-identical adversarial queries
  - Compatibility decompositions (e.g. ``½`` -> ``1/2``, ``ﬁ`` -> ``fi``)
"""

from __future__ import annotations

import hashlib
import unicodedata


def canonical_input_hash(text: str) -> bytes:
    """Return SHA-256 of the canonicalized text as 32 raw bytes.

    Canonicalization order (must stay stable — F047 Phase 3 will import this):
      1. NFKC Unicode normalization (compatibility-decomposition + canonical-combine)
      2. lowercase
      3. strip leading/trailing whitespace

    Used by:
      - F050 ``heart.query_expansions.input_hash``
      - F047 Phase 3 (planned) ``classifier_input_hash``
    """
    canonical = unicodedata.normalize("NFKC", text).lower().strip()
    return hashlib.sha256(canonical.encode("utf-8")).digest()
