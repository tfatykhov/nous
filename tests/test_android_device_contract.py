"""Device pairing: the Android app and the server must agree on the wire.

The same reasoning as `test_android_push_contract.py` next door: two
codebases, one contract, and nothing else checks it. A header the server
does not read is a phone that never proves itself; a refusal code the app
does not know is a phone that retries for ever; a redeem field with another
name is a pairing that always fails. This file reads the Kotlin source
literally and compares it with the server's.

The device routes live in `nous/api/companion_devices.py`; the checks that
need it skip only when that module is not in the tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "android" / "core" / "src" / "main" / "kotlin" / "us" / "fatykhov" / "nous" / "companion" / "core"
PAIRING = CORE / "Pairing.kt"
REST = ROOT / "nous" / "api" / "rest.py"
OWNER_GUARD = ROOT / "nous" / "api" / "owner_guard.py"
ACTIONS = ROOT / "nous" / "a2ui" / "actions.py"
DEVICES = ROOT / "nous" / "api" / "companion_devices.py"


def _text(path: Path) -> str:
    if not path.is_file():
        pytest.skip(f"not in this tree: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def _const(source: str, name: str) -> str:
    (value,) = re.findall(rf'const val {name} = "([^"]+)"', source)
    return value


def _redeem_block(kotlin: str) -> str:
    start = kotlin.index("suspend fun redeem(")
    return kotlin[start : kotlin.index("suspend fun unpair(", start)]


def test_the_app_sends_the_header_the_server_reads() -> None:
    (server,) = re.findall(r'^DEVICE_TOKEN_HEADER = "([^"]+)"', _text(OWNER_GUARD), re.MULTILINE)
    assert _const(_text(PAIRING), "HEADER") == server


def _app_codes() -> dict[str, str]:
    return dict(re.findall(r'"([A-Z_]+)" -> (NOT_PAIRED|TOKEN_REJECTED)', _text(PAIRING)))


def test_the_app_reads_the_routers_missing_proof_code() -> None:
    assert '"OWNER_PROOF_REQUIRED"' in _text(ACTIONS)
    assert _app_codes().get("OWNER_PROOF_REQUIRED") == "NOT_PAIRED"


def test_the_app_reads_the_guards_rejected_token_code() -> None:
    _text(DEVICES)  # the code arrives with the device routes
    (rejected,) = re.findall(r'^DEVICE_TOKEN_REJECTED_CODE = "([^"]+)"', _text(OWNER_GUARD), re.MULTILINE)
    assert _app_codes().get(rejected) == "TOKEN_REJECTED"


def test_the_redeem_body_is_the_one_the_server_reads() -> None:
    sent = set(re.findall(r'put\("(\w+)"', _redeem_block(_text(PAIRING))))
    devices = _text(DEVICES)
    block = devices[devices.index("class _RedeemBody") :]
    block = block[: block.index("\n\n\n")]
    read = set(re.findall(r"^    (\w+): ", block, re.MULTILINE))
    assert sent == read, f"app sends {sorted(sent)}, server reads {sorted(read)}"


def test_the_redeem_answer_has_every_field_the_app_reads() -> None:
    read = set(re.findall(r'o\["(\w+)"\]', _redeem_block(_text(PAIRING)))) - {"error"}
    devices = _text(DEVICES)
    handler = devices[devices.index("async def redeem(") :][:2500]
    payload = handler[handler.index("payload = {") : handler.index("}", handler.index("payload = {"))]
    sent = set(re.findall(r'"(\w+)":', payload))
    assert read <= sent, f"the app reads {sorted(read - sent)} that the redeem answer never has"


def test_every_device_endpoint_the_app_calls_exists_on_the_server() -> None:
    """(verb, path) pairs: a right path with a wrong verb is a 405."""
    kotlin = _text(PAIRING)
    client = {
        ("POST", _const(kotlin, "REDEEM_PATH")),
        ("POST", _const(kotlin, "UNPAIR_PATH")),
    }
    assert re.search(r"http\.postJson\(DeviceToken\.REDEEM_PATH", kotlin)
    assert re.search(r"http\.postJson\(DeviceToken\.UNPAIR_PATH", kotlin)
    server = set()
    for path, methods in re.findall(
        r'(?:Route|\.route)\(\s*"(/a2ui/devices[^"]*)",[^\n]*?methods=\[([^\]]*)\]', _text(DEVICES)
    ):
        for verb in re.findall(r'"([A-Z]+)"', methods):
            server.add((verb, path))
    missing = client - server
    assert not missing, (
        f"the app calls device endpoints the server does not serve: {sorted(missing)}; server has {sorted(server)}"
    )


def test_unpair_deletes_the_push_registration_on_a_route_the_server_serves() -> None:
    """Unpair and Disconnect delete this installation's push row with DELETE."""
    kotlin = _text(PAIRING)
    (path,) = re.findall(r'http\.delete\("(/a2ui/push/tokens/)" \+', kotlin)
    rest = REST.read_text(encoding="utf-8")
    assert re.search(r'Route\(\s*"' + re.escape(path) + r'\{[^}]+\}",\s*\w+,\s*methods=\["DELETE"\]', rest)
