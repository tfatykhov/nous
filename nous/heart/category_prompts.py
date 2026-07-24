"""Shared Tier-1 category definitions for all fact-extraction prompts.

The preference/person/rule categories feed the always-on "User Profile"
section of EVERY system prompt (and the dashboard identity view). A
mislabeled session event pollutes every future conversation — writers must
embed TIER1_CATEGORY_GUIDANCE verbatim rather than paraphrasing it
(three hand-copied variants had already drifted by 2026-07-24)."""

TIER1_CATEGORY_GUIDANCE = """\
Category definitions for user-profile categories (these are injected into
EVERY future conversation as the user's standing profile — label carefully):
- "person": durable identity facts about the user that stay true across
  sessions (name, contacts, location, family, health, background,
  working style). NOT one-time events involving the user.
- "preference": stable likes/dislikes and standing choices (formats,
  tools, communication style, units). NOT one-time requests.
- "rule": explicit standing directives the user stated ("always X",
  "never Y"). NOT lessons, observations, or project conventions.
NEVER use person/preference/rule for: session events or actions ("the
user requested...", "X was sent to..."), dated one-offs (trips, flights,
forecasts, meetings), document/article/dataset contents, engineering
lessons, or system observations. Use "technical" or "concept" (or
"event"/"status" where offered) for those instead. If in doubt, do NOT
use a user-profile category."""
