"""claim_verifier.py — F026 Execution Integrity Phase D.

ClaimVerifier: detects ungrounded action claims in assistant responses.
IntentTracker: detects ghost planning (describing work without doing it).
"""
from __future__ import annotations

import ast
import re
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

from nous.cognitive.bash_side_effect import (
    READ_COMMANDS,
    command_invocations,
    command_string,
    git_subcommand,
    program_name,
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
# "by 9am" and "by me" are not. Dots inside words pass, or the target would
# shrink to `/srv/q3` to keep ".md by" out of reach.
_NOT_BY = (r"(?!(?:[^.\n;]|\.(?=\S)){0,60}?\bby\s+(?!me\b|myself\b|\d|noon\b|midnight\b|tonight\b"
           r"|tomorrow\b|end\b|eod\b|eob\b|then\b|now\b|the\s+end\b)[\w$])")
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
# A condition earlier in the clause: "if I pushed", "check whether it was saved".
_CONDITION = re.compile(r"\b(?:if|whether|unless)\b", re.IGNORECASE)
# Text that is not the agent speaking: fenced code, block quotes, quotations.
_QUOTED = re.compile(r"```.*?(?:```|\Z)|^[ \t]*>[^\n]*|\"[^\"\n]{0,300}\"|“[^”\n]{0,300}”",
                     re.DOTALL | re.MULTILINE)


def _blank(match: re.Match[str]) -> str:
    """Same length, newlines kept: offsets and sentence ends stay put."""
    return re.sub(r"[^\n]", " ", match.group(0))

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
    "to_csv", "to_json", "to_excel", "to_parquet", "to_html", "to_markdown", "savefig", "save",
    "write_html", "write_image", "write_text", "write_bytes",
})
_PY_SHELL_CALLS = frozenset({
    "subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
    "subprocess.Popen", "os.system", "os.popen", "run", "call", "check_call", "check_output", "Popen",
})
_PY_DEPLOY_MODULES = frozenset({"docker", "kubernetes", "boto3", "paramiko", "fabric", "ansible"})


@dataclass
class _PyFacts:
    writes: list[str] = field(default_factory=list)   # literal destinations written
    write_unknown: bool = False                       # a write to a computed destination
    sends: bool = False
    recipients: set[str] = field(default_factory=set)
    recipient_unknown: bool = False                   # a recipient held in a variable
    # what it runs through a shell / subprocess, read like bash (argv kept)
    invocations: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    run_unknown: bool = False                         # a subprocess whose argv is not written out
    deploys: bool = False


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


def _executed(tree: ast.Module) -> list[ast.AST]:
    """The nodes a script RUNS: module-level statements (a class body's own
    statements included), the bodies of the functions and methods they call
    (transitively), and the taken side of a constant `if`/`while`. A call
    resolves to ITS definition: a bare `save()` to the module's `save`, an
    `Obj().save()` to Obj's method, an `x.save()` whose receiver is unknown
    to every class's `save` -- never to the module function of that name.
    A function merely defined, a lambda, a method never called, `if
    False:` -- never."""
    functions: dict[str, list[ast.AST]] = {}
    methods: dict[str, dict[str, ast.AST]] = {}  # method name -> class name -> def
    classes: set[str] = set()
    for node in tree.body:
        if isinstance(node, _FUNCTIONS):
            functions.setdefault(node.name, []).append(node)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.add(node.name)
            for item in node.body:
                if isinstance(item, _FUNCTIONS):
                    methods.setdefault(item.name, {})[node.name] = item
    executed: list[ast.AST] = []
    called: set[tuple[str, str]] = set()
    queue: list[ast.AST] = list(tree.body)
    while queue:
        for sub in _walk_live(queue.pop()):
            executed.append(sub)
            if not isinstance(sub, ast.Call):
                continue
            targets: list[tuple[str, ast.AST]] = []
            if isinstance(sub.func, ast.Name):
                targets = [(f"{sub.func.id}", fn) for fn in functions.get(sub.func.id, [])]
            elif isinstance(sub.func, ast.Attribute):
                by_class = methods.get(sub.func.attr, {})
                receiver = sub.func.value
                if isinstance(receiver, ast.Call):
                    receiver = receiver.func  # Obj().save() -> Obj
                owner = receiver.id if isinstance(receiver, ast.Name) else None
                if owner in by_class:
                    targets = [(f"{owner}.{sub.func.attr}", by_class[owner])]
                elif owner not in classes:  # self, or an instance whose class is unknown
                    targets = [(f"{c}.{sub.func.attr}", fn) for c, fn in by_class.items()]
            for key, fn in targets:
                if key not in called:
                    called.add(key)
                    queue.extend(fn.body)  # type: ignore[attr-defined]
    return executed


def _walk_live(node: ast.AST):
    """`ast.walk` along the paths that run: never into a nested definition,
    only the taken side of a constant `if`/`while`, never an `except`
    handler (it may never run), a class body's own statements."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, (*_FUNCTIONS, ast.Lambda)):
            continue
        yield current
        if isinstance(current, ast.ClassDef):
            stack.extend(s for s in current.body if not isinstance(s, _FUNCTIONS))
        elif isinstance(current, (ast.If, ast.While)):
            truth = _constant_truth(current.test)
            stack.append(current.test)
            if truth is not False:
                stack.extend(current.body)
            if truth is not True:
                stack.extend(current.orelse)
        elif isinstance(current, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            stack.extend(current.body + current.orelse + current.finalbody)
        else:
            stack.extend(ast.iter_child_nodes(current))


def _python_facts(code: str) -> _PyFacts | None:
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return None
    facts = _PyFacts()
    for node in _executed(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) else [])
            if any(n and n.split(".")[0] == "smtplib" for n in names):
                facts.sends = True
            if any(n and n.split(".")[0] in _PY_DEPLOY_MODULES for n in names):
                facts.deploys = True
        elif isinstance(node, ast.Assign):
            for target in node.targets:  # msg['To'] = '...'
                if isinstance(target, ast.Subscript) and _literal(target.slice) in ("To", "Cc", "Bcc"):
                    found = _literals(node.value)
                    if found is None:
                        facts.recipient_unknown = True
                    else:
                        facts.recipients |= {a for s in found for a in _addresses(s)}
        elif isinstance(node, ast.Call):
            _call_facts(node, facts)
    if facts.sends and not facts.recipients:
        facts.recipient_unknown = True  # it sends, to whom is not written out
    return facts


def _call_facts(node: ast.Call, facts: _PyFacts) -> None:
    name = _dotted(node.func)
    method = name.rsplit(".", 1)[-1]
    args = node.args
    kws = {k.arg: k.value for k in node.keywords if k.arg}
    if name == "open":
        mode = _literal(args[1]) if len(args) > 1 else _literal(kws.get("mode"))
        if mode and mode[:1] in "wax":
            _note_write(facts, args[0] if args else kws.get("file"))
    elif method in ("write_text", "write_bytes") and isinstance(node.func, ast.Attribute):
        receiver = node.func.value  # Path('...').write_text(...)
        inner = receiver.args[0] if isinstance(receiver, ast.Call) and receiver.args else None
        _note_write(facts, inner if _dotted(receiver.func if isinstance(receiver, ast.Call) else receiver)
                    .endswith("Path") else None)
    elif method in _PY_WRITE_METHODS and (args or "path" in kws or "fname" in kws):
        # a file-writing method takes its destination; `obj.save()` bare is
        # whatever a user-defined `save` does (resolved through _executed)
        _note_write(facts, args[0] if args else kws.get("path") or kws.get("fname"))
    elif name in ("json.dump", "pickle.dump", "yaml.dump", "yaml.safe_dump"):
        sink = args[1] if len(args) > 1 else kws.get("fp") or kws.get("stream")
        if isinstance(sink, ast.Call):  # json.dump(data, open('/tmp/x', 'w'))
            _call_facts(sink, facts)
        else:
            facts.write_unknown = True
    elif name in ("shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.move"):
        _note_write(facts, args[1] if len(args) > 1 else kws.get("dst"))
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
    elif name in _PY_SHELL_CALLS or method in ("system", "popen"):
        argv = args[0] if args else kws.get("args")
        if isinstance(argv, (ast.List, ast.Tuple)):  # argv: the executable and ITS arguments
            items = _literals(argv)
            if items:
                facts.invocations.append((program_name(items[0]), tuple(items[1:])))
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
        url = _literal(args[0]) if args else _literal(kws.get("url"))
        if url and "api.telegram.org" in url and method in ("post", "get", "request", "urlopen"):
            facts.sends = True
            facts.recipient_unknown = True


def _note_write(facts: _PyFacts, destination: ast.AST | None) -> None:
    if (path := _literal(destination)) is not None:
        facts.writes.append(path)
    else:
        facts.write_unknown = True


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
    target = _HOME.sub("~/", target.replace("\\", "/")).rstrip("/")
    path = _HOME.sub("~/", path.replace("\\", "/")).rstrip("/")
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
        dests.append(last.split(":", 1)[1] if ":" in last and not last.startswith("/") else last)
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
        elif base in _DESTROYERS or base in _WRITERS_LAST or base in _WRITERS_ALL or dests \
                or base in READ_COMMANDS or base in ("tar", "dd"):
            continue  # a delete, a known writer writing elsewhere, or a read naming the path
        elif _names(target, " ".join(args)):
            best = _best([best, "plausible"])
    return best


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
    if claim.kind == "file_write":
        if not facts.writes and not facts.write_unknown:
            return "none"
        if claim.target:
            if any(_same_path(claim.target, w) for w in facts.writes):
                return "exact"
            return "plausible" if facts.write_unknown else "none"  # a computed path; or elsewhere
        return "plausible"
    if claim.kind == "email":
        if not facts.sends:
            return "none"
        if claim.target and claim.target not in facts.recipients:
            return "plausible" if facts.recipient_unknown else "none"  # held in a variable; or someone else
        return "plausible"
    if any(_does(claim.kind, prog, args) for prog, args in facts.invocations):
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


def evidence_level(claim: Claim, ev: Evidence) -> str:
    """How well one successful call supports one claim: exact, plausible or none."""
    kind = CLAIM_KINDS[claim.kind]
    if ev.tool_name not in kind.capable_tools:
        return "none"
    if not ev.args:  # names-only evidence from a legacy caller
        return "plausible"
    if ev.tool_name in _PRODUCERS:
        return "none" if claim.target else "plausible"
    if ev.tool_name == "bash":
        return _bash_level(claim, ev)
    if ev.tool_name == "write_file":  # an explicit destination: it must BE the target
        path = ev.args.get("path") or ev.args.get("file_path") or ""
        return "exact" if not claim.target or _same_path(claim.target, path) else "none"
    if ev.tool_name == "send_email":  # whole addresses: malice@x.io is not alice@x.io
        recipients = _addresses(f"{ev.args.get('to', '')} {ev.args.get('cc', '')}")
        return "exact" if not claim.target or claim.target in recipients else "none"
    if ev.tool_name == "send_file":  # a Telegram send cannot have reached a named address
        return "none" if claim.target else "plausible"
    return _code_level(claim, ev.args.get("code", ""))


class ClaimVerifier:
    """Verifies that action claims in assistant responses are grounded in actual tool use."""

    def __init__(self) -> None:
        self._compiled: list[tuple[re.Pattern[str], str]] = [
            (re.compile(pattern, re.IGNORECASE), kind) for pattern, kind in _CLAIM_PATTERNS
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

    def _extract_claims(self, text: str) -> list[Claim]:
        """Every first-person completion claim in ``text``, in document order.

        Quoted text, code blocks and questions are not claims. Linear: every
        boundary is computed once and looked up by bisection.
        """
        text = _QUOTED.sub(_blank, text).replace("**", "")  # bold is still the agent's words
        ends = [m for m in _CLAUSE_END.finditer(text)]
        clause_ends = [m.start() for m in ends]
        clause_starts = [m.end() for m in ends]
        first_claims = [m.start() for m in _FIRST_CLAIM_RE.finditer(text)]
        conditions = [m.start() for m in _CONDITION.finditer(text)]
        spans: dict[str, list[tuple[int, int]]] = {}
        found: list[tuple[int, Claim]] = []
        for pattern, kind in self._compiled:
            for match in pattern.finditer(text):
                start, end = match.span()
                k = bisect_left(clause_ends, end)
                if k < len(clause_ends) and text[clause_ends[k]] == "?":
                    continue  # "Was the email sent to x?" asks; it does not claim
                c = bisect_right(clause_starts, start) - 1
                clause = clause_starts[c] if c >= 0 else 0
                i = bisect_left(conditions, clause)
                if i < len(conditions) and conditions[i] < start:
                    continue  # "if I pushed", "check whether it was saved": a condition
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
                found.append((start, Claim(kind=kind, text=match.group(0).strip(), target=target)))
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
