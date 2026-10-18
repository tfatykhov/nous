"""Tests for the browser user's kernel egress filter and process cleanup (F100): nous/browser/firewall.py.

The rule-building commands run through a fake runner; the process kill runs against a fake /proc tree and,
on Linux, a real child process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nous.browser import firewall
from nous.browser import session as browser_session
from nous.browser.session import BrowserSession, DomainAllowlist

PORT = 18181


class FakeRunner:
    """Records every command. `fail` makes a command whose argv contains one of its words exit non-zero; the
    jump rule (-C) is reported present once it was added; `probe_code` is the probe's exit code."""

    def __init__(self, fail: tuple[str, ...] = (), probe_code: int = 0) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail = fail
        self.probe_code = probe_code
        self.jumps: set[str] = set()

    def __call__(self, argv: list[str], stdin: str | None = None) -> tuple[int, str]:
        self.calls.append((argv, stdin))
        if any(word in argv[0] for word in self.fail):
            return 1, "Permission denied (you must be root)"
        if argv[0] == firewall.SETPRIV:
            return self.probe_code, ""
        if "-C" in argv:
            return (0 if argv[0] in self.jumps else 1), ""
        if "-I" in argv:
            self.jumps.add(argv[0])
        return 0, ""

    def commands(self, program: str) -> list[list[str]]:
        return [a for a, _ in self.calls if a[0] == program]


@pytest.fixture(autouse=True)
def _fresh_probe_cache() -> None:
    firewall._probed_ports.clear()


@pytest.fixture(autouse=True)
def _no_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(firewall, "_ipv6_in_use", lambda: False)


class TestRules:
    def test_installs_an_accept_for_the_proxy_port_then_a_reject_and_the_jump(self) -> None:
        run = FakeRunner()
        firewall.ensure_rules(PORT, "nous-browser", run)
        v4 = next(stdin for argv, stdin in run.calls if argv[0] == "iptables-restore")
        assert v4 is not None
        rules = [line for line in v4.splitlines() if line.startswith("-A")]
        assert rules == [
            f"-A {firewall.CHAIN} -o lo -d 127.0.0.1 -p tcp --dport {PORT} -j ACCEPT",
            f"-A {firewall.CHAIN} -j REJECT",
        ]
        assert [
            "iptables",
            "-I",
            "OUTPUT",
            "1",
            "-m",
            "owner",
            "--uid-owner",
            "nous-browser",
            "-j",
            firewall.CHAIN,
        ] in (run.commands("iptables"))

    def test_ipv6_chain_only_rejects(self) -> None:
        run = FakeRunner()
        firewall.ensure_rules(PORT, "nous-browser", run)
        v6 = next(stdin for argv, stdin in run.calls if argv[0] == "ip6tables-restore")
        assert v6 is not None
        assert [line for line in v6.splitlines() if line.startswith("-A")] == [f"-A {firewall.CHAIN} -j REJECT"]

    def test_running_twice_adds_the_jump_once(self) -> None:
        run = FakeRunner()
        firewall.ensure_rules(PORT, "nous-browser", run)
        firewall.ensure_rules(PORT, "nous-browser", run)
        inserts = [a for a in run.commands("iptables") if "-I" in a]
        assert len(inserts) == 1  # the second run found it with -C
        restores = [s for a, s in run.calls if a[0] == "iptables-restore"]
        assert restores[0] == restores[1]  # the chain is rebuilt to the same content: a repair, not an append

    @pytest.mark.parametrize("broken", ["iptables-restore", "iptables"])
    def test_a_failing_command_fails_closed_naming_net_admin(self, broken: str) -> None:
        with pytest.raises(firewall.FirewallError, match=r"cap_add: \[NET_ADMIN\]"):
            firewall.ensure_rules(PORT, "nous-browser", FakeRunner(fail=(broken,)))

    def test_a_missing_iptables_fails_closed(self) -> None:
        with pytest.raises(firewall.FirewallError, match="NET_ADMIN"):
            firewall.ensure_rules(PORT, "nous-browser", lambda argv, stdin=None: (127, "not found"))

    def test_ipv6_failure_is_tolerated_only_without_ipv6(self, monkeypatch: pytest.MonkeyPatch) -> None:
        firewall.ensure_rules(PORT, "nous-browser", FakeRunner(fail=("ip6tables",)))
        monkeypatch.setattr(firewall, "_ipv6_in_use", lambda: True)
        with pytest.raises(firewall.FirewallError, match="IPv6"):
            firewall.ensure_rules(PORT, "nous-browser", FakeRunner(fail=("ip6tables",)))


class TestProbe:
    def test_a_passing_probe_is_cached_per_port(self) -> None:
        run = FakeRunner()
        firewall.probe(PORT, "nous-browser", run)
        firewall.probe(PORT, "nous-browser", run)
        assert len(run.commands(firewall.SETPRIV)) == 1

    def test_the_probe_runs_as_the_browser_user(self) -> None:
        run = FakeRunner()
        firewall.probe(PORT, "nous-browser", run)
        argv = run.commands(firewall.SETPRIV)[0]
        assert "--reuid=nous-browser" in argv and "--clear-groups" in argv

    @pytest.mark.parametrize(
        "code, why", [(3, "around the proxy"), (4, "cannot reach the egress proxy"), (1, "failed")]
    )
    def test_a_failing_probe_fails_closed_and_is_not_cached(self, code: int, why: str) -> None:
        with pytest.raises(firewall.FirewallError, match=why):
            firewall.probe(PORT, "nous-browser", FakeRunner(probe_code=code))
        assert PORT not in firewall._probed_ports

    def test_rules_in_place_but_probe_failing_still_refuses(self) -> None:
        with pytest.raises(firewall.FirewallError):
            firewall.ensure_egress_filter(PORT, "nous-browser", FakeRunner(probe_code=3))


def _write_proc(root: Path, pid: int, uid_line: str, state: str = "S (sleeping)") -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "status").write_text(f"Name:\tx\nState:\t{state}\n{uid_line}\n")
    (d / "cmdline").write_bytes(b"sleep\x001000\x00")  # no session marker


class TestKillUserProcesses:
    def test_kills_by_uid_with_no_marker_and_spares_the_rest(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(firewall, "_uid_of", lambda user: 998)
        _write_proc(tmp_path, 10, "Uid:\t998\t998\t998\t998")  # real and effective
        _write_proc(tmp_path, 11, "Uid:\t998\t0\t0\t0")  # real only
        _write_proc(tmp_path, 12, "Uid:\t0\t998\t998\t998")  # effective only
        _write_proc(tmp_path, 13, "Uid:\t0\t0\t0\t0")  # root: spared
        _write_proc(tmp_path, 14, "Uid:\t1000\t1000\t1000\t1000")  # someone else: spared
        _write_proc(tmp_path, 15, "Uid:\t998\t998\t998\t998", state="Z (zombie)")  # nothing left to kill
        (tmp_path / "self").mkdir()
        killed: list[int] = []

        def fake_kill(pid: int, sig: int) -> None:
            assert sig == firewall._SIGKILL
            killed.append(pid)
            (tmp_path / str(pid) / "status").unlink()  # it is gone on the next pass

        assert firewall.kill_user_processes("nous-browser", tmp_path, fake_kill) == 3
        assert sorted(killed) == [10, 11, 12]

    def test_an_unknown_user_or_root_kills_nothing(self, tmp_path: Path, monkeypatch) -> None:
        _write_proc(tmp_path, 10, "Uid:\t0\t0\t0\t0")
        for uid in (None, 0):
            monkeypatch.setattr(firewall, "_uid_of", lambda user, uid=uid: uid)
            assert firewall.kill_user_processes("nous-browser", tmp_path, lambda pid, sig: pytest.fail("killed")) == 0

    @pytest.mark.skipif(not hasattr(os, "getuid") or os.getuid() != 0, reason="needs root to run as another uid")
    def test_a_real_stray_process_of_the_user_dies(self, monkeypatch) -> None:
        import pwd

        try:
            uid = pwd.getpwnam("nobody").pw_uid
        except KeyError:
            pytest.skip("no nobody user")
        monkeypatch.setattr(firewall, "_uid_of", lambda user: uid)
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(1000)"],
            user=uid,
            start_new_session=True,
        )
        try:
            assert firewall.kill_user_processes("nobody") >= 1
            assert child.wait(timeout=5) == -firewall._SIGKILL
        finally:
            child.kill()


class TestSessionWiring:
    """The session calls the firewall on the wrapper path, and only there."""

    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple]:
        calls: list[tuple] = []
        monkeypatch.setattr(
            firewall, "ensure_egress_filter", lambda port, user, *a: calls.append(("ensure", port, user))
        )
        monkeypatch.setattr(firewall, "kill_user_processes", lambda user, *a: calls.append(("kill", user)) or 0)

        class Stop(Exception):
            pass

        class FakePlaywright:
            async def start(self):
                raise Stop

        monkeypatch.setattr("playwright.async_api.async_playwright", lambda: FakePlaywright(), raising=False)
        self.Stop = Stop
        return calls

    async def _enter(self, tmp_path: Path, **kw) -> None:
        session = BrowserSession(DomainAllowlist(["example.com"]), tmp_path / "a", **kw)
        with pytest.raises(self.Stop):
            await session.__aenter__()

    async def test_wrapper_path_kills_filters_and_kills_again(self, monkeypatch, calls, tmp_path: Path) -> None:
        monkeypatch.setattr(browser_session, "_browser_user_unavailable", lambda: None)
        await self._enter(tmp_path, proxy_port=PORT)
        assert calls == [("kill", "nous-browser"), ("ensure", PORT, "nous-browser"), ("kill", "nous-browser")]

    async def test_a_firewall_error_stops_the_start(self, monkeypatch, calls, tmp_path: Path) -> None:
        monkeypatch.setattr(browser_session, "_browser_user_unavailable", lambda: None)

        def refuse(port, user, *a):
            raise firewall.FirewallError("no NET_ADMIN")

        monkeypatch.setattr(firewall, "ensure_egress_filter", refuse)
        session = BrowserSession(DomainAllowlist(["example.com"]), tmp_path / "a")
        with pytest.raises(firewall.FirewallError):
            await session.__aenter__()
        assert session._browser is None and session._playwright is None  # Chromium was never started

    async def test_the_switch_skips_the_firewall_but_not_the_kill(self, monkeypatch, calls, tmp_path: Path) -> None:
        monkeypatch.setattr(browser_session, "_browser_user_unavailable", lambda: None)
        await self._enter(tmp_path, require_firewall=False)
        assert ("ensure", PORT, "nous-browser") not in calls and ("kill", "nous-browser") in calls

    async def test_non_wrapper_path_touches_neither(self, monkeypatch, calls, tmp_path: Path) -> None:
        monkeypatch.setattr(browser_session, "_browser_user_unavailable", lambda: "not root")
        await self._enter(tmp_path)
        assert calls == []


def test_compose_grants_net_admin_and_passes_the_firewall_settings() -> None:
    text = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert "    cap_add:\n      - NET_ADMIN\n" in text.replace("\r\n", "\n")
    for line in (
        "      - NOUS_BROWSER_TASK_PROXY_PORT=${NOUS_BROWSER_TASK_PROXY_PORT:-18181}",
        "      - NOUS_BROWSER_TASK_REQUIRE_EGRESS_FIREWALL=${NOUS_BROWSER_TASK_REQUIRE_EGRESS_FIREWALL:-true}",
    ):
        assert line in text


def test_settings_defaults() -> None:
    from nous.config import Settings

    s = Settings(_env_file=None)
    assert s.browser_task_proxy_port == 18181 and s.browser_task_require_egress_firewall is True
    with pytest.raises(ValueError):
        Settings(_env_file=None, browser_task_proxy_port=1024)
