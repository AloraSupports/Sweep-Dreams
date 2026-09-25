"""Plain data shapes passed between steps."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Authorization:
    number: str
    start: date | None = None
    end: date | None = None
    program: str | None = None


@dataclass
class Prepared:
    """A visit ready to review. `payload` holds PHI in memory only — never written to disk."""
    visit_id: str
    provider_key: str
    service_date: date
    error_type: str
    reason_code: str | None
    caregiver: str
    payload: dict
    notes: list[str] = field(default_factory=list)


@dataclass
class Held:
    """A visit the app would not prepare, with the reason a person needs to act on."""
    visit_id: str
    reason: str
    caregiver: str = ""
    provider_key: str = ""
    needs_decision: bool = False

    def line(self) -> str:
        who = f" [{self.caregiver}]" if self.caregiver else ""
        tip = "  -> decide with: alora-evv decide {} <choice>".format(self.visit_id) if self.needs_decision else ""
        return f"visit {self.visit_id}{who}: {self.reason}{tip}"
