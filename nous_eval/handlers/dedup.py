"""F056 PR #2: dedup eval CLI — measures both legs of fact dedup.

Two distinct dedup legs that both ship in production and both have
shipped bugs in the last quarter (F051.5 PR #364 over-dedup; #354 sibling
class):

- **Leg 1 (hybrid-search pre-check):** `nous/handlers/fact_extractor.py:243-248`
  — `_dedup_via_search` flag pre-checks `Heart.search_facts(content)` against
  `fact_dedup_threshold` before calling `Heart.learn`.
- **Leg 2 (native cosine):** `nous/heart/facts.py::FactManager._learn` lines
  329-334 — cosine `>0.95` near-duplicate detection inside `Heart.learn`.

The eval measures BOTH separately so a regression in either is attributable.
Both legs route through `FactExtractor.extract_and_store(candidate_facts=[...])`
which short-circuits LLM extraction when `candidate_facts` is non-empty
(`fact_extractor.py:127-130`); legs differ only in the `dedup_via_search`
constructor flag.

Per F056 spec §B:
- Per-handler agent_id `nous-eval-handler-dedup`.
- Both content fields must be >= 30 chars (Heart.learn `facts.py:312`
  rejects shorter — F038-1.2). Schema enforces this at fixture-load.
- Admission control DISABLED for the eval — otherwise a paraphrase that
  would have dedup'd might get admission-rejected first, masking the
  dedup signal. Spec §B "isolation" rationale.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from nous.config import Settings
from nous.handlers.fact_extractor import FactExtractor
from nous.heart.schemas import FactInput, FactRejected
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.handlers._cli_base import (
    HandlerResult,
    _DeleteSpec,
    clear_handler_state,
    run_handler_eval,
)
from nous_eval.handlers._jsonl import load_jsonl
from nous_eval.handlers._models import DedupPair
from nous_eval.retrieval_runner import _build_heart_for_eval, _settings_for_eval_db

if TYPE_CHECKING:
    from nous.heart.heart import Heart

logger = logging.getLogger(__name__)


_AGENT_ID = "nous-eval-handler-dedup"
_HANDLER_NAME = "dedup"
_DEFAULT_FIXTURE = Path("tests/fixtures/handlers/dedup_paraphrases.jsonl")


def _settings_with_dedup_overrides(base: Settings) -> Settings:
    """Apply F056 §B required overrides.

    `admission_control_enabled=False` is critical: with admission on, a
    paraphrase that should dedup might get admission-rejected first
    (F023 admission gate at `facts.py:336-362` runs AFTER cosine dedup,
    so admission can't mask Leg 2 — but Leg 1's `search_facts` pre-check
    runs INSIDE FactExtractor BEFORE Heart.learn, so admission rejection
    would corrupt the per-leg attribution if a paraphrase reaches
    Heart.learn at all). Easiest correct stance: admission off for dedup
    eval, since we're measuring dedup in isolation.
    """
    update: dict[str, Any] = {
        "admission_control_enabled": False,
        # F056 PR #2 v2: also disable cross-type linking + graph backfill
        # so Heart.learn doesn't trigger GraphLinker work that's irrelevant
        # to dedup measurement and adds non-determinism.
        "cross_type_linking_enabled": False,
        "graph_backfill_enabled": False,
        "agent_id": _AGENT_ID,
    }
    return base.model_copy(update=update)


# F056 PR #2 v3: 50 LEXICALLY DIVERSE background facts per spec §B step 2.
#
# v2 used a templated `f"Generic background fact #{i:02d}..."` pattern that
# produced near-identical embeddings — Heart.learn's > 0.95 cosine dedup at
# facts.py:329-334 collapsed all 50 into 1 stored row, defeating the seed.
#
# v3 uses 50 hand-crafted sentences across 10 unrelated domains (5 each):
# astronomy, cooking, geography, music, animals, history, sports, plants,
# weather, transport. Domain rotation guarantees inter-fact cosine stays
# below 0.95. Each sentence >= 30 chars per F038-1.2.
_BACKGROUND_FACTS: tuple[str, ...] = (
    # astronomy
    "Jupiter has four large Galilean moons named Io, Europa, Ganymede, and Callisto.",
    "The Andromeda galaxy is approximately 2.5 million light-years from Earth.",
    "A solar eclipse occurs when the moon passes between Earth and the sun.",
    "Saturn's rings are composed mostly of water ice with traces of rocky material.",
    "The Hubble Space Telescope was launched into low Earth orbit in 1990.",
    # cooking
    "Sourdough bread requires a starter culture of wild yeast and lactobacilli.",
    "Caramelization of onions takes about 30 to 45 minutes over low heat.",
    "Fish sauce is a fermented condiment central to many Southeast Asian cuisines.",
    "Tempering chocolate involves heating and cooling to specific temperature ranges.",
    "Risotto is made by gradually adding warm broth while stirring arborio rice.",
    # geography
    "The Mariana Trench is the deepest known oceanic trench in the Pacific Ocean.",
    "Mount Kilimanjaro is the highest mountain in Africa, located in Tanzania.",
    "The Amazon River discharges more water than any other river in the world.",
    "Iceland sits on the Mid-Atlantic Ridge between two tectonic plate boundaries.",
    "Lake Baikal in Siberia holds about twenty percent of the world's fresh water.",
    # music
    "A grand piano typically has eighty-eight keys spanning seven full octaves.",
    "Bach composed the Brandenburg Concertos as a gift for the Margrave of Brandenburg.",
    "The Stradivarius violins were crafted in Cremona, Italy during the 1700s.",
    "Reggae music originated in Jamaica during the late nineteen sixties.",
    "Miles Davis pioneered cool jazz with his nineteen fifty-nine album Kind of Blue.",
    # animals
    "Octopuses have three hearts and blue blood due to copper-based hemocyanin.",
    "The Arctic tern migrates from polar region to polar region annually.",
    "Honey bees communicate the location of nectar sources via the waggle dance.",
    "Cheetahs are the fastest land animals, capable of brief bursts above seventy mph.",
    "Komodo dragons can grow over three meters long on the islands of Indonesia.",
    # history
    "The Rosetta Stone enabled scholars to decipher Egyptian hieroglyphic writing.",
    "Marco Polo traveled along the Silk Road to the court of Kublai Khan.",
    "The Treaty of Westphalia in sixteen forty-eight ended the Thirty Years' War.",
    "Cleopatra was the last active ruler of the Ptolemaic Kingdom of ancient Egypt.",
    "The printing press was invented by Johannes Gutenberg around fourteen forty.",
    # sports
    "A regulation soccer match consists of two halves of forty-five minutes each.",
    "The Tour de France bicycle race spans approximately three thousand five hundred kilometers.",
    "Wimbledon is the oldest tennis tournament in the world, founded in eighteen seventy-seven.",
    "Sumo wrestling matches are held in a circular ring called a dohyo.",
    "The Stanley Cup is awarded annually to the National Hockey League playoff champion.",
    # plants
    "Photosynthesis converts carbon dioxide and water into glucose using sunlight.",
    "Bamboo is one of the fastest-growing plants in the world, some species growing meters per day.",
    "The giant sequoia is the largest tree by volume on Earth, native to California.",
    "Carnivorous plants like the Venus flytrap evolved in nutrient-poor soil environments.",
    "Tulip mania in seventeenth-century Netherlands was one of history's first speculative bubbles.",
    # weather
    "A rainbow forms when sunlight is refracted through suspended water droplets in the air.",
    "Hurricanes are classified on the Saffir-Simpson scale based on sustained wind speed.",
    "Lightning strikes the Earth roughly one hundred times every second on average.",
    "The polar vortex is a persistent low-pressure system over the polar regions.",
    "Snowflakes are six-sided crystals formed by water vapor freezing in the atmosphere.",
    # transport
    "The Trans-Siberian Railway connects Moscow to Vladivostok over nine thousand kilometers.",
    "Diesel engines were patented by Rudolf Diesel in eighteen ninety-two.",
    "The Suez Canal connects the Mediterranean Sea with the Red Sea via Egypt.",
    "Concorde was a supersonic passenger jet that operated commercially until two thousand three.",
    "Container shipping standardized cargo through twenty and forty foot ISO containers.",
)
assert len(_BACKGROUND_FACTS) == 50, f"expected 50 background facts, got {len(_BACKGROUND_FACTS)}"


def filter_pairs(pairs: list[DedupPair], *, include_unreviewed: bool) -> list[DedupPair]:
    """Apply the reviewed_by gate. Mirrors qrels_loader.py:80-85 pattern."""
    if include_unreviewed:
        return pairs
    return [p for p in pairs if p.reviewed_by]


def compute_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """Return (precision, recall, F1). Mirrors handlers.admission.compute_f1.

    Duplicated rather than imported to keep handler modules independent
    and individually-testable. If a third copy lands in a future handler,
    extract to `_metrics.py`.
    """
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    if precision + recall == 0:
        return 0.0, 0.0, 0.0
    f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def _classify_dedup_outcome(
    returned_uuids: list[UUID], anchor_uuid: UUID,
) -> str:
    """Pure helper: did the FactExtractor return signal dedup against `anchor_uuid`?

    Returns "dedup" iff `anchor_uuid` is present in the returned UUID list.
    Returns "distinct" otherwise (which covers: no dedup at all, OR dedup
    against a non-anchor fact like a background seed — the eval treats both
    cases as "did NOT dedup against this specific anchor", which matches the
    test's intent).

    Production behavior: `_store_candidate_facts` (`fact_extractor.py:243-248`
    Leg 1; `:259` Leg 2 via Heart.learn → `_confirm`) appends the EXISTING
    fact's UUID when dedup fires. So `anchor_uuid in returned_uuids` is the
    dedup-against-anchor signal regardless of which leg fired.

    Eval correctness depends on background facts being dissimilar from
    anchors/paraphrases (else the paraphrase might dedup against background
    instead of anchor — which classifies as "distinct" but is actually
    misattribution). `_BACKGROUND_FACTS` are designed for this property.
    """
    if anchor_uuid in returned_uuids:
        return "dedup"
    return "distinct"


def _confusion_increment(cm: dict[str, int], expected: str, outcome: str) -> None:
    """Increment confusion counter. tp = correct dedup, tn = correct distinct."""
    if expected == "dedup" and outcome == "dedup":
        cm["tp"] += 1
    elif expected == "distinct" and outcome == "distinct":
        cm["tn"] += 1
    elif expected == "distinct" and outcome == "dedup":
        cm["fp"] += 1
    else:  # expected == "dedup" and outcome == "distinct"
        cm["fn"] += 1


async def _seed_background_facts(heart: "Heart") -> list[UUID]:
    """Seed 50 unrelated background facts per spec §B procedure step 2.

    Returns the list of seeded UUIDs so the caller can preserve them
    during per-pair cleanup (per-pair DELETE wipes anchor + new facts only,
    never background).
    """
    bg_uuids: list[UUID] = []
    for content in _BACKGROUND_FACTS:
        result = await heart.learn(
            FactInput(content=content, source="dedup_eval_background"),
            check_contradictions=False,
        )
        if isinstance(result, FactRejected):
            logger.warning(
                "dedup eval: background fact rejected: %s", result.explanation,
            )
            continue
        bg_uuids.append(result.id)
    logger.info("dedup eval: seeded %d background facts", len(bg_uuids))
    return bg_uuids


async def _delete_facts_by_ids(heart: "Heart", fact_ids: list[UUID]) -> None:
    """Targeted per-pair cleanup: DELETE only the listed UUIDs.

    Replaces v1's blanket `DELETE WHERE agent_id` which would have wiped
    the background seed too. Targeted deletion preserves the 50-row
    background corpus across all pair iterations.

    Uses the `text(...), {"ids": list_value}` execute pattern (matches
    production at `nous/api/dashboard_queries.py:206-209`). The
    `text(...).bindparams(ids=[...])` form does NOT work for `ANY(:ids)`
    array bindings — it only handles scalar parameters.
    """
    if not fact_ids:
        return
    async with heart.db.session() as session:
        await session.execute(
            text("DELETE FROM heart.facts WHERE id = ANY(:ids)"),
            {"ids": [str(fid) for fid in fact_ids]},
        )
        await session.commit()


async def _run_one_leg(
    pairs: list[DedupPair],
    heart: "Heart",
    settings: Settings,
    background_uuids: set[UUID],
    *,
    dedup_via_search: bool,
) -> dict[str, int]:
    """Run one leg (Leg 1 if dedup_via_search=True, else Leg 2).

    For each pair: insert anchor → call FactExtractor.extract_and_store
    with the paraphrase as a single candidate → check whether the returned
    UUID list contains the anchor's UUID (dedup signal). After scoring,
    DELETE the anchor + any newly-stored UUIDs by ID — preserves the
    background seed across iterations.

    `background_uuids` is used to identify which UUIDs in `returned_uuids`
    are NEW (not background, not anchor) and so need cleanup.
    """
    extractor = FactExtractor(
        heart=heart,
        settings=settings,
        bus=None,
        llm_client=None,
        dedup_via_search=dedup_via_search,
    )
    cm = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}

    for pair in pairs:
        # Insert anchor (check_contradictions=False — costs Haiku $ + adds
        # non-determinism; not measured here)
        anchor_result = await heart.learn(
            FactInput(content=pair.anchor, source="dedup_eval"),
            check_contradictions=False,
        )
        if isinstance(anchor_result, FactRejected):
            logger.warning(
                "dedup eval: anchor rejected (skipping pair %s): %s",
                pair.row_id, anchor_result.explanation,
            )
            continue
        anchor_uuid = anchor_result.id

        # Defensive: if Heart.learn dedup'd the anchor against an existing
        # background fact (cosine > 0.95), `anchor_uuid` IS the background
        # UUID. Skipping the pair is safer than measuring against a UUID
        # we'll then refuse to delete (would leave stale state) or DO
        # delete (would wipe our background corpus mid-run).
        if anchor_uuid in background_uuids:
            logger.warning(
                "dedup eval: anchor %s collided with background fact (cosine "
                "> 0.95). Skipping pair to preserve background corpus integrity.",
                pair.row_id,
            )
            continue

        # Submit paraphrase via FactExtractor (short-circuits LLM extraction
        # when candidate_facts is non-empty — see fact_extractor.py:127-130).
        returned_uuids = await extractor.extract_and_store(
            summary={},
            episode_id=f"dedup-eval-{pair.row_id}",
            candidate_facts=[{
                "content": pair.paraphrase,
                "subject": "dedup-eval",
                "category": "technical",
            }],
        )
        outcome = _classify_dedup_outcome(returned_uuids, anchor_uuid)
        _confusion_increment(cm, pair.expected, outcome)

        # Targeted cleanup: anchor + any new (non-background) UUID returned.
        # We already verified anchor_uuid not in background_uuids above.
        to_delete = [anchor_uuid]
        for ruid in returned_uuids:
            if ruid != anchor_uuid and ruid not in background_uuids:
                to_delete.append(ruid)
        try:
            await _delete_facts_by_ids(heart, to_delete)
        except Exception:
            logger.exception(
                "dedup eval: per-pair cleanup failed for %s — subsequent pairs "
                "may see stale state and produce incorrect metrics",
                pair.row_id,
            )

    return cm


async def _run_dedup_eval(
    args: argparse.Namespace,
    eval_settings: EvalSettings,
    main_settings: Settings,
) -> HandlerResult:
    fixture_path = args.fixture_path or _DEFAULT_FIXTURE
    pairs = load_jsonl(fixture_path, DedupPair)
    pairs = filter_pairs(pairs, include_unreviewed=args.include_unreviewed)
    pairs.sort(key=lambda p: p.row_id)

    if not pairs:
        logger.error("dedup eval: zero pairs after reviewed_by filter")
        return HandlerResult(
            metrics={"dedup_f1": 0.0, "dedup_f1_leg1": 0.0, "dedup_f1_leg2": 0.0},
            extras={"confusion_matrix_leg1": {"tp": 0, "fp": 0, "tn": 0, "fn": 0},
                    "confusion_matrix_leg2": {"tp": 0, "fp": 0, "tn": 0, "fn": 0}},
            report_lines=["No pairs passed the reviewed_by filter."],
            primary_metric="dedup_f1",
            fixture_size=0,
        )

    overridden = _settings_with_dedup_overrides(main_settings)
    # See admission.py for the agent_id clobber-restore rationale — same
    # pattern: _settings_for_eval_db picks up eval_settings.agent_id, which
    # would otherwise route writes to "nous-eval-corpus".
    eval_scoped = _settings_for_eval_db(eval_settings, overridden)
    eval_scoped = eval_scoped.model_copy(update={"agent_id": _AGENT_ID})

    eval_db = Database(eval_scoped)
    try:
        await eval_db.connect()

        # Lifecycle step 6: clean slate before seed under advisory lock.
        await clear_handler_state(
            eval_db, name=_HANDLER_NAME, agent_id=_AGENT_ID,
            deletes=[_DeleteSpec(schema_table="heart.facts", agent_id=_AGENT_ID)],
        )

        async with _build_heart_for_eval(eval_db, eval_scoped) as heart:
            # Seed 50 background facts ONCE. They survive per-pair cleanup
            # (which DELETEs by ID, not by agent_id). Both legs share the
            # same background corpus so per-leg numbers are comparable.
            bg_uuids = await _seed_background_facts(heart)
            bg_set: set[UUID] = set(bg_uuids)

            cm_leg1 = await _run_one_leg(
                pairs, heart, eval_scoped, bg_set, dedup_via_search=True,
            )
            cm_leg2 = await _run_one_leg(
                pairs, heart, eval_scoped, bg_set, dedup_via_search=False,
            )
    finally:
        await eval_db.disconnect()

    p1, r1, f1_1 = compute_f1(cm_leg1["tp"], cm_leg1["fp"], cm_leg1["fn"])
    p2, r2, f1_2 = compute_f1(cm_leg2["tp"], cm_leg2["fp"], cm_leg2["fn"])
    f1_mean = (f1_1 + f1_2) / 2.0

    n_unreviewed = sum(1 for p in pairs if not p.reviewed_by)
    report_lines = [
        f"- fixture pairs: {len(pairs)} ({n_unreviewed} unreviewed)",
        "",
        "### Leg 1 (hybrid-search pre-check)",
        f"- TP/FP/TN/FN: {cm_leg1['tp']}/{cm_leg1['fp']}/{cm_leg1['tn']}/{cm_leg1['fn']}",
        f"- precision: {p1:.3f}, recall: {r1:.3f}, F1: {f1_1:.3f}",
        "",
        "### Leg 2 (native cosine in Heart.learn)",
        f"- TP/FP/TN/FN: {cm_leg2['tp']}/{cm_leg2['fp']}/{cm_leg2['tn']}/{cm_leg2['fn']}",
        f"- precision: {p2:.3f}, recall: {r2:.3f}, F1: {f1_2:.3f}",
        "",
        f"### Combined dedup_f1 = mean(leg1, leg2) = {f1_mean:.3f}",
    ]

    return HandlerResult(
        metrics={
            "dedup_f1": f1_mean,
            "dedup_f1_leg1": f1_1,
            "dedup_f1_leg2": f1_2,
        },
        extras={
            "confusion_matrix_leg1": cm_leg1,
            "confusion_matrix_leg2": cm_leg2,
            "leg1_precision": p1,
            "leg1_recall": r1,
            "leg2_precision": p2,
            "leg2_recall": r2,
        },
        report_lines=report_lines,
        primary_metric="dedup_f1",
        fixture_size=len(pairs),
        handler_specific_notes=(
            f"admission_off, fact_dedup_threshold={eval_scoped.fact_dedup_threshold}, "
            f"include_unreviewed={args.include_unreviewed}"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    return run_handler_eval(
        _HANDLER_NAME,
        _run_dedup_eval,
        default_threshold=0.05,
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
