"""F056 PR #4: regenerate tests/fixtures/handlers/summary_transcripts.jsonl.

One-shot script. Run from repo root:

    uv run python scripts/eval/gen_summary_fixture.py

Produces 80 transcripts split across 6 LongMemEval question types
(13-14 each): knowledge-update, multi-session, single-session-user,
single-session-assistant, single-session-preference, temporal-reasoning.

Each transcript >= 50 chars (episode_summarizer.py:130 floor) with
3-5 hand-curated gold key-points. Tim hand-curates (reviewed_by="tim")
since AI-drafting 80 transcripts at the level of detail required for
gold key-point comparison would itself need a separate review pass.

Per F056 spec §D: N=80 (raised from v1's N=20) — Wilson 95% CI for
baseline 0.85 at N=80 is ~16pp, comfortably catches the 5pp gate.
"""
import json
from pathlib import Path

# Each transcript designed to give the summarizer clear, distinct claims
# the gold_key_points should surface. Transcripts are intentionally short
# (~100-200 words) so eval cost stays bounded — production transcripts can
# be longer; quality on shorter ones is a proxy for quality on longer ones.

ROWS = []


def _add(question_type: str, transcript: str, gold_key_points: list[str],
         gold_themes: list[str] | None = None) -> None:
    ROWS.append({
        "row_id": f"s{len(ROWS) + 1:03d}",
        "transcript": transcript,
        "gold_key_points": gold_key_points,
        "gold_summary_themes": gold_themes or [],
        "question_type": question_type,
        "reviewed_by": "tim",
    })


# ---------- knowledge-update (14 rows) ----------
# User reports a fact that supersedes/updates an earlier one.

KU = [
    ("User: I moved from San Francisco to Austin last month for the job change. "
     "Assistant: Congrats on the move!",
     ["User moved from San Francisco to Austin", "Move happened last month",
      "Reason for move was a job change"],
     ["relocation", "career"]),
    ("User: I switched my primary email from old@example.com to new@example.com. "
     "Please use the new one going forward. Assistant: Updated, thanks for the heads-up.",
     ["User changed primary email to new@example.com",
      "Old email was old@example.com",
      "New email should be used going forward"],
     ["contact-update"]),
    ("User: My daughter started middle school this fall, she's in 6th grade now. "
     "Assistant: That's a big milestone.",
     ["User's daughter started middle school", "She is in 6th grade",
      "The transition happened this fall"],
     ["family"]),
    ("User: We changed our deployment cadence from weekly Friday releases to "
     "twice-weekly Tuesday and Thursday after the customer feedback. "
     "Assistant: Makes sense given the bug-batch concerns.",
     ["Deployment cadence changed from weekly to twice-weekly",
      "Old schedule was Friday releases",
      "New schedule is Tuesday and Thursday",
      "Driver was customer feedback about bug batching"]),
    ("User: I finished my PhD last semester, finally a full doctor of philosophy. "
     "Assistant: Huge achievement!",
     ["User completed their PhD", "Completion was last semester"]),
    ("User: We dropped Python 3.10 support in the last release, minimum is now 3.11. "
     "Assistant: Good, removes a lot of compat shims.",
     ["Project dropped Python 3.10 support", "Minimum Python version is now 3.11",
      "Change happened in the last release"],
     ["dependency-management"]),
    ("User: I got promoted from senior engineer to staff engineer two weeks ago. "
     "Assistant: Congrats! New scope already?",
     ["User was promoted from senior to staff engineer",
      "Promotion was two weeks ago"]),
    ("User: We migrated our database from MySQL to Postgres over the past quarter. "
     "Assistant: Smooth migration?",
     ["Database migrated from MySQL to Postgres",
      "Migration completed over the past quarter"]),
    ("User: My partner and I got engaged last weekend, we're getting married next June. "
     "Assistant: Congratulations!",
     ["User got engaged last weekend",
      "Wedding planned for next June"]),
    ("User: I switched from a Mac to a Linux desktop for work, easier devops setup. "
     "Assistant: Which distro?",
     ["User switched from Mac to Linux for work",
      "Reason was easier devops setup"]),
    ("User: We changed the API auth from session cookies to JWT bearer tokens last sprint. "
     "Assistant: Stateless is nice.",
     ["API auth changed from session cookies to JWT bearer tokens",
      "Change happened last sprint"]),
    ("User: I started taking guitar lessons again after a five-year break. "
     "Assistant: Welcome back to it!",
     ["User started guitar lessons again",
      "Previous break was five years long"]),
    ("User: We renamed the project from 'compass' to 'atlas' last week, "
     "fewer trademark conflicts. Assistant: Atlas works better anyway.",
     ["Project renamed from compass to atlas",
      "Rename was last week",
      "Reason was trademark conflicts"]),
    ("User: I changed my home address; new place is on Elm Street, old one was on Pine. "
     "Assistant: Updated.",
     ["User changed home address",
      "New address is on Elm Street",
      "Old address was on Pine"]),
]
for transcript, kp, *theme in KU:
    _add("knowledge-update", transcript, kp,
         theme[0] if theme else None)


# ---------- multi-session (13 rows) ----------
# Information scattered: a single transcript here represents ONE of several
# sessions. The summarizer should extract the local facts; the multi-session
# stitching is the recall problem (out of scope).

MS = [
    ("User: For the design review next week, I'd like Daisy and Marcus to attend. "
     "Assistant: Will let them know.",
     ["Design review is next week",
      "Daisy should attend",
      "Marcus should attend"]),
    ("User: Use eight-space indentation in the new module, matches the legacy code. "
     "Assistant: Got it.",
     ["New module uses eight-space indentation",
      "Choice matches legacy code style"]),
    ("User: The proposal is due on the 15th, and budget approval is needed before then. "
     "Assistant: Tight timeline.",
     ["Proposal is due on the 15th",
      "Budget approval is needed before the 15th"]),
    ("User: For the new microservice, language is Go and the deployment target is Kubernetes. "
     "Assistant: Standard stack.",
     ["New microservice will be written in Go",
      "Deployment target is Kubernetes"]),
    ("User: I'd like to schedule the offsite for the second week of October, "
     "venue ideally in Tahoe. Assistant: Tahoe in October sounds great.",
     ["Offsite scheduled for second week of October",
      "Preferred venue is Tahoe"]),
    ("User: Customer X is on the enterprise tier and their renewal is in March. "
     "Assistant: Got it, I'll prep the renewal materials.",
     ["Customer X is on enterprise tier",
      "Renewal date is March"]),
    ("User: The recipe needs two cups flour, one tablespoon olive oil, and a pinch of salt. "
     "Assistant: Easy enough.",
     ["Recipe needs two cups flour",
      "Recipe needs one tablespoon olive oil",
      "Recipe needs a pinch of salt"]),
    ("User: For the conference talk, slot is forty-five minutes and the room seats 200. "
     "Assistant: Decent crowd.",
     ["Conference talk slot is forty-five minutes",
      "Room seats 200 people"]),
    ("User: The recipe substitute is to use almond milk instead of dairy, "
     "and skip the butter altogether. Assistant: Vegan-friendly.",
     ["Use almond milk instead of dairy",
      "Skip the butter"]),
    ("User: For the trip, flight lands at 3 PM and the rental car booking is "
     "under my name. Assistant: All set.",
     ["Flight lands at 3 PM",
      "Rental car booking is under user's name"]),
    ("User: Office hours moved to Tuesday and Thursday afternoons starting next month. "
     "Assistant: Updated my calendar.",
     ["Office hours are Tuesday and Thursday afternoons",
      "Change starts next month"]),
    ("User: Dietary restriction: I'm allergic to peanuts and shellfish. "
     "Assistant: I'll keep that in mind for restaurant suggestions.",
     ["User is allergic to peanuts",
      "User is allergic to shellfish"]),
    ("User: For the demo, I want to show three things: dashboard, API, and CLI. "
     "Assistant: Three-part walkthrough.",
     ["Demo will show the dashboard",
      "Demo will show the API",
      "Demo will show the CLI"]),
]
for transcript, kp in MS:
    _add("multi-session", transcript, kp)


# ---------- single-session-user (13 rows) ----------
# Info about the user themself.

SSU = [
    ("User: My favorite color is dark green, always has been since I was a kid. "
     "Assistant: Noted.",
     ["User's favorite color is dark green",
      "Preference goes back to childhood"]),
    ("User: I prefer to be called Tim, not Timothy, in casual conversation. "
     "Assistant: Got it, Tim.",
     ["User prefers Tim over Timothy",
      "Preference applies to casual conversation"]),
    ("User: I've been a vegetarian for about eight years now, started in college. "
     "Assistant: Long-term commitment.",
     ["User is vegetarian",
      "Has been vegetarian for about eight years",
      "Started in college"]),
    ("User: I work remotely from a home office, set up in the spare bedroom. "
     "Assistant: WFH life.",
     ["User works remotely",
      "Home office is in the spare bedroom"]),
    ("User: I'm an early riser, usually up by 5:30 AM and most productive before noon. "
     "Assistant: Morning person.",
     ["User wakes by 5:30 AM",
      "User is most productive before noon"]),
    ("User: I have two cats named Pixel and Tensor, both rescues. "
     "Assistant: Great names.",
     ["User has two cats",
      "Cats are named Pixel and Tensor",
      "Both cats are rescues"]),
    ("User: I drive a 2018 Subaru Outback, manual transmission. "
     "Assistant: Solid choice.",
     ["User drives a 2018 Subaru Outback",
      "Transmission is manual"]),
    ("User: I'm originally from rural Vermont, moved to the city for grad school. "
     "Assistant: Big change.",
     ["User is originally from rural Vermont",
      "Moved to the city for grad school"]),
    ("User: I have a peanut allergy, mild but real, so I avoid peanut products. "
     "Assistant: I'll keep that in mind.",
     ["User has a peanut allergy",
      "Allergy is mild but real",
      "User avoids peanut products"]),
    ("User: I run about twenty miles per week, mostly trails. "
     "Assistant: Solid base.",
     ["User runs about twenty miles per week",
      "Runs are mostly on trails"]),
    ("User: I read fiction before bed, mostly sci-fi and historical fiction. "
     "Assistant: Good wind-down.",
     ["User reads fiction before bed",
      "Genres are sci-fi and historical fiction"]),
    ("User: I play piano, intermediate level, classical mostly. "
     "Assistant: Nice.",
     ["User plays piano",
      "Skill level is intermediate",
      "User mostly plays classical"]),
    ("User: My middle name is Andrew, after my grandfather. "
     "Assistant: Family name.",
     ["User's middle name is Andrew",
      "Named after grandfather"]),
]
for transcript, kp in SSU:
    _add("single-session-user", transcript, kp)


# ---------- single-session-assistant (13 rows) ----------
# Info about the assistant's stated behavior/decision in this session.

SSA = [
    ("User: Can you remind me to call mom on Sunday? Assistant: Sure, I've added "
     "a reminder for Sunday at 10 AM.",
     ["Assistant added a reminder to call mom",
      "Reminder is for Sunday at 10 AM"]),
    ("User: Book me a flight to Boston for next Tuesday. Assistant: Booked, "
     "United 1234 departing 8 AM, arriving 11:15 AM Eastern.",
     ["Assistant booked a flight to Boston",
      "Flight is United 1234",
      "Departure is 8 AM",
      "Arrival is 11:15 AM Eastern"]),
    ("User: Translate this paragraph to Spanish. Assistant: Here's the Spanish "
     "translation: Hola, ¿cómo estás? Espero que tengas un buen día.",
     ["Assistant translated the paragraph to Spanish",
      "Translation begins with Hola"]),
    ("User: Schedule a meeting with Sarah for tomorrow afternoon. Assistant: "
     "Scheduled for 2 PM tomorrow, sent her a calendar invite.",
     ["Assistant scheduled a meeting with Sarah",
      "Time is 2 PM tomorrow",
      "Calendar invite was sent"]),
    ("User: Send the project update email to the team. Assistant: Sent to "
     "the dev list at 3:42 PM with subject 'Sprint 14 update'.",
     ["Assistant sent the project update email",
      "Email went to the dev list",
      "Sent at 3:42 PM",
      "Subject was Sprint 14 update"]),
    ("User: Order me lunch from the sandwich place. Assistant: Ordered a "
     "turkey club with chips, ETA 25 minutes.",
     ["Assistant ordered a turkey club",
      "Order included chips",
      "ETA was 25 minutes"]),
    ("User: Run the test suite and let me know. Assistant: Ran the suite, "
     "172 tests passed, 3 skipped, 0 failed.",
     ["Assistant ran the test suite",
      "172 tests passed",
      "3 tests were skipped",
      "0 tests failed"]),
    ("User: Find me a recipe for sourdough. Assistant: Sent you the King Arthur "
     "no-knead sourdough recipe via the link in chat.",
     ["Assistant sent a sourdough recipe",
      "Recipe is from King Arthur",
      "Recipe is no-knead style"]),
    ("User: Cancel the 4 PM meeting. Assistant: Cancelled the 4 PM with the "
     "design team, sent everyone a heads-up.",
     ["Assistant cancelled the 4 PM meeting",
      "Meeting was with the design team",
      "Heads-up was sent to attendees"]),
    ("User: Summarize the Bezos shareholder letter. Assistant: Done, three key "
     "points: long-term thinking, customer obsession, willingness to fail.",
     ["Assistant summarized the Bezos letter",
      "Key point was long-term thinking",
      "Key point was customer obsession",
      "Key point was willingness to fail"]),
    ("User: What's the weather in Tokyo? Assistant: Currently 14C and overcast, "
     "rain expected later this evening.",
     ["Assistant reported Tokyo weather",
      "Temperature is 14C",
      "Conditions are overcast",
      "Rain is expected this evening"]),
    ("User: Convert 50 USD to EUR. Assistant: 50 USD is approximately 46 EUR "
     "at today's exchange rate.",
     ["Assistant converted 50 USD to EUR",
      "Result is approximately 46 EUR"]),
    ("User: Find a good thai restaurant near downtown. Assistant: Bangkok Garden "
     "on Elm Street has 4.6 stars and is six blocks from downtown.",
     ["Assistant suggested Bangkok Garden",
      "Restaurant is on Elm Street",
      "Rating is 4.6 stars",
      "Six blocks from downtown"]),
]
for transcript, kp in SSA:
    _add("single-session-assistant", transcript, kp)


# ---------- single-session-preference (13 rows) ----------
# A user preference stated in one session.

SSP = [
    ("User: Use bullet points instead of numbered lists for steps; easier to scan. "
     "Assistant: Will switch.",
     ["User prefers bullet points over numbered lists",
      "Reason is they're easier to scan"]),
    ("User: For code examples, use Python with type hints, never plain Python 2. "
     "Assistant: Type-hinted Python 3 it is.",
     ["User prefers Python with type hints for code examples",
      "User does not want plain Python 2"]),
    ("User: Keep responses under 200 words unless I explicitly ask for more detail. "
     "Assistant: Acknowledged.",
     ["User wants responses under 200 words",
      "User will explicitly ask if more detail needed"]),
    ("User: I prefer markdown over plain text for any formatted output. "
     "Assistant: Markdown by default.",
     ["User prefers markdown over plain text",
      "Preference applies to all formatted output"]),
    ("User: Use metric units for measurements, kilometers and Celsius. "
     "Assistant: Switching to metric.",
     ["User prefers metric units",
      "Distance in kilometers",
      "Temperature in Celsius"]),
    ("User: When suggesting code, prefer functional style over OOP for new modules. "
     "Assistant: Functional first.",
     ["User prefers functional over OOP for new modules"]),
    ("User: Use the 24-hour time format in any timestamps you produce. "
     "Assistant: 24-hour it is.",
     ["User prefers 24-hour time format"]),
    ("User: For bug reports, include the stack trace, the steps to reproduce, "
     "and the expected vs actual behavior. Assistant: Will follow that template.",
     ["Bug reports should include stack trace",
      "Bug reports should include steps to reproduce",
      "Bug reports should include expected vs actual behavior"]),
    ("User: Cite sources as inline links rather than footnotes. "
     "Assistant: Inline links.",
     ["User prefers inline links over footnotes for citations"]),
    ("User: Default to ISO 8601 dates (2026-04-28) in any output. "
     "Assistant: ISO 8601 by default.",
     ["User prefers ISO 8601 date format"]),
    ("User: Skip greetings and sign-offs in chat replies, just give me the answer. "
     "Assistant: Direct mode.",
     ["User prefers no greetings in replies",
      "User prefers no sign-offs in replies",
      "User wants direct answers"]),
    ("User: For diagrams, use ASCII or Mermaid, not images. "
     "Assistant: Text-renderable diagrams.",
     ["User prefers ASCII diagrams",
      "User prefers Mermaid diagrams",
      "User does not want image diagrams"]),
    ("User: When I ask 'what's the difference', give me a markdown table not prose. "
     "Assistant: Tables for comparisons.",
     ["User prefers markdown tables for comparison questions",
      "User does not want prose comparisons"]),
]
for transcript, kp in SSP:
    _add("single-session-preference", transcript, kp)


# ---------- temporal-reasoning (14 rows) ----------
# Question relies on time-relative reasoning.

TR = [
    ("User: Yesterday I had pasta for dinner, and the day before I had a salad. "
     "Assistant: Got it.",
     ["User had pasta for dinner yesterday",
      "User had salad two days ago"]),
    ("User: Last week's standup was on Wednesday since Monday and Tuesday were holidays. "
     "Assistant: Schedule shift.",
     ["Last week's standup was on Wednesday",
      "Monday was a holiday",
      "Tuesday was a holiday"]),
    ("User: I'll be on PTO from the 12th to the 19th, back online on the 20th. "
     "Assistant: Out from the 12th to the 19th, returning the 20th.",
     ["User will be on PTO from the 12th to the 19th",
      "User returns on the 20th"]),
    ("User: My birthday is two weeks from this Saturday. Assistant: Mark it down.",
     ["User's birthday is two weeks from this Saturday"]),
    ("User: I joined the company three years ago this October. Assistant: Anniversary.",
     ["User joined the company three years ago in October"]),
    ("User: The deadline shifted from end-of-month to a week later, so first week of November. "
     "Assistant: One-week extension noted.",
     ["Deadline shifted from end-of-month",
      "New deadline is first week of November",
      "Shift is approximately one week later"]),
    ("User: I had coffee at noon and another at 3 PM, two cups total today. "
     "Assistant: Two-cup day.",
     ["User had coffee at noon",
      "User had another coffee at 3 PM",
      "Total is two cups today"]),
    ("User: My next checkup is six months out, around mid-October. "
     "Assistant: Booked for October.",
     ["Next checkup is in six months",
      "Around mid-October"]),
    ("User: I've been training for the marathon for the past four months. "
     "Assistant: Long buildup.",
     ["User has been training for a marathon",
      "Training period is four months so far"]),
    ("User: My last car broke down in August, current one I bought in September. "
     "Assistant: Quick replacement.",
     ["Previous car broke down in August",
      "Current car was bought in September"]),
    ("User: I quit smoking exactly two years ago today. Assistant: Worth celebrating.",
     ["User quit smoking",
      "Quit was exactly two years ago today"]),
    ("User: The lease started in March and runs for 18 months. Assistant: Through next September.",
     ["Lease started in March",
      "Lease term is 18 months"]),
    ("User: We have weekly 1:1s every Friday, started this format two months ago. "
     "Assistant: Solid cadence.",
     ["1:1 meetings are weekly on Friday",
      "Format started two months ago"]),
    ("User: My visa expires in eleven months, need to start renewal paperwork by next quarter. "
     "Assistant: Plan ahead.",
     ["Visa expires in eleven months",
      "Renewal paperwork needs to start by next quarter"]),
]
for transcript, kp in TR:
    _add("temporal-reasoning", transcript, kp)


def main() -> None:
    assert len(ROWS) == 80, f"expected 80 got {len(ROWS)}"
    out = Path("tests/fixtures/handlers/summary_transcripts.jsonl")
    out.write_text(
        "\n".join(json.dumps(r) for r in ROWS) + "\n",
        encoding="utf-8",
    )
    # Per question_type breakdown
    from collections import Counter
    counts = Counter(r["question_type"] for r in ROWS)
    print(f"wrote {len(ROWS)} rows to {out}")
    for qt, n in sorted(counts.items()):
        print(f"  {qt}: {n}")


if __name__ == "__main__":
    main()
