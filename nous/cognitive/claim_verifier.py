"""claim_verifier.py — F026 Execution Integrity Phase D.

ClaimVerifier: detects ungrounded action claims in assistant responses.
IntentTracker: detects ghost planning (describing work without doing it).
"""
from __future__ import annotations

import ast
import builtins
import functools
import posixpath
import re
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nous.cognitive.bash_side_effect import (
    _ASSIGNMENT,
    READ_COMMANDS,
    command_invocations,
    command_string,
    git_subcommand,
    program_name,
    simple_commands,
)
from nous.cognitive.execution_ledger import EVIDENCE_TRUNCATED, Invocation, bash_invocations

if TYPE_CHECKING:
    from nous.cognitive.execution_ledger import ExecutionLedger

_UNREAD: Any = object()  # sentinel: read the invocations from ``args`` when first needed


@dataclass
class ClaimViolation:
    """A single ungrounded action claim found in the assistant response."""

    claimed_text: str
    expected_tool: str
    found_in_turn: bool
    found_in_ledger: bool
    capable_tools: tuple[str, ...] = ()  # every tool that could have grounded it
    # "plausible": a call could have done it but does not show it did (verify_report: not confirmed)
    evidence: str = "none"


@dataclass(frozen=True)
class Evidence:
    """One successful tool call a claim may be grounded in."""

    tool_name: str
    args: Mapping[str, str] = field(default_factory=dict)
    exit_code: int | None = None   # bash: a non-zero exit is not evidence
    side_effect: str = "write"
    # bash: what the command runs when already read -- the ledger reads it at
    # record time from the WHOLE command; None = unreadable
    invocations: tuple[Invocation, ...] | None = field(default=_UNREAD, repr=False, compare=False)
    # False: the call failed or was blocked -- evidence only that it was TRIED
    # (a report's verdict on an action, verify_report)
    ok: bool = True
    # bash: the job status a `runner.sh status|result` output stated (execution_ledger.observed_status)
    observed_status: str | None = None

    @cached_property
    def runs(self) -> tuple[Invocation, ...] | None:
        """What a bash command runs, read once per evidence item; None = unreadable."""
        if self.invocations is not _UNREAD:
            return self.invocations
        # this turn's evidence: the full command, nothing capped
        return bash_invocations(self.args.get("command") or self.args.get("cmd") or "",
                                self.exit_code, bound=False)


@dataclass(frozen=True)
class ClaimCheck:
    """One extracted claim and the best evidence found for it."""

    kind: str
    text: str
    evidence: str  # "exact" | "plausible" | "none"


@dataclass
class VerificationResult:
    """Outcome of verifying all action claims in one response."""

    verified: bool
    violations: list[ClaimViolation] = field(default_factory=list)
    correction: str | None = None
    claims: list[ClaimCheck] = field(default_factory=list)


@dataclass(frozen=True)
class ClaimKind:
    primary_tool: str          # kept as ClaimViolation.expected_tool (back-compat)
    capable_tools: frozenset[str]


CLAIM_KINDS: dict[str, ClaimKind] = {
    "file_write": ClaimKind("write_file", frozenset({
        "write_file", "bash", "run_python",
        "compose_surface", "push_surface", "learn_fact", "ingest_document"})),
    "email": ClaimKind("send_email", frozenset({
        "send_email", "send_file", "bash", "run_python", "compose_surface", "push_surface"})),
    "vcs_push": ClaimKind("bash", frozenset({"bash", "run_python"})),
    "vcs_commit": ClaimKind("bash", frozenset({"bash", "run_python"})),
    "deploy": ClaimKind("bash", frozenset({"bash", "run_python"})),
}


@dataclass(frozen=True)
class Claim:
    kind: str
    text: str
    target: str | None = None  # a path or recipient the claim names
    span: tuple[int, int] = field(default=(0, 0), compare=False)
    attempt_only: bool = False  # a verdict on an action: any recorded try of it backs the claim
    clause: str = field(default="", compare=False)  # the clause the claim is in ("I sent it by email")
    sentence: str = field(default="", compare=False)  # its clause to the end of the sentence (a send_file's scope)
    opening: str = field(default="", compare=False)  # the sentence before its clause ("Done:", "For Bob,")


# First person: "I", "I've", "I have", "I just", "I also" ... The "I" is
# case-SENSITIVE inside patterns compiled IGNORECASE: a lowercase "i " is not a
# subject.
_FIRST = (r"(?<!\bas )\b(?-i:I)(?:['’]ve|\s+have)?"  # "As I wrote earlier, ..." narrates
          r"(?:\s+(?:just|already|also|then|now|successfully|finally)){0,2}\s+")
_CLAIM_VERBS = (r"(?:saved|wrote|written|created|generated|exported|stored|sent|emailed"
                r"|forwarded|mailed|pushed|committed|(?:re)?deployed)\b")
# The second clause of a FIRST-PERSON compound ("I saved X and sent Y"); only
# accepted when a first-person CLAIM ("I" + claim verb) appears earlier in the
# same CLAUSE -- "I checked: the DAG ran and sent the email" and "As I wrote
# earlier, the DAG finished and sent it" are narration.
_AND = r"\band\s+(?:also\s+)?"
# Words that start a new clause: what follows them is not the verb's object.
_STOP = (r"(?!\b(?:that|which|who|whose|to|so|because|since|when|while|where|if|unless"
         r"|for|and|but|then|via|by|as)\b)")
# The object of a claim, inside one clause. A dot ends it only when followed
# by a non-space, so `config.yaml` and `v1.2` stay inside; "e.g." / "i.e." /
# "etc." do not end it. The abbreviation branches take ONLY a dot followed by
# whitespace, so they never overlap `\.(?=\S)` -- overlapping branches
# backtrack exponentially on a failed match ("etc.," x20 took 4.5 s;
# exclusive branches: 0.03 ms).
_OBJECT = (rf"(?:{_STOP}(?:[^.\n;,—]|\.(?=\S)|(?<=\be\.g)\.(?=\s)|(?<=\bi\.e)\.(?=\s)"
           r"|(?<=\betc)\.(?=\s))){0,80}?")
# The object of a TARGETED claim ("saved <the report> to <path>"): short, so a
# path named later in the sentence is not its target.
_OBJ = rf"(?:{_STOP}(?:[^.\n;,(—]|\.(?=\S))){{0,40}}?"
# A path (absolute, `~/`, `./`, or `dir/file.ext`), or a bare file name; an
# extension starts with a letter (`v1.2` and `3.30pm` are not files).
_PATH = (r"(?P<target>(?:~|\.{0,2})/[\w./-]*\w|(?:[\w.-]+/)+[\w-]+\.[A-Za-z]\w{0,5}"
         r"|[\w-]+\.[A-Za-z]\w{0,5})")
_ADDRESS = r"(?P<target>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)"
_SUBJECT = rf"(?P<subj>{_FIRST}|{_AND})"
# The effect landed somewhere no file / git / deploy tool reaches: memory, a
# fact, the companion app, a micro-app, or the reply itself ("the report
# below"). A Nous artifact counts only as the HEAD of its phrase: "the
# dashboard changes" and "the surface renderer fix" are commits.
_ARTIFACT = (r"(?:companion(?:\s+app)?|dashboards?|micro-?apps?|surfaces?|cards?)"
             r"(?!\s+(?:changes?|fix(?:es)?|repo(?:sitory)?|branch|renderer|fixtures?|code|pwa"
             r"|app|components?|tests?|bugs?|features?|modules?|files?|PR|pipeline|build"
             r"|package|directory|folder|source|css|js|ts|svelte|refactor|update|patch)\b)"
             r"(?![/\\-]|\.\w)")  # not a path such as /tmp/dashboard.md; a final "." is fine
_NOT_ELSEWHERE = (r"(?!(?:[^.\n;]|\.(?=\S)){0,80}?"
                  r"(?:\b(?:to|in|into|on|as)\s+(?:your\s+|my\s+|the\s+|a\s+|an\s+|this\s+)?"
                  rf"(?:memory|chat|facts?|knowledge\s+base|repl(?:y|ies)|response|{_ARTIFACT})\b"
                  r"|\b(?:a|an|the|your|my|this|new|live|report|summary|health|status|interactive)"
                  rf"\s+(?:(?:live|report|summary|health|status|interactive)\s+)?{_ARTIFACT}"
                  r"|\b(?:report|summary|draft|notes?|text|content|version|it)\s+(?:below|above)\b))")
_DET = r"(?:(?:the|a|an|my|our|your|this|that|these|those|all|some|both|every|each)\s+)?"
_VCS_NOUN = (r"(?:branch(?:es)?|commits?|fix(?:es)?|changes?|patch(?:es)?|PRs?|pull\s+requests?"
             r"|tags?|code|repo(?:sitory)?|remote|origin|main|master|upstream|refactor|features?"
             r"|hotfix(?:es)?|updates?|work|files?|edits?|diffs?|version|release|migrations?"
             r"|tests?|docs|latest|v\d[\w.-]*|fixtures?|configs?|scripts?|schemas?|models?"
             r"|components?|modules?|assets?|templates?|specs?|manifest|lockfile|snapshots?"
             r"|renames?|cleanup|typos?|readme|changelog|deps|dependencies|workflow|ci)")
# A push "to Friday", "to the wiki", "to the device" is not a git push -- but
# "to the client repo", "to the team branch", "to 3 remotes" is.
_NOT_A_GIT_DESTINATION = (r"(?![^.\n;]{0,40}?\b(?:to|until|till)\s+(?:next|tomorrow|later|monday"
                          r"|tuesday|wednesday|thursday|friday|saturday|sunday|\d|q[1-4]\b|the\s+"
                          r"(?:wiki|device|afternoon|morning|evening|weekend|team|user|client)"
                          r"|january|february|march|april|may|june|july|august|september|october"
                          r"|november|december)(?!\S*\s+(?:repo(?:sitory)?|branch|remotes?)\b))")
# What was pushed or committed must be a version-control object: the head of
# the object phrase, or a bare pronoun -- "I pushed the fix", "I pushed it",
# "I committed and pushed", never "I pushed back", "I pushed an approval
# card", "I committed to a code freeze".
_VCS_OBJECT = (rf"{_NOT_A_GIT_DESTINATION}"
               r"(?=\s+(?!(?:for|back|hard|on|through|ahead|past|against|forward|toward|towards"
               r"|into|in|out|over|off|away|to|at)\b)"
               rf"{_DET}(?:[\w-]+\s+){{0,2}}?{_VCS_NOUN}\b"
               r"|\s+(?:it|them|that|this|those|these|everything|all)\b"
               r"(?:\s+(?:up|too|as\s+well|again))?\s*(?:[.;:!?,)]|$|\b(?:and|to|onto|up)\b)"
               r"|\s+to\s+(?:origin|github|gitlab|bitbucket|upstream|main|master"
               r"|the\s+(?:remote|repo(?:sitory)?|branch|server))\b"
               r"|\s+with\b|\s*(?:[.;:!?,)]|$)|\s+(?:and|then)\b|\s+—)")
# What was deployed: a build/release/service, a pronoun, a destination, or
# nothing at all ("I deployed.").
_DEPLOY_OBJECT = (rf"(?=\s+{_DET}(?:[\w-]+\s+){{0,2}}?(?:builds?|releases?|fix(?:es)?|changes?"
                  r"|services?|apps?|applications?|sites?|versions?|images?|containers?|updates?"
                  r"|code|branch|hotfix(?:es)?|patch(?:es)?|migrations?)\b"
                  r"|\s+(?:it|them|that|this|everything)\b|\s+to\s+\S|\s*(?:[.;:!?,)]|$))")
# Line- or sentence-initial: "Email sent to x", "Done. Saved to /tmp/x.md".
_OPENING = r"(?:(?<![^\n])|(?<=[.!?]\s)|(?<=[✓✅]\s))"
# "... by the scheduled task": someone else's completion, not the agent's;
# "by 9am", "by Friday", "by me" and a channel ("by email", "by text", "by Slack") are not; post, a courier
# or a hand is someone else's delivery. A channel is one only as the whole phrase: a mark, the end or
# to/at/on/for/with/and follows it; "by Slack's reminder bot", "by Telegram bot" name another actor.
# Dots inside words pass, or the target would shrink to `/srv/q3` to keep
# ".md by" out of reach.
_NOT_BY = (r"(?!(?:[^.\n;]|\.(?=\S)){0,60}?\bby\s+(?!me\b|myself\b|\d|noon\b|midnight\b|tonight\b"
           r"|tomorrow\b|today\b|end\b|eod\b|eob\b|cob\b|then\b|now\b|the\s+end\b|next\b|lunch\b"
           r"|this\s+(?:morning|afternoon|evening|week)\b|(?:mon|tues|wednes|thurs|fri|satur|sun)day\b"
           r"|(?:(?:e-?)?mail|text|sms|telegram|dms?|slack|whatsapp|signal|discord|imessage|teams|messenger|wechat"
           r"|gmail|outlook)(?=[ \t]*(?:[.,;:!?()\]\"”—–\n]|$)|\s+(?:to|at|on|for|with|and)\b))[\w$])")
_MESSAGE = r"\b(?:e-?mail|message|report|note|reminder|summary|heads-up|invite|notification|alert|digest)\b"

# (pattern, kind). Patterns that open with the subject group are claims only
# under the first-person rule in _extract_claims; the targeted file pattern
# runs first so its target wins over the untargeted one on the same span.
_CLAIM_PATTERNS: list[tuple[str, str]] = [
    (rf"{_SUBJECT}(?:saved|wrote|written|exported|stored)\b{_NOT_ELSEWHERE}{_OBJ}"
     rf"\s+(?:to|at|in|into)\s+{_PATH}", "file_write"),
    (rf"{_SUBJECT}(?:saved|wrote|written|created|generated)\b{_NOT_ELSEWHERE}{_OBJECT}"
     r"\b(?:file|document|report)\b", "file_write"),
    # actor-less completion: past tense ("was saved to") or an opening ("Saved to")
    (rf"\b(?:was|were|been|got)\s+(?:saved|written)\s+to[:\s]+{_PATH}{_NOT_BY}", "file_write"),
    (rf"{_OPENING}(?:saved|written)\s+to[:\s]+{_PATH}{_NOT_BY}", "file_write"),
    # a named recipient is the target, so a send to someone else is not
    # evidence; the address-first form runs first so its target wins
    (rf"{_SUBJECT}(?:sent|forwarded)\b{_NOT_ELSEWHERE}\s+{_ADDRESS}\b", "email"),
    (rf"{_SUBJECT}(?:sent|forwarded)\b{_NOT_ELSEWHERE}{_OBJECT}{_MESSAGE}"
     rf"(?:\s+to\s+{_ADDRESS})?", "email"),
    # "I sent it to Tim by email": the object may run through "to <person>"
    (rf"{_SUBJECT}(?:sent|forwarded)\b{_NOT_ELSEWHERE}(?:[^.\n;,—]|\.(?=\S)){{0,60}}?\bby\s+e-?mail\b",
     "email"),
    # "I emailed the report to alice@x.io": the recipient after the object
    (rf"{_SUBJECT}(?:e-?mailed|mailed)\b{_NOT_ELSEWHERE}(?:[^.\n;,—]|\.(?=\S)){{0,60}}?\s+to\s+{_ADDRESS}",
     "email"),
    (rf"{_SUBJECT}(?:e-?mailed|mailed)\b{_NOT_ELSEWHERE}(?:\s+{_ADDRESS}|(?=\s+\w))", "email"),
    (rf"{_OPENING}e-?mail(?:ed)?\s+sent\s+to\b(?:\s+{_ADDRESS})?{_NOT_BY}", "email"),
    (rf"\be-?mail\s+(?:was|has\s+been|got)\s+sent\s+to\b(?:\s+{_ADDRESS})?{_NOT_BY}", "email"),
    (rf"{_SUBJECT}pushed\b{_NOT_ELSEWHERE}{_VCS_OBJECT}", "vcs_push"),
    (rf"{_SUBJECT}committed\b{_NOT_ELSEWHERE}{_VCS_OBJECT}", "vcs_commit"),
    (rf"{_SUBJECT}(?:re)?deployed\b{_NOT_ELSEWHERE}{_DEPLOY_OBJECT}", "deploy"),
]
_FIRST_RE = re.compile(_FIRST, re.IGNORECASE)
_FIRST_CLAIM_RE = re.compile(_FIRST + _CLAIM_VERBS, re.IGNORECASE)
# Where a clause ends, for the `and` rule and the question rule; a comma
# right before "and" joins.
_CLAUSE_END = re.compile(r"[.!?](?=\s|$)|\n|[;:—]|,(?!\s*and\b)")
# Where a sentence ends, for what a send_file claim is judged by: a full stop, a blank line or a list item; but
# not the stop of an abbreviation ("to the user, i.e. Bob", "No. 5"), nor a line break right after "user" or "bot"
# (main reads "the user ..." as a recipient whatever follows it).
# (checked only where the stop is; a lookbehind's alternatives must be one width, so they are grouped by length)
_SENTENCE_END = re.compile(r"[!?](?=\s|$)|\.(?=\s|$)(?<!\b(?:cf|vs|dr|mr|ms)\.)(?<!\b(?:i\.e|e\.g|viz|etc|mrs)\.)"
                           r"(?<!\bprof\.)(?<!\ba\.k\.a\.)(?<!\bapprox\.)(?!(?<=\bno\.)\s*\d)"
                           r"|(?<!\buser)(?<!\buser )(?<!\bbot)(?<!\bbot )"
                           r"(?:\n[ \t]*\n|\n(?=[ \t]*(?:[-*+•]|\d+[.)])\s))", re.IGNORECASE)
# A condition earlier in the clause: "if I pushed", "check whether it was saved".
_CONDITION = re.compile(r"\b(?:if|whether|unless)\b", re.IGNORECASE)
# Text that is not the agent speaking: fenced code, block quotes, quotations.
_QUOTED = re.compile(r"```.*?(?:```|\Z)|^[ \t]*>[^\n]*|\"[^\"\n]{0,300}\"|“[^”\n]{0,300}”",
                     re.DOTALL | re.MULTILINE)


def _sentence_end(sentence_ends: list[int], clause_end: int, text: str) -> int:
    """Where the sentence holding a clause that ends at ``clause_end`` ends."""
    i = bisect_left(sentence_ends, clause_end)
    return sentence_ends[i] if i < len(sentence_ends) else len(text)


def _sentence_start(sentence_starts: list[int], clause_start: int) -> int:
    """Where the sentence holding a clause that starts at ``clause_start`` starts."""
    i = bisect_right(sentence_starts, clause_start) - 1
    return sentence_starts[i] if i >= 0 else 0


def _blank(match: re.Match[str]) -> str:
    """Same length, newlines kept: offsets and sentence ends stay put.

    A quoted work id (`job "job-…"`) is the agent naming it, not a quotation: it is kept, in backticks."""
    quoted = match.group(0)
    inner = quoted[1:-1]
    if quoted[0] in "\"“" and _QUOTED_ID.fullmatch(inner):
        return f"`{inner}`"
    return re.sub(r"[^\n]", " ", quoted)

# The signal an argument must carry for a capable tool to count as evidence.
# Every pattern is linear: this turn's evidence is untruncated and is scanned
# on the event loop, so no unbounded class may be followed by a search.
_PY_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")  # an address written out in code

# Nous's own producers: a claim with no named path or recipient may be about
# what they made ("I created the report" = a micro-app; "I sent you the
# summary" = a surface). They never ground a claim that names a target.
_PRODUCERS = frozenset({"compose_surface", "push_surface", "learn_fact", "ingest_document"})

_MAIL_CLIENTS = frozenset({"mail", "mailx", "mutt"})  # also READ a mailbox: need a recipient
_MAIL_SENDERS = frozenset({"sendmail", "msmtp", "ssmtp", "swaks"})
_TRANSFERS = frozenset({"rsync", "scp", "ansible-playbook"})
_DEPLOY_CLIS = frozenset({
    "docker", "docker-compose", "podman", "kubectl", "helm", "kustomize", "skaffold", "systemctl",
    "service", "supervisorctl", "pm2", "terraform", "pulumi", "ansible", "gcloud", "aws", "az",
    "vercel", "netlify", "fly", "flyctl", "railway", "firebase", "wrangler", "serverless", "sls",
    "heroku", "eb", "nomad", "cap",
})
# A deploy CLI's subcommand that CHANGES something: `kubectl get pods`,
# `docker logs`, `aws s3 ls` look at a deployment.
_DEPLOY_CHANGE = frozenset({
    "deploy", "up", "apply", "rollout", "restart", "start", "stop", "reload", "install", "upgrade",
    "rollback", "publish", "release", "promote", "push", "run", "scale", "set", "sync", "cp",
    "destroy", "update", "create", "patch", "replace", "expose", "enable", "disable",
    "daemon-reload", "update-service", "update-function-code", "create-deployment",
    "container:release", "container:push", "releases:rollback", "ps:restart",
})
# How many leading operands may carry a deploy CLI's verb: `docker compose
# up`, `aws s3 sync`, `service nginx restart` (2); `kubectl rollout`,
# `systemctl restart`, `pm2 restart` (1, the default).
_DEPLOY_VERB_DEPTH = {"docker": 2, "docker-compose": 2, "podman": 2, "aws": 2, "gcloud": 2, "az": 2,
                      "cap": 2, "service": 2}
_TASK_RUNNERS = frozenset({
    "make", "just", "npm", "npx", "pnpm", "yarn", "uv", "poetry", "pipenv", "pdm", "hatch", "tox",
    "nox", "invoke", "fab", "rake", "gradle", "mvn",
})
_PY_RUNNERS = frozenset({"uv", "poetry", "pipenv", "pdm", "hatch"})  # `uv run python ...`
_INTERPRETERS = frozenset({"python", "python3", "node", "deno", "bun", "perl", "ruby", "php"})
# Programs whose command string could not be read (see command_invocations).
_RUNNERS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "su", "eval", "env", "flock", "watch",
                      "ssh"})
_SCRIPT = re.compile(r"\.(?:sh|bash|zsh|py|js|mjs|ts|rb|pl|php)\Z")
# A test run cannot have pushed, sent or deployed anything on purpose.
_TESTY = re.compile(r"(?:^|[\s/])(?:pytest|unittest|tox|nox)\b|(?:^|/)tests?/|(?:^|/)test_\w*\.py\b"
                    r"|_test\.(?:py|js|ts)\b|\.spec\.[jt]s\b")
# What a TASK RUNNER must mention for its run to possibly have produced the
# claimed effect: `just release` may have pushed; `make lint` cannot have.
_HINTS = {
    "vcs_push": re.compile(r"push|commit|git|release|publish|deploy|ship|bump|version|tag", re.I),
    "vcs_commit": re.compile(r"push|commit|git|release|publish|deploy|ship|bump|version|tag", re.I),
    "email": re.compile(r"mail|smtp|send|notify|telegram|@|message|alert|digest|report|brief"
                        r"|dispatch|post|outreach|slack|announce|deliver|share|publish", re.I),
    "deploy": re.compile(r"deploy|release|rollout|restart|publish|ship|\bup\b|apply|start|serve"
                         r"|launch|promote", re.I),
    "file_write": re.compile(r"write|save|export|report|generat|build|render|\bout|dump|convert"
                             r"|create|compile|backup|plot|chart|fig|draw|snapshot|archive|pack",
                             re.I),
}
_LEVELS = {"none": 0, "plausible": 1, "exact": 2}


# What Python code DOES, read from its syntax tree so that a comment or a
# string ("open('/tmp/x', 'w')" printed) never counts: the destinations it
# writes, whom it sends to, the shell strings it runs. Code that does not
# parse falls back to the regexes over comment-stripped text.
_PY_WRITE_METHODS = frozenset({
    "to_csv", "to_json", "to_excel", "to_parquet", "to_html", "to_markdown", "to_pickle", "to_feather",
    "to_hdf", "savefig", "save", "savetxt", "imwrite", "imsave", "write_html", "write_image",
    "write_text", "write_bytes",
})
# keyword names a writing call takes its destination by
_PY_WRITE_KWARGS = ("path", "fname", "filename", "fp", "path_or_buf", "file", "buf", "excel_writer",
                    "dst", "fp_or_buf")
_PY_OPENERS = frozenset({"open", "io.open", "codecs.open"})
_PY_ARCHIVE_OPENERS = frozenset({"zipfile.ZipFile", "tarfile.open", "gzip.open", "bz2.open", "lzma.open",
                                 "h5py.File"})
_PY_COPIES = frozenset({"shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.move", "shutil.copytree",
                        "os.rename", "os.replace", "os.renames", "os.link", "os.symlink"})
_PY_ARCHIVE_SUFFIX = {"zip": "zip", "tar": "tar", "gztar": "tar.gz", "bztar": "tar.bz2", "xztar": "tar.xz"}
# a shell call is one QUALIFIED by its module (or an alias proven by an
# import): a bare `run(...)` is whatever the script defines
_PY_SHELL_CALLS = frozenset({
    "subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
    "subprocess.Popen", "subprocess.getoutput", "subprocess.getstatusoutput", "os.system", "os.popen",
    "asyncio.create_subprocess_shell",
})
_PY_DEPLOY_MODULES = frozenset({"docker", "kubernetes", "boto3", "paramiko", "fabric", "ansible"})
# A call into a library this reader does not know, named like the effect
# (`yag.send(to=...)`, `repo.git.push()`): plausible, never exact -- the
# same standing as a script whose NAME hints at the effect (_HINTS).
_PY_CALLEE_HINTS = {
    # a method plainly about mail; a bare `.send(...)` only with a recipient
    # argument or a receiver named for messaging (`sock.send`, `gen.send`,
    # `conn.send` are not mail)
    "email": re.compile(r"mail|telegram|slack|smtp|deliver", re.I),
    "email_send": re.compile(r"^send", re.I),
    "email_receiver": re.compile(r"mail|msg|message|sms|notif|bot|ses|sg|slack|telegram", re.I),
    "vcs_push": re.compile(r"^push$", re.I),
    "vcs_commit": re.compile(r"^commit$", re.I),
    "deploy": re.compile(r"deploy|rollout", re.I),
}
_PY_RECIPIENT_KWARGS = ("to", "recipients", "to_addrs", "to_emails", "rcpt", "destination")


@dataclass
class _PyFacts:
    writes: list[str] = field(default_factory=list)   # literal destinations written
    write_unknown: bool = False                       # a write to a computed destination
    sends: bool = False
    recipients: set[str] = field(default_factory=set)
    recipient_unknown: bool = False                   # a recipient held in a variable
    # what it runs through a shell / subprocess, read like bash (argv kept)
    invocations: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    checked_invocations: set[int] = field(default_factory=set)  # indices in invocations that propagate failure
    run_unknown: bool = False                         # a subprocess whose argv is not written out
    deploys: bool = False
    hints: set[str] = field(default_factory=set)      # claim kinds an unknown callee is named for


def _dotted(node: ast.AST) -> str:
    """`shutil.copy` for the callee of a call, `.to_csv` for a method."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else f".{node.attr}"
    return ""


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _literals(node: ast.AST | None) -> list[str] | None:
    """The strings of a string or list/tuple-of-strings literal; None otherwise."""
    if (s := _literal(node)) is not None:
        return [s]
    if isinstance(node, (ast.List, ast.Tuple)):
        items = [_literal(e) for e in node.elts]
        return [i for i in items if i is not None] if all(i is not None for i in items) else None
    return None


def _constant_truth(test: ast.AST) -> bool | None:
    """The value of an `if` test known without running: `False`, `0`,
    `True`, `__name__ == "__main__"` (a script); None when it is not."""
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
            and test.left.id == "__name__" and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq) and _literal(test.comparators[0]) == "__main__"):
        return True
    return None


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_TRY = (ast.Try, getattr(ast, "TryStar", ast.Try))
_EXIT_CALLS = frozenset({"sys.exit", "exit", "quit", "os._exit", "os.abort"})


def _exception_name(node: ast.AST | None) -> str:
    """The class a `raise` names when it is written out (`RuntimeError()`,
    `RuntimeError`, `errors.Fatal`); "" for a bare `raise` or a variable
    (`raise err`), whose class the reader cannot see."""
    if isinstance(node, ast.Call):
        node = node.func
    name = _dotted(node) if node is not None else ""
    return name if name.rsplit(".", 1)[-1][:1].isupper() else ""


def _catches(handler_type: ast.AST | None, raised: str) -> bool | None:
    """Whether an `except` clause catches an exception of class ``raised``:
    known for a bare clause, `BaseException`, the same name, or two builtin
    classes (their hierarchy is fixed); None when it depends on a class the
    reader cannot see (`except Other:` may name a base of `Fatal`)."""
    if handler_type is None:
        return True
    names = [_dotted(n) for n in (handler_type.elts if isinstance(handler_type, ast.Tuple) else [handler_type])]
    if "BaseException" in names:
        return True
    if not raised:
        return None
    raised_class = getattr(builtins, raised, None)
    known = isinstance(raised_class, type) and issubclass(raised_class, BaseException)
    verdicts: list[bool | None] = []
    for name in names:
        if name == raised:
            return True
        handler_class = getattr(builtins, name, None)
        if known and isinstance(handler_class, type) and issubclass(handler_class, BaseException):
            if issubclass(raised_class, handler_class):
                return True
            verdicts.append(False)
        else:
            verdicts.append(None)
    return False if all(v is False for v in verdicts) else None


def _handler_taken(handlers: list[ast.ExceptHandler], raised: set[str]) -> tuple[ast.ExceptHandler | None, bool]:
    """When a `try` body certainly raises one of ``raised``: (the handler that
    certainly runs, whether the exception certainly escapes them all). (None,
    False) once a handler is reached that may or may not catch it."""
    for handler in handlers:
        verdicts = {_catches(handler.type, r) for r in raised}
        if verdicts == {True}:
            return handler, False
        if verdicts != {False}:
            return None, False
    return None, True


def _breaks(body: list[ast.stmt]) -> bool:
    """A `break` that leaves THIS loop (not one nested inside it)."""
    stack = list(body)
    while stack:
        stmt = stack.pop()
        if isinstance(stmt, ast.Break):
            return True
        if isinstance(stmt, ast.If):
            stack.extend(stmt.body + stmt.orelse)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            stack.extend(stmt.body)
        elif isinstance(stmt, _TRY):
            stack.extend(stmt.body + stmt.orelse + stmt.finalbody)
            stack.extend(s for h in stmt.handlers for s in h.body)
        elif isinstance(stmt, ast.Match):
            stack.extend(s for case in stmt.cases for s in case.body)
    return False


def _exit_kinds(stmt: ast.stmt) -> frozenset[str]:
    """How a statement CERTAINLY leaves the body it is in -- {"return"},
    {"exit"}, {"raise:Class"} (`raise:` alone when the class is unknown),
    {"loop"} (a `while True` with no `break`: never) -- or empty when control
    may continue past it. A compound statement leaves when its taken side
    does (a constant `if`, a `with`), or when both sides of an `if` do; a
    `try` when its body returns, or raises what no handler catches, or what
    its certain handler re-raises."""
    if isinstance(stmt, ast.Return):
        return frozenset({"return"})
    if isinstance(stmt, ast.Raise):
        return frozenset({"raise:" + _exception_name(stmt.exc)})
    if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and _dotted(stmt.value.func) in _EXIT_CALLS):
        return frozenset({"exit"})
    if isinstance(stmt, ast.If):
        truth = _constant_truth(stmt.test)
        body, orelse = _body_exit_kinds(stmt.body), _body_exit_kinds(stmt.orelse)
        if truth is True:
            return body
        if truth is False:
            return orelse
        return body | orelse if body and orelse else frozenset()
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return _body_exit_kinds(stmt.body)
    if isinstance(stmt, ast.While) and _constant_truth(stmt.test) is True and not _breaks(stmt.body):
        return _body_exit_kinds(stmt.body) or frozenset({"loop"})
    if isinstance(stmt, ast.Try):
        kinds = _body_exit_kinds(stmt.body)
        raised = {k[6:] for k in kinds if k.startswith("raise:")}
        if not raised:
            return kinds  # a return or exit passes through `finally` and leaves
        if len(raised) < len(kinds):
            return frozenset()  # a raise on one path, a return on another: unknown
        taken, escapes = _handler_taken(stmt.handlers, raised)
        if escapes:
            return kinds
        return _body_exit_kinds(taken.body) if taken is not None else frozenset()
    return frozenset()


def _body_exit_kinds(body: list[ast.stmt]) -> frozenset[str]:
    for stmt in body:
        if kinds := _exit_kinds(stmt):
            return kinds
    return frozenset()


def _reachable(body: list[ast.stmt]) -> list[ast.stmt]:
    """A body up to and including the first statement that certainly leaves
    it (see ``_exit_kinds``); what follows never runs."""
    for i, stmt in enumerate(body):
        if _exit_kinds(stmt):
            return body[:i + 1]
    return body


def _statically_empty(node: ast.AST) -> bool:
    """`[]`, `()`, `{}`, `""`, `range(0)`: an iterable known to be empty."""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and not node.elts:
        return True
    if isinstance(node, ast.Dict) and not node.keys:
        return True
    if isinstance(node, ast.Constant) and not node.value:
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "range"
            and len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and node.args[0].value == 0)


def _executed(tree: ast.Module) -> list[ast.AST]:
    """The nodes a script RUNS: module-level statements (a class body's own
    statements included), the bodies of the functions and methods they call
    (transitively), up to an unconditional `return`/`raise`/`exit`, and the
    taken side of a constant `if`/`while`/`for`. A call resolves to ITS
    definition: a bare `save()` to the module's `save`, `Obj().save()` or
    `x = Obj(); x.save()` to Obj's method, `self.save()` to the enclosing
    class; an ambiguous receiver is withheld. A function merely defined, a
    lambda, a method never called, `if False:`, an `except` handler -- never.

    SCOPE. This is not an interpreter: it follows constant control flow and
    literal constructor assignments, nothing that needs a value at runtime.
    What it cannot decide it reports as uncertain, which the verifier reads
    as "plausible" -- never a false violation."""
    functions: dict[str, list[ast.AST]] = {}
    methods: dict[str, dict[str, ast.AST]] = {}  # method name -> class name -> def
    classes: set[str] = set()
    instances: dict[str, list[tuple[int, str]]] = {}  # `x = Cls(...)`: the variable's classes, by line
    for node in tree.body:
        if isinstance(node, _FUNCTIONS):
            functions.setdefault(node.name, []).append(node)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.add(node.name)
            for item in node.body:
                if isinstance(item, _FUNCTIONS):
                    methods.setdefault(item.name, {})[node.name] = item
    # `x = Cls(...)` on a LIVE path (an `if False:` never assigns), each call
    # reading the latest assignment before it in document order; a call
    # inside a called function may reveal more, so resolution repeats until
    # the instances settle
    for _ in range(3):
        executed, seen = _resolve(tree, functions, methods, classes, instances)
        assigned: dict[str, set[tuple[int, str]]] = {}
        for node in executed:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name) and node.value.func.id in classes):
                assigned.setdefault(node.targets[0].id, set()).add((node.lineno, node.value.func.id))
        settled = {name: sorted(at) for name, at in assigned.items()}
        if settled == instances:
            break
        instances = settled
    return executed


def _instance_class(history: list[tuple[int, str]], line: int) -> str:
    """The class a variable holds at ``line``: its latest assignment before
    that line, or (a call inside a function defined earlier) its last."""
    before = [cls for at, cls in history if at <= line]
    return (before or [history[-1][1]])[-1]


def _resolve(
    tree: ast.Module,
    functions: dict[str, list[ast.AST]],
    methods: dict[str, dict[str, ast.AST]],
    classes: set[str],
    instances: dict[str, list[tuple[int, str]]],
) -> tuple[list[ast.AST], set[str]]:
    executed: list[ast.AST] = []
    called: set[str] = set()
    queue: list[tuple[ast.AST, str | None]] = [(s, None) for s in _reachable(tree.body)]  # node, `self`
    while queue:
        node, self_class = queue.pop()
        for sub in _walk_live(node):
            executed.append(sub)
            if not isinstance(sub, ast.Call):
                continue
            targets: list[tuple[str, ast.AST, str | None]] = []
            if isinstance(sub.func, ast.Name):
                targets = [(sub.func.id, fn, None) for fn in functions.get(sub.func.id, [])]
            elif isinstance(sub.func, ast.Attribute):
                by_class = methods.get(sub.func.attr, {})
                receiver = sub.func.value
                if isinstance(receiver, ast.Call):
                    receiver = receiver.func  # Obj().save() -> Obj
                owner = receiver.id if isinstance(receiver, ast.Name) else None
                if owner == "self" and self_class:
                    owner = self_class
                elif owner in instances:
                    owner = _instance_class(instances[owner], sub.lineno)
                if owner in by_class:
                    candidates = {owner: by_class[owner]}
                elif owner in classes:
                    candidates = {}  # that class has no such method
                elif len(by_class) == 1:
                    candidates = by_class  # an unknown receiver, one possible method
                else:
                    candidates = {}  # ambiguous: withheld rather than guessed
                targets = [(f"{c}.{sub.func.attr}", fn, c) for c, fn in candidates.items()]
            for key, fn, cls in targets:
                if key not in called:
                    called.add(key)
                    queue.extend((s, cls) for s in _reachable(fn.body))  # type: ignore[attr-defined]
    return executed, called


def _walk_live(node: ast.AST):
    """`ast.walk` along the paths that run: never into a nested definition,
    only the taken side of a constant `if`/`while`, every body cut at the
    statement that certainly leaves it, an `except` handler only when the
    `try` body certainly raises what it certainly catches (it may never run
    otherwise), a class body's own statements."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, (*_FUNCTIONS, ast.Lambda)):
            continue
        yield current
        if isinstance(current, ast.ClassDef):
            stack.extend(s for s in _reachable(current.body) if not isinstance(s, _FUNCTIONS))
        elif isinstance(current, (ast.If, ast.While)):
            truth = _constant_truth(current.test)
            stack.append(current.test)
            if truth is not False:
                stack.extend(_reachable(current.body))
            if truth is not True:
                stack.extend(_reachable(current.orelse))
        elif isinstance(current, (ast.For, ast.AsyncFor)):
            stack.append(current.iter)
            if not _statically_empty(current.iter):  # `for x in []:` never runs its body
                stack.extend(_reachable(current.body))
            stack.extend(_reachable(current.orelse))
        elif isinstance(current, (ast.With, ast.AsyncWith)):
            stack.extend(item.context_expr for item in current.items)
            stack.extend(_reachable(current.body))
        elif isinstance(current, _TRY):
            kinds = _body_exit_kinds(current.body)
            raised = {k[6:] for k in kinds if k.startswith("raise:")}
            taken = None
            if isinstance(current, ast.Try) and raised and len(raised) == len(kinds):
                taken, _ = _handler_taken(current.handlers, raised)
            # `else` runs only when the body completes normally
            stack.extend(_reachable(current.body) + ([] if kinds else _reachable(current.orelse))
                         + (_reachable(taken.body) if taken is not None else []) + _reachable(current.finalbody))
        elif isinstance(current, ast.BoolOp):
            stop = isinstance(current.op, ast.Or)  # `False and x`, `True or x`: x is never evaluated
            for value in current.values:
                stack.append(value)
                if _constant_truth(value) is stop:
                    break
        elif isinstance(current, ast.IfExp):
            truth = _constant_truth(current.test)
            stack.append(current.test)
            if truth is not False:
                stack.append(current.body)
            if truth is not True:
                stack.append(current.orelse)
        elif isinstance(current, _COMPREHENSIONS):
            # a generator's element runs only when something consumes it (a call, below)
            stack.extend(_comprehension_live(current, consumed=not isinstance(current, ast.GeneratorExp)))
        elif isinstance(current, ast.Call):
            stack.append(current.func)
            for arg in (*current.args, *(k.value for k in current.keywords)):
                if isinstance(arg, ast.GeneratorExp):
                    stack.extend(_comprehension_live(arg, consumed=True))
                else:
                    stack.append(arg)
        else:
            stack.extend(ast.iter_child_nodes(current))


_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _comprehension_live(comp: ast.AST, consumed: bool) -> list[ast.AST]:
    """The parts of a comprehension that run: every iterable; its filters and
    element only when the first iterable is not an empty literal, no filter
    is a constant False, and (a generator) something consumes it."""
    parts: list[ast.AST] = [gen.iter for gen in comp.generators]  # type: ignore[attr-defined]
    if not consumed or _statically_empty(comp.generators[0].iter):  # type: ignore[attr-defined]
        return parts
    ifs = [test for gen in comp.generators for test in gen.ifs]  # type: ignore[attr-defined]
    if any(_constant_truth(test) is False for test in ifs):
        return parts
    parts.extend(ifs)
    parts.extend([comp.key, comp.value] if isinstance(comp, ast.DictComp) else [comp.elt])  # type: ignore[attr-defined]
    return parts


@functools.lru_cache(maxsize=128)
def _python_facts(code: str) -> _PyFacts | None:
    """What a script DOES, read once per script (a reply's claims are each
    checked against every call, so the read is cached; the facts are never
    mutated by a reader)."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return None
    facts = _PyFacts()
    executed = _executed(tree)
    # a name bound by an import (`import subprocess as sp`, `from subprocess
    # import run as sh`) is the module's: a constant binding
    aliases: dict[str, str] = {}
    for node in executed:
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".")[0]
                aliases[alias.asname or head] = alias.name if alias.asname else head
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    local = {node.name for node in ast.walk(tree) if isinstance(node, _FUNCTIONS)}
    for node in executed:
        if isinstance(node, ast.Assign):
            for target in node.targets:  # msg['To'] = '...'
                if isinstance(target, ast.Subscript) and _literal(target.slice) in ("To", "Cc", "Bcc"):
                    found = _literals(node.value)
                    if found is None:
                        facts.recipient_unknown = True
                    else:
                        facts.recipients |= {a for s in found for a in _addresses(s)}
        elif isinstance(node, ast.Call):
            _call_facts(node, facts, aliases, local)
    if facts.sends and not facts.recipients:
        facts.recipient_unknown = True  # it sends, to whom is not written out
    return facts


def _qualified(name: str, aliases: dict[str, str], local: set[str]) -> str:
    """`sp.run` -> `subprocess.run`, `sh` -> `subprocess.run` (imported as);
    a name the script defines itself is its own."""
    head, dot, rest = name.partition(".")
    if head in aliases and name not in local:
        return aliases[head] + dot + rest
    return name


def _call_facts(node: ast.Call, facts: _PyFacts, aliases: dict[str, str], local: set[str]) -> None:
    name = _qualified(_dotted(node.func), aliases, local)
    method = name.rsplit(".", 1)[-1]
    args = node.args
    kws = {k.arg: k.value for k in node.keywords if k.arg}
    if name in _PY_OPENERS or name in _PY_ARCHIVE_OPENERS:  # open(x, 'w'), zipfile.ZipFile(x, 'w')
        mode = _literal(args[1]) if len(args) > 1 else _literal(kws.get("mode"))
        if mode and mode[:1] in "wax":
            _note_write(facts, args[0] if args else kws.get("file") or kws.get("filename") or kws.get("name"))
    elif method == "open" and isinstance(node.func, ast.Attribute):  # Path('...').open('w')
        mode = _literal(args[0]) if args else _literal(kws.get("mode"))
        if mode and mode[:1] in "wax":
            _note_write(facts, _path_receiver(node.func.value))
    elif method in ("write_text", "write_bytes") and isinstance(node.func, ast.Attribute):
        _note_write(facts, _path_receiver(node.func.value))  # Path('...').write_text(...)
    elif method in _PY_WRITE_METHODS and (args or any(k in kws for k in _PY_WRITE_KWARGS)):
        # a file-writing method takes its destination; `obj.save()` bare is
        # whatever a user-defined `save` does (resolved through _executed)
        _note_write(facts, args[0] if args else next(kws[k] for k in _PY_WRITE_KWARGS if k in kws))
    elif name == "shutil.make_archive":  # writes base_name + the format's suffix
        base = _literal(args[0]) if args else _literal(kws.get("base_name"))
        fmt = _literal(args[1]) if len(args) > 1 else _literal(kws.get("format"))
        if base is not None and fmt is not None:
            facts.writes.append(f"{base}.{_PY_ARCHIVE_SUFFIX.get(fmt, fmt)}")
        else:
            facts.write_unknown = True
    elif name in ("json.dump", "pickle.dump", "yaml.dump", "yaml.safe_dump"):
        sink = args[1] if len(args) > 1 else kws.get("fp") or kws.get("stream")
        if isinstance(sink, ast.Call):  # json.dump(data, open('/tmp/x', 'w'))
            _call_facts(sink, facts, aliases, local)
        else:
            facts.write_unknown = True
    elif name in _PY_COPIES:
        _note_copy(facts, args[0] if args else kws.get("src"), args[1] if len(args) > 1 else kws.get("dst"))
    elif method == "sendmail":
        facts.sends = True
        to = args[1] if len(args) > 1 else kws.get("to_addrs")
        found = _literals(to)
        if found is None:
            facts.recipient_unknown = True
        else:
            facts.recipients |= {a for s in found for a in _addresses(s)}
    elif method == "send_message":
        facts.sends = True  # recipients come from msg['To'] = ..., in any order
    elif name in _PY_SHELL_CALLS or name == "asyncio.create_subprocess_exec":
        argv: ast.AST | None
        if name == "asyncio.create_subprocess_exec":
            argv = ast.List(elts=list(args)) if args else None  # the argv IS the arguments
        else:
            argv = args[0] if args else kws.get("args")
        # subprocess.check_call/check_output always raise on failure;
        # subprocess.run is checked only when check=True is passed explicitly
        _chk = kws.get("check")
        _is_checked = (
            name in ("subprocess.check_call", "subprocess.check_output")
            or (name == "subprocess.run" and isinstance(_chk, ast.Constant) and _chk.value is True)
        )
        if isinstance(argv, (ast.List, ast.Tuple)):  # argv: the executable and ITS arguments
            items = _literals(argv)
            if items:
                _idx = len(facts.invocations)
                facts.invocations.append((program_name(items[0]), tuple(items[1:])))
                if _is_checked:
                    facts.checked_invocations.add(_idx)
            else:
                facts.run_unknown = True
        elif (line := _literal(argv)) is not None:  # a shell line: read like bash
            found = command_invocations(line)
            if found is None:
                facts.run_unknown = True
            else:
                facts.invocations.extend((p, tuple(a)) for p, a in found)
        else:
            facts.run_unknown = True
    else:
        if name.split(".")[0] in _PY_DEPLOY_MODULES:
            facts.deploys = True  # an operation on a deployment integration (an import alone is none)
        target = args[0] if args else kws.get("url")
        if isinstance(target, ast.Call) and _dotted(target.func).endswith("Request"):  # urlopen(Request(url))
            target = target.args[0] if target.args else next((k.value for k in target.keywords if k.arg == "url"), None)
        url = _text_of(target)
        if url and "api.telegram.org" in url and method in ("post", "get", "request", "urlopen"):
            facts.sends = True
            facts.recipient_unknown = True
        elif method not in local and not (isinstance(node.func, ast.Name) and node.func.id in local):
            _hint_facts(_callee_text(node.func), method, kws, facts)


def _callee_text(func: ast.AST) -> str:
    """`Repo('.').remote().push` for a call chain (calls elided), for a
    what-is-this-named-for check."""
    if isinstance(func, ast.Call):
        return _callee_text(func.func)
    if isinstance(func, ast.Attribute):
        return f"{_callee_text(func.value)}.{func.attr}"
    return func.id if isinstance(func, ast.Name) else ""


def _hint_facts(name: str, method: str, kws: dict[str, ast.AST], facts: _PyFacts) -> None:
    """A call into a library this reader does not know, named like the
    effect: `yag.send(to=...)`, `repo.git.push()`, `client.deploy(...)`."""
    addressed = [key for key in _PY_RECIPIENT_KWARGS if key in kws]
    receiver = name.rsplit(".", 1)[0] if "." in name else ""
    if _PY_CALLEE_HINTS["email"].search(method) or (
            _PY_CALLEE_HINTS["email_send"].search(method)
            and (addressed or _PY_CALLEE_HINTS["email_receiver"].search(receiver))):
        facts.sends = True
        for key in addressed:
            found = _literals(kws[key])
            if found is None:
                facts.recipient_unknown = True
            else:
                facts.recipients |= {a for s in found for a in _addresses(s)}
    for kind in ("vcs_push", "vcs_commit"):
        if _PY_CALLEE_HINTS[kind].search(method) and re.search(r"git|repo|remote|index", name, re.I):
            facts.hints.add(kind)
    if _PY_CALLEE_HINTS["deploy"].search(method):
        facts.hints.add("deploy")


def _path_receiver(receiver: ast.AST) -> ast.AST | None:
    """The path a `Path('...')` receiver was built from; None otherwise."""
    if isinstance(receiver, ast.Call) and receiver.args and _dotted(receiver.func).endswith("Path"):
        return receiver.args[0]
    return None


def _text_of(node: ast.AST | None) -> str | None:
    """The literal text of a string expression with its computed parts
    blanked: a constant, an f-string, `'a' + x + 'b'`, `'a%s' % x`,
    `'a{}'.format(x)`. For a CONTAINS check, never an equality."""
    if (text := _literal(node)) is not None:
        return text
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal(v) or "{}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text_of(node.left), _text_of(node.right)
        return None if left is None and right is None else (left or "{}") + (right or "{}")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _text_of(node.left)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return _text_of(node.func.value)
    return None


def _note_write(facts: _PyFacts, destination: ast.AST | None) -> None:
    if (path := _literal(destination)) is not None:
        facts.writes.append(path)
    else:
        facts.write_unknown = True


def _note_copy(facts: _PyFacts, source: ast.AST | None, destination: ast.AST | None) -> None:
    """A copy or move: its destination, and -- a copy INTO a directory lands
    at dir/basename -- the source's name under it."""
    if (path := _literal(destination)) is None:
        facts.write_unknown = True
        return
    facts.writes.append(path)
    if (src := _literal(source)) is not None and (name := src.rstrip("/").rsplit("/", 1)[-1]):
        facts.writes.append(path.rstrip("/") + "/" + name)


def _names(target: str, text: str) -> bool:
    """True if a command's text names the claimed target: an absolute path in
    full (`/tmp/report.md` is not `/var/archive/report.md`), `~/x` also as
    `/x` (`$HOME/x`), a relative one in full or by basename."""
    if not target or not text:
        return False
    if target in text:
        return True
    if target.startswith("~/"):
        return target[1:] in text
    if target.startswith("/"):
        return False
    return target.rsplit("/", 1)[-1] in text


def _same_path(target: str, path: str) -> bool:
    """True if an explicit written path IS the claimed target, normalized:
    an absolute target must match in full, `~/x` any absolute path ending
    in `/x`, a relative target the path's tail on a `/` boundary."""
    if not target or not path:
        return False
    # `/tmp//x`, `/tmp/./x`, `/tmp/../tmp/x`, `./x` are the same path
    target = posixpath.normpath(_HOME.sub("~/", target.replace("\\", "/")))
    path = posixpath.normpath(_HOME.sub("~/", path.replace("\\", "/")))
    if path == target:
        return True
    if target.startswith("/"):
        return False
    if target.startswith("~/"):
        return path.startswith("/") and path.endswith(target[1:])
    return path.endswith("/" + target.removeprefix("./"))


_HOME = re.compile(r"^\$\{?HOME\}?/")  # `$HOME/x` is `~/x`


def _addresses(text: str) -> set[str]:
    """Every whole e-mail address written in ``text``, lowercased."""
    return {a.lower() for a in _PY_ADDRESS.findall(text)}


def _best(levels: list[str]) -> str:
    return max(levels, key=_LEVELS.__getitem__)


def _dry_run(sub: str, args: tuple[str, ...]) -> bool:
    """`git push -n` / `--dry-run` change nothing; for commit `-n` is --no-verify."""
    for a in args:
        if a == "--":
            break
        if a in ("--help", "-h"):  # shows the manual, runs nothing
            return True
        name = a.split("=", 1)[0]
        if len(name) >= 5 and "--dry-run".startswith(name):  # getopt abbreviation
            return True
        if sub == "push" and a.startswith("-") and not a.startswith("--") and "n" in a[1:]:
            return True
    return False


def _git_does(args: tuple[str, ...], sub: str) -> bool:
    split = git_subcommand(list(args))
    return split is not None and split[0] == sub and not _dry_run(sub, tuple(split[1]))


def _positional(args: tuple[str, ...]) -> list[str]:
    """Arguments that are neither options nor a heredoc body (`\\n…`) nor an
    output target (`\\t>…`)."""
    return [a for a in args if not a.startswith(("-", "\n", "\t"))]


# Where a command writes: its output redirects, plus the destination of the
# programs whose destination is a known argument. A program known to write
# elsewhere, or to delete, is not a save to the claimed path.
_WRITERS_LAST = frozenset({"cp", "mv", "install", "ln", "rsync", "scp", "sftp"})  # last positional
_WRITERS_ALL = frozenset({"tee", "touch", "truncate", "mkdir", "mkfifo"})           # every positional
_DESTROYERS = frozenset({"rm", "rmdir", "unlink", "shred"})
# Output options, scoped to the programs they mean output for: a long
# `--output`-style option for any program that is not a known read (`grep
# --file` reads patterns), `-o` for the programs that write with it, `-O`
# for wget, `-f`/`--file` for tar.
_OUTPUT_LONG = frozenset({"--output", "--out", "--outfile", "--out-file", "--dest", "--destination"})
_OUTPUT_LONG_PREFIXES = ("--output=", "--out=", "--outfile=", "--out-file=", "--dest=", "--destination=")
_DASH_O_PROGRAMS = frozenset({"pandoc", "gcc", "cc", "clang", "g++", "c++", "ld", "curl", "sort",
                              "wkhtmltopdf", "wkhtmltoimage", "pdftotext", "convert", "magick"})


def _destinations(prog: str, args: tuple[str, ...]) -> list[str]:
    dests = [a[2:] for a in args if a.startswith("\t>")]
    positional = _positional(args)
    if prog in _WRITERS_LAST and positional:
        last = positional[-1]
        dest = last.split(":", 1)[1] if ":" in last and not last.startswith("/") else last
        dests.append(dest)
        # a copy INTO a directory lands at dir/basename(source)
        for src in positional[:-1]:
            if name := src.rstrip("/").rsplit("/", 1)[-1]:
                dests.append(dest.rstrip("/") + "/" + name)
    elif prog in _WRITERS_ALL:
        dests += positional
    elif prog == "tar" and _tar_writes_archive(args):  # only a create/append writes the archive
        if args and not args[0].startswith("-") and "f" in args[0] and len(positional) > 1:
            dests.append(positional[1])  # `tar czf X`
        for i, a in enumerate(args):
            if (a in ("-f", "--file") or ("f" in _short_flags(a) and "-" in a[:1])) and i + 1 < len(args):
                dests.append(args[i + 1])
            elif a.startswith("--file="):
                dests.append(a.split("=", 1)[1])
    elif prog == "sed" and any("i" in _short_flags(a) or a.startswith("--in-place") for a in args):
        dests += positional
    elif prog == "dd":
        dests += [a[3:] for a in args if a.startswith("of=")]
    for i, a in enumerate(args):
        value = args[i + 1] if i + 1 < len(args) else None
        if a in _OUTPUT_LONG and prog not in READ_COMMANDS and value is not None:
            dests.append(value)
        elif a.startswith(_OUTPUT_LONG_PREFIXES) and prog not in READ_COMMANDS:
            dests.append(a.split("=", 1)[1])
        elif a == "-o" and prog in _DASH_O_PROGRAMS and value is not None:
            dests.append(value)
        elif a == "-O" and prog == "wget" and value is not None:
            dests.append(value)
    return dests


def _short_flags(word: str) -> str:
    return word[1:] if len(word) > 1 and word.startswith("-") and not word.startswith("--") else ""


def _tar_writes_archive(args: tuple[str, ...]) -> bool:
    """`-c`/`-r`/`-u`/`-A` (create, append, update, concatenate) write the
    archive; `-t` lists it and `-x` reads it."""
    if not args:
        return False
    modes = args[0] if not args[0].startswith("-") else ""  # old style: `tar czf`
    for a in args:
        if a in ("--create", "--append", "--update", "--concatenate", "--catenate"):
            return True
        modes += _short_flags(a)
    return any(m in modes for m in "cruA")


def _writes(target: str, runs: tuple) -> str:
    """How well the invocations show a write to ``target``: exact for a
    certain write to that destination, plausible for an uncertain one or a
    mention by a program whose destinations this reader does not know."""
    best = "none"
    for prog, args, certain in runs:
        base = _base(prog)
        dests = _destinations(base, args)
        if any(_same_path(target, d) for d in dests):
            best = _best([best, "exact" if certain else "plausible"])
        elif base in _WRITERS_LAST and _copies_tree(base, args) and any(_inside(target, d) for d in dests):
            best = _best([best, "plausible"])  # a tree copied into it: the file may be anywhere under it
        elif base in _DESTROYERS or base in _WRITERS_LAST or base in _WRITERS_ALL or dests \
                or base in READ_COMMANDS or base in ("tar", "dd"):
            continue  # a delete, a known writer writing elsewhere, or a read naming the path
        elif _names(target, " ".join(args)):
            best = _best([best, "plausible"])
    return best


def _copies_tree(prog: str, args: tuple[str, ...]) -> bool:
    """`cp -r`, `rsync`, `scp -r`: the destination receives a whole tree."""
    return prog == "rsync" or any(a in ("--recursive", "--archive") or
                                  any(f in "rRa" for f in _short_flags(a)) for a in args)


def _inside(target: str, directory: str) -> bool:
    """True if ``target`` lies under ``directory`` (both normalized)."""
    target = posixpath.normpath(_HOME.sub("~/", target.replace("\\", "/")))
    directory = posixpath.normpath(_HOME.sub("~/", directory.replace("\\", "/")))
    return directory not in ("", ".") and target.startswith(directory.rstrip("/") + "/")


# Options whose value is not a recipient: a subject, a sender, a body, an
# attachment, a header. curl addresses a mail ONLY through --mail-rcpt.
_NOT_RECIPIENT_OPTIONS = frozenset({
    "-s", "--subject", "-f", "--from", "--mail-from", "--body", "-h", "--header", "--add-header",
    "-r", "-a", "--attach", "--attach-type", "-i", "-H", "-d", "--data", "--data-raw",
    "--data-binary", "--data-urlencode", "-T", "--upload-file", "-u", "--user",
})
_NOT_RECIPIENT_PREFIXES = ("--subject=", "--from=", "--mail-from=", "--body=", "--header=",
                           "--data=", "--attach=")


def _recipients(prog: str, args: tuple[str, ...]) -> tuple[set[str], bool]:
    """The addresses a mail invocation sends TO (not its subject, sender,
    body or attachments), and whether one is held in a variable."""
    found: set[str] = set()
    variable = False
    take_next = False  # the next argument is a recipient (curl --mail-rcpt X)
    skip = False
    if prog in ("sendmail", "msmtp", "ssmtp") and (
            "--read-recipients" in args or any("t" in _short_flags(a) for a in args)):
        # `-t`: the recipients are the message's To/Cc/Bcc headers -- read
        # from a heredoc body, unreadable from a file or a pipe
        bodies = [a[1:] for a in args if a.startswith("\n")]
        for body in bodies:
            for line in body.splitlines():
                if line.lower().startswith(("to:", "cc:", "bcc:")):
                    found |= _addresses(line)
        variable |= not bodies
    for a in args:
        if a.startswith(("\n", "\t")):
            continue
        if take_next:
            take_next = False
            variable |= a.startswith("$")
            found |= _addresses(a)
            continue
        if skip:
            skip = False
            continue
        if prog == "curl":
            if a == "--mail-rcpt":
                take_next = True
            elif a.startswith("--mail-rcpt="):
                value = a.split("=", 1)[1]
                variable |= value.startswith("$")
                found |= _addresses(value)
            continue
        if a in _NOT_RECIPIENT_OPTIONS:
            skip = True
            continue
        if a.startswith(_NOT_RECIPIENT_PREFIXES):
            continue
        if a.startswith("$"):
            variable = True
        found |= _addresses(a)
    return found, variable


_SYSTEM_DIRS = frozenset({
    "/usr/bin", "/bin", "/usr/local/bin", "/usr/sbin", "/sbin", "/usr/local/sbin",
    "/opt/homebrew/bin", "/opt/local/bin", "/snap/bin",
})


def _base(prog: str) -> str:
    """The trusted tool a program is, or the local executable it stays:
    `/usr/bin/rsync` is rsync, `./git` and `/tmp/evil/git` are not git."""
    if prog.startswith("./"):
        return prog
    if "/" in prog:
        head, _, tail = prog.rpartition("/")
        trusted = (head in _SYSTEM_DIRS or "program files" in head.lower()
                   or head.lower().endswith("system32"))
        name = tail[:-4].lower() if tail.lower().endswith(".exe") else tail
        return name if trusted else "./" + name
    return prog


def _does(kind: str, prog: str, args: tuple[str, ...]) -> bool:
    """True if one invocation produces the claimed effect."""
    prog = _base(prog)
    positional = _positional(args)
    if kind == "vcs_push":
        return ((prog == "git" and _git_does(args, "push"))
                or (prog == "docker" and positional[:1] == ["push"])
                or prog in _TRANSFERS)  # "I pushed the files to the server with rsync"
    if kind == "vcs_commit":
        return prog == "git" and _git_does(args, "commit")
    if kind == "email":
        if prog in _MAIL_SENDERS:
            return "-bp" not in args  # `sendmail -bp` prints the queue
        if prog in _MAIL_CLIENTS:  # a recipient argument, never one inside the body
            return any("@" in a or a.startswith("$") for a in args if not a.startswith("\n"))
        return prog == "curl" and any(
            a.lower().startswith(("smtp://", "smtps://", "--mail-rcpt")) or "api.telegram.org" in a
            for a in args)
    # deploy: a deploy or transfer tool doing something, not any network call
    # and not a look at a deployment
    if prog == "git":
        return _git_does(args, "push") or _git_does(args, "pull")
    if prog in _TRANSFERS or "deploy" in prog:
        return True
    if prog == "gh":
        return positional[:2] == ["workflow", "run"]
    # the CLI's SUBCOMMAND, not any operand: `systemctl status restart` shows
    # the status of a unit named restart
    verbs = positional[:_DEPLOY_VERB_DEPTH.get(prog, 1)]
    if prog in _TASK_RUNNERS:
        return any(p in ("deploy", "release", "publish", "ship", "start", "serve") or "deploy" in p
                   for p in positional[:2])
    if prog in _DEPLOY_CLIS:
        return ((prog == "vercel" and not positional) or any(p in _DEPLOY_CHANGE for p in verbs)
                or any(a in ("--prod", "--production") for a in args))
    return False


def _python_code(prog: str, args: tuple[str, ...]) -> str | None:
    """The code an invocation runs when it is right there: `python -c CODE`,
    or a script fed on stdin by a heredoc (`python3 - <<EOF`)."""
    prog = _base(prog)
    if prog in _PY_RUNNERS and args[:1] == ("run",):
        for i, a in enumerate(args[1:], 1):
            if a in _INTERPRETERS or a.startswith("python3."):
                return _python_code(a, args[i + 1:])
            if not a.startswith("-"):
                return None
        return None
    if not (prog in ("python", "python3") or prog.startswith("python3.")):
        return None
    body = next((a[1:] for a in args if a.startswith("\n")), None)
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-c":
            return args[i + 1] if i + 1 < len(args) else ""
        if a.startswith("-c") and not a.startswith("--"):
            return a[2:]
        if a == "-" or a.startswith("\n"):
            return body
        if a in ("-m", "-W", "-X", "-Q"):
            return None if a == "-m" else _skip_value(args, i)
        if not a.startswith("-"):
            return None  # a script file: python's options come before it
        i += 1
    return body


def _skip_value(args: tuple[str, ...], i: int) -> str | None:
    return _python_code("python", args[i + 2:]) if i + 2 <= len(args) else None


def _is_script(prog: str) -> bool:
    return bool(_SCRIPT.search(prog)) or _base(prog).startswith("./")


_NOT_A_TASK_MODULE = frozenset({"pip", "venv", "ensurepip", "json.tool", "http.server", "this"})


def _runs_file(args: tuple[str, ...]) -> bool:
    """An interpreter given a script file or a module: it decides its own effects."""
    if "-m" in args:
        module = args[args.index("-m") + 1:][:1]
        return not module or module[0] not in _NOT_A_TASK_MODULE
    return any(_SCRIPT.search(a) for a in _positional(args))


def _opaque_for(kind: str, prog: str, args: tuple[str, ...]) -> bool:
    """A run this reader cannot see into that could have produced the effect."""
    if _python_code(prog, args) is not None:
        return False  # the code is right there: judged as code
    if prog.startswith("$"):
        return True  # `$CMD ...`, `$(cat cmd.txt)`: whatever it expands to
    base = _base(prog)
    if base in _RUNNERS and command_string(base, list(args)) is not None:
        return True  # its command string could not be read
    text = " ".join((prog, *args))
    if _TESTY.search(text):
        return False
    if base in _RUNNERS:
        return base != "ssh" and _runs_file(args) and bool(_HINTS[kind].search(text))
    if base in _INTERPRETERS or base.startswith("python3."):
        return _runs_file(args) and bool(_HINTS[kind].search(text))
    if _is_script(prog) or base in _TASK_RUNNERS:
        return bool(_HINTS[kind].search(text))
    return False


def _code_level(claim: Claim, code: str) -> str:
    """Evidence from Python source: a run_python call, `python -c`, or a heredoc."""
    facts = _python_facts(code)
    if facts is None:
        # a cut made by the ledger is unreadable (a cut string literal is not
        # code); anything else that does not parse did not run
        return "plausible" if EVIDENCE_TRUNCATED in code else "none"
    # what it runs through a shell is read like bash -- never certain, and a
    # subprocess whose argv is not written out could be anything
    runs = tuple((prog, args, False) for prog, args in facts.invocations)
    if claim.kind == "file_write":
        if claim.target:
            if any(_same_path(claim.target, w) for w in facts.writes):
                return "exact"
            if _writes(claim.target, runs) != "none" or facts.write_unknown or facts.run_unknown:
                return "plausible"  # a computed path, or a shell write to it
            return "none"  # writes elsewhere, or nothing
        if facts.writes or facts.write_unknown or facts.run_unknown:
            return "plausible"
        return "plausible" if any(_destinations(_base(p), a) for p, a, _ in runs) else "none"
    if claim.kind == "email":
        hits = [i for i, (prog, args, _) in enumerate(runs) if _does("email", prog, args)]
        if not facts.sends and not hits:
            return "plausible" if facts.run_unknown else "none"
        if claim.target:
            recipients, held = set(facts.recipients), facts.recipient_unknown
            for i in hits:
                found, variable = _recipients(_base(runs[i][0]), runs[i][1])
                recipients |= found
                held |= variable
            if claim.target in recipients:
                return "plausible"
            # held in a variable, or run opaquely, with no other recipient written out; or someone else
            return "plausible" if not recipients and (held or facts.run_unknown) else "none"
        return "plausible"
    if any(_does(claim.kind, prog, args) for prog, args in facts.invocations) or claim.kind in facts.hints:
        return "plausible"
    if claim.kind == "deploy" and facts.deploys:
        return "plausible"
    return "plausible" if facts.run_unknown else "none"  # argv not written out: could be anything


def _addressed(target: str, runs: tuple, hits: list[int], opaque: bool) -> bool:
    """True if the sending invocations address ``target``: among their
    recipient ARGUMENTS (never a body or subject), or held in a variable
    (or run opaquely) with no other recipient written out."""
    recipients: set[str] = set()
    variable = False
    for i in hits:
        found, held = _recipients(_base(runs[i][0]), runs[i][1])
        recipients |= found
        variable |= held
    if target in recipients:
        return True
    return not recipients and (variable or opaque)


def _bash_level(claim: Claim, ev: Evidence) -> str:
    """Evidence from one successful bash call.

    Only a command this reader READ can disprove a claim: an unreadable one
    (``runs is None`` -- a quote cut in half, past the ledger's cap) is
    ``plausible``, and so is one that runs something opaque (a script, a
    hinted task) that could have produced the effect. ``exact`` needs a
    command that CERTAINLY ran and succeeded (see ``command_runs``): one
    after `||`, or before a `;`, may have been skipped or masked and is
    ``plausible``. A non-zero exit is the LAST command's status: it
    disproves only an effect that command produced, and a failed opaque
    last command is no evidence at all.
    """
    runs = ev.runs
    if runs is None:
        return "plausible"
    failed = ev.exit_code not in (None, 0)
    succeeded = runs[:-1] if failed else runs  # the last one did not
    opaque = any(_opaque_for(claim.kind, prog, args) for prog, args, _ in succeeded)
    levels = [_code_level(claim, code) for prog, args, _ in succeeded
              if (code := _python_code(prog, args)) is not None]
    if claim.kind == "file_write":
        if ev.side_effect not in ("write", "external"):
            levels.append("none")  # a read cannot have saved anything
        elif failed and len(runs) <= 1:
            levels.append("none")
        elif claim.target:
            # a write to THAT destination; `rm x` and `touch x.bak` are not one
            written = _writes(claim.target, succeeded)
            levels.append("plausible" if written == "none" and opaque else written)
        else:
            levels.append("plausible")
        return _best(levels)
    # the hits that may have succeeded: a failed exit is the LAST one's
    hits = [i for i, (prog, args, _) in enumerate(runs)
            if _does(claim.kind, prog, args) and not (failed and i == len(runs) - 1)]
    if not hits:
        levels.append("plausible" if opaque else "none")
    elif claim.kind == "email" and claim.target and not _addressed(claim.target, runs, hits, opaque):
        levels.append("none")
    elif claim.kind in ("vcs_push", "vcs_commit") \
            and any(_base(runs[i][0]) in ("git", "docker") and runs[i][2] for i in hits):
        levels.append("exact")
    else:
        levels.append("plausible")
    return _best(levels)


# A send claim whose sentence names another destination than the owner's Telegram chat: a send_file could have
# reached it only through a chat id it was given, never certainly. THE RULE: a destination is named when the
# sentence, from the claim's clause to its end (a clause-ending mark does not hide "to the user — Bob"), has
#   1. mail as the verb, the thing sent, the channel or the tool: "emailed", "mailed", "Email sent", "the
#      email.", "via/by/over/through/in/using email", "the mail server", "send_email" (never "the email
#      summary", "email-style");
#   2. a named channel other than Telegram after via/on/over/through/by/in/into/using: Slack, WhatsApp,
#      Signal, Discord, SMS, iMessage, Teams, Messenger, WeChat, Gmail, Outlook, LinkedIn, Twitter, and "text"
#      or SMS after by/via/over, or after in/as with an article or before "message" ("in a text message", "as an
#      SMS"; not "in a text file/form/format", "in plain text", "as text") (never "Gmail-style"); after
#      via/through/using, any other word too ("via SMTP", "via the API") but Telegram, the bot, the chat, a DM,
#      "here", the sending tool by name ("via send_file", "via send_telegram"; not any other word with "_": "via
#      _Slack_", "via send_slack", "via telegram_group"), a link and a time (but "using" after a participle, or
#      before the data or a template, is how the report was made: "charts generated using matplotlib"); after
#      on/over, a place: a capitalised word, a handle or "the wiki/website/channel/..." (never a topic or a
#      time: "a note on the outage", "on it", "on Monday", "on Oct 24", "over lunch");
#      post, a courier, a hand or a fax after "by", and anyone right after "<the message> by" or "sent by"
#      but you, a time, the owner's chat, a link, a gerund or a manner ("the report by Bob", not "by Friday",
#      "by DM", "by attaching it", "by request", "by Friday EOD", "by our bot", "by send_file"); a chat id
#      ("chat_id -100123", "chat -1001234") is another chat;
#   3. a recipient after "to", "to you and", "shared ... with", "cc", "bcc" or "copied" (in): anything that
#      is not on the lists below -- a name ("Bob", "Tim": the owner's name is not known here; "Mark", "O'Brien",
#      "Иван"), an acronym ("HR", "S3"), a number ("3 people"), a handle "@bob", a channel "#general" (also
#      after in/on/into/via/over), a person or group in lowercase ("them", "bob", "the team", "the Telegram
#      group"), an address (anywhere in the sentence: "to the user at bob@x.io"), a place after "at" after a self
#      word ("to the user at Bob", "to you at the London office", not "to the owner at 7am", "at lunch", "at the
#      office"); a bracket, a dash or a mark after a self word holding anything but a sent thing, a count or an
#      adverb ("to the user: Bob", not "to you (report.pdf)", "to you - 3 pages"); and after "a link to" one that
#      is no sent thing ("a link to the team", not "a link to the report", "to it", "to https://...", "to
#      report.pdf").
# Nothing else is: not you, the owner, the user, the chat, a DM, the bot or Telegram, nor the owner's chat ("the user's
# DMs"; but "the user's team" or "inbox" is someone else's); not an infinitive in lowercase ("to review"; "to Mark" is a
# name), a format or language ("to PDF", "to pdf", "to Markdown", "to French"), a time ("to Friday", "to 5pm", "to
# date", "to the end", "the next morning"; so "to May" is read as a time too), an idiom ("due to", "up to", "according
# to", "converted to"); not a content word ("with a summary", "as a .docx"); after "to you and", not a verb or a
# function word in lowercase ("to you and archived the draft", "to you and on time"); but "a link to the summary and
# Bob" names Bob. A word on these lists counts only when it heads its phrase (_HEADS; a self word, _SELF_ENDS):
# "to the next reviewer", "to the end user", "to the user group", "to French colleagues" name someone.
# A word on the lists below is what a phrase names only when it heads the phrase: a punctuation mark, the end, a
# line break (not after "user" or "bot": main reads "the user ..." as a recipient) or a function word follows it, not a
# word it modifies ("to the end" and "to the end of the day", not "to the end user"; "to French", not "to French
# colleagues"; "to the user", not "to the user group"; not "today's").
_HEADS = (r"(?=\s*(?:[.,;:!?)\]\"”—–]|$)|(?<!user)(?<!bot)[ \t]*\n|(?:[ \t]+|(?<!user)(?<!bot)\s+)"
          r"(?:and|or|&|as|at|by|for|from|in|into|on|onto|of|over|via|through|using|with|without|to|before|after"
          r"|until|till|since|so|then|too|also|again|instead|directly|immediately|first|already|only|here|now|later"
          r"|today|yesterday|tonight|tomorrow|this|that|i|we|you|he|she|they|it)\b)")
# A time: a day or month name (before a day number or a part of the day), a month abbreviation before a day
# number ("Oct 24", not "Jan"), a clock time ("9am", "9:30", "21:00 UTC"), a time adverb, "next week", "the
# latest/previous/same/next/last" before a time noun ("the next morning", "the last hour", "the same time"), or "the
# end/top/bottom/start/beginning/latest/previous/same/next/last" alone or before "of"; each one heading its phrase. Each
# but "9:30" is a narrowing of an older, wider form, so no other phrase is a time now that was not before; "to 9:30"
# names no one either. The time nouns: second, minute,
# hour, day, night, morning, afternoon, evening, week, weekend, fortnight, month, quarter, year, sprint, run,
# cycle, round, iteration, time, and a version or release ("the next release").
_A_TIME = (r"(?:(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|march|april|may"
           r"|june|july|august|september|october|november|december)"
           r"(?:\s+(?:\d{1,2}(?:st|nd|rd|th)?|morning|afternoon|evening|night))?"
           r"|(?:jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec)\.?\s+\d{1,2}(?:st|nd|rd|th)?"
           r"|today|tomorrow|tonight|noon|midnight|now|later|eod|cob|next\s+week"
           r"|the\s+(?:latest|previous|same|next|last)\s+(?:second|minute|hour|day"
           r"|night|morning|afternoon|evening|week|weekend|fortnight|month|quarter|year|sprint|run|cycle|round"
           r"|iteration|time|version|release)s?"
           r"|the\s+(?:end|top|bottom|start|beginning|latest|previous|same|next|last)"
           r"|\d{1,2}:\d\d(?:\s*(?:am|pm))?(?:\s+(?:utc|gmt|bst|cet|cest|eet|eest|msk|ist|jst|[ecmp][sd]t))?"
           r"|\d{1,2}\s*(?:am|pm))\b" + _HEADS + ")")
# The owner's chat, as a noun: the owner, the user, the chat, a DM, the bot, Telegram, chained ("the Telegram
# bot") or as the owner's chat, DMs or Telegram ("the user's DMs"); "the user group", "the Telegram channel",
# "the user's team" and "the bot's owner" are someone else.
_SELF = r"(?:owner|user|chat|dms?|bot|telegram)"
# What may follow a self word for it to stay the owner's chat: what may follow any listed word (_HEADS), a
# bracket, a dash, or an adverb or conjunction ("to the owner earlier", "... (see above)", "... - done", "via
# Telegram successfully"). Anything else names someone else: a noun it modifies ("the user group"), a
# possessive ("the owner's manager"), a name ("the user Bob", "the user bob"). The list is closed, so an
# unlisted word errs to "Could not confirm" (main read every "to the user ..." so).
_SELF_ENDS = (rf"(?:{_HEADS}|(?=\s*\(|\s+[-–—]\s|\s+(?:earlier|just|twice|once|because|successfully|privately"
              r"|personally|separately|manually|automatically|promptly|quickly|securely|safely|both|alone|together"
              r"|while|when|per|yet|anyway|as\s+well|right|who|whom|which|asap)\b))")
_A_SELF_PHRASE = rf"(?:{_SELF}(?:\s+{_SELF})*(?:['’]s\s+(?:chat|dms?|telegram)(?:\s+{_SELF})*)?\b)"
_A_SELF = rf"(?:{_A_SELF_PHRASE}{_SELF_ENDS})"
_NOT_A_RECIPIENT = (
    # you, me, here, or the owner's chat as a noun
    rf"(?!(?:you|yourself|me|myself|here)\b|(?:(?:the|your|my|our|this)\s+)?{_A_SELF})"
    # an infinitive, in lowercase: "to Mark" is a name
    r"(?!(?-i:review|read|check|confirm|approve|see|look|keep|share|sign|use|be|date|make|help|show|let|get"
    r"|give|reply|respond|follow|discuss|decide|go|do|start|finish|test|verify|track|compare|print|download"
    r"|open|view|save|edit|update|fix|run|ensure|avoid|prepare|answer|inform|notify|remind|explain"
    r"|summari[sz]e|highlight|cover|include|mark|close|cancel|catch|meet|take|bring)\b)"
    # a format or a language
    r"(?!(?:pdf|html?|docx?|csv|tsv|json|ya?ml|xml|xlsx?|pptx?|odt|rtf|txt|text|plain\s+text|markdown|md|png"
    r"|jpe?g|gif|svg|webp|zip|tar|epub|latex|tex|excel|word|powerpoint|english|french|german|spanish|russian"
    rf"|italian|portuguese|chinese|japanese|korean|arabic|dutch|ukrainian|hebrew)\b{_HEADS}|\.\w)"
    rf"(?!{_A_TIME})"
    # anything else: a name, an acronym, a number, a word in lowercase, a handle, a channel
    r"[\w@#]"
)
# After "to you and": not a verb or a function word ("to you and archived it", "to you and on time"); the verbs
# are listed, since a name can end in -ed too ("to you and ahmed", "and fred")
_NOT_A_VERB = (r"(?!(?-i:then|also|now|later|again|so|on|in|at|by|with|as|for|from|via|over|through|i|it|we|will|would"
               r"|who|whom|which|because|since|while|when|thanks|please|ok|okay"
               r"|I|We|It|That|This|I['’]m|We['’]re"
               r"|can|could|should|have|has|had|is|was|am|only|nobody|no\s+one|none|done|sent|told|made|put|set|left"
               r"|got|wrote|b?cc(?:['’]?e?d)?|(?:archiv|forward|copi|sav|mov|notifi|ping|add|attach|upload|post|clos"
               r"|updat|delet|remov|fil|creat|merg|push|committ|schedul|shar|logg|not|confirm|check|verifi|open|renam"
               r"|pinn|deploy|start|stopp|finish|complet|publish|link|flagg|tagg|mark|repli|ask|clean|clear|sync"
               r"|export|convert|generat|queu|revert|restor|back|includ|summari[sz]|review|sign|print|download|edit"
               r"|fix|test|tidi|resolv|assign|approv|cancell?|declin|reject|accept|answer|respond|invit)ed)\b)")
# What "a link to" points at rather than sends to: a sent thing, it, a URL, a path or a file name with a known
# extension ("report.pdf", not "bob.smith" or "bob.smith@example.com").
_A_SENT_THING = (r"(?:(?:the|a|an|your|my|our|this|that|these|those)\s+(?:[\w.'’-]+\s+){0,3}?"
                 r"(?:report|summar(?:y|ies)|file|doc|document|page|dashboard|chart|sheet|spreadsheet|folder|draft"
                 r"|pr|pull\s+request|issue|repo|repository|article|post|site|website|video|recording|slide|deck|note"
                 r"|result|data|table|ticket|thread|commit|branch|diff|log|image|photo|screenshot|form|invite"
                 r"|presentation|release|spec|plan|proposal|agenda|minutes|transcript|archive|attachment|copy|version"
                 rf"|section)(?:e?s)?\b{_HEADS}|(?-i:it|this|that)\b|https?\b|www\.|[/~]"
                 r"|[\w-]+\.(?:pdf|md|txt|csv|tsv|json|ya?ml|xml|html?|docx?|xlsx?|pptx?|odt|ods|rtf|png|jpe?g|gif"
                 r"|svg|webp|zip|tar|gz|tgz|py|ipynb|js|ts|sh|sql|log|epub|mp4|mov|mp3|wav)\b)")
# Where such a tail must end for it to name no one: the bracket closes or the phrase or line ends ("to the owner
# (twice).", not "to the owner (twice and Bob)")
_TAIL_ENDS = r"(?=\s*(?:[).,;:!?]|$)|[ \t]*\n)"
# "i.e.", "e.g." or "viz.": what follows names this send's recipient again ("to the user, i.e. Bob")
_THAT_IS = r"(?:i\.e|e\.g|viz)\."
# What a connector or "i.e.", "e.g." or "viz." may name without naming anyone: a sent thing, "the Q3 one", a format,
# a link, charts or figures, or a review word ("to you, e.g. the Q3 one", "i.e. the PDF", "for review and approval";
# not "to you, i.e. Bob", "and that is Bob", "and it will reach Bob", nor a thing for someone: "plus a copy for Bob");
# and after "that is",
# nothing more ("to you, that is all", "and that is all"; but "and all" may be everyone)
_AN_EXAMPLE = (rf"(?!that\s+is\b|that['’]s\b|(?-i:it|this|that)\b(?!{_TAIL_ENDS}))"
               rf"(?:{_A_SENT_THING}|(?:the|this|that)\s+(?:[\w-]+\s+){{0,2}}?ones?\b{_HEADS}"
               r"|(?:(?:the|a|an)\s+)?(?:pdf|csv|xlsx?|docx?|pptx?|markdown|md|html?|json|png|zip)\b(?!['’.-])"
               r"|(?:(?:a|the)\s+)?(?:shared\s+)?links?\b|(?:all\s+)?(?:(?:the|my|our|your)\s+)?(?:[\w-]+\s+)?"
               r"(?:charts|tables|figures|numbers|graphs|plots|slides|files|attachments|notes|details)\b"
               r"|(?:approval|sign-?off|feedback|comments|reference|records|context)\b)"
               r"(?!\s+(?:for|to|with|from|by|via|cc)\b)")
# An adverb after a self word: "to the owner earlier", "via Telegram successfully"
_SELF_ADVERB = (r"(?:earlier|just|now|already|today|directly|first|again|too|also|both|twice|once|successfully"
                r"|personally|privately|separately|right\s+away)\b")
# A word that joins the owner and someone else: "to you and Bob", "to the owner, namely Bob", "to you not just Bob"
_CONNECTOR = (r"(?:and(?:\s+(?:also|then|later|afterwards))?|&|as\s+well\s+as|then|plus|also|namely|including"
              r"|not\s+(?:just|only)|but\s+also|in\s+particular|that\s+is|that['’]s|specifically|especially|meaning"
              r"|mainly|mostly|notably)")
_NOTHING_MORE = rf"(?:(?<=\bthat is )|(?<=\bthat['’]s )|that\s+is\s+|that['’]s\s+)(?:all|everything|it)\b{_TAIL_ENDS}"
# What a bracket, a dash or another mark after a self word may hold without naming anyone: a sent thing, a count
# or an adverb ("to you (report.pdf)", "to you - 3 pages", "to the owner (twice)").
_A_COUNT = (r"\d+\s*(?:pages?|slides?|sheets?|charts?|tables?|files?|attachments?|parts?|sections?|rows?|lines?|words?"
            r"|items?|kb|mb|gb|bytes?|min(?:ute)?s?|sec(?:ond)?s?|hours?|days?)\b")
_NOT_A_TAIL = rf"(?!(?:{_A_SENT_THING}|{_A_COUNT}|{_SELF_ADVERB}){_TAIL_ENDS})"
# What may end the sentence after a mark or on the next line without naming anyone: a note, a version, a date, a
# clock time, an adverb, a closer or a heading ("to the owner, FYI", ", v2", ", 2026-10-24", ", 10:05 UTC", ",
# finally", "to you<newline>Done", "<newline>Summary:"). Only at the end of the sentence, so nothing after it is hidden
# ("to the owner, FYI Bob", "<newline>Summary:<newline>Bob").
_OWNER_ONLY_END = (r"(?:fyi|asap|btw|nb|ps|v\d+(?:\.\d+)*|\d{4}-\d\d-\d\d|\d{1,2}:\d\d(?:\s*(?:utc|gmt|[ap]m))?"
                   r"|finally|also|too|again|anyway|instead|eventually|thanks|thank\s+you|cheers|done|all\s+good"
                   r"|see\s+attached|(?:summary|next\s+steps|details|notes?|attachments?|highlights|results|changes"
                   r"|status):)[ \t]*$")
# A time or a place after "to <self> at" that names no one: "at lunch", "at the usual time", "at the office" (not
# "at lunch with Bob", "at the office of Bob", "at Worktime HQ")
_AT_TAIL = (r"(?:lunch|[a-z]+time|dinner|breakfast|night|dawn|dusk|work|home|first|last|least|most|all|length|hand"
            rf"|the\s+(?:(?:[\w-]+\s+)?(?:time|moment|hour|meeting|weekend)s?|office))\b{_TAIL_ENDS}")
# A short prepositional tail after a self word: "to the user as a PDF", "on Monday", "for review", "by 5pm" (up to
# three words, none of them a preposition, a connector, an adverb or a self word, so the tail is read one way only
# and never starts another self word's reading: "via telegram via telegram ..." is linear); a clock time is one word
# ("by 9:30")
_PREPOSITION = r"(?:as|in|with|on|by|for|via|through|over|using)"
_PREP_TAIL = (rf"(?:{_PREPOSITION}|at(?!\s+(?:{_A_TIME}|{_AT_TAIL})))"
              rf"(?:\s+(?!{_SELF_ADVERB}|(?:{_PREPOSITION}|at|to|or|not|but|that|right|{_CONNECTOR}|{_SELF}|you|yourself"
              r"|me|myself|send_file|send_telegram)\b)[\w'’-]+(?::\d\d)?){1,3}")
# After via/through/using/on/over: Telegram, the bot, the chat, a DM, here, a link, the sending tool by name
# (send_file, send_telegram) or a time word.
_NOT_A_TRANSPORT = (r"(?!(?:the|a|an|your|my|our|this)\b|(?:telegram|bot|chat|dms?)(?:\s+(?:telegram|bot|chat|dms?))*\b"
                    rf"{_SELF_ENDS}|(?:here|(?:shared\s+)?links?|time|schedule|request|demand|completion|arrival|track"
                    rf"|hold|weekend|morning|afternoon|evening|night)\b{_HEADS}|send_(?:file|telegram)\b)")
# A participle that says how the report was made: "charts generated using matplotlib", "built using pandas"
_PRODUCED = ("generated", "rendered", "built", "made", "created", "produced", "formatted", "written", "wrote", "drawn",
             "plotted", "computed", "calculated", "compiled", "prepared", "designed", "styled", "drafted", "assembled",
             "analysed", "analyzed", "summarised", "summarized", "charted", "visualised", "visualized", "modelled",
             "modeled", "derived", "extracted", "processed", "cleaned", "converted", "exported", "edited", "done",
             "typeset", "laid out")
_USING = ("using" + "".join(rf"(?<!\b{w} using)" for w in _PRODUCED)  # checked only where "using" is
          + r"(?!\s+(?:the|a|an|your|my|our|this)\s+(?:[\w-]+\s+)?(?:data|dataset|numbers|figures|template|format"
          rf"|layout|style|results|stats|statistics|metrics)\b{_HEADS})")
_ELSEWHERE = re.compile(
    # every alternative starts at a word boundary or where an address can start: one test of that first, so a
    # long word costs one step per character, not one per alternative
    r"(?:\b|(?<![\w.+-])(?=[\w.+-]))(?:"
    # 1. mail
    r"\b(?:e-?)?mail(?:ed|ing)\b"
    r"|\b(?:e-?)?mails?\b(?=\s*(?:[.,;:!?)]|$)"
    r"|\s+(?:was|were|is|has|have|sent|to|via|by|with|on|at|for|about)\b)"
    r"|\b(?:via|using|in|into|over|through|by)\s+(?:(?:the|an?|your|my)\s+)?(?:e-?)?mail\b(?!-)"
    r"|\bsend_e?mail\b"
    # 2. a named channel
    r"|\b(?:via|on|over|through|by|in|into|using)\s+(?:(?:the|a|your|my)\s+)?(?:slack|whatsapp|signal|discord"
    r"|sms|imessage|teams|messenger|wechat|gmail|outlook|linkedin|twitter)\b(?!-)"
    # a text message or an SMS after in/as too ("in a text message", "as an SMS"; not "in a text file", "as text")
    r"|(?:\b(?:via|over|by)\s+(?:an?\s+)?(?:text|sms)|\b(?:in|as)\s+(?:an?\s+(?:text|sms)|sms|texts?\s+messages?))"
    r"\b(?!-)(?!\s+(?:file|form|format)s?\b)"
    r"|\bby\s+(?:post|courier|hand|fax)\b"
    r"|\bchat[ _-]?id\b\s*(?:[=:]\s*)?-?\d|\bchat\s+-\d{5,}"  # another chat, by its id (not "chat 12345 logs")
    # ... anyone or anything right after "<the message> by" or "sent by" ("I sent the report by Bob", "Message
    # sent by @bob", "by sendmail", "by the team") but you, a time, the owner's chat, a link, a gerund or a
    # manner ("by attaching it", "by request", "by direct message", "by Friday EOD", "by our bot"); not "sorted
    # by date", "requested by you". A closed list of what is no one: an unlisted word errs to "Could not confirm"
    r"|(?:\b(?:e-?mails?|messages?|reports?|notes?|reminders?|summary|summaries|invites?|notifications?|alerts?"
    r"|digests?|heads-up)|\bsent|\bforwarded)\s+by\s+"
    rf"(?!{_A_TIME}|\d|(?:you|yourself|me|myself|noon|midnight|midday|tonight|tomorrow|today|eod|eob|cob|then|now"
    r"|lunch|next\s+week|end\s+of|this\s+(?:morning|afternoon|evening|week)|(?:a\s+)?(?:shared\s+)?links?|request"
    r"|default|itself|mistake|accident|schedule|way\s+of|(?:direct|private)\s+messages?"
    r"|(?:the\s+)?(?:deadline|due\s+date|scheduled\s+time)|(?-i:[a-z]+ing)"
    # ... nor a day ("Friday EOD", "Monday 5pm"), a business or holiday time, "<the agreed> time", the sending
    # tool or a file
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday|eo[dwmqy]|t\+\d|q[1-4])\b"
    r"|(?:christmas|xmas|easter|thanksgiving|new\s+year|[a-z]+time|close\s+of\s+(?:business|play)"
    r"|(?:the|an?|our|your|my)\s+(?:[\w-]+\s+){0,2}?(?:time|deadline|date|cut-?off|hour|day|week|month|meeting"
    rf"|call|stand-?up)s?|send_(?:file|telegram)|files?)\b{_HEADS}"
    rf"|(?:(?:the|your|my|our)\s+)?{_A_SELF})[\w@#]"
    # ... or any other transport than Telegram, a tool, a link or a time after via/through/using, and a place
    # after on/over (not a topic: "a note on the outage"); "using" after a participle of making (_PRODUCED), or
    # before the data or a template, is how the report was made ("charts generated using matplotlib", "built
    # using pandas", "using the latest data"), not how it was sent
    rf"|\b(?:via|through|{_USING})\s+(?!{_A_TIME})(?:(?:the|a|an|your|my|our|this)\s+)?{_NOT_A_TRANSPORT}"
    r"(?![\w.]*-style\b)[\w@#]"
    rf"|\b(?:on|over)\s+(?!{_A_TIME})(?!the\s+(?:same|next|previous|last)\s+page\b)"  # no place
    rf"(?:{_NOT_A_TRANSPORT}(?-i:(?![a-zß-ÿ\d_])\w)|@\w|(?:the|a|an|your|my|our|this)\s+(?:[\w-]+\s+)?"
    r"(?:wiki|website|site|page|board|forum|server|channel|group|platform|portal|feed|blog|intranet|drive|cloud"
    r"|repo|repository|tracker)s?\b)"
    # 3. a recipient
    # (each idiom is checked only where "to" is: a check before it would run at every character)
    r"|\bto(?<!\bdue to)(?<!\bprior to)(?<!\bup to)(?<!\bthanks to)(?<!\baccording to)(?<!\bcompared to)"
    r"(?<!\bnext to)(?<!\baddition to)(?<!\border to)(?<!\bclose to)(?<!\bsimilar to)(?<!\brelative to)"
    r"(?<!\brespect to)(?<!\bregard to)(?<!\blink to)(?<!\blinks to)(?<!\bconvert to)(?<!\bconverted to)"
    r"(?<!\bconverting to)(?<!\bexported to)(?<!\btranslated to)(?<!\brendered to)(?<!\bformatted to)"
    r"(?<!\bcompiled to)(?<!\bchanged to)(?<!\bswitched to)"
    rf"\s+{_NOT_A_RECIPIENT}"
    # "a link to the team" is sent to the team; "a link to the report" points at the report
    rf"|\blinks?\s+to\s+(?!{_A_SENT_THING}){_NOT_A_RECIPIENT}"
    rf"|\blinks?\s+to\s+(?:[^.,;:!?\n]|\.(?=\w)){{0,60}}?\s(?:(?:and|&)\s+|for\s+(?=(?-i:(?![a-zß-ÿ\d_])\w[a-zß-ÿ])"
    rf"|[@#]\w)){_NOT_A_VERB}{_NOT_A_RECIPIENT}"
    # "to you and Bob", "to the owner and the team", "via Telegram and Bob", "to the owner earlier and Bob", "to
    # the owner as well as Bob", "to the owner (Bob)", "to the owner - Bob", "to the user: Bob", "to the user,
    # then Bob", "to the user, Bob and Carol", "to the user<newline>Bob" (not "to you and archived it", "to you
    # and on time", "to the owner (see above)", "to the owner - done", "to the owner; it covers Q3", a vocative
    # "to you, Tim", nor a line longer than three words); a time, an "at" place or a bracketed aside may come
    # between ("to the owner at dinner, then Bob", "to the owner (twice) and Bob")
    r"|\b(?:to|via|through|over|on|using)\s+(?:(?:the|your|my)\s+)?(?:you|yourself|me|myself|owner|user|chat|dms?"
    rf"|bot|telegram|send_file|send_telegram)\b(?:\s+{_SELF_ADVERB}|\s+at\s+(?:{_A_TIME}|{_AT_TAIL})"
    rf"|\s*\([^()\n]{{0,60}}\)|\s+{_PREP_TAIL})*"
    rf"(?:(?:\s*[,;:(—–-]\s*|\s+){_CONNECTOR},?\s+(?!{_AN_EXAMPLE}|{_NOTHING_MORE})|(?<=\))\s+"
    rf"|(?:\s*[,;:(—–]\s*|\s+){_THAT_IS},?\s*(?!{_AN_EXAMPLE})"  # "to the user, i.e. Bob"
    r"|(?:\s*\(\s*|\s+[-–—]\s+|\s*(?:[;—]|:(?!(?<=\d:)\d))\s*"  # not the colon of a clock time ("by 9:30")
    r"|(?<!\byou)(?<!\byourself)(?<!\bme)(?<!\bmyself),\s*"
    r"|[ \t]*\n[ \t]*(?:(?:then|plus|also)\s+)?(?=\S+(?:[ \t]+\S+){0,2}[ \t]*(?:\n|$))"
    r")"
    rf"{_NOT_A_TAIL}(?!{_THAT_IS}|{_CONNECTOR},?\s+(?:{_AN_EXAMPLE}|{_NOTHING_MORE}|(?:you|yourself|me|myself)\b{_TAIL_ENDS})"
    rf"|{_OWNER_ONLY_END})|\s+for\s+(?=(?-i:(?![a-zß-ÿ\d_])\w[a-zß-ÿ])|[@#]\w))"
    rf"(?:only\s+)?{_NOT_A_VERB}{_NOT_A_RECIPIENT}"
    # "to the user at Bob", "to you at the London office" (not "to the owner at 7am", "at once", "at lunch", "at
    # the usual time", "at the office"), and an address anywhere in the sentence
    r"|\bto\s+(?:(?:the|your|my)\s+)?(?:you|yourself|me|myself|owner|user|chat|dms?|bot|telegram)\s+at\s+"
    rf"(?!{_A_TIME}|\d|once\b|{_AT_TAIL})[\w@#]"
    r"|(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"  # anchored: a long word is scanned once, not from each letter
    rf"|\bshared\s+(?:\S+\s+){{0,4}}?with\s+{_NOT_A_RECIPIENT}"
    rf"|\bb?cc(?:['’]?e?d)?\s+(?!(?:you|yourself|me|myself)\b"
    rf"|(?:(?:the|your|my)\s+)?(?:owner|user|chat)\b{_SELF_ENDS})"
    r"[\w@#]"
    # "copied in the team", "copied Bob", not "copied the file"
    rf"|\bcopied\s+in\s+{_NOT_A_RECIPIENT}"
    r"|\bcopied\s+(?:(?-i:(?![a-zß-ÿ\d_])\w[a-zß-ÿ]+)\b(?![.'’]\w)|[@#]\w|[\w.+-]+@)"
    r"|\b(?:in|on|into|via|over)\s+#\w)",
    re.IGNORECASE)
# A later clause of its own that claims no completed send, up to the next mark or "but"/"and": a future, a negation or
# an instruction ("; I will send it via Slack tomorrow", ", but did not send it via email", ", not to Bob", "; please
# forward it to Bob"). It is not this send's destination. Not "not just Bob" (Bob got it too), nor a relative clause
# or another subject ("who will forward it to Bob", "Bob will review it"); the verbs are in lowercase ("to you and
# Will" names Will).
# What main read as a recipient whatever followed ("to the user", "to the bot", "via send_file")
_MAIN_RECIPIENT = re.compile(r"\b(?:user|bot|send_file)\b", re.IGNORECASE)
_PRONOUN = r"(?:i|we|you|it|they)"
_NOT_DONE = re.compile(
    r"(?:[;,:—–]\s*(?:(?:but|and|though|yet)\s+)?|(?<!\s)\s+(?:but|and|though|yet)\s+)"  # one try per run of spaces
    rf"(?:(?:{_PRONOUN}\s+)?(?-i:will|shall|(?:am|are|is)\s+going\s+to|plan\s+to|(?:did|do|have)(?:\s+not|n['’]t)"
    r"|won['’]t|can(?:not|['’]t)?|could|should|[Pp]lease)"
    rf"|{_PRONOUN}['’](?:ll|m\s+going\s+to|re\s+going\s+to)|(?-i:not)\s+(?:to|yet))\b"
    r"(?:(?!\b(?:but|and)\b)[^;,:—–()\n])*",
    re.IGNORECASE)
# A single _, * or backtick around a word is styling, as ** is: "via _Slack_", "to `Bob`". The word does not hold its
# own mark and is at most 60 characters, so an unmatched "_" does not re-read the rest of a long word ("-_a-_a-_a...")
_STYLED = re.compile(r"(?<![\w*`])([_*`])((?:(?!\1)[^\s*`]){1,60}?)\1(?![\w*`])")
# The self heads main took for the owner's chat on their own, copied from main's rule: "to you", "to me", "to the
# owner", "to (the/your/my) DM, chat or Telegram", "via (the/a/your) Telegram, bot or chat". They stay on the rules
# above.
_MAIN_EXEMPT_TO = r"(?:you|yourself|me|(?:(?:the|your|my)\s+)?(?:owner|chat|dm|telegram(?!\s+(?:group|channel)\b)))\b"
_MAIN_EXEMPT_VIA = r"(?:(?:the|a|your)\s+)?(?:telegram|bot|chat)\b"
# Every other self head after "to" or "via" ("to the user", "to our bot", "to the user's DMs", "via this chat", "via
# send_file") main read as someone else whatever followed. After it the rest of the sentence must be owner-only tails
# (_OWNER_ONLY_REST), all of it: anything left over could be someone ("with Bob", "as Bob", "so Bob can read it", "for
# Иван") and gives plausible, as on main. A head missing from the exempt list costs a "Could not confirm", not an
# overclaim.
_NEW_SELF_HEAD = re.compile(
    rf"\bto\s+(?!{_MAIN_EXEMPT_TO})(?:(?:the|your|my|our|this)\s+)?{_A_SELF_PHRASE}"
    rf"|\bvia\s+(?!{_MAIN_EXEMPT_VIA})(?:(?:the|a|your|my|our|this)\s+)?(?:{_A_SELF_PHRASE}|send_(?:file|telegram)\b)",
    re.IGNORECASE)
# A short prepositional phrase that names no one: a format, a time, a summary or charts, a review, "as requested"
_FORMAT = r"(?:pdf|csv|xlsx?|docx?|pptx?|markdown|md|html?|json|png|zip|txt)"
_OWNER_ONLY_PREP = (rf"(?:(?:as|in)\s+(?:(?:a|an)\s+)?\.?{_FORMAT}(?:\s+(?:file|attachment|document))?\b"
                    r"|(?:at|by)\s+the\s+end\s+of\s+(?:the|this|next)\s+(?:day|week|month|quarter|year)\b"
                    rf"|(?:on|by)\s+{_A_TIME}"
                    r"|with\s+(?:(?:a|an|the)\s+)?(?:short\s+|brief\s+)?(?:summary|charts?|tables?|figures|graphs?|notes?"
                    r"|details|attachments?)\b"
                    r"|for\s+(?:your\s+)?(?:review|approval|sign-?off|feedback|reference|records|comments)\b"
                    r"|as\s+(?:requested|discussed|promised|agreed|planned|asked|usual)\b)")
# A sent thing or a place in an owner-only tail has only these words before its noun ("the Q3 report", "the usual
# meeting"); any other word could be someone ("the Bob report", "the team's notes", "a copy for Bob's file")
_PLAIN_WORD = (r"(?:q[1-4]|h[12]|fy\d+|\d{4}|v\d+|final|weekly|monthly|daily|quarterly|annual|yearly|latest|updated"
               r"|revised|full|new|short|brief|draft|attached|requested|same|whole|first|last|previous|next|current|main|original"
               r"|usual|agreed|scheduled|pdf|csv|xlsx?|docx?|pptx?|html?|json|png|zip|md|markdown|status|progress|sales"
               r"|summary|test|pull|release|project)")
_NO_NAME_INSIDE = (rf"(?!(?:the|a|an|your|my|our|this|that|these|those)\s+(?:{_PLAIN_WORD}\s+){{0,3}}"
                   rf"(?!{_PLAIN_WORD}\b)[\w.'’-]+\s+[\w.'’-])")
# One owner-only tail: a self adverb, an "at" time or place, a bracket that holds only a sent thing, a count, an adverb
# or such a phrase ("(twice)", "(report.pdf)", "(as a PDF)"; not "(Bob)"), "via Telegram", the phrase itself (also
# after a comma), a closer or heading on the next line, a note, version, date or time after a comma ("FYI", "v2"), or
# "i.e.", "e.g.", "viz." or "namely" before a sent thing. A tail once taken is not read again, so the longer phrase
# comes first ("at the end of the day" before "at the end").
_OWNER_ONLY_TAIL = (rf",?\s+{_OWNER_ONLY_PREP}|\s+{_SELF_ADVERB}|\s+at\s+(?:{_A_TIME}|{_NO_NAME_INSIDE}{_AT_TAIL})"
                    rf"|\s*\(\s*(?:{_NO_NAME_INSIDE}{_A_SENT_THING}|{_A_COUNT}|{_SELF_ADVERB}|{_OWNER_ONLY_PREP})\s*\)"
                    rf"|\s+via\s+telegram\b|[ \t]*\n[ \t]*{_OWNER_ONLY_END}"
                    rf"|\s*,\s*{_OWNER_ONLY_END}|\s*(?:,\s*)?(?:{_THAT_IS}|namely),?\s+{_NO_NAME_INSIDE}{_A_SENT_THING}")
# The rest of the sentence after a new self head, all of it owner-only tails. Each tail is atomic (a later tail that
# fails does not make an earlier one try another reading) and the loop possessive, so a long rest is linear.
_OWNER_ONLY_REST = re.compile(rf"(?:(?>{_OWNER_ONLY_TAIL}))*+\s*", re.IGNORECASE)
# What a sentence may say before a new self head ("Done: I have also sent the Q3 report with the CI results"): an
# opener or a list mark, a subject, auxiliaries and self adverbs, a send verb and a sent thing (it, them, a file name,
# or a determiner, plain words and a noun) with its own attachment, or the passive "<sent thing> sent". Anything left
# over could be someone ("I sent Bob the report", "For Bob,", "a copy for Bob", "Bob's report") and gives plausible,
# as on main. A word before the noun is taken only where another listed word follows it, and every loop is
# possessive, so the check is linear.
_SENT_NOUN = (r"(?:summary|summaries|copy|copies|(?:report|message|e-?mail|note|reminder|digest|update|file|doc"
              r"|document|draft|chart|table|sheet|spreadsheet|deck|slide|presentation|result|log|image|screenshot"
              r"|attachment|version|link)s?|data)")
_SENT_WORD = rf"(?:{_PLAIN_WORD}|ci|{_SENT_NOUN})"
_SENT_PHRASE = rf"(?:{_SENT_WORD}\s++(?={_SENT_WORD}\b))*+{_SENT_NOUN}\b"
_SENT_FILE = (r"[\w-]++\.(?:pdf|md|txt|csv|tsv|json|ya?ml|xml|html?|docx?|xlsx?|pptx?|odt|ods|rtf|png|jpe?g|gif|svg"
              r"|webp|zip|tar|gz|tgz|py|ipynb|js|ts|sh|sql|log|epub|mp4|mov|mp3|wav)\b")
_SENT_DET = r"(?:the|a|an|my|our|your|this|today['’]s)\s++"
_BEFORE_HEAD = re.compile(
    r"\s*+(?:(?:done:|update:|as\s+requested,)\s*+)?+(?:(?:[-*•]|\d+[.)])\s++)?+"
    r"(?>(?:(?:i|we)(?:['’]ve)?\s++)?+(?:(?:have|had|just|also|already|earlier)\s++)*+"
    r"(?:sent|forwarded|shared|delivered|posted|uploaded|attached)\s++"
    rf"(?:(?:it|them)\b|{_SENT_FILE}|{_SENT_DET}{_SENT_PHRASE})"
    rf"(?:\s++(?:with|about|from|for)\s++{_SENT_DET}{_SENT_PHRASE})?+"
    rf"|(?:(?:it|them)\b|{_SENT_FILE}|(?:{_SENT_DET})?+{_SENT_PHRASE})\s++(?:(?:was|were|has\s+been|have\s+been)\s++)?+"
    r"(?:(?:just|also|already|earlier)\s++)*+(?:sent|forwarded|delivered))"
    rf"(?:\s++{_SELF_ADVERB})*+\s*+",
    re.IGNORECASE)
# Main's rule, verbatim. Where it finds a destination in the claim's clause, main scored a send_file plausible; there,
# exact needs each place it found to start a new self head (and the rest to be owner-only tails, above). Any other
# place main found ("to notify Bob", "to myself", "via a link", "according to Bob", "on text") stays plausible.
_MAIN_ELSEWHERE = re.compile(
    r"\b(?:e-?)?mail(?:ed|ing)\b"
    r"|\b(?:e-?)?mails?\b(?=\s*(?:[.,;:!?)]|$)|\s+(?:was|were|is|has|have|sent|to|via|by|with|on|at|for|about)\b)"
    r"|\b(?:in|into|over|through|by)\s+(?:(?:the|an?)\s+)?(?:e-?)?mail\b"
    r"|\b(?:via|on|over|through|by|in|into|using)\s+(?:(?:the|a)\s+)?(?:slack|whatsapp|signal|discord|sms"
    r"|imessage|teams|messenger|wechat|gmail|outlook|linkedin|twitter|text)\b"
    rf"|\bvia\s+(?!{_MAIN_EXEMPT_VIA})\w"
    rf"|\bto\s+(?!{_MAIN_EXEMPT_TO})"
    r"(?!(?:review|read|check|confirm|approve|see|look|keep|share|sign|use|be|date)\b)[\w@#]",
    re.IGNORECASE)


def evidence_level(claim: Claim, ev: Evidence) -> str:
    """How well one successful call supports one claim: exact, plausible or none."""
    kind = CLAIM_KINDS[claim.kind]
    if ev.tool_name not in kind.capable_tools:
        return "none"
    # a rule that reads no arguments is judged by the claim alone (its target;
    # for a Telegram send its clause too), whether the arguments were kept ({}
    # from the runner: a producer or a Telegram send carries none the verifier
    # uses) or never given
    if ev.tool_name in _PRODUCERS:  # writes to memory or the companion: no destination
        return "none" if claim.target else "plausible"
    if ev.tool_name == "send_file":  # a Telegram send: no named address, and no e-mail
        if claim.target:
            return "none"
        # the whole sentence from the claim's clause on: "to the user — Bob", "to the user: Bob"
        sentence = claim.sentence or claim.clause or claim.text
        clause = claim.clause or ""
        if clause and sentence.startswith(clause) and not _MAIN_RECIPIENT.search(sentence):
            # only past the claim's clause (main read no further), and not after a word main took for a
            # recipient whatever followed: the dropped clause can then raise no score above main
            sentence = clause + _NOT_DONE.sub("", sentence[len(clause):])
        sentence = _STYLED.sub(r"\2", sentence)
        if _ELSEWHERE.search(sentence):
            return "plausible"
        # main's verdict, on main's input: each place it found must start a new self head
        read = claim.clause or claim.text
        if any(not _NEW_SELF_HEAD.match(read, m.start()) for m in _MAIN_ELSEWHERE.finditer(read)):
            return "plausible"
        head = _NEW_SELF_HEAD.search(sentence)
        if head and not (_OWNER_ONLY_REST.fullmatch(sentence, head.end())
                         and _BEFORE_HEAD.fullmatch(claim.opening + sentence[:head.start()])):
            return "plausible"
        return "exact"
    if not ev.args:  # names-only evidence from a legacy caller
        return "plausible"
    if ev.tool_name == "bash":
        return _bash_level(claim, ev)
    if ev.tool_name == "write_file":  # an explicit destination: it must BE the target
        path = ev.args.get("path") or ev.args.get("file_path") or ""
        return "exact" if not claim.target or _same_path(claim.target, path) else "none"
    if ev.tool_name == "send_email":  # whole addresses: malice@x.io is not alice@x.io
        recipients = _addresses(f"{ev.args.get('to', '')} {ev.args.get('cc', '')}")
        return "exact" if not claim.target or claim.target in recipients else "none"
    return _code_level(claim, ev.args.get("code", ""))


# Report claims (F099 gate hardening B). A subtask's final report and a
# continuation turn's note speak in verdicts and the passive voice ("Email
# sent.", "PR #12 merged", "test 6 FAILED: the cancel did not take effect"),
# not only in the first person, and nobody reads a correction after them.
# verify_report checks them against the calls recorded for the work; the chat
# path (verify) does not use these patterns.
REPORT_KINDS: dict[str, ClaimKind] = {
    "merge": ClaimKind("bash", frozenset({"bash", "run_python"})),
    "pr_create": ClaimKind("bash", frozenset({"bash", "run_python"})),
    "test_run": ClaimKind("bash", frozenset({"bash", "run_python"})),
    "cancel": ClaimKind("cancel_task", frozenset({"cancel_task", "dag", "dag_manage", "bash"})),
}
# What a command must RUN to back the claim: read from the parsed invocations
# (the shell wrappers -- `bash -lc`, `env`, `cd x &&` -- already unwrapped), never
# from the command text, so `echo merge`, `cat pytest.log` and `grep 'gh pr
# merge'` back nothing.
_TEST_PROGRAMS = frozenset({"pytest", "py.test", "tox", "nox", "vitest", "jest", "phpunit", "rspec"})
_TEST_RUNNERS = frozenset({"npm", "pnpm", "yarn", "make", "just", "go", "cargo", "mvn", "gradle"})
# Modes that exit 0 without doing what the claim says: help and version
# output, a dry run or a browser form (`gh pr create --dry-run|--web`), a
# merge only scheduled or unscheduled (`gh pr merge --auto|--disable-auto`),
# and test runs that list or collect but execute nothing.
_HELP_FLAGS = frozenset({"-h", "--help", "--version"})
_NOT_CREATING = frozenset({"--dry-run", "-w", "--web"})
_NOT_MERGING = frozenset({"--auto", "--disable-auto"})
_NOT_TESTING = frozenset({
    "--collect-only", "--co", "--fixtures", "--fixtures-per-test", "--markers", "--setup-plan", "--setup-only",
    "--listTests", "--showConfig", "--list", "--listenvs", "--listenvs-all", "--showconfig", "--notest",
    "--list-sessions", "--list-tests", "--list-tests-xml", "--list-suites", "--list-groups", "-list", "--no-run",
    "--dry-run", "-DskipTests", "-Dmaven.test.skip",
})
_GH_RUN_EXIT_STATUS = frozenset({"--exit-status"})  # gh run view/watch must carry this to reflect CI outcome
_NOT_TESTING_SHORT = {"pytest": frozenset({"-V"}), "py.test": frozenset({"-V"}),
                      "tox": frozenset({"-l", "-a", "-n"}), "nox": frozenset({"-l"})}
_PR_NUMBER = re.compile(r"/pull/(\d+)|^#?(\d+)$")
_GH_API_FIELDS = ("-f", "-F", "--field", "--raw-field", "--input")


def _gh_api_method(args: tuple[str, ...]) -> str:
    """The HTTP method a `gh api` call uses: `-X`/`--method` when given, else
    POST if it adds parameters and GET if not (`gh help api`)."""
    method, fields = None, False
    for i, a in enumerate(args):
        if a in ("-X", "--method"):
            method = args[i + 1] if i + 1 < len(args) else ""
        elif a.startswith("--method="):
            method = a[len("--method="):]
        elif a.startswith("-X"):
            method = a[2:]
        elif a in _GH_API_FIELDS or a.startswith(("--field=", "--raw-field=", "--input=")) \
                or (a[:2] in ("-f", "-F") and not a.startswith("--")):
            fields = True
    if method is not None:
        return method.upper()
    return "POST" if fields else "GET"


# gh options that take a value (`gh help api`, `gh help pr merge`, the global `-R`): the value is text -- a
# title, a body, a field -- never the command's operand, so `--body 'see /pull/99'` names no PR 99.
_GH_VALUE_OPTIONS = frozenset({
    "-R", "--repo", "--hostname", "-X", "--method", "-f", "-F", "--field", "--raw-field", "--input", "-H",
    "--header", "-q", "--jq", "-t", "--template", "--cache", "-p", "--preview", "--subject", "-b", "--body",
    "--body-file", "-A", "--author-email", "--match-head-commit",
})


def _gh_operands(args: tuple[str, ...]) -> list[str]:
    """The operands of a gh invocation: ``_positional`` without the values its options take."""
    found: list[str] = []
    value = False
    for a in args:
        if value:
            value = False
        elif a in _GH_VALUE_OPTIONS:
            value = True
        elif not a.startswith(("-", "\n", "\t")):
            found.append(a)
    return found


def _pr_numbers(args: list[str]) -> set[str]:
    """The pull-request numbers a command names (`4`, `#4`, `.../pull/4`, `.../pulls/4/merge`)."""
    found: set[str] = set()
    for a in args:
        m = _PR_NUMBER.search(a) or re.search(r"/pulls/(\d+)", a)
        if m:
            found.add(next(g for g in m.groups() if g))
    return found


def _has_flag(args: tuple[str, ...], flags: frozenset[str]) -> bool:
    """True if ``args`` carries one of ``flags``, bare or as ``--flag=value``."""
    return any(a.split("=", 1)[0] in flags for a in args)


def _report_hit(kind: str, prog: str, args: tuple[str, ...], number: str | None, *, pr: bool = False) -> bool:
    """True if this one invocation does what a report claim of ``kind`` says.

    ``pr``: the claim names a pull request, which a local `git merge` does not merge."""
    base = _base(prog)
    pos = _gh_operands(args) if base == "gh" else _positional(args)
    if _has_flag(args, _HELP_FLAGS):
        return False
    if kind in ("merge", "pr_create") and base in ("gh", "hub", "git"):
        if base == "gh" and pos[:1] == ["api"]:
            paths = pos[1:]
            method = _gh_api_method(args)
            # each endpoint acts only on its own method: a GET of
            # `pulls/4/merge` asks whether it was merged
            if kind == "merge":
                if any(re.search(r"/pulls/\d+/merge$", p) for p in paths):
                    if method != "PUT":
                        return False
                elif not (any(re.search(r"/merges$", p) for p in paths) and method == "POST"):
                    return False
            elif not (any(re.search(r"/pulls$", p) for p in paths) and method == "POST"):
                return False
            named = _pr_numbers(paths)
            return not (kind == "merge" and number and named and number not in named)
        if kind == "merge":
            if base == "git":
                return not pr and pos[:1] == ["merge"]
            if base == "gh" and pos[:2] == ["pr", "merge"]:
                if _has_flag(args, _NOT_MERGING):
                    return False
                named = _pr_numbers(pos[2:])
                return not (number and named and number not in named)
            return False
        if base == "gh" and pos[:2] == ["pr", "create"]:
            return not _has_flag(args, _NOT_CREATING)
        return base == "hub" and pos[:1] == ["pull-request"]
    if kind != "test_run":
        return False
    if _has_flag(args, _NOT_TESTING) or _has_flag(args, _NOT_TESTING_SHORT.get(base, frozenset())) \
            or (base == "vitest" and pos[:1] == ["list"]):
        return False
    if base in _TEST_PROGRAMS:
        return True
    if base == "gh":
        if pos[:2] == ["pr", "checks"]:
            return True
        # `gh run list` only lists runs; `gh run view/watch` require --exit-status
        # to reflect the run's outcome — without it they exit 0 even on failure
        if pos[:1] == ["run"] and pos[1:2] in (["view"], ["watch"]):
            return _has_flag(args, _GH_RUN_EXIT_STATUS)
        return False
    if base in _INTERPRETERS or base.startswith("python3."):
        if "-m" in args:
            return args[args.index("-m") + 1:][:1] in (("pytest",), ("unittest",))
        return bool(_TESTY.search(" ".join(pos[:1])))
    if base in _TASK_RUNNERS or base in _TEST_RUNNERS:
        # `uv run pytest`, `npm run test`, `make test`, `go test ./...`
        return any(a in _TEST_PROGRAMS or a in ("test", "unittest") for a in pos[:4])
    return _is_script(prog) and bool(_TESTY.search(prog))


def _test_outcome(claim: Claim) -> str | None:
    """What a test claim says the run gave: "pass", "fail", or None ("I ran the tests")."""
    if claim.kind != "test_run":
        return None
    if re.search(r"\bfail", claim.text, re.IGNORECASE):
        return "fail"
    return "pass" if re.search(r"\b(?:pass|green)", claim.text, re.IGNORECASE) else None


def _report_run_level(claim: Claim, ev: Evidence) -> str:
    """Whether one recorded bash or run_python call RAN what a report claim says."""
    number = (m.group(1) if (m := re.search(r"#(\d+)", claim.text)) else None)
    pr = bool(re.search(r"\bPR\b|\bpull\s+request|#\d+", claim.text, re.IGNORECASE))
    if _test_outcome(claim) is not None:
        # the exit status is the LAST command's: only a test run there gave it
        # (a run_python exit is the script's, not the tests')
        runs = ev.runs if ev.tool_name == "bash" else None
        if not runs:
            return "none"
        prog, args, _ = runs[-1]
        if _report_hit(claim.kind, prog, args, number, pr=pr):
            return "exact"
        return "plausible" if prog.startswith("$") or _is_script(prog) else "none"
    if ev.tool_name == "run_python":
        facts = _python_facts(ev.args.get("code") or "")
        if facts is None:
            return "plausible" if EVIDENCE_TRUNCATED in (ev.args.get("code") or "") else "none"
        _matching = [i for i, (p, a) in enumerate(facts.invocations)
                     if _report_hit(claim.kind, p, a, number, pr=pr)]
        if _matching:
            # return "exact" only when the subprocess propagates failure
            # (check_call, check_output, or run(check=True)); a plain
            # subprocess.run can succeed while the inner command fails
            if any(i in facts.checked_invocations for i in _matching):
                return "exact"
            return "plausible"
        return "plausible" if facts.run_unknown else "none"
    runs = ev.runs
    if runs is None:  # unreadable: it cannot disprove anything
        return "plausible"
    if any(_report_hit(claim.kind, prog, args, number, pr=pr) for prog, args, _ in runs):
        return "exact"
    succeeded = runs[:-1] if ev.exit_code not in (None, 0) else runs
    # a script or an expansion could have run it; a read or an echo could not
    return "plausible" if any(p.startswith("$") or _is_script(p) for p, _, _ in succeeded) else "none"


_PR = r"(?:PR|pull\s+request)(?:\s*#\d+)?"
_PASSIVE = r"\s+(?:(?:was|were|has\s+been|have\s+been|got|is|is\s+now)\s+)?"
_CANCELLABLE = r"(?:task|subtask|run|job|dag|schedule|intention|work|chain)"
# The work item a cancel claim names: a runner job id, or a hex id (a whole UUID, or a prefix with a digit
# and a letter, so "decade" and a date are no id), each a whole token ("facade-1234" is none). A DAG's NAME is
# not read as one: no recorded cancel carries it (dag_manage cancels by dag_id), so it could neither
# confirm nor refute the claim, and "the DAG mid-run" would read as a name.
_JOB_ID = r"job-\d{8}-\d{6}-[0-9a-f]{6,}(?![\w-])"
_HEX_ID = (r"(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{6,})"
           r"(?![\w-])")
# after the noun: "task 8f10c3ba", "job (job-...)", "DAG: 5e6f7a8b", "task `8f10c3ba`"
_WORK_ID = rf"(?:\s+|\s*[(:#]\s*)`?(?:{_JOB_ID}|{_HEX_ID})`?"
# (pattern, kind, attempt_only). Active forms need the first person or a
# sentence opening ("Merged PR #4."): "Tim merged PR #4" is not the agent's.
_REPORT_PATTERNS: list[tuple[str, str, bool]] = [
    (rf"\b(?:e-?mails?|messages?){_PASSIVE}sent\b(?:\s+to\s+{_ADDRESS})?{_NOT_BY}", "email", False),
    (rf"{_OPENING}(?:sent|e-?mailed)\s+(?:the\s+|an\s+)?e-?mail\b(?:\s+to\s+{_ADDRESS})?", "email", False),
    (rf"\b{_PR}{_PASSIVE}merged\b{_NOT_BY}", "merge", False),
    (rf"(?:{_FIRST}|{_OPENING})merged\s+(?:the\s+)?(?:{_PR}|#\d+|branch)", "merge", False),
    (rf"\b{_PR}{_PASSIVE}(?:opened|created|raised|filed)\b{_NOT_BY}", "pr_create", False),
    (rf"(?:{_FIRST}|{_OPENING})(?:opened|created|raised|filed)\s+(?:a\s+|the\s+)?(?:new\s+|draft\s+)?{_PR}",
     "pr_create", False),
    (rf"(?:{_FIRST}|{_OPENING})cancel+ed\s+(?:the\s+|that\s+|this\s+)?"
     rf"(?:(?:[\w-]+\s+)?{_CANCELLABLE}\b(?!-)(?:{_WORK_ID})?|`?{_JOB_ID}`?)", "cancel", False),
    (rf"\b(?:{_CANCELLABLE}\b(?!-)(?:{_WORK_ID})?|{_JOB_ID}){_PASSIVE}cancel+ed\b{_NOT_BY}", "cancel", False),
    # a verdict on a cancel says one was issued, whatever it did
    (r"\bcancel(?:l?ation)?\s+(?:did\s+not|didn['’]t|failed|worked|succeeded|took\s+effect|was\s+ignored)"
     r"(?:\s+[\w-]+){0,3}", "cancel", True),
    (r"\b(?:tests?|test\s+suite|pytest|checks|CI)\s+(?:all\s+)?(?:passed|pass(?:es)?|failed|fail|(?:are|is)\s+green)\b",
     "test_run", False),
    (rf"(?:{_FIRST}|{_OPENING})ran\s+(?:the\s+|all\s+)?(?:[\w-]+\s+)?(?:tests?|test\s+suite|pytest)\b",
     "test_run", False),
]
# Earlier in the clause: the claim is denied, planned or asked for, not made
# ("No email was sent", "Next: open a PR", "make sure the tests pass").
_REPORT_HEDGE = re.compile(
    r"\b(?:no|not|never|nothing|without|will|would|should|shall|could|can|must|may|might|once|until"
    r"|need(?:s|ed)?\s+to|ha(?:ve|s)\s+to|going\s+to|want(?:s|ed)?\s+to|try(?:ing)?\s+to|next|ensure"
    r"|make\s+sure|so\s+that)\b|n['’]t\b",
    re.IGNORECASE)
# Earlier in the clause: someone else said it, or the text quotes an
# instruction -- "The subtask said the email was sent", "per the task, merge
# the PR". The agent's own "I said ..." is its own claim.
# A reporting word reaches to the end of its clause, not past a new one: in
# "The build reported a warning, but I pushed the changes" the push is claimed.
_REPORTED_SCOPE_END = re.compile(
    r"[.!?;\n]|\b(?:but|yet|however|whereas)\b|,\s*(?:so|while|although|though)\b"
    r"|\band\s+(?=(?:then\s+)?(?:(?-i:I)|we)\b)",
    re.IGNORECASE)
_REPORTED = re.compile(
    r"(?<!\bI )(?<!\bwe )(?<!\bI['’]ve )(?<!\bI have )"
    r"\b(?:said|says|stated?s?|claim(?:s|ed|ing)|report(?:s|ed|ing)|told|tells|mention(?:s|ed)"
    r"|assert(?:s|ed)|insist(?:s|ed)|indicat(?:es|ed)|repl(?:y|ies|ied)|respond(?:s|ed)"
    r"|believ(?:es|ed)|thinks|instruct(?:s|ed|ions?)|asked|requested|according\s+to|per\s+the)\b",
    re.IGNORECASE)


def _reported_before(reported: list[int], scope_starts: list[int], start: int) -> bool:
    """True if a reporting word comes earlier in the same reported clause as ``start``."""
    k = bisect_right(scope_starts, start) - 1
    i = bisect_left(reported, scope_starts[k] if k >= 0 else 0)
    return i < len(reported) and reported[i] < start


_JOB_ID_RE = re.compile(rf"(?<![\w-]){_JOB_ID}", re.IGNORECASE)
_HEX_ID_RE = re.compile(rf"(?<![\w-]){_HEX_ID}", re.IGNORECASE)
_QUOTED_ID = re.compile(rf"{_JOB_ID}|{_HEX_ID}", re.IGNORECASE)  # kept by _blank
# a hex id is read where a claim names its work item, after the noun: in "the cafe2024 job" it is a name
_NAMED_HEX_ID_RE = re.compile(rf"\b{_CANCELLABLE}\b(?!-)(?:\s+|\s*[(:#]\s*)`?(?P<id>{_HEX_ID})", re.IGNORECASE)


def _claimed_work_id(text: str) -> str | None:
    """The work item a cancel claim names (a job id, or a hex id after the noun), lowercased; None if none."""
    job = _JOB_ID_RE.search(text)
    if job:
        return job.group(0).lower()
    hex_id = _NAMED_HEX_ID_RE.search(text)
    return hex_id.group("id").lower() if hex_id else None


def _names_a_job(text: str) -> bool:
    """Whether a cancel claim names a runner job: a job id, or a hex id after "job" (its hex tail)."""
    hex_id = _NAMED_HEX_ID_RE.search(text)
    return bool(_JOB_ID_RE.search(text) or (hex_id and hex_id.group(0)[:3].lower() == "job"))


_UUID_FORM = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"


def _arg_hex(value: str) -> str | None:
    """The hex digits of an id a call was GIVEN (lowercase), or None if it is no hex id.

    A recorded argument is no prose, so the claim text's digit-and-letter rule (``_HEX_ID``) is not used here:
    any hex run of 6 or more is an id ("12345678", "abcdefab"), and so is a prefix of a hyphenated UUID and
    any spelling of a UUID that ``uuid.UUID`` reads, as dag_manage and cancel_task do."""
    try:
        return UUID(value).hex
    except ValueError:
        pass
    if re.fullmatch(r"[0-9a-f]{6,}", value):
        return value
    hyphens_fit = all(c == "-" if f == "-" else c in "0123456789abcdef" for c, f in zip(value, _UUID_FORM))
    if 8 < len(value) <= len(_UUID_FORM) and hyphens_fit:
        return value.replace("-", "")
    return None


def _work_level(claimed: str | None, values: list[str]) -> str:
    """Whether a call on ``values`` (the ids it was given) was on the work item a claim names.

    Only ids of one shape are compared -- a hex prefix with a UUID (or a job id's hex tail), a job id with
    a job id: a job id and a UUID neither confirm nor refute each other. Hex ids are compared without their
    hyphens, so a UUID matches however either side spells it."""
    if claimed is None:
        return "plausible"  # the claim names no work item: any cancel could be it
    job = bool(_JOB_ID_RE.fullmatch(claimed))
    comparable: list[str] = []
    for value in (v.lower() for v in values if v):
        if _JOB_ID_RE.fullmatch(value):
            comparable.append(value if job else value.rsplit("-", 1)[-1])
        elif not job and (digits := _arg_hex(value)) is not None:
            comparable.append(digits)
    if not comparable:
        return "plausible"
    if job:
        hit = claimed in comparable
    else:
        claimed = claimed.replace("-", "")
        hit = any(v.startswith(claimed) or claimed.startswith(v) for v in comparable)
    return "exact" if hit else "none"


def _runner_cancel_level(claim: Claim, ev: Evidence, claimed: str | None, runner: str | None) -> str:
    """What a bash call of the job runner tells about a cancel claim.

    `runner.sh cancel <id>` that exited 0 backs a cancel of that job (it exits 0 for a job already
    finished too, which it then marks cancelled). `runner.sh status|result <id>` is a read: its exit code
    is the job's state, not the read's, so it is ignored, and the job status the output stated
    (``observed_status``) backs only the passive "job <id> was cancelled", never "I cancelled job <id>".
    A cancel counts wherever it is in the command when it certainly ran and succeeded; a status read
    counts only when the command runs the runner alone (after a `cd`): a second command's output could
    be the status line. A row that kept no command backs nothing: it could be any command; an unreadable
    command cannot disprove anything."""
    runs = ev.runs
    if runner is None:
        return "none"
    if runs is None:
        return "plausible"
    levels = ["none"]
    dirs = _cd_dirs(ev.args.get("command") or ev.args.get("cmd") or "", runs)
    found = []  # per call: its program, whether it certainly ran, its runner arguments
    for (prog, args, certain), cd in zip(runs, dirs):
        word, cwd = cd or (prog, None)
        found.append((prog, certain, _runner_args(word, args, runner, cwd)))
    for _, certain, pos in found:
        if certain and pos is not None and len(pos) >= 2 and pos[0] == "cancel":
            levels.append(_work_level(claimed, [pos[1]]))
    calls = [pos for prog, _, pos in found if _base(prog) != "cd"]
    pos = calls[0] if len(calls) == 1 else None
    if pos is not None and len(pos) == 2 and pos[0] in ("status", "result") and ev.observed_status == "cancelled" \
            and re.search(r"cancel+ed$", claim.text, re.IGNORECASE):
        levels.append(_work_level(claimed, [pos[1]]))
    return _best(levels)


_TRACE_OPTIONS = re.compile(r"-[xveu]+")


def runner_path(workspace_dir: str) -> str:
    """The job runner of a workspace, by the path tool_policy lets a lineage run it."""
    return str(Path(workspace_dir) / "claude-jobs" / "runner.sh")


def _cd_dirs(command: str, runs: tuple) -> list[tuple[str, str] | None]:
    """Per call of ``runs``: its program word as written and the directory an absolute `cd <dir> &&` just
    before it certainly moved to; None for a call with no such cd.

    Read from the command text, not ``runs``: an invocation's certainty is unknown whenever the exit is not 0
    (a status read exits with the job's state), ``runs`` unwraps `bash -c 'cd x'` and `(cd x)`, whose cd
    moves a child shell only, and it keeps a relative program as `./<name>`. So the text's simple commands
    must be ``runs`` one to one (no branch dropped, no string unwrapped; an assignment alone runs nothing);
    the cd must be a simple command of its own, joined to the call by `&&` (blank or comment lines after it
    continue the command), and not after `||` or `|` (where it may be skipped or run in a subshell)."""
    found: list[tuple[str, str] | None] = [None] * len(runs)
    commands = simple_commands(command)
    if commands is None:
        return found
    kept: list[tuple[list[str], list[str], list[str], str]] = []
    for c in commands:  # blank and comment lines after `&&`, `||` or `|` continue the command
        if not (c == ([], [], [], "\n") and kept and kept[-1][3] in ("&&", "||", "|")):
            kept.append(c)
    commands = kept
    calls = [i for i, (words, _, _, _) in enumerate(commands)
             if words and not all(_ASSIGNMENT.match(w) for w in words)]
    if len(calls) != len(runs) or any(
            (program_name(commands[i][0][0]), *commands[i][0][1:]) != (prog, *args)
            for i, (prog, args, _) in zip(calls, runs)):
        return found
    for k, i in enumerate(calls):
        cd = commands[i - 1][0] if i >= 1 else []
        if len(cd) == 2 and cd[0] == "cd" and posixpath.isabs(cd[1]) and commands[i - 1][3] == "&&" \
                and (i < 2 or commands[i - 2][3] not in ("||", "|")):
            found[k] = (commands[i][0][0], cd[1])
    return found


def _runner_args(prog: str, args: tuple[str, ...], runner: str | None, cwd: str | None = None) -> list[str] | None:
    """The positional arguments of an invocation of ``runner`` (`bash [opts] <runner> ...` too); None if not one.

    ``runner`` is the workspace's `claude-jobs/runner.sh` by its absolute path, as tool_policy allows it: a
    relative `runner.sh` or another directory's could be any script. None: no call is a runner call. A
    relative script is resolved against ``cwd``, the absolute directory a `cd` certainly moved to
    (``_cd_dirs``): `./runner.sh`, `claude-jobs/runner.sh`, `bash runner.sh`; never a bare name run
    directly (PATH finds it, not the cwd) nor one through `..` (the kernel resolves it past a symlink).

    Only the options that change how a script runs, not what runs, may come before it (`bash -x runner.sh`):
    `-c` runs its string and `-s` its stdin, so the next word is not what ran; any other word there must be the
    runner's exact path (so a `-c` string, an option value or a word with spaces is none)."""
    if _base(prog) in ("bash", "sh"):
        i = 0
        while i < len(args) and _TRACE_OPTIONS.fullmatch(args[i]):
            i += 1
        if i < len(args) and args[i] == "--":  # the options end: the next word is the script
            i += 1
        if i == len(args):
            return None
        prog, args = args[i], args[i + 1:]
    elif "/" not in prog:
        cwd = None
    if cwd is not None and not posixpath.isabs(prog) and ".." not in prog.split("/"):
        prog = posixpath.normpath(posixpath.join(cwd, prog))
    return _positional(args) if runner is not None and prog == runner else None


def _cancel_level(claim: Claim, ev: Evidence, runner: str | None) -> str:
    """How well one recorded call backs a cancel claim: it must be a cancel, of the work item named."""
    claimed = _claimed_work_id(claim.text)
    if ev.tool_name == "bash":
        return _runner_cancel_level(claim, ev, claimed, runner)
    action = ev.args.get("action")
    if ev.tool_name != "cancel_task":
        if action is None:
            return "plausible"  # names only: a cancel cannot be told from a status read
        if action != "cancel":
            return "none"
    # a DAG cancel cancels the DAG (dag_id); a node_name it was given is not what it cancelled
    keys = ("task_id", "id") if ev.tool_name == "cancel_task" else ("dag_id",)
    return _work_level(claimed, [ev.args.get(k, "") for k in keys])


# Kinds a "plausible" level still backs in a report: a write to a computed path and a deploy have no
# exact form, a test claim's exit code is already checked, a verdict says only that something was tried,
# and a cancel that names no work item is backed by any cancel. Every other completion claim (sent,
# merged, pushed, committed, opened a PR, cancelled <id>) needs a call that shows it happened.
_PLAUSIBLE_BACKS = frozenset({"file_write", "deploy", "test_run"})


def _backed_by_plausible(claim: Claim) -> bool:
    return (claim.kind in _PLAUSIBLE_BACKS or claim.attempt_only
            or (claim.kind == "cancel" and _claimed_work_id(claim.text) is None))


UNVERIFIED_MARKER = "[Unverified claims]"
_UNVERIFIED_SHOWN = 5


def report_level(claim: Claim, ev: Evidence, runner: str | None = None) -> str:
    """How well one recorded call supports one report claim: exact, plausible or none.

    ``runner`` is the job runner's path (``runner_path``); without it no bash call backs a cancel claim.

    A failed or blocked call backs only a verdict on the action (``attempt_only``):
    "the cancel did not take effect" says a cancel was issued, not that it worked.
    """
    if not ev.ok and not claim.attempt_only:
        return "none"
    if claim.kind in CLAIM_KINDS:
        if not ev.ok:  # a failed send or write backs no claim that it happened
            return "none"
        return evidence_level(claim, ev)
    kind = REPORT_KINDS[claim.kind]
    if ev.tool_name not in kind.capable_tools:
        return "none"
    if claim.kind == "cancel":
        return _cancel_level(claim, ev, runner)
    outcome = _test_outcome(claim)
    if outcome is not None and (ev.exit_code is None or (ev.exit_code == 0) != (outcome == "pass")):
        return "none"  # a pass needs exit 0 and a failure a non-zero exit; an unknown exit backs neither
    code = ev.args.get("command") or ev.args.get("cmd") or ev.args.get("code") or ""
    if not code:  # names only: the durable ledger keeps a hash of a command
        return "plausible"
    if (claim.kind != "test_run" and not claim.attempt_only
            and ev.exit_code is not None and ev.exit_code != 0):
        return "none"  # a test run that fails still ran the tests
    return _report_run_level(claim, ev)


def ledger_evidence(actions: Any) -> list[Evidence]:
    """Every call of an in-memory ledger as evidence, the failed ones marked."""
    return [
        Evidence(a.tool_name, a.evidence_args, a.exit_code, a.side_effect_type,
                 a.invocations if a.tool_name == "bash" else _UNREAD, ok=a.status == "success",
                 observed_status=a.observed_status)
        for a in actions
    ]


def unverified_note(violations: list[ClaimViolation]) -> str:
    """The line a report or arrival note carries for its unbacked claims.

    The claims are quoted, so reading the note again extracts none of them. A claim some call could
    have done (``evidence == "plausible"``) is worded apart: it is not confirmed, not refuted."""
    shown = violations[:_UNVERIFIED_SHOWN]

    def quoted(group: list[ClaimViolation]) -> str:
        return "; ".join('"' + " ".join(v.claimed_text.replace('"', "'").split())[:120] + '"' for v in group)

    unbacked = [v for v in shown if v.evidence != "plausible"]
    unconfirmed = [v for v in shown if v.evidence == "plausible"]
    parts: list[str] = []
    if unbacked:
        parts.append(f"No recorded tool call of this work backs: {quoted(unbacked)}")
    if unconfirmed:
        parts.append(f"Could not confirm from the recorded calls: {quoted(unconfirmed)}")
    more = f" (and {len(violations) - _UNVERIFIED_SHOWN} more)" if len(violations) > _UNVERIFIED_SHOWN else ""
    return f"{UNVERIFIED_MARKER} {'. '.join(parts)}{more}. Treat these as not observed."


class ClaimVerifier:
    """Verifies that action claims in assistant responses are grounded in actual tool use."""

    def __init__(self, *, runner_path: str | None = None) -> None:
        self._runner_path = runner_path  # the job runner a report's cancel claim may be backed by; None: none
        self._compiled: list[tuple[re.Pattern[str], str]] = [
            (re.compile(pattern, re.IGNORECASE), kind) for pattern, kind in _CLAIM_PATTERNS
        ]
        self._report: list[tuple[re.Pattern[str], str, bool]] = [
            (re.compile(pattern, re.IGNORECASE), kind, attempt) for pattern, kind, attempt in _REPORT_PATTERNS
        ]

    def verify(
        self,
        assistant_response: str,
        tool_calls_this_turn: list[str],
        ledger: ExecutionLedger | None,
        *,
        turn_evidence: list[Evidence] | None = None,
    ) -> VerificationResult:
        """Check every action claim against this turn's calls and the ledger.

        A claim is grounded when any successful call of a tool capable of the
        claimed effect carries the effect's signal in its arguments (see
        ``evidence_level``); a named path or recipient must match.

        Args:
            assistant_response: Full text of the assistant's reply.
            tool_calls_this_turn: Tool names dispatched in the current turn
                (names-only evidence when ``turn_evidence`` is not given).
            ledger: Session execution ledger for historical lookup.
            turn_evidence: This turn's successful calls with their full arguments.

        Returns:
            VerificationResult with verified=True when no violations are found.
        """
        claims = self._extract_claims(assistant_response)
        if not claims:
            return VerificationResult(verified=True)

        pool = list(turn_evidence) if turn_evidence is not None else [
            Evidence(name) for name in tool_calls_this_turn]
        if ledger is not None:
            # Audit CL-4 (2026-06-09): only SUCCESSFUL actions count -- a blocked
            # or failed call must not let "I pushed the code" verify.
            recent = ledger.actions[-10:]
            current = [a for a in ledger.actions if a.turn == ledger.current_turn]
            if turn_evidence is not None:
                # this turn's calls are in turn_evidence, untruncated; the
                # ledger's bounded copies of them must not outvote it (a copy
                # past the invocation cap reads as unreadable -> plausible)
                current = []
                recent = [a for a in recent if a.turn != ledger.current_turn]
            for action in {id(a): a for a in [*current, *recent]}.values():
                if action.status == "success":
                    pool.append(Evidence(
                        action.tool_name, action.evidence_args, action.exit_code,
                        action.side_effect_type,
                        # read from the whole command at record time, not re-read
                        # from the ledger's bounded copy
                        action.invocations if action.tool_name == "bash" else _UNREAD,
                    ))
        turn_names = set(tool_calls_this_turn) | {ev.tool_name for ev in turn_evidence or ()}

        checks: list[ClaimCheck] = []
        violations: list[ClaimViolation] = []
        for claim in claims:
            levels = {evidence_level(claim, ev) for ev in pool}
            level = "exact" if "exact" in levels else "plausible" if "plausible" in levels else "none"
            checks.append(ClaimCheck(claim.kind, claim.text, level))
            if level == "none":
                kind = CLAIM_KINDS[claim.kind]
                violations.append(ClaimViolation(
                    claimed_text=claim.text,
                    expected_tool=kind.primary_tool,
                    found_in_turn=kind.primary_tool in turn_names,
                    found_in_ledger=False,
                    capable_tools=tuple(sorted(kind.capable_tools)),
                ))

        if not violations:
            return VerificationResult(verified=True, claims=checks)

        return VerificationResult(
            verified=False,
            violations=violations,
            correction=self._build_correction(violations),
            claims=checks,
        )

    def verify_report(
        self, text: str, evidence: list[Evidence], *, cancelled: bool = False,
    ) -> VerificationResult:
        """Check a final report's action claims against the calls its work recorded.

        For a subtask's final report and a continuation turn's note: the
        first-person claims ``verify`` reads, plus the verdicts and passive
        forms a report uses (``_REPORT_PATTERNS``). ``evidence`` is every
        recorded call of the work, failed ones marked ``ok=False``;
        ``cancelled`` is true when the work was cancelled outside any tool call
        (the owner's cancel), which backs a claim about a cancel that names no
        runner job id.
        A completion claim needs ``exact`` evidence; a ``plausible`` one is a
        violation marked ``evidence="plausible"`` (not confirmed), except for
        the kinds ``_backed_by_plausible`` lets through.
        Deterministic and model-free; it never blocks anything.
        """
        first = self._extract_claims(text)
        claims = first + self._extract_report_claims(text, first)
        if not claims:
            return VerificationResult(verified=True)
        checks: list[ClaimCheck] = []
        violations: list[ClaimViolation] = []
        for claim in claims:
            # the owner cancels the root's own work, never a runner job
            if claim.kind == "cancel" and cancelled and not _names_a_job(claim.text):
                level = "exact"
            else:
                level = _best(["none", *(report_level(claim, ev, self._runner_path) for ev in evidence)])
            checks.append(ClaimCheck(claim.kind, claim.text, level))
            if level == "none" or (level == "plausible" and not _backed_by_plausible(claim)):
                kind = CLAIM_KINDS.get(claim.kind) or REPORT_KINDS[claim.kind]
                violations.append(ClaimViolation(
                    claimed_text=claim.text,
                    expected_tool=kind.primary_tool,
                    found_in_turn=False,
                    found_in_ledger=False,
                    capable_tools=tuple(sorted(kind.capable_tools)),
                    evidence=level,
                ))
        if not violations:
            return VerificationResult(verified=True, claims=checks)
        return VerificationResult(verified=False, violations=violations,
                                  correction=unverified_note(violations), claims=checks)

    def _extract_report_claims(self, text: str, first: list[Claim]) -> list[Claim]:
        """The report-only claims in ``text`` (see ``_REPORT_PATTERNS``), in document order.

        Quoted text, questions, conditions and hedged clauses are not claims;
        a span one of ``first`` (``_extract_claims``'s) took for the same kind
        is not taken twice.
        """
        text = _QUOTED.sub(_blank, text).replace("**", "")
        clause_starts = [m.end() for m in _CLAUSE_END.finditer(text)]
        clause_ends = [m.start() for m in _CLAUSE_END.finditer(text)]
        sentence_marks = list(_SENTENCE_END.finditer(text))
        sentence_ends = [m.start() for m in sentence_marks]
        sentence_starts = [m.end() for m in sentence_marks]
        reported = [m.start() for m in _REPORTED.finditer(text)]
        scope_starts = [m.end() for m in _REPORTED_SCOPE_END.finditer(text)]
        taken = {(c.kind, c.span) for c in first}
        found: list[Claim] = []
        for pattern, kind, attempt in self._report:
            for match in pattern.finditer(text):
                start, end = match.span()
                k = bisect_left(clause_ends, end)
                if k < len(clause_ends) and text[clause_ends[k]] == "?":
                    continue
                c = bisect_right(clause_starts, start) - 1
                prefix = text[clause_starts[c] if c >= 0 else 0:start]
                if _CONDITION.search(prefix) or _REPORT_HEDGE.search(prefix):
                    continue
                if _reported_before(reported, scope_starts, start):
                    continue
                if any(k2 == kind and s < end and start < e for k2, (s, e) in taken):
                    continue
                taken.add((kind, (start, end)))
                target = match.groupdict().get("target")
                clause_start = clause_starts[c] if c >= 0 else 0
                clause_end = clause_ends[k] if k < len(clause_ends) else len(text)
                found.append(Claim(kind=kind, text=match.group(0).strip(), span=(start, end),
                                   target=target.lower() if target else None, attempt_only=attempt,
                                   clause=text[clause_start:clause_end],
                                   sentence=text[clause_start:_sentence_end(sentence_ends, clause_end, text)],
                                   opening=text[_sentence_start(sentence_starts, clause_start):clause_start]))
        found.sort(key=lambda claim: claim.span[0])
        return found

    def _extract_claims(self, text: str) -> list[Claim]:
        """Every first-person completion claim in ``text``, in document order.

        Quoted text, code blocks and questions are not claims. Linear: every
        boundary is computed once and looked up by bisection.
        """
        text = _QUOTED.sub(_blank, text).replace("**", "")  # bold is still the agent's words
        ends = [m for m in _CLAUSE_END.finditer(text)]
        clause_ends = [m.start() for m in ends]
        clause_starts = [m.end() for m in ends]
        sentence_marks = list(_SENTENCE_END.finditer(text))
        sentence_ends = [m.start() for m in sentence_marks]
        sentence_starts = [m.end() for m in sentence_marks]
        first_claims = [m.start() for m in _FIRST_CLAIM_RE.finditer(text)]
        conditions = [m.start() for m in _CONDITION.finditer(text)]
        reported = [m.start() for m in _REPORTED.finditer(text)]
        scope_starts = [m.end() for m in _REPORTED_SCOPE_END.finditer(text)]
        spans: dict[str, list[tuple[int, int]]] = {}
        found: list[tuple[int, Claim]] = []
        for pattern, kind in self._compiled:
            for match in pattern.finditer(text):
                start, end = match.span()
                k = bisect_left(clause_ends, end)
                if k < len(clause_ends) and text[clause_ends[k]] == "?":
                    continue  # "Was the email sent to x?" asks; it does not claim
                clause_end = clause_ends[k] if k < len(clause_ends) else len(text)
                c = bisect_right(clause_starts, start) - 1
                clause = clause_starts[c] if c >= 0 else 0
                i = bisect_left(conditions, clause)
                if i < len(conditions) and conditions[i] < start:
                    continue  # "if I pushed", "check whether it was saved": a condition
                if _reported_before(reported, scope_starts, start):
                    continue  # "the subtask said I sent it": reported, not claimed
                subj = match.groupdict().get("subj")
                if subj is not None and not _FIRST_RE.match(subj):
                    # an "and <verb>" clause: a claim only after a first-person
                    # CLAIM earlier in the same clause
                    i = bisect_left(first_claims, clause)
                    if not (i < len(first_claims) and first_claims[i] < start):
                        continue
                # same claim already captured? (the targeted pattern runs first)
                taken = spans.setdefault(kind, [])
                j = bisect_left(taken, (start, end))
                if (j > 0 and taken[j - 1][1] > start) or (j < len(taken) and taken[j][0] < end):
                    continue
                taken.insert(j, (start, end))
                target = match.groupdict().get("target")
                if target:
                    target = target.rstrip(".,;:)")
                    if "@" in target:
                        target = target.lower()
                found.append((start, Claim(kind=kind, text=match.group(0).strip(), target=target,
                                           span=(start, end), clause=text[clause:clause_end],
                                           sentence=text[clause:_sentence_end(sentence_ends, clause_end, text)],
                                           opening=text[_sentence_start(sentence_starts, clause):clause])))
        found.sort(key=lambda item: item[0])
        return [claim for _, claim in found]

    def _build_correction(self, violations: list[ClaimViolation]) -> str:
        """Build a correction message describing all ungrounded claims."""
        lines = [
            "[Execution Integrity] The previous response contained ungrounded action claims:"
        ]
        for v in violations:
            capable = f"; any of: {', '.join(v.capable_tools)}" if v.capable_tools else ""
            lines.append(
                f'  - Claimed: "{v.claimed_text}" '
                f"(expected tool: {v.expected_tool}{capable}) — "
                "no successful call that could have done this was recorded."
            )
        lines.append(
            "Do not assert that an action was taken unless the corresponding tool "
            "was actually called and succeeded."
        )
        return "\n".join(lines)


class IntentTracker:
    """Detects ghost planning: describing or presenting work without executing it."""

    # Regex patterns that signal the assistant is narrating work rather than doing it.
    WORK_PRODUCT_SIGNALS: list[str] = [
        r"```[\w]*\n.{200,}```",
        r"(?:here'?s|below is) (?:the|a|my) (?:draft|plan|outline|report|email|message)",
        r"(?:I'?ll|let me|going to) (?:write|create|save|send|push)",
        r"[Ss]aved? to[:\s]+[/\w.-]+",
    ]

    def __init__(self) -> None:
        self._compiled: list[re.Pattern[str]] = [
            re.compile(pattern, re.IGNORECASE | re.DOTALL)
            for pattern in self.WORK_PRODUCT_SIGNALS
        ]

    def check_ghost_planning(
        self,
        response: str,
        tool_calls_this_turn: list[str],
        ledger: ExecutionLedger,  # noqa: ARG002 — reserved for future heuristics
    ) -> bool:
        """Return True if the response looks like ghost planning.

        Ghost planning is suppressed when real tool calls occurred this turn.
        Requires >= 2 signal matches to reduce false positives on explanations.

        Args:
            response: Full assistant response text.
            tool_calls_this_turn: Tool names used this turn.
            ledger: Session ledger (reserved for future density heuristics).

        Returns:
            True if ghost planning is detected, False otherwise.
        """
        if tool_calls_this_turn:
            return False

        signal_count = sum(
            1 for pattern in self._compiled if pattern.search(response)
        )
        return signal_count >= 2

    def build_nudge(self) -> str:
        """Return the correction message injected when ghost planning is detected."""
        return (
            "[Execution Integrity] The previous response described or presented work "
            "without calling any tools. If an action needs to be taken (write a file, "
            "run a command, search the web, etc.), use the appropriate tool rather than "
            "narrating the output. Only describe results after the tool has been called."
        )
