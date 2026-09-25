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


# --- harness Phase 2b: the send stage is classified; QUIT never overrides it ---

import smtplib  # noqa: E402
from email.message import EmailMessage  # noqa: E402

import pytest  # noqa: E402


class _SMTP:
    def __init__(self, fail_send=None, fail_quit=None, refused=None):
        self.fail_send, self.fail_quit, self.refused = fail_send, fail_quit, refused or {}

    def __call__(self, host, port, timeout):
        return self

    def starttls(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg, to_addrs=None):
        if self.fail_send:
            raise self.fail_send
        return self.refused

    def quit(self):
        if self.fail_quit:
            raise self.fail_quit


def _settings():
    return Settings(_env_file=None, email_user="u", email_password="p")


def _msg():
    msg = EmailMessage()
    msg["To"], msg["Subject"] = "a@x.io", "s"
    return msg


@pytest.mark.parametrize("error, uncertain", [
    (TimeoutError("reply"), True),
    (smtplib.SMTPServerDisconnected("gone"), True),
    (ConnectionResetError("rst"), True),
    (smtplib.SMTPDataError(554, b"rejected"), False),
    (smtplib.SMTPRecipientsRefused({}), False),
])
def test_send_stage_errors_are_classified(monkeypatch, error, uncertain):
    from nous.api.email_tools import DeliveryUncertain

    monkeypatch.setattr("nous.api.email_tools.smtplib.SMTP", _SMTP(fail_send=error, fail_quit=TimeoutError()))
    with pytest.raises(DeliveryUncertain if uncertain else type(error)):
        _send_email_sync(_settings(), ["a@x.io"], _msg())


def test_a_failing_quit_after_success_is_swallowed(monkeypatch):
    monkeypatch.setattr("nous.api.email_tools.smtplib.SMTP", _SMTP(fail_quit=TimeoutError()))
    assert _send_email_sync(_settings(), ["a@x.io"], _msg()) == {}


def test_refused_recipients_are_returned(monkeypatch):
    refused = {"b@x.io": (550, b"no such user")}
    monkeypatch.setattr("nous.api.email_tools.smtplib.SMTP", _SMTP(refused=refused))
    assert _send_email_sync(_settings(), ["a@x.io", "b@x.io"], _msg()) == refused


def test_a_login_failure_is_definite(monkeypatch):
    class _BadLogin(_SMTP):
        def login(self, u, p):
            raise smtplib.SMTPAuthenticationError(535, b"bad creds")

    monkeypatch.setattr("nous.api.email_tools.smtplib.SMTP", _BadLogin())
    with pytest.raises(smtplib.SMTPAuthenticationError):
        _send_email_sync(_settings(), ["a@x.io"], _msg())
