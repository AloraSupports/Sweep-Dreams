"""Web app routes with sign-in turned off (local testing mode) and a fake sweep result."""
from datetime import datetime

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from alora_evv.ledger import Ledger  # noqa: E402
from alora_evv.pipeline import compile_all  # noqa: E402
from alora_evv.sweep import SweepResult  # noqa: E402
from alora_evv.web.app import RunState, create_app  # noqa: E402


@pytest.fixture
def client(cfg, fake_ax, export_rows, tmp_path):
    cfg.settings["web"]["auth"] = "none"
    prepared, held = compile_all(export_rows, {}, cfg, fake_ax, {}, {})
    state = RunState(result=SweepResult(5, prepared, held, tmp_path / "r.txt",
                                        datetime.now(), datetime.now()))
    return TestClient(create_app(cfg, state)), prepared


def test_dashboard_and_review(client):
    c, prepared = client
    r = c.get("/")
    assert r.status_code == 200
    assert "ready to submit" in r.text and "1000000001" in r.text
    assert "Fake" not in r.text  # no client names on the list page

    v = c.get("/visit/1000000001")
    assert v.status_code == 200
    assert v.headers["cache-control"] == "no-store"
    assert "1234567893" in v.text and "Personal Care - 5761" in v.text
    assert "forms.monday.com" in v.text and "number_mkk9zzbc=1000000001" in v.text

    r = c.post("/visit/1000000001/submitted", follow_redirects=True)
    assert "Submitted" in r.text
    assert c.post("/visit/1000000003/decide", data={"choice": "auth"}, follow_redirects=False).status_code == 303
    assert Ledger().decisions() == {"1000000003": "AUTH"}
    assert c.get("/health").text == "ok"


def test_google_mode_requires_login(cfg, monkeypatch):
    monkeypatch.setenv("ALORA_EVV_WEB_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("ALORA_EVV_GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("ALORA_EVV_GOOGLE_CLIENT_SECRET", "y")
    c = TestClient(create_app(cfg, RunState()))
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "Sign in with Google" in c.get("/login").text
