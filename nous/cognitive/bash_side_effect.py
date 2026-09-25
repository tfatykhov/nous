"""Whole-command side-effect classification for the ``bash`` tool.

Harness Phase 1b persists every side-effecting tool call to
``nous_system.execution_ledger`` and skips pure reads, so this verdict decides
whether a bash call leaves a durable record at all. The two errors are not
symmetric: a false ``write`` costs one ledger row, a false ``none`` means a
command that changed something is never recorded. Every ambiguity therefore
resolves to ``write``.

The whole command is classified: it is lexed with bash quoting (a quoted or
escaped ``;`` or ``<`` is a word, never an operator), split into simple
commands on ``| ; & && || ( )`` and newlines, each simple command is
classified on its own, and the most severe verdict wins. Output redirection
to anything but a ``/dev/null``-style sink or a file descriptor is a write;
command substitution, which is opaque here, is a write; an assignment that
can change what runs (``PATH=.``, ``LD_PRELOAD=``) is a write; wrappers
(``sudo``, ``timeout``, ``xargs`` ...) are parsed to the command they run;
and the read-only commands that have a writing or executing mode
(``find -delete``, ``sed -i``, ``sort -o``, awk programs that redirect or run
commands, ``git branch -D`` ...) are checked for it.

Classification runs on the event loop, so every recursion path -- ``eval``,
``bash -c``, ``find -exec``, ``flock -c``, ``watch`` -- shares one work budget,
exhausting it is a ``write``, and a command longer than ``_MAX_COMMAND_CHARS``
is a ``write`` without being lexed at all.
"""

from __future__ import annotations

import re
from contextvars import ContextVar

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

# Commands whose purpose is another host: transfer, remote shell, mail, cloud.
_EXTERNAL_COMMANDS = frozenset({
    "curl", "wget", "http", "httpie",
    "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "socat", "telnet", "ftp", "lftp",
    "mail", "mailx", "sendmail", "mutt",
    "gh", "aws", "gcloud", "gsutil", "az", "kubectl", "helm",
})
# Run another command, parsed to it: (short options taking a value, long
# options taking a value, operands before the command). Never below write.
_WRAPPERS: dict[str, tuple[str, tuple[str, ...], int]] = {
    "sudo": ("CDghpRrTtUu", (
        "--close-from", "--chdir", "--group", "--host", "--prompt", "--chroot",
        "--role", "--type", "--command-timeout", "--other-user", "--user",
    ), 0),
    "doas": ("Cu", (), 0),
    "timeout": ("sk", ("--signal", "--kill-after"), 1),
    "nohup": ("", (), 0),
    "time": ("fo", ("--format", "--output"), 0),
    "nice": ("n", ("--adjustment",), 0),
    "ionice": ("cnp", ("--class", "--classdata", "--pid", "--pgid", "--uid"), 0),
    "command": ("", (), 0),
    "exec": ("a", (), 0),
    "xargs": ("adEILnPs", (
        "--arg-file", "--delimiter", "--eof", "--replace", "--max-lines",
        "--max-args", "--max-procs", "--max-chars", "--process-slot-var",
    ), 0),
    "stdbuf": ("ioe", ("--input", "--output", "--error"), 0),
    "setsid": ("", (), 0),
    "chronic": ("", (), 0),
    "flock": ("wE", ("--wait", "--timeout", "--conflict-exit-code"), 1),
    "watch": ("n", ("--interval",), 0),
}
# Take a command STRING (`-c '...'`) and run it.
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "su"})
_SEVERITY = {"none": 0, "write": 1, "external": 2}

# Substitution this analysis cannot see into: $(...), `...`, <(...), >(...).
_OPAQUE = re.compile(r"\$\(|`|<\(|>\(")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# Assignments that cannot change what runs or what it does. Anything else --
# PATH, LD_PRELOAD, GIT_*, LESSOPEN, a variable a later segment reads -- is a
# write, including a bare assignment, which persists for the next segment.
_HARMLESS_ASSIGNMENT = re.compile(r"(?:LANG|LANGUAGE|LC_[A-Z]+|TZ|TERM|COLUMNS|LINES|NO_COLOR)=")

# bash quoting, as a single scan. A quoted or escaped operator character is
# part of a word; only a bare run of operator characters is an operator.
_LEX = re.compile(
    r"""(?P<ws>[ \t\r]+)
      | '(?P<sq>[^']*)'
      | "(?P<dq>(?:[^"\\]|\\.)*)"
      | \\(?P<esc>.)
      | (?P<op>[();<>|&\n]+)
      | (?P<bare>[^ \t\r'"\\();<>|&\n]+)
      | (?P<bad>['"\\])""",
    re.VERBOSE | re.DOTALL,
)
_DQ_ESCAPE = re.compile(r"\\([$`\"\\\n])")
_OPERATOR = re.compile(r"&>>|&>|>>|>&|>\||<>|<<<|<<|<&|\|\||\|&|&&|;;|[|;&()<>\n]")
_OUTPUT_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})
_INPUT_REDIRECTS = frozenset({"<", "<<", "<<<", "<&"})
_HARMLESS_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})
# bash opens a socket when a redirection names one of these -- in either direction.
_NETWORK_REDIRECT = ("/dev/tcp/", "/dev/udp/")

# Work units (words and lexed characters) one top-level classification may spend.
_WORK_LIMIT = 200_000
# Lexing is O(n) and runs on the event loop before the budget can meter it: a
# longer command is a write without being lexed (a false write is the safe
# direction, and a command this large is almost always writing something).
_MAX_COMMAND_CHARS = 64_000
_work: ContextVar[list[int] | None] = ContextVar("bash_classifier_work", default=None)


class _OverBudget(Exception):
    """The shared work budget ran out: the verdict is write."""


def _spend(units: int) -> None:
    left = _work.get()
    if left is not None:
        left[0] -= units
        if left[0] < 0:
            raise _OverBudget


def classify_bash_command(command: str) -> str:
    """Classify a bash command as ``'none'`` | ``'write'`` | ``'external'``.

    Total: an input this analysis did not anticipate is a ``write``, never an
    exception -- a raise here would fail the durable row open and the call
    would leave no record at all. Nested calls (``eval``, ``bash -c``) share
    the outermost call's work budget.
    """
    if len(command) > _MAX_COMMAND_CHARS:
        return "write"
    outer = _work.get()
    token = _work.set([_WORK_LIMIT]) if outer is None else None
    try:
        return _classify(command)
    except _OverBudget:
        if outer is not None:
            raise  # let the outermost call stop, instead of each level retrying
        return "write"
    except Exception:
        return "write"
    finally:
        if token is not None:
            _work.reset(token)


def command_invocations(command: str) -> list[tuple[str, list[str]]] | None:
    """``(program, arguments)`` of every simple command ``command`` runs, or
    None when it cannot be read.

    Read with the classifier's lexer and unwrapping: quoting is respected,
    redirect targets are not arguments, and reserved words, assignments,
    ``env`` and wrappers (``sudo``, ``timeout`` ...) are peeled off, so
    `cd r && sudo git push` runs git while `echo "git push"` and `git log
    --grep push` do not push. The split mirrors ``_classify``. A command
    STRING (``bash -c``, ``su -c``, ``eval``, ``env -S``, ``flock -c``,
    ``watch``, ``ssh host CMD``) is read the same way, to a bounded depth;
    one that cannot be read stays as the program that runs it.

    Evidence for completion claims (harness 2c). None -- an unbalanced quote
    (a heredoc body with an apostrophe), an input over the classifier's size
    cap, or anything unexpected -- is NOT "nothing runs": the caller must not
    read absence into it. ``[]`` means it was read and runs nothing.
    Total and linear.
    """
    runs = command_runs(command, None)
    return None if runs is None else [(prog, args) for prog, args, _ in runs]


def command_runs(command: str, exit_code: int | None) -> list[tuple[str, list[str], bool]] | None:
    """``command_invocations`` with, per invocation, whether it CERTAINLY ran
    and succeeded, as far as the exit code of the whole command can tell.

    Only the last AND-OR list's status is known: with exit 0 and no `||` in
    it, each of its commands ran and succeeded. A command after `||` (or
    before one) may have been skipped or masked; a command in an earlier
    list, joined by `;` or a newline, ran (the first) or may have (the rest)
    with its success unknown. Certainty never turns absence into a verdict:
    an uncertain command is still reported.
    """
    if len(command) > _MAX_COMMAND_CHARS:
        return None
    try:
        return _invocations(command, 0, exit_code)
    except Exception:
        return None


_MAX_STRING_DEPTH = 3
_LIST_JOINS = frozenset({"&&", "||"})
_PIPES = frozenset({"|", "|&"})
# The delimiter after an unquoted `<<` / `<<-`.
_HEREDOC_DELIM = re.compile(r"(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?(\$?\w[\w.-]*))")


def _split_heredocs(command: str) -> tuple[str, list[str]]:
    """Cut every heredoc body out of ``command``; return it and the bodies in
    order. A body is data handed to one command -- lexing it as commands read
    `cat > runbook.md <<EOF ... git push ... EOF` as a push, and made a body
    with an apostrophe unreadable. Unterminated, it runs to the end (bash).

    Quote-aware, or `git commit -m "explain <<EOF heredocs"` would swallow
    the lines after it, and a heredoc inside `bash -c "..."` would be cut at
    the wrong level; only a `<<` outside quotes opens one, and a nested string
    keeps its body for the recursive read. `<<<` is a here-string. The same
    scan cuts an unquoted comment (`#` starting a word) to the end of its
    line: the lexer does not know comments, and `echo ok # ; git push` runs
    no git.
    """
    lines = command.split("\n")
    kept: list[str] = []
    bodies: list[str] = []
    quote: str | None = None  # a quote may span lines
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        pending: list[tuple[str, bool]] = []
        j, n = 0, len(line)
        while j < n:
            c = line[j]
            if quote == "'":
                quote = None if c == "'" else quote
            elif quote == '"':
                if c == "\\":
                    j += 1
                elif c == '"':
                    quote = None
            elif c == "\\":
                j += 1
            elif c in "'\"":
                quote = c
            elif c == "#" and (j == 0 or line[j - 1] in " \t;&|(){}"):
                line = line[:j]  # a comment, to the end of the line
                break
            elif c == "<" and line.startswith("<<", j) and not line.startswith("<<<", j) \
                    and (j == 0 or line[j - 1] != "<"):
                m = _HEREDOC_DELIM.match(line, j + 2)
                if m:
                    pending.append((m.group(2) or m.group(3) or m.group(4), m.group(1) == "-"))
                    j = m.end()
                    continue
                j += 1
            j += 1
        kept.append(line)
        for delim, strip_tabs in pending:
            body: list[str] = []
            while i < len(lines):
                candidate = lines[i].rstrip("\r")
                i += 1
                if strip_tabs:
                    candidate = candidate.lstrip("\t")
                if candidate == delim:
                    break
                body.append(candidate)
            bodies.append("\n".join(body))
    return "\n".join(kept), bodies


def _invocations(
    command: str, depth: int, exit_code: int | None,
) -> list[tuple[str, list[str], bool]] | None:
    command, bodies = _split_heredocs(command)
    tokens = _lex(command)
    if tokens is None:
        return None
    # lists of pipelines of simple commands: `a; b && c | d`
    lists: list[list[tuple[str | None, list[list[str]]]]] = []
    pipelines: list[tuple[str | None, list[list[str]]]] = []
    commands: list[list[str]] = []
    join: str | None = None
    words: list[str] = []
    attached: list[str] = []  # heredoc bodies of the current simple command
    expect_target = False

    def end_command() -> None:
        nonlocal words, attached
        if words or attached:
            commands.append(words + attached)
        words, attached = [], []

    def end_pipeline(next_join: str | None) -> None:
        nonlocal commands, join
        end_command()
        if commands:
            pipelines.append((join, commands))
        commands, join = [], next_join

    def end_list() -> None:
        nonlocal pipelines
        end_pipeline(None)
        if pipelines:
            lists.append(pipelines)
        pipelines = []

    for tok, is_operator in tokens:
        if not is_operator:
            if expect_target:
                expect_target = False
            else:
                words.append(tok)
            continue
        for op in _OPERATOR.findall(tok):
            if op in _OUTPUT_REDIRECTS or op in _INPUT_REDIRECTS or op == ">&":
                expect_target = True
                if op == "<<" and bodies:
                    attached.append("\n" + bodies.pop(0))
            elif op in _LIST_JOINS:
                end_pipeline(op)
            elif op in _PIPES:
                end_command()
            else:  # `;`, a newline, `&`, `(`, `)`: the list ends
                end_list()
    end_list()

    found: list[tuple[str, list[str], bool]] = []
    for li, plist in enumerate(lists):
        last_list = li == len(lists) - 1
        has_or = any(j == "||" for j, _ in plist)
        # every command of the last list ran and succeeded iff it exited 0
        # with no `||` to skip or mask one
        certain = last_list and exit_code == 0 and not has_or
        for pi, (_, cmds) in enumerate(plist):
            last_pipeline = last_list and pi == len(plist) - 1
            for cmd_words in cmds:
                start = _command_start(cmd_words)
                if start is None or cmd_words[start].startswith("\n"):
                    continue  # nothing runs, or a heredoc body left where a program should be
                word = cmd_words[start]
                prog, args = _program(word), cmd_words[start + 1:]
                if "/" in word.replace("\\", "/"):
                    prog = "./" + prog  # path-qualified: a script or a local build, marked as such
                inner = command_string(prog, args)
                if inner is not None and depth < _MAX_STRING_DEPTH:
                    # a runner that certainly succeeded ran its string to exit
                    # 0; otherwise the string's exit code is the whole
                    # command's only when the runner is the last pipeline
                    if certain:
                        sub_exit: int | None = 0
                    else:
                        sub_exit = exit_code if last_pipeline and not has_or else None
                    sub = _invocations(inner, depth + 1, sub_exit)
                    if sub is not None:
                        found.extend((p, a, certain and c) for p, a, c in sub)
                        continue
                found.append((prog, args, certain))
    return found


_SSH_VALUE_OPTIONS = frozenset("bBcDEeFIiJLlmOopQRSWw")


def _env_split_string(args: list[str]) -> str | None:
    """The string `env -S` runs, past env's options and assignments."""
    for j, a in enumerate(args):
        if a in ("-S", "--split-string"):
            return args[j + 1] if j + 1 < len(args) else ""
        if a.startswith("-S"):
            return a[2:]
        if a.startswith("--split-string="):
            return a.split("=", 1)[1]
        if not a.startswith("-") and not _ASSIGNMENT.match(a):
            break
    return None


def command_string(prog: str, args: list[str]) -> str | None:
    """The command STRING an invocation runs, when it runs one that can be
    found; None when it runs a file, a session, or nothing of the kind."""
    if prog in _SHELLS:
        window = args[:8] if prog == "su" else args
        for j, a in enumerate(window):
            if a == "-c" or "c" in _short_flags(a):
                return args[j + 1] if j + 1 < len(args) else ""
            if prog != "su" and not a.startswith("-"):
                break
        return None
    if prog == "eval":
        return " ".join(args)
    if prog == "env":
        return _env_split_string(args)
    if prog in ("flock", "watch"):
        start = _wrapped_command_start(prog, [prog, *args], 1)
        return start if isinstance(start, str) else None
    if prog == "ssh":
        i = 0
        while i < len(args) and args[i].startswith("-"):
            flags = _short_flags(args[i])
            # the LAST letter of a cluster may take the next word: `-vp 2222`
            i += 2 if flags and flags[-1] in _SSH_VALUE_OPTIONS and len(flags) <= 2 else 1
        rest = args[i + 1:]  # after the host
        return " ".join(rest) if rest else None
    return None


# Shell syntax at the start of a simple command, not a program: what follows runs.
_RESERVED = frozenset({"if", "then", "elif", "else", "fi", "do", "done", "while", "until",
                       "case", "esac", "!", "{", "}"})


def _skip_reserved(words: list[str]) -> int:
    i = 0
    while i < len(words) and words[i] in _RESERVED:
        i += 1
    return i


def _command_start(words: list[str]) -> int | None:
    """Index of the word naming the program a simple command actually runs,
    past reserved words, assignments, ``env`` and wrappers. When what runs
    is a command STRING (``env -S``, ``flock -c``, ``watch``) it is the index
    of that runner; None when nothing runs."""
    i = _skip_reserved(words)
    while True:
        while i < len(words) and _ASSIGNMENT.match(words[i]):
            i += 1
        if i >= len(words):
            return None
        prog = _program(words[i])
        if prog == "env":
            if _env_split_string(words[i + 1:]) is not None:
                return i  # env -S: a command string, read by command_string
            start = _env_command_start(words, i + 1)
            if start is None:
                return i
        elif prog in _WRAPPERS:
            start = _wrapped_command_start(prog, words, i + 1)
            if isinstance(start, str):  # flock -c, watch: a command string
                return i
            if start is None:
                return None
        else:
            return i
        i = start


def _lex(command: str) -> list[tuple[str, bool]] | None:
    """``(text, is_operator)`` tokens with bash quoting; None if unbalanced."""
    tokens: list[tuple[str, bool]] = []
    word: list[str] = []
    in_word = False
    for m in _LEX.finditer(command):
        kind = m.lastgroup
        if kind == "bad":
            return None
        if kind in ("ws", "op"):
            if in_word:
                tokens.append(("".join(word), False))
                word, in_word = [], False
            if kind == "op":
                tokens.append((m.group("op"), True))
        elif kind == "esc":
            if m.group("esc") != "\n":  # backslash-newline is a line continuation
                word.append(m.group("esc"))
                in_word = True
        elif kind == "dq":
            word.append(_DQ_ESCAPE.sub(lambda e: "" if e.group(1) == "\n" else e.group(1), m.group("dq")))
            in_word = True
        else:  # sq, bare
            word.append(m.group(kind))
            in_word = True
    if in_word:
        tokens.append(("".join(word), False))
    return tokens


def _classify(command: str) -> str:
    if not command or not command.strip():
        return "write"
    _spend(len(command) // 32 + 1)
    tokens = _lex(command)
    if tokens is None:  # unbalanced quote
        return "write"
    _spend(len(tokens))

    verdict = "write" if _OPAQUE.search(command) else "none"
    words: list[str] = []
    expect: str | None = None  # a redirect waiting for its target word
    for tok, is_operator in tokens:
        if not is_operator:
            if expect is not None and tok.startswith(_NETWORK_REDIRECT):
                verdict = _worst(verdict, "external")
            elif expect == "out" and tok not in _HARMLESS_SINKS:
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
    """Classify one simple command (no operators, redirections removed).

    Assignments, ``env`` and wrappers are peeled off in a loop -- never by
    recursion, so a chain of any length is linear -- each raising the floor
    it implies, until the command that actually runs is reached.
    """
    _spend(len(words) + 1)
    floor = "none"
    i = _skip_reserved(words)
    while True:
        while i < len(words) and _ASSIGNMENT.match(words[i]):
            if not _HARMLESS_ASSIGNMENT.match(words[i]):
                floor = "write"
            i += 1
        if i >= len(words):
            return floor  # assignments only, or a wrapper/env with nothing to run
        cmd = words[i]
        prog = _program(cmd)
        if prog != cmd:
            # A path may name any program: `/usr/bin/curl` is curl and
            # escalates, but `./cat` is not cat, so it never reads below write.
            floor = _worst(floor, "write")
        if prog == "env":
            start = _env_command_start(words, i + 1)
            if start is None:
                return _worst(floor, "write")
            i = start
            continue
        if prog in _WRAPPERS:
            floor = _worst(floor, "write")
            start = _wrapped_command_start(prog, words, i + 1)
            if isinstance(start, str):  # a command string: flock -c, watch
                return _worst(floor, classify_bash_command(start))
            if start is None:
                return floor
            i = start
            continue
        return _worst(floor, _classify_program(prog, words[i + 1:]))


def _program(word: str) -> str:
    """The program a command word names: `/usr/bin/curl`, `curl.exe` -> `curl`."""
    name = word.replace("\\", "/").rsplit("/", 1)[-1]
    # A Windows executable name is case-insensitive: `Curl.exe` is curl.
    return name[:-4].lower() if name.lower().endswith(".exe") else name


def _classify_program(cmd: str, args: list[str]) -> str:
    if cmd == "git":
        return _classify_git(args)
    if cmd in _SHELLS:
        return _classify_shell(cmd, args)
    if cmd == "eval":
        return _worst("write", classify_bash_command(" ".join(args)))
    if cmd == "docker":
        remote = any(a in ("-H", "--host", "--context") or a.startswith(("--host=", "--context="))
                     for a in args)
        positional = [a for a in args if not a.startswith("-")]
        return "external" if remote or positional[:1] in (["push"], ["pull"], ["login"]) else "write"
    if cmd in _EXTERNAL_COMMANDS:
        return "external"
    if cmd not in READ_COMMANDS:
        return "write"
    rule = _READ_COMMAND_RULES.get(cmd)
    return rule(args) if rule else "none"


def _env_command_start(words: list[str], i: int) -> int | None:
    """Index of the first word after ``env``'s options -- its assignments and
    command follow. None for an option whose command is not a plain word
    list (``-S``)."""
    while i < len(words):
        a = words[i]
        if a in ("-i", "-", "--ignore-environment", "-0", "--null"):
            i += 1
        elif a in ("-u", "--unset", "-C", "--chdir"):
            i += 2
        elif a.startswith(("-u", "--unset=", "-C", "--chdir=")):
            i += 1
        elif a == "--":
            return i + 1
        elif a.startswith("-"):
            return None
        else:
            return i
    return i


def _wrapped_command_start(prog: str, words: list[str], i: int) -> int | str | None:
    """Where the wrapped command starts: an index, a command STRING (``flock
    -c``, ``watch``), or None when nothing is run."""
    short_values, long_values, operands = _WRAPPERS[prog]
    while i < len(words):
        a = words[i]
        if prog == "flock" and (a in ("-c", "--command") or a.startswith("--command=")):
            return _option_value(words, i)
        if a == "--":
            i += 1
            break
        if a.startswith("--") and len(a) > 2:
            i += 2 if "=" not in a and a in long_values else 1
            continue
        flags = _short_flags(a)
        if not flags:
            break
        i += 1
        for k, letter in enumerate(flags):
            if letter in short_values:
                if k == len(flags) - 1:
                    i += 1  # the value is the next word; attached otherwise
                break
    i += operands
    if prog == "flock" and i < len(words) and words[i] in ("-c", "--command"):
        return _option_value(words, i)
    if i >= len(words):
        return None
    if prog == "watch":
        return " ".join(words[i:])  # watch runs its arguments through `sh -c`
    return i


def _option_value(words: list[str], i: int) -> str | None:
    if "=" in words[i]:
        return words[i].split("=", 1)[1]
    return words[i + 1] if i + 1 < len(words) else None


def _classify_shell(cmd: str, args: list[str]) -> str:
    """`bash -c '...'` / `su -c '...'`: the string is a command and is classified
    as one; a script file cannot be vetted. Never below write. A shell's options
    precede its script, so the scan stops at the first operand; su also takes
    `-c` after the user, within its first few words."""
    window = args[:8] if cmd == "su" else args
    for j, a in enumerate(window):
        if a == "-c" or "c" in _short_flags(a):
            if j + 1 >= len(args):
                return "write"
            return _worst("write", classify_bash_command(args[j + 1]))
        if cmd != "su" and not a.startswith("-"):
            break
    return "write"


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


def _classify_find(args: list[str]) -> str:
    verdict = "none"
    j = 0
    while j < len(args):
        a = args[j]
        if a in ("-delete", "-fprint", "-fprint0", "-fprintf", "-fls"):
            return "write"
        if a in ("-exec", "-execdir", "-ok", "-okdir"):
            end = j + 1
            while end < len(args) and args[end] not in (";", "+"):
                end += 1
            if end == j + 1:
                return "write"
            verdict = _worst(verdict, _classify_simple(args[j + 1:end]))
            j = end + 1  # past the terminator; an unterminated -exec ends the scan
            continue
        j += 1
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
    # `>` / `|` redirect or pipe to a command, system() runs one, and gawk's
    # `@include "inplace"` / `@load` edit files or load code.
    return "write" if re.search(r"[>|@]|\bsystem\s*\(", args[i]) else "none"


def _classify_uniq(args: list[str]) -> str:
    """``uniq INPUT OUTPUT`` writes OUTPUT."""
    positional = 0
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            positional += len(args) - i - 1
            break
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
# Talk to another repository -- over the network, conservatively, whatever
# the transport turns out to be.
_GIT_REMOTE_SUBCOMMANDS = frozenset(
    {"push", "push-upstream", "fetch", "pull", "clone", "ls-remote", "send-email", "lfs"}
)

_GIT_BRANCH_WRITES = ("dDmMcCuf", (
    "--delete", "--move", "--copy", "--force", "--set-upstream-to",
    "--unset-upstream", "--edit-description", "--track", "--create-reflog",
))
# Listing switches. --sort / --format / --column only change how a listing
# looks: `git branch --sort=refname x` still creates x.
_GIT_BRANCH_LISTS = ("lar", (
    "--list", "--all", "--remotes", "--contains", "--no-contains", "--merged",
    "--no-merged", "--points-at", "--show-current",
))
_GIT_TAG_WRITES = ("adsfumFe", (
    "--annotate", "--sign", "--local-user", "--force", "--delete", "--message",
    "--file", "--edit", "--cleanup", "--create-reflog",
))
_GIT_TAG_LISTS = ("ln", (
    "--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
))
# Options whose separate next word is their value, never a ref name.
_GIT_LISTING_VALUE_OPTIONS = frozenset({
    "--sort", "--format", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
})


def _git_global_options(args: list[str]) -> tuple[str, int]:
    """Skip git's global options: ``(floor, index of the subcommand)``.

    Configuration can make any git command run a program, so it sets a
    floor -- but the subcommand is still what runs: `git -c x fetch` is a fetch.
    """
    floor = "none"
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a in ("-c", "--config-env"):
            floor, i = "write", i + 2
        elif a.startswith(("-c", "--config-env=", "--exec-path=")):
            floor, i = "write", i + 1
        else:
            i += 2 if a in ("-C", "--git-dir", "--work-tree", "--namespace") else 1
    return floor, i


def _classify_git(args: list[str]) -> str:
    floor, i = _git_global_options(args)
    if i >= len(args):
        return floor
    return _worst(floor, _classify_git_subcommand(args[i], args[i + 1:]))


def git_subcommand(args: list[str]) -> tuple[str, list[str]] | None:
    """``(subcommand, its arguments)`` for git's argument list, or None."""
    _, i = _git_global_options(args)
    return (args[i], args[i + 1:]) if i < len(args) else None


def _classify_git_subcommand(sub: str, rest: list[str]) -> str:
    if sub in _GIT_REMOTE_SUBCOMMANDS:
        return "external"
    if sub == "archive":
        return "external" if any(_long_matches(a, ("--remote",)) for a in rest) else "write"
    if sub == "submodule":
        positional = [a for a in rest if not a.startswith("-")]
        return "external" if positional[:1] == ["update"] else "write"
    if sub in _GIT_READ_SUBCOMMANDS:
        return "write" if any(_long_matches(a, ("--output",)) for a in rest) else "none"
    if sub == "branch":
        return _git_listing(rest, _GIT_BRANCH_WRITES, _GIT_BRANCH_LISTS)
    if sub == "tag":
        return _git_listing(rest, _GIT_TAG_WRITES, _GIT_TAG_LISTS)
    if sub == "remote":
        positional = [a for a in rest if not a.startswith("-")]
        if not positional or positional[0] == "get-url":
            return "none"
        if positional[0] == "show":
            # `remote show <name>` queries the remote unless told not to (-n);
            # a bare `remote show` only lists the configured names.
            return "none" if len(positional) == 1 or "-n" in rest else "external"
        if positional[0] in ("update", "prune"):
            return "external"
        return "write"
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
    skip_value = False
    for a in args:
        if skip_value:
            skip_value = False
            continue
        flags = _short_flags(a)
        if set(flags) & set(writes[0]) or _long_matches(a, writes[1]):
            return "write"
        if a in _GIT_LISTING_VALUE_OPTIONS:
            skip_value = True
        if set(flags) & set(lists[0]) or _long_matches(a, lists[1]):
            listing = True
        elif not a.startswith("-"):
            positional = True
    return "write" if positional and not listing else "none"
