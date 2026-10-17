"""scripts/ci/protected_paths.py: the CODEOWNERS reader and the pull-request tripwire."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "protected_paths.py"
spec = importlib.util.spec_from_file_location("protected_paths", SCRIPT)
pp = importlib.util.module_from_spec(spec)
sys.modules["protected_paths"] = pp
spec.loader.exec_module(pp)

NOUS = "nous@tfatykhov.com"
OWNER = "owner@example.com"
PATTERNS = ["/scripts/ops/**", "/Dockerfile", "/tests/test_task_scor*.py", "/.github/**", "/nous/identity/"]


def test_parse_ignores_comments_blank_lines_and_owners():
    text = "# comment\n\n/a/** @owner\n/b.py @owner @other  # trailing\n"
    assert pp.parse_codeowners(text) == ["/a/**", "/b.py"]


@pytest.mark.parametrize("bad", ["*.py", "docs/x.md", "/a/[bc].py", "/a?b", "!/x", "/a/***", "/a\\ b"])
def test_unsupported_patterns_fail_loudly(bad):
    with pytest.raises(pp.UnsupportedPattern):
        pp.parse_codeowners(f"{bad} @owner\n")


@pytest.mark.parametrize(
    ("path", "hit"),
    [
        ("scripts/ops/db_roles.sql", True),
        ("scripts/ops/deep/er/x", True),
        ("scripts/opsx/db_roles.sql", False),
        ("scripts/ops", False),
        ("Dockerfile", True),
        ("Dockerfile.dev", False),
        ("sub/Dockerfile", False),
        ("tests/test_task_scoring.py", True),
        ("tests/test_task_scores_migration.py", True),
        ("tests/sub/test_task_scoring.py", False),
        (".github/CODEOWNERS", True),
        (".github/workflows/ci.yml", True),
        ("nous/identity/manager.py", True),
        ("nous/identity", False),
        ("nous/identity_other.py", False),
    ],
)
def test_pattern_matching(path, hit):
    assert bool(pp.protected([path], PATTERNS)) is hit


def test_the_real_codeowners_is_inside_the_supported_subset_and_every_pattern_still_matches_a_file():
    patterns = pp.parse_codeowners((ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8"))
    assert patterns, "CODEOWNERS lists no path"
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.split("\0")
    dead = [p for p in patterns if not pp.protected(tracked, [p])]
    assert not dead, f"CODEOWNERS patterns that match no tracked file (renamed or mistyped?): {dead}"


def test_codeowners_protects_itself_and_the_tripwire():
    patterns = pp.parse_codeowners((ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8"))
    for path in (
        ".github/CODEOWNERS",
        ".github/workflows/protected-paths.yml",
        "scripts/ci/protected_paths.py",
        "scripts/ops/db_roles.sql",
        "docker-compose.yml",
        "Dockerfile",
    ):
        assert pp.protected([path], patterns), path


# -- a throwaway repository ---------------------------------------------------------------------------------


def git(repo: Path, *args: str, email: str = OWNER) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": email,
        "PATH": os.environ["PATH"],
        "HOME": str(repo),
    }
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env).stdout


def commit(repo: Path, files: dict[str, str | None], email: str = OWNER) -> None:
    for name, content in files.items():
        path = repo / name
        if content is None:
            git(repo, "rm", "-q", name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        git(repo, "add", name)
    git(repo, "commit", "-q", "-m", "c", email=email)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, {"README.md": "x", "scripts/ops/a.sql": "a", "app/x.py": "x"})
    git(tmp_path, "branch", "pr")
    git(tmp_path, "checkout", "-q", "pr")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(emails=(NOUS,), login="", logins=()):
    return pp.violations(PATTERNS, "main", "pr", list(emails), login, list(logins))


def test_an_agent_commit_outside_the_protected_paths_is_clean(repo):
    commit(repo, {"app/y.py": "y"}, email=NOUS)
    assert run() == []


def test_an_agent_commit_inside_a_protected_path_is_flagged(repo):
    commit(repo, {"scripts/ops/new.sql": "n"}, email=NOUS)
    assert [f for f in run() if "scripts/ops/new.sql" in f]


def test_the_owner_changing_a_protected_path_is_not_flagged(repo):
    commit(repo, {"scripts/ops/new.sql": "n"}, email=OWNER)
    assert run() == []


def test_a_deleted_protected_file_and_a_rename_out_of_a_protected_path_are_flagged(repo):
    commit(repo, {"scripts/ops/a.sql": None}, email=NOUS)
    assert run()
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-q", "-D", "pr")
    git(repo, "checkout", "-q", "-b", "pr")
    git(repo, "mv", "scripts/ops/a.sql", "app/moved.sql")
    git(repo, "commit", "-q", "-m", "mv", email=NOUS)
    assert [f for f in run() if "scripts/ops/a.sql" in f]
    assert [f for f in run(emails=(), login="nous-bot", logins=["nous-bot"]) if "scripts/ops/a.sql" in f]


def test_the_committer_email_counts_as_well_as_the_author(repo):
    (repo / "Dockerfile").write_text("FROM x")
    git(repo, "add", "Dockerfile")
    subprocess.run(
        ["git", "commit", "-q", "-m", "c", "--author", f"t <{OWNER}>"],
        cwd=repo,
        check=True,
        capture_output=True,
        env={"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": NOUS, "PATH": os.environ["PATH"], "HOME": str(repo)},
    )
    assert run()


def test_email_match_is_case_insensitive(repo):
    commit(repo, {".github/workflows/x.yml": "x"}, email=NOUS.upper())
    assert run()


def test_a_pull_request_opened_by_the_agent_login_is_judged_on_its_net_change_whatever_the_commit_email(repo):
    commit(repo, {"Dockerfile": "FROM x"}, email="someone-else@example.com")
    assert run() == []  # commits alone say nothing
    findings = run(login="Nous-Bot", logins=["nous-bot"])
    assert [f for f in findings if "Dockerfile" in f]


def test_a_commit_that_is_undone_inside_the_pull_request_leaves_no_net_change(repo):
    commit(repo, {"Dockerfile": "FROM x"}, email="someone-else@example.com")
    commit(repo, {"Dockerfile": None}, email="someone-else@example.com")
    assert run(login="nous-bot", logins=["nous-bot"]) == []


def test_a_merge_commit_that_carries_a_protected_change_is_flagged(repo):
    git(repo, "checkout", "-q", "main")
    commit(repo, {"scripts/ops/main_side.sql": "m"})
    git(repo, "checkout", "-q", "pr")
    commit(repo, {"app/z.py": "z"})
    git(repo, "merge", "-q", "--no-ff", "-m", "merge main", "main", email=NOUS)
    assert [f for f in run() if "scripts/ops/main_side.sql" in f]


def test_main_exits_one_when_flagged_and_zero_when_clean(repo, monkeypatch, capsys):
    (repo / ".github").mkdir()
    (repo / ".github" / "CODEOWNERS").write_text("/scripts/ops/** @owner\n")
    monkeypatch.setenv("NOUS_COMMIT_EMAILS", NOUS)
    commit(repo, {"app/y.py": "y"}, email=NOUS)
    assert pp.main(["--base", "main", "--head", "pr"]) == 0
    commit(repo, {"scripts/ops/n.sql": "n"}, email=NOUS)
    assert pp.main(["--base", "main", "--head", "pr"]) == 1
    assert "::error::protected path" in capsys.readouterr().out


def test_main_refuses_to_run_without_an_identity_list_or_with_a_bad_codeowners(repo, monkeypatch):
    (repo / "CODEOWNERS").write_text("*.py @owner\n")
    monkeypatch.setenv("NOUS_COMMIT_EMAILS", NOUS)
    assert pp.main(["--codeowners", "CODEOWNERS", "--base", "main", "--head", "pr"]) == 2
    (repo / "CODEOWNERS").write_text("/scripts/ops/** @owner\n")
    monkeypatch.setenv("NOUS_COMMIT_EMAILS", "")
    assert pp.main(["--codeowners", "CODEOWNERS", "--base", "main", "--head", "pr"]) == 2


def test_main_says_so_when_the_login_rule_has_nobody_to_apply_to(repo, monkeypatch, capsys):
    (repo / ".github").mkdir()
    (repo / ".github" / "CODEOWNERS").write_text("/scripts/ops/** @owner\n")
    monkeypatch.setenv("NOUS_COMMIT_EMAILS", NOUS)
    monkeypatch.delenv("NOUS_PR_LOGINS", raising=False)
    assert pp.main(["--base", "main", "--head", "pr"]) == 0
    assert "NOUS_PR_LOGINS is empty" in capsys.readouterr().out
    monkeypatch.setenv("NOUS_PR_LOGINS", "nous-bot")
    assert pp.main(["--base", "main", "--head", "pr"]) == 0
    assert "NOUS_PR_LOGINS is empty" not in capsys.readouterr().out
