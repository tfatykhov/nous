"""F120 tool loading, On: check one model id against deferred tool definitions through production's own client.

On sends tools outside the core set with ``defer_loading`` and answers Nous's own loader with ``tool_reference``
blocks (nous/api/tool_loading.py). A model id goes into ``tool_loading.DEFERRING_MODELS`` only after this script passes
on it: claude-sonnet-5-5 and claude-opus-5-5 passed its checks, claude-fable-5-2 the whole script, in production.
It drives ``SdkAnthropicClient`` (``call``, ``stream`` and ``call_streaming_aggregated``) with the settings of the
process it runs in, so run it inside the agent's container, where the credentials and the installed SDK are
production's, with the model id as its one argument:

    docker exec -i nous-agent python - claude-sonnet-5-5 < scripts/ops/tool_loading_smoke.py

It sends a few dozen small requests to that model (``SMOKE_ONLY=S2,S8`` runs only the named groups). It prints one
line per check: the check, OK or REFUSED, the stop reason, each reply block's type (a tool call by its name and its
input's keys, a text by its first words) and the usage numbers; a refusal by its HTTP status and the API's message. It
never prints a header, a key, a token or more of a message than its first words.

What passing means: S2, S2b, S3, S5, S6.2, S6c.3, S8.3, S10, S12b, S12c, S12d and S15 are OK (S12d.3 and .4 say
"partial" when the model made fewer than the three calls asked for), and S4, S6.1, S6.3, S8.1 and
S8.2 are REFUSED (a reference to a tool the request does not send, and a reference beside text or in an error result,
are refused; Nous never sends either). S1 and S1b (a forced tool choice) are refused by some models with or without
deferral; Nous does not force a tool when thinking is on. S7 and S9 describe the model's behaviour and pass either
way.
"""

import asyncio
import copy
import os
import sys

from nous.api import anthropic_client as ac
from nous.api.preamble import CLAUDE_CODE_PREAMBLE
from nous.config import Settings

if len(sys.argv) != 2:
    raise SystemExit("usage: tool_loading_smoke.py <model id>")
MODEL = sys.argv[1]
ONLY = set(filter(None, os.environ.get("SMOKE_ONLY", "").split(",")))
FILLER = " ".join(f"Rule {i}: keep answers short and factual; item {i} has no other meaning." for i in range(1, 260))
ADAPTIVE = {"type": "adaptive"}
WORDS = 6  # the first words of a reply's text that a line shows


def tool(name, desc, props, required, **extra):
    return {
        "name": name,
        "description": desc,
        "input_schema": {"type": "object", "properties": props, "required": required},
        **extra,
    }


WEATHER_DESC = "Get the weather for a city. " + " ".join(
    f"Note {i}: celsius unless asked otherwise." for i in range(1, 60)
)
LOAD_FIRST = "Load a tool by name before calling it. Tools you can load: "
INDEX = LOAD_FIRST + "get_weather (weather for a city); get_stock (price of a stock)."
LOADER = tool("load_tools", INDEX, {"names": {"type": "array", "items": {"type": "string"}}}, ["names"])
CORE = tool("echo", "Echo a text back.", {"text": {"type": "string"}}, ["text"])
REPORT = tool("submit_final_report", "Hand in the final report.", {"report": {"type": "string"}}, ["report"])
WEATHER = tool(
    "get_weather",
    WEATHER_DESC,
    {
        "city": {"type": "string"},
        "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
        "reason": {"type": "string"},
    },
    ["city", "unit"],
)
STOCK = tool("get_stock", "Get the latest price of a stock by its ticker.", {"ticker": {"type": "string"}}, ["ticker"])
D = {"defer_loading": True}
TOOLS = [LOADER, CORE, REPORT, {**WEATHER, **D}, {**STOCK, **D}]
SYSTEM = [
    {"type": "text", "text": CLAUDE_CODE_PREAMBLE},
    {"type": "text", "text": FILLER, "cache_control": {"type": "ephemeral"}},
]
ASK = "What is the weather in Paris in celsius? Use your tools."
NOT_LOADED = "get_stock is not loaded yet, so this call did not run. Call load_tools with its name, then call it again."


def base(messages, tools=TOOLS, thinking=ADAPTIVE, **extra):
    p = {"model": MODEL, "max_tokens": 2000, "system": SYSTEM, "messages": messages}
    if tools is not None:
        p["tools"] = tools
    if thinking is not None:
        p["thinking"] = thinking
    p.update(extra)
    return p


def blocks(content):
    out = []
    for b in content or []:
        t = b.get("type")
        if t == "tool_use":
            out.append(f"tool_use:{b.get('name')}:{sorted((b.get('input') or {}).keys())}")
        elif t == "text":
            out.append("text:" + " ".join((b.get("text") or "").split()[:WORDS]))
        else:
            out.append(t)
    return out


def usage(u):
    u = u or {}
    return (
        f"in {u.get('input_tokens')} cw {u.get('cache_creation_input_tokens')} "
        f"cr {u.get('cache_read_input_tokens')} out {u.get('output_tokens')}"
    )


def refusal(e):
    """An API refusal as its HTTP status and the API's own message, nothing else of the response."""
    cause = e.__cause__ or e
    status = getattr(cause, "status_code", None)
    body = getattr(cause, "body", None)
    message = body.get("error", {}).get("message") if isinstance(body, dict) else None
    return f"status {status} | {(message or type(cause).__name__)[:200]}"


async def call(client, payload):
    """(ok, content, stop, usage, refusal)"""
    try:
        r = await client.call(payload)
        return True, r.content, r.stop_reason, r.usage, ""
    except Exception as e:  # the client raises its own error from the SDK's
        return False, [], None, None, refusal(e)


def line(tag, ok, content, stop, u, err):
    if ok:
        print(f"{tag} | OK stop={stop} | {blocks(content)} | {usage(u)}")
    else:
        print(f"{tag} | REFUSED | {err}")
    sys.stdout.flush()


def first_use(content, name=None):
    for b in content or []:
        if b.get("type") == "tool_use" and (name is None or b.get("name") == name):
            return b
    return None


def result(use, content, **extra):
    return {"type": "tool_result", "tool_use_id": use["id"], "content": content, **extra}


REF_W = {"type": "tool_reference", "tool_name": "get_weather"}


async def load_turn(client, thinking=ADAPTIVE, tools=TOOLS):
    """First request: the model should call load_tools. Returns the reply, the messages so far and the loader call."""
    msgs = [{"role": "user", "content": ASK}]
    ok, content, stop, u, err = await call(client, base(msgs, tools=tools, thinking=thinking))
    use = first_use(content, "load_tools")
    return ok, content, stop, u, err, msgs, use


async def main():
    s = Settings()
    client = ac.SdkAnthropicClient(s)
    await client.start()
    print("model", MODEL, "| sdk backend | anthropic", getattr(sys.modules.get("anthropic"), "__version__", "?"))

    def want(name):
        return not ONLY or name in ONLY

    try:
        # S2: adaptive thinking, full round trip
        if want("S2"):
            ok, content, stop, u, err, msgs, use = await load_turn(client)
            line("S2.1 adaptive thinking, first call", ok, content, stop, u, err)
            if use:
                m2 = msgs + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": [result(use, [REF_W])]},
                ]
                ok2, c2, st2, u2, e2 = await call(client, base(m2))
                line("S2.2 after the reference (thinking blocks passed back)", ok2, c2, st2, u2, e2)
                w = first_use(c2, "get_weather")
                if w:
                    m3 = m2 + [
                        {"role": "assistant", "content": c2},
                        {"role": "user", "content": [result(w, "18 C, sunny")]},
                    ]
                    line("S2.3 after the tool's result", *await call(client, base(m3)))
                    # S9: clear the loader's result (as pruning old results does), then ask for another city
                    if want("S9"):
                        m9 = copy.deepcopy(m3)
                        m9[2]["content"][0]["content"] = (
                            "[Tool output cleared - content was processed in earlier turns]"
                        )
                        m9 += [
                            {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "It is 18 C and sunny in Paris."}],
                            },
                            {"role": "user", "content": "And in Rome, in celsius?"},
                        ]
                        line("S9   loader result cleared, tool used again", *await call(client, base(m9)))

        # S2b: the production shape: cache_control on the tool_result that holds the reference; then a text after it
        if want("S2b"):
            ok, content, stop, u, err, msgs, use = await load_turn(client)
            if use:
                r = result(use, [REF_W], cache_control={"type": "ephemeral"})
                m2 = msgs + [{"role": "assistant", "content": content}, {"role": "user", "content": [r]}]
                line("S2b.1 cache_control on the tool_result with the reference", *await call(client, base(m2)))
                after = {"type": "text", "text": "Status: step 2 of 20.", "cache_control": {"type": "ephemeral"}}
                m2t = msgs + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": [result(use, [REF_W]), after]},
                ]
                line("S2b.2 reference, then a text block carrying cache_control", *await call(client, base(m2t)))
            else:
                line("S2b first call", ok, content, stop, u, err)

        # S3: streaming and the aggregated background path
        if want("S3"):
            ok, content, stop, u, err, msgs, use = await load_turn(client)
            if use:
                m2 = msgs + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": [result(use, [REF_W])]},
                ]
                kinds, names, failed = [], [], ""
                async for ev in client.stream(base(m2, stream=True)):
                    if ev.type not in kinds:
                        kinds.append(ev.type)
                    if ev.tool_name and ev.tool_name not in names:
                        names.append(ev.tool_name)
                    if ev.type == "error":
                        failed = f"status {ev.status}"
                state = f"REFUSED | {failed}" if failed else "OK"
                print(f"S3.1 stream after the reference | {state} | event kinds {kinds} | tool names {names}")
                try:
                    r = await client.call_streaming_aggregated(base(m2))
                    print(
                        f"S3.2 aggregated stream after the reference | OK stop={r.stop_reason} | "
                        f"{blocks(r.content)} | {usage(r.usage)}"
                    )
                except Exception as e:
                    print(f"S3.2 aggregated stream after the reference | REFUSED | {refusal(e)}")
                try:
                    r = await client.call_streaming_aggregated(base([{"role": "user", "content": ASK}]))
                    print(f"S3.3 aggregated stream, first call | OK stop={r.stop_reason} | {blocks(r.content)}")
                except Exception as e:
                    print(f"S3.3 aggregated stream, first call | REFUSED | {refusal(e)}")
            sys.stdout.flush()

        # S4: a reference to a name that is not in tools
        if want("S4"):
            ok, content, stop, u, err, msgs, use = await load_turn(client)
            if use:
                bad = msgs + [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": [result(use, [{"type": "tool_reference", "tool_name": "no_such_tool"}])],
                    },
                ]
                line("S4   reference to a name not in tools", *await call(client, base(bad)))

        # S5/S6: several references, a non-deferred name, mixed content
        if want("S6"):
            ok, content, stop, u, err, msgs, use = await load_turn(client)
            if use:

                def after(c, **extra):
                    return msgs + [
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": [result(use, c, **extra)]},
                    ]

                three = [REF_W, {"type": "tool_reference", "tool_name": "get_stock"}, REF_W]
                line("S5.1 three references (weather, stock, weather again)", *await call(client, base(after(three))))
                echo = [{"type": "tool_reference", "tool_name": "echo"}]
                line("S5.2 reference to a non-deferred tool (echo)", *await call(client, base(after(echo))))
                mixed = [REF_W, {"type": "text", "text": "Loaded get_weather. no_such is not available."}]
                line("S6.1 reference plus a text block", *await call(client, base(after(mixed))))
                line("S6.2 text only", *await call(client, base(after("Loaded: get_weather."))))
                line("S6.3 reference with is_error true", *await call(client, base(after([REF_W], is_error=True))))

        # S7/S6c: the model calls a deferred tool WITHOUT loading it
        if want("S7"):
            msgs = [
                {
                    "role": "user",
                    "content": "Call get_weather directly for Paris in celsius now. Do not call load_tools first.",
                }
            ]
            ok, content, stop, u, err = await call(client, base(msgs))
            line("S7   asked to call the deferred tool without loading", ok, content, stop, u, err)
            w = first_use(content, "get_weather")
            txt = {"type": "text", "text": "get_weather was not loaded. It is loaded now: call it again."}
            answers = [
                ("S6c.1 answer: reference + text, is_error true", [REF_W, txt], {"is_error": True}),
                ("S6c.2 answer: reference + text, is_error false", [REF_W, txt], {}),
                (
                    "S6c.3 answer: text only, is_error true",
                    NOT_LOADED.replace("get_stock", "get_weather"),
                    {"is_error": True},
                ),
            ]
            if w:
                for tag, c, extra in answers:
                    m = msgs + [
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": [result(w, c, **extra)]},
                    ]
                    line(tag, *await call(client, base(m)))
            else:
                # build the unloaded call by hand (thinking off: a hand-built assistant turn has no thinking block)
                fake = {
                    "type": "tool_use",
                    "id": "toolu_smoke_unloaded_0001",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                }
                m = [{"role": "user", "content": ASK}, {"role": "assistant", "content": [fake]}]
                for tag, c, extra in answers:
                    tag = tag + " (hand-built call)"
                    request = base(m + [{"role": "user", "content": [result(fake, c, **extra)]}], thinking=None)
                    line(tag, *await call(client, request))

        # S8: history holding a reference while the tools array changed
        if want("S8"):
            ok, content, stop, u, err, msgs, use = await load_turn(client, thinking=None)
            if use:
                hist = msgs + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": [result(use, [REF_W])]},
                ]
                line(
                    "S8.1 history has a reference, no tools sent",
                    *await call(client, base(hist, tools=None, thinking=None)),
                )
                lacking = [LOADER, CORE, REPORT, {**STOCK, **D}]
                line(
                    "S8.2 history has a reference, tools lack that name",
                    *await call(client, base(hist, tools=lacking, thinking=None)),
                )
                in_full = [LOADER, CORE, REPORT, WEATHER, STOCK]
                line(
                    "S8.3 history has a reference, that tool sent in full",
                    *await call(client, base(hist, tools=in_full, thinking=None)),
                )
                forced = {"type": "tool", "name": "submit_final_report"}
                line(
                    "S1b  history has a reference, forced submit_final_report",
                    *await call(client, base(hist, thinking=None, tool_choice=forced)),
                )
                ask = [{"role": "user", "content": "Price of ACME?"}]
                forced = {"type": "tool", "name": "get_stock"}
                line(
                    "S1   forced choice of a deferred, unloaded tool",
                    *await call(client, base(ask, thinking=None, tool_choice=forced)),
                )

        # S12: parallel tool calls in one assistant message, all answered in one user message (hand-built, thinking off)
        if want("S12"):
            a = {
                "type": "tool_use",
                "id": "toolu_smoke_par_0001",
                "name": "load_tools",
                "input": {"names": ["get_weather"]},
            }
            b = {"type": "tool_use", "id": "toolu_smoke_par_0002", "name": "echo", "input": {"text": "hi"}}
            c = {"type": "tool_use", "id": "toolu_smoke_par_0003", "name": "get_stock", "input": {"ticker": "ACME"}}
            head = [
                {"role": "user", "content": ASK + " Also echo hi and get the price of ACME."},
                {"role": "assistant", "content": [a, b, c]},
            ]
            mixed = result(
                c,
                [
                    {"type": "tool_reference", "tool_name": "get_stock"},
                    {"type": "text", "text": "get_stock was not loaded. It is loaded now: call it again."},
                ],
            )
            m = head + [{"role": "user", "content": [result(a, [REF_W]), result(b, "hi"), mixed]}]
            line(
                "S12  a load, a core tool, an unloaded tool's reference beside text",
                *await call(client, base(m, thinking=None)),
            )
            # S12b: what On sends: a references-only load, a core tool's result and a text-only error result together
            together = [result(a, [REF_W]), result(b, "hi"), result(c, NOT_LOADED, is_error=True)]
            line(
                "S12b a references-only load, a core result and a text-only error result",
                *await call(client, base(head + [{"role": "user", "content": together}], thinking=None)),
            )
            # S12c: the same, with the words of a load that did not load everything after the results
            note = {"type": "text", "text": "load_tools: Not available: no_such_tool."}
            line(
                "S12c the same, then a text block after the results",
                *await call(client, base(head + [{"role": "user", "content": [*together, note]}], thinking=None)),
            )

        # S12d: S12b and S12c in production's shape: the payload builder marks the last block of the last user message
        # with cache_control. A hand-built assistant message cannot carry thinking (its blocks need the API's
        # signatures), so .1 and .2 are hand-built with thinking off, and .3 and .4 answer the three calls of a real
        # first response with adaptive thinking (its assistant message passed back unchanged).
        if want("S12d"):
            mark = {"cache_control": {"type": "ephemeral"}}
            a = {
                "type": "tool_use",
                "id": "toolu_smoke_d_0001",
                "name": "load_tools",
                "input": {"names": ["get_weather"]},
            }
            b = {"type": "tool_use", "id": "toolu_smoke_d_0002", "name": "echo", "input": {"text": "hi"}}
            c = {"type": "tool_use", "id": "toolu_smoke_d_0003", "name": "get_stock", "input": {"ticker": "ACME"}}
            head = [
                {"role": "user", "content": ASK + " Also echo hi and get the price of ACME."},
                {"role": "assistant", "content": [a, b, c]},
            ]
            marked = [result(a, [REF_W]), result(b, "hi"), result(c, NOT_LOADED, is_error=True, **mark)]
            line(
                "S12d.1 S12b with cache_control on its last block",
                *await call(client, base(head + [{"role": "user", "content": marked}], thinking=None)),
            )
            unmarked = [result(a, [REF_W]), result(b, "hi"), result(c, NOT_LOADED, is_error=True)]
            note = {"type": "text", "text": "load_tools: Not available: no_such_tool.", **mark}
            line(
                "S12d.2 S12c with cache_control on the text after the results",
                *await call(client, base(head + [{"role": "user", "content": [*unmarked, note]}], thinking=None)),
            )
            ask = [
                {
                    "role": "user",
                    "content": 'In one step, call load_tools with ["get_weather"], call echo with "hi", and call echo '
                    'again with "bye". Make all three calls now, together.',
                }
            ]
            ok, content, stop, u, err = await call(client, base(ask))
            uses = [block for block in content or [] if block.get("type") == "tool_use"]
            print(f"S12d.3 first response (adaptive thinking) | {blocks(content)} | {len(uses)} calls")
            if ok and uses:
                answers, plain = [], False
                for use in uses:
                    if use.get("name") == "load_tools":
                        answers.append(result(use, [REF_W]))
                    elif not plain:
                        answers.append(result(use, "hi"))
                        plain = True
                    else:
                        answers.append(result(use, "echo failed: smoke check.", is_error=True))
                shape = "complete" if len(uses) >= 3 and any(u.get("name") == "load_tools" for u in uses) else "partial"
                last = {**answers[-1], **mark}
                m = ask + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": [*answers[:-1], last]},
                ]
                line(f"S12d.3 the three results, cache_control on the last ({shape})", *await call(client, base(m)))
                tail = {"type": "text", "text": "load_tools: Not available: no_such_tool.", **mark}
                m = ask + [{"role": "assistant", "content": content}, {"role": "user", "content": [*answers, tail]}]
                line(f"S12d.4 the same, then a marked text block ({shape})", *await call(client, base(m)))

        # S15/S16/S10: 100 deferred tools with an index; does the model load unprompted; cost of the stubs; a load of 20
        if want("S15"):
            many = [
                tool(
                    f"svc_{i:03d}_lookup",
                    f"Look up record {i} in service {i}. " + "Details follow. " * 30,
                    {"id": {"type": "string"}},
                    ["id"],
                    **D,
                )
                for i in range(98)
            ]
            names = ", ".join(t["name"] for t in many)
            idx = tool(
                "load_tools",
                LOAD_FIRST + "get_weather (weather for a city); get_stock (price of a stock); " + names + ".",
                {"names": {"type": "array", "items": {"type": "string"}}},
                ["names"],
            )
            big = [idx, CORE, REPORT, {**WEATHER, **D}, {**STOCK, **D}, *many]
            msgs = [{"role": "user", "content": "What is the price of the stock ACME right now?"}]
            ok, content, stop, u, err = await call(client, base(msgs, tools=big))
            line("S15.1 100 deferred tools, task needs one: first call", ok, content, stop, u, err)
            use = first_use(content, "load_tools")
            if use:
                refs = [
                    {"type": "tool_reference", "tool_name": n}
                    for n in (use["input"].get("names") or [])
                    if n in {"get_stock", "get_weather"}
                ]
                if refs:
                    m2 = msgs + [
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": [result(use, refs)]},
                    ]
                    line("S15.2 after the load", *await call(client, base(m2, tools=big)))
            few = [idx, CORE, REPORT, {**WEATHER, **D}, {**STOCK, **D}]
            say_ok = [{"role": "user", "content": "Say ok."}]
            line(
                "S10.1 same loader and index, only 2 deferred tools sent", *await call(client, base(say_ok, tools=few))
            )
            line("S10.2 100 deferred tools sent", *await call(client, base(say_ok, tools=big)))
            full = [
                idx,
                CORE,
                REPORT,
                WEATHER,
                STOCK,
                *[{k: v for k, v in t.items() if k != "defer_loading"} for t in many],
            ]
            line("S10.3 the same 100 tools sent in full", *await call(client, base(say_ok, tools=full)))
            a = {
                "type": "tool_use",
                "id": "toolu_smoke_many_0001",
                "name": "load_tools",
                "input": {"names": [t["name"] for t in many[:20]]},
            }
            twenty = [{"type": "tool_reference", "tool_name": t["name"]} for t in many[:20]]
            m = [
                {"role": "user", "content": "Load the first twenty svc tools, then say ok."},
                {"role": "assistant", "content": [a]},
                {"role": "user", "content": [result(a, twenty)]},
            ]
            line("S16  one load of 20 tools", *await call(client, base(m, tools=big, thinking=None)))
    finally:
        await client.close()


asyncio.run(main())
