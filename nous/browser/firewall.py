"""Kernel egress filter for the browser user (F100).

Chromium runs as `nous-browser` with its own sandbox off and the container's network namespace shared, so
code that escapes a renderer could open a plain socket around the egress proxy: to the REST API on
127.0.0.1:8000, to the database, to the LAN. The proxy alone is enforced by Chromium's configuration; this
module makes the kernel enforce it, with an owner-match rule on uid `nous-browser`:

    OUTPUT -m owner --uid-owner nous-browser -j NOUS_BROWSER
    NOUS_BROWSER: -o lo -d 127.0.0.1 -p tcp --dport <proxy port> -j ACCEPT
                  -j REJECT

IPv6 gets the same chain with the REJECT only (Chromium reaches the proxy over IPv4). Replies need no
conntrack rule: the proxy's sockets belong to root, so the owner match never sees them.

It fails closed: when the rules cannot be installed, or the probe (run as `nous-browser`) shows they do not
have the intended effect, `FirewallError` is raised and the browser does not start. Needs `cap_add: [NET_ADMIN]`.
Also here: `kill_user_processes`, which ends every process the browser user owns.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

CHAIN = "NOUS_BROWSER"
SETPRIV = "/usr/bin/setpriv"
PROBE_TIMEOUT = 20.0
_SIGKILL = getattr(signal, "SIGKILL", 9)  # Windows has no SIGKILL; this module only acts on Linux

# (argv, stdin) -> (return code, combined output). Replaced in tests.
Runner = Callable[[list[str], str | None], tuple[int, str]]

# Run as the browser user: the "blocked" port must refuse, the proxy port must accept. Exit code says which
# half failed. A DROP would time out, which also counts as blocked.
_PROBE = """
import socket, sys
def reach(port):
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()
blocked, allowed = int(sys.argv[1]), int(sys.argv[2])
if reach(blocked):
    sys.exit(3)
if not reach(allowed):
    sys.exit(4)
"""
_probed_ports: set[int] = set()


class FirewallError(RuntimeError):
    """The browser egress filter is not in place; the browser must not start."""


def _hint(detail: str) -> str:
    return (
        "the browser egress firewall is not in place: the container needs `cap_add: [NET_ADMIN]` and the "
        "`iptables` package (or NOUS_BROWSER_TASK_REQUIRE_EGRESS_FIREWALL=false, not recommended). "
        f"Cause: {detail}"
    )


def run_command(argv: list[str], stdin: str | None = None) -> tuple[int, str]:
    """Run `argv`, return (code, output). A missing program is code 127."""
    try:
        done = subprocess.run(  # noqa: S603
            argv, input=stdin, capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False
        )
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out"
    return done.returncode, (done.stdout + done.stderr).strip()


def _jump(user: str) -> list[str]:
    return ["OUTPUT", "-m", "owner", "--uid-owner", user, "-j", CHAIN]


def chain_restore(port: int | None) -> str:
    """The chain as one iptables-restore transaction: flushed and refilled atomically, so there is never a
    moment with the jump in place and the chain empty. `port` None means reject everything (IPv6)."""
    lines = ["*filter", f":{CHAIN} - [0:0]", f"-F {CHAIN}"]
    if port is not None:
        lines.append(f"-A {CHAIN} -o lo -d 127.0.0.1 -p tcp --dport {port} -j ACCEPT")
    lines += [f"-A {CHAIN} -j REJECT", "COMMIT", ""]
    return "\n".join(lines)


def _install(binary: str, restore: str, user: str, port: int | None, run: Runner) -> None:
    code, out = run([restore, "--noflush"], chain_restore(port))
    if code != 0:
        raise FirewallError(_hint(f"{restore} failed ({code}): {out[:300]}"))
    if run([binary, "-C", *_jump(user)], None)[0] != 0:
        code, out = run([binary, "-I", *_jump(user)[:1], "1", *_jump(user)[1:]], None)
        if code != 0:
            raise FirewallError(_hint(f"{binary} could not add the OUTPUT jump ({code}): {out[:300]}"))
    if run([binary, "-C", *_jump(user)], None)[0] != 0:
        raise FirewallError(_hint(f"{binary}: the OUTPUT jump to {CHAIN} is not in place after adding it"))


def _ipv6_in_use() -> bool:
    try:
        lines = Path("/proc/net/if_inet6").read_text().splitlines()
    except OSError:
        return False
    return any(line.split()[:1] != ["00000000000000000000000000000001"] for line in lines if line.strip())


def ensure_rules(port: int, user: str, run: Runner = run_command) -> None:
    """Install (or repair) the egress rules for `user`; idempotent. Raises FirewallError."""
    _install("iptables", "iptables-restore", user, port, run)
    try:
        _install("ip6tables", "ip6tables-restore", user, None, run)
    except FirewallError as e:
        if _ipv6_in_use():
            raise FirewallError(f"IPv6 is configured but its egress rule failed: {e}") from e
        logger.warning("browser_task: no IPv6 egress rule (no IPv6 addresses in the container): %s", e)


def probe(port: int, user: str, run: Runner = run_command) -> None:
    """Check the rules' effect as `user`, once per process and port: a local port nobody allowed refuses,
    the proxy port accepts. Raises FirewallError."""
    if port in _probed_ports:
        return
    with socket.socket() as decoy:  # listening, so only the rule can stop a connect
        decoy.bind(("127.0.0.1", 0))
        decoy.listen(1)
        blocked = decoy.getsockname()[1]
        code, out = run(
            [
                SETPRIV,
                f"--reuid={user}",
                f"--regid={user}",
                "--clear-groups",
                sys.executable,
                "-I",
                "-c",
                _PROBE,
                str(blocked),
                str(port),
            ],
            None,
        )
    if code == 3:
        raise FirewallError(_hint(f"{user} can still open a socket to 127.0.0.1 around the proxy"))
    if code == 4:
        raise FirewallError(_hint(f"{user} cannot reach the egress proxy on 127.0.0.1:{port}"))
    if code != 0:
        raise FirewallError(_hint(f"the egress probe as {user} failed ({code}): {out[:300]}"))
    _probed_ports.add(port)


def ensure_egress_filter(port: int, user: str, run: Runner = run_command) -> None:
    """Rules in place and proven, or FirewallError. The proxy must already listen on `port`."""
    ensure_rules(port, user, run)
    probe(port, user, run)


def _owned_by(status: str, uid: int) -> bool:
    """Whether /proc/<pid>/status shows a live (not zombie) process whose real or effective uid is `uid`."""
    ids: list[int] = []
    for line in status.splitlines():
        if line.startswith("State:") and line.split()[1:2] == ["Z"]:
            return False
        if line.startswith("Uid:"):
            ids = [int(v) for v in line.split()[1:3]]
    return uid in ids


def _uid_of(user: str) -> int | None:
    import pwd  # POSIX only

    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


def kill_user_processes(user: str, proc_root: Path = Path("/proc"), kill: Callable[[int, int], None] = os.kill) -> int:
    """SIGKILL every process whose real or effective uid is `user`'s, marker or not. Repeats while any are
    left (a process may fork as it dies). Returns how many were killed. Never touches uid 0 or our own."""
    uid = _uid_of(user)
    if uid is None or uid == 0 or uid == getattr(os, "getuid", lambda: -1)():
        return 0
    killed = 0
    for _ in range(5):
        victims = []
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if _owned_by((entry / "status").read_text(), uid):
                    victims.append(int(entry.name))
            except OSError:
                continue
        if not victims:
            break
        for pid in victims:
            try:
                # The pid may have exited and been reused since the scan: look at it again right before the kill.
                if not _owned_by((proc_root / str(pid) / "status").read_text(), uid):
                    continue
                kill(pid, _SIGKILL)
                killed += 1
            except OSError:
                continue
        time.sleep(0.05)
    if killed:
        logger.info("browser_task: killed %d process(es) owned by %s", killed, user)
    return killed
