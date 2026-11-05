"""strip_tool_markup removes tool-call markup a model leaked into a stored value, and nothing else."""

from __future__ import annotations

import gc
import logging
import time

import pytest

from nous.tool_markup import (
    calls_in,
    clean_model_text,
    leaked_values,
    markup_note,
    params_in,
    strip_tool_markup,
)

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
    # Fenced code blocks (CommonMark), separate from inline spans: tilde fences, a closing run
    # longer than the opener, an indented opener, and an unclosed fence protecting to the end.
    (
        'Example:\n~~~xml\n<invoke name="learn_fact">\n<parameter name="content">example</parameter>\n'
        "</invoke>\n~~~\nDo not execute this.",
        "content",
        LEARN,
    ),
    (
        'Example:\n```\n<invoke name="learn_fact">\n<parameter name="content">x</parameter>\n</invoke>\n````\nDone.',
        "content",
        LEARN,
    ),
    ('Example:\n   ~~~~\nf.</content>\n<parameter name="category">technical\n~~~~~\nDone.', "content", LEARN),
    ('Example:\n```\n<invoke name="learn_fact">\n<parameter name="content">x', "content", LEARN),
    ('Example:\n~~~\nf.</content>\n<parameter name="category">technical', "content", LEARN),
    # An open <parameter name=...> mid-sentence is prose about the format (a leak starts its line).
    ('Each value opens with <parameter name="content"> and the text follows on the same line.', "content", LEARN),
    ('The opening tag is <parameter name="content">. Nothing else is needed.', "content", LEARN),
    ('After </parameter> comes <parameter name="category"> and its value, technical for instance.', "content", LEARN),
    ('Tags used: <function_calls>, <invoke name="x">, <parameter name="y">.', "content", LEARN),
    (
        'Facts open with <parameter name="content">; decisions open with <parameter name="description">.',
        "content",
        LEARN,
    ),
    # Tags inside code take no part in any scan: not as candidates, closers or what follows a run.
    (
        "Use <context> to wrap values; close them with `</context></invoke>`; do not execute this example.",
        "context",
        DECIDE,
    ),
    (
        "Use <context> to wrap values, closed like this:\n```\n</context>\n</invoke>\n```\nDo not run it.",
        "context",
        DECIDE,
    ),
    # An open plain <x> of a tool parameter at the end, with no tool tag: kept (see the module docstring).
    ("f.\n<category>technical</category>\n<subject>s", "content", LEARN),
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


def _best_time(text: str, n: int = 3) -> float:
    """Best of n runs with the garbage collector off: a collection inside one timed run was
    the main source of noise on a loaded machine."""
    best = float("inf")
    gc.disable()
    try:
        for _ in range(n):
            t0 = time.perf_counter()
            strip_tool_markup(text, "content", LEARN)
            best = min(best, time.perf_counter() - t0)
    finally:
        gc.enable()
    return best


def _assert_linear(shape, n: int = 5000) -> None:
    """Eight times the input takes about eight times as long; a rescan per tag takes about 64.

    A ratio, not a wall-clock limit: a shared CI runner can be several times slower than a
    dev box, and an absolute bound failed there on linear code. n against 8n, not 2n: at 2n
    the linear code's noise on a loaded machine (up to 3.4x) overlapped the quadratic's 3.1x.
    Measured: linear 7.9-8.5x; the quadratic next_of this was written against, 32x at n=5000.
    Each shape's n puts its smaller run at a few milliseconds, so the 2 ms margin is not most
    of the bound.
    """
    t1, t8 = _best_time(shape(n), 5), _best_time(shape(8 * n))
    assert t8 < 16 * t1 + 0.002, (n, t1, t8)


@pytest.mark.parametrize(
    ("shape", "n"),
    [
        (lambda n: "</b>" * n, 5000),
        (lambda n: "x</b>" * n, 5000),
        (lambda n: "<content>" * (n // 2) + "</content>" * (n // 2), 5000),
        (lambda n: "</content>" * n + '<parameter name="x">', 5000),
        (lambda n: '<parameter name="x">' * n + " prose", 5000),
        (lambda n: "<category>a</category>" * (n // 2) + " prose", 5000),
        (lambda n: "`" * n + "</content>" + "``" * (n // 4), 400000),
        (lambda n: " " * (n * 5 // 2) + "</content>" + " " * (n * 5 // 2), 50000),
        (lambda n: "<x>" * n + "v</x>" + " " * n + "prose", 5000),
    ],
    ids=[
        "closes",
        "closes-in-prose",
        "paired",
        "closes-then-param",
        "open-params",
        "runs",
        "backticks",
        "whitespace",
        "many-openers-one-closer",
    ],
)
def test_linear_on_adversarial_input(shape, n):
    """Model text reaches this on the event loop: a rescan per tag would stall it. The
    many-openers shape: each opener's run reaches the same closer, and the gap after it must
    be read once, not once per opener."""
    _assert_linear(shape, n)


def test_a_leading_call_is_not_reported_as_one_that_never_ran():
    """The <invoke> before the value is the call that ran; only the tail's calls did not."""
    text = '<invoke name="learn_fact">\n<parameter name="content">Fact.</content>\n<parameter name="category">technical'
    clean, removed = strip_tool_markup(text, "content", LEARN)
    assert clean == "Fact."
    assert calls_in(removed) == []
    assert params_in(removed) == ["category"]
    assert dict(leaked_values(removed)) == {"category": "technical"}


@pytest.mark.parametrize(
    ("removed", "expected"),
    [
        # The value runs to its matching closer; a literal '<' inside is kept.
        (
            '</description><parameter name="context">Only enable when load < 5; otherwise reject.</parameter></invoke>',
            {"context": "Only enable when load < 5; otherwise reject."},
        ),
        ("</content>\n<category>a < b</category>\n<subject>s</subject>", {"category": "a < b", "subject": "s"}),
        # A tag inside a value does not start a pair of its own.
        (
            '</content>\n<parameter name="subject">see <category>x</category> here</parameter>',
            {"subject": "see <category>x</category> here"},
        ),
        # No closer, nothing tag-shaped after it: the rest is the value.
        ('</content>\n<parameter name="category">technical', {"category": "technical"}),
        # No closer, and a tag after it: the value cannot be told, so it is declined (None).
        ('</content>\n<parameter name="subject">zone <b>x', {"subject": None}),
        # Only the closed call's part: a later call's arguments are not this call's.
        ('</content>\n</invoke>\n<invoke name="learn_fact">\n<parameter name="category">technical', {}),
    ],
)
def test_leaked_values_run_to_the_matching_closer(removed, expected):
    assert dict(leaked_values(removed)) == expected


def test_an_unterminated_tag_is_linear():
    """A '<' with no '>' after it: the tokenizer must not retry every name/attribute split.

    This one keeps a wall-clock bound on purpose: the work is inside the regex engine, where
    nothing can be counted, and the backtracking it guards against took about half a second
    at 10k letters against well under a millisecond for the linear tokenizer."""
    t20 = _best_time("<" + "a" * 20000, 5)
    t40 = _best_time("<" + "a" * 40000, 5)
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
