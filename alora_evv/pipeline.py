"""Decides, for each visit in the portal's queue, whether it can be prepared and how.

Inputs:  portal export rows + claim-error dialog text + AxisCare lookups + rules.yaml
Outputs: Prepared (ready for a person to review) or Held (with the reason).
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from .axiscare import AxisCare, AxisCareError
from .config import Config
from .models import Held, Prepared
from .validate import matches, npi_valid, parse_date

# Portal export column names (Mobile Caregiver+ claims export)
COL = {
    "visit_id": "Internal Visit ID",
    "status": "Status",
    "cg_first": "User First Name",
    "cg_last": "User Last Name",
    "rc_first": "Recipient Name",
    "rc_last": "Recipient Last Name",
    "medicaid_id": "Recipient Medicaid ID",
    "agency_id": "Provider Agency Medicaid ID",
    "auth": "Authorization Number",
    "auth_override": "Manual Override Auth No",
    "start_method": "Start Verification Method",
    "end_method": "End Verification Method",
    "start": "Actual Start Date",
    "procedure": "Procedure Code",
}


class Skip(Exception):
    def __init__(self, reason: str, needs_decision: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.needs_decision = needs_decision


# ---------------------------------------------------------------------------
# Reading portal data
# ---------------------------------------------------------------------------
def load_export(path: Path, statuses: list[str]) -> list[dict]:
    wanted = {s.upper() for s in statuses}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()}
                for r in csv.DictReader(f)]
    return [r for r in rows if r.get(COL["status"], "").upper() in wanted]


def derive_verification_error(start_method: str, end_method: str) -> str | None:
    start = (start_method or "").strip().upper()
    end = (end_method or "").strip().upper()
    if start == "NON" and end != "NON":
        return "VSTR"
    if end == "NON":
        return "VVER"
    return None


_CODE = re.compile(r"\b[A-Z][A-Z0-9]{2,7}\b")
_NOT_CODES = {"CRITICAL", "ERROR", "WARNING", "TYPE", "INFO", "NON", "EVV", "N/A"}


def blocking_codes(dialog_lines: list[str]) -> set[str]:
    """Error codes from the portal's 'claim matching errors' dialog that block payment.

    Rows marked CRITICAL/ERROR count, unless the portal says 'acceptable per payer rule'.
    A blocking row with no recognizable code yields 'UNREADABLE' so the visit is held.
    """
    codes: set[str] = set()
    for line in dialog_lines or []:
        if not re.search(r"\b(CRITICAL|ERROR)\b", line, re.I):
            continue
        if "acceptable per payer rule" in line.lower():
            continue
        found = [c for c in _CODE.findall(line) if c not in _NOT_CODES]
        codes.add(found[0] if found else "UNREADABLE")
    return codes


# ---------------------------------------------------------------------------
# Deciding one visit
# ---------------------------------------------------------------------------
def _code_to_type(cfg: Config) -> dict[str, str]:
    out = {}
    for key, rule in cfg.rules.get("error_types", {}).items():
        for code in rule.get("portal_codes") or []:
            out[str(code)] = key
    return out


def _same_fix(cfg: Config, etype: str) -> set[str]:
    rule = cfg.error_type(etype)
    return {k for k, r in cfg.rules["error_types"].items()
            if r.get("reason_code") == rule.get("reason_code")
            and r.get("critical_error") == rule.get("critical_error")}


def _pick_error_type(row, lines, cfg: Config, decision: str | None) -> tuple[str, list[str]]:
    notes = []
    code_map = _code_to_type(cfg)
    codes = blocking_codes(lines)

    if decision:
        notes.append(f"operator decision: {decision}")
        return decision, notes

    derived = derive_verification_error(row.get(COL["start_method"]), row.get(COL["end_method"]))
    if derived:
        ok_types = _same_fix(cfg, derived)
        extra = sorted(c for c in codes if code_map.get(c) not in ok_types)
        if extra:
            raise Skip(f"{derived} (missing verification), but the portal also lists {extra}",
                       needs_decision=True)
        return derived, notes

    if not codes:
        raise Skip("both clock-in and clock-out verified and no readable portal error — "
                   "open it in the portal", needs_decision=True)
    unmapped = sorted(c for c in codes if c not in code_map)
    if unmapped:
        raise Skip(f"portal lists {unmapped}, which isn't in rules.yaml yet", needs_decision=True)
    types = {code_map[c] for c in codes}
    if len(types) > 1:
        raise Skip(f"portal lists more than one kind of error {sorted(codes)}", needs_decision=True)
    return types.pop(), notes


def compile_visit(row: dict, lines: list[str], cfg: Config, ax: AxisCare,
                  decision: str | None = None) -> Prepared:
    s, rules = cfg.settings, cfg.rules
    vid = row.get(COL["visit_id"], "")
    if not matches(rules["validation"]["visit_id"], vid):
        raise Skip(f"visit ID '{vid}' isn't 10 digits")

    cg_first, cg_last = row.get(COL["cg_first"], ""), row.get(COL["cg_last"], "")
    provider = cfg.provider_by_medicaid_id(row.get(COL["agency_id"], ""))
    if not provider:
        raise Skip(f"unknown provider agency Medicaid ID '{row.get(COL['agency_id'])}' — "
                   "add it to settings.yaml providers")

    etype, notes = _pick_error_type(row, lines, cfg, decision)
    rule = cfg.error_type(etype)
    if rule is None:
        raise Skip(f"error type {etype} isn't defined in rules.yaml")
    if not rule.get("auto") and not decision:
        raise Skip(f"{etype} ({rule.get('description')}) needs an operator decision",
                   needs_decision=True)

    medicaid = row.get(COL["medicaid_id"], "")
    if not matches(rules["validation"]["recipient_medicaid_id"], medicaid):
        raise Skip("recipient Medicaid ID on the claim isn't 11 digits")

    started = parse_date(row.get(COL["start"], ""), rules["validation"]["export_date_formats"])
    if not started:
        raise Skip(f"can't read Actual Start Date '{row.get(COL['start'])}'")
    service_date = started.date()

    # --- service code and program --------------------------------------------
    proc = row.get(COL["procedure"], "").strip()
    programs = cfg.programs_for_code(proc)
    if not programs:
        raise Skip(f"procedure code {proc} isn't in rules.yaml programs — mapping needed")

    try:
        ax_auth = ax.authorization(medicaid, proc, service_date)
    except AxisCareError as e:
        raise Skip(f"AxisCare: {e}")

    ax_program = (ax_auth.program or "").strip().upper() if ax_auth else ""
    if ax_program:
        if ax_program not in rules["programs"]:
            raise Skip(f"AxisCare lists program '{ax_program}' — the state form has no "
                       "service-code dropdown for it")
        program = ax_program
    elif proc in (rules.get("ambiguous_codes") or {}):
        raise Skip(f"procedure code {proc}: {rules['ambiguous_codes'][proc]} "
                   "(no program found in AxisCare)")
    elif len(programs) > 1:
        raise Skip(f"procedure code {proc} is in several programs {programs}")
    else:
        program = programs[0]
    prog = cfg.program(program)
    if proc not in {str(k) for k in prog["services"]}:
        raise Skip(f"procedure code {proc} isn't a {program} service on the state form")

    # --- authorization ---------------------------------------------------------
    claim_auth = row.get(COL["auth"]) or row.get(COL["auth_override"]) or ""
    require = s.get("axiscare", {}).get("require_auth_confirmation", False)
    if etype == "AUTH":
        if not ax_auth:
            raise Skip("filing as an authorization exception needs the correct "
                       "authorization from AxisCare, and none was found")
        auth = ax_auth.number
    elif ax_auth and claim_auth and ax_auth.number != claim_auth:
        raise Skip(f"claim uses authorization {claim_auth} but AxisCare shows "
                   f"{ax_auth.number} for this service on {service_date:%m/%d/%Y} — "
                   "fix it in AxisCare, or file as AUTH", needs_decision=True)
    elif claim_auth:
        auth = claim_auth
        if ax_auth:
            notes.append("authorization confirmed in AxisCare")
        elif ax.configured:
            if require:
                raise Skip(f"authorization {claim_auth} not found in AxisCare for this "
                           "service and date")
            notes.append("authorization NOT confirmed in AxisCare")
    elif ax_auth:
        auth = ax_auth.number
        notes.append("authorization taken from AxisCare (claim had none)")
    else:
        raise Skip("no authorization number on the claim or in AxisCare")

    # --- caregiver NPI ----------------------------------------------------------
    try:
        npi = ax.caregiver_npi(cg_first, cg_last)
    except AxisCareError as e:
        raise Skip(f"AxisCare: {e}")
    if not npi:
        raise Skip("no NPI in AxisCare for this caregiver" if ax.configured
                   else "AxisCare isn't configured, so there's no caregiver NPI")
    if not npi_valid(npi):
        raise Skip(f"caregiver NPI {npi} in AxisCare fails the NPI check digit — likely a typo")

    # --- build the form payload -------------------------------------------------
    caregiver = f"{cg_first} {cg_last}".strip()
    text = " ".join((rule.get("justification") or "").split())
    if text:
        text = text.format(auth=auth, service_date=f"{service_date:%m/%d/%Y}", caregiver=caregiver)
        if not text.endswith("."):
            text += "."
    if rule.get("signer_must_confirm"):
        notes.append("generic justification — confirm it's accurate for this visit")

    reason_code = rule.get("reason_code")
    reason = cfg.reason(reason_code) or {}
    recipient = f"{row.get(COL['rc_first'], '')} {row.get(COL['rc_last'], '')}".strip().title()

    payload = {
        "visitId": vid,
        "providerName": provider["name"],
        "providerMedicaidId": str(provider["medicaid_id"]),
        "providerPhone": str(provider["phone"]),
        "providerEmail": provider["email"],
        "evvVendor": s.get("evv_vendor", "Axiscare"),
        "ticketNumber": str(s.get("default_ticket_number", "0")),
        "caregiverName": caregiver,
        "caregiverNpi": npi,
        "recipientName": recipient,
        "recipientMedicaidId": medicaid,
        "programType": prog.get("form_value", program),
        "serviceCodeDropdown": prog["dropdown"],
        "serviceCode": prog["services"][proc],
        "serviceAuth": auth,
        "criticalError": rule.get("critical_error"),
        "dateOfService": service_date.isoformat(),
        "justification": text,
        "reasonCode": reason.get("label"),
        "reasonSubGroup": reason.get("sub_group"),
        "reasonSubLabel": reason.get("sub_label"),
        "reasonSubOption": reason.get("sub_option"),
        "reasonFreeText": reason.get("free_text"),
    }
    return Prepared(visit_id=vid, provider_key=provider["key"], service_date=service_date,
                    error_type=etype, reason_code=reason_code, caregiver=caregiver,
                    payload=payload, notes=notes)


# ---------------------------------------------------------------------------
# Whole queue
# ---------------------------------------------------------------------------
def compile_all(rows: list[dict], claim_errors: dict[str, list[str]], cfg: Config,
                ax: AxisCare, decisions: dict[str, str], submitted: dict[str, str],
                limit: int | None = None) -> tuple[list[Prepared], list[Held]]:
    prepared: list[Prepared] = []
    held: list[Held] = []
    counts = Counter(r.get(COL["visit_id"], "") for r in rows)
    seen: set[str] = set()

    for row in rows:
        vid = row.get(COL["visit_id"], "")
        if vid in seen:
            continue
        seen.add(vid)
        caregiver = f"{row.get(COL['cg_first'], '')} {row.get(COL['cg_last'], '')}".strip()
        prov = cfg.provider_by_medicaid_id(row.get(COL["agency_id"], ""))
        pkey = prov["key"] if prov else ""

        if counts[vid] > 1:
            held.append(Held(vid, f"appears {counts[vid]}x in the export (overnight split? "
                                  "Reason Code 100 territory)", caregiver, pkey, True))
            continue
        if vid in submitted:
            held.append(Held(vid, f"already submitted {submitted[vid][:10]} — waiting for "
                                  "the state's response", caregiver, pkey))
            continue
        decision = decisions.get(vid)
        if decision == "SKIP":
            held.append(Held(vid, "skipped by operator decision", caregiver, pkey))
            continue
        if limit is not None and len(prepared) >= limit:
            held.append(Held(vid, "over the --max limit for this run", caregiver, pkey))
            continue
        try:
            prepared.append(compile_visit(row, claim_errors.get(vid, []), cfg, ax, decision))
        except Skip as e:
            held.append(Held(vid, e.reason, caregiver, pkey, e.needs_decision))
    return prepared, held


def write_report(prepared: list[Prepared], held: list[Held], path: Path) -> Path:
    """Plain-text run summary. Contains no client names or Medicaid IDs."""
    lines = [f"EVV sweep {datetime.now():%Y-%m-%d %H:%M} — prepared {len(prepared)}, "
             f"held {len(held)}", ""]
    if prepared:
        lines.append("PREPARED (review, captcha and Submit are yours):")
        for p in prepared:
            extra = f"  | {'; '.join(p.notes)}" if p.notes else ""
            lines.append(f"  visit {p.visit_id} [{p.provider_key}] {p.service_date:%m/%d/%Y} "
                         f"{p.error_type} / reason {p.reason_code or '-'} — {p.caregiver}{extra}")
        lines.append("")
    if held:
        lines.append("HELD (needs a person):")
        lines.extend("  " + h.line() for h in held)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
