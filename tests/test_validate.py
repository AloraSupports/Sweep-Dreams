from alora_evv.validate import npi_valid, parse_date


def test_npi_check_digit():
    assert npi_valid("1234567893")          # CMS's published example NPI
    assert not npi_valid("1234567890")      # right shape, wrong check digit
    assert not npi_valid("3234567893")      # must start with 1 or 2
    assert not npi_valid("123456789")
    assert not npi_valid("")


def test_parse_date_formats():
    fmts = ["%m/%d/%Y %I:%M %p", "%m/%d/%Y"]
    assert parse_date("09/21/2026 09:00 AM", fmts).day == 21
    assert parse_date("09/21/2026", fmts).month == 9
    assert parse_date("2026-09-21", fmts) is None
