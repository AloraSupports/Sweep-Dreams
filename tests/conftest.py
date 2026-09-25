import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Tests never touch the real data folder."""
    monkeypatch.setenv("ALORA_EVV_DATA", str(tmp_path / "data"))
    yield


@pytest.fixture
def cfg():
    from alora_evv.config import load_config
    return load_config(ROOT / "config")


class FakeAxisCare:
    configured = True

    def __init__(self, npis=None, auths=None):
        self.npis = npis or {}
        self.auths = auths or {}

    def caregiver_npi(self, first, last):
        return self.npis.get(f"{first} {last}")

    def authorization(self, medicaid_id, service_code, on):
        return self.auths.get((medicaid_id, str(service_code)))


@pytest.fixture
def fake_ax():
    from alora_evv.models import Authorization
    return FakeAxisCare(
        npis={"Test Caregiver": "1234567893", "Other Worker": "1111111112",
              "Nonpi Person": None},
        auths={("11111111111", "5761"): Authorization("AUTH111"),
               ("22222222222", "9510"): Authorization("AUTH222"),
               ("33333333333", "5761"): Authorization("AUTH333")})


@pytest.fixture
def export_rows(cfg):
    from alora_evv.pipeline import load_export
    return load_export(ROOT / "tests" / "fixtures" / "portal_export_fake.csv",
                       cfg.settings["queue_statuses"])
