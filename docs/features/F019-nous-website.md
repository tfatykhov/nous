# F019 — Website & Brand Architecture (v3 — Brand Restructure)

> **Status:** Planned
> **Priority:** P2
> **Depends on:** ~~F018 (Agent Identity)~~ — removed as hard dependency (see §Dependency Note)
> **Estimated effort:** ~2–3 days design + ~3–4 days build (Phase 1)
> **Last updated:** March 9, 2026

## Changelog (v3)

- 🏗️ **Major: Three-tier brand architecture adopted** — Cognition Engines (company) → Nous (OSS) → Verdicto (enterprise)
- 🌐 Domain reassigned: `mem-brain.ai` → redirect to `cognition-engines.ai`; primary enterprise site is now `verdicto.ai`
- 🏷️ Tagline finalized: _"Verdicto — Enterprise AI that remembers, decides, and learns. Built on Nous. Powered by Cognition Engines."_
- 📄 `/cognition-engines` bridge page redesigned for three-tier brand hierarchy
- 🎯 Positioning updated to reflect enterprise (Verdicto) vs open-source (Nous) split
- 📋 Domain mapping table added
- 🔁 Supersedes v2 decision to use `mem-brain.ai` as primary domain

## Changelog (v2)

- ❌ Removed hard F018 dependency — messaging already exists in this spec
- 🎨 Added full Design System section (colors, typography, components, responsive, a11y)
- 🔀 Reordered homepage: Quick Start moves to position 3, Concepts Grid moved to /concepts
- 📊 Flagged stale stats — added build-time stats pipeline requirement
- 📈 Moved analytics from Phase 3 → Phase 1
- 🏠 Resolved domain to `mem-brain.ai` (was conflicting with INDEX.md) — **superseded in v3**
- 🚀 Added "What to Build Next" adoption section
- 📱 Added responsive design and accessibility specs
- 🔍 Added SEO implementation details
- ⏱️ Updated effort estimates to be realistic
- 📄 Added 404 page spec
- 🗂️ Specified repo location

---

## Brand Architecture

### Three-Tier Hierarchy

```
Cognition Engines          ← The company / umbrella brand
├── Nous                   ← Open-source cognitive agent framework
│   └── Membrain           ← Neuromorphic memory substrate (sub-project)
└── Verdicto               ← Enterprise product
```

**Tagline:**
> _Verdicto — Enterprise AI that remembers, decides, and learns._
> _Built on Nous. Powered by Cognition Engines._

### Domain Mapping

| Domain | Role | Traffic | Action |
|--------|------|---------|--------|
| `cognition-engines.ai` | Company site, blog, research, articles | 2.21k visits | **Primary brand — keep building** |
| `verdicto.ai` | Enterprise product site, docs, API, pricing | 0 visits | **Build as enterprise product home** |
| `nous-agent.ai` | OSS project | 0 visits | **Redirect to GitHub repo or OSS docs** |
| `mem-brain.ai` | Legacy | 0 visits | **Redirect to cognition-engines.ai** |
| `decision-memory.ai` | Reserved | 522 visits | **Parked — potential content play** |

### Brand Positioning

**Cognition Engines** — The vision, the research, the thought leadership.
- Home for Tim's LinkedIn article series
- Company identity for external communications
- Audience: industry peers, CTOs, AI leaders

**Nous** — The open-source framework. Free, Apache 2.0, community-driven.
- Audience: developers, researchers, agent builders
- Lives on GitHub (`tfatykhov/membrain`)
- The nerdy Greek name works perfectly for the OSS crowd

**Verdicto** — The enterprise product. Managed, supported, compliance-ready.
- Audience: enterprise teams, VPs of Engineering, procurement departments
- Latin root: "verdictum" = a true saying / a judgment
- Pronounceable, memorable, unique in search, communicates value instantly
- Features beyond OSS: audit trails, compliance, SSO, managed memory, SLA

### Pattern Reference

This follows the proven open-source-to-enterprise playbook:
- **Elastic** → Elasticsearch (OSS) / Elastic Cloud (enterprise)
- **HashiCorp** → Terraform (OSS) / Terraform Cloud (enterprise)
- **Databricks** → Apache Spark (OSS) / Databricks (enterprise)
- **Grafana Labs** → Grafana (OSS) / Grafana Cloud (enterprise)
- **Neo4j** → Neo4j Community (OSS) / Neo4j Aura (enterprise)

---

## Problem

Nous is a serious open-source framework with a genuinely novel foundation — Minsky's Society of Mind as a first-class architecture. But its only public front door right now is a GitHub README.

A README serves people who already found you. A website converts curious passers-by into actual adopters. For an open-source framework targeting developer adoption, that gap matters enormously.

With the Verdicto enterprise brand, we now also need a distinct enterprise presence that speaks to decision-makers, not just developers.

The risk of waiting: other frameworks (LangChain, CrewAI, AutoGen) continue to dominate discovery simply because they have polished public presence — not because they're architecturally stronger.

## Goal

**Two websites, one ecosystem:**

1. **cognition-engines.ai** — Company site with blog, research, and brand story. Links to both Nous and Verdicto.
2. **verdicto.ai** — Enterprise product site: what it does, how it's different, how to evaluate/buy it.

The Nous OSS project lives on GitHub with a solid README + docs. `nous-agent.ai` redirects there.

## Audience

**Nous (OSS):**
- Software engineers building AI agents who are frustrated with the shallowness of current frameworks (LangChain-style prompt plumbing). They want something architecturally honest.

**Verdicto (Enterprise):**
- Technical leads and CTOs evaluating agent platforms for enterprise deployment. They need to understand the "why" quickly, trust the architecture, see compliance/security story, and bring it to procurement.

**Cognition Engines (Company):**
- Industry peers, AI practitioners, potential partners. Following Tim's thought leadership and research.

---

## Dependency Note

**F018 (Agent Identity) is NOT a blocker for Phase 1.**

F018 is a runtime architecture feature — it replaces the static `NOUS_IDENTITY_PROMPT` with a DB-backed identity system. The website needs public-facing messaging and positioning, which already exists in this spec (hero section, value props, content principles).

**Resolution:** Messaging and positioning should be finalized before launch. F018's structured identity work may inform future updates, but is not a prerequisite.

---

## Site Architecture

### cognition-engines.ai

```
/                   → Company homepage (vision + links to Nous & Verdicto)
/blog               → Articles (LinkedIn cross-posts + originals)
/research           → Research summaries, paper reviews
/about              → Tim's background, company story
```

### verdicto.ai

```
/                   → Product homepage (hero + value props + how it works + CTA)
/features           → Enterprise features breakdown
/architecture       → Technical deep dive (Minsky foundation, memory layers, decision intelligence)
/docs               → Enterprise documentation
/pricing            → Pricing/contact (Phase 2)
/concepts           → Deep dives: K-Lines, Censors, Calibration, Frames, B-Brain
/concepts/{name}    → Individual concept pages
/404                → Custom 404
```

### nous-agent.ai
```
/  → Redirect to github.com/tfatykhov/membrain
```

### mem-brain.ai
```
/  → Redirect to cognition-engines.ai
```

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

**Note:** Verdicto may adopt a slightly warmer/more professional variant of this palette for enterprise positioning. Design exploration needed in Phase 1.

### Typography

| Role | Font | Weight | Size |
|------|------|--------|------|
| Headings | `Space Grotesk` (Google Fonts) | 700 | 2.5rem / 2rem / 1.5rem |
| Body | `Inter` (Google Fonts) | 400/500 | 1rem (16px) |
| Code / mono | `JetBrains Mono` (Google Fonts) | 400 | 0.875rem |
| Hero tagline | `Space Grotesk` | 700 | 3.5rem (desktop) / 2rem (mobile) |

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

### verdicto.ai Homepage

**Conversion funnel:** Hook → Prove It Works → Differentiate → Try It → Go Deeper

#### Section 1: Hero

```
┌─────────────────────────────────────────────────────┐
│                                                     │
│   Verdicto                                          │
│                                                     │
│   Enterprise AI that remembers, decides, and learns.│
│                                                     │
│   Built on 40 years of cognitive science. Verdicto  │
│   gives your AI agents structured memory, decision  │
│   intelligence, and self-monitoring — so they get   │
│   smarter with every interaction.                   │
│                                                     │
│   [Request Demo →]       [View Architecture ↗]      │
│                                                     │
│   Built on Nous · Powered by Cognition Engines      │
└─────────────────────────────────────────────────────┘
```

---

#### Section 2: "Why Verdicto?" (3 columns)

**Column 1: Structured Memory**
> Current agents store text and hope for the best. Verdicto gives agents four memory layers — from fast working memory to persistent decision intelligence — so they actually learn from experience.

**Column 2: Decision Intelligence**
> Every significant decision is recorded with reasoning, confidence, and outcome. Your agent tracks its own calibration. When it faces the same situation again, it knows what worked.

**Column 3: Grounded in Cognitive Science**
> Every architectural choice traces back to Minsky's *Society of Mind*. Not metaphors — actual implemented concepts: structured recall, guardrails, cognitive frames, and self-monitoring.¹
>
> ¹ [Deep dive into the concepts →](/concepts)

---

#### Section 3: Enterprise Value Props

**Column 1: Cost Reduction**
> After 10 conversations per user, Verdicto's memory architecture costs 26% less than long-context alternatives. The savings compound with every interaction.

**Column 2: Compliance & Audit**
> Full decision audit trail. Every judgment your agent makes is traceable — reasoning, confidence, outcome. Built for regulated industries.

**Column 3: Your Infrastructure**
> Runs entirely in your environment. No data leaves your perimeter. No vendor lock-in. Built on open-source Nous, so you can inspect every line.

---

#### Section 4: The Cognitive Loop (simplified)

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

---

#### Section 5: Open Source Foundation

```
┌─────────────────────────────────────────────────────┐
│   Built on Nous — the open-source cognitive agent   │
│   framework with {lines} lines of Python,           │
│   {tests} tests, and an Apache 2.0 license.         │
│                                                     │
│   [View on GitHub →]    [Read the Research →]        │
│                                                     │
│   Verdicto adds enterprise features on top:         │
│   managed deployment, SLA, SSO, audit trails,       │
│   compliance tooling, and dedicated support.         │
└─────────────────────────────────────────────────────┘
```

---

#### Section 6: CTA

```
┌─────────────────────────────────────────────────────┐
│   Ready to give your AI agents real memory?         │
│                                                     │
│   [Request Demo →]    [Read the Docs →]             │
│                                                     │
│   Or start with the open-source framework:          │
│   git clone https://github.com/tfatykhov/membrain  │
└─────────────────────────────────────────────────────┘
```

---

#### Section 7: Footer

- GitHub link + star button (Nous OSS)
- Cognition Engines link
- Apache 2.0 license note (for Nous OSS core)
- Enterprise contact / support link
- *"Built on Minsky and too much coffee ☕"*

---

### cognition-engines.ai Homepage

```
┌─────────────────────────────────────────────────────┐
│   Cognition Engines                                 │
│                                                     │
│   Building AI that thinks, not just predicts.       │
│                                                     │
│   We research and build cognitive architectures     │
│   for AI agents — grounded in Minsky's Society of   │
│   Mind and validated against the latest research.   │
│                                                     │
│   ┌──────────────┐    ┌──────────────┐              │
│   │ Nous (OSS)   │    │ Verdicto     │              │
│   │ The framework│    │ Enterprise   │              │
│   │ [GitHub →]   │    │ [Learn More]│              │
│   └──────────────┘    └──────────────┘              │
│                                                     │
│   Latest Articles                                   │
│   ────────────────                                  │
│   • Your AI Agent Has Amnesia...                    │
│   • Stop Renting AI...                              │
│   • The Silent Risk...                              │
│   • AI agents make hundreds of decisions...         │
└─────────────────────────────────────────────────────┘
```

---

### /concepts Pages (on verdicto.ai)

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

### /concepts/{name} Pages

One page per core concept. Template:

```
# Structured Recall (K-Lines)

> From Minsky's Society of Mind, Chapter 8

## The Idea
[1-2 para plain English — what problem this solves]

## Minsky's Original Insight
[Quote + brief context from the book]

## How Verdicto Implements It
[Code snippet or diagram showing the actual implementation]

## Why It Matters for Enterprise Agents
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

### /404 Page

```
# Wrong K-Line Activated

Looks like your memory recalled a page that doesn't exist.

[← Go Home]  [Search Docs]  [GitHub ↗]
```

On-brand, helpful, not annoying.

---

## Build-Time Stats Pipeline

Stats displayed on the site must be current at deploy time. Stale stats undermine credibility.

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

## SEO Implementation

### Technical SEO
- `sitemap.xml` — auto-generated by `@astrojs/sitemap`
- `robots.txt` — allow all, point to sitemap
- Canonical URLs on all pages
- Structured data: `SoftwareApplication` JSON-LD on homepage
- Open Graph + Twitter Card meta on all pages

### OG Image
- Default: 1200×630, dark background, Verdicto logo + tagline
- Per-concept pages: concept name + one-line description
- Generated at build time via `@vercel/og` or Satori

### Target Keywords

**verdicto.ai:**
- Primary: "enterprise AI agent", "AI agent memory", "AI decision intelligence"
- Secondary: "agent memory architecture", "cognitive AI platform", "enterprise AI compliance"
- Long-tail: "AI agent that learns from decisions", "alternative to LangChain enterprise", "Minsky Society of Mind AI"

**cognition-engines.ai:**
- Primary: "cognitive AI research", "AI agent architecture", "Society of Mind framework"
- Secondary: "AI agent memory research", "LLM agent cognition"

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
- Domains: `verdicto.ai` and `cognition-engines.ai` — HTTPS via Vercel automatically
- Preview deploys on PRs for content review

### Assets Needed

- [ ] Verdicto logo and wordmark
- [ ] Cognition Engines logo and wordmark
- [ ] Architecture diagram — render existing mermaid as SVG (build-time or manual export)
- [ ] Memory layers diagram — render existing mermaid as SVG
- [ ] Loop diagram — custom SVG of the 7-step cycle (circular layout preferred)
- [ ] Minsky portrait or book cover — **check MIT Press licensing first; have a Plan B** (abstract geometric alternative)
- [ ] OG image templates (1200×630) for both sites
- [ ] Favicons for both sites (SVG preferred for crisp rendering at all sizes)

---

## Content Principles

1. **Show, don't tell** — code snippets over adjectives
2. **Credit the source** — Minsky gets cited, not vaguely referenced
3. **Capability first, jargon second** — Lead with what it does ("structured recall"), follow with what it's called ("K-Lines"). On product pages, capabilities only. On /concepts, full Minsky terminology.
4. **Honest about status** — actively built, enterprise-ready features clearly distinguished from roadmap
5. **Two voices:**
   - **Nous (OSS):** Developer voice — "Nous gives agents structured memory" not "unlock the power of next-generation AI"
   - **Verdicto (Enterprise):** Professional but still technical — "Verdicto reduces inference costs 26% after 10 user sessions" not "leverage synergies"
6. **Short paragraphs** — developers and executives both skim. Every paragraph should survive being skipped.

---

## Launch Sequence

### Phase 0.5 — Landing Pages (~1 day)
- [ ] `verdicto.ai`: Hero + tagline + "Coming Soon" + email capture
- [ ] `cognition-engines.ai`: Hero + article links + Nous/Verdicto cards
- [ ] Domain redirects: `mem-brain.ai` → CE, `nous-agent.ai` → GitHub
- [ ] All domains live with HTTPS

### Phase 1 — MVP (~2–3 days build per site)
- [ ] `verdicto.ai` full homepage (all 7 sections per this spec)
- [ ] `cognition-engines.ai` full homepage + blog (article cross-posts)
- [ ] Design system implemented (colors, typography, components)
- [ ] Build-time stats pipeline
- [ ] Analytics installed (Plausible — privacy-respecting, 5-minute setup)
- [ ] SEO basics (sitemap, robots.txt, OG images, JSON-LD)
- [ ] 404 pages for both sites

### Phase 2 — Content Complete (~3–4 days)
- [ ] `/concepts` landing page + all 7 concept deep-dive pages (on verdicto.ai)
- [ ] `/docs` — enterprise documentation
- [ ] `/architecture` — technical deep dive
- [ ] Per-page OG images
- [ ] Launch blog post on cognition-engines.ai
- [ ] Playground or hosted demo exploration

### Phase 3 — Growth
- [ ] `/blog` on cognition-engines.ai — ongoing posts, Minsky deep dives, build log
- [ ] `/pricing` on verdicto.ai — pricing model, contact form
- [ ] Community growth (GitHub Discussions → Discord if needed)
- [ ] Video: 90-second demo (agent running, showing memory, decisions, calibration)
- [ ] "Coming from LangChain/CrewAI" migration guide
- [ ] Newsletter signup (if warranted by traffic)
- [ ] Case studies / testimonials

---

## Effort Estimates (Revised)

| Phase | Design | Build | Content | Total |
|-------|--------|-------|---------|-------|
| Phase 0.5 | 2h | 4h | 1h | ~1 day |
| Phase 1 | 6–8h | 20–24h | 4–6h | ~4–5 days |
| Phase 2 | 2–3h | 8–12h | 8–10h | ~3–4 days |
| Phase 3 | Ongoing | Ongoing | Ongoing | Ongoing |

**Note:** Effort increased from v2 due to now building two sites instead of one. Shared design system mitigates some duplication. Phase 0.5 landing pages can be live within a day.

---

## Success Metrics

**Nous (OSS):**
- GitHub stars — primary signal for developer interest
- Inbound issues/PRs — quality signal (are people actually using it?)

**Verdicto (Enterprise):**
- Demo requests — primary conversion metric
- Time on site — proxy for content quality (target: > 2 min average)
- Search visibility — track rankings for target keywords monthly

**Cognition Engines (Brand):**
- Article engagement — LinkedIn reactions, comments, shares
- Referral sources — which channels (HN, dev.to, Twitter, LinkedIn, organic) drive awareness?
- Cross-site navigation — are CE readers clicking through to Verdicto?

---

## Resolved Questions ✅

1. **Brand architecture** → Three-tier: Cognition Engines (company) → Nous (OSS) → Verdicto (enterprise). Decision recorded March 9, 2026.
2. **Domain mapping** → `cognition-engines.ai` (company), `verdicto.ai` (enterprise product), `nous-agent.ai` (redirect to GitHub), `mem-brain.ai` (redirect to CE), `decision-memory.ai` (parked)
3. **F018 dependency** → Removed as hard blocker (messaging exists in this spec)
4. **Repo location** → In-repo at `/website/`, decoupled deploys via Vercel path triggers
5. **Analytics timing** → Phase 1 (Plausible, 5-minute setup, don't lose early data)
6. **Community from day one** → GitHub Discussions in Phase 1, Discord evaluated in Phase 3
7. **Tagline** → _"Verdicto — Enterprise AI that remembers, decides, and learns. Built on Nous. Powered by Cognition Engines."_

## Open Questions (Remaining)

1. **Verdicto visual identity** — Does it share the Nous dark theme or get a slightly warmer/more professional variant? Needs design exploration.
2. **Docs hosting** — Embedded in site (MDX) or link to GitHub? Embedded is better UX but more maintenance. Decision needed before Phase 2.
3. **Video** — 90-second demo could be very effective for launch. Worth the effort before Phase 2?
4. **Minsky portrait licensing** — MIT Press permission needed. If denied, Plan B: abstract geometric art inspired by Society of Mind diagrams.
5. **Playground/sandbox** — Can we offer a hosted demo without requiring the user's API key? Options: pre-recorded demo, shared sandbox with rate limiting, or WebContainer-based local runtime. Research spike in Phase 2.
6. **Verdicto pricing model** — Freemium? Per-seat? Usage-based? Deferred to Phase 3 but needs research before then.
7. **Logo design** — Need logos for both Verdicto and Cognition Engines. Budget for professional design or DIY?
