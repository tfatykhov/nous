# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One user: the owner-operator of a single Nous deployment, who also builds Nous. They read these surfaces to watch their own agent. They are an expert reader who knows the internals and wants them called by their real names.

The repository is public, but self-hosters and collaborators are not an audience these surfaces are designed for.

## Product Purpose

Nous is a cognitive agent with persistent memory and decision intelligence that learns from experience. Its human-facing surfaces exist for three primary jobs, all confirmed by the owner:

- **See what Nous did and why.** Each conversation and background task, its turns, the frame chosen for each turn and the reason, and every tool call and model call.
- **Act on what Nous raised.** Approvals, triage and course corrections on actions it took.
- **Decide from evidence.** Read the numbers behind a flag or a change (recall, latency, cost, rates) before switching it on.

Checking general health (memory, retrieval, graph, write health, heartbeat) supports these jobs but is not one of them. The owner wants a high-level all-clear, not a tour of every subsystem.

Success means the owner does those three jobs from these surfaces.

## Positioning

These surfaces read Nous's own cognitive record rather than generic LLM traces. That record holds:

- the frame of every turn, with the reason it was chosen (pattern, default, carried, referent, critic or direct) and what the critic recommended;
- the reason the model gave for every tool call, including refused, gated and failed calls;
- every decision, with its rationale, calibration and outcome;
- facts, episodes and procedures, with where each one came from.

The companion is composed by the agent itself: Nous decides what the owner needs to see and act on, and renders it.

## Operating Context

- **Telegram** is the conversational and notification channel. The companion and Telegram write into the same conversation.
- **The companion** is used mainly on a phone, installed to the home screen. *(Inferred from the PWA setup; not confirmed.)*
- **The dashboard** is used mainly in a desktop browser, and should still work at phone width. A phone-width pass is pending. *(Inferred; not confirmed.)*
- **Access:** one self-hosted deployment, reached through a login proxy.
- **Flags:** features ship behind flags that stay off until the owner reads the evidence and turns them on. The dashboard is where that evidence is read, usually right after a deploy or a flag change.
- **Data:** screens read live data from the Nous REST API and poll for updates.

## Capabilities and Constraints

Two surfaces, with different owners and lifecycles (F092):

- **Dashboard** at `/dashboard/v2/`:
  - fixed, human-designed and read-mostly; a Svelte 5 SPA in `dashboard-app/` with hash routes;
  - 18 nav screens on main: Overview, Graph, Browse, Decisions, Heartbeat, Observability, Retrieval, Health, Scoring, Work, Model calls, Guards, Intentions, Autonomy, Consolidation, Workflows, Subtasks, Identity;
  - retired route names redirect to the screens that absorbed them (activity, admission, density, cache, execution).
- **Companion** at `/companion`, an installable PWA:
  - agent-composed, ephemeral and action-oriented;
  - Nous describes the UI in the A2UI protocol, using a closed catalog of components (`dashboard-app/src/companion/catalog/`). No code crosses the boundary, and a new component is a catalog change, not agent freedom;
  - Nous pushes surfaces, such as a triage card waiting after an overnight heartbeat;
  - an escalation arrives as a recommendation with options: the recommendation is pre-selected and its reasoning is inline, and a click records the decision.

Recorded data:

- Recording of turns, tool calls and model calls is flag-gated.
- Recorded text is redacted before it is stored, and screens show only what was stored.
- Retention: turns and tool calls are kept 30 days, model-call numbers 30 days, model-call text 7 days.

Terminology is the internal vocabulary, drawn from Minsky's Society of Mind:

- the organs: Brain (decisions, calibration, guardrails) and Heart (episodes, facts, procedures, censors, working memory);
- session, turn, frame, tool call, model call, intention, heartbeat;
- consolidation (sleep), tool notes.

Out of scope or undecided:

- **Native Android companion (F097):** a separate Kotlin app, not covered here.
- **Actions in the dashboard:** acting on what Nous raised is a primary job, but today it happens in the companion and Telegram. Whether the dashboard should also carry actions is undecided.

## Brand Commitments

- **Name:** Nous (Greek: mind, intellect). The companion installs under the name "Nous".
- **Vocabulary:** Minsky's Society of Mind is the conceptual frame, and its vocabulary is product terminology (organs, frames, censors).

## Evidence on Hand

Real content:

- Live production data through the REST API (`docs/reference/rest-api.md`).
- Feature specs in `docs/features/`. The surfaces are covered by F021 (memory dashboard), F032 (execution ledger), F092 (companion), F092.1 and F092.2 (micro-apps and agent actions), F093 (micro-app design system), F094 (visualisation vocabulary), F096 (report vocabulary) and F108 (agent work view).
- Component fixtures in `dashboard-app/src/companion/catalog/__fixtures__/`.

Absences:

- There are no outside users, testimonials or usage claims, and none should be invented.
- Sample data must never be presented as real agent activity.

## Product Principles

1. **The why sits beside the what.** An action shown without its reason (frame reason, tool-call reason, decision rationale) is half a record.
2. **Evidence before switches.** A screen that supports a decision shows its numbers with their window (how many, since when) and says when the baseline is too thin to judge.
3. **One expert reader.** No onboarding and no explaining the product to itself. Internal names are the vocabulary. Density is fine; ambiguity is not.
4. **Fewer, merged screens.** The owner asked for simplicity: overlapping screens are merged (the November 2026 reorganisation took the nav from 22 screens to 18), and health is one stop for an all-clear.
5. **The agent composes, the owner decides.** In the companion, Nous chooses what to show and recommends an action. The owner's choice is recorded against the decision it answers.
