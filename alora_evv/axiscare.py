"""AxisCare lookups: caregiver NPI and the authorization on file for a visit.

This replaces the Google tracking sheet. Two backends share one interface:

  ExportBackend — reads AxisCare report exports (CSV) saved in the app's data
                  folder. Works today without API access.
  ApiBackend    — placeholder until AxisCare provides API access and docs.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from .models import Authorization
from .paths import sub
from .validate import norm_name, parse_date


class AxisCareError(RuntimeError):
    pass


class AxisCare:
    """Interface. Return None when there's no match; raise AxisCareError when ambiguous."""

    configured: bool = False

    def caregiver_npi(self, first: str, last: str) -> str | None:
        raise NotImplementedError

    def authorization(self, medicaid_id: str, service_code: str, on: date) -> Authorization | None:
        raise NotImplementedError


class NullBackend(AxisCare):
    configured = False

    def caregiver_npi(self, first, last):
        return None

    def authorization(self, medicaid_id, service_code, on):
        return None


class ExportBackend(AxisCare):
    configured = True

    def __init__(self, cfg: dict, folder: Path | None = None):
        self.cfg = cfg
        self.folder = folder or sub("axiscare")
        self.formats = cfg.get("date_formats", ["%m/%d/%Y", "%Y-%m-%d"])
        self._npi: dict[str, list[str]] = {}
        self._auths: list[dict] = []
        self._load()

    def _read(self, filename: str, columns: dict, optional: set[str]) -> list[dict]:
        path = self.folder / filename
        if not path.exists():
            raise AxisCareError(
                f"AxisCare export not found: {path}\n"
                f"Download the report from AxisCare and save it there with that name.")
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            header = [h.strip() for h in (reader.fieldnames or [])]
            missing = [v for k, v in columns.items() if k not in optional and v not in header]
            if missing:
                raise AxisCareError(
                    f"{filename} is missing columns {missing}.\n"
                    f"Columns found: {header}\n"
                    f"Update the column names under axiscare.export in settings.yaml.")
            rows = []
            for raw in reader:
                raw = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
                rows.append({key: raw.get(col, "") for key, col in columns.items()})
            return rows

    def _load(self) -> None:
        cg = self._read(self.cfg["caregivers_file"], self.cfg["caregiver_columns"], set())
        for r in cg:
            key = norm_name(r["first_name"]) + "|" + norm_name(r["last_name"])
            if r["npi"]:
                self._npi.setdefault(key, [])
                if r["npi"] not in self._npi[key]:
                    self._npi[key].append(r["npi"])
        self._auths = self._read(self.cfg["authorizations_file"],
                                 self.cfg["authorization_columns"], {"program"})
        for n, r in enumerate(self._auths, start=2):  # row 1 is the header
            for col in ("start_date", "end_date"):
                if r[col] and not parse_date(r[col], self.formats):
                    raise AxisCareError(
                        f"{self.cfg['authorizations_file']} row {n}: can't read the date "
                        f"'{r[col]}' — add its format to axiscare.export.date_formats in "
                        "settings.yaml. (An unreadable date would otherwise make that "
                        "authorization look valid forever.)")

    def caregiver_npi(self, first: str, last: str) -> str | None:
        found = self._npi.get(norm_name(first) + "|" + norm_name(last), [])
        if len(found) > 1:
            raise AxisCareError(f"more than one NPI in AxisCare for {first} {last}")
        return found[0] if found else None

    def authorization(self, medicaid_id: str, service_code: str, on: date) -> Authorization | None:
        hits = []
        for r in self._auths:
            if r["client_medicaid_id"] != medicaid_id or r["service_code"].strip() != str(service_code):
                continue
            start = parse_date(r["start_date"], self.formats)
            end = parse_date(r["end_date"], self.formats)
            if start and on < start.date():
                continue
            if end and on > end.date():
                continue
            hits.append(Authorization(number=r["auth_number"],
                                      start=start.date() if start else None,
                                      end=end.date() if end else None,
                                      program=(r.get("program") or None)))
        numbers = {h.number for h in hits}
        if len(numbers) > 1:
            raise AxisCareError(f"more than one active authorization in AxisCare for service "
                                f"{service_code} on {on:%m/%d/%Y}: {sorted(numbers)}")
        return hits[0] if hits else None


class ApiBackend(AxisCare):
    """TODO: implement once AxisCare provides API access.

    Ask AxisCare for: API base URL, how to authenticate, and endpoints for
    (1) caregivers with custom fields (NPI) and (2) client authorizations with
    service code, auth number and date range. The API key goes in the secure store:
        alora-evv credentials set axiscare_api_key
    """
    configured = True

    def __init__(self, cfg: dict):
        raise AxisCareError("The AxisCare API backend isn't built yet — use backend: export "
                            "in settings.yaml until AxisCare provides API documentation.")


def open_axiscare(settings: dict) -> AxisCare:
    cfg = settings.get("axiscare", {})
    backend = cfg.get("backend", "export")
    if backend == "export":
        return ExportBackend(cfg.get("export", {}))
    if backend == "api":
        return ApiBackend(cfg)
    if backend in ("", "none", None):
        return NullBackend()
    raise AxisCareError(f"unknown axiscare.backend '{backend}'")
