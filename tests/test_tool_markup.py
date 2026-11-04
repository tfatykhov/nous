"""strip_tool_markup removes tool-call markup a model leaked into a stored value, and nothing else."""

from __future__ import annotations

import logging

import pytest

from nous.tool_markup import calls_in, clean_model_text, markup_note, params_in, strip_tool_markup

# The namespaced form of the tags, built so this file holds no literal namespaced tag.
NS = "antml" + ":"

STRIP = [
    # The shape in the incident: the value is closed as XML and the next parameter follows.
    (
        'Zone X has a small craft advisory.</content>\n<parameter name="category">technical',
        "content",
        "Zone X has a small craft advisory.",
    ),
    # Parameters written as tags named after them, then the end of the call.
    (
        "Fact text.</content>\n<category>technical</category>\n<subject>zone</subject>\n</invoke>",
        "content",
        "Fact text.",
    ),
    # Tags named after the parameters and no parameter/invoke tag: the field's own close counts.
    ('Fact text.</content>\n<category>technical</category>\n<tags>["a"]</tags>', "content", "Fact text."),
    # A parameter close and the next parameter.
    ('Decided X.</parameter>\n<parameter name="context">why</parameter>', "description", "Decided X."),
    # A decision context closed as XML, then the next parameter.
    ('Context.</context>\n<parameter name="pattern">a pattern', "context", "Context."),
    # Only the end of the call.
    ("Fact text.\n</invoke>", "content", "Fact text."),
    # A closing tag of another name followed by the end of the call.
    ("Fact text.</merged_content>\n</invoke>", "merged_content", "Fact text."),
    # A value with '<' in the spilled call (code) is still removed whole.
    (
        'Ctx.</context>\n</invoke>\n<invoke name="run_python">\n<parameter name="code">if a < b:\n    print(a)',
        "context",
        "Ctx.",
    ),
    # The namespaced tags.
    (f'Fact.</{NS}parameter>\n<{NS}parameter name="category">x', "content", "Fact."),
    # A parameter tag opening the value.
    ('<parameter name="content">The fact.</parameter>', "content", "The fact."),
]

KEEP = [
    ('Use <div class="x">a</div> for layout.', "content"),
    ("a < b and c > d", "content"),
    ("Atom entries wrap their text in <content>...</content>", "content"),
    ("Close the value with </parameter> and then go on.", "content"),
    ('The fact ended with `</content><parameter name="category">technical`', "content"),
    ('The XML format is <parameter name="x">v</parameter>', "content"),
    ("Remember to close it with </div>", "content"),
    # A lone close of the field, with nothing after: not clearly a leak.
    ("Fact text.</content>", "content"),
    # The field's own close with tags after, but for another field: needs a tool tag.
    ("Fact text.</content>\n<category>technical</category>", "description"),
    ("List<String> xs = new ArrayList<>();", "content"),
    ("return <App/>;", "content"),
    ("", "content"),
    ("no markup at all", "content"),
]


@pytest.mark.parametrize(("text", "field", "expected"), STRIP)
def test_strips_leaked_markup(text, field, expected):
    clean, removed = strip_tool_markup(text, field)
    assert clean == expected
    assert removed


@pytest.mark.parametrize(("text", "field"), KEEP)
def test_keeps_text_that_is_not_leaked_markup(text, field):
    assert strip_tool_markup(text, field) == (text, "")


@pytest.mark.parametrize(
    "text",
    [
        "</b>" * 20000,
        "x</b>" * 20000,
        "<content>" * 10000 + "</content>" * 10000,
        "</content>" * 20000 + '<parameter name="x">',
        " " * 50000 + "</content>" + " " * 50000,
    ],
    ids=["closes", "closes-in-prose", "paired", "closes-then-param", "whitespace"],
)
def test_linear_on_adversarial_input(text):
    """Model text reaches this on the event loop: a rescan per tag would stall it."""
    import time

    t0 = time.perf_counter()
    strip_tool_markup(text, "content")
    assert time.perf_counter() - t0 < 1.0


def test_idempotent():
    clean, _ = strip_tool_markup('Fact.</content>\n<parameter name="category">technical', "content")
    assert strip_tool_markup(clean, "content") == (clean, "")


def test_calls_in_names_the_calls_that_never_ran():
    text = (
        "Fact one.</content>\n<category>t</category>\n</invoke>\n"
        '<invoke name="learn_fact">\n<parameter name="content">Fact two.</content>\n</invoke>'
    )
    clean, removed = strip_tool_markup(text, "content")
    assert clean == "Fact one."
    assert calls_in(removed) == ["learn_fact"]
    assert params_in(removed) == ["category"]  # the closed call's, not the next call's content
    assert params_in('</context>\n<parameter name="pattern">p</parameter>\n<tags>x</tags>') == ["pattern", "tags"]
    assert "learn_fact" in markup_note(removed)
    assert markup_note("") == ""


def test_clean_model_text_logs_the_tool_but_not_the_text(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.tool_markup"):
        clean, removed = clean_model_text(
            'Secret fact.</content>\n<parameter name="category">technical', tool="learn_fact", field="content"
        )
    assert clean == "Secret fact."
    assert removed
    assert "learn_fact" in caplog.text and "content" in caplog.text
    assert "Secret" not in caplog.text and "technical" not in caplog.text


def test_clean_model_text_is_silent_when_nothing_leaked(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.tool_markup"):
        assert clean_model_text("plain", tool="learn_fact", field="content") == ("plain", "")
        assert clean_model_text(None, tool="learn_fact", field="subject") == (None, "")
    assert caplog.text == ""
