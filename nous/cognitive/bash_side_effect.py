"""Whole-command side-effect classification for the ``bash`` tool.

Harness Phase 1b persists every side-effecting tool call to
``nous_system.execution_ledger`` and skips pure reads, so this verdict decides
whether a bash call leaves a durable record at all. The two errors are not
symmetric: a false ``write`` costs one ledger row, a false ``none`` means a
command that changed something is never recorded. Every ambiguity therefore
resolves to ``write``.

The first-token classifier this replaces returned ``none`` for
``echo data > file``, ``find . -delete`` and ``cat file | curl ...``. The
whole command is classified instead: it is lexed with quoting respected,
split into simple commands on ``| ; & && || ( )`` and newlines, each simple
command is classified on its own, and the most severe verdict wins. Output
redirection to anything but a ``/dev/null``-style sink or a file descriptor
is a write; command substitution, which the lexer cannot see into, is a
write; and the read-only commands that have a writing or executing mode
(``find -delete``, ``sed -i``, ``sort -o``, awk programs that redirect or run
commands, ``git branch -D`` ...) are checked for it.
"""

from __future__ import annotations

import re
import shlex

# Commands that only read, unless a mode checked in _READ_COMMAND_RULES says
# otherwise.
READ_COMMANDS: frozenset[str] = frozenset(
    {
        "cat", "ls", "ll", "find", "grep", "rg", "awk", "sed", "head", "tail",
        "wc", "diff", "stat", "file", "echo", "printf", "which", "type",
        "pwd", "env", "printenv", "less", "more", "sort", "uniq", "cut",
        "tr", "basename", "dirname", "realpath", "readlink",
    }
)

_EXTERNAL_COMMANDS = frozenset({"curl", "wget", "http", "httpie"})
_SEVERITY = {"none": 0, "write": 1, "external": 2}

# Substitution the lexer returns as opaque word text: $(...), `...`, <(...), >(...).
_OPAQUE = re.compile(r"\$\(|`|<\(|>\(")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_OPERATOR_CHARS = frozenset("();<>|&\n")
_OPERATOR = re.compile(r"&>>|&>|>>|>&|>\||<>|<<<|<<|<&|\|\||\|&|&&|;;|[|;&()<>\n]")
_OUTPUT_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})
_INPUT_REDIRECTS = frozenset({"<", "<<", "<<<", "<&"})
_HARMLESS_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})


def classify_bash_command(command: str) -> str:
    """Classify a bash command as ``'none'`` | ``'write'`` | ``'external'``."""
    if not command or not command.strip():
        return "write"
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"  # newline is a command separator, not whitespace
    lexer.whitespace_split = True
    lexer.commenters = ""  # a mid-word '#' must not swallow a trailing '> file'
    try:
        tokens = list(lexer)
    except ValueError:  # unbalanced quote
        return "write"

    verdict = "write" if _OPAQUE.search(command) else "none"
    words: list[str] = []
    expect: str | None = None  # a redirect waiting for its target word
    for tok in tokens:
        if not tok or not set(tok) <= _OPERATOR_CHARS:
            if expect == "out" and tok not in _HARMLESS_SINKS:
                verdict = _worst(verdict, "write")
            elif expect == "dup" and not (tok.isdigit() or tok == "-" or tok in _HARMLESS_SINKS):
                verdict = _worst(verdict, "write")
            elif expect is None:
                words.append(tok)
            expect = None
            continue
        for op in _OPERATOR.findall(tok):
            if expect is not None:  # a redirect with no target
                return "write"
            if op in _OUTPUT_REDIRECTS:
                expect = "out"
            elif op == ">&":
                expect = "dup"
            elif op in _INPUT_REDIRECTS:
                expect = "in"
            else:  # a command separator
                verdict = _worst(verdict, _classify_simple(words))
                words = []
    if expect is not None:
        return "write"
    return _worst(verdict, _classify_simple(words))


def _worst(a: str, b: str) -> str:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _classify_simple(words: list[str]) -> str:
    """Classify one simple command (no operators, redirections removed)."""
    i = 0
    while i < len(words) and _ASSIGNMENT.match(words[i]):
        i += 1
    if i == len(words):
        return "none"  # an empty segment, or shell variable assignments only
    cmd, args = words[i], words[i + 1:]
    if cmd == "env":
        return _classify_env(args)
    if cmd == "git":
        return _classify_git(args)
    if cmd in _EXTERNAL_COMMANDS:
        return "external"
    if cmd not in READ_COMMANDS:
        return "write"
    rule = _READ_COMMAND_RULES.get(cmd)
    return rule(args) if rule else "none"


def _short_flags(word: str) -> str:
    """The letters of a short-option cluster (``-Ei`` -> ``Ei``), else ''."""
    if len(word) > 1 and word.startswith("-") and not word.startswith("--"):
        return word[1:]
    return ""


def _long_matches(word: str, options: tuple[str, ...]) -> bool:
    """True if ``word`` names one of ``options``, including getopt-style
    unambiguous abbreviations (``--in`` for ``--in-place``)."""
    name = word.split("=", 1)[0]
    return len(name) > 2 and name.startswith("--") and any(o.startswith(name) for o in options)


def _flag_rule(short: str, long: tuple[str, ...]):
    def rule(args: list[str]) -> str:
        for a in args:
            if set(_short_flags(a)) & set(short) or _long_matches(a, long):
                return "write"
        return "none"

    return rule


def _classify_env(args: list[str]) -> str:
    """``env`` prints the environment, or runs its argument."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-i", "-", "--ignore-environment", "-0", "--null"):
            i += 1
        elif a in ("-u", "--unset", "-C", "--chdir"):
            i += 2
        elif a.startswith(("-u", "--unset=", "-C", "--chdir=")):
            i += 1
        elif a.startswith("-"):
            return "write"  # -S and friends: the command is not a plain word list
        elif _ASSIGNMENT.match(a):
            i += 1
        else:
            return _classify_simple(args[i:])
    return "none"


def _classify_find(args: list[str]) -> str:
    verdict = "none"
    for j, a in enumerate(args):
        if a in ("-delete", "-fprint", "-fprint0", "-fprintf", "-fls"):
            return "write"
        if a in ("-exec", "-execdir", "-ok", "-okdir"):
            sub: list[str] = []
            for w in args[j + 1:]:
                if w in (";", "+"):
                    break
                sub.append(w)
            if not sub:
                return "write"
            verdict = _worst(verdict, _classify_simple(sub))
    return verdict


def _classify_sed(args: list[str]) -> str:
    scripts: list[str] = []
    positional: list[str] = []
    explicit = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            positional.extend(args[i + 1:])
            break
        if a.startswith("--"):
            if _long_matches(a, ("--in-place", "--file")):
                return "write"
            if _long_matches(a, ("--expression",)):
                explicit = True
                if "=" in a:
                    scripts.append(a.split("=", 1)[1])
                elif i + 1 < len(args):
                    i += 1
                    scripts.append(args[i])
                else:
                    return "write"
            elif _long_matches(a, ("--line-length",)) and "=" not in a:
                i += 1
            i += 1
            continue
        flags = _short_flags(a)
        if not flags:
            positional.append(a)
            i += 1
            continue
        for k, c in enumerate(flags):
            if c in "if":  # in-place edit; a script file cannot be vetted
                return "write"
            if c in "el":  # takes an argument: the rest of the cluster, or the next word
                rest = flags[k + 1:]
                if not rest:
                    i += 1
                    if i >= len(args):
                        return "write"
                    rest = args[i]
                if c == "e":
                    explicit = True
                    scripts.append(rest)
                break
        i += 1
    if not explicit and positional:
        scripts.append(positional[0])
    return "write" if any(_sed_script_writes(s) for s in scripts) else "none"


def _sed_script_writes(script: str) -> bool:
    """True if a sed script writes a file or runs a command: a ``w``/``W``/
    ``e`` command, or a ``w``/``e`` flag on ``s``. Unparseable is True."""
    s, i, n = script, 0, len(script)
    while i < n:
        c = s[i]
        if c in " \t\n;{}!,$~0123456789":
            i += 1
        elif c in "/\\":  # address regex: /re/ or \cREc
            if c == "\\":
                if i + 1 >= n:
                    return True
                i = _skip_to_delimiter(s, i + 2, s[i + 1])
            else:
                i = _skip_to_delimiter(s, i + 1, "/")
            if i < 0:
                return True
            while i < n and s[i] in "IM":
                i += 1
        elif c in "sy":
            if i + 1 >= n:
                return True
            delimiter = s[i + 1]
            i = _skip_to_delimiter(s, i + 2, delimiter)
            if i >= 0:
                i = _skip_to_delimiter(s, i, delimiter)
            if i < 0:
                return True
            start = i
            while c == "s" and i < n and s[i].isalnum():
                i += 1
            if set(s[start:i]) & {"w", "e"}:
                return True
        elif c in "wWe":
            return True
        elif c in "aicrR#":  # text, a file to read, or a comment: runs to end of line
            end = s.find("\n", i)
            i = n if end < 0 else end + 1
        elif c in "btT:":  # a label
            while i < n and s[i] not in ";\n":
                i += 1
        else:
            i += 1
    return False


def _skip_to_delimiter(s: str, i: int, delimiter: str) -> int:
    """Index just past the next unescaped ``delimiter`` at or after ``i``, else -1."""
    while i < len(s):
        if s[i] == "\\":
            i += 2
        elif s[i] == delimiter:
            return i + 1
        else:
            i += 1
    return -1


def _classify_awk(args: list[str]) -> str:
    """awk is a language: only a vetted program text reads."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-F", "-v"):
            i += 2
        elif a.startswith(("-F", "-v")):
            i += 1
        elif a == "--":
            i += 1
            break
        elif a.startswith("-"):
            return "write"  # -f progfile, gawk -i/-E/-o/-p/-d: not vettable here
        else:
            break
    if i >= len(args):
        return "write"
    return "write" if re.search(r"[>|]|\bsystem\s*\(", args[i]) else "none"


def _classify_uniq(args: list[str]) -> str:
    """``uniq INPUT OUTPUT`` writes OUTPUT."""
    positional = 0
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-f", "-s", "-w"):
            i += 2
            continue
        if a == "-" or not a.startswith("-"):
            positional += 1
        i += 1
    return "write" if positional >= 2 else "none"


_READ_COMMAND_RULES = {
    "find": _classify_find,
    "sed": _classify_sed,
    "awk": _classify_awk,
    "uniq": _classify_uniq,
    "sort": _flag_rule("o", ("--output", "--compress-program")),
    "rg": _flag_rule("", ("--pre",)),
    "file": _flag_rule("C", ("--compile",)),
    "less": _flag_rule("oO", ("--log-file", "--LOG-FILE")),
}


# ---- git ----

_GIT_READ_SUBCOMMANDS = frozenset({"log", "status", "diff", "show", "ls-files"})

_GIT_BRANCH_WRITES = ("dDmMcCuf", (
    "--delete", "--move", "--copy", "--force", "--set-upstream-to",
    "--unset-upstream", "--edit-description", "--track", "--create-reflog",
))
_GIT_BRANCH_LISTS = ("lar", (
    "--list", "--all", "--remotes", "--contains", "--no-contains", "--merged",
    "--no-merged", "--points-at", "--format", "--sort", "--show-current", "--column",
))
_GIT_TAG_WRITES = ("adsfumFe", (
    "--annotate", "--sign", "--local-user", "--force", "--delete", "--message",
    "--file", "--edit", "--cleanup", "--create-reflog",
))
_GIT_TAG_LISTS = ("ln", (
    "--list", "--contains", "--no-contains", "--merged", "--no-merged",
    "--points-at", "--sort", "--format", "--column",
))


def _classify_git(args: list[str]) -> str:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a.startswith(("-c", "--config-env", "--exec-path=")):
            return "write"  # config can make any git command run arbitrary programs
        i += 2 if a in ("-C", "--git-dir", "--work-tree", "--namespace") else 1
    if i >= len(args):
        return "none"
    sub, rest = args[i], args[i + 1:]
    if sub in ("push", "push-upstream"):
        return "external"
    if sub in _GIT_READ_SUBCOMMANDS:
        return "write" if any(_long_matches(a, ("--output",)) for a in rest) else "none"
    if sub == "branch":
        return _git_listing(rest, _GIT_BRANCH_WRITES, _GIT_BRANCH_LISTS)
    if sub == "tag":
        return _git_listing(rest, _GIT_TAG_WRITES, _GIT_TAG_LISTS)
    if sub == "remote":
        positional = [a for a in rest if not a.startswith("-")]
        return "none" if not positional or positional[0] in ("show", "get-url") else "write"
    return "write"


def _git_listing(
    args: list[str],
    writes: tuple[str, tuple[str, ...]],
    lists: tuple[str, tuple[str, ...]],
) -> str:
    """``git branch`` / ``git tag`` read only when listing: no mutating option,
    and a positional argument only as a pattern to a listing option."""
    listing = False
    positional = False
    for a in args:
        flags = _short_flags(a)
        if set(flags) & set(writes[0]) or _long_matches(a, writes[1]):
            return "write"
        if set(flags) & set(lists[0]) or _long_matches(a, lists[1]):
            listing = True
        elif not a.startswith("-"):
            positional = True
    return "write" if positional and not listing else "none"
