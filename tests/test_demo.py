"""Demo is deterministic, offline, and fast — the whole point."""


def test_demo_returns_zero_and_shows_verdicts(capsys):
    from mailbot.demo import run_demo, INBOX

    assert run_demo() == 0
    out = capsys.readouterr().out
    assert "Mailbot demo" in out
    for m in INBOX:
        assert m["verdict"] in out
    assert "setup.sh --fast" in out
