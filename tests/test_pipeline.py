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


# --- decisions, limits and dialogs ---------------------------------------------

def test_known_decision_leaves_choices_to_the_signer(cfg, fake_ax, export_rows):
    from alora_evv.form import prefill_params
    p = compile_visit(by_id(export_rows, "1000000001"), [], cfg, fake_ax, decision="KNOWN")
    assert p.payload["criticalError"] is None and p.payload["reasonCode"] is None
    assert p.payload["justification"] == ""
    sent = {col for col, _ in prefill_params(p.payload, cfg.form).values()}
    assert "label__1" not in sent and "single_select_mkk96g2w" not in sent


def test_auth_decision_without_axiscare_auth_stays_decidable(cfg, export_rows):
    ax = FakeAxisCare(npis={"Test Caregiver": "1234567893"})
    with pytest.raises(Skip) as e:
        compile_visit(by_id(export_rows, "1000000001"), [], cfg, ax, decision="AUTH")
    assert e.value.needs_decision and "change the decision" in e.value.reason


def test_non_auth_decision_does_not_hide_a_mismatch(cfg, fake_ax, export_rows):
    with pytest.raises(Skip) as e:
        compile_visit(by_id(export_rows, "1000000003"), [], cfg, fake_ax, decision="KNOWN")
    assert e.value.needs_decision and "decision KNOWN" in e.value.reason


def test_limit_and_skip_on_duplicates(cfg, fake_ax, export_rows):
    prepared, held = compile_all(export_rows, {}, cfg, fake_ax, {}, {}, limit=1)
    assert len(prepared) == 1
    assert any("--max limit" in h.reason for h in held)

    _, held = compile_all(export_rows, {}, cfg, fake_ax, {"1000000005": "SKIP"}, {})
    reasons = {h.visit_id: h for h in held}
    assert reasons["1000000005"].reason == "skipped by operator decision"

    _, held = compile_all(export_rows, {}, cfg, fake_ax, {"1000000005": "VVER"}, {})
    reasons = {h.visit_id: h for h in held}
    assert "split visits aren't supported" in reasons["1000000005"].reason
    assert reasons["1000000005"].needs_decision


def test_non_critical_rows_do_not_block_and_known_code_wins():
    assert blocking_codes(["VTIM Non-Critical Late clock-in", "VVER Critical Missing clock out"]) == {"VVER"}
    assert blocking_codes(["ABC VLOC CRITICAL something"], known={"VLOC"}) == {"VLOC"}
    assert blocking_codes(["ABC VLOC CRITICAL something"]) == {"ABC"}


def test_unread_dialog_is_not_no_errors(cfg, fake_ax, export_rows):
    from alora_evv.pipeline import UNREAD_NOTE
    p = compile_visit(by_id(export_rows, "1000000001"), None, cfg, fake_ax)
    assert UNREAD_NOTE in p.notes
    assert UNREAD_NOTE not in compile_visit(by_id(export_rows, "1000000001"), [], cfg, fake_ax).notes
    with pytest.raises(Skip, match="wasn't read"):
        compile_visit(by_id(export_rows, "1000000002"), None, cfg, fake_ax)
    # compile_all treats a visit missing from claim_errors as unread
    prepared, _ = compile_all(export_rows, {"1000000001": []}, cfg, fake_ax, {}, {})
    notes = {p.visit_id: p.notes for p in prepared}
    assert UNREAD_NOTE not in notes["1000000001"]


def test_bad_justification_placeholder_holds_instead_of_crashing(cfg, fake_ax, export_rows):
    cfg.rules["error_types"]["VVER"]["justification"] = "Visit for {client} on {service_date}"
    with pytest.raises(Skip, match="bad placeholder"):
        compile_visit(by_id(export_rows, "1000000001"), [], cfg, fake_ax)


def test_manual_override_auth(cfg, fake_ax, export_rows):
    row = dict(by_id(export_rows, "1000000001"), **{"Manual Override Auth No": "AUTH111"})
    assert compile_visit(row, [], cfg, fake_ax).payload["serviceAuth"] == "AUTH111"
    row["Manual Override Auth No"] = "OTHER1"
    with pytest.raises(Skip, match="Manual Override") as e:
        compile_visit(row, [], cfg, fake_ax)
    assert e.value.needs_decision
    row["Authorization Number"] = ""
    row["Manual Override Auth No"] = "AUTH111"
    assert compile_visit(row, [], cfg, fake_ax).payload["serviceAuth"] == "AUTH111"


def test_recipient_name_is_not_recased(cfg, fake_ax, export_rows):
    row = dict(by_id(export_rows, "1000000001"), **{"Recipient Name": "FAKE", "Recipient Last Name": "MCDONALD"})
    assert compile_visit(row, [], cfg, fake_ax).payload["recipientName"] == "FAKE MCDONALD"


# --- export file -----------------------------------------------------------------

def test_load_export_tolerates_extra_fields_and_explains_missing_columns(tmp_path, cfg):
    from alora_evv.pipeline import ExportError, load_export
    from tests.conftest import ROOT
    src = (ROOT / "tests" / "fixtures" / "portal_export_fake.csv").read_text()
    lines = src.splitlines()
    lines[1] += ",surplus,fields"
    (tmp_path / "e.csv").write_text("\n".join(lines) + "\n")
    rows = load_export(tmp_path / "e.csv", cfg.settings["queue_statuses"])
    assert rows[0]["Internal Visit ID"] == "1000000001"

    (tmp_path / "bad.csv").write_text(src.replace("Status", "Claim Status"))
    with pytest.raises(ExportError, match="Columns found"):
        load_export(tmp_path / "bad.csv", cfg.settings["queue_statuses"])


def test_unquoted_numeric_keys_are_normalised():
    from alora_evv.config import _string_keys
    rules = _string_keys({"programs": {"AD": {"services": {5761: "Personal Care - 5761"}}},
                          "ambiguous_codes": {7494: "x"}, "reason_codes": {140: {"label": "R"}}})
    assert list(rules["programs"]["AD"]["services"]) == ["5761"]
    assert list(rules["ambiguous_codes"]) == ["7494"] and list(rules["reason_codes"]) == ["140"]


# --- prefilled link ---------------------------------------------------------------

def test_prefill_keeps_client_details_out_of_the_url(cfg, fake_ax, export_rows):
    from alora_evv.form import prefill_params, prefill_url
    p = compile_visit(by_id(export_rows, "1000000001"), [], cfg, fake_ax)
    url = prefill_url(p.payload, cfg.form)
    assert "Fake" not in url and "11111111111" not in url and "number_mkk9zzbc=1000000001" in url
    assert "11111111111" in prefill_url(p.payload, cfg.form, include_phi=True)
    # the follow-up answer goes to the reason's own sub_group column
    assert prefill_params(p.payload, cfg.form)["reasonSubOption"][0] == "single_select_mkm8p2yf"
    cfg.rules["reason_codes"]["140"]["sub_group"] = "single_select_other"
    p2 = compile_visit(by_id(export_rows, "1000000001"), [], cfg, fake_ax)
    assert prefill_params(p2.payload, cfg.form)["reasonSubOption"][0] == "single_select_other"
