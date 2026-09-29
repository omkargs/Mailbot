"""Wizard: env import, defaults, doctor. No network, no TTY needed."""
from __future__ import annotations

import json
import os


def _fresh_config(monkeypatch, tmp_path):
    import mailbot.config as C

    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(C, "CONFIG_PATH", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(C, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(C, "DB_PATH", tmp_path / "data" / "agent.db")
    from mailbot import setup as S

    monkeypatch.setattr(S, "config_dir", lambda: tmp_path / "cfg")
    from mailbot import wizard as W

    monkeypatch.setattr(W, "config_dir", lambda: tmp_path / "cfg")
    return C, S, W


def test_import_env_writes_secrets_without_tty(monkeypatch, tmp_path):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    monkeypatch.setenv("ROUTER_API_KEY", "sk-test-123")
    monkeypatch.setenv("ROUTER_MODEL", "combo/claude2mail")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    written = W.import_env()
    assert "ROUTER_API_KEY" in written
    secrets = S.read_secrets()
    assert secrets["ROUTER_API_KEY"] == "sk-test-123"
    mode = (tmp_path / "cfg" / ".secrets").stat().st_mode & 0o777
    assert mode == 0o600


def test_import_env_accepts_inline_google_json(monkeypatch, tmp_path):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    blob = json.dumps({"installed": {"client_id": "x"}})
    monkeypatch.setenv("GOOGLE_CREDENTIALS", blob)
    W.import_env()
    dest = tmp_path / "cfg" / "google-credentials.json"
    assert dest.exists()
    assert json.loads(dest.read_text())["installed"]["client_id"] == "x"


def test_apply_defaults_fills_gaps_only(monkeypatch, tmp_path):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    monkeypatch.setenv("ROUTER_MODEL", "custom/model")
    for k in ("ROUTER_BASE_URL", "ROUTER_API_KEY", "ROUTER_MODEL",
              "GOOGLE_CREDENTIALS", "GOOGLE_ACCOUNT", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    W.apply_defaults()
    s = S.read_secrets()
    assert s["ROUTER_BASE_URL"] == "https://router.bynara.id"
    assert s["ROUTER_MODEL"] == "combo/claude2mail"
    assert s["AGENT_SEND_MODE"] == "auto"


def test_setup_non_interactive_fails_loudly_without_key(monkeypatch, tmp_path, capsys):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    for k in ("ROUTER_API_KEY", "ROUTER_BASE_URL", "ROUTER_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("MAIL_AGENT_CONFIG_DIR", raising=False)

    class Args:
        step = "provider"
        non_interactive = True
        yes = True
        import_env = False
        skip_voice = True
        skip_service = True

    rc = W.cmd_setup(Args(), C.load())
    assert rc == 1
    out = capsys.readouterr().out
    assert "ROUTER_API_KEY" in out


def test_doctor_reports_problems_not_ok(monkeypatch, tmp_path, capsys):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    for k in ("ROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)

    class Args:
        pass

    rc = W.cmd_doctor(Args(), C.load())
    assert rc == 1
    out = capsys.readouterr().out
    assert "doctor:" in out


def _setup_args(**kw):
    class Args:
        step = ""
        non_interactive = False
        yes = False
        fast = False
        dry_run = False
        import_env = False
        skip_voice = False
        skip_service = False

    for k, v in kw.items():
        setattr(Args, k, v)
    return Args()


def test_dry_run_writes_nothing(monkeypatch, tmp_path, capsys):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    rc = W.cmd_setup(_setup_args(dry_run=True), C.load())
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert not (tmp_path / "cfg" / ".secrets").exists()
    assert not (tmp_path / "cfg" / "config.json").exists()


def test_fast_skips_voice_service_chat(monkeypatch, tmp_path, capsys):
    C, S, W = _fresh_config(monkeypatch, tmp_path)
    for k in ("ROUTER_API_KEY", "ROUTER_BASE_URL", "ROUTER_MODEL"):
        monkeypatch.delenv(k, raising=False)
    rc = W.cmd_setup(_setup_args(fast=True), C.load())
    assert rc == 1  # missing key + creds: honest failure, not a crash
    out = capsys.readouterr().out
    assert "[1/" in out
    assert "Next:" in out  # one next action, not a state dump
    state = __import__("json").loads((tmp_path / "cfg" / ".setup-state.json").read_text())
    assert state.get("provider") == "missing-key"
    assert "voice" not in state  # skipped, not failed
    assert "service" not in state


def test_import_env_picks_up_triage_model(monkeypatch, tmp_path):
    from mailbot import wizard as W
    from mailbot import setup as S
    from mailbot import config as C

    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(S, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(W, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setenv("ROUTER_TRIAGE_MODEL", "cheap/triage-1")
    assert "ROUTER_TRIAGE_MODEL" in W.import_env()
    assert S.read_secrets()["ROUTER_TRIAGE_MODEL"] == "cheap/triage-1"


def test_looks_like_client_accepts_desktop_json():
    import json as _json
    from mailbot import wizard as W

    blob = _json.dumps({"installed": {"client_id": "abc.apps.googleusercontent.com",
                                      "client_secret": "s"}})
    assert W._looks_like_client(blob) is True
    assert W._looks_like_client('{"foo": 1}') is False
    assert W._looks_like_client("not json {{{") is False
    assert W._looks_like_client('["installed"]') is False


def test_collect_credentials_json_pastes_and_chmods(monkeypatch, tmp_path):
    import json as _json
    from mailbot import wizard as W

    dest = tmp_path / "sub" / "google-credentials.json"
    blob = _json.dumps({"web": {"client_id": "abc", "client_secret": "s"}})
    inputs = iter(blob.split("\n") + [""])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    assert W.collect_credentials_json(dest) is True
    assert _json.loads(dest.read_text())["web"]["client_id"] == "abc"
    assert (dest.stat().st_mode & 0o777) == 0o600


def test_collect_credentials_json_rejects_garbage(monkeypatch, tmp_path, capsys):
    from mailbot import wizard as W

    dest = tmp_path / "google-credentials.json"
    inputs = iter(["hello world", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    assert W.collect_credentials_json(dest) is False
    assert not dest.exists()
    assert "nothing written" in capsys.readouterr().out


def test_provider_base_for_maps_all_choices():
    from mailbot import wizard as W

    assert W.provider_base_for(W.PROVIDER_CHOICES[0][0]) == "https://router.bynara.id"
    assert W.provider_base_for(W.PROVIDER_CHOICES[1][0]) == "https://api.anthropic.com"
    assert W.provider_base_for(W.PROVIDER_CHOICES[2][0]) == "https://openrouter.ai/api/anthropic"
    assert W.provider_base_for(W.PROVIDER_CHOICES[3][0]) == "http://localhost:4000"
    assert W.provider_base_for(W.PROVIDER_CHOICES[4][0]) is None
    assert W.provider_base_for("nonsense") == "https://router.bynara.id"


def test_openrouter_models_ranked():
    from mailbot.agent import discovery as D

    ranked = D.rank(["zzz-model", "anthropic/claude-sonnet-4", "aaa-model"])
    assert ranked[0] == "anthropic/claude-sonnet-4"


def test_resume_line_shows_prior_state(monkeypatch, tmp_path, capsys):
    import json as _json
    from mailbot import wizard as W
    from mailbot import setup as S
    from mailbot import config as C

    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(S, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(W, "config_dir", lambda: tmp_path / "cfg")
    (tmp_path / "cfg").mkdir(parents=True)
    (tmp_path / "cfg" / ".setup-state.json").write_text(
        _json.dumps({"provider": "ok"}))

    class Args:
        step = ""
        non_interactive = False
        yes = False
        fast = False
        dry_run = True
        import_env = False
        skip_voice = False
        skip_service = False

    assert W.cmd_setup(Args(), C.load()) == 0
    assert "Resuming" in capsys.readouterr().out


def test_loopback_gives_up_on_time():
    """The callback wait must be a guillotine, not a lifetime."""
    import time
    from mailbot import wizard as W

    port = W._free_port()
    t0 = time.time()
    try:
        W._loopback_code(port, timeout=1)
        assert False, "should have raised"
    except TimeoutError:
        pass
    assert time.time() - t0 < 10


def test_loopback_captures_the_code():
    import threading
    import time
    import urllib.request
    from mailbot import wizard as W

    port = W._free_port()
    out = {}

    def _go():
        time.sleep(0.4)
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/?code=abc123&state=x", timeout=3).read()
        except Exception:
            pass
        out["done"] = True

    threading.Thread(target=_go, daemon=True).start()
    assert W._loopback_code(port, timeout=8) == "abc123"


def test_consent_url_has_a_live_redirect():
    """The OOB flow was deleted by Google in 2022.

    Regression: the old headless path asked for
    redirect_uri=urn:ietf:wg:oauth:2.0:oob, so it could never complete -
    and it failed late, after the user had done all the Google console
    work. Also guard the SDK trap the reviewer hit: an InstalledAppFlow
    with http://localhost emits a URL with no redirect_uri at all.
    """
    from urllib.parse import parse_qs, urlparse
    from mailbot import wizard as W

    url = W._consent_url("cid.apps.googleusercontent.com", ["a", "b"])
    q = parse_qs(urlparse(url).query)
    assert "oob" not in q["redirect_uri"][0]
    assert q["redirect_uri"][0].startswith("http://localhost")
    assert q["client_id"] == ["cid.apps.googleusercontent.com"]
    assert q["response_type"] == ["code"]
    assert q["access_type"] == ["offline"]      # else no refresh token
    assert q["prompt"] == ["consent"]


def test_paste_accepts_a_code_or_a_whole_url():
    from mailbot import wizard as W

    assert W._code_from_paste("4/abc") == "4/abc"
    assert W._code_from_paste(
        "http://localhost:8080/?state=s&code=4/abc&scope=x") == "4/abc"
    assert W._code_from_paste("  4/xyz ") == "4/xyz"
    assert W._code_from_paste("") == ""


def test_google_client_reads_installed_and_web():
    import json as _json
    from mailbot import wizard as W

    for kind in ("installed", "web"):
        p = W.config_dir() / f"{kind}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps({kind: {"client_id": "cid-" + kind,
                                        "client_secret": "sec-" + kind}}))
        assert W._google_client(p) == ("cid-" + kind, "sec-" + kind)


def test_token_file_matches_what_google_auth_reads(tmp_path):
    """Credentials.from_authorized_user_file is picky about the shape."""
    import json as _json
    from google.oauth2.credentials import Credentials
    from mailbot import wizard as W
    from mailbot.providers.gmail import SCOPES

    out = tmp_path / "tok.json"
    W._write_google_token(out, {"refresh_token": "r1", "access_token": "a1"},
                          "cid", "sec", SCOPES)
    assert oct(out.stat().st_mode)[-3:] == "600"
    creds = Credentials.from_authorized_user_file(str(out), SCOPES)
    assert creds.refresh_token == "r1"
    assert creds.token == "a1"


def test_invalid_grant_does_not_look_like_a_generic_failure(monkeypatch, capsys):
    """Auth codes are single-use. Say so, or people paste the dead one again."""
    from mailbot import wizard as W

    monkeypatch.setattr(W, "_exchange_code",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("invalid_grant: code expired")))
    monkeypatch.setattr(W, "_loopback_code", lambda *a, **k: "")
    monkeypatch.setattr(W, "_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "4/dead")

    p = W.config_dir() / "c.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    import json as _json
    p.write_text(_json.dumps({"installed": {"client_id": "c", "client_secret": "s"}}))

    assert W.headless_google_auth(p) is False
    out = capsys.readouterr().out
    assert "single-use" in out
    assert "approve the consent screen again" in out


def test_key_help_points_at_each_provider():
    from mailbot import wizard as W

    assert "openrouter.ai/keys" in W._key_help("https://openrouter.ai/api/anthropic")
    assert "console.anthropic.com" in W._key_help("https://api.anthropic.com")
    assert "LiteLLM" in W._key_help("http://localhost:4000")
    assert "Bynara" in W._key_help("https://router.bynara.id")


def test_missing_key_says_what_and_where(monkeypatch, tmp_path, capsys):
    import json as _json
    from mailbot import wizard as W
    from mailbot import setup as S
    from mailbot import config as C

    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(S, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(W, "config_dir", lambda: tmp_path / "cfg")
    for k in ("ROUTER_API_KEY", "ROUTER_BASE_URL", "ROUTER_MODEL"):
        monkeypatch.delenv(k, raising=False)

    class Args:
        step = "provider"
        non_interactive = True
        yes = True
        fast = False
        dry_run = False
        import_env = False
        skip_voice = True
        skip_service = True

    assert W.cmd_setup(Args(), C.load()) == 1
    out = capsys.readouterr().out
    assert "password your AI provider" in out
    assert "export ROUTER_API_KEY=" in out
    assert "Next:" in out


def test_labelled_maps_back_to_ids():
    from mailbot import wizard as W

    labels, back = W._labelled(["aaa-model", "cheap/x"],
                               {"cheap/x": (0.15, 0.47)})
    assert labels[0] == "aaa-model"
    assert labels[1] == "cheap/x  ($0.15/$0.47/M)"
    assert back[labels[1]] == "cheap/x"


def test_list_pricing_parses_openrouter_shape(monkeypatch):
    import io
    import json as _json
    import urllib.request
    from mailbot.agent import discovery as D

    payload = _json.dumps({"data": [
        {"id": "cheap/x", "pricing": {"prompt": "0.00000015", "completion": "0.00000047"}},
        {"id": "no-price", "pricing": {}},
    ]}).encode()

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return payload

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResp())
    out = D.list_pricing("https://openrouter.ai/api/anthropic", "k")
    assert out["cheap/x"] == (0.15, 0.47)
    assert "no-price" not in out


def _setup_args(step="provider", **kw):
    class Args:
        pass
    a = Args()
    a.step = step
    a.non_interactive = False
    a.yes = False
    a.fast = False
    a.dry_run = False
    a.import_env = False
    a.skip_voice = True
    a.skip_service = True
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _wire(monkeypatch, tmp_path, probe_ok):
    """Point the wizard at a scratch config and stub the network probe."""
    from mailbot import wizard as W, setup as S, config as C
    d = tmp_path / "cfg"
    monkeypatch.setattr(C, "CONFIG_DIR", d)
    monkeypatch.setattr(S, "config_dir", lambda: d)
    monkeypatch.setattr(W, "config_dir", lambda: d)
    monkeypatch.setattr(W, "_tty", lambda: True)
    for k in ("ROUTER_API_KEY", "ROUTER_BASE_URL", "ROUTER_MODEL",
              "ROUTER_TRIAGE_MODEL"):
        monkeypatch.delenv(k, raising=False)
    # The wizard reads the secrets file, not the environment, so seed that.
    S.write_secret("ROUTER_API_KEY", "sk-test")
    S.write_secret("ROUTER_BASE_URL", "https://example.test")
    S.write_secret("ROUTER_MODEL", "m1")
    from mailbot.agent import discovery as D
    calls = []

    def fake_probe(base, key, model, timeout=30.0):
        calls.append((base, key, model))
        return {"ok": probe_ok, "model": model, "said": "OK", "error": ""}

    monkeypatch.setattr(D, "probe", fake_probe)
    monkeypatch.setattr(D, "list_models", lambda *a, **k: ["m1", "m2"])
    monkeypatch.setattr(D, "list_pricing", lambda *a, **k: {})
    # A full run walks on into Google. Stub it so these tests are about the
    # provider decision and nothing else.
    monkeypatch.setattr(W, "collect_credentials_json", lambda p: True)
    monkeypatch.setattr(W, "headless_google_auth", lambda *a, **k: True)
    monkeypatch.setattr(W, "_test_notify", lambda which: True)
    # getpass cannot read a terminal under pytest; blank means "give up",
    # which is the safe default for tests that are not about the key.
    import getpass
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "")
    return calls


def test_working_provider_is_not_asked_again(monkeypatch, tmp_path, capsys):
    """A provider that already works must not open a menu every run.

    The user asked for this repeatedly. A prompt with no decision in it
    trains people to press Enter without reading, which is the last thing
    a tool that sends mail for you should do.
    """
    import mailbot.menu as M
    from mailbot import wizard as W, config as C

    opened = []
    monkeypatch.setattr(M, "choose", lambda *a, **k: opened.append(a) or None)
    _wire(monkeypatch, tmp_path, probe_ok=True)

    W.cmd_setup(_setup_args(step=None), C.load())
    out = capsys.readouterr().out
    assert not any("brain run" in a[0] for a in opened), \
        f"provider menu opened even though it already works: {opened}"
    assert "working" in out
    assert "setup --step provider" in out      # but there is a way to change it


def test_broken_provider_still_asks(monkeypatch, tmp_path, capsys):
    """If the key is wrong there IS a decision, so the menu must appear."""
    import mailbot.menu as M
    from mailbot import wizard as W, config as C

    opened = []
    monkeypatch.setattr(M, "choose", lambda *a, **k: opened.append(a) or "Keep current")
    _wire(monkeypatch, tmp_path, probe_ok=False)

    W.cmd_setup(_setup_args(step=None), C.load())
    out = capsys.readouterr().out
    prompts = [a[0] for a in opened]
    assert "Where should the brain run?" in prompts, prompts


def test_set_triage_model_is_not_re_asked(monkeypatch, tmp_path, capsys):
    """Once triage is decided, stop asking — same rule as the provider."""
    import mailbot.menu as M
    from mailbot import wizard as W, config as C

    asked = []
    real_choose = M.choose

    def spy(prompt, options, **k):
        asked.append(prompt)
        return "Keep current" if "brain run" in prompt else options[0]

    monkeypatch.setattr(M, "choose", spy)
    from mailbot import setup as S
    _wire(monkeypatch, tmp_path, probe_ok=True)
    S.write_secret("ROUTER_TRIAGE_MODEL", "m2")

    W.cmd_setup(_setup_args(step=None), C.load())
    capsys.readouterr()
    assert not any("triage" in p for p in asked), asked


def test_explicit_step_provider_always_offers_the_picker(monkeypatch, tmp_path):
    """Asking for the provider step is asking to change it — honour that."""
    import mailbot.menu as M
    from mailbot import wizard as W, config as C

    opened = []
    monkeypatch.setattr(M, "choose", lambda *a, **k: opened.append(a) or "Keep current")
    _wire(monkeypatch, tmp_path, probe_ok=True)

    W.cmd_setup(_setup_args(step="provider"), C.load())
    assert opened, "--step provider must show the picker even when it works"


def test_rejected_key_offers_a_replacement(monkeypatch, tmp_path, capsys):
    """A 401 must be fixable in the wizard, not a dead end.

    This is the reported bug: the key was wrong, the wizard printed
    "provider FAILED: ... 401" and moved on, and there was nowhere to put
    a working key short of hand-editing the secrets file.
    """
    import getpass
    from mailbot import wizard as W, setup as S, config as C
    from mailbot.agent import discovery as D

    _wire(monkeypatch, tmp_path, probe_ok=False)

    asked = []

    def fake_probe(base, key, model, timeout=30.0):
        asked.append(key)
        # The pre-flight probe with the bad key fails; the retry with the
        # new key succeeds. The pre-flight result must be reused, not
        # re-probed, so exactly two calls happen.
        good = key == "sk-good"
        return {"ok": good, "model": model, "said": "OK" if good else "",
                "error": "" if good else
                "AuthenticationError: 401 {'message': 'A valid API key is required.'}"}

    monkeypatch.setattr(D, "probe", fake_probe)
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "sk-good")

    W.cmd_setup(_setup_args(step=None), C.load())
    out = capsys.readouterr().out
    assert "sk-good" in asked, "the new key was never tried"
    assert "That key was rejected." in out
    assert "provider OK" in out
    # And it was actually persisted.
    assert S.read_secrets()["ROUTER_API_KEY"] == "sk-good"


def test_blank_key_gives_up_without_looping(monkeypatch, tmp_path, capsys):
    """Blank input must stop, not spin asking forever."""
    import getpass
    from mailbot import wizard as W, config as C
    from mailbot.agent import discovery as D

    _wire(monkeypatch, tmp_path, probe_ok=False)
    n = []
    monkeypatch.setattr(D, "probe", lambda *a, **k: (
        n.append(1), {"ok": False, "model": "m1", "said": "",
                      "error": "401 unauthorized"})[1])
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "")

    W.cmd_setup(_setup_args(step=None), C.load())
    out = capsys.readouterr().out
    assert len(n) == 1, (f"probed {len(n)} times; the pre-flight result must be "
                        "reused and blank must stop immediately")
    assert "mail-agent setup --step provider" in out
