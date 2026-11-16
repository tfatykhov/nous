"""The owner's secret for the diagnostic scripts' calls to chat and to the sleep trigger.

Chat requires the owner's secret when the API runs with ``NOUS_OWNER_ENTRY_MODE=enforce``; the sleep trigger requires
the owner's or the proxy's secret in every mode. A script sends it from ``NOUS_OWNER_ROUTE_SECRET`` in the shell that
runs it, and sends no header when that is not set.
"""

from __future__ import annotations

import os


def owner_headers() -> dict[str, str]:
    """``{"X-Nous-Owner-Secret": ...}`` from ``NOUS_OWNER_ROUTE_SECRET``, or ``{}`` when it is not set."""
    secret = os.environ.get("NOUS_OWNER_ROUTE_SECRET", "")
    return {"X-Nous-Owner-Secret": secret} if secret else {}
