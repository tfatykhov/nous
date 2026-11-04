"""Find tool-call markup that leaked into stored memory text, and remove it. Dry run unless --apply.

A model writing a tool call could end a string parameter with markup (``</content>`` and the
parameters after it, sometimes a whole further call), and the write paths stored all of it. They
strip it now (nous/tool_markup.py); this script cleans the rows written before, with the same
function:

  heart.facts       content (active and inactive rows)
  brain.decisions   description, context, pattern
  heart.procedures  description
  heart.censors     reason, action_instruction
  heart.episodes    title, summary, and the title and summary in structured_summary

A dry run reads in a READ ONLY transaction and prints, per table, how many rows --apply would
rewrite: their ids, the columns, and the parameter names the markup gave values to (category,
pattern: values meant as the call's other arguments, which the cleaning drops), never their text.
It also counts the rows the SQL prefilter matched that hold no markup the function removes. A row
whose markup holds a tool call that never ran (a learn_fact inside a fact) is listed apart, with
the call names, and --apply leaves it alone unless --include-calls: the stored text is the only
copy of that call.

--apply rewrites each row to the cleaned text in its own transaction, only if every column its new
text and embedding are built from is still as it was read, and re-embeds it from the text its
write path embeds (the provider nous/main.py builds) when that text changed. A row whose embed
fails is left as it was. The updated_at triggers on facts, decisions, procedures and censors move
updated_at, as for any edit; episodes have no updated_at. Agent-scoped and idempotent: a second
run finds nothing.
A cleaned fact can equal a clean copy stored later (write-time dedup compared the dirty text); the
script does not look for such pairs or merge them.

Usage::

    uv run python scripts/ops/clean_tool_markup.py --agent-id nous-default
    uv run python scripts/ops/clean_tool_markup.py --agent-id nous-default --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.api.tools import _CREATE_CENSOR_SCHEMA, _LEARN_FACT_SCHEMA, _RECORD_DECISION_SCHEMA
from nous.brain.brain import _decision_embed_text
from nous.config import Settings
from nous.heart.censors import _censor_embed_text
from nous.heart.procedures import _build_embed_text
from nous.storage.database import Database
from nous.tool_markup import calls_in, params_in, strip_tool_markup

try:
    from scripts.ops.reembed_write_quality import embedding_provider
except ImportError:  # run as a file: scripts/ops is sys.path[0]
    from reembed_write_quality import embedding_provider

# A cheap SQL prefilter: every text strip_tool_markup can change holds one of these.
_CANDIDATE = r"</|<(antml:)?(invoke|function_calls|parameter)\M"


@dataclass(frozen=True)
class _Table:
    name: str
    # Text columns to clean, with the tool parameter each came from (None: not a tool's).
    columns: dict[str, str | None]
    # Other columns read for the embed text.
    extra: tuple[str, ...]
    embed_text: Callable[[dict[str, Any], Settings], str]
    has_active: bool = False
    # A JSONB column whose string keys are cleaned too.
    json_column: str | None = None
    json_keys: tuple[str, ...] = ()
    # The parameters of the tool that wrote the text: <category> is markup only for one that has it.
    params: frozenset[str] = frozenset()

    def read_columns(self) -> list[str]:
        """Every column the new text and embedding are built from: the guard of an --apply."""
        return [*self.columns, *self.extra, *([self.json_column] if self.json_column else [])]


def _episode_embed_text(r: dict[str, Any], s: Settings) -> str:
    """The text heart/episodes.py embeds: an ended episode's title, summary, outcome and lessons
    (_end and reembed_missing), an ongoing one's title and summary (_start)."""
    if r["ended_at"] is not None:
        lessons = " ".join(r["lessons_learned"] or [])
        return f"{r['title'] or ''} {r['summary'] or ''} {r['outcome'] or ''} {lessons}".strip()
    return f"{r['title'] or ''} {r['summary'] or ''}".strip()


TABLES: dict[str, _Table] = {
    "facts": _Table(
        "heart.facts",
        {"content": "content"},
        (),
        lambda r, s: r["content"],
        has_active=True,
        params=frozenset(_LEARN_FACT_SCHEMA["properties"]),
    ),
    "decisions": _Table(
        "brain.decisions",
        {"description": "description", "context": "context", "pattern": "pattern"},
        (),
        lambda r, s: _decision_embed_text(r["description"], r["context"], r["pattern"]),
        params=frozenset(_RECORD_DECISION_SCHEMA["properties"]),
    ),
    "procedures": _Table(
        "heart.procedures",
        {"description": "description"},
        ("name", "core_patterns", "goals", "core_tools", "core_concepts", "implementation_notes"),
        lambda r, s: _build_embed_text(
            r["name"],
            r["description"],
            r["core_patterns"],
            r["goals"],
            r["core_tools"],
            r["core_concepts"],
            r["implementation_notes"],
            s.procedure_embed_evidence,
        ),
        has_active=True,
        params=frozenset({"type", "content", "domain", "source"}),  # MCP nous_teach (nous/api/mcp.py)
    ),
    "censors": _Table(
        "heart.censors",
        {"reason": "reason", "action_instruction": None},
        ("trigger_pattern",),
        lambda r, s: _censor_embed_text(
            r["trigger_pattern"], r["reason"], r["action_instruction"], s.censor_embed_text_mode
        ),
        has_active=True,
        params=frozenset(_CREATE_CENSOR_SCHEMA["properties"]),
    ),
    "episodes": _Table(
        "heart.episodes",
        {"title": None, "summary": None},
        ("outcome", "lessons_learned", "ended_at"),
        _episode_embed_text,
        has_active=True,
        json_column="structured_summary",
        json_keys=("title", "summary"),
    ),
}


@dataclass
class Change:
    """One row the script would rewrite: the columns that change, old and new."""

    table: str
    id: Any
    active: bool | None
    # md5 of every column the new text and embedding are built from, as read.
    fingerprint: str
    old: dict[str, Any]
    new: dict[str, Any]
    calls: list[str]
    params: list[str]
    old_embed_text: str
    new_embed_text: str


@dataclass
class Report:
    planned: dict[str, list[Change]] = field(default_factory=dict)
    # Rows the SQL prefilter matched that hold no markup the function removes.
    unchanged: dict[str, int] = field(default_factory=dict)
    cleaned: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    skipped_calls: dict[str, int] = field(default_factory=dict)


def _clean(value: Any, param: str | None, params: frozenset[str]) -> tuple[Any, str]:
    if not isinstance(value, str):
        return value, ""
    return strip_tool_markup(value, param, params)


def _fingerprint(t: _Table) -> str:
    return f"md5(ROW({', '.join(t.read_columns())})::text)"


async def scan(
    session: AsyncSession, settings: Settings, tables: list[str], unchanged: dict[str, int] | None = None
) -> dict[str, list[Change]]:
    """Every row of the agent whose text the sanitiser changes, per table. Reads only.

    ``unchanged`` gets, per table, how many rows the prefilter matched and the function left.
    """
    out: dict[str, list[Change]] = {}
    for key in tables:
        t = TABLES[key]
        cols = list(t.columns) + list(t.extra)
        select_cols = ", ".join(
            ["id", "active" if t.has_active else "NULL AS active", *cols, f"{_fingerprint(t)} AS fp"]
        )
        where = " OR ".join(f"{c} ~ :pat" for c in t.columns)
        if t.json_column:
            select_cols += f", {t.json_column}::text AS {t.json_column}"
            where += f" OR {t.json_column}::text ~ :pat"
        rows = (
            await session.execute(
                text(f"SELECT {select_cols} FROM {t.name} WHERE agent_id = :a AND ({where}) ORDER BY id"),
                {"a": settings.agent_id, "pat": _CANDIDATE},
            )
        ).mappings()
        changes: list[Change] = []
        matched = 0
        for row in rows:
            matched += 1
            row = dict(row)
            old: dict[str, Any] = {}
            new: dict[str, Any] = {}
            removed: list[str] = []
            for col, param in t.columns.items():
                clean, cut = _clean(row[col], param, t.params)
                if cut:
                    old[col], new[col] = row[col], clean
                    removed.append(cut)
            if t.json_column and row[t.json_column] is not None:
                doc = json.loads(row[t.json_column])
                if isinstance(doc, dict):
                    cleaned_doc = dict(doc)
                    for k in t.json_keys:
                        clean, cut = _clean(doc.get(k), None, t.params)
                        if cut:
                            cleaned_doc[k] = clean
                            removed.append(cut)
                    if cleaned_doc != doc:
                        old[t.json_column], new[t.json_column] = row[t.json_column], json.dumps(cleaned_doc)
            if not new:
                continue
            after = {**row, **{c: v for c, v in new.items() if c in t.columns}}
            changes.append(
                Change(
                    table=key,
                    id=row["id"],
                    active=row["active"],
                    fingerprint=row["fp"],
                    old=old,
                    new=new,
                    calls=[call for cut in removed for call in calls_in(cut)],
                    params=list(dict.fromkeys(name for cut in removed for name in params_in(cut))),
                    old_embed_text=t.embed_text(row, settings),
                    new_embed_text=t.embed_text(after, settings),
                )
            )
        out[key] = changes
        if unchanged is not None:
            unchanged[key] = matched - len(changes)
    return out


async def _write(session: AsyncSession, change: Change, agent_id: str, vector: list[float] | None) -> bool:
    """Rewrite one row if every column it is built from still holds what was read. True when it did."""
    t = TABLES[change.table]
    params: dict[str, Any] = {"id": change.id, "a": agent_id, "fp": change.fingerprint}
    sets = []
    for i, col in enumerate(change.new):
        params[f"new{i}"] = change.new[col]
        sets.append(f"{col} = CAST(:new{i} AS jsonb)" if col == t.json_column else f"{col} = :new{i}")
    if vector is not None:
        params["vec"] = "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
        sets.append("embedding = CAST(:vec AS vector)")
    result = await session.execute(
        text(f"UPDATE {t.name} SET {', '.join(sets)} WHERE id = :id AND agent_id = :a AND {_fingerprint(t)} = :fp"),
        params,
    )
    return result.rowcount == 1


async def run(
    db: Database,
    settings: Settings,
    provider: Any,
    *,
    tables: list[str],
    apply: bool,
    include_calls: bool = False,
) -> Report:
    """Scan (read only) and, with ``apply``, rewrite and re-embed each row in its own transaction."""
    if apply and provider is None:
        raise ValueError("apply needs an embedding provider: a cleaned row would keep its old vector")
    report = Report()
    async with db.session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        report.planned = await scan(session, settings, tables, report.unchanged)
        await session.rollback()
    if not apply:
        return report
    for key, changes in report.planned.items():
        report.cleaned[key] = report.failed[key] = report.skipped_calls[key] = 0
        for change in changes:
            if change.calls and not include_calls:
                report.skipped_calls[key] += 1
                continue
            vector = None
            if change.new_embed_text != change.old_embed_text:
                try:
                    vector = await provider.embed(change.new_embed_text)
                except Exception:
                    report.failed[key] += 1
                    continue
            async with db.session() as session:
                if await _write(session, change, settings.agent_id, vector):
                    await session.commit()
                    report.cleaned[key] += 1
                else:
                    await session.rollback()
                    report.failed[key] += 1
    return report


def _print_report(report: Report, apply: bool, include_calls: bool) -> None:
    for key, changes in report.planned.items():
        calls = [c for c in changes if c.calls]
        line = f"{TABLES[key].name}: {len(changes)} rows hold leaked markup"
        if TABLES[key].has_active:
            line += f" ({sum(1 for c in changes if c.active)} active)"
        line += f"; {len(calls)} of them hold tool calls that never ran"
        line += f"; {report.unchanged.get(key, 0)} more matched the prefilter and hold none the function removes"
        print(line)
        for c in sorted(changes, key=lambda c: bool(c.calls)):
            line = f"  {c.id}  {','.join(c.new)}  params: {','.join(c.params) or '-'}"
            print(line + (f"  calls: {','.join(c.calls)}" if c.calls else ""))
        if apply:
            print(
                f"  cleaned {report.cleaned[key]}, failed or changed since read {report.failed[key]}, "
                f"left alone (calls, no --include-calls) {report.skipped_calls[key]}"
            )
    if not apply:
        print("dry run, nothing written" + ("" if include_calls else "; --apply leaves the rows with calls alone"))


async def main_async(agent_id: str, *, tables: list[str], apply: bool, include_calls: bool) -> None:
    settings = Settings().model_copy(update={"agent_id": agent_id})
    provider = embedding_provider(settings)
    if apply and provider is None:
        raise SystemExit("--apply needs OPENAI_API_KEY: a cleaned row is re-embedded")
    db = Database(settings)
    await db.connect()
    try:
        report = await run(db, settings, provider, tables=tables, apply=apply, include_calls=include_calls)
    finally:
        if provider is not None:
            await provider.close()
        await db.disconnect()
    print(f"agent {agent_id}")
    _print_report(report, apply, include_calls)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-id", default=None, help="Agent whose rows to clean (default: NOUS_AGENT_ID).")
    parser.add_argument(
        "--table", action="append", choices=sorted(TABLES), help="Only these tables (repeatable; default: all)."
    )
    parser.add_argument("--apply", action="store_true", help="Rewrite and re-embed the rows (default: dry run).")
    parser.add_argument(
        "--include-calls", action="store_true", help="With --apply, also clean rows that hold tool calls."
    )
    args = parser.parse_args()
    asyncio.run(
        main_async(
            args.agent_id or Settings().agent_id,
            tables=args.table or list(TABLES),
            apply=args.apply,
            include_calls=args.include_calls,
        )
    )


if __name__ == "__main__":
    main()
