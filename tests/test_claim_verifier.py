"""Harness Phase 2c: first-person claims, extracted by kind, one sentence at a time."""

import pytest

from nous.cognitive.claim_verifier import ClaimVerifier


def _kinds(text):
    return [(c.kind, c.target) for c in ClaimVerifier()._extract_claims(text)]


def test_a_word_ending_in_i_is_not_a_subject():
    assert _kinds("The API sent the message to the queue.") == []


def test_a_claim_never_spans_sentences():
    assert _kinds("I created a helper. It reads data.\n\nLater the report file was large.") == []


def test_two_claims_in_one_paragraph_stay_two():
    text = "I saved the summary file. I sent the email to the team."
    assert [k for k, _ in _kinds(text)] == ["file_write", "email"]


def test_a_first_person_compound_is_two_claims():
    assert [k for k, _ in _kinds("I saved the file and sent the email.")] == ["file_write", "email"]


@pytest.mark.parametrize("narration", [
    "The send node completed and sent the premarket email to Tim.",
    "The subtask finished and pushed the branch.",
    "The heartbeat check ran and sent a message to the channel.",
    "Bob reviewed it and sent the email to the team.",
    "I checked: the DAG finished and sent the summary email to Tim.",
    "I see the subtask completed and sent the email to the team.",
    "I confirmed the scheduled task ran and pushed the branch.",
    "As I expected, the node ran and deployed the build.",
    "i sent the email",  # lowercase i is not the first person
])
def test_third_person_narration_is_not_a_claim(narration):
    assert _kinds(narration) == []


def test_a_failed_match_never_backtracks_exponentially():
    import time

    start = time.perf_counter()
    ClaimVerifier()._extract_claims("I saved " + "etc.," * 32 + " nothing " * 3 + "e.g., " * 32)
    assert time.perf_counter() - start < 0.05


@pytest.mark.parametrize("claim", [
    "I wrote the config.yaml file.",
    "I generated the v1.2 report for the board.",
    "I saved the notes (see e.g. section 2) in a file.",
])
def test_dots_inside_words_do_not_end_a_sentence(claim):
    assert [k for k, _ in _kinds(claim)] == ["file_write"]


def test_saved_to_captures_the_target():
    assert _kinds("The output was saved to /tmp/report.txt.") == [("file_write", "/tmp/report.txt")]


def test_i_saved_x_to_path_captures_the_target():
    assert _kinds("I saved the report to /tmp/x.md.") == [("file_write", "/tmp/x.md")]


def test_email_sent_to_captures_the_recipient():
    assert _kinds("Email sent to Alice@Example.com.") == [("email", "alice@example.com")]


def test_push_commit_deploy_are_distinct_kinds():
    assert [k for k, _ in _kinds("I committed the fix. I pushed it. I deployed the build.")] == [
        "vcs_commit", "vcs_push", "deploy"]


def test_plans_are_not_claims():
    assert _kinds("I will save the file and then I'll send the email.") == []


def test_a_curly_apostrophe_subject_is_a_claim():
    assert [k for k, _ in _kinds("I’ve saved the report file.")] == ["file_write"]
