"""Field checks that catch bad data before it reaches a state form."""
from __future__ import annotations

import re
from datetime import datetime


def npi_valid(npi: str) -> bool:
    """10 digits, starts with 1 or 2, and passes the NPI check digit (Luhn with prefix 80840).

    The check digit catches most typos, which a plain 10-digit check does not.
    """
    npi = (npi or "").strip()
    if not re.fullmatch(r"[12]\d{9}", npi):
        return False
    digits = [int(c) for c in "80840" + npi[:9]]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:  # rightmost payload digit is doubled (check digit is appended after it)
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(npi[9])


def matches(pattern: str, value: str) -> bool:
    return bool(re.fullmatch(pattern, (value or "").strip()))


def parse_date(value: str, formats: list[str]) -> datetime | None:
    value = (value or "").strip()
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def norm_name(value: str) -> str:
    return re.sub(r"[^A-Z]", "", (value or "").upper())
