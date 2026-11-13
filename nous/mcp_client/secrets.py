"""F112 PR 2c: the MCP client's stored credentials, encrypted (``nous_system.mcp_server_secrets``).

AES-256-GCM (``cryptography``) with the key in ``NOUS_MCP_CLIENT_KEY_FILE``: a file holding 32 random bytes, base64
on one line, kept on the host outside the repository and away from the database's backups. The encryption protects
the database, its dumps and its backups, not the running process: code running in this process (the agent's own
included) can read the key file.

A row's plaintext is canonical JSON, ``{"v":1,"headers":{name: value}}``. Its associated data is recomputed at every
open and never stored: it names the agent, the server's name, the URL the row was sealed for and the row's kind, so a
row whose server was renamed or moved, or whose kind changed, cannot be opened. That is a backstop: the registry
deletes a server's secret when its URL changes (``McpRegistry._upsert_server``).

No rotation: a row sealed with another key is reported as "stored with another key", and the owner enters it again.
Nothing here logs a value, a key or a plaintext, and no exception carries one.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

KEY_BYTES = 32
_NONCE_BYTES = 12
_AAD_PREFIX = b"nous-mcp-client-secret\x00v1\x00"
# Why a stored secret cannot be used (the plan's skip reason is "credentials unavailable: <this>").
NO_KEY = "no key file"
OTHER_KEY = "stored with another key"
UNREADABLE = "cannot be decrypted"

# (agent_id, server name, the URL the row is bound to, kind)
AadFields = tuple[str, str, str, str]


class SecretUnavailable(Exception):
    """A secret that cannot be opened. It carries no detail."""


@dataclass(frozen=True)
class SealedSecret:
    """A credential sealed for one server, ready to be stored as a new row."""

    kind: str
    key_id: str
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)
    header_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class StoredSecret:
    """One ``nous_system.mcp_server_secrets`` row, still sealed. ``id`` is the credential's version: the owner's set or
    replace makes a new row (a new id), a token refresh (PR 2d) updates the row in place (the same id)."""

    id: uuid.UUID
    kind: str
    key_id: str
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)
    header_names: tuple[str, ...]
    updated_at: datetime | None


def key_id_of(key: bytes) -> str:
    """The first 16 hex characters of the key's SHA-256: names the key a row was sealed with."""
    return hashlib.sha256(key).hexdigest()[:16]


def aad_fields(agent_id: str, server: str, url: str, kind: str) -> AadFields:
    """The associated data's fields for a row of ``server`` bound to ``url`` (the URL exactly as stored)."""
    return (agent_id, server, url, kind)


def _aad(fields: AadFields) -> bytes:
    return _AAD_PREFIX + "\x00".join(fields).encode("utf-8")


class SecretBox:
    """AES-256-GCM with one key. ``open`` raises ``SecretUnavailable``, with no detail, for anything that does not
    open: another key, other associated data, a changed or cut ciphertext."""

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError("a key is 32 bytes")
        self._aead = AESGCM(key)
        self.key_id = key_id_of(key)

    def seal(self, fields: AadFields, plaintext: bytes) -> tuple[str, bytes, bytes]:
        """``(key_id, nonce, ciphertext)``, with a new random nonce; the ciphertext includes the GCM tag."""
        nonce = os.urandom(_NONCE_BYTES)
        return self.key_id, nonce, self._aead.encrypt(nonce, plaintext, _aad(fields))

    def open(self, fields: AadFields, key_id: str, nonce: bytes, ciphertext: bytes) -> bytes:
        if key_id != self.key_id:
            raise SecretUnavailable()
        try:
            return self._aead.decrypt(nonce, ciphertext, _aad(fields))
        except Exception:  # InvalidTag, a nonce of the wrong length...: the reason stays out
            raise SecretUnavailable() from None


class KeyFile:
    """``NOUS_MCP_CLIENT_KEY_FILE``: read at first use, and again at every use while no key is loaded (a key file
    put in place later is picked up without a restart). Anything but 32 bytes of base64 means no key: one WARNING
    naming the setting and the reason, never the content, each time the reason changes. An empty setting is silent:
    stored credentials are simply not configured."""

    def __init__(self, path: str) -> None:
        self._path = path.strip()
        self._box: SecretBox | None = None
        self._reported: str | None = None

    def box(self) -> SecretBox | None:
        """The loaded key's box, or None when there is no usable key."""
        if self._box is None and self._path:
            self._box = self._load()
        return self._box

    def _load(self) -> SecretBox | None:
        problem: str
        try:
            raw = Path(self._path).read_bytes()
        except FileNotFoundError:
            problem = "the file does not exist"
        except OSError:
            problem = "the file cannot be read"
        else:
            try:
                key = base64.b64decode(raw.strip(), validate=True)
            except (binascii.Error, ValueError):
                problem = "the file does not hold base64"
            else:
                if len(key) == KEY_BYTES:
                    return SecretBox(key)
                problem = f"the key is not {KEY_BYTES} bytes"
        if problem != self._reported:
            self._reported = problem
            logger.warning("MCP client: NOUS_MCP_CLIENT_KEY_FILE: %s, so stored credentials cannot be used", problem)
        return None


def headers_plaintext(headers: Mapping[str, str]) -> bytes:
    """The canonical plaintext of a header map."""
    return json.dumps({"v": 1, "headers": dict(headers)}, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _headers_of(plaintext: bytes) -> dict[str, str]:
    try:
        record = json.loads(plaintext)
    except ValueError:
        raise SecretUnavailable() from None
    headers = record.get("headers") if isinstance(record, dict) and record.get("v") == 1 else None
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise SecretUnavailable()
    return headers


def seal_headers(box: SecretBox, fields: AadFields, headers: Mapping[str, str]) -> SealedSecret:
    """``headers`` sealed for the row ``fields`` describe (its kind must be ``headers``)."""
    key_id, nonce, ciphertext = box.seal(fields, headers_plaintext(headers))
    return SealedSecret(fields[3], key_id, nonce, ciphertext, tuple(headers))


def open_headers(
    box: SecretBox | None, fields: AadFields, secret: StoredSecret
) -> tuple[dict[str, str] | None, str | None]:
    """``(headers, None)``, or ``(None, reason)``: ``NO_KEY``, ``OTHER_KEY`` or ``UNREADABLE`` (a row whose name, URL
    or kind is not the one it was sealed for, a damaged row, or a kind this version cannot use)."""
    if box is None:
        return None, NO_KEY
    if secret.key_id != box.key_id:
        return None, OTHER_KEY
    try:
        return _headers_of(box.open(fields, secret.key_id, secret.nonce, secret.ciphertext)), None
    except SecretUnavailable:
        return None, UNREADABLE
