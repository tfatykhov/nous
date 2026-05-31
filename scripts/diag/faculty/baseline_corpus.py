"""Capability Baseline Instrument corpus (docs/research/018).

Persona = one invented person; ~50 private facts (kills parametric leak). This module
currently carries the no-handle / plasticity smoke material (cells 11 + 18) at full
corpus size, plus filler that doubles as the distractor field for cell 3. The remaining
cell items (6-10, 12-17) are added when the full agentic harness is built.

No-handle pairs use REGISTER CONTRAST: the target is a sharply different life-domain from
the query (web-admin vs medical, auto vs reading) so cosine sinks it below top-k — the
precondition for both the validity gate AND the weighted-neighbor path being the only route
in (the smoke showed a same-register target stays mid-pack and masks the weight effect).
"""
from __future__ import annotations

# Each pair: SEED hit by the query (lexical handle) + TARGET that co-occurred in the same
# session but is register-contrasted so it falls OUTSIDE bare top-k. answer_token must surface
# only via a co-activation edge (cell 11) and its rank must respond to edge weight (cell 18).
# Cell 11 — "no-handle" associative surfacing. Each pair is two DISTINCT activities that
# share a single real-world OCCASION (same day / trip / life-period) but share no words and
# are not semantically near. The occasion is NOT written into either fact — it exists only
# because the two were recorded together. The query ASSERTS the occasion and asks for the
# co-occurring activity; the answer is reachable only via a link formed from the co-occurrence.
# answer_token is unique to the target (no collision with filler) so the grader can't match
# the wrong fact.
# Cell 11 — "no-handle" associative surfacing. The query is a CUE that names only the SEED;
# it does NOT ask for the target. The test is whether cueing the seed makes the co-experienced
# TARGET surface — which it should in an associative memory, but cannot via similarity because
# the target is from an UNRELATED life-domain (pets→real-estate, physio→web-admin, …) so it
# shares no words and is not cosine-near. The two genuinely happened on one occasion (same day /
# move / event); that occasion is recorded only by their co-occurrence, never written into either
# fact. NOTE (design tension, empirically confirmed): you cannot reword the cue to *ask* for the
# target without giving the embedding a handle and destroying disjointness — so the cue stays bare.
# answer_token is unique to the target (no filler collision) so the grader can't match a wrong fact.
NO_HANDLE_PAIRS = [
    {
        "id": "nh_pim",
        "seed": "I adopted a retired greyhound named Pim from the rescue shelter.",
        "target": "I signed a two-year lease on the Galt Street office unit.",
        "query": "Tell me about the day I adopted my greyhound Pim.",
        "answer_token": "Galt",
    },
    {
        "id": "nh_osei",
        "seed": "I started physical therapy for my left knee with a physio named Dr. Osei.",
        "target": "I renewed the domain name quillford.net for another three years.",
        "query": "How is my knee physiotherapy with Dr. Osei going?",
        "answer_token": "quillford",
    },
    {
        "id": "nh_sable",
        "seed": "I take weekly throwing-wheel pottery classes from an instructor named Sable.",
        "target": "I moved my emergency savings into the Drennby credit union.",
        "query": "Tell me about my pottery classes with Sable.",
        "answer_token": "Drennby",
    },
    {
        "id": "nh_move",
        "seed": "I moved into the flat on Harwick Court at the end of the month.",
        "target": "I switched my electricity supply over to Vantle Energy.",
        "query": "Tell me about moving into the Harwick Court flat.",
        "answer_token": "Vantle",
    },
    {
        "id": "nh_recital",
        "seed": "I performed in an amateur piano recital of a piece called Vellmont.",
        "target": "I had the leaking garage roof patched up.",
        "query": "How did my Vellmont piano recital go?",
        "answer_token": "garage",
    },
    {
        "id": "nh_birthday",
        "seed": "I threw a party for my daughter's first birthday.",
        "target": "I opened a long-term savings account at the Kessler building society.",
        "query": "Tell me about my daughter's first birthday party.",
        "answer_token": "Kessler",
    },
]

# Invented filler — varied domains; the distractor field. ~40 to make K=10 selective.
FILLER = [
    "My dentist is Dr. Yarvik on Halden Avenue; checkups every March.",
    "I drive a slate-grey Tolmark Estate wagon, plate KQ-7720.",
    "My espresso machine is a Brevill Duo; I pull 18-gram shots.",
    "I am allergic to the antibiotic claramycin.",
    "My building's super is named Oletti; intercom code 4471.",
    "I keep my passport in the blue Hessian folder in the hall closet.",
    "My favourite hiking trail is the Korrel Ridge loop, about 9 km.",
    "I subscribe to the Fennimore Quarterly journal on urban design.",
    "My bank card for groceries ends in 0312.",
    "I take vitamin D every morning with breakfast.",
    "My sister Wenna lives in the Pellan district near the old tram depot.",
    "My laptop is a Korven X13 running the Aurelis Linux distro.",
    "I prefer aisle seats and pack only a single carry-on.",
    "My gym is the Ostra Strength club; I train Tuesdays and Fridays.",
    "I am slowly learning the Tindric language using flashcards each evening.",
    "My houseplant collection includes a Verda fern and two Sollis cacti.",
    "My optometrist is Dr. Halben; my prescription is -2.25 in both eyes.",
    "I roast my own coffee using beans from the Marrow Hill co-op.",
    "My bicycle is a steel Renning tourer with bar-end shifters.",
    "I volunteer at the Caldon Street food bank on alternate Saturdays.",
    "My favourite restaurant is the Sorrel Spoon on the waterfront.",
    "I am restoring an old Hessler upright piano in the spare room.",
    "My car insurance renews each November through Brackwater Mutual.",
    "I collect vintage fountain pens, mostly Pellan-made nibs.",
    "My weekly grocery run is to the Ostry Market on Linden Road.",
    "I keep a saltwater aquarium with two Quill gobies and a Verda shrimp.",
    "My barber is Frell on Tanner Lane; I go every five weeks.",
    "I am training for the Korrel half-marathon in the autumn.",
    "My favourite tea is the smoky Drenn Lapsang from the Marrow Hill co-op.",
    "I keep my bicycle helmet on the hook by the Verro Street side door.",
    "My accountant is Sasha Demir; tax filings are due each April.",
    "I am reupholstering a mid-century Brackett armchair in green wool.",
    "My phone plan is the Tindle 20-gig prepaid, topped up monthly.",
    "I keep a sourdough starter named Bram that I feed every morning.",
    "My garden has three raised beds of Korren heirloom tomatoes.",
    "I swim laps at the Hallen public pool on Sunday mornings.",
    "My favourite board game night is Thursdays with the Ostra group.",
    "I store winter clothes in a cedar chest at the foot of the bed.",
    "My umbrella is a wooden-handled Renning that I leave by the door.",
    "I am slowly cataloguing my late grandfather's stamp collection.",
]


def smoke_facts() -> list[tuple[str, str]]:
    """(content, source) for every smoke fact: both pair facts + filler."""
    out: list[tuple[str, str]] = []
    for p in NO_HANDLE_PAIRS:
        out.append((p["seed"], f"baseline:{p['id']}:seed"))
        out.append((p["target"], f"baseline:{p['id']}:target"))
    for f in FILLER:
        out.append((f, "baseline:filler"))
    return out


# ---------------------------------------------------------------------------
# Full 18-cell spec (docs/research/018). Each cell is self-documenting: the facts
# it needs, the probe, the lens, the pre-registered prediction, and the controls.
#   lens:    "bare" | "agentic" | "both"
#   facts:   [(content, session_tag)] — session_tag groups facts into one ingest
#            session (for co-occurrence / multi-session cells). None = own session.
#   answer:  token that must appear (None => must ABSTAIN).
#   flag:    env var to also run flag-ON (cells whose capability is gated).
#   pos_ctrl:"edge" | "weight" | None  (limit cells).
#   bridge_seed: substring identifying the SEED fact, so the harness can log whether
#            the seed was retrieved (disambiguates a bridging FAIL — advisor req #2).
#   false:   wrong entities that count as a confabulated bridge (precision, cells 6-11).
# Some cells reuse FILLER facts (no new fact needed) — facts=[] then.
# ---------------------------------------------------------------------------
CELLS = [
    # A. retrieval mechanics
    {"id": "c1_surface", "family": "A", "lens": "bare", "facts": [],
     "query": "What is my dentist's name?", "answer": "Yarvik",
     "predict": "PASS rank1-3 (lexical)"},
    {"id": "c2_semantic", "family": "A", "lens": "bare", "facts": [],
     "query": "Which medication do I need to avoid?", "answer": "claramycin",
     "predict": "PASS top-k (embedding paraphrase)"},
    {"id": "c3_needle", "family": "A", "lens": "bare", "facts": [],
     "query": "What is the intercom code for my building?", "answer": "4471",
     "predict": "PASS but rank degrades under distractors"},
    {"id": "c4_abstain", "family": "A", "lens": "agentic", "facts": [],
     "query": "What is my lawyer's name?", "answer": None,
     "predict": "ABSTAIN (no such fact)"},
    {"id": "c5_crosstype", "family": "A", "lens": "bare",
     "facts": [("Project Halberd's launch is currently behind schedule.", "s_halberd")],
     "decision": "Decided to push the Halberd launch to Q3 to absorb the slippage.",
     "query": "What's going on with Project Halberd's launch?", "answer": "Q3",
     "predict": "PARTIAL — cross-type edge exists, rarely top-k", "pos_ctrl": None},

    # B. association / bridging
    {"id": "c6_comention", "family": "B", "lens": "both",
     "facts": [("Project Halberd's launch is behind schedule.", "s_hb1"),
               ("I put Tomas Pell in charge of Project Halberd.", "s_hb2")],
     "query": "Who is leading the project that is behind schedule?", "answer": "Tomas Pell",
     "bridge_seed": "behind schedule", "false": ["Gus Trelawny", "Marl Venn", "Wrenn Hald"],
     "predict": "PARTIAL (co-mention fires, ranking-capped)"},
    {"id": "c7_roleskill", "family": "B", "lens": "agentic",
     "facts": [("The Quill ledger system is written entirely in the Tindric language.", "s_q1"),
               ("Gus Trelawny is the most fluent Tindric programmer I know.", "s_q2")],
     "query": "Who could I ask to fix a bug in the Quill ledger system?", "answer": "Trelawny",
     "bridge_seed": "Quill ledger", "false": ["Tomas Pell", "Marl Venn", "Sable"],
     "predict": "PASS via agent re-query on the skill token"},
    {"id": "c8_multihop", "family": "B", "lens": "agentic",
     "facts": [("Marl Venn owes me a favour from last winter.", "s_m1"),
               ("Marl Venn is a licensed electrician.", "s_m2")],
     "query": "Who that owes me a favour could rewire my shed?", "answer": "Marl Venn",
     "bridge_seed": "owes me a favour", "false": ["Gus Trelawny", "Tomas Pell"],
     "predict": "PASS but unreliable"},
    {"id": "c9_experiential", "family": "B", "lens": "agentic",
     "facts": [("I adopted a retired greyhound named Pim the same afternoon.", "s_day"),
               ("That same afternoon I signed the Verro Street office lease.", "s_day")],
     "query": "What else did I do the day I adopted my greyhound Pim?", "answer": "Verro",
     "bridge_seed": "greyhound named Pim", "false": [],
     "predict": "PARTIAL — agent finds shared session, not graph"},
    {"id": "c10_abstract", "family": "B", "lens": "both",
     "facts": [("Wrenn Hald once untangled a circular-wait standoff where two services "
                "each blocked waiting on the other to release first.", "s_w1")],
     "query": "Who do I know who has resolved a situation where two parties were each "
              "stuck waiting on the other to move first?", "answer": "Wrenn Hald",
     "bridge_seed": None, "false": ["Marl Venn", "Gus Trelawny"],
     "predict": "PARTIAL (embedding spans ~structure)"},
    {"id": "c11_nohandle", "family": "B", "lens": "bare", "facts": [],  # uses NO_HANDLE_PAIRS
     "query": None, "answer": None, "pos_ctrl": "edge",
     "predict": "FAIL bare+agentic; edge rescues (pos-ctrl)"},

    # C. temporal / dynamic
    {"id": "c12_contradiction", "family": "C", "lens": "both",
     "facts": [("I bank primarily with Halloway Federal.", "s_b1"),
               ("Update: I moved everything to Pellan Mutual after Halloway shut my branch.", "s_b2")],
     "query": "What is my current primary bank?", "answer": "Pellan Mutual",
     "false": ["Halloway"], "flag": "NOUS_RECENCY_RESOLVER_ENABLED",
     "predict": "PARTIAL prod-default; better flag-on"},
    {"id": "c13_recency", "family": "C", "lens": "agentic",
     "facts": [("In September 2025 I chose the Korren framework for the dashboard.", "s_d1"),
               ("In February 2026 I dropped Korren and rebuilt the dashboard in Aurelis.", "s_d2")],
     "query": "What is my most recent decision about the dashboard framework?", "answer": "Aurelis",
     "false": ["Korren"], "flag": "NOUS_TEMPORAL_EXTRACTION_ENABLED",
     "predict": "PARTIAL/FAIL prod-default; needs temporal flag"},
    {"id": "c14_multisession", "family": "C", "lens": "agentic",
     "facts": [("My conference talk this year is in Pellan City.", "s_t1"),
               ("My conference talk is scheduled for the second week of March.", "s_t2")],
     "query": "When and where is my conference talk?", "answer": "March",
     "answer2": "Pellan", "predict": "PARTIAL (retrieval breadth)"},

    # D. learning / adaptation
    {"id": "c16_correction", "family": "D", "lens": "agentic",
     "facts": [("I like my status reports written in the past tense.", "s_c1"),
               ("Correction: always write my status reports in the present tense.", "s_c2")],
     "query": "What tense should my status reports use?", "answer": "present",
     "false": ["past"], "predict": "PARTIAL (correction path)"},
    {"id": "c17_goalgated_a", "family": "D", "lens": "agentic",
     "facts": [("Marl Venn is a licensed electrician.", "s_g1"),
               ("Bex Carrow leads hard routes at my climbing gym.", "s_g2")],
     "query": "I need to rewire my shed — who do I know who could help?", "answer": "Marl Venn",
     "false": ["Bex Carrow"], "predict": "PASS (goal filter)"},
    {"id": "c17_goalgated_b", "family": "D", "lens": "agentic", "facts": [],  # reuse c17 facts
     "query": "I want a partner for a hard climbing route — who do I know?", "answer": "Bex Carrow",
     "false": ["Marl Venn"], "predict": "PASS (goal filter)"},
]

# Glorptax rules (cell 15) — reuse the continual harness's proven rule set + one probe.
GLORPTAX_RULES = [
    "Glorptax rule R1: vexil and mellis parcels are class Aurex; quorn parcels are class Borix.",
    "Glorptax rule R2: an Aurex parcel's base fee is 40 credits; a Borix parcel's base fee is 25 credits.",
    "Glorptax rule R3: any parcel heavier than 5 kg adds a 15-credit surcharge.",
    "Glorptax rule R4: the Drennel route subtracts 10 credits; the Pellan route adds 5 credits.",
    "Glorptax rule R5: a Borix parcel via the Drennel route is exempt from the R3 weight surcharge.",
]
GLORPTAX_PROBE = {
    "id": "c15_rules", "family": "D", "lens": "agentic",
    "query": ("A 7 kg quorn parcel is shipped via the Drennel route. Using the Glorptax rules, "
              "what is the total fee in credits? End with a line exactly: FEE: <number>"),
    "answer": "15", "predict": "PASS (RAG+compose, banked)"}


def full_facts() -> list[tuple[str, str]]:
    """(content, session_tag) for the whole 18-cell corpus: cell facts + Glorptax rules +
    no-handle pairs + filler. session_tag groups co-occurrence/multi-session facts."""
    out: list[tuple[str, str]] = []
    for c in CELLS:
        for content, sess in c.get("facts", []):
            out.append((content, sess))
    for r in GLORPTAX_RULES:
        out.append((r, "s_glorptax"))
    for p in NO_HANDLE_PAIRS:
        out.append((p["seed"], f"nh_{p['id']}"))
        out.append((p["target"], f"nh_{p['id']}"))
    for f in FILLER:
        out.append((f, "s_filler"))
    return out
