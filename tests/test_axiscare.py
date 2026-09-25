from datetime import date

import pytest

from alora_evv.axiscare import AxisCareError, ExportBackend

CFG = {
    "caregivers_file": "cg.csv",
    "caregiver_columns": {"first_name": "First Name", "last_name": "Last Name", "npi": "NPI"},
    "authorizations_file": "auth.csv",
    "authorization_columns": {"client_medicaid_id": "Medicaid ID", "service_code": "Service Code",
                              "auth_number": "Authorization Number", "start_date": "Start Date",
                              "end_date": "End Date", "program": "Program"},
    "date_formats": ["%m/%d/%Y"],
}


def write(folder, cg, auth):
    (folder / "cg.csv").write_text(cg)
    (folder / "auth.csv").write_text(auth)


def test_export_lookups(tmp_path):
    write(tmp_path, "First Name,Last Name,NPI\nTest,Caregiver,1234567893\n",
          "Medicaid ID,Service Code,Authorization Number,Start Date,End Date\n"
          "11111111111,5761,OLD1,01/01/2026,06/30/2026\n"
          "11111111111,5761,NEW2,07/01/2026,12/31/2026\n")
    ax = ExportBackend(CFG, folder=tmp_path)
    assert ax.caregiver_npi("test", "CAREGIVER") == "1234567893"
    assert ax.authorization("11111111111", "5761", date(2026, 9, 21)).number == "NEW2"
    assert ax.authorization("11111111111", "5761", date(2027, 1, 5)) is None


def test_overlapping_auths_are_ambiguous(tmp_path):
    write(tmp_path, "First Name,Last Name,NPI\n",
          "Medicaid ID,Service Code,Authorization Number,Start Date,End Date\n"
          "1,5761,A,01/01/2026,12/31/2026\n1,5761,B,06/01/2026,12/31/2026\n")
    with pytest.raises(AxisCareError, match="more than one"):
        ExportBackend(CFG, folder=tmp_path).authorization("1", "5761", date(2026, 9, 1))


def test_wrong_columns_explained(tmp_path):
    write(tmp_path, "Name,Npi Number\n", "x\n")
    with pytest.raises(AxisCareError, match="Columns found"):
        ExportBackend(CFG, folder=tmp_path)


def test_unreadable_date_is_an_error_not_an_open_ended_auth(tmp_path):
    write(tmp_path, "First Name,Last Name,NPI\n",
          "Medicaid ID,Service Code,Authorization Number,Start Date,End Date\n"
          "1,5761,OLD,Sep 1 2025,Dec 31 2025\n")
    with pytest.raises(AxisCareError, match="date_formats"):
        ExportBackend(CFG, folder=tmp_path)
