"""alora-evv command line.

  alora-evv credentials set portal_username      store a login in Keychain / Credential Manager
  alora-evv check                                confirm everything is set up
  alora-evv sweep --review                       read the portal and open filled forms to review
  alora-evv sweep --from-csv export.csv          same, from an export file (no portal login)
  alora-evv decide 1234567890 AUTH               tell the app how to handle a held visit
  alora-evv ledger                               recent prepared / submitted visits
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import credentials
from .config import load_config
from .ledger import Ledger
from .paths import CONFIG_DIR, data_dir


def cmd_credentials(args) -> int:
    if args.action == "set":
        credentials.set_interactive(args.name)
    elif args.action == "delete":
        credentials.delete(args.name)
    elif args.action == "import-env":
        credentials.import_legacy_env()
    else:
        for name, ok in credentials.status().items():
            print(f"  {'set    ' if ok else 'MISSING'}  {name}  — {credentials.KNOWN[name][0]}")
    return 0


def cmd_check(args) -> int:
    ok = True
    try:
        cfg = load_config()
        print(f"config: loaded from {CONFIG_DIR}")
    except Exception as e:
        print(f"config: ERROR {e}")
        return 1
    for name, present in credentials.status().items():
        needed = name.startswith("portal")
        if needed and not present:
            ok = False
        print(f"credential {name}: {'set' if present else 'missing'}{'' if present or not needed else '  <- required'}")
    from .axiscare import AxisCareError, open_axiscare
    try:
        ax = open_axiscare(cfg.settings)
        extra = f" ({len(ax._npi)} caregivers, {len(ax._auths)} authorizations)" if hasattr(ax, "_npi") else ""
        print(f"axiscare: {cfg.settings['axiscare']['backend']} backend ready{extra}")
    except AxisCareError as e:
        ok = False
        print(f"axiscare: {e}")
    import importlib.util
    if importlib.util.find_spec("playwright"):
        print("playwright: installed")
    else:
        ok = False
        print("playwright: missing — run: pip install -e .")
    print(f"data folder: {data_dir()}")
    print("\nAll set." if ok else "\nFix the items above, then run check again.")
    return 0 if ok else 1


def cmd_sweep(args) -> int:
    from .sweep import run
    return run(load_config(), from_csv=[Path(p) for p in args.from_csv] if args.from_csv else None,
               archive=args.archive, show_portal=args.show_portal, do_review=args.review,
               limit=args.max, parallel=args.parallel, sign_name=args.sign_name,
               include_submitted=args.include_submitted)


def cmd_decide(args) -> int:
    ledger = Ledger()
    if args.clear:
        ledger.clear_decision(args.visit_id)
        print(f"Cleared the decision for visit {args.visit_id}.")
        return 0
    allowed = load_config().allowed_decisions()
    choice = (args.choice or "").upper()
    if choice not in allowed:
        print(f"Choice must be one of: {', '.join(sorted(allowed))}")
        return 1
    ledger.decide(args.visit_id, choice, args.note or "")
    print(f"Visit {args.visit_id} will be handled as {choice} on the next sweep.")
    return 0


def cmd_ledger(args) -> int:
    for r in Ledger().recent(args.limit):
        sub = f"submitted {r['submitted_at'][:16]}" if r["submitted_at"] else f"prepared {r['prepared_at'][:16]}"
        print(f"  {r['visit_id']}  {r['service_date']}  {r['error_type']:<5} reason {r['reason_code'] or '-':<4} "
              f"{r['provider']:<11} {r['caregiver']:<24} {sub}")
    return 0


def cmd_unmark(args) -> int:
    Ledger().unmark_submitted(args.visit_id)
    print(f"Visit {args.visit_id} is no longer marked as submitted.")
    return 0


def main(argv: list[str] | None = None) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="alora-evv", description="Alora EVV adjustment assistant")
    sp = ap.add_subparsers(dest="cmd", required=True)

    c = sp.add_parser("credentials", help="store or check logins and tokens")
    c.add_argument("action", choices=["set", "delete", "status", "import-env"])
    c.add_argument("name", nargs="?", choices=list(credentials.KNOWN))
    c.set_defaults(func=cmd_credentials)

    sp.add_parser("check", help="confirm setup").set_defaults(func=cmd_check)
    sp.add_parser("where", help="show the config and data folders").set_defaults(
        func=lambda a: print(f"config: {CONFIG_DIR}\ndata:   {data_dir()}") or 0)

    s = sp.add_parser("sweep", help="read the queue and prepare adjustment forms")
    s.add_argument("--review", action="store_true", help="open filled forms in a browser to review")
    s.add_argument("--from-csv", nargs="+", metavar="FILE", help="use portal export file(s) instead of logging in")
    s.add_argument("--archive", action="store_true", help="read the portal's Archive tab")
    s.add_argument("--show-portal", action="store_true", help="show the portal browser (for troubleshooting)")
    s.add_argument("--max", type=int, help="prepare at most this many forms")
    s.add_argument("--parallel", type=int, help="forms filled at once (default from settings)")
    s.add_argument("--sign-name", help='name typed into the signature ("" leaves it unsigned)')
    s.add_argument("--include-submitted", action="store_true", help="don't skip recently submitted visits")
    s.set_defaults(func=cmd_sweep)

    d = sp.add_parser("decide", help="choose how to handle a held visit")
    d.add_argument("visit_id")
    d.add_argument("choice", nargs="?", help="e.g. AUTH, KNOWN, VLOC, VVER, SKIP")
    d.add_argument("--note")
    d.add_argument("--clear", action="store_true")
    d.set_defaults(func=cmd_decide)

    lg = sp.add_parser("ledger", help="recent prepared / submitted visits")
    lg.add_argument("--limit", type=int, default=30)
    lg.set_defaults(func=cmd_ledger)

    u = sp.add_parser("unmark", help="undo a visit's 'submitted' mark")
    u.add_argument("visit_id")
    u.set_defaults(func=cmd_unmark)

    args = ap.parse_args(argv)
    if args.cmd == "credentials" and args.action in ("set", "delete") and not args.name:
        ap.error(f"credentials {args.action} needs a name: {', '.join(credentials.KNOWN)}")
    try:
        sys.exit(args.func(args) or 0)
    except credentials.MissingCredential as e:
        print(e)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
