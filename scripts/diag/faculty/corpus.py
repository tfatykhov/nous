"""Phase 0 — discriminating associative-FACULTY eval corpus (docs/research/017).

Measures the FACULTY, not retrieval@k. Private INVENTED entities throughout (kills
parametric leak). CONTROLS exist so a 0/N on faculty cases is interpretable:
  - positive control: a directly-named fact the current similarity pipe MUST retrieve.
  - negative control: a query with NO answer in the corpus -> must ABSTAIN.

Four faculty classes (the cases the current similarity-pipe should NOT solve):
  - concept_bridge: answer reachable only via a shared single-token CONCEPT + a role
    chain (the "who can fix program ABC? -> T8me does Java" archetype). Bridge concept
    is single-token (co-mention's >=2-token rule misses it) and absent from the query.
  - goal_gated: same entity, TWO associated facts; the GOAL decides which is relevant.
  - experiential: two DISSIMILAR facts that co-occurred in ONE real episode (must be
    born full-cycle; reachable only by shared experience, not similarity).
  - used_before: an association exercised earlier should come back stronger than an
    un-exercised twin (longitudinal; NULL by construction on today's frozen weights).

PRE-REGISTERED baseline predictions (write before running, advisor rule):
  positive control -> PASS | negative control -> PASS(abstain)
  concept_bridge -> FAIL(bare) | experiential -> FAIL | goal_gated -> FAIL/PARTIAL
  used_before -> NULL by construction (no plasticity yet)
"""
from __future__ import annotations

PREDICTIONS = {
    "control_positive": "PASS",
    "control_negative": "PASS (abstain)",
    "concept_bridge": "FAIL (bare) / OPEN (agentic — the gbrain question)",
    "goal_gated": "FAIL or PARTIAL",
    "experiential": "FAIL",
    "used_before": "NULL by construction (frozen edge weights)",
}

# --- CONTROLS (built first; make the instrument interpretable) ---
CONTROL_POSITIVE = {
    "id": "ctrl_pos_dentist",
    "facts": ["My dentist is Dr. Yarvik at the Pellmoor Lane clinic."],
    "query": "Who is my dentist?",
    "answer_token": "Yarvik",
    "predict": "PASS",  # directly named -> similarity retrieval must surface it
}
CONTROL_NEGATIVE = {
    "id": "ctrl_neg_accountant",
    "facts": [],  # no accountant fact anywhere in the corpus
    "query": "Who is my accountant?",
    "answer_token": None,
    "predict": "ABSTAIN",  # primarily agentic-graded: must not fabricate a name
}

# --- CONCEPT-BRIDGE class (single-token invented concept + role chain) ---
# Each item: hop1 fact names the query handle; bridge fact holds the answer + the
# single-token concept. Query names the handle + asks the role; the bridge concept is
# NOT in the query, so the bridge fact is query-disjoint (validity gate checks it sits
# OUTSIDE baseline top-k). co-mention can't link them (concept is single-token).
CONCEPT_BRIDGE = [
    {
        "id": "cb_zterra",
        "hop1": "Project Zterra is written in Korlang and needs urgent fixes.",
        "bridge": "Wrenn Hald is one of the strongest Korlang developers around.",
        "query": "Who could fix Project Zterra?",
        "answer_token": "Wrenn Hald",
        "concept": "Korlang",
        "hop1_token": "Zterra",
        "predict_bare": "FAIL",
    },
    {
        "id": "cb_marlott",
        "hop1": "The Marlott report is blocked because the Quillon service is down.",
        "bridge": "Sasha Demir is the engineer who maintains the Quillon service.",
        "query": "Who can unblock the Marlott report?",
        "answer_token": "Sasha Demir",
        "concept": "Quillon",
        "hop1_token": "Marlott",
        "predict_bare": "FAIL",
    },
    {
        "id": "cb_brae",
        "hop1": "The Brae outage traces back to the Dossin cache layer.",
        "bridge": "Tomas Pell is our in-house Dossin specialist.",
        "query": "Who should handle the Brae outage?",
        "answer_token": "Tomas Pell",
        "concept": "Dossin",
        "hop1_token": "Brae",
        "predict_bare": "FAIL",
    },
]

# --- GOAL-GATED class (same entity, two facts; goal decides relevance) ---
# Primarily agentic-graded: the bare pipeline has no goal input beyond the query.
GOAL_GATED = [
    {
        "id": "gg_marlvenn",
        "facts": [
            "Marl Venn is my climbing partner at the Granite Wall gym.",
            "Marl Venn is also my tax advisor at the Pellmoor & Co firm.",
        ],
        "goal_a": "I'm planning a climbing trip this weekend.",
        "query_a": "Should I bring Marl Venn?",
        "relevant_a_token": "climbing",
        "goal_b": "I need to get my taxes filed before the deadline.",
        "query_b": "Should I bring Marl Venn?",
        "relevant_b_token": "tax",
        "predict": "FAIL/PARTIAL",
    },
]

# --- EXPERIENTIAL class (born full-cycle; reachable only by co-occurrence) ---
# Two dissimilar facts stated in ONE real session; query asks one via the other's
# context. Handled by the full-cycle ingest path, not direct-load.
EXPERIENTIAL = [
    {
        "id": "ex_halberd",
        # Two UNRELATED notes in ONE session -> they share only source_episode_id
        # (no shared entity, no shared vocab). The query reaches the Halberd note and
        # asks for the OTHER content of that same conversation; the Gus/ramen note
        # shares NO surface vocabulary with the query, so it is reachable ONLY via the
        # shared episode (the genuine experiential bridge, not similarity).
        "session_notes": [
            "I've decided we're adopting the Halberd framework for the big rewrite.",
            "Unrelated, but Gus Trelawny tipped me off about a great ramen spot called Tonkotsu Bay.",
        ],
        "query": "What else came up in the same conversation where I settled on the Halberd framework?",
        "answer_token": "Gus Trelawny",
        "predict": "FAIL (bare: needs fact->episode->fact 2-hop; agentic: maybe via episode summary)",
    },
]

# --- USED-BEFORE class (longitudinal; null by construction today) ---
USED_BEFORE = [
    {
        "id": "ub_orsa",
        "facts": [
            "Orsa Kine leads the Tellwright migration.",
            "The Tellwright migration is the reason the Bowmar deploys are frozen.",
        ],
        "query": "Who do I talk to about the Bowmar deploy freeze?",
        "answer_token": "Orsa Kine",
        "note": "Exercise the Orsa<->Tellwright<->Bowmar path, then re-measure vs an "
                "un-exercised twin; today edge weights are frozen so this is NULL.",
        "predict": "NULL (no plasticity)",
    },
]

# --- FILLER: query-competing facts so top-k is selective and the disjoint bridge
# facts sit OUTSIDE baseline top-k (the privfact validity-gate lesson). Shares
# fix/project/developer/report/outage vocabulary with the queries but NONE of the
# bridge concepts (Korlang/Quillon/Dossin) or answer tokens. ---
FILLER = [
    "The Pendar project needs a few fixes before Friday's release.",
    "Greta Lim is a talented frontend developer on the design team.",
    "The Almsworth report is overdue and the client keeps asking about it.",
    "The Corvale outage last week was resolved within an hour.",
    "Niko Brandt handles most of our database migrations these days.",
    "The Hessel dashboard project is finally out of beta.",
    "I owe Priya a coffee for covering my on-call shift.",
    "The Larkhill service had a brief outage on Tuesday morning.",
    "Devon Marsh is the person who usually fixes our build pipeline.",
    "The Quentle report needs sign-off from two reviewers before it ships.",
    "Our Wexford project slipped its deadline by two weeks.",
    "Mara Voss is a backend developer who joined the platform team in spring.",
    "The Tindale outage was caused by a bad config push.",
    "The Ravelin report summarizes last quarter's incidents.",
    "Felix Orr keeps the staging environment running smoothly.",
    "The Brackwell project is blocked on a vendor contract.",
    "Someone needs to fix the flaky test in the Holloway suite.",
    "Imani Cole is the developer who owns the notifications service.",
    "The Sandoval report was praised by the leadership team.",
    "The Yarrow deploy was rolled back after a regression.",
    "Petra Klee usually triages the urgent production issues.",
    "The Merrick project kicked off with a design review on Monday.",
    "The Ostry outage page was finally retired last month.",
    "Dane Holloway is a reliable developer for backend fixes.",
    "The Calder report is due at the end of the sprint.",
]


# --- ABSTRACT class (the frontier): bridge is a STRUCTURAL pattern, not a shared
# surface token. Query describes a problem by its abstract structure (domain A); the
# answer fact describes an INVENTED person who solved a surface-DIFFERENT instance of
# the SAME structure (domain B), with ~zero shared content words. co-mention is 0 by
# construction; the load-bearing question is whether the EMBEDDING spans the structure
# (does the answer fact surface for the query at all?). Reachable only by recognizing
# the abstract pattern. ---
ABSTRACT = [
    {
        "id": "ab_deadlock",
        "pattern": "circular dependency / deadlock",
        "query": "Our services are stuck in a circular wait — each one is blocked on the next and nothing moves. Who could help untangle that?",
        "fact": "Ravi Lund is the consultant who broke the airline crew-scheduling gridlock where every reassignment depended on another.",
        "answer_token": "Ravi Lund",
    },
    {
        "id": "ab_bottleneck",
        "pattern": "single bottleneck / SPOF",
        "query": "Every release has to funnel through one gatekeeper and it's choking the whole pipeline. Who has dealt with that kind of chokepoint?",
        "fact": "Priya Sundar redesigned the harbor so cargo no longer piled up behind a single overworked crane.",
        "answer_token": "Priya Sundar",
    },
    {
        "id": "ab_cascade",
        "pattern": "cascade / domino failure",
        "query": "When one region goes down it knocks over the next and the failure keeps spreading. Who knows how to stop that kind of chain reaction?",
        "fact": "Tomas Eberg is the engineer who kept a single substation fault from rolling across the entire power grid.",
        "answer_token": "Tomas Eberg",
    },
    {
        "id": "ab_thrashing",
        "pattern": "thrashing / self-inflicted contention",
        "query": "Under load our system retries so aggressively that the retries themselves drag everything down. Who has tackled that kind of self-inflicted overload?",
        "fact": "Nadia Frost calmed the concert gate where everyone shoving at once jammed the crowd to a standstill.",
        "answer_token": "Nadia Frost",
    },
    {
        "id": "ab_starvation",
        "pattern": "starvation / priority inversion",
        "query": "Our low-priority jobs never get to run because urgent work always jumps ahead of them. Who has solved that kind of fairness problem?",
        "fact": "Dr. Imani Cole reorganized the clinic so minor cases stopped waiting forever behind a constant stream of emergencies.",
        "answer_token": "Imani Cole",
    },
    {
        "id": "ab_runaway",
        "pattern": "positive-feedback runaway",
        "query": "We've got a runaway loop — a small spike causes scaling, which causes more load, which causes still more scaling. Who can break a vicious cycle like that?",
        "fact": "Felix Ohm stabilized the greenhouse where warmth drove growth which drove yet more warmth.",
        "answer_token": "Felix Ohm",
    },
]

# Concrete TWIN control: same deadlock structure but the answer fact SHARES surface
# words with the query -> must be cosine-reachable -> bare PASS. Confirms the instrument
# registers the easy version (so a 0 on ABSTRACT means "abstraction is hard", not
# "harness broken").
ABSTRACT_CONTROL = {
    "id": "ab_ctrl_twin",
    "query": "Our services are stuck in a circular wait, each one blocked on the next. Who could help?",
    "fact": "Quill Marsh specializes in untangling circular-wait deadlocks between services that block on each other.",
    "answer_token": "Quill Marsh",
    "predict": "PASS (shares surface terms -> cosine-reachable)",
}


def abstract_facts() -> list[tuple[str, str]]:
    """(content, source_tag) answer facts for the ABSTRACT class + the concrete twin."""
    out = [(c["fact"], "faculty-abstract") for c in ABSTRACT]
    out.append((ABSTRACT_CONTROL["fact"], "faculty-abstract-ctrl"))
    return out


def direct_load_facts() -> list[tuple[str, str]]:
    """(content, source_tag) for everything safe to direct-load for the BARE lens:
    positive control + concept-bridge facts + filler. Experiential/used-before/goal-
    gated are handled by their own rigs (full-cycle / longitudinal / agentic)."""
    out: list[tuple[str, str]] = []
    for f in CONTROL_POSITIVE["facts"]:
        out.append((f, "faculty-control"))
    for c in CONCEPT_BRIDGE:
        out.append((c["hop1"], "faculty-cb"))
        out.append((c["bridge"], "faculty-cb"))
    for f in FILLER:
        out.append((f, "faculty-filler"))
    return out


if __name__ == "__main__":
    facts = direct_load_facts()
    contents = [c for c, _ in facts]
    assert len(contents) == len(set(contents)), "duplicate fact content"
    print(f"concept_bridge={len(CONCEPT_BRIDGE)} goal_gated={len(GOAL_GATED)} "
          f"experiential={len(EXPERIENTIAL)} used_before={len(USED_BEFORE)} "
          f"filler={len(FILLER)} direct_load={len(facts)}")
    print("PRE-REGISTERED PREDICTIONS:")
    for k, v in PREDICTIONS.items():
        print(f"  {k:18} -> {v}")
    print("corpus OK")
