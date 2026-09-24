"""Harness Phase 0: the SMTP connection is bounded.

smtplib.SMTP had no timeout and nothing set socket.setdefaulttimeout, so a
hung SMTP server held a to_thread worker forever — and a caller's timeout
cancelled only the await, not the thread, which could still deliver later.
"""

from unittest.mock import MagicMock, patch

from nous.api.email_tools import _send_email_sync
from nous.config import Settings


def test_smtp_connection_is_opened_with_the_configured_timeout():
    settings = Settings(
        _env_file=None, email_user="u", email_password="p", email_smtp_timeout_seconds=17,
    )
    with patch("nous.api.email_tools.smtplib.SMTP") as smtp:
        smtp.return_value = MagicMock()
        _send_email_sync(settings, ["a@example.com"], MagicMock())
    assert smtp.call_args.kwargs["timeout"] == 17


def test_default_timeout_is_finite():
    assert 0 < Settings(_env_file=None).email_smtp_timeout_seconds <= 120
