"""Local record of what's been prepared and submitted, plus operator decisions.

Stores visit IDs, codes and dates only — no client names or Medicaid IDs.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .paths import data_dir, make_private

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (
    visit_id      TEXT PRIMARY KEY,
    provider      TEXT,
    service_date  TEXT,
    error_type    TEXT,
    reason_code   TEXT,
    caregiver     TEXT,
    prepared_at   TEXT,
    submitted_at  TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    visit_id    TEXT PRIMARY KEY,
    choice      TEXT NOT NULL,
    note        TEXT,
    decided_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "ledger.sqlite3"
        new = not self.path.exists()
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if new:
            make_private(self.path)

    # -- decisions -----------------------------------------------------------
    def decisions(self) -> dict[str, str]:
        return {r["visit_id"]: r["choice"] for r in self.db.execute("SELECT * FROM decisions")}

    def decide(self, visit_id: str, choice: str, note: str = "") -> None:
        self.db.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?,?)",
                        (visit_id, choice, note, _now()))
        self.db.commit()

    def clear_decision(self, visit_id: str) -> None:
        self.db.execute("DELETE FROM decisions WHERE visit_id=?", (visit_id,))
        self.db.commit()

    # -- visits --------------------------------------------------------------
    def record_prepared(self, p) -> None:
        self.db.execute(
            """INSERT INTO visits (visit_id, provider, service_date, error_type, reason_code,
                                   caregiver, prepared_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(visit_id) DO UPDATE SET provider=excluded.provider,
                 service_date=excluded.service_date, error_type=excluded.error_type,
                 reason_code=excluded.reason_code, caregiver=excluded.caregiver,
                 prepared_at=excluded.prepared_at""",
            (p.visit_id, p.provider_key, p.service_date.isoformat(), p.error_type,
             p.reason_code, p.caregiver, _now()))
        self.db.commit()

    def mark_submitted(self, visit_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO visits (visit_id, prepared_at) VALUES (?, ?)",
                        (visit_id, _now()))
        self.db.execute("UPDATE visits SET submitted_at=? WHERE visit_id=?", (_now(), visit_id))
        self.db.commit()

    def unmark_submitted(self, visit_id: str) -> None:
        self.db.execute("UPDATE visits SET submitted_at=NULL WHERE visit_id=?", (visit_id,))
        self.db.commit()

    def recently_submitted(self, days: int) -> dict[str, str]:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        rows = self.db.execute(
            "SELECT visit_id, submitted_at FROM visits WHERE submitted_at >= ?", (cutoff,))
        return {r["visit_id"]: r["submitted_at"] for r in rows}

    def recent(self, limit: int = 30) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM visits ORDER BY COALESCE(submitted_at, prepared_at) DESC LIMIT ?",
            (limit,)))
