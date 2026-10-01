"""Push-first supervision: no polling while IDLE is healthy."""
from __future__ import annotations

import time


class _Idle:
    def __init__(self, connected):
        self.connected = connected


def _run_one_cycle(s):
    """Run _main_loop until the first scan_fn call, then stop it."""
    calls = []
    orig_scan = s.scan_fn

    def scan_once():
        calls.append(1)
        s._stop.set()

    s.scan_fn = scan_once
    # Jobs + auth must not interfere: no providers means no scan at all, so
    # keep the provider and stub the clock instead.
    s._sleep_or_wake = lambda secs: True  # stop after the cycle
    s._run_due_jobs = lambda: None
    s._check_auth = lambda providers: None
    rc = s._main_loop(60.0)
    s.scan_fn = orig_scan
    return calls


def _sup(cfg, idle=None, last_scan=0.0, woke=False):
    from mailbot.agent.supervisor import Supervisor

    s = Supervisor(cfg, lambda: {"google": object()},
                   lambda: None, lambda: None, notify=None)
    s.idle = idle
    s.health.last_scan = last_scan
    s.woke = woke
    return s


def test_interval_scan_runs_without_push(cfg):
    s = _sup(cfg, idle=None, last_scan=0.0)
    assert _run_one_cycle(s) == [1]


def test_healthy_push_stands_down_interval_scan(cfg):
    s = _sup(cfg, idle=_Idle(True), last_scan=time.time())
    s._stop.set()  # would exit only via sleep; assert scan skipped first
    # No wake, fresh scan, healthy push: _main_loop must not call scan_fn.
    seen = []
    s.scan_fn = lambda: seen.append(1)
    s._sleep_or_wake = lambda secs: True
    s._run_due_jobs = lambda: None
    s._check_auth = lambda providers: None
    s._main_loop(60.0)
    assert seen == []


def test_wake_scans_despite_healthy_push(cfg):
    s = _sup(cfg, idle=_Idle(True), last_scan=time.time(), woke=True)
    assert _run_one_cycle(s) == [1]


def test_backstop_scans_on_stale_push(cfg):
    s = _sup(cfg, idle=_Idle(True), last_scan=time.time() - 3700)
    assert _run_one_cycle(s) == [1]


def test_dead_push_resumes_polling(cfg):
    s = _sup(cfg, idle=_Idle(False), last_scan=time.time())
    assert _run_one_cycle(s) == [1]


def test_health_reports_push_state(cfg, tmp_path, monkeypatch):
    import json

    from mailbot.agent.supervisor import Supervisor

    monkeypatch.setenv("MAIL_AGENT_STATE", str(tmp_path))
    s = Supervisor(cfg, lambda: {}, lambda: None, lambda: None, notify=None)
    s.idle = _Idle(True)
    s._write_health()
    snap = json.loads((tmp_path / "daemon.json").read_text())
    assert snap["idle_connected"] is True
