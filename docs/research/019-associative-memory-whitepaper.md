# Building and Measuring an Associative-Memory Faculty for a Stateless Language-Model Agent

**A self-contained research report — 2026-05-31**

---

## Abstract

A large language model (LLM) is **stateless**: each call begins with no memory of the previous
one. Any agent built on an LLM must therefore *re-supply* everything it "knows" as context on every
turn. For such an agent, the memory system is not a feature — it is the substrate of cognition. This
report asks a single question: **does the agent's memory behave like an associative faculty (recall
by association, links that form and strengthen from experience), or is it merely a similarity search
dressed up with a graph?**

We answer it empirically. We built a battery of controlled tests using **invented entities** (so the
LLM cannot answer from prior knowledge), measured performance on **18 real-world memory scenarios**
plus two focused "discriminator" tests, and — crucially — instrumented *which internal mechanism*
produced each answer. The result is consistent and, we believe, important:

> **Everything the system does well is carried by *similarity* (keyword and vector search) plus the
> *LLM's own reasoning over the retrieved text*. The associative graph — the edges between memories,
> multi-hop traversal, edge weights — carries essentially none of the load.**

This is not a defect report; it is a map. It shows the lever is **not** "more or denser graph edges."
It is two specific capabilities the system genuinely lacks: **forming** links from co-experience, and
**resolving** which of several equally-retrievable facts is the correct one. Both are reproduced under
controlled conditions, and for both we demonstrate — with a positive control — that the fix works
*before* building it. This document is written to be understood on its own: all test data, example
inputs and outputs, and result tables are included.

---

## 1. Background

### 1.1 The system under test (in plain terms)

The agent is an LLM wrapped in a memory architecture. The reader needs only six concepts:

| Term | What it means here |
|---|---|
| **Fact / memory** | A short natural-language statement the agent has stored (e.g. *"My dentist is Dr. Yarvik."*). Each is stored with a **vector embedding** (a numeric fingerprint of meaning). |
| **Similarity retrieval** | Given a query, the system finds stored facts whose embeddings are nearest the query's (cosine similarity), optionally blended with keyword matching. This is ordinary vector search. |
| **Graph edges** | Typed links between memories (e.g. "related-to", "supersedes", "happened-before"), each with a numeric **weight**. Edges are created at write time (by similarity threshold) and during a periodic **consolidation** phase. |
| **Consolidation ("sleep")** | An offline phase that re-examines memories, builds more edges, clusters, and does housekeeping — loosely analogous to memory consolidation during sleep. |
| **Context injection (pre-turn)** | Before the LLM answers, the system automatically retrieves a few relevant facts and pastes them into the prompt. The LLM never "calls" anything for this; it just appears in its context. |
| **Memory-search tool** | Separately, the LLM *can* choose to call a search tool mid-answer to fetch more memories (an explicit retrieval action we can count). |

The distinction between the last two is central: an answer can be produced because the right fact was
**auto-injected** before the turn, or because the LLM **actively searched** for it, or because the LLM
simply **guessed**. Telling these apart is the heart of our method.

### 1.2 Why associative memory, specifically

A human's associative memory does two things a similarity index does not:

1. **Recall by association.** A cue activates related memories — including ones that share little
   surface wording — because they were *experienced together* or are *structurally analogous*.
2. **Consolidation with plasticity.** Associations that prove useful **strengthen**; unused ones
   fade; new links **form** from co-activation ("cells that fire together, wire together").

The goal of this work is to give the agent a genuine **associative-memory faculty** — recall and
consolidation as one loop — rather than a static similarity index. Before building toward that, we
needed to know precisely *where the current system falls short of it*.

### 1.3 A prior structural observation

A code-level audit of the system's store / consolidation / retrieval found that nearly every stage
reduces to similarity:

- **Storage** links new memories almost entirely by embedding-similarity threshold.
- **Consolidation** re-derives similarity (vector backfill, re-ranking, clustering) plus LLM
  housekeeping; the only association-like signals are a thin temporal-order edge and a shared-named-
  entity edge.
- **Retrieval** is top-k vector + keyword fusion, with static (edge-weight × distance-decay) scoring.
- **The standout structural gap:** **edge weights are frozen at creation.** They never strengthen
  with use or decay with disuse. The system records how often each memory is recalled but never feeds
  that back into the graph. It cannot learn "this association keeps proving useful."

That audit was a *hypothesis*. The rest of this report tests it.

---

## 2. The central question and pre-registered hypotheses

**Question:** When the agent answers a memory-dependent question correctly, *what mechanism is
responsible* — similarity retrieval, the associative graph, or the LLM's reasoning over retrieved
text? And where it fails, *which mechanism is missing*?

**Pre-registered hypotheses (written before running):**

- H1. Pure similarity will **surface** most answers (vector search is strong).
- H2. The associative graph will **not** be necessary for most surfacing or selection.
- H3. Two regimes will resist similarity: (a) **no-handle association** — facts linked only by
  co-experience, with no shared words and no semantic closeness; (b) **resolution** — choosing the
  current fact among several equally-retrievable competitors (e.g. a value that changed over time).
- H4. Because edge weights are frozen, **plasticity** (improvement from repeated co-activation) will
  be null.

---

## 3. Methodology

The chief risk in evaluating a memory system is **measuring the wrong thing** — usually letting the
LLM's general world-knowledge answer a question the memory was supposed to answer. Our method is a
stack of safeguards designed to make every result interpretable.

### 3.1 Controls and design rules

| Safeguard | Purpose | Example |
|---|---|---|
| **Invented entities** | The LLM cannot know the answer without retrieving it | "Glorptax", "Drennby credit union", "the Tindric language", "Project Halberd" |
| **Positive control** | A question the system *must* get right (else the instrument is broken) | "What is my dentist's name?" → *Yarvik* (a stored fact) |
| **Negative control** | A question the system *must* abstain on (else it hallucinates) | "What is my lawyer's name?" → there is no lawyer fact; correct answer is "I don't know" |
| **Validity gate** | An item that's trivially findable tests nothing | For "no-handle" items, the answer fact must fall *outside* the top-10 of plain similarity search — verified on the full corpus |
| **Two lenses** | The LLM's tool-use loop can solve what static search can't | Every claim measured both on the **bare retrieval pipeline** and on the **live agent** answering in natural language |
| **False-bridge tracking** | A confidently wrong association is itself a failure | On "who can do X?" items we record whether a *wrong* person was named, not just whether the right one was |

### 3.2 Mechanism attribution — the core instrument

For each probe we record four independent signals and infer what carried the answer:

1. **Bare rank** — the answer fact's position in plain similarity search. If it's in the top-k,
   *similarity* can surface it.
2. **Injected IDs** — the exact facts auto-pasted into the prompt before the turn. (We added a debug
   readout for this; it is the only way to see auto-injection, which is otherwise invisible.)
3. **Tool calls** — how many times the LLM actively searched memory. Zero means it answered from
   whatever was auto-injected (or guessed).
4. **Grader** — was the correct token produced; was a false bridge produced.

**Worked example of why this matters.** In one test the agent scored *perfectly* with **zero tool
calls**. Naively that looks like "the memory search tool works great." The injected-IDs signal showed
the truth: the relevant facts were **auto-injected** before the turn; the LLM never searched. The
mechanism was pre-turn injection + reasoning, not the retrieval tool we thought we were testing.
Without signal #2 we would have mis-attributed the win.

> **An infrastructure bug the instrument caught.** Early agent runs returned *empty* answers with zero
> tool calls. This was not a memory finding — it was a real defect: the model's adaptive "thinking"
> sometimes ends a message with a reasoning block *after* a tool-use block, so the API reported the
> turn as finished while tool-use blocks were still present, and the agent's dispatch loop (which only
> ran tools when the turn's stop-reason was exactly "tool use") silently dropped them. We corrected the
> loop to run tools whenever tool-use blocks are present, and added a regression test. The instrument
> surfaced an infrastructure bug before it could be mistaken for a capability gap.

---

## 4. The test data

All corpora use a single fictional persona and invented entities. They are reproduced here in full so
the report stands alone.

### 4.1 Phase 1 — a synthetic "profession" (declarative test-time learning)

To test whether the agent can **learn new rules and apply them**, we invented a parcel-fee scheme,
"**Glorptax**", with five private rules and arithmetic, checkable answers.

**The rules (the "reference material" the agent learns):**

| Rule | Content |
|---|---|
| R1 | A parcel's class is set by material: **vexil** and **mellis** are class **Aurex**; **quorn** is class **Borix**. |
| R2 | Base fee by class: **Aurex = 40 credits**; **Borix = 25 credits**. |
| R3 | Any parcel heavier than **5 kg** adds a **15-credit** surcharge. |
| R4 | The **Drennel** route subtracts **10**; the **Pellan** route adds **5**; standard adds 0. |
| R5 | A **Borix** parcel via the **Drennel** route is **exempt** from the R3 surcharge. |

**The tasks** (the agent sees the parcel, must produce the fee). The invented credit values mean the
agent cannot guess — it must have learned the rules.

| # | Parcel | Rule chain | Correct fee |
|---|---|---|---|
| 1 | 3 kg vexil, standard | R1→R2 | 40 |
| 2 | 2 kg quorn, standard | R1→R2 | 25 |
| 3 | 8 kg mellis, standard | R1→R2→R3 | 55 |
| 4 | 4 kg vexil, Pellan | R1→R2→R4 | 45 |
| 5 | 3 kg quorn, Pellan | R1→R2→R4 | 30 |
| 6 | **7 kg quorn, Drennel** | R1→R2→**R5**→R4 | **15** |
| 7 | **9 kg quorn, Drennel** | R1→R2→**R5**→R4 | **15** |
| 8 | **7 kg vexil, Drennel** | R1→R2→R3→R4 (**R5 must NOT fire — Aurex**) | **45** |
| 9 | **6 kg mellis, Drennel** | R1→R2→R3→R4 (**R5 must NOT fire — Aurex**) | **45** |
| 10 | 4 kg quorn, Drennel | R1→R2→R4 (under 5 kg, R5 moot) | 15 |

Tasks 6–10 require **composing several rules**; tasks 8–9 are *discrimination* traps (a naive "Drennel
waives the surcharge" reading gives the wrong 35 instead of 45).

### 4.2 The 18-cell capability corpus

The main instrument: one persona, ~77 facts total (the cell facts below, the five Glorptax rules, six
"no-handle" pairs from §4.3, and ~40 unrelated **filler** facts that act as distractors). The 18 cells
span four families. Below, each cell shows the **stored facts**, the **query**, the **expected
answer**, and any **distractor** that a confused system might wrongly surface.

**Family A — Retrieval mechanics**

| Cell | Stored fact(s) | Query | Expected | Distractor |
|---|---|---|---|---|
| 1 Surface | *My dentist is Dr. Yarvik.* | "What is my dentist's name?" | Yarvik | — |
| 2 Semantic | *I am allergic to the antibiotic claramycin.* | "Which medication do I need to avoid?" | claramycin | — (no shared words with query) |
| 3 Needle | *My building's super is Oletti; intercom code 4471.* | "What's the intercom code for my building?" | 4471 | — (among ~40 filler facts) |
| 4 Abstain | *(no lawyer fact exists)* | "What is my lawyer's name?" | *abstain* | any invented name |
| 5 Cross-type | *Project Halberd's launch is behind schedule.* + a stored **decision** "push launch to Q3" | "What's going on with Halberd's launch?" | Q3 | — |

**Family B — Association / bridging**

| Cell | Stored fact(s) | Query | Expected | False bridges |
|---|---|---|---|---|
| 6 Co-mention | *Halberd is behind schedule.* + *I put Tomas Pell in charge of Halberd.* | "Who leads the project that's behind schedule?" | Tomas Pell | Gus Trelawny, Wrenn Hald |
| 7 Role/skill | *The Quill ledger is written in the Tindric language.* + *Gus Trelawny is the most fluent Tindric programmer I know.* | "Who could fix the Quill ledger system?" | Trelawny | Tomas Pell, Marl Venn |
| 8 Multi-hop | *Marl Venn owes me a favour.* + *Marl Venn is a licensed electrician.* | "Who that owes me a favour could rewire my shed?" | Marl Venn | Gus Trelawny |
| 9 Experiential | *I adopted a greyhound named Pim that afternoon.* + *That same afternoon I signed the Verro Street office lease.* | "What else did I do the day I adopted Pim?" | Verro | — |
| 10 Abstract | *Wrenn Hald once untangled a circular-wait standoff where two services each blocked waiting on the other.* | "Who do I know who has resolved a situation where two parties were each stuck waiting on the other to move first?" | Wrenn Hald | Marl Venn, Gus Trelawny |
| 11 No-handle | *(see §4.3 — 6 pairs)* | *(per pair)* | *(per pair)* | — |

**Family C — Temporal / dynamic**

| Cell | Stored fact(s) | Query | Expected | Stale distractor |
|---|---|---|---|---|
| 12 Contradiction | *I bank primarily with Halloway Federal.* + *Update: I moved everything to Pellan Mutual after Halloway shut my branch.* | "What is my current primary bank?" | Pellan Mutual | **Halloway** |
| 13 Recency | *In Sept 2025 I chose the Korren framework for the dashboard.* + *In Feb 2026 I dropped Korren and rebuilt the dashboard in Aurelis.* | "What is my most recent dashboard-framework decision?" | Aurelis | **Korren** |
| 14 Multi-session | *My conference talk is in Pellan City.* + *My talk is the second week of March.* | "When and where is my conference talk?" | March + Pellan | — |

**Family D — Learning / adaptation**

| Cell | Stored fact(s) | Query | Expected | Distractor |
|---|---|---|---|---|
| 15 Rules | *(Glorptax rules)* | "A 7 kg quorn parcel via Drennel — total fee?" | 15 | — |
| 16 Correction | *I like my status reports in past tense.* + *Correction: always use present tense for status reports.* | "What tense for my status reports?" | present | **past** |
| 17a Goal-gated | *Marl Venn is a licensed electrician.* + *Bex Carrow leads hard routes at my climbing gym.* | "I need to rewire my shed — who do I know?" | Marl Venn | Bex Carrow |
| 17b Goal-gated | *(same two facts)* | "I want a partner for a hard climbing route — who do I know?" | Bex Carrow | Marl Venn |

### 4.3 The "no-handle" pairs (cell 11) — the association discriminator

This cell is **not** a question-answering test, and it is worth being precise about why. Each pair is
two activities from one person's life that genuinely happened on **one occasion** (the same day, the
same move, the same event) but are in **completely different life-domains** (e.g. *pet adoption* and
*signing an office lease*). The two facts share **no words** and are **not semantically close**, and
the shared occasion is **never written into either fact** — it exists only because they were recorded
together.

The query is a **cue**, not a question: it names only the **seed** ("Tell me about the day I adopted my
greyhound Pim"). It deliberately does **not** ask for the other fact. The thing being measured is
whether cueing the seed makes the **co-experienced fact surface** — the way recalling one event in a
human memory brings up another from the same occasion. The column below is therefore labelled
"**co-experienced fact that should surface**", *not* "the answer."

A design subtlety we confirmed empirically: one *cannot* reword the cue to explicitly *ask* for the
co-experienced fact without giving the embedding a semantic handle (asking "what else did I do that
day?" makes the answer match on "activities I did"), which destroys the disjointness the test depends
on. So the cue must stay bare, and the target must live in an unrelated domain — that is exactly what
makes the association "no-handle." The answer tokens are unique inventions so the grader cannot
accidentally match an unrelated fact.

| Pair | Seed fact (the cue names this) | Co-experienced fact that *should* surface (unrelated domain) | Same occasion | Cue | Token |
|---|---|---|---|---|---|
| 1 | I adopted a greyhound named **Pim**. | I signed a lease on the **Galt Street** office unit. | that day | "Tell me about the day I adopted my greyhound Pim." | Galt |
| 2 | I started physiotherapy with **Dr. Osei**. | I renewed the domain **quillford**.net. | that day's errands | "How is my physiotherapy with Dr. Osei going?" | quillford |
| 3 | I take pottery classes from **Sable**. | I moved my savings into the **Drennby** credit union. | same admin day | "Tell me about my pottery classes with Sable." | Drennby |
| 4 | I moved into the flat on **Harwick Court**. | I switched my electricity to **Vantle** Energy. | the move | "Tell me about moving into the Harwick Court flat." | Vantle |
| 5 | I performed in a piano recital of **Vellmont**. | I had the leaking **garage** roof patched. | that weekend | "How did my Vellmont piano recital go?" | garage |
| 6 | I threw my daughter's first **birthday** party. | I opened a savings account at the **Kessler** building society. | that occasion | "Tell me about my daughter's first birthday party." | Kessler |

All six are confirmed *disjoint*: plain similarity does not place the co-experienced fact in the
top-10 for the cue (the validity gate). Pairs are **different-domain on purpose** — that is what makes
the association unreachable by similarity and therefore a genuine test of an associative faculty.

A useful contrast with **cell 9 (experiential)**: cell 9 uses a *similar* same-day scenario but writes
the shared moment **into the fact text** ("…that same afternoon I signed the lease"), so the LLM can
read the link and bridge it. Cell 11 deliberately leaves the occasion **out** of the text. The two
cells are therefore a near-minimal pair: with the occasion in the text the system succeeds (cell 9);
with it removed, leaving only the raw co-occurrence, the system fails (cell 11). That difference is
exactly the faculty under test — forming and using a link that *isn't* spelled out in any single
memory.

### 4.4 Filler (distractors)

~40 unrelated invented facts give the corpus realistic competition, e.g.: *"My espresso machine is a
Brevill Duo; I pull 18-gram shots." · "My favourite hiking trail is the Korrel Ridge loop." · "My
accountant is Sasha Demir." · "I keep a sourdough starter named Bram." · "My laptop runs the Aurelis
Linux distro."* These ensure a correct answer at rank 1 has genuinely beaten ~50 alternatives, not 5.

---

## 5. Results

### 5.1 Phase 1 — declarative learning works, and composes

| Condition | Overall | Simple cases | Composition cases |
|---|---|---|---|
| **Cold** (rules not in memory) | **0 / 10** | 0/5 | 0/5 |
| **After learning** (rules in memory) | **10 / 10** | 5/5 | 5/5 |

Cold, the agent **abstained on every item** — it never guessed a fee, because the invented credit
values are unguessable (the leak guard held). After the rules were learned (through the normal learning
path, followed by a consolidation pass), the agent answered **every unseen variation correctly**,
including all multi-rule compositions and both discrimination traps (it correctly charged 45, not 35,
for the Aurex-via-Drennel parcels).

**Reading:** Storing facts and retrieving them to reason over is **not** a bottleneck. The agent learns
private rules and *composes* them on novel inputs. (What this test does **not** show is *improvement
with repetition* — see §5.4 on plasticity.)

### 5.2 The bare pass — similarity surfaces almost everything (but not correctly)

Plain similarity search, no LLM, no graph traversal. "Reachable" = the answer fact is in the top-10.

| Cell | Answer rank | Reachable? | Wrong answer also in top-10? |
|---|---|---|---|
| 1 Surface / 2 Semantic / 3 Needle | 1 / 1 / 1 | ✓ | — |
| 5 Cross-type | — | ✗ | *(invalid — see §7)* |
| 6 Co-mention | 3 | ✓ | **yes** — Gus Trelawny, Wrenn Hald |
| 7 Role/skill | 2 | ✓ | **yes** — Tomas Pell, Marl Venn |
| 8 Multi-hop | 1 | ✓ | — |
| 9 Experiential | 3 | ✓ | — |
| 10 Abstract | 1 | ✓ | **yes** — Marl Venn, Gus Trelawny |
| 12 Contradiction | 4 | ✓ | **yes — Halloway (the stale bank)** |
| 13 Recency | 2 | ✓ | **yes — Korren (the old framework)** |
| 14 Multi-session | 1 | ✓ | — |
| 15 Rules | 1 | ✓ | — |
| 16 Correction | 2 | ✓ | **yes — past (the corrected-away value)** |
| 17a / 17b Goal-gated | 1 / 1 | ✓ | the *other* goal's answer (expected) |

**15 of 16 testable cells are reachable by pure similarity.** This confirms H1: vector search surfaces
the answer for nearly every real-world scenario, including the "bridging" and temporal ones.

**But reachability is not correctness.** On contradiction, recency, and correction, the **wrong/stale
value is equally reachable** — sitting in the top-10 right beside the right one. For contradiction it
even **out-ranks** the correct answer. Similarity can *surface* these; it cannot *resolve* them.

### 5.3 The agentic pass — the LLM selects well, with one telling failure

The live agent answered each query in natural language. **12 of 13 cells passed.** Example answers
(abbreviated), with the mechanism signals:

| Cell | Verdict | Agent's answer (excerpt) | Tool calls |
|---|---|---|---|
| 4 Abstain | **PASS** | "I don't have that information. I have records of several contacts — Sasha Demir, Dr. Yarvik… but no lawyer." | 1 |
| 6 Co-mention | **PASS** | "The project behind schedule is Project Halberd, and you put **Tomas Pell** in charge of it." | 1 |
| 7 Role/skill | **PASS** | "**Gus Trelawny** — the Quill ledger is written in Tindric, and Gus is your most fluent Tindric programmer." | 1 |
| 8 Multi-hop | **PASS** | "**Marl Venn** — he owes you a favour and he's your electrician." | 2 |
| 9 Experiential | **PASS** | "You signed the **Verro Street** office lease that same afternoon." | 1 |
| 10 Abstract | **PASS** | "**Wrenn Hald** — he untangled a circular-wait standoff where two services each blocked on the other." | 0 |
| 12 Contradiction | **FAIL — confabulated** | "Your primary bank is **Halloway Federal**." *(the stale value)* | 0 |
| 13 Recency | **PASS** | "Most recently, Feb 2026, you dropped Korren and rebuilt in **Aurelis**. This supersedes the earlier choice." | 0 |
| 14 Multi-session | **PASS** | "Your talk is the **second week of March**, in **Pellan City**." | 0 |
| 16 Correction | **PASS** | "**Present tense.** I have two conflicting notes; the correction says always use present." | 0 |
| 17a / 17b Goal-gated | **PASS** | "For the shed: **Marl Venn** (electrician)." / "For climbing: **Bex Carrow**." | 0 |
| 15 Rules | **PASS** | "quorn = Borix (25), Drennel exempts the surcharge, −10 route → **FEE: 15**." | 1 |

Two things stand out.

**First, the mechanism.** On most cells **tool calls = 0** and exactly **two facts were auto-injected**
before the turn. The work was done by **pre-turn injection + LLM reasoning**. The agent's own search
loop barely fired, and **the associative graph carried none of the passes.**

**Second, the one failure is the most informative cell.** On contradiction (c12) the agent answered
with the **stale** bank, "Halloway Federal," and missed the superseding "moved to Pellan Mutual" fact.
Why? The bare pass already showed the stale value out-ranks the current one; in the tiny two-fact
auto-injected window, the *correcting* fact didn't make it in, and there is no mechanism to prefer the
newer value. (Note the *honest caveat*: cells 13 and 16 pass only because the resolving cue — a date,
the word "Correction" — sits **in the fact text**, which the LLM reads. That is text comprehension, not
a temporal/supersession faculty; it breaks the moment the resolving fact doesn't survive retrieval, as
in c12.)

### 5.4 Cell 11 (no-handle) and plasticity — the discriminator results

**Validity:** all 6 pairs were confirmed disjoint (the co-experienced fact fell outside the top-10 of
plain similarity for the cue). Three fell **entirely outside the top-30** (truly unreachable); three sat
mid-pack (ranks 12–28) — present in the wider pool but never in the top-10.

**Positive control — does an injected link surface the co-experienced fact?** We manually inserted a
single co-activation edge between the two facts of each pair and re-ran retrieval (weight 0.9):

| Pair | Rank before edge | Rank after edge | Surfacing mechanism |
|---|---|---|---|
| Osei → quillford | absent (>30) | **2** | weighted neighbour path |
| Sable → Drennby | absent (>30) | **2** | weighted neighbour path |
| Recital → garage | absent (>30) | **2** | weighted neighbour path |
| Move → Vantle | 12 | **6** | adjacency (weight-blind) |
| Birthday → Kessler | 25 | 17 | adjacency (weight-blind) |
| Pim → Galt | 28 | 19 | adjacency (weight-blind) |

In every pair the injected link **moves the co-experienced fact up**; for the three genuinely-absent
ones it lifts them from *unreachable* into the **top-10**.

**Does the link's *weight* matter (the plasticity question)?** We swept the edge weight from 0.3 to 0.9
on the three genuinely-absent pairs (the only ones where the weighted path applies):

| Pair | Weight 0.3 | Weight 0.9 |
|---|---|---|
| Osei / Sable / Recital | rank ~31 (still absent) | **rank 2–5 (into top-10)** |

The weight **strongly modulates** the result — **but only for targets that were genuinely absent** from
the candidate pool. For a target already mid-pack, weight is inert: the system has two edge-consuming
mechanisms that behave differently — one (an "adjacency" boost) ignores weight entirely, and the
weight-sensitive one **skips any candidate already retrieved**.

*(Methodological note: an earlier version of one pair used an answer token that also appeared in an
unrelated filler fact, so the grader matched the wrong fact and made that pair look anomalously
"in-pool." Re-running with unique invented tokens removed the artifact — that pair then behaved exactly
like the other mid-pack ones, and the finding above held unchanged.)*

**Reading.** This is the crux of the plasticity story and it is *good news with a precise boundary*:
strengthening a link by use **would** change retrieval — exactly in the case that matters most for
associative memory: **recalling something that plain similarity missed.** It would *not* re-rank things
already found. But none of this happens today, because (a) the co-occurrence link is **never created**
in the first place (we had to inject it), and (b) even if created, the weight **never strengthens** with
use (it is frozen). Both are write-side gaps.

### 5.5 The resolution mechanism — sound but mis-wired

The system *has* a "recency resolver" intended to handle contradiction/recency: among same-subject
facts with differing dates, mark the newer "current" and demote the older "superseded." We tested it
directly on cells 12 and 13 after giving the facts the metadata it requires (a shared subject, distinct
dates, and parallel phrasing):

| Cell | Resolver OFF | Resolver ON |
|---|---|---|
| 12 Contradiction | stale "Halloway" **rank 1**; current "Pellan Mutual" rank 2 *(stale wins)* | current "Pellan Mutual" **rank 1**, tagged *current*; stale "Halloway" demoted to **rank 10** |
| 13 Recency | current "Aurelis" rank 1; stale "Korren" rank 2 | current rank 1; stale "Korren" demoted **rank 2 → 15**, tagged *superseded* |

**The mechanism works** — when it fires, it flips the stale-over-current ordering and tags the correct
value. **But it does not fire in normal operation**, for two reasons:

1. **Wrong path.** It runs only in the *active memory-search tool*, not in the *pre-turn auto-injection*
   path that the agent actually used (recall: tool calls = 0 on c12). So in live operation the agent
   never sees the resolved ordering.
2. **Under-fed trigger.** It requires structured metadata (subject, dates) and near-restatement
   phrasing. Natural correction text ("Update: I moved everything to Pellan Mutual after Halloway shut
   my branch") is too dissimilar from the original to trip the trigger.

So contradiction-handling fails not because the capability is *absent*, but because it is **in the wrong
place and starved of the data it needs**.

---

## 6. What the evidence says

Pulling the threads together against the hypotheses:

- **H1 confirmed.** Similarity surfaces 15/16 cells. Vector search is the workhorse of *retrieval*.
- **H2 confirmed.** The associative graph carried **none** of the 12 agentic passes; every success is
  attributable to similarity + the LLM's reasoning over auto-injected text.
- **H3 confirmed, sharpened.** Two regimes resist similarity:
  - **No-handle association** — with no shared words and no semantic closeness, the answer is simply
    unreachable; only an explicit co-occurrence link rescues it (proven by the positive control).
  - **Resolution under competition** — when current and stale values are both retrievable, similarity
    surfaces the stale one first and the agent answers wrongly.
- **H4 confirmed, with nuance.** Plasticity is null today (links don't form; weights don't strengthen).
  But the positive control shows that *if* a link existed and its weight grew with use, retrieval would
  respond — specifically for the novel-recall case.

A one-line synthesis: **the system is a competent similarity-plus-LLM machine; its "associative graph"
is decorative for the scenarios tested.** What it lacks is not retrieval breadth but two faculties.

---

## 7. The gaps (the prioritized backlog)

### Gap 1 — **Formation**: links are never created from experience
*Evidence: cell 11.* Two things experienced together are not linked unless they happen to be
word-similar or share a named entity. So a genuinely novel association is unreachable. The positive
control proves that **once the link exists, retrieval already uses it** (and honors its weight for novel
targets).
**Fix:** a consolidation step that creates a link between memories that **co-occur / co-activate**, plus
(later) **strengthen-by-use / decay-by-disuse** so the weight becomes meaningful. **Success test:** cell
11 moves from FAIL to PASS *without* manual link injection.

**The hard sub-question this fix must answer: *which* co-occurrence is worth a link?** Linking every
pair of facts merely mentioned in the same session would flood the graph with noise (two unrelated
remarks in one conversation are not a meaningful association). A useful formation rule needs a signal
of *genuine* co-experience — same event, same day, causal or referential connection — not mere
proximity. Designing and validating that signal is the core of the formation work, and it is where a
naive "link everything co-mentioned" approach would do more harm than good.

### Gap 2 — **Resolution**: the right fact among competitors isn't chosen
*Evidence: cell 12.* When several retrievable facts compete (current vs. stale value), the stale one is
surfaced first and the agent answers with it. The resolver that would fix this exists but runs in the
wrong path and is starved of metadata.
**Fix:** (a) run the resolver in the **auto-injection** path, not just the search tool; (b) populate the
metadata it needs (subjects, dates) and **loosen its trigger** so it fires on natural correction
phrasing. **Success test:** cell 12's confabulation flips to PASS.

### What is explicitly *not* the gap
- **Not denser similarity edges** — similarity already surfaces 15/16 cells.
- **Not multi-hop graph traversal** — the LLM bridges concept-chains at query time on its own.
- **Not declarative learning** — the agent learns and composes private rules at ceiling (Phase 1).
- Consequently, elaborate graph/substrate machinery (including biologically-inspired replay networks)
  is **deferred**: edges are not where association happens in the cases we measured; the embedding and
  the LLM are.

### One invalid cell, disclosed
Cell 5 (cross-type) returned no result because of a **fixture error on our part** — the linked
"decision" record was never actually inserted into the store, so there was nothing to surface. It is
marked invalid rather than counted, and is noted here for completeness.

---

## 8. Association types vs. mechanisms (the scope of the faculty)

A fair worry on seeing the gaps arrive one at a time — co-occurrence, then supersession, then
role/skill — is that this is whack-a-mole: the real world has *dozens* of ways things associate
(causal, part-whole, analogy, goal-relevance, symbolic, cross-modal…), so will the backlog ever
end? The instrument's answer is no, because **the many association *types* collapse to a few
*mechanisms*, and most of them already work.**

| Mechanism | Association types it covers | Status |
|---|---|---|
| **Similarity** (embedding geometry) | semantic, paraphrase, abstract/structural analogy, metaphorical proximity | ✅ works |
| **LLM reasoning** over what's in context | causal, role/skill, part-whole, multi-hop, goal-relevance, contradiction, conceptual metaphor | ✅ works (if the facts are present) |
| **Experiential co-activation** (edge formed from co-occurrence, strengthened by use) | episodic, contextual, "experienced-together" — *regardless of the specific relation* | ⬛ Gap 1 |
| **Temporal structure** (event-date ordering / resolver) | recency, supersession, before/after | ⬛ Gap 2 |

Every one of the 18 cells fell into these four; none escaped. We are not fixing types — we are
building the **two missing mechanisms**. The formatter fix that surfaced graph-neighbours is itself
type-agnostic: it renders neighbours of *any* relation, and the LLM reads the relation label and
interprets. Two refinements sharpen the scope:

### 8.1 World-true vs. context-true associations
There are two fundamentally different kinds of association, and the memory faculty owns only one:
- **World-true** — how concepts relate *in general* ("anger looks like turbulence"; "loud and
  bright are both high-intensity"; "a ring conventionally signifies marriage"). These are
  **parametric**: already carried by the LLM and the embedding from training. The faculty should
  *not* re-derive them.
- **Context-true** — what is true of *this user/situation* ("my grandfather's watch means
  resilience *to me*"). These are **episodic**: they don't exist until experience creates them, so
  they must be learned and stored. This is the faculty's job.

This dissolves a whole apparent class of "hard" associations. **Synesthesia / cross-modal**
("a loud colour"), **personification** ("the ocean is angry"), and **conventional symbolism**
(ring → marriage, flag → nation) are largely *world-true* — the LLM does conceptual metaphor and
structure-mapping natively, and the embedding already places metaphorically-linked terms near each
other (Phase-0 confirmed it spans abstract structure). They feel exotic but are home turf for a
model trained on human metaphor. For a *text* agent, cross-modal binding reduces to embedding
proximity; genuine multimodal binding is a joint-embedding (representation) question, not a memory
one. None of these require a new memory mechanism.

### 8.2 Emergent personal symbolism — the earned entry for an attractor substrate
The genuinely memory-relevant slice is **personal symbolism**, which splits once more:
- **Explicit** ("this watch means resilience to me" — stated) → stored as a fact, retrieved,
  applied. Covered by similarity + LLM.
- **Emergent / implicit** — the user *never states* the symbol, but it is latent in a *recurring
  pattern* (every mention of the watch coincides with talk of hard times). Inferring "the watch
  symbolises resilience for this user" from a co-activation pattern **across many episodes** is
  covered by *nothing we have built*.

This emergent case is not a fifth mechanism — it is a *composition* of **Gap 1** (co-activation
detection) **plus the LLM-as-cortex naming the pattern during consolidation**. But it stresses a
capability pairwise-edge formation lacks: **discovering and naming a latent association from a
statistical pattern over the whole history**, not from a single co-occurrence. Reconstructing a
coherent association (watch ⇄ resilience) from many partial, noisy co-activations is exactly what an
attractor / dense-associative (Hopfield-style) network or replay-consolidation does that edge
counting cannot. **This is the concrete, discriminating need that would justify the heavy substrate
(the "tinyHippo" rung)** — and it comes with its own test: a cell where the symbol is *never stated*,
only emergent. Until such a need is demonstrated, the substrate stays deferred.

**Net principle: build mechanisms, not types.** Count mechanisms (four, two working); add a new one
only when a cell proves that none of the four can carry an association — and gate the substrate on
the emergent-symbolism test, not on a someday-rung.

## 9. Limitations and threats to validity

We hold our own conclusions to the same scrutiny as the system:

- **Authoring bias.** Corpora are hand-written with invented entities. The abstract-analogy items were
  phrased evocatively, so the embedding's success there is partly author-driven; a version where the
  structure is buried in domain detail would stress it harder. Findings are **strong-directional**, not
  proofs.
- **Small samples.** The plasticity weight-modulation result rests on four disjoint pairs; several cells
  have one to three items. Enough to register a clean qualitative gap, not to estimate effect sizes.
- **Text-leak passes.** Cells 9, 13, and 16 pass partly because the resolving cue (a date, "Correction",
  "same afternoon") is *in the fact text* and the LLM reads it — text comprehension, not a temporal
  faculty. It breaks exactly when the cue doesn't survive retrieval (cell 12).
- **One model, one embedding.** Results are specific to the LLM and the embedding model used; the
  "embedding spans abstract structure" finding in particular is representation-dependent.
- **Evaluation shape.** We used a shared-corpus setting (all facts present at once) rather than isolating
  each question, after an earlier finding that isolation inflates results. Absolute numbers are
  within-instrument comparisons, not a production benchmark.
- **A design tension intrinsic to the no-handle test.** The cleanest test of an associative faculty
  needs a cue that gives *no* information about the target — but a natural-language *question* must, by
  definition, describe what it wants. We confirmed empirically that rewording the cue to "ask for" the
  co-experienced fact gives the embedding a handle and collapses the test (the target becomes
  similarity-reachable). The test therefore relies on bare cues and different-domain targets, which is
  honest but means the cell measures *associative surfacing under a bare cue*, not the more natural
  "the agent reasons its way to the connection." The latter is partly covered by the experiential and
  multi-hop cells.
- **A grading pitfall we hit and fixed.** Substring grading can match the wrong fact if an answer token
  also appears in an unrelated stored fact; one no-handle pair was affected and looked anomalous until
  we switched to unique invented tokens. Re-running confirmed the finding was unchanged. The lesson —
  answer tokens must be unique within the corpus — is general to this style of evaluation.

---

## 10. Conclusion

We set out to learn whether an LLM agent's memory behaves like an associative faculty or a similarity
index with graph dressing, and — before building anything new — to locate the real gap. The instrument
answered clearly and repeatedly: **for surfacing and selection, the system is already a competent
similarity-plus-LLM machine, and the associative graph is not carrying the load.** What the system
genuinely lacks is the ability to **form links from experience** and to **resolve among the facts
similarity surfaces** — not more retrieval, and not a denser graph.

That is a stronger position than where we began. The vague goal "improve associative memory" is now two
concrete, controlled engineering problems — **formation** and **resolution** — each with a positive
control that already demonstrates the fix will work, and each with an unambiguous success test on a
specific scenario. The next phase builds those two fixes and lets the same instrument report whether the
cells move.

---

## 11. Postscript — what we built, what an independent dataset revealed, and the one test that remains (2026-06-01)

The two fixes were built, validated on the instrument, and deployed.

**Formation (the experiential gap).** A consolidation pass now forms an explicit associative link
between facts that were *learned in the same episode* — co-experience, not similarity. The link is
weighted, deduplicated, and idempotent; it is consumed at recall time by interleaving the linked memory
into the ranked result list with an explicit "[via …]" marker rather than burying it in a trailing
section. On the instrument, the dedicated experiential cell moved from 0/3 to 3/3 with no regression on
the strength cells. The links were also backfilled onto the live deployment (212 links over its real
history).

**Resolution (the selection gap).** When similarity surfaces two facts that state different values for
the same attribute and each carries a different *event date*, a recall-time resolver now marks the newer
one "current" and the older one "superseded" (demoting, never deleting it) and renders that verdict
inline. The previously failing supersession cell now passes.

**What an independent dataset revealed.** Validating both mechanisms on a separate, externally authored
long-conversation benchmark — deliberately, to escape the bias of testing on data we wrote — showed that
*as built, both mechanisms are tuned to clean, discrete inputs and do not yet generalize to messy,
prod-shaped sessions.* Co-experience linking keyed on a whole episode is too coarse when a single session
spans many sub-topics with many facts (it trips the noise gate and forms nothing); and real
contradictions are usually diffuse and implicit rather than two crisply opposed dated statements, which
the single-shot synthesizer already reconciles on its own. This is the central honest result of the
follow-on: **the formed-association layer demonstrably helps on clean data and is, so far, decorative on
messy task-shaped data — exactly where a competent retriever plus a strong language model is already the
workhorse.**

**The one test that remains.** Across four separate investigations, every formed-structure layer we
added was either load-bearing only on clean synthetic data or fully substitutable by retrieval-plus-LLM.
That leaves a single class of problem a similarity index and a language model provably *cannot* serve: an
association that is **never stated and exists only as a recurring co-activation pattern across many
episodes** — a private, emergent symbol with no text to retrieve and no parametric knowledge to draw on.
Reconstructing such a latent association from many partial, noisy co-activations is precisely what a
dense pattern-completion substrate does that pairwise-edge counting cannot. Whether that class occurs
often enough to justify the substrate is an empirical question, and it is the next — and decisive — test:
a three-arm experiment (retrieve-plus-LLM vs. a formed structure vs. a learned one) over invented,
never-stated symbols, with an explicit guard against authoring an item that only the structure can solve.
If retrieval-plus-LLM handles it, the heavy substrate is permanently deferred and this line of work is
*complete at the cheap-wins level it has already reached*. If it provably fails where a formed structure
succeeds, that is the first earned reason to build the substrate — and only then. We did not build it on
hope; we will not build it without that result.

---

### Appendix A — How to read the four mechanism signals

| If the answer is… | …and the signals show | then it was carried by |
|---|---|---|
| in plain-search top-k | (any) | **similarity** (keyword if shared words, else embedding) |
| not in top-k; auto-injected; 0 tool calls | injected-IDs contains it | **pre-turn injection** (still similarity at root) |
| not in top-k; produced after a tool call | tool calls > 0 | **the LLM's active search loop** |
| reachable only after injecting a link/weight | positive control | **the associative graph** (the faculty under study) |
| not produced even after injecting the link | — | a deeper traversal/selection gap |

### Appendix B — Glossary

**Embedding** — a numeric vector representing a text's meaning; nearby vectors mean similar meaning.
**Cosine similarity** — the standard nearness measure between two embeddings.
**Top-k / rank** — a fact's position in a similarity-sorted result list; "rank 1" is the closest match.
**Edge / weight** — a stored link between two memories and its strength.
**Consolidation ("sleep")** — an offline pass that builds links and does memory housekeeping.
**Pre-turn injection** — facts auto-added to the prompt before the LLM answers, with no explicit search.
**False bridge** — a confidently-stated but wrong association (e.g. naming the wrong person).
**No-handle association** — two facts linked only by co-experience, with no shared words or semantic
closeness; the strict test of an associative faculty.
