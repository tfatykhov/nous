"""strip_tool_markup removes tool-call markup a model leaked into a stored value, and nothing else."""

from __future__ import annotations

import logging
import time

import pytest

from nous.tool_markup import calls_in, clean_model_text, markup_note, params_in, strip_tool_markup

# The namespaced form of the tags, built so this file holds no literal namespaced tag.
NS = "antml" + ":"
# The parameters of the tools whose text is cleaned (learn_fact, record_decision).
LEARN = frozenset({"content", "category", "subject", "confidence", "source", "source_decision_id", "tags"})
DECIDE = frozenset({"description", "confidence", "category", "stakes", "context", "pattern", "tags", "reasons"})

STRIP = [
    # The shape in the incident: the value is closed as XML and the next parameter follows.
    (
        'Zone X has a small craft advisory.</content>\n<parameter name="category">technical',
        "content",
        LEARN,
        "Zone X has a small craft advisory.",
    ),
    # Parameters written as tags named after them, then the end of the call.
    (
        "Fact text.</content>\n<category>technical</category>\n<subject>zone</subject>\n</invoke>",
        "content",
        LEARN,
        "Fact text.",
    ),
    # The field's own close, then a tag named after one of the tool's parameters.
    ('Fact text.</content>\n<category>technical</category>\n<tags>["a"]</tags>', "content", LEARN, "Fact text."),
    ("Fact text.</content>\n<category>technical", "content", LEARN, "Fact text."),
    ("Ctx.</context>\n<pattern>p</pattern>\n<reasons>[]", "context", DECIDE, "Ctx."),
    # A parameter close and the next parameter.
    ('Decided X.</parameter>\n<parameter name="context">why</parameter>', "description", DECIDE, "Decided X."),
    ('Context.</context>\n<parameter name="pattern">a pattern', "context", DECIDE, "Context."),
    # Only the end of the call.
    ("Fact text.\n</invoke>", "content", LEARN, "Fact text."),
    ("Fact text.\n</invoke>\n</function_calls>", "content", LEARN, "Fact text."),
    # A closing tag of another name followed by the end of the call.
    ("Fact text.</merged_content>\n</invoke>", "merged_content", (), "Fact text."),
    ("Ctx.</tags>\n</invoke>", "context", DECIDE, "Ctx."),
    # A value with '<' in the spilled call (code) is still removed whole.
    (
        'Ctx.</context>\n</invoke>\n<invoke name="run_python">\n<parameter name="code">if a < b:\n    print(a)',
        "context",
        DECIDE,
        "Ctx.",
    ),
    # A parameter left open to the end, with no closing tag before it (B2).
    ('f.\n<parameter name="category">technical', "content", LEARN, "f."),
    ('f.\n<parameter name="category">technical</parameter>\n</invoke>', "content", LEARN, "f."),
    # Elements named after parameters, no closing tag before them, then the end of the call.
    ("f.\n<category>technical</category>\n</invoke>", "content", LEARN, "f."),
    # A plain element run after a close, ended by the end of the call (no parameter list).
    ("f.</content>\n<summary>brief</summary>\n</invoke>", None, (), "f."),
    # The namespaced tags.
    (f'Fact.</{NS}parameter>\n<{NS}parameter name="category">x', "content", LEARN, "Fact."),
    # A parameter tag opening the value.
    ('<parameter name="content">The fact.</parameter>', "content", LEARN, "The fact."),
]

KEEP = [
    ('Use <div class="x">a</div> for layout.', "content", LEARN),
    ("a < b and c > d", "content", LEARN),
    ("Atom entries wrap their text in <content>...</content>", "content", LEARN),
    ("RSS <content><p>hi</p></content> done", "content", LEARN),
    # B1: the field's own close followed by another closing tag is prose.
    ("Atom closing tags: </content> </entry>", "content", LEARN),
    ("Atom closing tag is </content>", "content", LEARN),
    ("Close </section>\n<footer>f</footer>", "content", LEARN),
    ("Close the value with </parameter> and then go on.", "content", LEARN),
    ("the </invoke> tag ends a call in the format", "content", LEARN),
    # Quoted in code spans, single and double backticks, and in a fence (CommonMark).
    ('The fact ended with `</content><parameter name="category">technical`', "content", LEARN),
    ('Store this example: ``</content><parameter name="category">technical``', "content", LEARN),
    ('Uses ``a`b`` then `</content><parameter name="category">x`', "content", LEARN),
    ('```\n<invoke name="learn_fact">\n<parameter name="content">x</parameter>\n</invoke>\n```', "content", LEARN),
    # A complete element at the end is as likely a quoted example.
    ('The XML format is <parameter name="x">v</parameter>', "content", LEARN),
    ("f.\n<category>technical</category>", "content", LEARN),
    ("Use <context> tags in the prompt", "context", DECIDE),
    ("Remember to close it with </div>", "content", LEARN),
    # A lone close of the field, with nothing after: not clearly a leak.
    ("Fact text.</content>", "content", LEARN),
    # Another field's close followed by a parameter-named element, nothing after.
    ("Fact text.</content>\n<category>technical</category>", "description", DECIDE),
    ('<?xml version="1.0"?>\n<feed><entry><title>t</title></entry></feed>', "content", LEARN),
    ("<li>a</li><li>b</li></ul>", "content", LEARN),
    ("List<String> xs = new ArrayList<>();", "content", LEARN),
    ("return <App/>;", "content", LEARN),
    ("See <https://example.com/x> or mail <tim@example.com>", "content", LEARN),
    ("", "content", LEARN),
    ("no markup at all", "content", LEARN),
]


@pytest.mark.parametrize(("text", "field", "params", "expected"), STRIP)
def test_strips_leaked_markup(text, field, params, expected):
    clean, removed = strip_tool_markup(text, field, params)
    assert clean == expected
    assert removed
    assert strip_tool_markup(clean, field, params) == (clean, "")  # idempotent


@pytest.mark.parametrize(("text", "field", "params"), KEEP)
def test_keeps_text_that_is_not_leaked_markup(text, field, params):
    assert strip_tool_markup(text, field, params) == (text, "")


@pytest.mark.parametrize(
    "text",
    [
        "</b>" * 20000,
        "x</b>" * 20000,
        "<content>" * 10000 + "</content>" * 10000,
        "</content>" * 20000 + '<parameter name="x">',
        '<parameter name="x">' * 20000 + " prose",
        "<category>a</category>" * 10000 + " prose",
        "`" * 20000 + "</content>" + "``" * 5000,
        " " * 50000 + "</content>" + " " * 50000,
    ],
    ids=["closes", "closes-in-prose", "paired", "closes-then-param", "open-params", "runs", "backticks", "whitespace"],
)
def test_linear_on_adversarial_input(text):
    """Model text reaches this on the event loop: a rescan per tag would stall it."""
    t0 = time.perf_counter()
    strip_tool_markup(text, "content", LEARN)
    assert time.perf_counter() - t0 < 1.0


def _best_of(text: str, n: int = 5) -> float:
    best = float("inf")
    for _ in range(n):
        t0 = time.perf_counter()
        strip_tool_markup(text, "content", LEARN)
        best = min(best, time.perf_counter() - t0)
    return best


def test_an_unterminated_tag_is_linear():
    """A '<' with no '>' after it: the tokenizer must not retry every name/attribute split."""
    t20 = _best_of("<" + "a" * 20000)
    t40 = _best_of("<" + "a" * 40000)
    assert t20 < 0.2
    # Linear doubles; quadratic quadruples. 3x leaves room for timer noise on small numbers.
    assert t40 < 3 * t20 + 0.005


def test_calls_in_names_the_calls_that_never_ran():
    text = (
        "Fact one.</content>\n<category>t</category>\n</invoke>\n"
        '<invoke name="learn_fact">\n<parameter name="content">Fact two.</content>\n</invoke>'
    )
    clean, removed = strip_tool_markup(text, "content", LEARN)
    assert clean == "Fact one."
    assert calls_in(removed) == ["learn_fact"]
    assert params_in(removed) == ["category"]  # the closed call's, not the next call's content
    assert params_in('</context>\n<parameter name="pattern">p</parameter>\n<tags>x</tags>') == ["pattern", "tags"]
    assert "if you meant to run them" in markup_note(removed)
    assert markup_note("") == ""


def test_params_in_bounds_the_names_it_prints():
    removed = '</content>\n<parameter name="' + "x" * 200 + '\x1b[31m">v'
    (name,) = params_in(removed)
    assert name == "x" * 64


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
        assert clean_model_text(3, tool="resolve_decisions", field="resolution_note") == (3, "")
    assert caplog.text == ""
