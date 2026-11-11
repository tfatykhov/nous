---
version: 1
slug: "dashboard-app"
primary_target: "dashboard-app"
related_targets: ["dashboard-app/companion.html"]
---

# Surface brief: Nous dashboard and companion

## Scope and mode
The dashboard (`dashboard-app/`, 18 screens at `/dashboard/v2/`) and the companion's default theme (`/companion`). Mode: Operate. The companion's other themes (alpine-dusk, harbor, paper, signal, report) and the theme mechanism stay.

## Audience, jobs, constraints
- The owner only.
- Jobs: see what Nous did and why; act on what Nous raised; decide from evidence.
- Owner answers (2026-11-10):
  - the look should be "the agent's mind, visible";
  - dense data screens stay dense;
  - companion themes stay;
  - delivery in staged PRs;
  - propose before implementation.
- Owner redirects after the first prototype (2026-11-10):
  - **No invented symbols or glyphs:** the weather glyphs and fronts were rejected, then four symbol options (orbit, route, bloom, pulse) were dropped as too complicated. Plain words and colour carry the meaning.
  - **Smooth and polished:** rounded corners, rounded cards, pills.
  - **A compact Overview timeline:** it must not dominate the page.
- Light ground: an assumption from the scene (a desk under room light, a phone in daylight). The owner did not object.

## Where the frame treatment goes, and where density holds
- **Frame treatment** (frame colour, plain-language frame changes, replay): the Work trace and Overview.
- **Not the Work list or Heartbeat:** the session list and the heartbeat endpoints return no frame (a session spans many frames, and a heartbeat tick records none), so both are dense screens. This was corrected after the finish review, so that nothing is faked.
- **Dense**: every other screen keeps its tables and charts, re-set in the world's type, palette and rounded cards.

## Direction contract
THESIS: the agent's mind made visible in plain language. Every turn is tinted with the colour of the frame Nous was in, and every frame change is said in one sentence, with why. It refuses the category default: near-black, a violet glow and span trees.

OWN-WORLD:
- **Ground:** chart paper #eef2f3, with white rounded cards (radius 18, soft offset shadow).
- **Rail:** deep Atlantic blue #12304f, with a paper-coloured rounded pill for the current item.
- **Ink:** #14202b, with secondary #56687a.
- **Frame colours, one each:**

  | Frame | Colour |
  |---|---|
  | conversation | #55697a |
  | question | #2346c8 |
  | task | #0b6e7a |
  | decision | #7b3fa0 |
  | debug | #b4531c |
  | creative | #a3266f |
  | initiation | #8f5c0f |

- **Status:** tinted pill badges. The marks are ok #2f8a4e, warn #b7791f and error #d2352b; pill text uses the darker text tones (#23703d, #8a5a0e, #b02a22) on the tints.
- **Type:** Archivo with tabular figures; Red Hat Mono for tool inputs and outputs.
- **Controls:** pill buttons and a round replay handle.

STORY: the owner opens a trace and sees each turn's frame as a tinted column with its name, why it was chosen, its numbers and the critic's verdict. A sentence between turns says where the frame changed and why. Tool calls read as rows, reason first. The owner replays the session or the day with one control, and acts on what Nous raised from the companion.

FIRST VIEWPORT (Work trace):
- The rail on the left.
- The title, then the facts as pills (turns, tool calls, error and gated in tint).
- A replay card.
- The trace card: each turn has a 168px frame column tinted in its frame colour (time, frame name, chosen by, tokens in, seconds, tool calls, critic pill) beside its call rows (lane, reason, tool, badge, duration, result in a soft rounded block). A frame-change sentence sits between turns.
- The "What it used, and why" card on the right.
- Refresh sits top right as an outline pill, after the "updated" note. Filled Atlantic is kept for actions that change something.

FORM: synoptic weather chart was the roll (position 5 of 7, seed:d30dc77e1ef3816a). The owner kept its palette and its replay and removed its notation, so the build is code-led on the owner's redirect. The raises that survive:
- **one plot owns each screen, compact** (Factory Catalog);
- **nothing disappears, it is marked** (Ticket Wallet);
- **one replay control drives the page** (Type Specimen).

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Unresolved
- A dark variant: not asked for.
- Nav regrouping: not part of this direction.
- The dense screens are not prototyped; they get the type, palette and cards in the build.
- The approved prototype is `.impeccable/mocks/prototype/index.html` (views #trace, #overview, #companion). `symbols.html` and `symbols.js` beside it are the rejected symbol options.
- Follow-ups outside this build:
  - the companion's "Decided" state for an answered approval, which needs server work;
  - frame-coloured words in companion text;
  - self-hosting the two typefaces. They load from Google Fonts, as the old faces did; asked on 2026-11-11, the owner chose "Ship with Google Fonts" for this release, with bundling as a follow-up PR;
  - the native Android companion, which keeps its own dark palette.
- Finish review (2026-11-11): the verdict was "ship" after two fix rounds. `nous-dark` renders as a dark card inside the paper shell; the owner looked at it and kept it for now.
