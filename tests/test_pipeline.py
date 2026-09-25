from alora_evv.models import Authorization
from alora_evv.pipeline import (Skip, blocking_codes, compile_all, compile_visit,
                                derive_verification_error, write_report)
from tests.conftest import FakeAxisCare

import pytest


def by_id(rows, vid):
    return next(r for r in rows if r["Internal Visit ID"] == vid)


def test_status_filter_drops_approved(export_rows):
    assert "1000000006" not in {r["Internal Visit ID"] for r in export_rows}


def test_derive_verification_error():
    assert derive_verification_error("NON", "GPS") == "VSTR"
    assert derive_verification_error("GPS", "NON") == "VVER"
    assert derive_verification_error("NON", "NON") == "VVER"
    assert derive_verification_error("GPS", "GPS") is None


def test_blocking_codes():
    lines = ["VLOC CRITICAL Clock-in outside service area",
             "VTIM WARNING Late clock-in",
             "VSCH ERROR acceptable per payer rule",
             "CRITICAL something unreadable"]
    assert blocking_codes(lines) == {"VLOC", "UNREADABLE"}


def test_missing_clock_out_prepared(cfg, fake_ax, export_rows):
    p = compile_visit(by_id(export_rows, "1000000001"), [], cfg, fake_ax)
    assert p.error_type == "VVER"
    pl = p.payload
    assert pl["criticalError"] == "VVER"
    assert pl["reasonCode"] == "Reason Code 140"
    assert pl["reasonSubOption"] == "A. Failure to Clock In, Clock Out or Both"
    assert pl["serviceCode"] == "Personal Care - 5761"
    assert pl["programType"] == "AD"
    assert pl["providerName"] == "Alora Supports NE LLC"
    assert pl["caregiverNpi"] == "1234567893"
    assert pl["serviceAuth"] == "AUTH111"
    assert pl["dateOfService"] == "2026-09-21"
    assert pl["justification"].endswith(".")
    assert "authorization confirmed in AxisCare" in p.notes


def test_location_error_from_portal_dialog(cfg, fake_ax, export_rows):
    row = by_id(export_rows, "1000000002")
    p = compile_visit(row, ["VLOC CRITICAL Clock-in outside service area"], cfg, fake_ax)
    assert p.error_type == "VLOC"
    assert p.payload["criticalError"] == "VLOC"
    assert p.payload["reasonCode"] == "Reason Code 150"
    assert p.payload["providerName"].startswith("FIRST CHOICE")


def test_unknown_portal_code_held(cfg, fake_ax, export_rows):
    with pytest.raises(Skip) as e:
        compile_visit(by_id(export_rows, "1000000002"), ["VXYZ CRITICAL new thing"], cfg, fake_ax)
    assert e.value.needs_decision and "VXYZ" in e.value.reason


def test_authorization_mismatch_held_then_filed_as_auth(cfg, fake_ax, export_rows):
    row = by_id(export_rows, "1000000003")
    with pytest.raises(Skip) as e:
        compile_visit(row, [], cfg, fake_ax)
    assert "AUTH999" in e.value.reason and "AUTH333" in e.value.reason

    p = compile_visit(row, [], cfg, fake_ax, decision="AUTH")
    assert p.payload["serviceAuth"] == "AUTH333"
    assert p.payload["criticalError"] is None
    assert "AUTH333" in p.payload["justification"]
    assert p.payload["reasonCode"] == "Reason Code 120"


def test_missing_and_bad_npi_held(cfg, fake_ax, export_rows):
    with pytest.raises(Skip, match="no NPI"):
        compile_visit(by_id(export_rows, "1000000004"), [], cfg, fake_ax)
    fake_ax.npis["Test Caregiver"] = "1234567890"
    with pytest.raises(Skip, match="check digit"):
        compile_visit(by_id(export_rows, "1000000001"), [], cfg, fake_ax)


def test_ambiguous_code_needs_program(cfg, export_rows):
    row = dict(by_id(export_rows, "1000000001"), **{"Procedure Code": "7494"})
    ax = FakeAxisCare(npis={"Test Caregiver": "1234567893"})
    with pytest.raises(Skip, match="FSW"):
        compile_visit(row, [], cfg, ax)
    ax.auths[("11111111111", "7494")] = Authorization("AUTH111", program="FSW")
    with pytest.raises(Skip, match="no service-code dropdown"):
        compile_visit(row, [], cfg, ax)
    ax.auths[("11111111111", "7494")] = Authorization("AUTH111", program="DD")
    p = compile_visit(row, [], cfg, ax)
    assert p.payload["serviceCode"] == "Supported Family Living - 7494"


def test_compile_all_duplicates_submitted_and_report(cfg, fake_ax, export_rows, tmp_path):
    prepared, held = compile_all(export_rows, {}, cfg, fake_ax, decisions={},
                                 submitted={"1000000001": "2026-09-24T10:00:00"})
    held_ids = {h.visit_id: h.reason for h in held}
    assert "appears 2x" in held_ids["1000000005"]
    assert "already submitted" in held_ids["1000000001"]
    report = write_report(prepared, held, tmp_path / "r.txt").read_text()
    for secret in ("Fake", "Client", "11111111111", "22222222222"):
        assert secret not in report  # no client names or Medicaid IDs on disk
