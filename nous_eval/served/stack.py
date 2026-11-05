"""The read-path components of one run, wired as nous/main.py wires them.

Database, the runtime overrides in nous_system.config (main.py loads them at startup; an arm
that sets one of their keys wins over the stored value), the fusion parameters published to the
environment (``nous.heart.search`` re-reads them per query), the embedding provider behind a
disk cache, Heart with query expansion, residual activation and the date-window parser wired
when their flags are on, Brain, the agent tools and the ContextEngine.

Read-path writes that would make results depend on query order are held off identically for
every arm: fact access tracking (recall_count UPDATEs, which also add HNSW tuples and drift later
approximate searches) and the keyed leg's own tracking. The date-window parser is wrapped by a
store shared across runs, keyed (as-of date, query, model) and holding the unpadded window: the
parse a query gets is fixed once and every later run reuses it, so latency excludes the parse
and arms see the same windows. Query-expansion variants are shared the same way when expansion
is on. Formatters are observed, not changed: recall_deep's (the order the model reads its items)
and the pre-turn section formatters (the text each item got, and its place).
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import time
from array import array
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nous_eval.served.judge import append_lines, read_jsonl

IDENTITY = "You are Nous, a cognitive agent."
_LEAF_RESOLVERS = (
    "_resolve_vector_weight",
    "_resolve_rrf_k",
    "_resolve_keyword_enabled",
    "_resolve_keyword_or_fallback_enabled",
)


class EmbedDiskCache:
    """The embedding provider's in-memory cache, persisted as JSONL (float32 both ways, so a
    fresh vector and a cached one are the same numbers)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, str] = {d["k"]: d["v"] for d in read_jsonl(path)}

    def install(self, provider: Any) -> bool:
        if not hasattr(provider, "_cache"):
            return False  # a test double
        provider._cache_size = max(getattr(provider, "_cache_size", 0), 1_000_000)
        for k, v in self.entries.items():
            a = array("f")
            a.frombytes(base64.b64decode(v))
            provider._cache[k] = a
        orig_embed, orig_batch = provider.embed, provider.embed_batch

        async def embed(text: str, *, fail_fast: bool = False) -> list[float]:
            return array("f", await orig_embed(text, fail_fast=fail_fast)).tolist()

        async def embed_batch(texts: list[str], *, fail_fast: bool = False) -> list[list[float]]:
            out = await orig_batch(texts, fail_fast=fail_fast)
            return [array("f", v).tolist() if v else v for v in out]

        provider.embed, provider.embed_batch = embed, embed_batch
        return True

    def save(self, provider: Any) -> int:
        if not hasattr(provider, "_cache"):
            return 0
        new = [
            (k, base64.b64encode(array("f", a).tobytes()).decode())
            for k, a in provider._cache.items()
            if k not in self.entries
        ]
        append_lines(self.path, [json.dumps({"k": k, "v": v}) + "\n" for k, v in new])
        self.entries.update(new)
        return len(new)


class CachedDateParser:
    """Stands in for ``heart.date_window_parser``: the code's own gate, then a persistent window
    store keyed (as-of date, query, model). A miss parses once (the pinned code's LLM parse, up to
    three tries on a transient failure) against the query's as-of date, not today.

    The store holds the UNPADDED window: ``inner`` must parse with ``date_leg_pad_days`` 0, and
    each run applies its own ``pad_days`` on the way out, so an arm that changes the padding is
    not served another arm's padded window."""

    def __init__(self, inner: Any, path: Path, as_of: dict[str, datetime.date], model: str, pad_days: int) -> None:
        self.inner, self.path, self.as_of, self.model, self.pad_days = inner, path, as_of, model, pad_days
        self.store = {d["sk"]: d for d in read_jsonl(path)}
        self.last: dict[str, Any] | None = None

    def _sk(self, day: datetime.date, query: str) -> str:
        return hashlib.sha1(f"{day.isoformat()}\x00{self.model}\x00{query}".encode()).hexdigest()

    async def parse(self, query: str, today: datetime.date) -> Any:
        from nous.heart.date_window import DateWindow, has_temporal_signal

        self.last = {"gate": bool(query and has_temporal_signal(query)), "window": None, "cached": None}
        if not self.last["gate"]:
            return None
        day = self.as_of.get(query, today)
        sk = self._sk(day, query)
        rec = self.store.get(sk)
        self.last["cached"] = rec is not None
        if rec is None:
            rec = await self._fetch(query, day, sk)
        if rec["window"]:
            pad = datetime.timedelta(days=self.pad_days)
            try:
                w = DateWindow(
                    start=datetime.date.fromisoformat(rec["window"][0]) - pad,
                    end=datetime.date.fromisoformat(rec["window"][1]) + pad,
                )
            except OverflowError:  # as DateWindowParser: a confirmed but unusable window
                return None
            self.last["window"] = [w.start.isoformat(), w.end.isoformat()]
            return w
        return None

    async def prefill(self, queries: list[str]) -> dict[str, int]:
        """Parse every gated query the store lacks before anything is measured, so no measured
        call pays for a parse and the first run's latency matches later runs'."""
        from nous.heart.date_window import has_temporal_signal

        stats = {"gated": 0, "parsed_now": 0, "transient": 0}
        for q in dict.fromkeys(queries):
            if not (q and has_temporal_signal(q)):
                continue
            stats["gated"] += 1
            day = self.as_of.get(q, datetime.date.today())
            sk = self._sk(day, q)
            if sk not in self.store:
                rec = await self._fetch(q, day, sk)
                stats["parsed_now"] += 1
                stats["transient"] += rec["status"] == "transient"
        return stats

    async def _fetch(self, query: str, day: datetime.date, sk: str) -> dict:
        tries, window, ok = [], None, False
        for _ in range(3):
            t0 = time.perf_counter()
            ok, window = await self.inner._parse_llm(query, day)
            tries.append({"ms": round((time.perf_counter() - t0) * 1000, 1), "cacheable": ok})
            if ok:
                break
        rec = {
            "sk": sk,
            "day": day.isoformat(),
            "model": self.model,
            "status": ("window" if window else "no_date") if ok else "transient",
            "window": [window.start.isoformat(), window.end.isoformat()] if window else None,
            "tries": tries,
        }
        if ok:  # a transient failure is not stored: the next run parses again
            self.store[sk] = rec
            append_lines(self.path, [json.dumps(rec) + "\n"])
        return rec


class ExpansionStore:
    """Query-expansion variants shared by every run of a data dir, keyed (query hash, model,
    max variants): the clone's ``heart.query_expansions`` table is thrown away with the clone, so
    without this each arm would ask the LLM again for a query the template lacked and retrieve
    with different variants."""

    def __init__(self, path: Path, settings: Any) -> None:
        self.path = path
        self.config = f"{settings.query_expansion_model}\x00{settings.query_expansion_max_variants}"
        self.rows = {d["k"]: d["variants"] for d in read_jsonl(path)}

    def _k(self, h: bytes) -> str:
        return hashlib.sha1(h.hex().encode() + b"\x00" + self.config.encode()).hexdigest()

    def get(self, h: bytes) -> list[str] | None:
        k = self._k(h)
        if k not in self.rows:  # another run may have stored it since this one loaded the file
            self.rows.update({d["k"]: d["variants"] for d in read_jsonl(self.path)})
        return self.rows.get(k)

    def put(self, h: bytes, variants: list[str]) -> None:
        k = self._k(h)
        if k not in self.rows and variants:
            self.rows[k] = list(variants)
            append_lines(self.path, [json.dumps({"k": k, "variants": list(variants)}) + "\n"])


def share_expansions(expander: Any, store: ExpansionStore) -> None:
    """Route the expander's cache through the shared store: a stored entry wins (the DB lookup
    still runs first, as prod's does), a DB hit or a fresh expansion is stored for later runs."""
    orig_get, orig_put = expander._cache_get, expander._cache_put

    async def cache_get(h: bytes) -> list[str] | None:
        from_db = await orig_get(h)
        shared = store.get(h)
        if shared is not None:
            return shared
        if from_db is not None:
            store.put(h, from_db)
        return from_db

    async def cache_put(h: bytes, query: str, variants: list[str]) -> None:
        store.put(h, variants)
        await orig_put(h, query, variants)

    expander._cache_get, expander._cache_put = cache_get, cache_put


@dataclass
class Stack:
    settings: Any
    db: Any
    heart: Any
    brain: Any
    tools: dict
    ctx: Any
    classifier: Any
    frames: Any
    recorder: Any
    runtime: dict
    leaf_start: dict
    leaf_seen: dict
    date_parser: CachedDateParser | None
    embeddings: Any = None
    ce_calls: list = field(default_factory=list)
    render_orders: list = field(default_factory=list)  # one per recall_deep call: [(type, id), ...] as read
    formatter_calls: list = field(default_factory=list)  # the pre-turn build's section formatter calls
    listed_episodes: list = field(default_factory=list)  # the build's last heart.list_episodes result


def embedding_cache_size(provider: Any) -> int | None:
    """Entries in the provider's in-memory cache (it only grows: capacity is raised to 1M), so the
    growth over a phase counts the embedding API requests made in it; None for a test double."""
    cache = getattr(provider, "_cache", None)
    return len(cache) if cache is not None else None


def render_order(results: list, emitted: list, parent_episodes: list | None) -> list[tuple[str, str]]:
    """The order the model reads ``recall_deep``'s items, from what ``_format_pipeline_text``
    reported emitting: Heart Memory (session-grouped and date-ordered as rendered), Graph-Connected
    and Brain Decisions, then the Parent Episode Context section, then the stored-examples block.
    A row emitted twice (a decision in two sections) keeps its first place."""
    exemplar = {
        (str(r.id), r.type) for r in results if (getattr(r, "metadata", None) or {}).get("retrieval_leg") == "exemplar"
    }
    keys = [(str(i), t) for i, t in emitted]
    order = [k for k in keys if k not in exemplar]
    order += [(str(ep_id), "episode") for ep_id, _ in (parent_episodes or ())]
    order += [k for k in keys if k in exemplar]
    return [(t, i) for i, t in dict.fromkeys(order)]


def _capture_render_order(sink: list) -> Any:
    """Wrap ``nous.api.tools._format_pipeline_text`` (the recall_deep closure looks it up by name
    at call time) so every call records its read order in ``sink``."""
    import nous.api.tools as tools_mod

    orig = tools_mod._format_pipeline_text

    def wrapped(*a: Any, **kw: Any) -> str:
        emitted = kw.get("emitted_out")
        if emitted is None:
            emitted = kw["emitted_out"] = []
        text = orig(*a, **kw)
        parents = kw.get("parent_episodes", a[3] if len(a) > 3 else None)
        sink.append(render_order(a[0] if a else kw["results"], emitted, parents))
        return text

    tools_mod._format_pipeline_text = wrapped
    return orig


def _capture_formatters(stack: Stack) -> None:
    """Record every call the pre-turn build makes to a section formatter (facts, decisions, Past
    Episodes, procedure lines) with its arguments, and the blocks ``_format_procedure_bodies``
    returns. preturn.formatter_lines re-renders each item from these after the timed build, so
    the wrappers only append to a list inside it."""
    ctx = stack.ctx
    for name, kind in (
        ("_format_facts", "fact"),
        ("_format_decisions", "decision"),
        ("_format_episodes", "episode"),
        ("_format_procedures", "procedure"),
    ):
        orig = getattr(ctx, name)

        def wrapped(items: list, _orig: Any = orig, _kind: str = kind, **kw: Any) -> str:
            stack.formatter_calls.append((_kind, _orig, list(items), dict(kw)))
            return _orig(items, **kw)

        setattr(ctx, name, wrapped)
    orig_bodies = ctx._format_procedure_bodies

    def bodies(details: list, per_item_cap: int) -> list[str]:
        blocks = orig_bodies(details, per_item_cap)
        stack.formatter_calls.append(("procedure_blocks", list(zip(details, blocks, strict=False))))
        return blocks

    ctx._format_procedure_bodies = bodies


def make_embeddings(settings: Any) -> Any:
    """The eval embedding provider (model and dimensions from the run's settings)."""
    from nous_eval.retrieval_runner import make_eval_embedding_provider

    emb = make_eval_embedding_provider(settings)
    if emb is None:
        raise SystemExit("OPENAI_API_KEY is required (embeddings); pass --env-file")
    return emb


def _hold_read_writes(heart: Any) -> None:
    async def _no_track(ids: Any) -> None:
        return None

    heart.facts._fire_track_access = lambda ids: None
    heart.facts.track_access = _no_track
    orig_fetch = heart.facts.fetch_by_entity_keys

    async def fetch_untracked(keys: Any, limit: int = 8, *, track: bool = True, **kw: Any) -> Any:
        return await orig_fetch(keys, limit, track=False, **kw)

    heart.facts.fetch_by_entity_keys = fetch_untracked


def _install_ce_timer(calls: list) -> Any:
    """Wrap heart's cross_encoder_rerank to record each call's wall time and candidate count."""
    import nous.heart.heart as hh

    orig = hh.cross_encoder_rerank

    async def timed(*a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return await orig(*a, **kw)
        finally:
            n = len(kw.get("candidates") or (a[1] if len(a) > 1 else []))
            calls.append(
                {"ms": round((time.perf_counter() - t0) * 1000, 1), "n": min(n, int(kw.get("max_candidates") or n))}
            )

    hh.cross_encoder_rerank = timed
    return orig


def _counting_leaves(seen: dict) -> dict:
    import nous.heart.search as search_mod

    originals = {name: getattr(search_mod, name) for name in _LEAF_RESOLVERS}

    def wrap(name: str, fn: Any) -> Any:
        def wrapper() -> Any:
            v = fn()
            seen[name[len("_resolve_") :]][str(v)] += 1
            return v

        return wrapper

    for name, fn in originals.items():
        setattr(search_mod, name, wrap(name, fn))
    return originals


@asynccontextmanager
async def open_stack(
    settings: Any,
    *,
    pinned: list[str],
    data_dir: Path,
    as_of: dict[str, datetime.date],
    snippet_chars: int,
    embeddings: Any = None,
    llm: Any = None,
    migrate: bool = True,
):
    """Yield a Stack bound to ``settings``; ``embeddings``/``llm`` are injectable for tests;
    ``migrate=False`` skips applying pending migrations (a test database migrated by hand)."""
    import nous.api.tools as tools_mod
    import nous.heart.heart as hh
    import nous.heart.search as search_mod
    from nous.api.tools import create_nous_tools
    from nous.brain.brain import Brain
    from nous.cognitive.context import ContextEngine
    from nous.cognitive.frames import FrameEngine
    from nous.cognitive.intent import IntentClassifier
    from nous.heart.heart import Heart
    from nous.observability.retrieval_logger import RetrievalLogger, set_active
    from nous.runtime_config import RuntimeConfig
    from nous.storage.database import Database
    from nous_eval.beam.harness import load_runtime_config, prepare_db, published_resolver_env

    if migrate:
        await prepare_db(settings)  # pending migrations + schema check, as every eval ingest does
    emb = embeddings if embeddings is not None else make_embeddings(settings)
    cache = EmbedDiskCache(data_dir / "embedding_cache.jsonl")
    cache.install(emb)
    owns_llm = llm is None
    if owns_llm:
        from nous_eval.served.judge import make_llm_client

        llm = await make_llm_client()
    db = Database(settings)
    await db.connect()
    ce_calls: list = []
    render_orders: list = []
    orig_ce = hh.cross_encoder_rerank
    orig_format = tools_mod._format_pipeline_text
    leaf_seen: dict = {n[len("_resolve_") :]: defaultdict(int) for n in _LEAF_RESOLVERS}
    originals: dict = {}
    recorder = RetrievalLogger(candidate_sample_rate=1.0, snippet_chars=snippet_chars, max_candidates=2000, ring_size=4)
    env = published_resolver_env(settings)
    env.__enter__()
    try:
        _install_ce_timer(ce_calls)
        _capture_render_order(render_orders)
        runtime = await load_runtime_config(db, settings, pinned)
        leaf_start = {n[len("_resolve_") :]: getattr(search_mod, n)() for n in _LEAF_RESOLVERS}
        eff = runtime["effective"]
        if leaf_start["vector_weight"] != float(eff["vector_weight"]) or leaf_start["rrf_k"] != int(eff["rrf_k"]):
            raise SystemExit(f"search leaves resolve {leaf_start}, runtime config says {eff}")
        originals = _counting_leaves(leaf_seen)
        heart = Heart(db, settings, emb, owns_embeddings=False)
        if settings.query_expansion_enabled:
            from nous.heart.query_expansion import QueryExpander

            expander = QueryExpander(llm=llm, settings=settings, db=db, model=settings.query_expansion_model)
            share_expansions(expander, ExpansionStore(data_dir / "query_expansions.jsonl", settings))
            heart.set_query_expander(expander)
        if settings.residual_activation_enabled:
            from nous.heart.residual_activation import ResidualActivator

            heart.set_residual_activator(ResidualActivator(settings=settings, wm=heart.working_memory, db=db))
        date_parser = None
        if settings.date_leg_enabled:
            from nous.heart.date_window import DateWindowParser

            date_parser = CachedDateParser(
                DateWindowParser(llm, settings.model_copy(update={"date_leg_pad_days": 0})),
                data_dir / "date_windows_unpadded.jsonl",
                as_of,
                settings.date_leg_model,
                settings.date_leg_pad_days,
            )
            heart.date_window_parser = date_parser
        _hold_read_writes(heart)
        async with heart:
            brain = Brain(db, settings, emb)
            set_active(recorder)
            stack = Stack(
                settings=settings,
                db=db,
                heart=heart,
                brain=brain,
                tools=create_nous_tools(brain, heart, settings=settings),
                ctx=ContextEngine(brain, heart, settings, identity_prompt=IDENTITY),
                classifier=IntentClassifier(settings),
                frames=FrameEngine(db, settings),
                recorder=recorder,
                runtime=runtime,
                leaf_start=leaf_start,
                leaf_seen=leaf_seen,
                date_parser=date_parser,
                embeddings=emb,
                ce_calls=ce_calls,
                render_orders=render_orders,
            )
            _hook_list_episodes(stack)
            _capture_formatters(stack)
            yield stack
    finally:
        set_active(None)
        for name, fn in originals.items():
            setattr(search_mod, name, fn)
        hh.cross_encoder_rerank = orig_ce
        tools_mod._format_pipeline_text = orig_format
        RuntimeConfig.reset()
        env.__exit__(None, None, None)
        cache.save(emb)
        if owns_llm:
            await llm.close()
        await db.disconnect()


def _hook_list_episodes(stack: Stack) -> None:
    """Keep the episodes Recent Conversations was built from (the build calls list_episodes once)."""
    orig = stack.heart.list_episodes

    async def list_episodes(*a: Any, **kw: Any) -> Any:
        res = await orig(*a, **kw)
        stack.listed_episodes = list(res or [])
        return res

    stack.heart.list_episodes = list_episodes


def ce_environment(settings: Any) -> dict[str, Any]:
    """Where the cross-encoder ran (call after its first use, which loads it)."""
    if not settings.cross_encoder_enabled:
        return {}
    import os

    try:
        import torch

        from nous.heart.reranker import _load_cross_encoder

        m = _load_cross_encoder(settings.cross_encoder_model)
        dev = getattr(m, "device", None) or getattr(getattr(m, "model", None), "device", None)
        return {
            "model": settings.cross_encoder_model,
            "device": str(dev),
            "torch": torch.__version__,
            "cuda_available": torch.cuda.is_available(),
            "torch_threads": torch.get_num_threads(),
            "os_cpu_count": os.cpu_count(),
        }
    except Exception as exc:  # noqa: BLE001 - recorded, not fatal
        return {"model": settings.cross_encoder_model, "error": type(exc).__name__}
