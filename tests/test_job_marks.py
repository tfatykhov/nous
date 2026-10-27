"""The marks Nous writes into a Claude Code job's directory (nous.api.job_marks), on a real filesystem."""

from __future__ import annotations

import os
import sys
import time

import pytest

from nous.api import job_marks
from nous.api.job_marks import JOB_MARK_NAMES, read_job_mark, write_job_mark

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="job marks need dir_fd and O_NOFOLLOW")

JOB = "job-20261027-101500-ab12cd34"


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "claude-jobs" / JOB).mkdir(parents=True)
    return tmp_path


def _mark(ws, name="cancelled_by"):
    return ws / "claude-jobs" / JOB / name


def test_the_mark_names_are_the_two_nous_writes():
    assert JOB_MARK_NAMES == frozenset({"cancelled_by", "nous_root"})
    assert job_marks._SUPPORTED


def test_write_then_read_round_trips_and_strips_one_trailing_newline(ws):
    assert write_job_mark(str(ws), JOB, "cancelled_by", "line\n\n", overwrite=True) is True
    assert _mark(ws).read_bytes() == b"line\n\n"
    assert read_job_mark(str(ws), JOB, "cancelled_by", max_bytes=512) == "line\n"
    assert write_job_mark(str(ws), JOB, "nous_root", "abc", overwrite=False) is True
    assert read_job_mark(str(ws), JOB, "nous_root", max_bytes=64) == "abc"


def test_overwrite_replaces_the_whole_file(ws):
    assert write_job_mark(str(ws), JOB, "cancelled_by", "x" * 100, overwrite=True)
    assert write_job_mark(str(ws), JOB, "cancelled_by", "short", overwrite=True)
    assert _mark(ws).read_text() == "short"


def test_without_overwrite_an_existing_mark_is_kept(ws):
    assert write_job_mark(str(ws), JOB, "nous_root", "first", overwrite=False)
    assert write_job_mark(str(ws), JOB, "nous_root", "second", overwrite=False) is False
    assert _mark(ws, "nous_root").read_text() == "first"


@pytest.mark.parametrize(
    "job_id",
    ["job-1", "..", f"{JOB}/..", f"../claude-jobs/{JOB}", f"{JOB}x", JOB.upper(), "", None],
)
def test_an_invalid_job_id_is_refused(ws, job_id):
    assert write_job_mark(str(ws), job_id, "cancelled_by", "x", overwrite=True) is False
    assert read_job_mark(str(ws), job_id, "cancelled_by", max_bytes=512) is None
    assert not _mark(ws).exists()


@pytest.mark.parametrize("name", ["status", "../cancelled_by", "cancelled_by/x", "", "nous_root "])
def test_a_name_outside_the_list_is_refused(ws, name):
    assert write_job_mark(str(ws), JOB, name, "x", overwrite=True) is False
    assert read_job_mark(str(ws), JOB, name, max_bytes=512) is None
    assert sorted(p.name for p in (ws / "claude-jobs" / JOB).iterdir()) == []


def test_a_missing_job_dir_creates_nothing(tmp_path):
    assert write_job_mark(str(tmp_path), JOB, "cancelled_by", "x", overwrite=True) is False
    assert not (tmp_path / "claude-jobs").exists()
    (tmp_path / "claude-jobs").mkdir()
    assert write_job_mark(str(tmp_path), JOB, "cancelled_by", "x", overwrite=True) is False
    assert list((tmp_path / "claude-jobs").iterdir()) == []
    assert read_job_mark(str(tmp_path), JOB, "cancelled_by", max_bytes=512) is None


def test_a_symlinked_job_dir_is_neither_written_nor_read(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "nous_root").write_text("planted")
    (tmp_path / "claude-jobs").mkdir()
    (tmp_path / "claude-jobs" / JOB).symlink_to(elsewhere, target_is_directory=True)
    assert write_job_mark(str(tmp_path), JOB, "cancelled_by", "x", overwrite=True) is False
    assert not (elsewhere / "cancelled_by").exists()
    assert read_job_mark(str(tmp_path), JOB, "nous_root", max_bytes=64) is None


def test_a_symlinked_mark_is_neither_written_through_nor_read(ws, tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("keep me")
    _mark(ws).symlink_to(target)
    assert write_job_mark(str(ws), JOB, "cancelled_by", "x", overwrite=True) is False
    assert target.read_text() == "keep me"
    assert read_job_mark(str(ws), JOB, "cancelled_by", max_bytes=512) is None


def test_a_dangling_symlinked_mark_creates_nothing(ws, tmp_path):
    target = tmp_path / "created-through-the-link"
    _mark(ws, "nous_root").symlink_to(target)
    for overwrite in (True, False):
        assert write_job_mark(str(ws), JOB, "nous_root", "x", overwrite=overwrite) is False
    assert not target.exists()


def test_a_hard_linked_mark_is_neither_truncated_nor_read(ws, tmp_path):
    other = tmp_path / "other.txt"
    other.write_text("not a mark")
    os.link(other, _mark(ws))
    assert write_job_mark(str(ws), JOB, "cancelled_by", "x", overwrite=True) is False
    assert other.read_text() == "not a mark"
    assert read_job_mark(str(ws), JOB, "cancelled_by", max_bytes=512) is None


def test_a_directory_or_a_fifo_named_like_the_mark_is_refused_without_blocking(ws):
    _mark(ws).mkdir()
    assert write_job_mark(str(ws), JOB, "cancelled_by", "x", overwrite=True) is False
    assert read_job_mark(str(ws), JOB, "cancelled_by", max_bytes=512) is None
    os.mkfifo(_mark(ws, "nous_root"))
    started = time.monotonic()
    assert write_job_mark(str(ws), JOB, "nous_root", "x", overwrite=True) is False
    assert read_job_mark(str(ws), JOB, "nous_root", max_bytes=64) is None
    assert time.monotonic() - started < 5


def test_a_symlinked_claude_jobs_dir_is_the_owners_layout_and_works(tmp_path):
    volume = tmp_path / "volume"
    (volume / JOB).mkdir(parents=True)
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "claude-jobs").symlink_to(volume, target_is_directory=True)
    assert write_job_mark(str(tmp_path / "ws"), JOB, "cancelled_by", "x", overwrite=True) is True
    assert (volume / JOB / "cancelled_by").read_text() == "x"


def test_content_over_the_limit_or_not_utf8_reads_as_none(ws):
    _mark(ws, "nous_root").write_bytes(b"x" * 64)
    assert read_job_mark(str(ws), JOB, "nous_root", max_bytes=64) == "x" * 64
    _mark(ws, "nous_root").write_bytes(b"x" * 65)
    assert read_job_mark(str(ws), JOB, "nous_root", max_bytes=64) is None
    _mark(ws).write_bytes(b"\xff\xfe")
    assert read_job_mark(str(ws), JOB, "cancelled_by", max_bytes=512) is None


def test_a_missing_mark_reads_as_none(ws):
    assert read_job_mark(str(ws), JOB, "cancelled_by", max_bytes=512) is None


def test_a_job_id_with_non_ascii_digits_is_refused(ws):
    lookalike = "job-٢٠٢٦١٠٢٧-١٠١٥٠٠-ab12cd34"
    (ws / "claude-jobs" / lookalike).mkdir()
    assert write_job_mark(str(ws), lookalike, "cancelled_by", "x", overwrite=True) is False
    assert list((ws / "claude-jobs" / lookalike).iterdir()) == []
    assert read_job_mark(str(ws), lookalike, "cancelled_by", max_bytes=512) is None


@pytest.mark.parametrize("workspace_dir", [None, 7, "/tmp/ws\x00x"])
def test_a_workspace_that_is_not_a_clean_string_is_refused_without_raising(workspace_dir):
    assert write_job_mark(workspace_dir, JOB, "cancelled_by", "x", overwrite=True) is False
    assert read_job_mark(workspace_dir, JOB, "cancelled_by", max_bytes=512) is None
