"""The read-path components of one run, wired as nous/main.py wires them.

Database, the runtime overrides in nous_system.config (main.py loads them at startup; an arm
that sets one of their keys wins over the stored value), the fusion parameters published to the
environment (``nous.heart.search`` re-reads them per query), the embedding provider behind a
disk cache, Heart with query expansion, residual activation and the date-window parser wired
when their flags are on, Brain, the agent tools and the ContextEngine.

Read-path writes that would make results depend on query order are held off identically for
every arm: fact access tracking (recall_count UPDATEs, which also add HNSW tuples and drift later
approximate searches) and the keyed leg's own tracking. The date-window parser is wrapped by a
store shared across runs, keyed (as-of date, query, model): the parse a query gets is fixed once
and every later run reuses it, so latency excludes the parse and arms see the same windows.
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
    three tries on a transient failure) against the query's as-of date, not today."""

    def __init__(self, inner: Any, path: Path, as_of: dict[str, datetime.date], model: str) -> None:
        self.inner, self.path, self.as_of, self.model = inner, path, as_of, model
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
            self.last["window"] = rec["window"]
            return DateWindow(
                start=datetime.date.fromisoformat(rec["window"][0]), end=datetime.date.fromisoformat(rec["window"][1])
            )
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
    ce_calls: list = field(default_factory=list)
    intent_leg_hits: list = field(default_factory=list)
    ce_env: dict = field(default_factory=dict)


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
    orig_ce = hh.cross_encoder_rerank
    leaf_seen: dict = {n[len("_resolve_") :]: defaultdict(int) for n in _LEAF_RESOLVERS}
    originals: dict = {}
    recorder = RetrievalLogger(candidate_sample_rate=1.0, snippet_chars=snippet_chars, max_candidates=2000, ring_size=4)
    env = published_resolver_env(settings)
    env.__enter__()
    try:
        _install_ce_timer(ce_calls)
        runtime = await load_runtime_config(db, settings, pinned)
        leaf_start = {n[len("_resolve_") :]: getattr(search_mod, n)() for n in _LEAF_RESOLVERS}
        eff = runtime["effective"]
        if leaf_start["vector_weight"] != float(eff["vector_weight"]) or leaf_start["rrf_k"] != int(eff["rrf_k"]):
            raise SystemExit(f"search leaves resolve {leaf_start}, runtime config says {eff}")
        originals = _counting_leaves(leaf_seen)
        heart = Heart(db, settings, emb, owns_embeddings=False)
        if settings.query_expansion_enabled:
            from nous.heart.query_expansion import QueryExpander

            heart.set_query_expander(
                QueryExpander(llm=llm, settings=settings, db=db, model=settings.query_expansion_model)
            )
        if settings.residual_activation_enabled:
            from nous.heart.residual_activation import ResidualActivator

            heart.set_residual_activator(ResidualActivator(settings=settings, wm=heart.working_memory, db=db))
        date_parser = None
        if settings.date_leg_enabled:
            from nous.heart.date_window import DateWindowParser

            date_parser = CachedDateParser(
                DateWindowParser(llm, settings), data_dir / "date_windows.jsonl", as_of, settings.date_leg_model
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
                ce_calls=ce_calls,
            )
            _hook_intent_leg(stack)
            yield stack
    finally:
        set_active(None)
        for name, fn in originals.items():
            setattr(search_mod, name, fn)
        hh.cross_encoder_rerank = orig_ce
        RuntimeConfig.reset()
        env.__exit__(None, None, None)
        cache.save(emb)
        if owns_llm:
            await llm.close()
        await db.disconnect()


def _hook_intent_leg(stack: Stack) -> None:
    """The Session Profile (intent) leg is not in the retrieval trace; record its hits so the
    ones that reach that section count as rendered items (query-mode A/B)."""
    orig = stack.heart.search_facts

    async def search_facts(*a: Any, **kw: Any) -> Any:
        res = await orig(*a, **kw)
        if kw.get("include_categories"):
            stack.intent_leg_hits.extend(res or [])
        return res

    stack.heart.search_facts = search_facts


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
