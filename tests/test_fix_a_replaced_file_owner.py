"""Post-merge review of #652 (P2-15): a file that write_file replaces keeps its owner and group.

Phase 2.8 (#652) made every write_file a temp file renamed over the target
and copied only the mode onto the new file. With every flag off the file
therefore came back owned by the Nous process: root in the image, where the
workspace belongs to claude-runner.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from pathlib import Path

import pytest

from nous.api.builtin_tools import atomic_replace_bytes, write_file_tool

_POSIX = pytest.mark.skipif(not hasattr(os, "fchown"), reason="POSIX ownership; proven by CI (Linux), not on Windows")
# setgid with group-execute: any chown clears it, so it has to be put back
# after the file is given to its owner.
_SETGID = 0o2750


def _someone_else() -> tuple[int, int] | None:
    """An owner and group this process may give a file to, other than its
    own: anything as root, otherwise its own uid with a supplementary group."""
    if os.geteuid() == 0:
        return 12345, 12345
    groups = [g for g in os.getgroups() if g != os.getegid()]
    return (os.geteuid(), groups[0]) if groups else None


def _someone_elses_file(tmp_path: Path, mode: int = 0o640) -> tuple[Path, tuple[int, int]]:
    """``f.txt`` holding b"old", given to another owner and group."""
    someone = _someone_else()
    if someone is None:
        pytest.skip("needs root, or a supplementary group to give the file to")
    target = tmp_path / "f.txt"
    target.write_bytes(b"old")
    os.chown(target, *someone)
    os.chmod(target, mode)
    return target, someone


@_POSIX
@pytest.mark.asyncio
async def test_write_file_leaves_the_file_with_its_owner_and_group(tmp_path):
    """A plain write_file -- every flag off, nothing bound to the call -- over
    a file that belongs to someone else: it is still theirs afterwards. On
    236c110 it came back owned by the process, uid and gid."""
    target, someone = _someone_elses_file(tmp_path)

    result = await write_file_tool("f.txt", "new", _workspace_dir=str(tmp_path))

    assert not result.get("is_error"), result
    after = target.stat()
    assert target.read_bytes() == b"new"
    assert (after.st_uid, after.st_gid) == someone
    assert stat.S_IMODE(after.st_mode) == 0o640


@_POSIX
def test_snapshotted_replace_keeps_the_owner_and_group(tmp_path):
    """The compare-and-replace form of the same primitive: what a snapshotted
    write and its revert call. The mode comes back whole, set-id bits
    included: the chown clears them, and the mode is copied again after it."""
    target, someone = _someone_elses_file(tmp_path, _SETGID)

    atomic_replace_bytes(target, b"new", expected=hashlib.sha256(b"old").hexdigest(), root=tmp_path)

    after = target.stat()
    assert target.read_bytes() == b"new"
    assert (after.st_uid, after.st_gid) == someone
    assert stat.S_IMODE(after.st_mode) == _SETGID


@_POSIX
def test_group_is_kept_when_the_owner_cannot_be_given(tmp_path, monkeypatch):
    """Only root may give a file away, and the kernel refuses owner and group
    together. A process that is not root may still set a group it is in, so a
    file in a shared-group workspace keeps its group -- and its previous
    owner keeps access to it."""
    target, someone = _someone_elses_file(tmp_path)
    real_fchown = os.fchown

    def no_giveaway(fd, uid, gid):
        if uid != -1:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real_fchown(fd, uid, gid)

    monkeypatch.setattr(os, "fchown", no_giveaway)

    atomic_replace_bytes(target, b"new", expected=hashlib.sha256(b"old").hexdigest(), root=tmp_path)

    after = target.stat()
    assert target.read_bytes() == b"new"
    assert after.st_gid == someone[1]
    assert stat.S_IMODE(after.st_mode) == 0o640


@_POSIX
def test_a_refused_chown_does_not_fail_the_write(tmp_path, monkeypatch):
    """Where the process may change neither owner nor group, the write still
    lands with its content and mode; the file is the process's own."""

    def refused(fd, uid, gid):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "fchown", refused)
    target = tmp_path / "f.txt"
    target.write_bytes(b"old")
    os.chmod(target, 0o640)

    atomic_replace_bytes(target, b"new", expected=hashlib.sha256(b"old").hexdigest(), root=tmp_path)

    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.txt"]


@_POSIX
@pytest.mark.parametrize(
    ("mode", "kept"),
    [
        pytest.param(0o640, 0o640, id="plain-mode"),
        # the chown cleared the setgid bit, and it could not be put back
        pytest.param(_SETGID, 0o750, id="setgid"),
    ],
)
def test_a_chmod_refused_after_the_chown_does_not_fail_the_write(tmp_path, monkeypatch, mode, kept):
    """Root with CAP_CHOWN and without CAP_FOWNER can give a file away but can
    no longer change its mode once it is someone else's. The mode is copied
    first, while the temp file is still the process's own, so the write lands
    with the owner, the group and the permission bits; only a set-id bit,
    which the chown clears, is lost."""
    target, someone = _someone_elses_file(tmp_path, mode)
    real_fchown, real_fchmod = os.fchown, os.fchmod
    given_away: set[int] = set()

    def fchown(fd, uid, gid):
        real_fchown(fd, uid, gid)
        given_away.add(fd)

    def fchmod(fd, bits):
        if fd in given_away:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        real_fchmod(fd, bits)

    monkeypatch.setattr(os, "fchown", fchown)
    monkeypatch.setattr(os, "fchmod", fchmod)

    atomic_replace_bytes(target, b"new", expected=hashlib.sha256(b"old").hexdigest(), root=tmp_path)

    after = target.stat()
    assert target.read_bytes() == b"new"
    assert (after.st_uid, after.st_gid) == someone
    assert stat.S_IMODE(after.st_mode) == kept
