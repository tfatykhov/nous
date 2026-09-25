# Harness Autonomy Phase 2c — Evidence-Aware Claim Verification Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A first-person completion claim ("I saved the file", "I pushed", "email sent to …") is grounded when *any* tool that can produce that effect succeeded with arguments that show it did — not only the one tool the claim's regex names, and not merely any successful call of a capable tool.

**Architecture:** Claims become *kinds* (file_write, email, vcs_push, vcs_commit, deploy). Each successful call becomes an `Evidence` item (tool, evidence-bearing args, bash exit code, side-effect class) built from this turn's full `ToolResult.arguments` and the in-memory ledger's recent actions. Per kind, a tool counts only when its arguments carry the effect's *signal* (a write redirect, a mail command, `git push`, `smtplib` in a script). A claim is a violation only when no evidence item reaches `exact` or `plausible`. Enforcement semantics are unchanged (next-turn correction).

**Tech Stack:** Python 3.12+, stdlib `re`/`dataclasses`, pytest. No DB change.

**Spec:** `docs/superpowers/plans/2026-09-24-harness-autonomy-roadmap.md` §2 row P2.7 and §3 row 2c. Anchors from `main` `240c795`.

**v2.17 (codex round 13):** a shell function definition (`name() { … }`, `function name { … }`) runs nothing — its body is emitted (uncertain) only if the name is called, transitively; an `elif`/`else` arm after one already taken never runs; a Python method resolves through `x = Cls()` assignments and the enclosing class of `self`, and an ambiguous receiver with several candidate methods is withheld rather than guessed; `for x in []:` / `range(0)` never runs its body.

**v2.16 (codex round 12):** a shell branch whose condition is a constant command never runs (`if false; then git push; fi`, `while false; do …`, nested) and its commands are not evidence; a real condition's branch may have run and stays uncertain. A deploy CLI is judged by its subcommand, not any operand (`systemctl status restart` shows a unit's status). `tar` writes its archive only when creating or appending (`-tf`/`-xf` read it).

**v2.15 (codex round 11):** a Python call resolves to *its* definition — a bare `save()` to the module's `save`, `Obj().save()` to Obj's method, an `x.save()` with an unknown receiver to every class's `save`, never to the module function of that name; a class body's own statements run at definition; the ledger marks a cut in a stored argument (`EVIDENCE_TRUNCATED`) and cut code is unreadable (plausible), never regex-scanned — the regex fallback is gone, code that does not parse is no evidence (it did not run).

**v2.14 (codex round 10):** a program named by a path is the trusted tool only from a system directory (`/usr/bin/git` is git; `./git`, `bin/git`, `/tmp/evil/git` are local executables — plausible at most, never exact); a `subprocess` call is read as argv (`['echo', 'git', 'push']` runs echo) or, for a shell line, through the bash reader; the executable walk resolves constant `while` tests and never enters an `except` handler.

**v2.13 (codex round 9):** the syntax-tree read covers only what a script *runs* — module-level statements, the functions and methods they call (transitively, by name), the taken side of a constant `if` (`if False:` never; `if __name__ == "__main__":` always) — so a call inside an uncalled `def`, a lambda or a dormant method is not evidence; a failed final send never supplies its recipient even when an earlier send exists.

**v2.12 (codex round 8):** Python evidence is read from the syntax tree (`_python_facts`: the destinations written, whom it sends to, the shell strings it runs, deploy modules), so a comment or a printed string never counts; code that does not parse falls back to the regexes over comment-stripped text. When full turn evidence is supplied, the ledger's bounded copies of this turn's calls are not pooled (a copy past the 64-invocation cap reads as unreadable → plausible and would outvote the untruncated *none*). Output options are scoped to the programs they mean output for (`grep --file` reads patterns; `-o` for pandoc/gcc/curl/sort…, `-O` for wget, `--file` for tar). `${#x}` is not a comment.

**v2.11 (codex round 7):** "I emailed the report to alice@x.io" captures its recipient (object-first form); curl addresses a mail only through `--mail-rcpt` (a `--data` payload, an attachment name or a header never name a recipient); a read command naming the path (`cat /tmp/report.md; touch other`) is not a save to it.

**v2.10 (codex round 6):** substitution contents are emitted *before* the outer commands, so the last invocation is always the outer command a non-zero exit belongs to (`git push origin $(git branch --show-current)` exiting 1 is a failed push, not a failed branch lookup); a `$(…)` folded inside a quoted word is read (uncertain), a backtick or unbalanced one is opaque; git's terminal global options (`--version`, `-v`, `--help`, `--exec-path` …) run no subcommand, for the ledger classifier too.

**v2.9 (codex round 5):** a command inside `$(…)` / `<(…)` has its status masked by the outer command (`echo $(git push)` exits 0 because `echo` did) and is never certain; a plain subshell `(…)` keeps its own. Python evidence matches the write's *destination* (`open('/tmp/x.bak','w')` is not a save to `/tmp/x`; a computed path is plausible) and the *recipients* written out (`sendmail(from, TO, …)`, `msg['To']`, `to_addrs=`), never a sender, body or comment.

**v2.8 (codex round 4):** a negated pipeline (`! git push`) and a backgrounded list (`git push &`) are never certain — the shell's 0 means the command failed, or merely started.

**v2.7 (codex round 3):** the reader keeps output-redirect targets as a command's destination (`\t>`-marked argument), and a bash save must write to *that* destination — a redirect, a `cp`/`mv`/`tee`/`rsync` target, `-o`/`--output`, `sed -i`, `tar czf` — so `rm x` and `touch x.bak` never ground "saved to x" (a known writer writing elsewhere, or a delete, is none; a script naming the path in an unknown option stays plausible); a mail's recipient is read from the sending invocation's recipient arguments, never its subject or body (`sendmail bob <<EOF hello alice EOF` is not a send to alice).

**v2.6 (codex round 2):** a pipeline's exit code is its last stage's, so a piped push (`git push | tail`) is plausible, never exact; `write_file` evidence must *be* the claimed target after normalization (an absolute target in full — `/var/archive/report.md` is not `/tmp/report.md`); recipients compare as whole addresses everywhere (`malice@x.io` is not `alice@x.io`); `send_file` never grounds a claim that names an address; `_PATH` accepts `dir/file.ext`.

**v2.5 (codex round 1 on #646):** an invocation carries whether it *certainly ran and succeeded* (`command_runs`): from the exit code only the last AND-OR list's status is known, so `exact` is reserved for its commands when it exited 0 with no `||`; a command after `||`, or before `;`, is plausible (skipped-vs-masked is unknowable, and a false violation on `cd repo && git push || echo failed` is the worse failure under `enforce`); a failed opaque last command is no evidence; unquoted `#` comments are cut before reading; first-person email claims capture a named recipient, so `send_email` to someone else is not exact evidence (code that sends to a variable stays plausible unless it writes out a different address). Timing tests pin scaling (4× input ≪ 16×), not laptop wall-clock.

**v2.4 (review round 3):** a heredoc body is data handed to one command, never commands — the reader cuts it out before lexing and attaches it as one argument, so `python3 - <<'EOF' … EOF` is judged as code (like `python -c`) and `cat > runbook.md <<'EOF' git push EOF` no longer grounds a push. A script, an interpreter given a script or module, a task runner, or a shell running a file counts when its text hints at the claimed effect (`python3 scripts/daily_brief.py` may have mailed; `python3 --version` cannot have), and a test run never does. Deploy adds process managers, `cap`, `gh workflow run`, `npm start`; rsync/scp ground "pushed the files to the server". A Nous artifact suppresses a claim only as the head of its phrase — "the dashboard changes" and "the surface renderer fix" are commits, "a report dashboard" is not.

**v2.3 (review round 2):** a command string (`bash -c`, `su -c`, `eval`, `env -S`, `flock -c`, `watch`, `ssh host CMD`) is read, not treated as a wall; `python -c CODE` is judged as code; an interpreter, script or task runner is opaque only when its text hints at the claimed effect (`uv run python export.py` may have exported; `python3 --version`, `uv sync`, `npm test` cannot have pushed, sent or deployed anything — otherwise every bash call grounded every claim again). Deploy is an allowlist of changing subcommands (`docker ps`, `kubectl get`, `tail deploy.log` look at a deployment). The invocation cap is the ledger's only, and past it the ledger keeps "unreadable", never fewer commands. Nous's own producers (`compose_surface`, `push_surface`, `learn_fact`, `ingest_document`) ground an untargeted file/message claim. Extraction: the object of a claim is inside one clause and stops at a relative/infinitive clause, so "I created a schedule that generates the report" is not a file claim; a VCS noun must head what was pushed or committed; "by <someone>" marks another actor's completion; a condition earlier in the clause is not a claim; a question is judged by the next clause end so "I pushed the fix — want me to deploy it?" still counts.

**v2.2 (after implementation review — deliberate deviation from the Global Constraint "a claim with no successful capable call carrying the signal is still a violation"):** an independent verify-by-execution review of the implementation found that stricter grounding produced NEW false violations relative to `main` (which grounded any vcs claim on any bash call) wherever the evidence could not be read. So **unreadable is not no-signal**: `command_invocations` returns None (not `[]`) for a command it cannot read — a heredoc body with an apostrophe, a quote cut in half, over the size cap — and such evidence is `plausible`; so is a command that runs something opaque (`bash -c`, `eval`, `ssh`, an interpreter, a script, a task runner). Only a command the reader READ, doing something else, disproves a claim. The ledger reads a bash command's invocations at record time from the whole command (its head+tail copy could drop a push). A non-zero exit disproves only an effect of the last command. Shell reserved words are skipped by the reader and the ledger classifier alike. Extraction narrows to the agent's own completed actions: push/commit need a VCS object ("I pushed an approval card" is `push_surface` wording), effects landing in memory/the companion app/"below" are not file or git claims, a target is only where the object was saved, the `and` rule is clause-scoped, and questions, conditions and quoted text are not claims.

**v2.1 (after re-review):** the abbreviation branches of the sentence span take only a dot followed by whitespace — overlapping branches backtracked exponentially ("etc.," ×20 = 4.5 s on the event loop); an `and <verb>` clause needs an earlier first-person *claim*, not just any "I" ("I checked: the DAG … and sent the email" is narration), and "I" is case-sensitive; a bash mail command must classify `external`, a commit must be a write, `git push -n` is not a push, and a deploy needs a deploy/transfer tool, not any network call.

**v2 (after 3-agent review):** evidence requires a per-kind signal — a run_python call or a `curl` no longer grounds an email claim (devil's advocate P1); a read-only bash call never grounds a file claim (architect P2); an `and <verb>` clause is a claim only after a first-person subject in the same sentence, so "the node ran and sent the email" is narration, not a claim (both); the span tolerates dots inside words (`config.yaml`, `v1.2`); a target is captured from "I saved the report to /tmp/x.md" too; evidence keeps the head *and* tail of long commands, and this turn's evidence is untruncated; `git push --dry-run`/`git log | grep push` are not a push; deploy accepts `docker`/`kubectl`/`helm`/`systemctl` commands.

## Global Constraints

- No migration, no new setting. `NOUS_CLAIM_VERIFICATION_MODE` keeps `shadow|warn|enforce`; `enforce` still means "inject a correction into the next turn".
- `ClaimViolation.expected_tool` keeps its current values (`write_file`, `send_email`, `bash`); the full capable set is a new field. Every pre-existing `TestClaimVerifier` test keeps passing unchanged.
- `ExecutedAction` gains only default-valued fields.
- Claims are **first-person** statements ("I" + claim verb, or "and" + claim verb after one) plus the two actor-less completion statements the existing tests pin ("… saved to <path>", "email sent to …"). Third-person narration of other agents' work ("the node ran and sent the email", "I checked: the DAG … and sent …") must never become a violation — prod runs `enforce`.
- Claim extraction runs synchronously on the event loop after every reply: no pattern may backtrack exponentially (a timing test pins it).
- A claim with **no** successful capable call carrying the signal is still a violation.
- Nothing new is persisted durably: evidence args live in the in-memory ledger only.
- Known, accepted limits: a pipeline masks a failed push's exit code (`git push | tail`); quoted user text that reads as a claim.
- Tests run with `UV_PROJECT_ENVIRONMENT=E:/Projects/nous/.venv uv run --frozen pytest …`. Commit explicit paths only — never `git add <dir>`/`.`/`-A` in this repo.

## Why (verified defects, `main` `240c795`)

| Defect | Anchor |
|---|---|
| One tool per claim: "I created a backup file" after `cp` via bash is a violation | `nous/cognitive/claim_verifier.py:39-60` |
| "I pushed" passes on **any** successful bash call (`ls` counts) | `claim_verifier.py:48-51,94-113` |
| A failed `git push` counts: bash never flags a non-zero exit | `nous/api/builtin_tools.py:105-117` |
| `re.DOTALL` + greedy `.+` spans paragraphs; `finditer` merges claims | `claim_verifier.py:39-66,124-130` |
| No word boundary: "API sent the message" matches `I sent` | `claim_verifier.py:40-51` |
| "I saved the file and sent the email" misses the email claim | `claim_verifier.py:44-47`; `reports/f026_eval.md` |
| This turn's full arguments are available but discarded | `nous/api/runner.py:2798` |
| The event cannot measure precision (no claim count, evidence level, turn) | `runner.py:2803-2821` |

## Design

| Kind | `expected_tool` | Counts as evidence (exit 0 for bash; target must match when the claim names one) |
|---|---|---|
| `file_write` | `write_file` | write_file (path matches target, or no target → exact); bash with side effect `write`/`external` (names target → exact, else plausible); run_python whose code writes a file (`open(…,'w'/'a')`, `.write_text(`, `.write_bytes(`, `to_csv(`, `to_json(`, `savefig(`, `json.dump(`, `shutil.copy/move`) |
| `email` | `send_email` | send_email (recipient matches target, or none → exact); send_file (plausible); bash whose command sends mail (`mail`, `mailx`, `sendmail`, `mutt`, `msmtp`, `swaks`, `curl … smtp(s)://` or `--mail-rcpt`); run_python whose code sends (`smtplib`, `sendmail`, `api.telegram.org`) |
| `vcs_push` | `bash` | bash whose command runs `git … push` (not `--dry-run`) and classifies `external` → exact; run_python with `git` and `push` → plausible |
| `vcs_commit` | `bash` | bash whose command runs `git … commit` (not `--dry-run`) → exact; run_python with `git` and `commit` → plausible |
| `deploy` | `bash` | bash that classifies `external`, or whose command names `deploy`, `docker`, `kubectl`, `helm`, `systemctl`, `terraform`, `ansible` → plausible; run_python naming the same → plausible |

A names-only evidence item (legacy `verify(response, ["write_file"], ledger)` callers pass names, no args) is `plausible` for its capable kinds.

---

### Task 1: Evidence-bearing fields on the in-memory ledger

**Files:**
- Modify: `nous/cognitive/execution_ledger.py` (`ExecutedAction` `:97-107`, `record` `:130-158`)
- Modify: `nous/cognitive/ledger_store.py` (use the shared exit-code parser; drop its private `_BASH_EXIT_CODE`)
- Test: `tests/test_execution_ledger.py` (new class `TestEvidenceFields`)

**Interfaces:**
- Produces: `ExecutedAction.exit_code: int | None = None`; `ExecutedAction.evidence_args: dict[str, str] = field(default_factory=dict)`; `evidence_args(tool_name: str, tool_input: dict[str, Any], *, limit: int | None = EVIDENCE_ARG_CHARS) -> dict[str, str]`; `bash_exit_code(result: str | None) -> int | None`; `EVIDENCE_ARG_CHARS = 2000`.

- [ ] **Step 1: Write the failing tests**

```python
class TestEvidenceFields:
    def test_bash_exit_code_is_parsed_from_the_trailer(self):
        ledger = ExecutionLedger(session_id="s")
        ok = ledger.record("bash", {"command": "git push"}, "Everything up-to-date\nExit code: 0", "success")
        bad = ledger.record("bash", {"command": "git push"}, "rejected\nExit code: 1", "success")
        assert ok.exit_code == 0 and bad.exit_code == 1

    def test_a_timeout_has_no_exit_code(self):
        ledger = ExecutionLedger(session_id="s")
        assert ledger.record("bash", {"command": "sleep 99"}, "Command timed out after 30s.", "error").exit_code is None

    def test_exit_code_only_for_bash(self):
        ledger = ExecutionLedger(session_id="s")
        assert ledger.record("write_file", {"path": "/tmp/x"}, "ok\nExit code: 1", "success").exit_code is None

    def test_long_commands_keep_head_and_tail(self):
        cmd = "git commit -m '" + "x" * 5000 + "' && git push origin main"
        ledger = ExecutionLedger(session_id="s")
        action = ledger.record("bash", {"command": cmd}, "Exit code: 0", "success")
        kept = action.evidence_args["command"]
        assert kept.startswith("git commit") and kept.endswith("git push origin main")
        assert len(kept) <= EVIDENCE_ARG_CHARS + 5
        assert action.key_args["command"] == cmd[:80]  # the prompt-facing summary is unchanged

    def test_unbounded_evidence_for_this_turn(self):
        cmd = "x" * 5000
        assert evidence_args("bash", {"command": cmd}, limit=None)["command"] == cmd

    def test_evidence_args_only_for_effect_tools(self):
        assert evidence_args("recall_deep", {"query": "q"}) == {}
        assert evidence_args("send_email", {"to": ["a@x.io"], "subject": "s", "body": "b"}) == {
            "to": "['a@x.io']", "subject": "s"}
        assert evidence_args("run_python", {"code": "open('r.md','w')"}) == {"code": "open('r.md','w')"}

    def test_bash_exit_code_parser(self):
        assert bash_exit_code("out\nExit code: 0\n") == 0
        assert bash_exit_code("Exit code: -9") == -9
        assert bash_exit_code("quoted 'Exit code: 0' then\nExit code: 2") == 2
        assert bash_exit_code(None) is None and bash_exit_code("no trailer") is None
```

Add `EVIDENCE_ARG_CHARS, bash_exit_code, evidence_args` to the file's `from nous.cognitive.execution_ledger import (...)` block.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen pytest tests/test_execution_ledger.py -k TestEvidenceFields -q` → FAIL (ImportError).

- [ ] **Step 3: Implement** in `nous/cognitive/execution_ledger.py`, next to `_KEY_ARGS`:

```python
# Argument values a completion claim can be checked against (harness Phase 2c).
# In-memory only -- never persisted, never rendered into the prompt. Bounded
# head AND tail, so a long commit message still shows the `git push` after it.
EVIDENCE_ARG_CHARS = 2000
_EVIDENCE_ARGS: dict[str, tuple[str, ...]] = {
    "bash": ("command", "cmd"),
    "run_python": ("code",),
    "write_file": ("path", "file_path"),
    "send_email": ("to", "cc", "subject"),
    "send_file": ("file_path", "chat_id", "caption"),
}
# bash_tool always appends this trailer: the authoritative wrapper status.
_BASH_EXIT_CODE = re.compile(r"(?:\A|\n)Exit code: (-?\d+)\s*\Z")


def _bounded(value: str, limit: int | None) -> str:
    if limit is None or len(value) <= limit:
        return value
    half = limit // 2
    return f"{value[:half]}\n…\n{value[-half:]}"


def evidence_args(
    tool_name: str, tool_input: dict[str, Any], *, limit: int | None = EVIDENCE_ARG_CHARS,
) -> dict[str, str]:
    """The argument values a claim about this call can be checked against."""
    return {
        key: _bounded(str(tool_input[key]), limit)
        for key in _EVIDENCE_ARGS.get(tool_name, ())
        if tool_input.get(key) is not None
    }


def bash_exit_code(result: str | None) -> int | None:
    """Exit code from bash_tool's trailer; None when absent (timeout, spawn error)."""
    if not result:
        return None
    match = _BASH_EXIT_CODE.search(result)
    return int(match.group(1)) if match else None
```

`ExecutedAction` gains, after `side_effect_type`:

```python
    exit_code: int | None = None  # bash only: a non-zero exit is not evidence
    evidence_args: dict[str, str] = field(default_factory=dict)
```

`record()` sets `exit_code=bash_exit_code(result) if tool_name == "bash" else None` and `evidence_args=evidence_args(tool_name, tool_input)`. In `ledger_store.py`, import `bash_exit_code` and replace the `_BASH_EXIT_CODE.search(text)` use in `_summary` with `if (code := bash_exit_code(text)) is not None: parts.append(f"exit code {code}")`; delete the private regex.

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_execution_ledger.py tests/test_ledger_store.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/execution_ledger.py nous/cognitive/ledger_store.py tests/test_execution_ledger.py
git commit -m "feat(ledger): evidence args and bash exit code on the in-memory ledger (harness 2c)"
```

---

### Task 2: Claim extraction by kind, first person only

**Files:**
- Modify: `nous/cognitive/claim_verifier.py` (`ACTION_CLAIM_PATTERNS` `:39-60`, `__init__` `:62-66`, `_extract_claims` `:124-130`)
- Test: `tests/test_claim_verifier.py` (new; `TestClaimVerifier` in `tests/test_execution_integrity.py` stays)

**Interfaces:**
- Produces: `@dataclass(frozen=True) class ClaimKind: primary_tool: str; capable_tools: frozenset[str]`; `CLAIM_KINDS: dict[str, ClaimKind]`; `@dataclass(frozen=True) class Claim: kind: str; text: str; target: str | None = None`; `ClaimVerifier._extract_claims(text) -> list[Claim]` in document order.

- [ ] **Step 1: Write the failing tests**

```python
"""Harness Phase 2c: first-person claims, extracted by kind, one sentence at a time."""

import pytest

from nous.cognitive.claim_verifier import ClaimVerifier


def _kinds(text):
    return [(c.kind, c.target) for c in ClaimVerifier()._extract_claims(text)]


def test_a_word_ending_in_i_is_not_a_subject():
    assert _kinds("The API sent the message to the queue.") == []


def test_a_claim_never_spans_sentences():
    assert _kinds("I created a helper. It reads data.\n\nLater the report file was large.") == []


def test_two_claims_in_one_paragraph_stay_two():
    text = "I saved the summary file. I sent the email to the team."
    assert [k for k, _ in _kinds(text)] == ["file_write", "email"]


def test_a_first_person_compound_is_two_claims():
    assert [k for k, _ in _kinds("I saved the file and sent the email.")] == ["file_write", "email"]


@pytest.mark.parametrize("narration", [
    "The send node completed and sent the premarket email to Tim.",
    "The subtask finished and pushed the branch.",
    "The heartbeat check ran and sent a message to the channel.",
    "Bob reviewed it and sent the email to the team.",
    "I checked: the DAG finished and sent the summary email to Tim.",
    "I see the subtask completed and sent the email to the team.",
    "I confirmed the scheduled task ran and pushed the branch.",
    "As I expected, the node ran and deployed the build.",
    "i sent the email",  # lowercase i is not the first person
])
def test_third_person_narration_is_not_a_claim(narration):
    assert _kinds(narration) == []


def test_a_failed_match_never_backtracks_exponentially():
    import time

    start = time.perf_counter()
    ClaimVerifier()._extract_claims("I saved " + "etc.," * 32 + " nothing " * 3 + "e.g., " * 32)
    assert time.perf_counter() - start < 0.05


@pytest.mark.parametrize("claim", [
    "I wrote the config.yaml file.",
    "I generated the v1.2 report for the board.",
    "I saved the notes (see e.g. section 2) in a file.",
])
def test_dots_inside_words_do_not_end_a_sentence(claim):
    assert [k for k, _ in _kinds(claim)] == ["file_write"]


def test_saved_to_captures_the_target():
    assert _kinds("The output was saved to /tmp/report.txt.") == [("file_write", "/tmp/report.txt")]


def test_i_saved_x_to_path_captures_the_target():
    assert _kinds("I saved the report to /tmp/x.md.") == [("file_write", "/tmp/x.md")]


def test_email_sent_to_captures_the_recipient():
    assert _kinds("Email sent to Alice@Example.com.") == [("email", "alice@example.com")]


def test_push_commit_deploy_are_distinct_kinds():
    assert [k for k, _ in _kinds("I committed the fix. I pushed it. I deployed the build.")] == [
        "vcs_commit", "vcs_push", "deploy"]


def test_plans_are_not_claims():
    assert _kinds("I will save the file and then I'll send the email.") == []


def test_a_curly_apostrophe_subject_is_a_claim():
    assert [k for k, _ in _kinds("I’ve saved the report file.")] == ["file_write"]
```

- [ ] **Step 2: Run to verify they fail** — `uv run --frozen pytest tests/test_claim_verifier.py -q` → FAIL.

- [ ] **Step 3: Implement** — replace the pattern table and extraction:

```python
@dataclass(frozen=True)
class ClaimKind:
    primary_tool: str          # kept as ClaimViolation.expected_tool (back-compat)
    capable_tools: frozenset[str]


CLAIM_KINDS: dict[str, ClaimKind] = {
    "file_write": ClaimKind("write_file", frozenset({"write_file", "bash", "run_python"})),
    "email": ClaimKind("send_email", frozenset({"send_email", "send_file", "bash", "run_python"})),
    "vcs_push": ClaimKind("bash", frozenset({"bash", "run_python"})),
    "vcs_commit": ClaimKind("bash", frozenset({"bash", "run_python"})),
    "deploy": ClaimKind("bash", frozenset({"bash", "run_python"})),
}


@dataclass(frozen=True)
class Claim:
    kind: str
    text: str
    target: str | None = None  # a path or recipient the claim names


# First person: "I", "I've", "I have", "I just", "I already". The "I" is
# case-SENSITIVE inside patterns compiled IGNORECASE: a lowercase "i " is not a subject.
_FIRST = r"\b(?-i:I)(?:['’]ve|\s+have|\s+just|\s+already)?\s+"
_CLAIM_VERBS = (r"(?:saved|wrote|written|created|generated|exported|stored|sent|emailed"
                r"|forwarded|mailed|pushed|committed|deployed)\b")
# The second clause of a FIRST-PERSON compound ("I saved X and sent Y"); only
# accepted when a first-person CLAIM ("I" + claim verb) appears earlier in the
# same sentence -- "I checked: the DAG ran and sent the email" is narration.
_AND = r"\band\s+(?:also\s+)?"
# One sentence: a dot ends it only when followed by whitespace or the end, so
# `config.yaml` and `v1.2` stay inside; "e.g." / "i.e." / "etc." do not end it.
# The abbreviation branches take ONLY a dot followed by whitespace, so they
# never overlap `\.(?=\S)` -- overlapping branches backtrack exponentially on
# a failed match ("etc.," x20 took 4.5 s; exclusive branches: 0.03 ms).
_SPAN = (r"(?:[^.\n]|\.(?=\S)|(?<=\be\.g)\.(?=\s)|(?<=\bi\.e)\.(?=\s)"
         r"|(?<=\betc)\.(?=\s)){0,160}?")
_PATH = r"(?P<target>(?:~|\.{0,2})/[\w./-]*\w|[\w-]+\.\w{1,6})"
_ADDRESS = r"(?P<target>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)"

# (pattern, kind, subject-bearing): subject-bearing patterns start with the
# subject group and are subject to the first-person rule.
_CLAIM_PATTERNS: list[tuple[str, str]] = [
    (rf"(?P<subj>{_FIRST}|{_AND})(?:saved|wrote|written|exported|stored)\b{_SPAN}\s+(?:to|at|in)\s+{_PATH}", "file_write"),
    (rf"(?P<subj>{_FIRST}|{_AND})(?:saved|wrote|written|created|generated)\b{_SPAN}\b(?:file|document|report)\b", "file_write"),
    (rf"\b(?:saved|written)\s+to[:\s]+{_PATH}", "file_write"),
    (rf"(?P<subj>{_FIRST}|{_AND})(?:sent|emailed|forwarded|mailed)\b{_SPAN}\b(?:e-?mail|message|report)\b", "email"),
    (rf"\be-?mail(?:ed)?\s+sent\s+to\b(?:\s+{_ADDRESS})?", "email"),
    (rf"(?P<subj>{_FIRST}|{_AND})pushed\b", "vcs_push"),
    (rf"(?P<subj>{_FIRST}|{_AND})committed\b", "vcs_commit"),
    (rf"(?P<subj>{_FIRST}|{_AND})deployed\b", "deploy"),
]
_FIRST_RE = re.compile(_FIRST, re.IGNORECASE)
_FIRST_CLAIM_RE = re.compile(_FIRST + _CLAIM_VERBS, re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n")
```

`__init__` compiles `_CLAIM_PATTERNS` with `re.IGNORECASE` only (no `DOTALL`). `ACTION_CLAIM_PATTERNS` is removed (no other reader). Extraction:

```python
    def _extract_claims(self, text: str) -> list[Claim]:
        found: list[tuple[int, int, Claim]] = []
        for pattern, kind in self._compiled:
            for match in pattern.finditer(text):
                subj = match.groupdict().get("subj")
                if subj is not None and not _FIRST_RE.match(subj):
                    # an "and <verb>" clause: a claim only after a first-person
                    # CLAIM earlier in the same sentence
                    start = max((m.end() for m in _SENTENCE_END.finditer(text, 0, match.start())), default=0)
                    if not _FIRST_CLAIM_RE.search(text, start, match.start()):
                        continue
                target = match.groupdict().get("target")
                if target:
                    target = target.rstrip(".,;:)")
                    if "@" in target:
                        target = target.lower()
                start, end = match.start(), match.end()
                if any(c.kind == kind and s < end and start < e for s, e, c in found):
                    continue  # same claim already captured; the targeted pattern runs first and wins
                found.append((start, end, Claim(kind=kind, text=match.group(0).strip(), target=target)))
        found.sort(key=lambda item: item[0])
        return [claim for _, _, claim in found]
```

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_claim_verifier.py tests/test_execution_integrity.py -k Claim -q` — new file PASSES; `TestClaimVerifier` passes once Task 3 lands (record any failure, fix there).

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/claim_verifier.py tests/test_claim_verifier.py
git commit -m "feat(claims): first-person claims by kind, one sentence, with targets (harness 2c)"
```

---

### Task 3: Evidence levels and `verify()`

**Files:**
- Modify: `nous/cognitive/claim_verifier.py` (`ClaimViolation`, `VerificationResult`, `verify` `:68-122`, `_build_correction` `:132-147`)
- Test: `tests/test_claim_verifier.py`

**Interfaces:**
- Consumes: `Claim`, `CLAIM_KINDS` (Task 2); `ExecutedAction.exit_code/evidence_args/side_effect_type` (Task 1).
- Produces: `@dataclass(frozen=True) class Evidence: tool_name: str; args: Mapping[str, str] = {}; exit_code: int | None = None; side_effect: str = "write"`; `@dataclass(frozen=True) class ClaimCheck: kind: str; text: str; evidence: str`; `evidence_level(claim: Claim, ev: Evidence) -> str` (`exact|plausible|none`); `verify(response, tool_calls_this_turn, ledger, *, turn_evidence: list[Evidence] | None = None) -> VerificationResult`; `VerificationResult.claims: list[ClaimCheck]`; `ClaimViolation.capable_tools: tuple[str, ...] = ()`.

- [ ] **Step 1: Write the failing tests** (append)

```python
from nous.cognitive.claim_verifier import ClaimVerifier, Evidence
from nous.cognitive.execution_ledger import ExecutionLedger


def _verify(text, *evidence, ledger=None):
    return ClaimVerifier().verify(text, [], ledger or ExecutionLedger(session_id="s"),
                                  turn_evidence=list(evidence))


def _bash(command, exit_code=0, side_effect="write"):
    return Evidence("bash", {"command": command}, exit_code=exit_code, side_effect=side_effect)


def test_a_bash_backup_grounds_a_file_claim():
    assert _verify("I created a backup file.", _bash("cp db.sqlite db.bak")).verified


def test_a_read_only_bash_never_grounds_a_file_claim():
    assert not _verify("It was saved to /tmp/report.md.", _bash("cat /tmp/report.md", side_effect="none")).verified


def test_any_bash_no_longer_grounds_a_push():
    assert not _verify("I pushed the changes.", _bash("ls -la", side_effect="none")).verified


@pytest.mark.parametrize("command, side_effect", [
    ("git log --oneline | grep push", "none"),
    ("git push --dry-run origin main", "external"),
])
def test_things_that_mention_push_are_not_a_push(command, side_effect):
    assert not _verify("I pushed the changes.", _bash(command, side_effect=side_effect)).verified


def test_git_push_grounds_a_push_but_a_failed_one_does_not():
    assert _verify("I pushed the changes.", _bash("cd r && git push origin main", side_effect="external")).verified
    assert not _verify("I pushed the changes.", _bash("git push origin main", exit_code=1, side_effect="external")).verified


def test_a_named_target_must_match():
    assert not _verify("It was saved to /tmp/report.md.", Evidence("write_file", {"path": "/tmp/other.md"})).verified
    assert not _verify("It was saved to /tmp/report.md.", _bash("touch /tmp/x")).verified
    assert _verify("It was saved to /tmp/report.md.", Evidence("write_file", {"path": "/tmp/report.md"})).verified


def test_a_script_that_only_reads_grounds_nothing():
    script = Evidence("run_python", {"code": "print(recall_deep('premarket'))"})
    for claim in ("I saved the report file.", "I sent the email to the team.", "I deployed the build."):
        assert not _verify(claim, script).verified, claim


def test_a_web_request_is_not_an_email():
    assert not _verify("I sent the email.", _bash("curl -s https://wttr.in", side_effect="external")).verified


@pytest.mark.parametrize("claim, evidence", [
    ("I sent the email.", _bash("cat /var/log/mail.log", side_effect="none")),
    ("I committed the fix.", _bash("git log --grep commit", side_effect="none")),
    ("I pushed the fix.", _bash("git push -n origin main", side_effect="external")),
    ("I deployed the build.", _bash("curl -s https://api.example.com/status", side_effect="external")),
])
def test_reads_and_no_ops_ground_nothing(claim, evidence):
    assert not _verify(claim, evidence).verified


def test_an_rsync_grounds_a_deploy():
    assert _verify("I deployed the build.", _bash("rsync -a build/ host:/srv", side_effect="external")).verified


@pytest.mark.parametrize("evidence", [
    _bash("sendmail a@x.io < m", side_effect="external"),
    Evidence("run_python", {"code": "import smtplib\ns = smtplib.SMTP('h')"}),
    Evidence("send_file", {"file_path": "/tmp/r.png"}),
])
def test_other_ways_to_send_are_plausible(evidence):
    result = _verify("I sent the email.", evidence)
    assert result.verified and result.claims[0].evidence == "plausible"


def test_a_script_that_writes_grounds_a_file_claim():
    assert _verify("I saved the report file.", Evidence("run_python", {"code": "df.to_csv('r.csv')"})).verified


def test_email_recipient_must_match_when_named():
    other = Evidence("send_email", {"to": "['bob@x.io']", "subject": "s"})
    assert not _verify("Email sent to alice@x.io.", other).verified


def test_docker_compose_grounds_a_deploy():
    assert _verify("I deployed the build.", _bash("docker compose up -d")).verified


def test_no_capable_tool_is_still_a_violation():
    result = _verify("I saved the report file.", Evidence("recall_deep", {}))
    assert not result.verified
    assert result.violations[0].expected_tool == "write_file"
    assert set(result.violations[0].capable_tools) == {"write_file", "bash", "run_python"}


def test_ledger_history_with_evidence_args_counts():
    ledger = ExecutionLedger(session_id="s")
    ledger.record("bash", {"command": "git push"}, "Exit code: 0", "success")
    assert ClaimVerifier().verify("I pushed the fix.", [], ledger).verified


def test_names_only_callers_keep_working():
    assert ClaimVerifier().verify("I saved the report file.", ["write_file"],
                                  ExecutionLedger(session_id="s")).verified


def test_claims_are_reported_even_when_verified():
    result = _verify("I saved the report file.", Evidence("write_file", {"path": "/tmp/r.md"}))
    assert [(c.kind, c.evidence) for c in result.claims] == [("file_write", "exact")]
```

- [ ] **Step 2: Run to verify they fail** — `Evidence` not defined.

- [ ] **Step 3: Implement**

```python
@dataclass(frozen=True)
class Evidence:
    """One successful tool call a claim may be grounded in."""

    tool_name: str
    args: Mapping[str, str] = field(default_factory=dict)
    exit_code: int | None = None   # bash: a non-zero exit is not evidence
    side_effect: str = "write"


@dataclass(frozen=True)
class ClaimCheck:
    kind: str
    text: str
    evidence: str  # "exact" | "plausible" | "none"


_MAIL_COMMAND = re.compile(
    r"\b(?:mail|mailx|sendmail|mutt|msmtp|ssmtp|swaks)\b|\bcurl\b[^|;&]*(?:smtps?://|--mail-rcpt)", re.I)
_PY_WRITES = re.compile(
    r"open\([^)]*['\"][wax]b?\+?['\"]|\.write_(?:text|bytes)\(|\.to_(?:csv|json|excel|parquet)\("
    r"|savefig\(|json\.dump\(|shutil\.(?:copy\w*|move)\(")
_PY_SENDS = re.compile(r"\bsmtplib\b|\bsendmail\b|api\.telegram\.org")
_GIT_PUSH = re.compile(r"\bgit\b[^|;&]*\bpush\b", re.I)
_GIT_COMMIT = re.compile(r"\bgit\b[^|;&]*\bcommit\b", re.I)
_NOT_REALLY = re.compile(r"--dry-run|\s-n\b")  # git push -n / --dry-run change nothing
_DEPLOY_WORDS = re.compile(
    r"\b(?:deploy\w*|docker|kubectl|helm|systemctl|terraform|ansible|rsync|scp|ssh|gcloud|aws|az)\b", re.I)


def _names(target: str, text: str) -> bool:
    """True if ``text`` names the claimed target (full path or basename)."""
    return bool(target and text) and (target in text or target.rsplit("/", 1)[-1] in text)


def evidence_level(claim: Claim, ev: Evidence) -> str:
    """How well one successful call supports one claim."""
    kind = CLAIM_KINDS[claim.kind]
    if ev.tool_name not in kind.capable_tools:
        return "none"
    if ev.tool_name == "bash" and ev.exit_code not in (None, 0):
        return "none"
    if not ev.args:  # names-only evidence from a legacy caller
        return "plausible"
    command = ev.args.get("command") or ev.args.get("cmd") or ""
    code = ev.args.get("code", "")
    if claim.kind == "file_write":
        if ev.tool_name == "write_file":
            path = ev.args.get("path") or ev.args.get("file_path") or ""
            return "exact" if not claim.target or _names(claim.target, path) else "none"
        if ev.tool_name == "bash":
            if ev.side_effect not in ("write", "external"):
                return "none"  # a read cannot have saved anything
            if claim.target:
                return "exact" if _names(claim.target, command) else "none"
            return "plausible"
        if not _PY_WRITES.search(code):
            return "none"
        return "exact" if claim.target and _names(claim.target, code) else (
            "none" if claim.target else "plausible")
    if claim.kind == "email":
        if ev.tool_name == "send_email":
            recipients = f"{ev.args.get('to', '')} {ev.args.get('cc', '')}".lower()
            return "exact" if not claim.target or claim.target in recipients else "none"
        if ev.tool_name == "send_file":
            return "plausible"
        if ev.tool_name == "bash":
            # a mail command that actually leaves the host: `cat mail.log` is a read
            if ev.side_effect != "external" or not _MAIL_COMMAND.search(command):
                return "none"
            body = command
        else:
            if not _PY_SENDS.search(code):
                return "none"
            body = code
        if claim.target and claim.target not in body.lower():
            return "none"
        return "plausible"
    if claim.kind in ("vcs_push", "vcs_commit"):
        pattern = _GIT_PUSH if claim.kind == "vcs_push" else _GIT_COMMIT
        body = command if ev.tool_name == "bash" else code
        if not pattern.search(body) or _NOT_REALLY.search(body):
            return "none"
        if ev.tool_name == "bash":
            needed = ("external",) if claim.kind == "vcs_push" else ("write", "external")
            return "exact" if ev.side_effect in needed else "none"  # `git log --grep commit` is a read
        return "plausible"
    # deploy: a deploy or transfer tool, not any network call (`curl` of an API is not a deploy)
    if ev.tool_name == "bash" and ev.side_effect != "none" and _DEPLOY_WORDS.search(command):
        return "plausible"
    if ev.tool_name == "run_python" and _DEPLOY_WORDS.search(code):
        return "plausible"
    return "none"
```

`verify()`:

```python
    def verify(self, assistant_response, tool_calls_this_turn, ledger, *, turn_evidence=None):
        claims = self._extract_claims(assistant_response)
        if not claims:
            return VerificationResult(verified=True)
        pool = list(turn_evidence) if turn_evidence is not None else [
            Evidence(name) for name in tool_calls_this_turn]
        if ledger is not None:
            recent = ledger.actions[-10:]
            current = [a for a in ledger.actions if a.turn == ledger.current_turn]
            for action in {id(a): a for a in [*current, *recent]}.values():
                if action.status == "success":
                    pool.append(Evidence(action.tool_name, action.evidence_args,
                                         action.exit_code, action.side_effect_type))
        turn_names = set(tool_calls_this_turn) | {ev.tool_name for ev in turn_evidence or ()}
        checks, violations = [], []
        for claim in claims:
            levels = {evidence_level(claim, ev) for ev in pool}
            level = "exact" if "exact" in levels else "plausible" if "plausible" in levels else "none"
            checks.append(ClaimCheck(claim.kind, claim.text, level))
            if level == "none":
                kind = CLAIM_KINDS[claim.kind]
                violations.append(ClaimViolation(
                    claimed_text=claim.text, expected_tool=kind.primary_tool,
                    found_in_turn=kind.primary_tool in turn_names, found_in_ledger=False,
                    capable_tools=tuple(sorted(kind.capable_tools))))
        if not violations:
            return VerificationResult(verified=True, claims=checks)
        return VerificationResult(verified=False, violations=violations, claims=checks,
                                  correction=self._build_correction(violations))
```

`ClaimViolation` gains `capable_tools: tuple[str, ...] = ()`; `VerificationResult` gains `claims: list[ClaimCheck] = field(default_factory=list)`. `_build_correction` line per violation: `f'  - Claimed: "{v.claimed_text}" (expected tool: {v.expected_tool}; any of: {", ".join(v.capable_tools)}) — no successful call that could have done this was recorded.'` — keeps `ungrounded action claims`, `write_file`, `Do not assert` (pinned by `test_build_correction_message`). Add `from collections.abc import Mapping`.

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_claim_verifier.py tests/test_execution_integrity.py -q` → PASS, every pre-existing `TestClaimVerifier` test unchanged.

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/claim_verifier.py tests/test_claim_verifier.py
git commit -m "feat(claims): ground a claim in any capable call whose arguments show the effect (harness 2c)"
```

---

### Task 4: Runner passes full evidence; the event records claims

**Files:**
- Modify: `nous/api/runner.py` `_verify_claims` (`:2781-2834`), imports (`:47`)
- Test: `tests/test_claim_verifier.py`

**Interfaces:**
- Consumes: `Evidence`, `VerificationResult.claims` (Task 3); `evidence_args(limit=None)`, `bash_exit_code`, `classify_side_effect`.
- Produces: event `f026_claim_verification` gains `claim_count: int`, `claims: [{kind, evidence, text[:120]}]`, `turn: int | None`. Existing keys unchanged.

- [ ] **Step 1: Write the failing tests** (append)

```python
from types import SimpleNamespace

from nous.cognitive.schemas import ToolResult


def _runner_with_capture():
    from nous.api.runner import AgentRunner

    runner = AgentRunner.__new__(AgentRunner)
    runner._claim_verifier = ClaimVerifier()
    runner._intent_tracker = None
    runner._settings = SimpleNamespace(claim_verification_mode="enforce")
    runner._pending_corrections = {}
    events = []
    runner._log_f026_decision = lambda kind, data, session_id: events.append((kind, data))
    return runner, events


def test_runner_grounds_a_claim_in_this_turns_untruncated_arguments():
    runner, events = _runner_with_capture()
    long_cmd = "git commit -m '" + "x" * 5000 + "' && git push origin main"
    results = [ToolResult(tool_name="bash", arguments={"command": long_cmd}, result="Exit code: 0")]
    runner._verify_claims("s1", "I pushed the fix.", results, ExecutionLedger(session_id="s1"))
    (_, data), = events
    assert data["verified"] and data["claim_count"] == 1
    assert data["claims"] == [{"kind": "vcs_push", "evidence": "exact", "text": "I pushed"}]
    assert runner._pending_corrections == {}


def test_runner_rejects_a_failed_push():
    runner, events = _runner_with_capture()
    results = [ToolResult(tool_name="bash", arguments={"command": "git push"}, result="rejected\nExit code: 1")]
    runner._verify_claims("s1", "I pushed the fix.", results, ExecutionLedger(session_id="s1"))
    assert not events[0][1]["verified"] and runner._pending_corrections["s1"]


def test_runner_ignores_third_person_narration():
    runner, events = _runner_with_capture()
    runner._verify_claims("s1", "The send node completed and sent the email to Tim.", [],
                          ExecutionLedger(session_id="s1"))
    assert events[0][1]["verified"] and events[0][1]["claim_count"] == 0
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** in `_verify_claims`:

```python
        turn_results = [tr for tr in tool_results if tr.error is None]
        turn_tool_names = [tr.tool_name for tr in turn_results]
        turn_evidence = [
            Evidence(
                tool_name=tr.tool_name,
                args=evidence_args(tr.tool_name, tr.arguments or {}, limit=None),
                exit_code=bash_exit_code(tr.result) if tr.tool_name == "bash" else None,
                side_effect=classify_side_effect(tr.tool_name, tr.arguments or {}),
            )
            for tr in turn_results
        ]
        if self._claim_verifier:
            verification = self._claim_verifier.verify(
                response_text, turn_tool_names, ledger, turn_evidence=turn_evidence)
```

and add to the event dict: `"claim_count": len(verification.claims)`, `"claims": [{"kind": c.kind, "evidence": c.evidence, "text": c.text[:120]} for c in verification.claims]`, `"turn": ledger.current_turn if ledger is not None else None`. Imports: `from nous.cognitive.claim_verifier import ClaimVerifier, Evidence, IntentTracker`; add `bash_exit_code, classify_side_effect, evidence_args` to the `nous.cognitive.execution_ledger` import.

- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_claim_verifier.py tests/test_execution_integrity.py tests/test_f026_persistence.py tests/test_runner.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/api/runner.py tests/test_claim_verifier.py
git commit -m "feat(claims): runner passes untruncated turn evidence; event records each claim (harness 2c)"
```

---

### Task 5: Docs

**Files:** `CLAUDE.md` (row `NOUS_CLAIM_VERIFICATION_MODE`)

- [ ] **Step 1:** Replace the row description with: `Claim verification mode (shadow/warn/enforce). Only first-person completion claims are checked ("I saved…", "I pushed", "email sent to…"; third-person narration of other agents' work is not a claim). A claim is grounded when any capable tool succeeded with arguments that show the effect — write_file, a bash write, or a script that writes a file; send_email, send_file, a mail command, or a script using smtplib; bash running \`git push\` (not --dry-run) — and a named path or recipient must match. A bash call with a non-zero exit is not evidence. \`enforce\` injects a correction into the next turn (streaming cannot unsend). The \`f026_claim_verification\` event records every claim with its evidence level (exact/plausible/none) and the turn, so precision is measurable.`

- [ ] **Step 2: Commit** `git add CLAUDE.md && git commit -m "docs: evidence-aware claim verification (harness 2c)"`

---

## Out of scope (recorded)

- Blocking before the reply is sent — unchanged, as the roadmap accepts.
- One-shot sessions (`dag-summary-*`, `subtask-*`) drop the next-turn correction; the event still records the violation.
- A failed push hidden behind a pipeline (`git push | tail`), and quoted user text that reads as a claim.
- Claims about memory writes, scheduling or DAGs — no pattern exists today.
