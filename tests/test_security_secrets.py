"""scan_secrets: credential formats it must catch, and prose it must let through.

Every token is assembled at runtime from low-entropy pieces so the repo never
holds a string that looks like a live credential to a secret scanner.
"""

from __future__ import annotations

import pytest

from nous.security.secrets import scan_secrets

_HITS = [
    ("legacy sk key", "key: " + "sk-" + "a1B2" * 6),
    ("aws access key", "AKIA" + "ABCD" * 4),
    ("password colon", "password: hunter2"),
    ("password equals", "DB_PASSWORD=hunter2"),
    ("bearer", "Authorization: Bearer " + "abc." * 6),
    ("anthropic key", "ANTHROPIC=" + "sk-" + "ant-" + "api03-" + "Xy_9-" * 8),
    ("openai project key", "sk-" + "proj-" + "Ab3_" * 8),
    ("github classic pat", "token " + "gh" + "p_" + "Ab3D" * 9),
    ("github oauth", "gh" + "o_" + "Ab3D" * 9),
    ("github server token", "gh" + "s_" + "Ab3D" * 9),
    ("github fine-grained pat", "gith" + "ub_pat_" + "11ABCDEFG" + "0" * 13 + "_" + "Ab3D" * 12),
    ("telegram bot token", "bot token 123456789:" + "AAh" + "x" * 32),
    ("postgres dsn", "postgresql://nous:" + "s3cr3t" + "@db.internal:5432/nous"),
    ("https basic auth", "https://deploy:" + "tok" * 4 + "@git.example.com/repo.git"),
    ("jwt", "eyJ" + "hbGciOiJIUzI1" + ".eyJ" + "zdWIiOiIxMjM0" + "." + "SflKxwRJSMeK" * 2),
    ("env api key", "OPENAI_API_KEY=" + "abcd1234" * 3),
    ("yaml api_key", "api_key: " + "Zq9" * 7),
    ("json api_key", '{"api_key": "' + "Zq9" * 7 + '"}'),
    ("header x-api-key", "x-api-key: " + "Zq9-" * 5),
    ("slack bot token", "SLACK=" + "xox" + "b-" + "1234567890-1234567890-" + "Ab3D" * 6),
    ("slack user token", "xox" + "p-" + "1234567890-1234567890-" + "Ab3D" * 6),
    ("slack app token", "xox" + "a-" + "2-" + "1234567890-" + "Ab3D" * 6),
    ("google api key", "key=" + "AI" + "za" + "Sy" + "Ab3_-" * 6 + "Ab3"),
]

_MISSES = [
    ("prose with sk words", "We will review the risk-management-framework-review doc."),
    ("hyphenated slug", "See desk-organization-guidelines for the layout."),
    ("url with port", "Open https://example.com:8080/path?q=1 in a browser."),
    ("url with user only", "Clone ssh://git@github.com/org/repo.git"),
    ("timestamps", "The meeting moved from 12:30 to 14:45 on 2026-10-04."),
    ("uuid and sha", "Subtask 3fa9c1d2-7b4e-4c1a-9f0e-0123456789ab at commit 6e1893765ab2c4d"),
    ("api key prose", "The API key is in the vault; ask the user for it."),
    ("api_key short value", "api_key: see the vault"),
    ("api_key from env", 'api_key = os.environ["SERVICE_KEY"]'),
    ("api_key template", "API_KEY=${API_KEY}"),
    ("ratio", "Throughput 1234567890:1 is impossible."),
    ("short telegram-like", "123456789:short"),
    ("eyJ word", "eyJust a coincidence.eyJ nope"),
    ("slack prefix prose", "A bot token starts with xoxb- and a user token with xoxp-."),
    ("google prefix prose", "A Google key starts with AIza; Aizawl is a city."),
]


@pytest.mark.parametrize("text", [c[1] for c in _HITS], ids=[c[0] for c in _HITS])
def test_scan_secrets_catches(text):
    assert scan_secrets(text) is True


@pytest.mark.parametrize("text", [c[1] for c in _MISSES], ids=[c[0] for c in _MISSES])
def test_scan_secrets_lets_prose_through(text):
    assert scan_secrets(text) is False
