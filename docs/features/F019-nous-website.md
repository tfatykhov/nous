# F019 — Nous Website (v2 — Review-Updated)

> **Status:** Planned
> **Priority:** P2
> **Depends on:** ~~F018 (Agent Identity)~~ — removed as hard dependency (see §Dependency Note)
> **Estimated effort:** ~2–3 days design + ~3–4 days build (Phase 1) — see §Effort Estimates
> **Domain:** `mem-brain.ai` *(resolved — matches INDEX.md)*

## Changelog (v2)

- ❌ Removed hard F018 dependency — messaging already exists in this spec
- 🎨 Added full Design System section (colors, typography, components, responsive, a11y)
- 🔀 Reordered homepage: Quick Start moves to position 3, Concepts Grid moved to /concepts
- 📊 Flagged stale stats — added build-time stats pipeline requirement
- 📈 Moved analytics from Phase 3 → Phase 1
- 🏠 Resolved domain to `mem-brain.ai` (was conflicting with INDEX.md)
- 🚀 Added "What to Build Next" adoption section
- 📱 Added responsive design and accessibility specs
- 🔍 Added SEO implementation details
- ⏱️ Updated effort estimates to be realistic
- 📄 Added 404 page spec
- 🗂️ Specified repo location

---

## Problem

Nous is a serious open-source framework with a genuinely novel foundation — Minsky's Society of Mind as a first-class architecture. But its only public front door right now is a GitHub README.

A README serves people who already found you. A website converts curious passers-by into actual adopters. For an open-source framework targeting developer adoption, that gap matters enormously.

The risk of waiting: other frameworks (LangChain, CrewAI, AutoGen) continue to dominate discovery simply because they have polished public presence — not because they're architecturally stronger.

## Goal

A fast, minimal, developer-first website that:

1. Communicates what Nous is in under 10 seconds
2. Earns credibility with technical developers through honest depth
3. Provides a clear path from curiosity → running agent → building with it
4. Cross-links with `cognition-engines.ai` as the companion decision layer

## Audience

**Primary:** Software engineers building AI agents who are frustrated with the shallowness of current frameworks (LangChain-style prompt plumbing). They want something architecturally honest.

**Secondary:** Technical leads and CTOs evaluating frameworks for team adoption. They need to understand the "why" quickly, trust the architecture, and hand it off to their devs.

## Positioning

**One-liner:**
> *"An AI agent framework that thinks, learns, and grows — grounded in Minsky's Society of Mind."*

**What Nous is NOT:**
- Not another prompt-chaining library
- Not a thin wrapper around OpenAI
- Not a cloud service you pay for

**What Nous IS:**
- An opinionated open-source framework
- Grounded in 40 years of cognitive science (Minsky, 1986)
- Embeds decision memory, structured recall, self-monitoring, and calibration as first-class concepts
- Runs entirely in your infrastructure

---

## Dependency Note

**F018 (Agent Identity) is NOT a blocker for Phase 1.**

F018 is a runtime architecture feature — it replaces the static `NOUS_IDENTITY_PROMPT` with a DB-backed identity system. The website needs public-facing messaging and positioning, which already exists in this spec (hero section, value props, content principles).

**Resolution:** Messaging and positioning should be finalized before launch. F018's structured identity work may inform future updates, but is not a prerequisite.

---

## Site Architecture

```
/                   → Homepage (hero + quick start + why + architecture + CTA)
/docs               → Documentation (mirrors /docs in repo)
/concepts           → Deep dives: K-Lines, Censors, Calibration, Frames, B-Brain
/concepts/{name}    → Individual concept pages
/blog               → Posts (Phase 2)
/cognition-engines  → Bridge page — relationship to cognition-engines.ai
/404                → Custom 404 with search + navigation help
```

**Repo location:** Website lives in the Nous monorepo at `/website/`. Rationale: keeps content co-located with code, single PR workflow, and build-time stats can read from the same repo. Deployment is decoupled via Vercel's path-based build triggers.

---

## Design System

### Color Palette

| Role | Color | Usage |
|------|-------|-------|
| Background | `#0A0A0F` (near-black) | Page background |
| Surface | `#14141F` | Cards, code blocks, sections |
| Surface raised | `#1E1E2E` | Hover states, active elements |
| Text primary | `#E8E8ED` | Body text, headings |
| Text secondary | `#9898A6` | Captions, labels, timestamps |
| Accent primary | `#6C63FF` (violet) | CTAs, links, active states |
| Accent secondary | `#4ECDC4` (teal) | Success states, secondary highlights |
| Warning | `#FFB347` | Alerts, status indicators |
| Border | `#2A2A3A` | Dividers, card borders |

**Rationale:** Dark theme signals "developer tool." Violet accent differentiates from the sea of blue developer sites. Teal secondary provides warmth without competing.

### Typography

| Role | Font | Weight | Size |
|------|------|--------|------|
| Headings | `Space Groto` (Google Fonts) | 700 | 2.5rem / 2rem / 1.5rem |
| Body | `Inter` (Google Fonts) | 400/500 | 1rem (16px) |
| Code / mono | `JetBrains Mono` (Google Fonts) | 400 | 0.875rem |
| Hero tagline | `Space Groto` | 700 | 3.5rem (desktop) / 2rem (mobile) |

**Rationale:** Space Grotesk conveys technical precision with personality. Inter is the developer standard — familiar and highly readable. JetBrains Mono for code blocks signals seriousness to devs.

### Component Patterns

- **Buttons:** Rounded corners (6px), accent-primary fill for primary CTA, ghost/outline for secondary. Min height 44px for touch targets.
- **Cards:** Surface background, 1px border, 8px radius, 24px padding. Subtle hover lift (translateY -2px + shadow).
- **Code blocks:** Surface background, left accent border (2px violet), syntax highlighting via Shiki (bundled with Astro). Copy button top-right.
- **Navigation:** Sticky top bar, transparent on hero, solid surface on scroll. Logo left, nav links center, GitHub star button right.
- **Section spacing:** 96px between major sections (desktop), 64px on mobile.

### Responsive Breakpoints

| Breakpoint | Width | Behavior |
|-----------|-------|----------|
| Mobile | < 640px | Single column, hamburger nav, reduced section spacing |
| Tablet | 640–1024px | Two-column grids, abbreviated stats bar |
| Desktop | > 1024px | Full layout, three-column grids, all sections visible |
| Max width | 1280px | Content container max-width, centered |

### Accessibility

- **WCAG 2.1 AA** target
- All images require alt text (diagrams get descriptive alt, decorative images get `alt=""`)
- Color contrast minimum 4.5:1 for body text, 3:1 for large text
- Keyboard navigation: all interactive elements focusable, visible focus rings
- Skip-to-content link
- Reduced motion: respect `prefers-reduced-motion` — disable hover animations, diagram transitions
- Semantic HTML: proper heading hierarchy, landmark regions, ARIA labels on nav

### Performance Budget

- Lighthouse: ≥ 95 on all categories (Performance, Accessibility, Best Practices, SEO)
- First Contentful Paint: < 1.0s
- Total bundle: < 100KB (JS), < 50KB (CSS)
- Images: WebP/AVIF with fallbacks, lazy-loaded below fold

---

## Page Designs

### 1. Homepage

**Conversion funnel:** Hook → Prove It Works → Try It → Understand Why → Go Deeper

#### Section 1: Hero

```
┌─────────────────────────────────────────────────────┐
│                                                     │
│   [Nous project image — "Minds from Mindless Stuff"]│
│                                                     │
│   AI agents that think, learn, and grow.            │
│                                                     │
│   Nous is an open-source agent framework grounded   │
│   in Minsky's Society of Mind — with structured     │
│   memory, decision intelligence, and self-monitoring│
│   built in from the start.                         │
│                                                     │
│   [Get Started →]          [GitHub ↗]               │
│                                                     │
│   v0.x.x · Apache 2.0 · {lines} lines · {tests} tests │
└─────────────────────────────────────────────────────┘
```

**Changes from v1:**
- Stats are now dynamic — pulled at build time (see §Build-Time Stats)
- Terminal animation or short code snippet embedded in hero background (subtle, non-distracting)

---

#### Section 2: "Why Nous?" (3 columns — tightened)

**Column 1: Structured Memory**
> Current agents store text and hope for the best. Nous gives agents four memory layers — from fast working memory to persistent decision intelligence — so they actually learn from experience.

**Column 2: Decision Intelligence**
> Every significant decision is recorded with reasoning, confidence, and outcome. Your agent tracks its own calibration. When it faces the same situation again, it knows what worked.

**Column 3: Grounded in Cognitive Science**
> Every architectural choice traces back to Minsky's *Society of Mind*. Not metaphors — actual implemented concepts: structured recall, guardrails, cognitive frames, and self-monitoring.¹
>
> ¹ [Deep dive into the concepts →](/concepts)

**Changes from v1:**
- Removed insider jargon (K-Lines, B-Brains, chapter numbers) from homepage
- Capability-first language — what it *does*, not what it's *called*
- Minsky citation kept but as footnote link to /concepts, not inline

---

#### Section 3: Quick Start ⬆️ MOVED UP

Show the minimum viable code to get a Nous agent running.

```bash
# Clone and start (requires Docker)
git clone https://github.com/tfatykhov/nous
cd nous
cp .env.example .env  # add your ANTHROPIC_API_KEY
docker compose up
```

```bash
# Talk to your agent
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-Nous-Owner-Secret: $NOUS_OWNER_ROUTE_SECRET" \
  -d '{"message": "Hello — what do you remember about me?"}'
```

> Your agent is now running with structured memory, decision recording, and self-monitoring. Everything it learns persists across sessions.

**Prerequisites callout (honest about requirements):**
> 📋 You'll need: Docker, an Anthropic API key ([get one here](https://console.anthropic.com)), and ~10 minutes.

**[Read the full quickstart docs →]** · **[Try the playground →]** *(Phase 2)*

**Changes from v1:**
- Moved from position 6 → position 3 (critical for conversion)
- Added honest prerequisites callout (Docker, API key, realistic time)
- Added playground link placeholder for Phase 2

---

#### Section 4: The Nous Loop (simplified)

Visual treatment of the 7-step cognitive cycle. Rendered as a clean circular/linear SVG diagram with hover/click to expand.

```
SENSE → FRAME → RECALL → DELIBERATE → ACT → MONITOR → LEARN
```

One-line descriptions:

- **Sense** — Receive input: a message, an event, a timer
- **Frame** — What kind of problem is this? Select the right cognitive lens
- **Recall** — Reconstruct relevant context from structured memory
- **Deliberate** — Check past decisions, consult guardrails, plan the response
- **Act** — Execute. A self-monitoring layer watches the work happen
- **Monitor** — Did the action match the intent? Were there surprises?
- **Learn** — Update memory, calibration, and guardrails for next time

**Changes from v1:**
- Moved from position 3 → position 4 (after Quick Start)
- Removed table format — now a visual diagram with progressive disclosure
- Removed Minsky jargon from labels (B-Brain → "self-monitoring layer")

---

#### Section 5: Stats Bar ⬆️ MOVED UP

```
{lines} lines of Python · {tests} tests · {tables} Postgres tables · {endpoints} REST endpoints · v{version}
```

**All stats are build-time dynamic** — pulled from the repo at deploy. See §Build-Time Stats.

---

#### Section 6: What to Build Next 🆕

> **Got it running? Here's where to go:**
>
> 🔧 **[Build a Custom Agent →]** — Give your agent a unique identity, tools, and knowledge base
> 📖 **[Explore the Concepts →]** — Understand the cognitive architecture under the hood
> 🤝 **[Join the Community →]** — GitHub Discussions for questions, ideas, and show-and-tell
> 🔄 **[Coming from LangChain? →]** — Key differences and migration patterns *(Phase 2)*

**Rationale:** The original spec had no adoption path. Developers who completed Quick Start had nowhere to go. This section bridges "I tried it" → "I'm building with it."

---

#### Section 7: Footer

- GitHub link + star button
- Community link (GitHub Discussions — Phase 1; Discord considered for Phase 2)
- Cognition Engines link (one line: "Nous uses Cognition Engines for decision intelligence")
- Apache 2.0 license note
- *"Built on Minsky and too much coffee ☕"*

**Changes from v1:**
- Cognition Engines moved from its own section → footer link (new visitors don't need the internal project structure)
- Community link added to Phase 1

---

### 2. /concepts Landing Page

Grid of cards, one per core Minsky concept. Each card shows:
- Concept name (capability-first label)
- One-sentence description
- Link to deep-dive page

Cards:
- **Structured Recall (K-Lines)** — Context bundles that reconstruct relevant mental state
- **Guardrails (Censors)** — Safety constraints that block before harm happens
- **Cognitive Frames** — Active interpretation lens that shapes how the agent thinks
- **Self-Monitoring (B-Brains)** — A layer that watches the agent work and catches mistakes
- **Calibration** — Confidence tracking with Brier scores — does the agent know what it knows?
- **Parallel Reasoning (Bundles)** — Multiple reasons are stronger than one chain of thought
- **Papert's Principle** — Understanding by debugging — how agents learn from their own failures

**Changes from v1:**
- Moved from homepage → dedicated /concepts page (unclutters conversion funnel)
- Added capability-first labels alongside Minsky terms
- Chapter numbers removed from cards (available on deep-dive pages)

### 3. /concepts/{name} Pages

One page per core concept. Template:

```
# Structured Recall (K-Lines)

> From Minsky's Society of Mind, Chapter 8

## The Idea
[1-2 para plain English — what problem this solves]

## Minsky's Original Insight
[Quote + brief context from the book]

## How Nous Implements It
[Code snippet or diagram showing the actual implementation]

## Why It Matters for Agents
[Practical consequence — what breaks without this, what it enables]

## Try It
[Interactive example or curl command demonstrating this concept]
```

Pages to build:
- `/concepts/k-lines` — Structured Recall
- `/concepts/censors` — Guardrails
- `/concepts/frames` — Cognitive Frames
- `/concepts/b-brains` — Self-Monitoring
- `/concepts/calibration` — Calibration
- `/concepts/parallel-bundles` — Parallel Reasoning
- `/concepts/paperts-principle` — Papert's Principle

### 4. /cognition-engines Bridge Page

Explains the relationship for developers who find one project and wonder about the other.

Content:
- What Cognition Engines is (standalone decision server, MCP integration)
- What Nous is (full agent framework, embedded Brain)
- How they relate (same philosophy, independent codebases)
- When to use which:
  - **Cognition Engines:** You have an existing agent stack and want to add decision intelligence via MCP
  - **Nous:** You're building a new agent from scratch and want the full cognitive architecture

### 5. /404 Page 🆕

```
# Wrong K-Line Activated

Looks like your memory recalled a page that doesn't exist.

[← Go Home]  [Search Docs]  [GitHub ↗]
```

On-brand, helpful, not annoying.

---

## Build-Time Stats Pipeline 🆕

Stats displayed on the site must be current at deploy time. Stale stats (the v1 spec said 11,800 lines; repo currently has 38,000+) undermine credibility.

**Implementation:**
```javascript
// scripts/build-stats.js — runs at Astro build time
export async function getStats() {
  return {
    lines: execSync("find src -name '*.py' | xargs wc -l | tail -1").toString().trim(),
    tests: execSync("grep -r 'def test_' tests/ | wc -l").toString().trim(),
    tables: execSync("grep 'CREATE TABLE' src/**/migrations/*.sql | wc -l").toString().trim(),
    endpoints: execSync("grep '@router' src/**/*.py | wc -l").toString().trim(),
    version: JSON.parse(readFileSync('package.json')).version
  }
}
```

Stats are injected into the Astro build via a custom integration. No manual updates needed.

---

## SEO Implementation 🆕

### Technical SEO
- `sitemap.xml` — auto-generated by `@astrojs/sitemap`
- `robots.txt` — allow all, point to sitemap
- Canonical URLs on all pages
- Structured data: `SoftwareApplication` JSON-LD on homepage
- Open Graph + Twitter Card meta on all pages

### OG Image
- Default: 1200×630, dark background, Nous logo + tagline
- Per-concept pages: concept name + one-line description
- Generated at build time via `@vercel/og` or Satori

### Target Keywords
- Primary: "AI agent framework", "Society of Mind agent", "Minsky AI framework"
- Secondary: "agent memory architecture", "decision intelligence AI", "cognitive AI agent"
- Long-tail: "alternative to LangChain", "AI agent structured memory", "agent calibration framework"

---

## Technical Spec

### Stack

**Astro + Tailwind** (confirmed)
- Static site generation — fast, cheap to host
- MDX support for concept docs and blog posts
- Shiki for syntax highlighting (built into Astro)
- No client-side JS framework needed (Astro islands if interactive elements needed later)

### Component Framework
- **No UI library** for Phase 1 — custom Tailwind components
- If interactive elements grow in Phase 2+, consider Svelte islands (lightest Astro integration)

### Hosting
- **Vercel** (free tier, instant deploys from GitHub)
- Build trigger: changes to `/website/**` path only
- Domain: `mem-brain.ai` — HTTPS via Vercel automatically
- Preview deploys on PRs for content review

### Assets Needed

- [ ] `docs/nous-project-image.png` — already exists, use in hero
- [ ] Architecture diagram — render existing mermaid as SVG (build-time or manual export)
- [ ] Memory layers diagram — render existing mermaid as SVG
- [ ] Loop diagram — custom SVG of the 7-step cycle (circular layout preferred)
- [ ] Minsky portrait or book cover — **check MIT Press licensing first; have a Plan B** (abstract geometric alternative)
- [ ] OG image template (1200×630)
- [ ] Favicon (SVG preferred for crisp rendering at all sizes)

---

## Content Principles

1. **Show, don't tell** — code snippets over adjectives
2. **Credit the source** — Minsky gets cited, not vaguely referenced
3. **Capability first, jargon second** — Lead with what it does ("structured recall"), follow with what it's called ("K-Lines"). On the homepage, capabilities only. On /concepts, full Minsky terminology.
4. **Honest about status** — v0.x, actively built, not "production ready for everyone"
5. **Developer voice** — no marketing speak. "Nous gives agents structured memory" not "unlock the power of next-generation AI"
6. **Short paragraphs** — developers skim. Every paragraph should survive being skipped.

### Content Authorship 🆕
- **Homepage copy:** Exists in this spec, needs copyedit pass before build
- **7 concept pages:** Tim (or Nous drafts, Tim reviews). ~500 words each. Estimate: 1 day total
- **Launch blog post:** Tim. Should explain the Minsky connection and why this framework exists. Good for HN/dev.to. Estimate: 2–3 hours

---

## Launch Sequence

### Phase 0.5 — Landing Page (optional, ~2–3 hours) 🆕
- [ ] Hero + tagline + GitHub link + stats bar + footer
- [ ] Domain live with HTTPS
- [ ] Gets a URL testable immediately while full Phase 1 is built

### Phase 1 — MVP (~2–3 days build)
- [ ] Full homepage (all 7 sections per this spec)
- [ ] Design system implemented (colors, typography, components)
- [ ] Build-time stats pipeline
- [ ] Analytics installed (Plausible — privacy-respecting, 5-minute setup) ⬆️ MOVED FROM PHASE 3
- [ ] Community link active (GitHub Discussions)
- [ ] SEO basics (sitemap, robots.txt, OG image, JSON-LD)
- [ ] 404 page
- [ ] Domain purchased and DNS configured

### Phase 2 — Content Complete (~3–4 days)
- [ ] `/concepts` landing page + all 7 concept deep-dive pages
- [ ] `/docs` (mirror or integrate GitHub docs via MDX)
- [ ] `/cognition-engines` bridge page
- [ ] Per-page OG images
- [ ] Launch blog post
- [ ] Playground or hosted demo exploration (research spike: can we offer a sandbox without requiring user's API key?)

### Phase 3 — Growth
- [ ] `/blog` — ongoing posts, Minsky deep dives, build log
- [ ] Community growth (Discord if GitHub Discussions outgrows itself)
- [ ] Video: 90-second demo (agent running, showing memory, decisions, calibration)
- [ ] "Coming from LangChain/CrewAI" migration guide
- [ ] Newsletter signup (if warranted by traffic)

---

## Effort Estimates (Revised) 🆕

| Phase | Design | Build | Content | Total |
|-------|--------|-------|---------|-------|
| Phase 0.5 | 0 | 2–3h | 0 | ~3h |
| Phase 1 | 4–6h | 12–16h | 2–3h (copyedit) | ~2–3 days |
| Phase 2 | 2–3h | 8–12h | 8–10h (7 concept pages + blog) | ~3–4 days |
| Phase 3 | Ongoing | Ongoing | Ongoing | Ongoing |

**Note:** Original estimate was "~1 day design + ~2 days build" which significantly underestimated the design system work, responsive implementation, stats pipeline, and SEO setup. Revised estimates include realistic buffer for iteration.

---

## Success Metrics

- **GitHub stars** — primary signal for developer interest
- **Quick Start completion rate** — tracked via analytics (page view: homepage → docs/quickstart)
- **Time on site** — proxy for content quality (target: > 2 min average)
- **Inbound issues/PRs** — quality signal (are people actually using it?)
- **Search visibility** — track rankings for target keywords monthly
- **Referral sources** — which channels (HN, dev.to, Twitter, organic) drive adoption?

---

## Resolved Questions ✅

1. **Domain name** → `mem-brain.ai` (matches INDEX.md, decision recorded)
2. **F018 dependency** → Removed as hard blocker (messaging exists in this spec)
3. **Repo location** → In-repo at `/website/`, decoupled deploys via Vercel path triggers
4. **Analytics timing** → Phase 1 (Plausible, 5-minute setup, don't lose early data)
5. **Community from day one** → GitHub Discussions in Phase 1, Discord evaluated in Phase 3

## Open Questions (Remaining)

1. **Docs hosting** — Embedded in site (MDX) or link to GitHub? Embedded is better UX but more maintenance. Decision needed before Phase 2.
2. **Video** — 90-second demo could be very effective for HN/dev.to launch. Worth the effort before Phase 2 launch?
3. **Should cognition-engines.ai and mem-brain.ai visually relate?** — Shared design language signals ecosystem. But each has different audiences.
4. **Minsky portrait licensing** — MIT Press permission needed. If denied, Plan B: abstract geometric art inspired by Society of Mind diagrams.
5. **Playground/sandbox** — Can we offer a hosted demo without requiring the user's API key? Options: pre-recorded demo, shared sandbox with rate limiting, or WebContainer-based local runtime. Research spike in Phase 2.
6. **Cookie consent / GDPR** — Plausible is cookieless and GDPR-compliant by default. Confirm no consent banner needed.
7. **Internationalization** — Consciously deferred. English only for foreseeable future. Note: keep content in MDX files (not hardcoded) to make future i18n possible.
