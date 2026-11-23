"""F118 PR 3: the second judge's settings on the dashboard (``NOUS_OUTSIDE_JUDGE_SETTINGS_ENABLED``).

The owner switches the second judge (Jev) on and off, sets its model and its daily limit, and saves its API key, from
the Autonomy screen's card (``nous/api/judge_second_routes.py``). The values are stored in
``nous_system.judge_second_settings`` (migration 122), one row per agent; each is NULL for the server's value.

How the values combine (``resolve``): a stored value applies over the server's (``NOUS_OUTSIDE_JUDGE_SECOND=jev`` for
the switch, ``NOUS_OUTSIDE_JUDGE_JEV_MODEL``, ``NOUS_OUTSIDE_JUDGE_DAILY_CALLS``). The daily limit is never above
``NOUS_OUTSIDE_JUDGE_DAILY_CALLS``, the most the owner can set: a stored limit above it is used lowered. A stored key
that opens is used; with no stored key, ``TYPESAFE_API_KEY``; a stored key that does not open is never replaced by the
server's. The second judge runs exactly when ``sends``: it is on, has a key and a daily limit above 0.

The key is sealed with ``nous.mcp_client.secrets.SecretBox`` and the key in ``NOUS_MCP_CLIENT_KEY_FILE`` (its own
``KeyFile``), with associated data whose kind no MCP row can have, so a sealed value of either table does not open in
the other. No plaintext, length or part of the key is stored. In memory it is held by the live configuration and the
judge that uses it, and by the ledger's redaction (``register_own_secret``) from before it is stored or used until
the judge that used it has closed. As for the MCP credentials, the sealing protects the database, its dumps and its
backups, not the running process.

``SecondJudgeManager`` applies a change at once: under one lock, it checks the change and the confirmation (a write
that would start the sending, or raise the daily limit while it sends, needs the owner's yes to the sentence that names
the limit after it, sent back as that number), registers a new key, writes the row and commits, then builds and
starts the new second judge (when it sends), swaps it into the ``JudgeSet`` in one assignment and closes the old one,
whose queued decisions are dropped. The second judge's hourly pass (with ``NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED``, the
setting of the first judge's) starts with it and stops when it is replaced or closed: a new judge's pass counts from
zero, and its first pass runs two minutes after its start. The change runs in a task the manager holds, so a caller
that goes away does not stop it, and its answer comes after the old judge has closed. The first judge is never
replaced, closed or read here. Nothing here logs a key, a request body or an exception's text: class names only.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text

from nous.cognitive.ledger_store import forget_own_secret, register_own_secret
from nous.judge import jev_judge, store
from nous.judge.evidence import EVIDENCE_MAX_CHARS
from nous.judge.jev_judge import JevJudge
from nous.judge.service import SWEEP_BATCH, OutsideJudge
from nous.mcp_client.secrets import AadFields, KeyFile, SecretBox, SecretUnavailable, aad_fields

logger = logging.getLogger(__name__)

JUDGE_NAME = jev_judge.NAME
SENDS_TO = "api.typesafe.ai"
# The associated data of the sealed key: no MCP server row has this kind (``headers`` or ``oauth``) or an empty URL.
AAD_NAME = "outside-judge:jev"
AAD_KIND = "api_key"
MODEL_FORM = re.compile(r"jev-[0-9]+\.[0-9]+\.[0-9]+")
MODEL_MAX_CHARS = 40
KEY_FORM = re.compile(r"[A-Za-z0-9._~+/=:\-]{16,200}")
# Why a stored key cannot be used.
NO_KEY_FILE = "no key file"
OTHER_KEY = "stored with another key"
UNREADABLE = "cannot be opened"
RETRY_FIRST_SECONDS = 30.0
RETRY_MAX_SECONDS = 600.0
CHECK_SPACING_SECONDS = 10.0
CHECKS_A_DAY = 20

# The answers' sentences: fixed, never a value from the request.
MODE_OFF = "The server has the outside judge switched off."
NOT_LOADED = "The saved settings could not be read; the second judge stays off until they can."
SHUTTING_DOWN = "Nous is shutting down; nothing was changed."
FAILED = "The change could not be saved."
NOTHING_TO_CHANGE = "Nothing to change."
NO_KEY_FILE_TEXT = "This server has no key file, so a key cannot be saved here."
NO_KEY_TO_CHECK = "There is no key to check."
CHECK_LIMIT = "One check at a time, at least 10 seconds apart and at most 20 a day. Try again later."
MODEL_RULE = "model must be a version like jev-1.14.0, or null for the server's value."
ON_RULE = "on must be true or false."
KEY_RULE = "key must be 16 to 200 characters: letters, digits and . _ ~ + / = : -"

CONFIRM_TEXT = (
    "Send a copy of each decision Nous resolves to TypeSafe (api.typesafe.ai), an outside company? The copy holds the "
    "decision, Nous's note and grade, and what its tools did in that session, with their inputs and results, up to "
    "{chars} characters.{hourly} Text that looks like a key or a password is removed; everything else is sent as it "
    "is. TypeSafe says it does not train on what it receives; it does not say for how many days it keeps it. At most "
    "{calls} {copies} a day. Nothing is sent while this is off."
)
# The sentence's added part when the hourly pass is on (NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED); it starts with a space.
HOURLY_TEXT = (
    " Earlier decisions are sent too: about once an hour, and two minutes after a start or a saved change, up to "
    "{batch} that this model of Jev has not graded yet, from those Nous graded in the last {agent_days} days and those "
    "graded by the owner, the review route or an automatic check in the last {days} days. Decisions graded by the "
    "owner, the review route or an automatic check are sent without the grade and its note. A copy whose request "
    "failed is sent again, at most {attempts} times in all."
)

_sleep = asyncio.sleep  # the retry's wait; tests replace it


def confirm_text(daily_calls: int, hourly: bool = False) -> str:
    """The confirmation sentence, with the daily limit after the change; ``hourly``: the hourly pass is on."""
    extra = (
        HOURLY_TEXT.format(
            batch=SWEEP_BATCH,
            agent_days=store.SWEEP_AGENT_DAYS,
            days=store.SWEEP_DECISION_DAYS,
            attempts=store.MAX_ATTEMPTS,
        )
        if hourly
        else ""
    )
    return CONFIRM_TEXT.format(
        hourly=extra,
        chars=f"{EVIDENCE_MAX_CHARS:,}",
        calls=f"{daily_calls:,}",
        copies="copy" if daily_calls == 1 else "copies",
    )


def calls_rule(most: int) -> str:
    """The daily limit's rule, with the server's number."""
    return f"daily_calls must be a whole number from 0 to {most}, or null for the server's value."


# ---- the row ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredRow:
    """One ``nous_system.judge_second_settings`` row, its key still sealed. A setting of None is the server's."""

    enabled: bool | None = None
    model: str | None = None
    daily_calls: int | None = None
    key_id: str | None = None
    nonce: bytes | None = field(default=None, repr=False)
    ciphertext: bytes | None = field(default=None, repr=False)
    key_set_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None

    @property
    def has_key(self) -> bool:
        return self.key_id is not None


_ROW_COLUMNS = "enabled, model, daily_calls, key_id, nonce, ciphertext, key_set_at, updated_at, updated_by"
_READ = text(
    f"SELECT {_ROW_COLUMNS} FROM nous_system.judge_second_settings "
    "WHERE agent_id = :agent_id AND judge_name = :judge_name"
)
# The columns a write may name; a write names only these (never a name from a request).
WRITABLE = ("enabled", "model", "daily_calls", "key_id", "nonce", "ciphertext", "key_set_at")
KEY_COLUMNS = ("key_id", "nonce", "ciphertext", "key_set_at")


def _row_of(mapping: Any) -> StoredRow:
    return StoredRow(**{name: mapping[name] for name in _ROW_COLUMNS.split(", ")})


async def read_row(session: Any, agent_id: str) -> StoredRow | None:
    """The agent's row, or None when it has none."""
    found = (await session.execute(_READ, {"agent_id": agent_id, "judge_name": JUDGE_NAME})).mappings().first()
    return _row_of(found) if found is not None else None


async def upsert_row(session: Any, agent_id: str, values: dict[str, Any], actor: str) -> StoredRow:
    """Write the named columns of the agent's row (the others keep their value; a new row has NULL for them), with
    ``updated_at`` now and ``updated_by`` the actor, and return the row as it then stands. Does not commit."""
    names = [name for name in WRITABLE if name in values]
    if not names or set(values) - set(names):
        raise ValueError("a write names the writable columns only")
    statement = text(
        f"INSERT INTO nous_system.judge_second_settings (agent_id, judge_name, updated_by, {', '.join(names)}) "
        f"VALUES (:agent_id, :judge_name, :updated_by, {', '.join(f':{name}' for name in names)}) "
        "ON CONFLICT (agent_id, judge_name) DO UPDATE SET "
        f"{', '.join(f'{name} = EXCLUDED.{name}' for name in names)}, "
        f"updated_at = now(), updated_by = EXCLUDED.updated_by RETURNING {_ROW_COLUMNS}"
    )
    params = {**values, "agent_id": agent_id, "judge_name": JUDGE_NAME, "updated_by": actor}
    return _row_of((await session.execute(statement, params)).mappings().one())


# ---- sealing ---------------------------------------------------------------------------------------------------------


def key_fields(agent_id: str) -> AadFields:
    """The associated data's fields of an agent's sealed key."""
    return aad_fields(agent_id, AAD_NAME, "", AAD_KIND)


def seal_key(box: SecretBox, agent_id: str, key: str) -> dict[str, Any]:
    """The key's sealed columns (``key_id``, ``nonce``, ``ciphertext``), with a new nonce."""
    key_id, nonce, ciphertext = box.seal(key_fields(agent_id), key.encode("utf-8"))
    return {"key_id": key_id, "nonce": nonce, "ciphertext": ciphertext}


def open_key(box: SecretBox | None, agent_id: str, row: StoredRow | None) -> tuple[str | None, str | None]:
    """``(key, None)`` for a stored key that opens, ``(None, why)`` for one that does not (``NO_KEY_FILE``,
    ``OTHER_KEY``, ``UNREADABLE``), ``(None, None)`` with no stored key."""
    if row is None or not row.has_key:
        return None, None
    if box is None:
        return None, NO_KEY_FILE
    if row.key_id != box.key_id:
        return None, OTHER_KEY
    try:
        key = box.open(key_fields(agent_id), row.key_id, row.nonce or b"", row.ciphertext or b"").decode("utf-8")
    except (SecretUnavailable, UnicodeDecodeError):
        return None, UNREADABLE
    return (key, None) if KEY_FORM.fullmatch(key) else (None, UNREADABLE)


# ---- how the values combine ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SecondConfig:
    """The second judge's values in effect, and where each comes from. ``key`` is never in ``repr``."""

    enabled: bool
    model: str
    key: str | None = field(repr=False)
    daily_calls: int
    enabled_source: str  # stored | server
    model_source: str  # stored | server
    calls_source: str  # stored | server | lowered
    key_source: str  # stored | server | none
    key_problem: str | None


def resolve(settings: Any, row: StoredRow | None, opened_key: str | None, key_problem: str | None) -> SecondConfig:
    """The values in effect from the server's settings, the stored row and its key as ``open_key`` gave it."""
    row = row or StoredRow()
    most = settings.outside_judge_daily_calls
    if row.daily_calls is None:
        calls, calls_source = most, "server"
    elif row.daily_calls > most:
        calls, calls_source = most, "lowered"
    else:
        calls, calls_source = row.daily_calls, "stored"
    server_key = settings.typesafe_api_key.strip()
    if row.has_key:
        key, key_source = opened_key, "stored"
    else:
        key, key_source, key_problem = (server_key or None), ("server" if server_key else "none"), None
    return SecondConfig(
        enabled=row.enabled if row.enabled is not None else settings.outside_judge_second == JUDGE_NAME,
        model=row.model if row.model is not None else settings.outside_judge_jev_model,
        key=key,
        daily_calls=calls,
        enabled_source="stored" if row.enabled is not None else "server",
        model_source="stored" if row.model is not None else "server",
        calls_source=calls_source,
        key_source=key_source,
        key_problem=key_problem if row.has_key else None,
    )


def sends(config: SecondConfig) -> bool:
    """Whether the second judge runs: it is on, has a key and a daily limit above 0."""
    return config.enabled and bool(config.key) and config.daily_calls > 0


def _same_run(a: SecondConfig, b: SecondConfig) -> bool:
    """Whether a judge built for ``a`` is the judge ``b`` asks for."""
    return (a.model, a.key, a.daily_calls) == (b.model, b.key, b.daily_calls)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class Answer:
    """What a route answers: a status and a JSON body (never a key)."""

    status: int
    body: dict[str, Any]


def _refusal(status: int, message: str, **extra: Any) -> Answer:
    return Answer(status, {"error": message, **extra})


# ---- the manager -----------------------------------------------------------------------------------------------------


class SecondJudgeManager:
    """The second judge's stored settings and the judge they run, for one agent. ``judges`` is the running
    ``JudgeSet`` (None with ``NOUS_OUTSIDE_JUDGE_MODE=off``: the row is read for the state and a stored key may be
    removed; no key is opened and nothing is applied). ``transport`` is for tests only."""

    def __init__(
        self,
        database: Any,
        settings: Any,
        judges: Any | None,
        *,
        transport: Any | None = None,
        key_file: KeyFile | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._agent_id = settings.agent_id
        self._judges = judges
        self._transport = transport
        self._key_file = key_file if key_file is not None else KeyFile(settings.mcp_client_key_file)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._lock = asyncio.Lock()
        self._row: StoredRow | None = None
        self._opened: tuple[str | None, str | None] = (None, None)
        self._live = resolve(settings, None, None, None)
        self._running: SecondConfig | None = None  # what the second judge in the set was built for
        self._loaded = False
        self._closed = False
        self._retry: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()  # the changes in flight and the old judges closing
        self._registered: set[str] = set()
        self._checking = False
        self._last_check: float | None = None
        self._checks_on: date | None = None
        self._checks = 0

    # ---- reading ---------------------------------------------------------------------------------------------------

    @property
    def locked_off(self) -> bool:
        """Nothing runs and only a stored key's removal is allowed (``NOUS_OUTSIDE_JUDGE_MODE=off``)."""
        return self._judges is None or self._settings.outside_judge_mode != "shadow"

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def live(self) -> SecondConfig:
        """The values in effect now."""
        return self._live

    def _hourly(self) -> bool:
        """Whether the second judge runs the hourly pass (``NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED``)."""
        return bool(getattr(self._settings, "outside_judge_reports_enabled", False))

    def state(self) -> dict[str, Any]:
        """What the card shows: each value and where it comes from, whether a key is saved and when, never the key,
        its length or any part of it."""
        live, row = self._live, self._row
        has_key = row is not None and row.has_key
        return {
            "enabled": True,
            "locked": "off" if self.locked_off else None,
            "loaded": self._loaded,
            "running": self._judges is not None and self._judges.second is not None,
            "on": {"value": live.enabled, "source": live.enabled_source},
            "model": {"value": live.model, "source": live.model_source},
            "daily_calls": {
                "value": live.daily_calls,
                "most": self._settings.outside_judge_daily_calls,
                "source": live.calls_source,
            },
            "key": {
                "source": live.key_source,
                "set_at": _iso(row.key_set_at) if has_key and row is not None else None,
                "problem": live.key_problem,
                "server_has_key": bool(self._settings.typesafe_api_key.strip()),
            },
            "can_store_key": self._key_file.box() is not None,
            "sends_to": SENDS_TO,
            "confirm_text": confirm_text(live.daily_calls, self._hourly()),
            "updated_at": _iso(row.updated_at) if row is not None else None,
            "updated_by": row.updated_by if row is not None else None,
        }

    async def _read(self) -> StoredRow | None:
        async with self._database.session() as session:
            return await read_row(session, self._agent_id)

    # ---- start, retry and close -------------------------------------------------------------------------------------

    async def load(self) -> None:
        """At start: read the row and apply it. When it cannot be read the second judge stays off, the changes are
        refused, and the row is read again in the background until it can be."""
        try:
            row = await self._read()
        except Exception as exc:
            logger.warning(
                "F118: the second judge's saved settings could not be read (%s); it stays off and they are read "
                "again in the background",
                type(exc).__name__,
            )
            self._start_retry()
            return
        async with self._lock:
            await self._install_read(row)

    def _start_retry(self) -> None:
        if not self._closed and (self._retry is None or self._retry.done()):
            self._retry = asyncio.get_running_loop().create_task(self._read_again(), name="outside-judge-second-read")

    async def _read_again(self) -> None:
        delay = RETRY_FIRST_SECONDS
        while not self._closed:
            await _sleep(delay)
            try:
                row = await self._read()
                async with self._lock:
                    if self._closed:
                        return
                    await self._install_read(row)
            except Exception as exc:
                delay = min(delay * 2, RETRY_MAX_SECONDS)
                logger.warning(
                    "F118: the second judge's saved settings could still not be read (%s); trying again in %d s",
                    type(exc).__name__,
                    int(delay),
                )
                continue
            logger.info("F118: the second judge's saved settings were read and apply")
            return

    async def close(self) -> None:
        """At shutdown, before the judges close: refuse further changes, stop reading again, and wait for the change
        in flight and for every old second judge still closing (a read cancelled here leaves its old judge closing,
        never half closed). Waiting cancels none of them, also when this call is cancelled."""
        self._closed = True
        retry, self._retry = self._retry, None
        if retry is not None and not retry.done():
            retry.cancel()
            await asyncio.wait({retry})
        while waiting := {task for task in self._tasks if not task.done()}:
            await asyncio.wait(waiting)

    # ---- applying (the lock held) ------------------------------------------------------------------------------------

    async def _install_read(self, row: StoredRow | None) -> None:
        """Apply a row read from the table: its key opened (unless locked off) and registered before it is used."""
        opened, problem = (None, None) if self.locked_off else open_key(self._key_file.box(), self._agent_id, row)
        if opened:
            self._hold(opened)
        await self._install(row, opened, problem)

    async def _install(self, row: StoredRow | None, opened: str | None, problem: str | None) -> None:
        """Make ``row`` (now stored) the one in effect and run the second judge it asks for."""
        self._row, self._opened, self._loaded = row, (opened, problem), True
        self._live = resolve(self._settings, row, opened, problem)
        if not self.locked_off:
            await self._switch(self._live)

    async def _switch(self, config: SecondConfig) -> None:
        """Run the second judge ``config`` asks for: nothing to do when the running one is that judge already;
        otherwise build and start the new one (when it sends), swap it in (one assignment: a decision submitted
        meanwhile lands on the old judge or the new one), close the old one, and forget the keys nothing uses."""
        current = self._judges.second
        wanted = sends(config)
        same = current is not None and self._running is not None and _same_run(self._running, config)
        if (wanted and same) or (not wanted and current is None):
            self._settle()
            return
        new = await self._start_service(config) if wanted else None
        old = self._judges.replace_second(new)
        self._running = config if new is not None else None
        await self._close_old(old)
        self._settle()

    async def _start_service(self, config: SecondConfig) -> Any | None:
        """The second judge's service for ``config``, started; None, with one WARNING, when it cannot start."""
        service = None
        try:
            service = self._make_service(config)
            service.start()
            return service
        except Exception as exc:
            logger.warning("F118: the second judge did not start (%s); it is not running", type(exc).__name__)
            await self._close_old(service, timeout=0)
            return None

    async def _close_old(self, service: Any | None, timeout: float | None = None) -> None:
        """Close a second judge that is out of the set, as a task this manager holds: a cancelled caller (the
        background read at shutdown) leaves it closing, and ``close`` waits for it."""
        if service is None:
            return
        task = asyncio.get_running_loop().create_task(
            _close_quietly(service, timeout), name="outside-judge-second-close"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await asyncio.shield(task)

    def _make_service(self, config: SecondConfig) -> OutsideJudge:
        judge = JevJudge(config.key or "", config.model, transport=self._transport)
        return OutsideJudge(
            self._database,
            self._settings,
            judge,
            timeout_seconds=jev_judge.TIMEOUT_SECONDS,
            daily_calls=config.daily_calls,
            sweep=self._hourly(),
        )

    def _hold(self, key: str) -> None:
        """Register ``key`` with the redaction (once) before it is stored or used."""
        if key not in self._registered:
            register_own_secret(key)
            self._registered.add(key)

    def _settle(self) -> None:
        """Forget every registered key that neither the values in effect nor the running second judge use (a judge
        that used it has closed)."""
        in_use = {self._live.key, self._running.key if self._running is not None else None}
        for key in self._registered - in_use:
            forget_own_secret(key)
            self._registered.discard(key)

    # ---- changes ---------------------------------------------------------------------------------------------------

    async def patch(self, changes: dict[str, Any], *, confirmed: int | None, actor: str) -> Answer:
        """Set the switch (``on``), the model and the daily limit; a None model or limit is the server's.
        ``confirmed``: the daily limit named by the confirmation the owner accepted (``_change``)."""
        refused = self._refused_now(removal=False)
        if refused is not None:
            return refused
        if not changes or set(changes) - {"on", "model", "daily_calls"}:
            return _refusal(400, NOTHING_TO_CHANGE)
        values: dict[str, Any] = {}
        if "on" in changes:
            if not isinstance(changes["on"], bool):
                return _refusal(400, ON_RULE)
            values["enabled"] = changes["on"]
        if "model" in changes:
            model = changes["model"]
            if model is not None and not (
                isinstance(model, str) and len(model) <= MODEL_MAX_CHARS and MODEL_FORM.fullmatch(model)
            ):
                return _refusal(400, MODEL_RULE)
            values["model"] = model
        if "daily_calls" in changes:
            calls, most = changes["daily_calls"], self._settings.outside_judge_daily_calls
            if calls is not None and (isinstance(calls, bool) or not isinstance(calls, int) or not 0 <= calls <= most):
                return _refusal(400, calls_rule(most))
            values["daily_calls"] = calls
        return await self._run(lambda: self._change(values, confirmed=confirmed, actor=actor))

    async def put_key(self, key: str, *, confirmed: int | None, actor: str) -> Answer:
        """Save the key (sealed); nothing is sent to the provider."""
        refused = self._refused_now(removal=False)
        if refused is not None:
            return refused
        key = key.strip() if isinstance(key, str) else ""
        if not KEY_FORM.fullmatch(key):
            return _refusal(400, KEY_RULE)
        if self._key_file.box() is None:
            return _refusal(409, NO_KEY_FILE_TEXT)
        return await self._run(lambda: self._change({}, key=key, confirmed=confirmed, actor=actor))

    async def remove_key(self, *, confirmed: int | None, actor: str) -> Answer:
        """Remove the stored key (allowed with the mode off too)."""
        refused = self._refused_now(removal=True)
        if refused is not None:
            return refused
        return await self._run(lambda: self._change(dict.fromkeys(KEY_COLUMNS), confirmed=confirmed, actor=actor))

    def _refused_now(self, *, removal: bool) -> Answer | None:
        if self._closed:
            return _refusal(503, SHUTTING_DOWN)
        if self.locked_off and not removal:
            return _refusal(409, MODE_OFF)
        if not self._loaded:
            return _refusal(503, NOT_LOADED)
        return None

    async def _run(self, work: Callable[[], Awaitable[Answer]]) -> Answer:
        """Run one change as a task this manager holds; a caller that goes away does not stop it."""
        if self._closed:
            return _refusal(503, SHUTTING_DOWN)
        task = asyncio.get_running_loop().create_task(self._locked(work), name="outside-judge-second-change")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await asyncio.shield(task)

    async def _locked(self, work: Callable[[], Awaitable[Answer]]) -> Answer:
        try:
            async with self._lock:
                if self._closed:
                    return _refusal(503, SHUTTING_DOWN)
                if not self._loaded:
                    return _refusal(503, NOT_LOADED)
                return await work()
        except Exception as exc:
            logger.warning("F118: a change of the second judge's settings failed (%s)", type(exc).__name__)
            return _refusal(500, FAILED)

    def _needs_confirmation(self, after: SecondConfig, confirmed: int | None) -> bool:
        """Whether a change that gives ``after`` needs the owner's yes and does not have it. It does when the second
        judge would send after it and either was not sending before or would send more a day than before (a raise,
        also to the server's value). The yes counts only for the number its sentence named: ``confirmed`` must be
        the daily limit after the change (a bool is not a number)."""
        if self.locked_off or not sends(after):
            return False
        if sends(self._live) and after.daily_calls <= self._live.daily_calls:
            return False
        accepted = isinstance(confirmed, int) and not isinstance(confirmed, bool) and confirmed == after.daily_calls
        return not accepted

    async def _change(
        self, values: dict[str, Any], *, key: str | None = None, confirmed: int | None, actor: str
    ) -> Answer:
        """One change, the lock held: the confirmation, a new key registered, the row written and committed, then
        applied. ``key``: a new key to seal (its sealed columns become the values). An unchanged write writes
        nothing, and starts the second judge again when the values in effect send and none runs (a start that
        failed)."""
        row = self._row or StoredRow()
        if key is not None:
            proposed, opened, problem = replace(row, key_id="new"), key, None
            unchanged = row.has_key and self._opened[0] == key
        elif "key_id" in values:  # the key's removal
            proposed, opened, problem = replace(row, **values), None, None
            unchanged = not row.has_key
        else:
            proposed = replace(row, **values)
            opened, problem = self._opened
            unchanged = all(getattr(row, name) == value for name, value in values.items())
        if unchanged:
            if not self.locked_off and sends(self._live) and self._judges.second is None:
                await self._switch(self._live)
            return Answer(200, {**self.state(), "changed": False})
        after = resolve(self._settings, proposed, opened, problem)
        if self._needs_confirmation(after, confirmed):
            return _refusal(
                409,
                confirm_text(after.daily_calls, self._hourly()),
                needs_confirm=True,
                confirm_daily_calls=after.daily_calls,
            )
        if key is not None:
            box = self._key_file.box()
            if box is None:
                return _refusal(409, NO_KEY_FILE_TEXT)
            self._hold(key)  # before it is stored or used
            values = {**seal_key(box, self._agent_id, key), "key_set_at": self._clock()}
        outcome, stored = await self._store(values, actor)
        if outcome == _NOT_WRITTEN:
            self._settle()  # a new key is not used
            return _refusal(500, FAILED)
        if outcome == _UNKNOWN:
            await self._lost()
            return _refusal(500, FAILED)
        if outcome == _STORED:
            await self._install(stored, opened, problem)
        else:
            await self._install_read(stored)  # the commit's outcome was not known: what the table holds applies
            if not _holds(stored, values):
                return _refusal(500, FAILED)
        named = {"on" if name == "enabled" else "key" if name in KEY_COLUMNS else name for name in values}
        logger.info("F118: second judge settings changed (%s) by %s", ", ".join(sorted(named)), actor)
        return Answer(200, {**self.state(), "changed": True})

    async def _store(self, values: dict[str, Any], actor: str) -> tuple[str, StoredRow | None]:
        """Write and commit. ``(_STORED, row)``; ``(_NOT_WRITTEN, None)`` when the write failed before the commit;
        when the commit failed its outcome is not known and the row is read back: ``(_READ_BACK, row or None)``, or
        ``(_UNKNOWN, None)`` when that read fails too."""
        committing = False
        try:
            async with self._database.session() as session:
                stored = await upsert_row(session, self._agent_id, values, actor)
                committing = True
                await session.commit()
            return _STORED, stored
        except Exception as exc:
            logger.warning(
                "F118: the second judge's settings were not %s (%s)",
                "committed" if committing else "written",
                type(exc).__name__,
            )
            if not committing:
                return _NOT_WRITTEN, None
        try:
            return _READ_BACK, await self._read()
        except Exception as exc:
            logger.warning("F118: the second judge's settings could not be read back (%s)", type(exc).__name__)
            return _UNKNOWN, None

    async def _lost(self) -> None:
        """What the table holds is not known: the second judge stops, the changes are refused, and the row is read
        again in the background."""
        self._loaded = False
        self._row, self._opened = None, (None, None)
        self._live = resolve(self._settings, None, None, None)
        old = self._judges.replace_second(None) if self._judges is not None else None
        self._running = None
        await self._close_old(old)
        self._settle()
        self._start_retry()

    # ---- the check ---------------------------------------------------------------------------------------------------

    async def check(self) -> Answer:
        """One request with a fixed record and the key and model in effect: ``ok``, ``refused`` or ``unreachable``.
        Writes no verdict and does not count against the daily limit; one at a time, at least
        ``CHECK_SPACING_SECONDS`` apart, at most ``CHECKS_A_DAY`` a UTC day."""
        refused = self._refused_now(removal=False)
        if refused is not None:
            return refused
        live = self._live
        if not live.key:
            return _refusal(409, NO_KEY_TO_CHECK)
        now, today = self._monotonic(), self._clock().date()
        if self._checks_on != today:
            self._checks_on, self._checks = today, 0
        if (
            self._checking
            or (self._last_check is not None and now - self._last_check < CHECK_SPACING_SECONDS)
            or self._checks >= CHECKS_A_DAY
        ):
            return _refusal(429, CHECK_LIMIT)
        self._checking, self._last_check, self._checks = True, now, self._checks + 1
        try:
            result = await JevJudge(live.key, live.model, transport=self._transport).probe(jev_judge.TIMEOUT_SECONDS)
        finally:
            self._checking = False
        return Answer(200, {"result": result})


_STORED, _READ_BACK, _NOT_WRITTEN, _UNKNOWN = "stored", "read back", "not written", "unknown"


def _holds(row: StoredRow | None, values: dict[str, Any]) -> bool:
    """Whether ``row`` holds the written ``values`` (a key by its sealed columns)."""
    if row is None:
        return False
    return all(getattr(row, name) == value for name, value in values.items() if name != "key_set_at")


async def _close_quietly(service: Any | None, timeout: float | None = None) -> None:
    """Close a second judge's service; a failure is one WARNING with the class name."""
    if service is None:
        return
    try:
        await (service.close() if timeout is None else service.close(timeout))
    except Exception as exc:
        logger.warning("F118: a second judge did not close cleanly (%s)", type(exc).__name__)
