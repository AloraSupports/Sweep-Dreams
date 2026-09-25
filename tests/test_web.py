"""Web app routes with sign-in turned off (local testing mode) and a fake sweep result."""
from datetime import datetime, timedelta

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from alora_evv.ledger import Ledger  # noqa: E402
from alora_evv.pipeline import compile_all  # noqa: E402
from alora_evv.sweep import SweepResult  # noqa: E402
from alora_evv.web import app as webapp  # noqa: E402
from alora_evv.web.app import RunState, clock, create_app  # noqa: E402


def make_state(cfg, fake_ax, export_rows, tmp_path, warnings=()):
    prepared, held = compile_all(export_rows, {}, cfg, fake_ax, {}, {})
    return RunState(result=SweepResult(5, prepared, held, tmp_path / "r.txt",
                                       datetime.now(), datetime.now(), list(warnings)))


@pytest.fixture
def client(cfg, fake_ax, export_rows, tmp_path):
    cfg.settings["web"]["auth"] = "none"
    state = make_state(cfg, fake_ax, export_rows, tmp_path)
    return TestClient(create_app(cfg, state)), state


def test_dashboard_and_review(client):
    c, state = client
    r = c.get("/")
    assert r.status_code == 200
    assert "ready to submit" in r.text and "1000000001" in r.text
    assert "Fake" not in r.text  # no client names on the list page

    v = c.get("/visit/1000000001")
    assert v.status_code == 200
    assert v.headers["cache-control"] == "no-store"
    assert "1234567893" in v.text and "Personal Care - 5761" in v.text
    assert "forms.monday.com" in v.text and "number_mkk9zzbc=1000000001" in v.text
    assert "11111111111" not in v.text.split("forms.monday.com")[1].split('"')[0]  # not in the link
    assert 'value="11111111111"' in v.text  # but shown to copy by hand

    r = c.post("/visit/1000000001/submitted", follow_redirects=True)
    assert "Submitted" in r.text and 'name="undo"' in r.text
    r = c.post("/visit/1000000001/submitted", data={"undo": "1"}, follow_redirects=True)
    assert 'href="/visit/1000000001"' in r.text
    assert c.get("/health").text == "ok"


def test_earlier_submission_does_not_hide_a_reprepared_visit(cfg, fake_ax, export_rows, tmp_path):
    cfg.settings["web"]["auth"] = "none"
    ledger = Ledger()
    ledger.mark_submitted("1000000001")
    ledger.db.execute("UPDATE visits SET submitted_at=? WHERE visit_id='1000000001'",
                      ((datetime.now() - timedelta(days=40)).isoformat(timespec="seconds"),))
    ledger.db.commit()
    c = TestClient(create_app(cfg, make_state(cfg, fake_ax, export_rows, tmp_path)))
    assert 'href="/visit/1000000001"' in c.get("/").text


def test_decisions_from_rules_and_feedback(client, cfg):
    c, _ = client
    home = c.get("/").text
    assert 'value="AUTH"' in home and 'value="KNOWN"' in home and "Clock-in (start)" in home
    r = c.post("/visit/1000000003/decide", data={"choice": "auth"}, follow_redirects=True)
    assert "handled as AUTH" in r.text
    assert Ledger().decisions() == {"1000000003": "AUTH"}
    assert "Saved: AUTH" in r.text and 'value="CLEAR"' in r.text
    r = c.post("/visit/1000000003/decide", data={"choice": "bogus"}, follow_redirects=True)
    assert "a choice this app knows" in r.text and Ledger().decisions() == {"1000000003": "AUTH"}
    r = c.post("/visit/1000000003/decide", data={"choice": "clear"}, follow_redirects=True)
    assert "Cleared" in r.text and Ledger().decisions() == {}


def test_unknown_visit_and_cross_site_posts_are_refused(client):
    c, _ = client
    r = c.post("/visit/9999999999/submitted", follow_redirects=True)
    assert "in the current run; nothing changed" in r.text and Ledger().recently_submitted(1) == {}
    r = c.post("/visit/1000000001/submitted", headers={"sec-fetch-site": "same-site"})
    assert r.status_code == 403 and Ledger().recently_submitted(1) == {}
    r = c.post("/sweep", headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_run_sweep_passes_options_and_shows_warnings(cfg, fake_ax, export_rows, tmp_path, monkeypatch):
    cfg.settings["web"]["auth"] = "none"
    calls = []

    def fake_collect(cfg_, *, limit, include_submitted):
        calls.append((limit, include_submitted))
        return make_state(cfg_, fake_ax, export_rows, tmp_path, warnings=["alora: 100+ rows"]).result

    monkeypatch.setattr(webapp, "collect", fake_collect)
    state = RunState()
    c = TestClient(create_app(cfg, state))
    c.post("/sweep", data={"limit": "2", "include_submitted": "1"})
    import time
    for _ in range(100):  # the sweep runs on its own thread
        if not state.running:
            break
        time.sleep(0.05)
    assert calls == [(2, True)] and state.error is None
    assert "100+ rows" in c.get("/").text


def test_clock_filter_is_portable():
    assert clock(datetime(2026, 9, 25, 8, 5)) == "8:05 AM"
    assert clock(datetime(2026, 9, 25, 20, 5), day=True) == "Friday 8:05 PM"
    assert clock(None) == ""


def test_google_mode_requires_login(cfg, monkeypatch):
    monkeypatch.setenv("ALORA_EVV_WEB_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("ALORA_EVV_GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("ALORA_EVV_GOOGLE_CLIENT_SECRET", "y")
    c = TestClient(create_app(cfg, RunState()))
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "Sign in with Google" in c.get("/login").text
    assert c.post("/sweep", follow_redirects=False).status_code == 303


def test_env_forces_google_and_blank_domain_is_refused(cfg, monkeypatch):
    monkeypatch.setenv("ALORA_EVV_WEB_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("ALORA_EVV_GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("ALORA_EVV_GOOGLE_CLIENT_SECRET", "y")
    cfg.settings["web"]["auth"] = "none"
    monkeypatch.setenv("ALORA_EVV_WEB_AUTH", "google")   # what the Docker image sets
    c = TestClient(create_app(cfg, RunState()))
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    cfg.settings["web"]["allowed_domain"] = ""
    with pytest.raises(RuntimeError, match="allowed_domain"):
        create_app(cfg, RunState())


def test_oauth_callback_checks_the_domain_and_survives_errors(cfg, monkeypatch):
    from authlib.integrations.base_client.errors import OAuthError
    from authlib.integrations.starlette_client import StarletteOAuth2App
    monkeypatch.setenv("ALORA_EVV_WEB_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("ALORA_EVV_GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("ALORA_EVV_GOOGLE_CLIENT_SECRET", "y")
    outcome = {}

    async def fake_token(self, request):
        if outcome.get("error"):
            raise OAuthError("mismatching_state")
        return {"userinfo": {"email": outcome["email"], "email_verified": True}}

    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", fake_token)
    c = TestClient(create_app(cfg, RunState()), base_url="https://testserver")
    outcome["error"] = True
    assert "Please try again" in c.get("/auth/callback").text
    outcome.clear()
    outcome["email"] = "someone@gmail.com"
    assert "Sign in with an @alorasupports.com account" in c.get("/auth/callback").text
    assert c.get("/", follow_redirects=False).status_code == 303
    outcome["email"] = "staff@alorasupports.com"
    r = c.get("/auth/callback", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    home = c.get("/")
    assert home.status_code == 200 and "staff@alorasupports.com" in home.text
