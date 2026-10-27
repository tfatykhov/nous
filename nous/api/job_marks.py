"""Small files Nous writes into a Claude Code job's directory, ``<workspace>/claude-jobs/<job id>/``.

This module is the only code that touches files in a job dir. A mark is one small file whose name is in
``JOB_MARK_NAMES``. The job dir is opened relative to ``claude-jobs`` and the mark relative to the job dir, neither
through a symbolic link, so a mark is never written or read through a link something with write access there
planted, and the job dir is always directly under ``claude-jobs``. A mark must be a regular file with one link.
Where the platform cannot open a file relative to a directory without following links, every call fails closed.
"""

from __future__ import annotations

import os
import re
import stat

JOB_MARK_NAMES = frozenset({"cancelled_by", "nous_root"})
JOB_ID_RE = re.compile(r"job-\d{8}-\d{6}-[0-9a-f]{8}", re.ASCII)  # runner.sh's job directory names

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)  # a FIFO planted as a mark fails or reads empty, never blocks the loop
_SUPPORTED = bool(_O_NOFOLLOW and _O_DIRECTORY) and os.open in os.supports_dir_fd


def _valid(workspace_dir: object, job_id: object, name: object) -> bool:
    return (
        _SUPPORTED
        and isinstance(workspace_dir, str)
        and "\x00" not in workspace_dir  # os.open raises ValueError, not OSError, on a NUL
        and isinstance(job_id, str)
        and JOB_ID_RE.fullmatch(job_id) is not None
        and name in JOB_MARK_NAMES
    )


def _open_mark(workspace_dir: str, job_id: str, name: str, flags: int) -> int:
    """The mark's fd, opened under the job dir, which is opened under ``claude-jobs``. Raises OSError."""
    jobs_fd = os.open(os.path.join(workspace_dir, "claude-jobs"), os.O_RDONLY | _O_DIRECTORY)
    try:
        job_fd = os.open(job_id, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=jobs_fd)
    finally:
        os.close(jobs_fd)
    try:
        return os.open(name, flags | _O_NOFOLLOW | _O_NONBLOCK, 0o644, dir_fd=job_fd)
    finally:
        os.close(job_fd)


def _is_single_regular_file(fd: int) -> bool:
    info = os.fstat(fd)
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def write_job_mark(workspace_dir: str, job_id: str, name: str, content: str, *, overwrite: bool) -> bool:
    """Write ``content`` (UTF-8) to the job's mark ``name``. ``overwrite=False`` writes only a new file. Whether it
    was written: False for an invalid workspace dir, job id or name, a job dir that is missing or a link, a mark
    that exists (``overwrite=False``) or is not a regular file with one link, and any OSError. Never raises."""
    if not _valid(workspace_dir, job_id, name):
        return False
    try:
        data = content.encode("utf-8")
    except (AttributeError, UnicodeError):
        return False
    flags = os.O_WRONLY | os.O_CREAT | (0 if overwrite else os.O_EXCL)
    try:
        fd = _open_mark(workspace_dir, job_id, name, flags)
        try:
            if not _is_single_regular_file(fd):
                return False
            os.ftruncate(fd, 0)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


def read_job_mark(workspace_dir: str, job_id: str, name: str, *, max_bytes: int) -> str | None:
    """The text of the job's mark ``name`` without one trailing newline, or None: an invalid workspace dir, job id
    or name, a missing mark, a link or anything but a regular file with one link, content over ``max_bytes`` or not
    UTF-8, and any OSError. Never raises."""
    if not _valid(workspace_dir, job_id, name):
        return None
    try:
        fd = _open_mark(workspace_dir, job_id, name, os.O_RDONLY)
        try:
            if not _is_single_regular_file(fd):
                return None
            chunks: list[bytes] = []
            left = max_bytes + 1
            while left > 0 and (chunk := os.read(fd, left)):
                chunks.append(chunk)
                left -= len(chunk)
        finally:
            os.close(fd)
    except OSError:
        return None
    data = b"".join(chunks)
    if len(data) > max_bytes:
        return None
    try:
        return data.decode("utf-8").removesuffix("\n")
    except UnicodeDecodeError:
        return None
