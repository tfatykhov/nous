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
from types import SimpleNamespace

import pytest

from nous.api import builtin_tools
from nous.api.builtin_tools import atomic_replace_bytes, write_file_tool

_POSIX = pytest.mark.skipif(not hasattr(os, "fchown"), reason="POSIX ownership; proven by CI (Linux), not on Windows")
# Set-id modes. A set-id bit goes onto the new file last, and only together
# with the owner (setuid) or the group (setgid) it refers to.
_SETGID = 0o2750
_SETUID = 0o4755
_SETUID_SETGID = 0o6750


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


def _own_file(tmp_path: Path, mode: int) -> Path:
    """``f.txt`` holding b"old", the process's own, with ``mode``."""
    target = tmp_path / "f.txt"
    target.write_bytes(b"old")
    os.chmod(target, mode)
    if stat.S_IMODE(target.stat().st_mode) != mode:
        pytest.skip("this platform does not let a file's owner set those bits")
    return target


def _seen_as_someone_elses(monkeypatch, *, uid: int = 0, gid: int = 0) -> None:
    """Show atomic_replace_bytes the file it replaces with owner and group ids
    ``uid`` and ``gid`` higher than they are: another user's file, which takes
    root to create for real. Everything else it reads about that file, and
    everything about the new one, is real."""
    real = builtin_tools._read_state

    def read_state(dfd, name, limit):
        state = real(dfd, name, limit)
        seen = SimpleNamespace(**{k: getattr(state.stat, k) for k in dir(state.stat) if k.startswith("st_")})
        seen.st_uid += uid
        seen.st_gid += gid
        return builtin_tools._State(state.digest, seen)

    monkeypatch.setattr(builtin_tools, "_read_state", read_state)


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
@pytest.mark.parametrize("mode", [pytest.param(_SETGID, id="setgid"), pytest.param(_SETUID, id="setuid")])
def test_snapshotted_replace_keeps_the_owner_and_group(tmp_path, mode):
    """The compare-and-replace form of the same primitive: what a snapshotted
    write and its revert call. The mode comes back whole, set-id bits
    included: the new file has the owner and the group they refer to."""
    target, someone = _someone_elses_file(tmp_path, mode)

    atomic_replace_bytes(target, b"new", expected=hashlib.sha256(b"old").hexdigest(), root=tmp_path)

    after = target.stat()
    assert target.read_bytes() == b"new"
    assert (after.st_uid, after.st_gid) == someone
    assert stat.S_IMODE(after.st_mode) == mode


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
        # the setgid bit goes on after the give-away, and that was refused
        pytest.param(_SETGID, 0o750, id="setgid"),
    ],
)
def test_a_chmod_refused_after_the_chown_does_not_fail_the_write(tmp_path, monkeypatch, mode, kept):
    """Root with CAP_CHOWN and without CAP_FOWNER can give a file away but can
    no longer change its mode once it is someone else's. The mode is copied
    first, while the temp file is still the process's own, so the write lands
    with the owner, the group and the permission bits; only a set-id bit,
    which goes on last, is lost."""
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


@_POSIX
@pytest.mark.parametrize("mode", [pytest.param(_SETGID, id="setgid"), pytest.param(_SETUID, id="setuid")])
def test_the_file_carries_no_set_id_bit_when_it_is_given_away(tmp_path, monkeypatch, mode):
    """A set-id bit is never on the new file while it is still under another
    owner or group than the replaced file's: what the owner step is handed
    has the permission bits only, and the set-id bits come after it. The
    replaced file is the process's own here, so this needs no root."""
    target = _own_file(tmp_path, mode)
    real_fchown = os.fchown
    handed_over: list[int] = []

    def spy(fd, uid, gid):
        handed_over.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_fchown(fd, uid, gid)

    monkeypatch.setattr(os, "fchown", spy)

    atomic_replace_bytes(target, b"new", expected=hashlib.sha256(b"old").hexdigest(), root=tmp_path)

    assert handed_over and set(handed_over) == {mode & 0o777}
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == mode  # same owner and group: the bit is back


@_POSIX
def test_a_refused_chown_leaves_no_set_id_bit(tmp_path, monkeypatch):
    """The replaced file was another user's and neither chown is allowed (any
    process that is not root; root without CAP_CHOWN). The new file stays the
    process's own, so it does not carry the set-id bits of the other owner
    and group."""
    target = _own_file(tmp_path, _SETUID_SETGID)
    _seen_as_someone_elses(monkeypatch, uid=1, gid=1)

    def refused(fd, uid, gid):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "fchown", refused)

    atomic_replace_bytes(target, b"new", expected=hashlib.sha256(b"old").hexdigest(), root=tmp_path)

    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o750


@_POSIX
def test_the_group_only_fallback_keeps_setgid_and_drops_setuid(tmp_path, monkeypatch):
    """The owner could not be given and the group could: the new file has the
    replaced file's group and the process as its owner, so setgid stays and
    setuid does not. The group is a real one the process may set; the
    replaced file's owner is shown as another user's."""
    target, someone = _someone_elses_file(tmp_path, _SETUID_SETGID)
    _seen_as_someone_elses(monkeypatch, uid=1)
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
    assert stat.S_IMODE(after.st_mode) == _SETGID
