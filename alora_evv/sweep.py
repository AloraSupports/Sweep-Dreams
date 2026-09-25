"""The morning sweep: portal -> AxisCare checks -> prepared forms for review."""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from . import credentials
from .axiscare import AxisCareError, open_axiscare
from .browser import launch
from .config import Config
from .form import fill_form, watch_for_submission
from .ledger import Ledger
from .pipeline import compile_all, load_export, write_report
from .portal import Portal
from .paths import sub


async def read_portal(cfg: Config, tmp: Path, archive: bool, show: bool):
    s = cfg.settings
    user = credentials.get("portal_username")
    pwd = credentials.get("portal_password")
    rows, errors = [], {}
    async with async_playwright() as pw:
        browser = await launch(pw, headless=not show, channel=s["browser"].get("channel"))
        portal = Portal(browser, s, user, pwd)
        try:
            await portal.open()
            for prov in s["providers"]:
                await portal.use_provider(prov["portal_menu_match"])
                path, errs = await portal.harvest(prov["key"], tmp, archived=archive)
                errors.update(errs)
                if path:
                    got = load_export(path, s["queue_statuses"])
                    print(f"portal [{prov['key']}]: {len(got)} visits need adjustment")
                    rows.extend(got)
        finally:
            await portal.close()
            await browser.close()
    return rows, errors


async def review(prepared, cfg: Config, parallel: int, sign_name: str, ledger: Ledger):
    async with async_playwright() as pw:
        browser = await launch(pw, headless=False, channel=cfg.settings["browser"].get("channel"))
        ctx = await browser.new_context()
        sem = asyncio.Semaphore(max(1, parallel))

        async def fill_one(p):
            async with sem:
                page = await ctx.new_page()
                try:
                    problems = await fill_form(page, p.payload, cfg.form, sign_name)
                except Exception as e:
                    problems = [f"couldn't fill: {str(e).splitlines()[0]}"]
                state = "ready" if not problems else "CHECK — " + "; ".join(problems)
                print(f"  tab: visit {p.visit_id} ({p.caregiver}) {state}")
                return page

        print(f"Filling {len(prepared)} form(s)...")
        pages = await asyncio.gather(*(fill_one(p) for p in prepared))

        async def watch(p, page):
            if await watch_for_submission(page, cfg.form):
                ledger.mark_submitted(p.visit_id)
                print(f"  submitted: visit {p.visit_id}")

        tasks = [asyncio.create_task(watch(p, pg)) for p, pg in zip(prepared, pages)]
        print("\nGo tab by tab: check every field, tick the captcha, click Submit yourself.")
        print("Submitted visits are recorded automatically. Close the browser when done.\n")

        closed = asyncio.Event()
        browser.on("disconnected", lambda *_: closed.set())
        while not closed.is_set() and not all(pg.is_closed() for pg in pages):
            await asyncio.sleep(1)
        for t in tasks:
            t.cancel()
        try:
            await browser.close()
        except Exception:
            pass
    print("Review finished.")


@dataclass
class SweepResult:
    queued: int
    prepared: list
    held: list
    report: Path
    started: datetime
    finished: datetime


def collect(cfg: Config, *, from_csv: list[Path] | None = None, archive: bool = False,
            show_portal: bool = False, limit: int | None = None,
            include_submitted: bool = False, ledger: Ledger | None = None) -> SweepResult:
    """Read the queue, decide each visit, record it, write the report. No browser review."""
    s = cfg.settings
    ledger = ledger or Ledger()
    started = datetime.now()
    ax = open_axiscare(s)  # raises AxisCareError with a clear message

    with tempfile.TemporaryDirectory(prefix="alora_evv_") as tmp:
        if from_csv:
            rows, errors = [], {}
            for path in from_csv:
                rows.extend(load_export(path, s["queue_statuses"]))
        else:
            rows, errors = asyncio.run(read_portal(cfg, Path(tmp), archive, show_portal))
    # The temporary folder (and the downloaded export) is deleted here.

    submitted = {} if include_submitted else ledger.recently_submitted(s.get("resubmit_after_days", 30))
    prepared, held = compile_all(rows, errors, cfg, ax, ledger.decisions(), submitted, limit)
    for p in prepared:
        ledger.record_prepared(p)
    report = write_report(prepared, held, sub("reports") / f"sweep_{started:%Y%m%d_%H%M}.txt")
    queued = len({r.get("Internal Visit ID") for r in rows})
    return SweepResult(queued, prepared, held, report, started, datetime.now())


def run(cfg: Config, *, from_csv: list[Path] | None, archive: bool, show_portal: bool,
        do_review: bool, limit: int | None, parallel: int | None, sign_name: str | None,
        include_submitted: bool) -> int:
    s = cfg.settings
    ledger = Ledger()
    try:
        res = collect(cfg, from_csv=from_csv, archive=archive, show_portal=show_portal,
                      limit=limit, include_submitted=include_submitted, ledger=ledger)
    except AxisCareError as e:
        print(f"AxisCare: {e}")
        return 2

    print(f"\n{res.queued} visit(s) in the queue: {len(res.prepared)} prepared, {len(res.held)} held.")
    for h in res.held:
        print("  HELD " + h.line())
    print(f"Report: {res.report}")

    if do_review and res.prepared:
        asyncio.run(review(res.prepared, cfg, parallel or s["browser"].get("parallel_tabs", 4),
                           s.get("signer_name", "") if sign_name is None else sign_name,
                           ledger))
    elif do_review:
        print("Nothing to review.")
    return 0
