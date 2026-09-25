"""claim_verifier.py — F026 Execution Integrity Phase D.

ClaimVerifier: detects ungrounded action claims in assistant responses.
IntentTracker: detects ghost planning (describing work without doing it).
"""
from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

from nous.cognitive.bash_side_effect import command_string, git_subcommand
from nous.cognitive.execution_ledger import Invocation, bash_invocations

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
# A path, or a bare file name whose extension starts with a letter (`v1.2`
# and `3.30pm` are not files).
_PATH = r"(?P<target>(?:~|\.{0,2})/[\w./-]*\w|[\w-]+\.[A-Za-z]\w{0,5})"
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
_PY_WRITES = re.compile(
    r"open\([^\n]{0,300}?['\"][wax]b?\+?['\"]|\.write_(?:text|bytes|html|image)\("
    r"|\.to_(?:csv|json|excel|parquet|html|markdown)\(|savefig\(|json\.dump\(|pickle\.dump\("
    r"|yaml\.(?:safe_)?dump\(|\.save\(|shutil\.(?:copy\w*|move)\(")
_PY_SENDS = re.compile(r"\bsmtplib\b|\bsendmail\b|api\.telegram\.org")
_PY_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")  # an address written out in code
_PY_GIT = {
    "vcs_push": re.compile(
        r"\bgit\b[^|;&]{0,200}?\bpush\b(?![^|;&]{0,200}?(?:--dry-run|[\s'\"]-n\b))"),
    "vcs_commit": re.compile(r"\bgit\b[^|;&]{0,200}?\bcommit\b(?![^|;&]{0,200}?--dry-run)"),
}
_PY_DEPLOY = re.compile(
    r"\b(?:deploy\w*|docker|kubectl|helm|systemctl|terraform|ansible|rsync|scp|ssh|gcloud|aws|az)\b",
    re.I)

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


def _names(target: str, text: str) -> bool:
    """True if ``text`` names the claimed target (full path or basename)."""
    return bool(target and text) and (target in text or target.rsplit("/", 1)[-1] in text)


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
    return [a for a in args if not a.startswith("-") and not a.startswith("\n")]


def _base(prog: str) -> str:
    """`/usr/bin/rsync` is reported as `./rsync`; the allowlists know `rsync`."""
    return prog[2:] if prog.startswith("./") else prog


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
    if prog in _TASK_RUNNERS:
        return any(p in ("deploy", "release", "publish", "ship", "start", "serve") or "deploy" in p
                   for p in positional)
    if prog in _DEPLOY_CLIS:
        return ((prog == "vercel" and not positional) or any(p in _DEPLOY_CHANGE for p in positional)
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
    return bool(_SCRIPT.search(prog)) or prog.startswith("./")


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
    if claim.kind == "file_write":
        if not _PY_WRITES.search(code):
            return "none"
        if claim.target:
            return "exact" if _names(claim.target, code) else "none"
        return "plausible"
    if claim.kind == "email":
        if not _PY_SENDS.search(code):
            return "none"
        if claim.target and claim.target not in code.lower():
            # a recipient held in a variable may be the named one; one written
            # out as someone else is not
            return "none" if _PY_ADDRESS.search(code) else "plausible"
        return "plausible"
    if claim.kind in ("vcs_push", "vcs_commit"):
        return "plausible" if _PY_GIT[claim.kind].search(code) else "none"
    return "plausible" if _PY_DEPLOY.search(code) else "none"


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
    command = ev.args.get("command") or ev.args.get("cmd") or ""
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
        elif claim.target and not _names(claim.target, command):
            levels.append("plausible" if opaque else "none")
        elif claim.target and not failed:
            levels.append("exact")
        else:
            levels.append("plausible")
        return _best(levels)
    hits = [i for i, (prog, args, _) in enumerate(runs) if _does(claim.kind, prog, args)]
    if not hits:
        levels.append("plausible" if opaque else "none")
    elif failed and hits == [len(runs) - 1]:
        levels.append("none")
    elif claim.kind == "email" and claim.target and claim.target not in command.lower():
        # a recipient held in a variable may be the named one
        variable = any(a.startswith("$") for i in hits for a in runs[i][1])
        levels.append("plausible" if opaque or variable else "none")
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
    if ev.tool_name == "write_file":
        path = ev.args.get("path") or ev.args.get("file_path") or ""
        return "exact" if not claim.target or _names(claim.target, path) else "none"
    if ev.tool_name == "send_email":
        recipients = f"{ev.args.get('to', '')} {ev.args.get('cc', '')}".lower()
        return "exact" if not claim.target or claim.target in recipients else "none"
    if ev.tool_name == "send_file":
        return "plausible"
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
