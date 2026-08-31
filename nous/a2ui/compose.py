"""F092.1: the micro-app composer — LLM composition inside hard walls.

Pipeline (F092 §9.2, amended by F092.1):

1. Resolve the declared ``data_sources`` server-side FIRST — the server
   owns every sourced value; the model only ever binds paths to them.
2. Prompt = catalog summary (~component names + props, not full schemas)
   + the three archetypes + the grammar rules + the resolved source keys
   (with a small sample so the model knows the shape) + the intent.
3. Model emits ``{title, archetype, components, dataModel, refine_options}``.
4. Validate: schema + structure (BuiltSurface.validate) + grammar lint +
   composer-level constraints (stable ids root/header/footer, sourced keys
   not overwritten). On failure, feed the error list back — max 2 repairs.
5. Fallback: a grammar-conforming markdown surface carrying the intended
   content as prose. Nothing unvalidated ever reaches a client.

Model-supplied data is the sanctioned exception to the self-sourcing rule,
confined to compose: any dataModel subtree the model invented (no source
behind it) is recorded in ``app_spec.provenance`` and its Section rendered
amber via the ``provenance: "model"`` prop. The gap is visible, never
silent — and Phase 5 measures the unsourced fraction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from nous.handlers import call_background_llm

from .catalog_summary import catalog_property_summary
from .dsl import BuiltSurface, SurfaceValidationError
from .grammar import lint_micro_app
from .sources import SourceRegistry, UnknownSourceError, is_series

logger = logging.getLogger(__name__)

MAX_REPAIRS = 2
_META_KEY = "meta"

# F093 §3 — the closed theme enum. Unknown ⇒ validation error (a silently
# ignored theme is indistinguishable from a broken one). Absent ⇒ default.
_THEMES: dict[str, str] = {
    "nous-default": "violet-on-dark, the standard companion look",
    "alpine-dusk": "warm near-black with an alpenglow accent — trips, outdoor plans",
    "harbor": "cool blue-grey — status, ops, monitoring dashboards",
    "paper": "warm light with a serif display — reading, briefings, digests",
    "signal": "high-contrast dark with an electric accent — alerts, dense data",
}
_DEFAULT_THEME = "nous-default"

_ARCHETYPES = """\
Pick ONE archetype and fill it:
- status — "where does X stand": StatRow of counts/deadlines, sections by
  subsystem, open items last. (vacation, project status, DAG overview)
- briefing — "what's happening in window W": a time-ordered Timeline,
  conditions summary, linked sources. (sailing forecast, weekly digest)
- ledger — "list of things with attributes": KeyValueTable per item,
  drill-down per row offered as a refine option. (bookings, decisions)
"""

_GRAMMAR_RULES = """\
Hard rules (violations are rejected and returned to you to fix):
- Skeleton: component id "root" is a Column whose children are, in order:
  one AppHeader (id "header"), optionally one StatRow, 1-5 Section blocks,
  one AppFooter (id "footer").
- A StatRow holds AT MOST 4 StatTile children. More stats than that?
  Keep the 4 that matter.
- AppHeader.composedAt MUST be the binding {"path": "/meta/composedAt"}.
  Do NOT put a literal timestamp there and do NOT write /meta yourself —
  the server owns it.
- Every Section has a non-empty title and exactly one body component via
  "child" (a single component id STRING — Section has no "children" array;
  wrap multiple components in a Column and point child at it).
- Budget: 40 components (80 for ledger/briefing), nesting depth max 5.
  Over budget? Use a repeat template (below) or summarize + a refine option.
- Allowed components ONLY: the ones listed under "Component properties"
  below — nothing else exists. Input components (TextField, Slider,
  DateTimeInput, CheckBox, ChoicePicker) are BANNED — micro-apps are
  read-only.
- No component id may appear twice, be referenced by two parents, or be
  repeated inside one children array.
- Buttons may not carry actions (the AppFooter renders the app's controls).
- Sections whose data came from a provided source key get
  "provenance": "source". Sections showing data YOU supplied (no source
  key behind it) MUST get "provenance": "model".
- Bind data through the data model ({"path": "/key/..."}), don't inline
  long literals. A source that resolved data MUST be bound by something —
  an unbound source is rejected.

CHARTS (bind `path` to a source marked kind=series; REQUIRED, no literal):
- Sparkline {path, label?, tone?} — inline trend, tone ∈ neutral|ok|warn|crit.
- LineChart {path, series:[{key,label?,tone?}], label?, xLabel?, yLabel?} —
  ≤4 series; each `key` must be a numeric key present in the series points.
- BarChart {path, label?, orientation?: vertical|horizontal, tone?} —
  categorical (each point's `t` is a category, `v` its bar value).
The renderer owns scale, axes, colour and gaps — you pick tone/series, never
a colour. Binding a chart to a record LIST (not a series) is rejected.

REPEAT a component over an array instead of hand-writing N bindings: set a
Column's "children" to {"componentId": "<template-id>", "path": "/array"};
the template component renders once per item, and bare paths inside it (or
"@index") resolve relative to the item. This counts as ONE component and
renders ALL items — a fixed /array/0../k slice that under-covers the source
is rejected.

SECTION LAYOUT via Section "layout": stack (default) | hero (large/primary,
for the headline section) | grid-2 | grid-3 (N-column grid over the child's
items) | rail (horizontal scroll) | accordion (collapsed until tapped — use
for long secondary detail like raw tables or per-item breakdowns, never for
the headline section). Use hero to create hierarchy — a flat stack of equal
sections reads as generated.

TABS for 2-5 ALTERNATIVE views of the same subject (e.g. "By day" vs "By
category"): Tabs {tabs: [{"title": "...", "child": "<component id>"}]} — each
child is its own component in the flat list, exactly like a Section child, and
only the active panel renders. Prefer Tabs over stacking near-duplicate
sections; prefer separate Sections when the user should see both at once.
"""

_THEME_MENU = "Pick a `theme` (or omit for nous-default):\n" + "\n".join(
    f"- {tid}: {desc}" for tid, desc in _THEMES.items()
)

_RESPONSE_SHAPE = """\
Respond with ONLY a JSON object (no prose, no code fence) shaped:
{
  "title": "...",
  "archetype": "status" | "briefing" | "ledger",
  "theme": "<one of the listed theme ids>",  // optional; omit for nous-default
  "components": [ ... A2UI component objects, each with "id" and "component" ... ],
  "dataModel": { ... ONLY the subtrees you are supplying yourself ... },
  "refine_options": [ {"id": "slug", "label": "Button label"} ]  // 0-4. Each RE-RENDERS
  // the SAME data sources from a different angle (narrower window, one grouping, one section
  // expanded). There is no component that exports, downloads, emails, notifies or schedules,
  // so a button promising one cannot work and will be dropped — label them as VIEWS
  // ("Last 7 days", "Group by category"), never as actions ("Email report").
}
"""


@dataclass
class ComposedApp:
    built: BuiltSurface
    app_spec: dict
    fallback: bool
    repairs: int


class SurfaceComposer:
    """Composes, validates, repairs — and always returns something renderable."""

    def __init__(self, llm_client: Any, settings: Any, sources: SourceRegistry) -> None:
        self._client = llm_client
        self._settings = settings
        self._sources = sources
        # LLM discipline (every LLM feature carries both — F050, epistemic
        # gate): a per-round timeout and an hourly budget, serialized by an
        # in-process lock like the F050 counter.
        self._recent_calls: list[float] = []
        self._budget_lock = asyncio.Lock()

    async def compose(
        self,
        intent: str,
        *,
        archetype: str | None = None,
        data_sources: list[dict] | None = None,
        origin: str = "chat",
        priority: int = 0,
    ) -> ComposedApp:
        data_sources = list(data_sources or [])
        # 1. Server-resolved data first. An unknown source here is the
        # CALLER's error (the agent declared it), not a repair case.
        source_data = await self._sources.resolve(data_sources)
        composed_at = datetime.now(UTC).isoformat(timespec="seconds")

        prompt = self._build_prompt(intent, archetype, source_data)
        errors: list[str] = []
        raw: str | None = None
        parsed: dict | None = None
        repairs = 0

        for attempt in range(MAX_REPAIRS + 1):
            repairs = attempt
            user_message = prompt if not errors else (
                prompt
                + "\n\nYour previous attempt was REJECTED. Fix exactly these "
                + "violations and resend the full JSON object:\n- "
                + "\n- ".join(errors)
                + (f"\n\nYour previous attempt:\n{raw}" if raw else "")
            )
            raw = await self._call_llm(user_message)
            if raw is None:
                # Transport failure or timeout — a TERMINAL condition, not a
                # repair case: retrying an unreachable model burns the repair
                # budget on nothing the model can fix (rev-arch 7b).
                errors = ["LLM unavailable (transport failure or timeout)"]
                break
            parsed = _parse_json_object(raw)
            if parsed is None:
                errors = ["response was not a parseable JSON object"]
                continue
            errors = self._validate(parsed, source_data)
            if not errors:
                break

        if errors or parsed is None:
            logger.warning(
                "F092.1 compose fell back to markdown after %d attempts: %s",
                MAX_REPAIRS + 1,
                errors[:3],
            )
            return self._fallback(intent, composed_at, data_sources, source_data, origin, priority)

        data_model = self._merge_data_model(parsed, source_data, composed_at)
        provenance = {
            key: "model" for key in (parsed.get("dataModel") or {}) if key not in source_data
        }
        refine_options = _clean_refine_options(parsed.get("refine_options"))
        built = BuiltSurface(
            kind="micro_app",
            origin=origin,
            title=str(parsed.get("title") or intent)[:200],
            priority=min(int(priority), 1),
            allowed_actions=["app.close"],
            components=self._with_footer_options(
                parsed["components"], refine_options, has_sources=bool(data_sources)
            ),
            data_model=data_model,
            expires_in=None,
        )
        app_spec = {
            "intent": intent,
            "archetype": str(parsed.get("archetype") or archetype or ""),
            "composed_at": composed_at,
            "refine_options": refine_options,
            "data_sources": data_sources,
            "provenance": provenance,
            # Validated against _THEMES in _validate; default is byte-identical
            # to today's render (F093 §3.1).
            "theme": str(parsed.get("theme") or _DEFAULT_THEME),
        }
        built.app_spec = app_spec
        return ComposedApp(built=built, app_spec=app_spec, fallback=False, repairs=repairs)

    # ------------------------------------------------------------------ refresh

    async def refresh_data(self, app_spec: dict) -> dict:
        """Re-run the app's declared sources ONLY — no LLM. Returns the
        patches for update_data: sourced keys + the new /meta/composedAt.

        An app with NO sources has nothing refresh can honestly do — a
        restamp would advance the header over content that did not move
        (codex P2), so it is refused (422) and compose withholds the
        refresh control from unsourced apps entirely. Mixed apps restamp
        legitimately: the sourced portions were genuinely re-read, and the
        model-supplied sections render amber REGARDLESS of the stamp
        (spec §3.6) — the amber treatment, not the timestamp, is what
        marks that content's age as unknown.
        """
        data_sources = list(app_spec.get("data_sources") or [])
        if not data_sources:
            raise ValueError(
                "this app has no registered data sources — nothing to refresh"
            )
        # No compose round follows a refresh, so it must not be charged for
        # LLM time it will never spend (codex P1).
        source_data = await self._sources.resolve(data_sources, for_compose=False)
        source_data[_META_KEY] = {
            "composedAt": datetime.now(UTC).isoformat(timespec="seconds")
        }
        return source_data

    # ---------------------------------------------------------------- internals

    async def _call_llm(self, user_message: str) -> str | None:
        """One LLM round under the budget cap and per-round timeout.

        Raises ValueError on budget exhaustion (the caller's error — a 422,
        not a fallback). Returns None on timeout/transport failure, which
        the compose loop treats as terminal. NOTE: rounds run inside a tool
        dispatch bounded by NOUS_TOOL_TIMEOUT — keep
        (repairs+1) * a2ui_compose_timeout_seconds under it or the outer
        cancel swallows the real error (prod runs NOUS_TOOL_TIMEOUT=2000).
        """
        async with self._budget_lock:
            now = time.monotonic()
            self._recent_calls = [t for t in self._recent_calls if now - t < 3600.0]
            if len(self._recent_calls) >= self._settings.a2ui_compose_max_per_hour:
                raise ValueError(
                    "compose budget exhausted "
                    f"({self._settings.a2ui_compose_max_per_hour}/hour) — try later"
                )
            self._recent_calls.append(now)
        try:
            return await asyncio.wait_for(
                call_background_llm(
                    self._client,
                    self._model(),
                    "You compose A2UI micro-app surfaces for the Nous companion. "
                    "You emit only valid JSON.",
                    user_message,
                    # A full status app (≤40 components + dataModel) can run
                    # well past 4k output tokens; a truncated response reads
                    # as "not parseable JSON" and burns a repair round on
                    # nothing.
                    # 16k, raised from 8k (2026-08-31): a ledger/briefing app
                    # may legitimately carry 80 components (caps_for), and a
                    # truncated JSON response burns a repair round on a
                    # response the model got RIGHT but could not finish.
                    max_tokens=16000,
                ),
                timeout=float(self._settings.a2ui_compose_timeout_seconds),
            )
        except TimeoutError:
            logger.warning("F092.1 compose LLM round timed out")
            return None

    def _model(self) -> str:
        return self._settings.a2ui_compose_model or self._settings.background_model

    def _build_prompt(
        self, intent: str, archetype: str | None, source_data: dict
    ) -> str:
        if source_data:
            # Mark each source's kind so the model binds series to charts and
            # record lists to repeats/fields BEFORE it guesses (F094 §5).
            marked = {
                f"{k} [{'series — chartable' if is_series(v) else 'records'}]": _sample(v)
                for k, v in source_data.items()
            }
            source_desc = json.dumps(marked, default=str)[:4000]
        else:
            source_desc = (
                "(none — any data you show is model-supplied and must be marked "
                "provenance: model)"
            )
        want = f"Preferred archetype: {archetype}.\n" if archetype else ""
        return (
            f"Compose a micro-app for this intent:\n{intent}\n\n"
            + want
            + _ARCHETYPES
            + "\n"
            + _GRAMMAR_RULES
            + "\n"
            + _THEME_MENU
            + "\n\n"
            + catalog_property_summary()
            + "\nServer-resolved data available at these data-model keys "
            + "(bind with {\"path\": \"/<key>/...\"} — do NOT copy the values "
            + "into dataModel, the server injects them; the [series]/[records] "
            + "tag tells you which sources a chart can bind):\n"
            + source_desc
            + "\n\n"
            + _RESPONSE_SHAPE
        )

    def _validate(self, parsed: dict, source_data: dict) -> list[str]:
        components = parsed.get("components")
        if not isinstance(components, list) or not components:
            return ["components must be a non-empty array"]
        # Reject non-object entries OUTRIGHT (codex P2): the checks below
        # filter them out to survive, but the accepted array is consumed
        # unfiltered downstream — a stray null must be a repair error, not
        # an AttributeError that skips both the repair loop and the fallback.
        bad = sum(1 for c in components if not isinstance(c, dict))
        if bad:
            return [f"components contains {bad} non-object entr{'y' if bad == 1 else 'ies'}"]
        model_supplied = parsed.get("dataModel")
        if model_supplied is not None and not isinstance(model_supplied, dict):
            return ["dataModel must be an object"]

        errors: list[str] = []
        theme = parsed.get("theme")
        # `theme not in _THEMES` would hash the value — a model-emitted dict or
        # list raises TypeError INSIDE _validate, escaping the repair loop and
        # the guaranteed fallback (codex P2). Reject a non-string as invalid.
        if theme is not None and (not isinstance(theme, str) or theme not in _THEMES):
            errors.append(
                f"theme {theme!r} is not one of {sorted(_THEMES)} — pick a listed "
                "theme or omit it for the default"
            )
        # Archetype selects the grammar caps and is fed to lint_micro_app; a
        # non-string one would raise TypeError there (same hash trap as theme).
        archetype = parsed.get("archetype")
        if archetype is not None and not isinstance(archetype, str):
            errors.append("archetype must be a string or omitted")
        for key in model_supplied or {}:
            if key in source_data:
                errors.append(
                    f"dataModel key {key!r} shadows a server-resolved source — "
                    "bind to it instead of supplying values"
                )
            if key == _META_KEY:
                errors.append("dataModel key 'meta' is server-owned")

        by_id = {c.get("id"): c for c in components if isinstance(c, dict)}
        header = by_id.get("header")
        if header is None or header.get("component") != "AppHeader":
            errors.append('the AppHeader must have id "header"')
        elif header.get("composedAt") != {"path": "/meta/composedAt"}:
            errors.append(
                'AppHeader.composedAt must be exactly {"path": "/meta/composedAt"}'
            )
        footer = by_id.get("footer")
        if footer is None or footer.get("component") != "AppFooter":
            errors.append('the AppFooter must have id "footer"')
        for comp in components:
            if isinstance(comp, dict) and comp.get("action") is not None:
                errors.append(
                    f"component {comp.get('id')!r} carries an action — "
                    "micro-app controls live in the AppFooter only"
                )

        errors += lint_micro_app(
            [c for c in components if isinstance(c, dict)],
            archetype=parsed.get("archetype"),
        )

        # --- data-aware rules (F093 §5.1, F094 §5) — need the RESOLVED
        # source data, which is why they live here and not in grammar. ---
        full_model = self._merge_data_model(parsed, source_data, "1970-01-01T00:00:00+00:00")
        comp_dicts = [c for c in components if isinstance(c, dict)]
        bindings = _collect_bindings(comp_dicts)
        errors += _binding_rules(comp_dicts, source_data, full_model, bindings)

        # Full schema + structural pass on a probe build. Composer-level
        # errors above are cheaper to report first, but schema errors must
        # also reach the repair loop verbatim.
        probe = BuiltSurface(
            kind="micro_app",
            origin="chat",
            title=str(parsed.get("title") or "probe"),
            allowed_actions=["app.close"],
            components=[c for c in components if isinstance(c, dict)],
            data_model=self._merge_data_model(parsed, source_data, "1970-01-01T00:00:00+00:00"),
        )
        try:
            probe.validate()
        except SurfaceValidationError as exc:
            errors += [str(e) for e in exc.errors[:5]]
        return errors

    def _merge_data_model(
        self, parsed: dict, source_data: dict, composed_at: str
    ) -> dict:
        """Server authority: sourced keys and /meta always come from the
        server; the model's dataModel fills only the gaps it declared."""
        model = dict(parsed.get("dataModel") or {})
        model.update(source_data)
        model[_META_KEY] = {"composedAt": composed_at}
        return model

    def _with_footer_options(
        self,
        components: list[dict],
        refine_options: list[dict],
        *,
        has_sources: bool,
    ) -> list[dict]:
        """The footer renders what the SERVER validated, not what the model
        drew: refineOptions is overwritten from the cleaned list so the
        buttons and the app_spec allowlist cannot diverge, and the refresh
        control is withheld from apps with no sources — refresh on an
        unsourced app could only restamp content that did not move
        (codex P2)."""
        out = []
        for comp in components:
            if comp.get("id") == "footer":
                comp = {**comp, "refineOptions": refine_options, "showRefresh": has_sources}
            out.append(comp)
        return out

    def _fallback(
        self,
        intent: str,
        composed_at: str,
        data_sources: list[dict],
        source_data: dict,
        origin: str,
        priority: int,
    ) -> ComposedApp:
        """Grammar-conforming markdown fallback — degraded, never broken."""
        body = f"Could not compose a structured app for:\n\n**{intent}**"
        if source_data:
            body += "\n\nRaw data:\n```\n" + json.dumps(source_data, default=str)[:3000] + "\n```"
        built = BuiltSurface(
            kind="micro_app",
            origin=origin,
            title=intent[:120],
            priority=min(int(priority), 1),
            allowed_actions=["app.close"],
            components=[
                {
                    "id": "root",
                    "component": "Column",
                    "children": ["header", "sec_body", "footer"],
                    "align": "stretch",
                },
                {
                    "id": "header",
                    "component": "AppHeader",
                    "title": intent[:120],
                    "subtitle": "composition fallback",
                    "composedAt": {"path": "/meta/composedAt"},
                    "staleAfterS": float(self._settings.a2ui_app_stale_after_s),
                },
                {"id": "sec_body", "component": "Section", "title": "Content", "child": "body", "provenance": "model"},
                {"id": "body", "component": "Text", "text": body},
                {
                    "id": "footer",
                    "component": "AppFooter",
                    "refineOptions": [],
                    "showRefresh": bool(data_sources),
                },
            ],
            data_model={_META_KEY: {"composedAt": composed_at}, **source_data},
            expires_in=None,
        )
        app_spec = {
            "intent": intent,
            "archetype": "fallback",
            "composed_at": composed_at,
            "refine_options": [],
            "data_sources": data_sources,
            "provenance": {} if source_data else {"body": "model"},
        }
        built.app_spec = app_spec
        return ComposedApp(built=built, app_spec=app_spec, fallback=True, repairs=MAX_REPAIRS)


_CHART_COMPONENTS = frozenset({"Sparkline", "LineChart", "BarChart"})


def _collect_bindings(components: list[dict]) -> list[str]:
    """Every data-model path any component binds — DynamicValue {path:…}
    anywhere in the component, a chart's string `path`, and a Repeat
    template's `path`. Used by the unread-source and over-capacity rules."""
    paths: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            p = value.get("path")
            if isinstance(p, str):
                paths.append(p)
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    for comp in components:
        walk(comp)
        # A chart's `path` is a plain string prop, not a {path:…} binding.
        if comp.get("component") in _CHART_COMPONENTS and isinstance(comp.get("path"), str):
            paths.append(comp["path"])
    return paths


def _get_path(model: dict, path: str) -> Any:
    """Resolve an absolute JSON-pointer-ish path (/a/b/0) against a model."""
    cur: Any = model
    for seg in path.strip("/").split("/"):
        if seg == "":
            continue
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _is_nonempty(value: Any) -> bool:
    if isinstance(value, dict) and value.get("kind") == "series":
        return bool(value.get("points"))
    if isinstance(value, (list, dict, str)):
        return len(value) > 0
    return value is not None


def _child_ids(comp: dict) -> list[str]:
    """Component ids this component references — a `child` string, a `children`
    list of strings, or a Repeat template's `children.componentId`."""
    out: list[str] = []
    if isinstance(comp.get("child"), str):
        out.append(comp["child"])
    children = comp.get("children")
    if isinstance(children, list):
        out += [c for c in children if isinstance(c, str)]
    elif isinstance(children, dict) and isinstance(children.get("componentId"), str):
        out.append(children["componentId"])
    return out


def _containing_templates(comp_id: str, by_id: dict[str, dict]) -> list[str]:
    """The source paths of every Repeat template whose subtree contains
    ``comp_id``. A Repeat template is a component whose ``children`` is a
    ``{componentId, path}`` dict; its ``componentId`` roots a per-item scope
    (pointer.ts `absolute`). More than one means NESTED scopes — the renderer
    composes each template path against its parent scope, which this static
    resolver does not model, so a nested chart is left to the renderer rather
    than validated against a wrong path (codex P2)."""
    found: list[str] = []
    for comp in by_id.values():
        children = comp.get("children")
        if not (isinstance(children, dict) and isinstance(children.get("path"), str)):
            continue
        root = children.get("componentId")
        if not isinstance(root, str):
            continue
        seen, stack = set(), [root]
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            node = by_id.get(cid)
            if node:
                stack.extend(_child_ids(node))
        if comp_id in seen:
            found.append(children["path"])
    return found


def _chart_shape_errors(ctype: str, comp: dict, path: str, resolved: Any) -> list[str]:
    """Series-shape (rule 2), single-value (2b) and LineChart-arity (3) checks
    for ONE resolved chart target. Returned as a list so a repeat can validate
    every item independently (codex P2)."""
    errs: list[str] = []
    if not (isinstance(resolved, dict) and resolved.get("kind") == "series"):
        shape = (
            "an array"
            if isinstance(resolved, list)
            else "nothing"
            if resolved is None
            else type(resolved).__name__
        )
        errs.append(
            f"{ctype} {comp.get('id')!r} binds {path} which resolved to "
            f"{shape}, not a series — chart paths need a series-shaped source"
        )
        return errs
    # `points` must be an array: {kind:"series", points:{}} passes the kind
    # check but readSeries requires a list and renders "not a series" (codex P2).
    if not isinstance(resolved.get("points"), list):
        errs.append(
            f"{ctype} {comp.get('id')!r} binds {path}: series `points` must be an "
            f"array, got {type(resolved.get('points')).__name__}"
        )
        return errs
    # Sparkline and BarChart both read the default `v` key, so a MULTI-series
    # source (named keys, no `v`) renders "no data" despite valid rows.
    if ctype in ("Sparkline", "BarChart") and isinstance(resolved.get("keys"), list):
        errs.append(
            f"{ctype} {comp.get('id')!r} binds {path}, a multi-series source "
            f"(keys {sorted(str(k) for k in resolved['keys'])}) — Sparkline/BarChart "
            "read a single-value (v) series; use LineChart or a single-key source"
        )
        return errs
    if ctype == "LineChart":
        specs = comp.get("series") or []
        if not specs:
            errs.append(f"LineChart {comp.get('id')!r} declares no series — name 1–4 keys")
        elif len(specs) > 4:
            errs.append(f"LineChart {comp.get('id')!r} has {len(specs)} series — max 4")
        # Declared keys are authoritative even when points is empty; else fall
        # back to the union across ALL points (a per-point non-finite reading is
        # omitted from that point), never points[0] alone.
        points = resolved.get("points") or []
        declared = resolved.get("keys")
        available = (
            {str(k) for k in declared}
            if isinstance(declared, list)
            else {k for p in points if isinstance(p, dict) for k in p if k != "t"}
        )
        if points or isinstance(declared, list):
            for spec in specs:
                key = spec.get("key") if isinstance(spec, dict) else None
                if isinstance(key, str) and key not in available:
                    errs.append(
                        f"LineChart {comp.get('id')!r} series key {key!r} is absent "
                        f"from the series (keys: {sorted(available)})"
                    )
    return errs


def _binding_rules(
    components: list[dict], source_data: dict, full_model: dict, bindings: list[str]
) -> list[str]:
    errors: list[str] = []

    # (1) Unread-source (F093 §1.1): a source that resolved non-empty must be
    # bound by SOMETHING, or its records are silently dropped and the model
    # inlined strings instead.
    for key, value in source_data.items():
        if not _is_nonempty(value):
            continue
        if not any(b == f"/{key}" or b.startswith(f"/{key}/") for b in bindings):
            errors.append(
                f"source {key!r} resolved data but no component binds /{key} — "
                "bind it (a chart, a repeat template, or a field) instead of "
                "inlining values"
            )

    by_id = {c["id"]: c for c in components if isinstance(c.get("id"), str)}
    for comp in components:
        ctype = comp.get("component")
        if ctype not in _CHART_COMPONENTS:
            continue
        path = comp.get("path")
        if not isinstance(path, str) or not path.strip():
            continue  # binding-mandatory already caught this in grammar
        # A relative chart path resolves the way the renderer resolves it
        # (pointer.ts `absolute`): an absolute /path from root; inside a repeat
        # template, against EACH item base; with no scope, from root as /path.
        # Validate every resolved target — a root lookup would false-reject a
        # per-item chart (codex round 4), a blanket skip lets `{trend:[]}`
        # through (round 6), and validating only item 0 lets a later
        # heterogeneous item through (round 7).
        if path.startswith("/"):
            targets = [(path, _get_path(full_model, path))]
        else:
            templates = _containing_templates(comp.get("id", ""), by_id)
            if len(templates) > 1:
                continue  # nested scopes — the renderer composes; leave to it
            if not templates:
                targets = [("/" + path, _get_path(full_model, "/" + path))]
            else:
                source_path = templates[0]
                items = _get_path(full_model, source_path)
                if not isinstance(items, list) or not items:
                    continue  # template has no items yet — nothing to validate
                targets = [
                    (f"{source_path}/{i}/{path}", _get_path(full_model, f"{source_path}/{i}/{path}"))
                    for i in range(len(items))
                ]
        for tpath, resolved in targets:
            errs = _chart_shape_errors(ctype, comp, tpath, resolved)
            if errs:
                errors.extend(errs)
                break  # one failing item rejects the chart — no N duplicates

    # (4) Over-capacity / under-render (F093 §5.1 / AC7): a record-LIST source
    # bound only by FIXED indices that cover fewer than all records renders a
    # partial source as if complete. A repeat template (binds /key with no
    # index) covers all records, so it exempts the key.
    for key, value in source_data.items():
        if not isinstance(value, list) or not value:
            continue
        n = len(value)
        template_bound = any(b == f"/{key}" for b in bindings)
        if template_bound:
            continue
        prefix = f"/{key}/"
        prefix_bound = any(b.startswith(prefix) for b in bindings)
        indices = set()
        for b in bindings:
            if b.startswith(prefix):
                seg = b[len(prefix) :].split("/", 1)[0]
                if seg.isdigit():
                    indices.add(int(seg))
        # Coverage is the count of DISTINCT IN-RANGE bound indices — not max+1,
        # not the raw count, and not merely "some index binding exists". Binding
        # /key/11 of 12 has max+1 == n; n-1 real indices plus /key/999 has raw
        # count == n; binding only /key/foo (non-numeric) or /key/999 has ZERO
        # valid indices — all leave real records unrendered (codex P2). Only
        # indices inside range(n) render a record; a prefix binding with none
        # valid renders NOTHING, which is under-render at its worst.
        covered = {i for i in indices if i < n}
        if prefix_bound and len(covered) < n:
            errors.append(
                f"source {key!r} resolved {n} records but only {len(covered)} "
                "are bound by fixed index — use a repeat template so all render, "
                "never a partial slice of a complete source"
            )

    return errors


def _parse_json_object(raw: str) -> dict | None:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# Issue #620 gap 1 — verbs the micro-app grammar cannot perform under ANY
# model response. A refine option is not a dispatched action: `app_refine`
# appends the option's LABEL to the intent and re-composes against the SAME
# stored data_sources, so a button promising a file or a message is
# unsatisfiable by construction. F092.1 makes invented DATA visible (amber
# provenance); there was no equivalent for invented CAPABILITY, so a fake
# button was pixel-identical to a real one and surfaced a raw ValueError on
# press. Rejecting at compose is consistent with an unknown theme being a hard
# error rather than a silent default.
# Matched as COMMAND PHRASES, never bare substrings (codex P2). Containment
# alone reads "Group by sender" as send, "Email volume by week" as email, and
# "Compare attachment types" as attach — all legitimate analytical labels for
# mail/file dashboards. Rejecting those is worse than the bug being fixed:
# repeated false matches burn the repair loop and force a markdown fallback.
# So: imperative verbs only where they can ONLY be imperative — anchored at
# the start of the label, or in a phrase that has no noun reading.
_REFINE_COMMAND_RE = re.compile(
    # Imperative only where it can ONLY be imperative: anchored at the START of
    # the label, or inside a phrase with no noun reading ("email me", "save to").
    # So "Group by sender", "Email volume by week" and "Compare attachment
    # types" all pass — those are analytical labels for mail/file dashboards,
    # and rejecting them would burn the repair loop into a markdown fallback.
    # A label OPENING with the verb is still rejected even when a noun reading
    # exists ("Export trends by month"): leading-imperative is the strongest
    # signal available, and a rephrase costs one repair round while an
    # undeliverable button costs the user a raw ValueError.
    # NOTE: export/download/print/upload deliberately live in
    # _LEADING_ACTION_RE instead, so the analytical exemption applies to them
    # too — "Print volume by department" and "Download counts by file type" are
    # metrics, while "Export raw data" and "Print this" still fail (codex P2).
    # A leading messaging/scheduling verb followed by a determiner, possessive
    # or cadence word is imperative — "Email the report", "Notify the team",
    # "Schedule monthly digest". Followed by a measurement noun it is a noun
    # modifier and passes: "Email volume by week", "Send rate by hour".
    r"^\s*(?:e-?mail|send|notify|remind|text|share|schedule|deliver|post)\s+"
    r"(?:me|us|it|them|the|a|an|this|that|these|those|my|our|all|everyone|team|"
    r"daily|weekly|monthly|quarterly|nightly|hourly)\b"
    # A delivery target, but only when the verb OPENS the label — i.e. it is an
    # imperative clause: "Send report to Alice", "Email summary to ops". Not
    # anywhere, which matched "Compare email volume to last month" from `email`
    # through `to l` and rejected a perfectly good comparison (codex P2).
    r"|^\s*(?:e-?mail|send|deliver|post)\b[^,;]*\bto\s+\w"
    # The ONLY unanchored branch, and safe because the pronoun object makes it
    # imperative in every reading: "notify me", "send us". A bare noun after
    # the same verb ("send rate", "email volume") is analysis and passes.
    r"|\b(?:e-?mail|send|notify|remind|text)\s+(?:me|us|it|them)\b"
    # Everything below is START-ANCHORED. Four review rounds each found the
    # same defect in a different token — "Group by sender", "Email volume by
    # week", "Compare market share with last month", "Compare subscribe vs
    # purchase" — so the rule is now categorical rather than per-word: a verb
    # only counts as a command when it OPENS the label. Mid-label it is a noun
    # naming a metric or an event, and rejecting those burns the repair loop
    # into a markdown fallback, losing the whole app to save one button.
    r"|^\s*schedule\s+(?:a|an|the|this|daily|weekly|monthly|quarterly)\b"
    r"|^\s*save\s+(?:to|as)\b"
    r"|^\s*(?:export|download)\s+as\b"
    r"|^\s*share\s+(?:via|with|to)\b"
    r"|^\s*subscribe\s+(?:to|me|us)\b"
    # File-generation imperatives — "Generate CSV", "Create a PDF", "Open in
    # Excel". No component emits a file, so these are as undeliverable as
    # export; pressing one just recomposes the same data (codex P2).
    r"|^\s*(?:generate|create|produce|make|open|render|build)\b[^,;]{0,24}"
    r"\b(?:csv|pdf|xlsx?|excel|spreadsheet|zip|docx?)\b"
    # Calendar/reminder imperatives — "Add to calendar", "Set a reminder",
    # "Create calendar event", "Book a slot". None open with a delivery verb,
    # and `create` only matched the file branch (codex P2). A micro-app has no
    # component that writes a calendar any more than it has one that emails.
    r"|^\s*(?:add|create|make|set|book|schedule)\b[^,;]{0,24}"
    r"\b(?:calendar|reminder|event|invite|meeting|appointment|slot)\b",
    re.IGNORECASE,
)


# A delivery/scheduling verb OPENING the label, with any object at all —
# "Email report", "Notify stakeholders", "Remind Alice", "Schedule digest".
# Enumerating the objects that may follow could never keep up (codex P2), so
# the default for a leading action verb is INVERTED: it is a command unless
# the label reads analytically.
_LEADING_ACTION_RE = re.compile(
    r"^\s*(?:e-?mail|send|notify|remind|text|share|schedule|deliver|post|subscribe"
    r"|export|download|print|upload"
    # MUTATION verbs (codex P2). A micro-app is read-only by construction —
    # the grammar bans every input component — so "Archive completed tasks",
    # "Delete old records", "Approve request" and "Mark all as read" are as
    # undeliverable as an export. The analytical exemption still applies:
    # "Approval rate by reviewer" and "Delete volume per day" are metrics.
    r"|archive|delete|remove|approve|reject|dismiss|resolve|close|acknowledge"
    r"|mark|assign|rename|edit|update|merge|cancel|retry|restart|snooze|mute"
    r"|clear|purge|reset)\b",
    re.IGNORECASE,
)

# ...and this is what "reads analytically" means. These markers are what a
# metric label has and an imperative does not: a grouping ("by week"), a
# comparison ("vs"), or a measurement noun. "Email volume by week" and "Send
# rate by hour" survive; "Email report" does not.
_ANALYTICAL_RE = re.compile(
    # "by" groups — unless it introduces a DEADLINE ("Schedule review by
    # Monday"), which is a command, not a grouping (codex P2).
    r"\bby\b(?!\s+(?:mon|tues?|wed|thur?s?|fri|sat|sun|tomorrow|today|tonight|"
    r"eod|eow|noon|midnight|next|end\b|\d))"
    r"|\b(?:per|vs\.?|versus|between|across|over)\b"
    # Measurement nouns only. Deliberately NOT summary/overview/report/digest:
    # those are things you would EMAIL, so listing them as analytical exempted
    # "Email summary" and "Send overview" — the exemption was swallowing the
    # very commands it sits behind (codex P2).
    r"|\b(?:trend|trends|rate|rates|volume|count|counts|total|totals|share of|"
    r"ratio|average|median|mean|percentile|growth|distribution|breakdown|"
    r"histogram|funnel|leaderboard|activity|latency|throughput|adherence|"
    r"conversion|split|mix|cohort|heatmap)\b",
    re.IGNORECASE,
)


def _refine_capability_errors(raw: Any) -> list[str]:
    """Repair errors for refine options promising capabilities that do not
    exist. Checked on the RAW model output, before _clean_refine_options
    quietly normalizes it."""
    errors: list[str] = []
    if not isinstance(raw, list):
        return errors
    for opt in raw[:4]:
        if not isinstance(opt, dict):
            continue
        label = str(opt.get("label") or "").strip()
        match = _REFINE_COMMAND_RE.search(label)
        if match is None:
            leading = _LEADING_ACTION_RE.match(label)
            if leading and not _ANALYTICAL_RE.search(label):
                match = leading
        if match:
            errors.append(
                f"refine option {label!r} promises {match.group(0).strip()!r}, which no "
                "micro-app component can do — refine RE-RENDERS the existing data "
                "sources, it cannot export, send or schedule anything. Offer a "
                "different view of the same data instead."
            )
    return errors


def _clean_refine_options(raw: Any) -> list[dict]:
    """Normalize the model's refine options, dropping any the system cannot
    deliver.

    A capability failure DROPS the option rather than failing validation
    (issue #620). Eight review rounds established that no lexical rule
    separates "Print volume by department" (a metric) from "Schedule report
    distribution across teams" (a command) — they are the same shape, and the
    distinction is semantic. So the classifier is treated as what it is: a
    heuristic. Wiring a heuristic into `_validate` made every misjudgement
    cost the WHOLE APP, because repeated rejections exhaust the repair loop
    into the markdown fallback. Dropping instead bounds the blast radius to
    one button: a false positive loses an option nobody may have wanted, a
    false negative leaves today's behaviour. The prompt, which now states that
    refine re-renders existing sources, is what actually prevents these at
    source; this is the backstop.
    """
    if not isinstance(raw, list):
        return []
    out = []
    for opt in raw[:4]:
        if not isinstance(opt, dict):
            continue
        oid = str(opt.get("id") or "").strip()
        label = str(opt.get("label") or "").strip()
        if not (oid and label):
            continue
        undeliverable = _refine_capability_errors([opt])
        if undeliverable:
            logger.info("F092.1 dropped refine option: %s", undeliverable[0])
            continue
        out.append({"id": oid[:60], "label": label[:80]})
    return out


def _sample(value: Any) -> Any:
    """First items / truncated shape so the prompt shows structure, not bulk."""
    if isinstance(value, list):
        return value[:2] + ([f"... {len(value) - 2} more"] if len(value) > 2 else [])
    if isinstance(value, dict):
        return {k: _sample(v) for k, v in list(value.items())[:8]}
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + "…"
    return value


__all__ = ["SurfaceComposer", "ComposedApp", "UnknownSourceError"]
