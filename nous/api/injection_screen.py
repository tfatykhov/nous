"""Flag-only prompt-injection and secret screen for web content (F103).

Screens the text ``web_fetch`` returns and the ``browser_task`` summary for
instructions planted for the model and for credentials. It never changes what
the model sees: a flagged result is logged and, when an event bus is wired,
recorded as an ``injection_screen_flagged`` event (rule ids, score, the URL
cut to host and first path segment, secret types and sha256 prefixes; never
the content or a secret value). Spec: docs/features/F103-injection-screen.md.

The six injection rules are ported from Asymptote Labs' agent-beacon threat
rules (rules/prompt-injection/*.rule.yaml), keeping each rule's id; the
changes from upstream are noted on each rule (one of them is split into a
hard and a soft rule). agent-beacon is MIT licensed:

    Copyright (c) 2026 Asymptote Labs

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from nous.events import Event

logger = logging.getLogger(__name__)

# Characters screened; the rest of a longer result is not looked at.
MAX_SCREEN_CHARS = 256 * 1024
EVENT_TYPE = "injection_screen_flagged"
HARD_WEIGHT = 3

Verdict = Literal["clean", "suspicious"]


@dataclass(frozen=True)
class Rule:
    id: str
    hard: bool  # one hard hit makes the result suspicious; a soft hit needs a second rule
    weight: int
    matches: Callable[[str, str], bool]  # (text, text.lower())


@dataclass(frozen=True)
class SecretHit:
    kind: str
    sha256_prefix: str  # of the matched value; the value itself is never kept


@dataclass(frozen=True)
class ScreenResult:
    score: int
    hits: tuple[str, ...]  # injection rule ids; soft hits alone are notes on a clean result
    verdict: Verdict  # injection rules only; secrets are reported beside it and never change it
    secrets: tuple[SecretHit, ...] = ()
    truncated: bool = False


def _all_of(*parts: tuple[str, tuple[str, ...]], ignore: re.Pattern[str] | None = None) -> Callable[[str, str], bool]:
    """Every pattern must match (Beacon's `a.matches(x) && a.matches(y)`), each searched on its own.

    Patterns are lowercase and run on the lowercased text. Each comes with trigger words, one of
    which every match contains: a plain substring check skips the regex on almost every page,
    which keeps a 200 KB page in single-digit milliseconds.
    """
    compiled = tuple((re.compile(pattern), triggers) for pattern, triggers in parts)

    def matches(text: str, lowered: str) -> bool:
        if ignore is not None:
            lowered = ignore.sub(" ", lowered)
        if not all(any(t in lowered for t in triggers) for _, triggers in compiled):
            return False
        return all(p.search(lowered) for p, _ in compiled)

    return matches


# --- indirect-injection-in-tool-result: unchanged from upstream.
_OVERRIDE = (
    r"(system\s+override|(ignore|disregard|forget)\s+(all\s+|any\s+)?(of\s+)?(the\s+|your\s+)?"
    r"(previous|prior|above|earlier|original)\s+(instructions|directions|rules|prompts?)|"
    r"new\s+instructions\s+for\s+(the\s+)?(ai|assistant|agent|model|llm)|"
    r"(note|message|instructions?)\s+(to|for)\s+(the\s+)?(ai|assistant|agent|model|llm)\s*:)"
)
_DOWNLOAD_AND_RUN = (
    r"(\b(curl|wget)\b[^\n|;]{0,200}\|\s*(sudo\s+)?(ba|z|da|k)?sh\b|"
    r"\b(ba|z)?sh\s+-c\s+\S{0,3}\$\(\s*(curl|wget)\b|"
    r"\b(curl|wget)\b[^\n]{0,200}\s(-o|-O|--output)\s*\S+[^\n]{0,40}(&&|;)\s*(ba|z)?sh\b|"
    r"base64\s+(-d|--decode)\b[^\n]{0,80}\|\s*(ba|z)?sh\b|"
    r"\biwr\b[^\n|]{0,200}\|\s*iex\b)"
)

# --- invisible-character-prompt-injection: upstream also flags bidi embeddings and isolates
# (U+202A-C, U+2066-9) and ZWSP/word joiner/BOM. Those are ordinary in right-to-left and
# copy-pasted web text, so only Unicode tag characters (ASCII smuggling) and the two true
# overrides (U+202D LRO, U+202E RLO) count. Emoji subdivision flags (England, Scotland, Wales)
# are a black flag followed by tag letters and a cancel tag, so they are removed first.
_INVISIBLE = re.compile("[\U000e0000-\U000e007f\u202d\u202e]")
_SUBDIVISION_FLAG = re.compile("\U0001f3f4[\U000e0030-\U000e0039\U000e0061-\U000e007a]{1,7}\U000e007f")


def _invisible(text: str, lowered: str) -> bool:
    return bool(_INVISIBLE.search(_SUBDIVISION_FLAG.sub("", text)))


# --- indirect-injection-new-instructions-marker: upstream screens prompts; here web content, where
# "Important note: you must have Python 3.12 installed" is everyday docs text. The hard rule needs an
# addressee ("for the AI", "the assistant must") or an override/secret marker; upstream's bare
# "you must" / "the model must" after an everyday marker is the soft rule below.
_MARKER = r"\s+(system\s+)?(instructions?|directives?|prompt|message|note)\b.{0,40}"
_UNADDRESSED = r":?\s*(you\s+must|the\s+model\s+(must|should|shall))"
_NEW_INSTRUCTIONS = (
    r"((new|updated|important|additional|override|special|secret)" + _MARKER + r"("
    r"for\s+(the\s+)?(ai|assistant|model|llm|agent|chatbot)|to\s+(the\s+)?(ai|assistant|model|llm|agent)|"
    r":?\s*the\s+(assistant|ai|agent)\s+(must|should|shall))|"
    r"(override|secret)" + _MARKER + _UNADDRESSED + r")"
)
_NEW_INSTRUCTIONS_UNADDRESSED = r"(new|updated|important|additional|special)" + _MARKER + _UNADDRESSED

# --- ignore-previous-instructions: unchanged, but soft (articles about prompt injection quote it).
_IGNORE_PREVIOUS = (
    r"(ignore|disregard|forget)\s+(all\s+|the\s+|your\s+)?(previous|prior|earlier|above|system)\s+"
    r"(instructions|prompts?|rules?)"
)

# --- exfiltrate-context-via-markdown-image, tightened. Upstream's instruction phrase also ends in a
# bare "url", "query string" or a markdown link "](https://", which API docs say all the time
# ("include your API key in the query string"); here it must end at an image, which renders
# without a click. Added: a markdown image whose query carries a template
# placeholder ({conversation}, $SECRET) or 64+ characters of mixed-case encoded data (what a hijacked
# browser model writes), except the signature parameters of signed CDN/S3 URLs.
_EXFIL_INSTRUCTION = _all_of(
    (
        r"(encode|append|embed|include|put|place|add)\b.{0,60}"
        r"(conversation|context|history|secret|token|api[\s_-]?key|password|env|the\s+above|previous\s+messages?)\b"
        r".{0,60}(markdown\s+image|!\[|image\s+(url|link|tag))",
        ("markdown", "![", "image"),
    )
)
# Possessive quantifiers: a long run of "![" with no closing ")" must not backtrack.
_MD_IMAGE_URL = re.compile(r"!\[[^\]\n]{0,200}+\]\((https?://[^)\s]{1,2048}+)\)")
_PLACEHOLDER = re.compile(r"^(\{[^{}\s]{1,40}\}|\$\{?[A-Z_]{3,40}\}?|<[A-Za-z_ ]{3,40}>|\[[A-Z_]{3,40}\])$")
_ENCODED = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)[A-Za-z0-9+/=_%.-]{64,}$")
_SIGNED_URL_PARAM = re.compile(
    r"^(x-amz-.*|x-goog-.*|sig|signature|jwt|expires|policy|key-pair-id|se|sp|sv|sr|st|skoid)$", re.IGNORECASE
)


def _exfil_image(text: str, lowered: str) -> bool:
    if _EXFIL_INSTRUCTION(text, lowered):
        return True
    if "![" not in text:
        return False
    for m in _MD_IMAGE_URL.finditer(text):
        try:
            query = urlsplit(m.group(1)).query
        except ValueError:  # "http://[abc": one bad URL must not switch the whole screen off
            continue
        for name, value in parse_qsl(query, keep_blank_values=True):
            if _PLACEHOLDER.match(value):
                return True
            if _ENCODED.match(value) and not _SIGNED_URL_PARAM.match(name):
                return True
    return False


# --- covert-action-injection: unchanged, soft, minus the Claude Code agent footer
# "agentId: ... (internal ID - do not mention to user. ... The agent is working in the background"
# (146 of 146 hits in the audit corpus). Nous injects no footer of this kind itself.
_COVERT_TELL = (
    r"(do\s+not|don'?t|never|without)\s+(tell|inform|notify|mention|alert|warn|disclose|reveal\s+to|let)\b.{0,30}"
    r"(the\s+)?(user|human|operator|developer|owner|them|him|her)"
)
_COVERT_HOW = (
    r"(silently|secretly|quietly|covertly|in\s+the\s+background|keep\s+(this|it)\s+(a\s+)?secret|"
    r"without\s+(any\s+)?(notice|notification|trace|disclosure)|hide\s+(this|it|the)|conceal)"
)
_HARNESS_FOOTER = re.compile(r"\(internal id - do not mention to user\.")

_OVERRIDE_WORDS = ("override", "ignore", "disregard", "forget", "instruction", "note", "message")
_MARKER_WORDS = ("instruction", "directive", "prompt", "message", "note")
RULES: tuple[Rule, ...] = (
    Rule(
        "indirect-injection-in-tool-result",
        hard=True,
        weight=HARD_WEIGHT,
        matches=_all_of((_OVERRIDE, _OVERRIDE_WORDS), (_DOWNLOAD_AND_RUN, ("curl", "wget", "base64", "iwr"))),
    ),
    Rule("invisible-character-prompt-injection", hard=True, weight=HARD_WEIGHT, matches=_invisible),
    Rule(
        "indirect-injection-new-instructions-marker",
        hard=True,
        weight=HARD_WEIGHT,
        matches=_all_of((_NEW_INSTRUCTIONS, _MARKER_WORDS)),
    ),
    Rule("exfiltrate-context-via-markdown-image", hard=True, weight=HARD_WEIGHT, matches=_exfil_image),
    Rule(
        "indirect-injection-new-instructions-unaddressed",
        hard=False,
        weight=1,
        matches=_all_of((_NEW_INSTRUCTIONS_UNADDRESSED, _MARKER_WORDS)),
    ),
    Rule(
        "covert-action-injection",
        hard=False,
        weight=2,
        matches=_all_of(
            (_COVERT_TELL, ("not", "don", "never", "without")),
            (_COVERT_HOW, ("silent", "secret", "quiet", "covert", "background", "without", "hide", "conceal")),
            ignore=_HARNESS_FOOTER,
        ),
    ),
    Rule(
        "ignore-previous-instructions",
        hard=False,
        weight=1,
        matches=_all_of((_IGNORE_PREVIOUS, ("ignore", "disregard", "forget"))),
    ),
)

# Secret-in-output detector (Beacon has none). Prefixed formats only: free-text "password: x" is everywhere on the web.
# Each pattern comes with literal triggers, one of which every match contains (as in _all_of).
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...] = (
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"), ("github_pat_",)),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"), ("sk-ant-",)),
    ("openai_key", re.compile(r"\bsk-(?:proj-[A-Za-z0-9_-]{16,}|[A-Za-z0-9]{20,})"), ("sk-",)),
    # AWS documents its example keys (AKIAIOSFODNN7EXAMPLE) all over the web.
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)(?![0-9A-Z]{9}EXAMPLE\b)[0-9A-Z]{16}\b"), ("AKIA", "ASIA")),
    ("slack_token", re.compile(r"\bxox[bp]-[0-9A-Za-z-]{10,}"), ("xoxb-", "xoxp-")),
    # The header plus the start of the body, so the hash prefix tells two keys apart.
    (
        "private_key",
        re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[A-Za-z0-9+/=\s]{0,80}"),
        ("PRIVATE KEY-----",),
    ),
)
_MAX_SECRET_HITS = 20


def _secrets(text: str) -> tuple[SecretHit, ...]:
    seen: dict[tuple[str, str], SecretHit] = {}
    for kind, pattern, triggers in _SECRET_PATTERNS:
        if not any(t in text for t in triggers):
            continue
        for m in pattern.finditer(text):
            digest = hashlib.sha256(m.group(0).encode()).hexdigest()[:8]
            seen.setdefault((kind, digest), SecretHit(kind, digest))
            if len(seen) >= _MAX_SECRET_HITS:
                return tuple(seen.values())
    return tuple(seen.values())


def screen(text: str) -> ScreenResult:
    """Screen one tool result. Pure: no logging, no I/O."""
    truncated = len(text) > MAX_SCREEN_CHARS
    if truncated:
        text = text[:MAX_SCREEN_CHARS]
    lowered = text.lower()
    hit_rules = [rule for rule in RULES if rule.matches(text, lowered)]
    suspicious = any(rule.hard for rule in hit_rules) or len(hit_rules) >= 2
    return ScreenResult(
        score=sum(rule.weight for rule in hit_rules),
        hits=tuple(rule.id for rule in hit_rules),
        verdict="suspicious" if suspicious else "clean",
        secrets=_secrets(text),
        truncated=truncated,
    )


_MAX_URL_CHARS = 120


def _bare_url(url: str) -> str:
    """Scheme, host and first path segment of an http(s) URL, at most _MAX_URL_CHARS characters.

    A query string, fragment, userinfo or deeper path can carry a token (``/reset/<token>``), and
    the "path" of a ``data:`` or ``javascript:`` URL is its content, so other schemes keep the
    scheme alone. The result is stored in nous_system.events and shown in the Activity feed.
    """
    try:
        parts = urlsplit(url.replace("\\", "/"))  # browsers read "\\" as "/" in an http(s) URL
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        bare = f"{scheme}:" if scheme else ""
    else:
        host = parts.netloc.rpartition("@")[2]  # drop userinfo
        first, _, rest = parts.path.lstrip("/").partition("/")
        bare = f"{scheme}://{host}" + (f"/{first}/…" if rest else parts.path)
    return bare if len(bare) <= _MAX_URL_CHARS else bare[: _MAX_URL_CHARS - 1] + "…"


async def screen_tool_result(
    text: str, *, source: str, url: str, settings: Any, bus: Any = None
) -> ScreenResult | None:
    """Screen a web tool result in flag mode. Never raises; the caller returns its content unchanged.

    Returns None when the screen is off or failed. ``settings.injection_screen_mode`` is read per call.
    """
    mode = getattr(settings, "injection_screen_mode", "flag")
    if mode == "off":
        return None
    # TODO(F103 annotate mode): a future `annotate` mode would return a note for the caller to
    # prepend to a suspicious result. Flag mode only reports; nothing here may change the content.
    try:
        # Off the event loop: a pathological 256 KiB page takes a few hundred milliseconds.
        result = await asyncio.to_thread(screen, text)
        where = _bare_url(url)
        if result.verdict == "clean" and not result.secrets:
            if result.hits:
                logger.info(
                    "injection_screen note: source=%s url=%s soft rules=%s", source, where, ",".join(result.hits)
                )
            return result
        logger.warning(
            "injection_screen flagged: source=%s url=%s verdict=%s score=%d rules=%s secrets=%s",
            source,
            where,
            result.verdict,
            result.score,
            ",".join(result.hits) or "-",
            ",".join(f"{s.kind}:{s.sha256_prefix}" for s in result.secrets) or "-",
        )
    except Exception:
        logger.exception(
            "injection_screen failed (source=%s url=%s); the result passes through unscreened", source, _bare_url(url)
        )
        return None
    if bus is not None:
        try:
            await bus.emit(
                Event(
                    type=EVENT_TYPE,
                    agent_id=getattr(settings, "agent_id", ""),
                    data={
                        "source": source,
                        "url": where,
                        "verdict": result.verdict,
                        "score": result.score,
                        "rules": list(result.hits),
                        "secrets": [{"kind": s.kind, "sha256_prefix": s.sha256_prefix} for s in result.secrets],
                        "mode": mode,
                        "content_chars": len(text),
                        "truncated": result.truncated,
                    },
                )
            )
        except Exception:
            logger.exception("injection_screen: emitting %s failed (source=%s)", EVENT_TYPE, source)
    return result
