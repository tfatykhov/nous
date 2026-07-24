"""Pure-function unit tests for the tier-1 integrity backfill's event-noise
classifiers (2026-07-24). NO database or LLM access here — the regex phase's
`classify_event_noise_ab` / `classify_event_noise_c` are pinned against the
audit samples, including the documented precision trades (word-boundary guard,
`instructed` exclusion, and C's weekday/ephemerality bias).
"""
from scripts.backfill_tier1_integrity import (
    classify_event_noise_ab,
    classify_event_noise_c,
)


# --- A + B: delivery past-passive + request-verb anchored -------------------

def test_ab_delivery_past_passive():
    # A: "was sent" and "sent to" both fire.
    assert classify_event_noise_ab("The forecast was sent to timandeugene@gmail.com")
    assert classify_event_noise_ab("The report was emailed this morning")


def test_ab_request_verb_anchored():
    # B: subject + request-verb at the start of the statement.
    assert classify_event_noise_ab("The user requested to trigger sleep mode")
    assert classify_event_noise_ab("The assistant proposed a refactor of the loop")
    assert classify_event_noise_ab("A user asked about pricing tiers")


def test_ab_excludes_instructed_standing_directive():
    # `instructed` is deliberately NOT a B verb — a standing directive must
    # survive the scrub. Neither A nor B fires here.
    assert not classify_event_noise_ab(
        "The user instructed: Do not recommend trading bot to sell underwater assets"
    )


def test_ab_word_boundary_blocks_substring_hits():
    # `\bsent to\b` must not fire on "present to" / "consent to".
    assert not classify_event_noise_ab("present to review the quarterly document")
    assert not classify_event_noise_ab("I gave consent to the updated terms")


def test_ab_keeps_genuine_profile_facts():
    assert not classify_event_noise_ab("Tim prefers Celsius for temperature readings")
    assert not classify_event_noise_ab("HARD RULE: Sources must be cited")


# --- C: dated-logistics (doc-atom sources only) ----------------------------

def test_c_dated_logistics_fire():
    assert classify_event_noise_c("Tim's seat on flight UA3455 is 17C")  # UA flight
    assert classify_event_noise_c("Deadline is 12/25 for the submission")  # m/d
    assert classify_event_noise_c("Standup moved to 3:30 pm today")  # time
    assert classify_event_noise_c("Ship the build tomorrow")  # tomorrow


def test_c_weekday_ephemerality_bias_is_accepted():
    # Documented precision trade: a weekday-bearing standing rule matches C.
    # C is scoped in the phase to doc-atom sources only, where this is fine.
    assert classify_event_noise_c("Weekly summary every Monday morning is preferred")


def test_c_keeps_dateless_profile_facts():
    assert not classify_event_noise_c("Tim prefers Celsius for temperature readings")
    assert not classify_event_noise_c("HARD RULE: Sources must be cited")
    assert not classify_event_noise_c("The user requested to trigger sleep mode")


def test_ab_contact_fact_email_was_not_demoted():
    """codex r2: bare 'email was' matched durable contact facts — pattern A is
    qualified to the delivery event ('email was sent')."""
    assert not classify_event_noise_ab("Tim's email was tim@example.com")
    assert not classify_event_noise_ab("Tim's work email was updated to timur@fanniemae.com")
    assert classify_event_noise_ab("The email was sent at nine this morning")
