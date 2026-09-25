"""Mobile Caregiver+ EVV dashboard: read-only access to the claims Work List.

Ported from the AloraEVVSweep selectors. It logs in, switches between provider
agencies, reads each visit's claim-matching errors and downloads the export.
It never changes records in the portal.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import TimeoutError as PWTimeout

from .browser import FORBIDDEN_PORTAL, SafetyStop, safe_click
from .paths import data_dir, make_private

PROVIDER_SWITCH = "evv-select-provider"
DIALOG = "mat-dialog-container, [role=dialog]"
DIALOG_BUTTONS = "mat-dialog-container button, [role=dialog] button"
OK = re.compile(r"^\s*ok\s*$", re.I)


class PortalChanged(RuntimeError):
    """The page didn't look the way this code expects. Stop rather than improvise."""


def _session_files() -> tuple[Path, Path]:
    d = data_dir()
    return d / "portal_session.json", d / "portal_session_storage.json"


def _seed_script(origin: str, ss: dict) -> str:
    # Restores the portal's sessionStorage login token (not the password).
    return ("(() => { try { if (location.origin === %s && !localStorage.getItem('evv_noseed') "
            "&& !sessionStorage.length) { const d = %s; for (const k of Object.keys(d)) "
            "sessionStorage.setItem(k, d[k]); } } catch (e) {} })()"
            % (json.dumps(origin), json.dumps(ss)))


class Portal:
    def __init__(self, browser, settings: dict, username: str, password: str):
        self.browser = browser
        self.s = settings
        self.p = settings["portal"]
        self.username = username
        self.password = password
        self.ctx = None
        self.page = None

    def worklist_url(self, archived: bool) -> str:
        return self.p["worklist_url"].format(archived=str(archived).lower())

    async def open(self) -> None:
        state, ss_file = _session_files()
        try:
            self.ctx = await self.browser.new_context(
                accept_downloads=True, storage_state=str(state) if state.exists() else None)
        except Exception:
            state.unlink(missing_ok=True)
            self.ctx = await self.browser.new_context(accept_downloads=True)
        if ss_file.exists():
            try:
                ss = json.loads(ss_file.read_text(encoding="utf-8"))
                await self.ctx.add_init_script(_seed_script(self.p["origin"], ss))
            except Exception:
                ss_file.unlink(missing_ok=True)
        self.page = await self.ctx.new_page()
        await self._login()

    async def close(self) -> None:
        if self.ctx:
            await self.ctx.close()

    async def _login(self) -> None:
        page = self.page
        await page.goto(self.worklist_url(False), wait_until="domcontentloaded")
        try:
            await page.locator(PROVIDER_SWITCH).first.wait_for(timeout=12000)
            print("portal: saved session still valid — no login needed")
            return
        except PWTimeout:
            pass

        state, ss_file = _session_files()
        state.unlink(missing_ok=True)
        ss_file.unlink(missing_ok=True)
        await page.evaluate("() => { try { localStorage.setItem('evv_noseed','1'); "
                            "sessionStorage.clear(); } catch (e) {} }")
        await page.goto(self.p["login_url"], wait_until="domcontentloaded")

        user = page.get_by_label(re.compile(r"username|email", re.I)).first
        pwd = page.get_by_label(re.compile(r"password", re.I)).first
        try:
            await user.wait_for(timeout=20000)
        except PWTimeout:
            raise PortalChanged(f"login page has no username/password fields: {page.url}")
        await user.fill(self.username)
        await pwd.fill(self.password)

        btn = page.locator('[data-automation="login-userSubmit-button"]')
        if await btn.count() == 0:
            btn = page.get_by_role("button", name=re.compile(r"log ?in|sign ?in", re.I))
        await safe_click(btn.first, FORBIDDEN_PORTAL)
        try:
            await page.wait_for_url(re.compile(r"/provider/"), timeout=45000)
        except PWTimeout:
            raise PortalChanged("login didn't reach the provider area within 45s "
                                "(wrong password? MFA? page changed?)")
        print("portal: signed in")
        await page.evaluate("() => { try { localStorage.removeItem('evv_noseed') } catch (e) {} }")

        if not self.p.get("save_session", True):
            return  # portal.save_session: false — log in fresh every run, keep no token on disk
        try:
            await self.ctx.storage_state(path=str(state))
            make_private(state)
            ss_file.write_text(await page.evaluate("() => JSON.stringify(sessionStorage)"),
                               encoding="utf-8")
            make_private(ss_file)
        except Exception as e:
            print(f"note: couldn't save the portal session ({e}); next run logs in again")

    async def use_provider(self, menu_match: str) -> None:
        """Switch the header's provider-agency selector (a view toggle; changes no records)."""
        page = self.page
        pat = re.compile(re.escape(menu_match), re.I)
        switch = page.locator(PROVIDER_SWITCH).first
        await switch.wait_for(timeout=20000)
        if pat.search(await switch.inner_text()):
            return
        await safe_click(switch, FORBIDDEN_PORTAL)
        option = page.locator(".cdk-overlay-container button").filter(has_text=pat).first
        try:
            await option.wait_for(timeout=8000)
        except PWTimeout:
            raise PortalChanged(f"provider '{menu_match}' not in the provider menu")
        await safe_click(option, FORBIDDEN_PORTAL)
        ok = page.locator(DIALOG_BUTTONS).filter(has_text=OK).first
        try:
            await ok.wait_for(timeout=8000)
            await safe_click(ok, FORBIDDEN_PORTAL)
        except PWTimeout:
            pass
        for _ in range(20):
            if pat.search(await switch.inner_text()):
                return
            await asyncio.sleep(0.5)
        raise PortalChanged(f"provider switch to '{menu_match}' didn't take effect")

    async def _visit_id_column(self) -> int | None:
        """Index of the 'Internal Visit ID' column, read from the table header."""
        heads = await self.page.locator(".mat-mdc-header-cell").all_inner_texts()
        for i, h in enumerate(heads):
            if re.search(r"internal\s+visit\s+id", h, re.I):
                return i
        return None

    async def harvest(self, label: str, out_dir: Path, archived: bool = False
                      ) -> tuple[Path | None, dict[str, list[str]], list[str]]:
        """Search the Work List, read claim-error dialogs, download the export.

        Returns (export path, {visit ID: dialog lines} for every row whose dialog was
        read — an empty list means no errors button — and a list of warnings for the
        person running the sweep). A visit missing from the dict means its dialog
        couldn't be read, so the pipeline can say so rather than assume 'no errors'.
        """
        page = self.page
        warnings: list[str] = []
        print(f"portal [{label}]: opening the {'Archive' if archived else 'Work List'}...")
        await page.goto(self.worklist_url(archived), wait_until="domcontentloaded")
        search = page.get_by_role("button", name=re.compile(r"^\s*search\s*$", re.I)).first
        await search.wait_for(timeout=45000)
        if archived:
            tab = page.get_by_text(re.compile(r"^\s*Archive\s*$")).first
            if await tab.count() == 0:
                raise PortalChanged("Archive tab not found on the Work List page")
            await safe_click(tab, FORBIDDEN_PORTAL)
            await asyncio.sleep(2)
        await safe_click(search, FORBIDDEN_PORTAL)

        await page.wait_for_selector(".mat-mdc-row, mat-dialog-container", timeout=30000)
        dialog = page.locator("mat-dialog-container")
        if await dialog.count() and await dialog.first.is_visible():
            text = (await dialog.first.inner_text()).strip()
            if "No Data" in text:
                ok = page.locator(DIALOG_BUTTONS).filter(has_text=OK).first
                if await ok.count():
                    await safe_click(ok, FORBIDDEN_PORTAL)
                print(f"portal [{label}]: no data")
                return None, {}, warnings
            raise PortalChanged(f"unexpected dialog after Search: {text[:120]}")

        rows = page.locator(".mat-mdc-row")
        n = await rows.count()
        if n >= 100:
            warnings.append(f"{label}: the portal listed 100+ rows and only the first page "
                            "was read — work the queue down and run again")
        vid_col = await self._visit_id_column()
        if vid_col is None:
            warnings.append(f"{label}: no 'Internal Visit ID' column header found; visit IDs "
                            "were guessed from the first 10-digit cell")

        errors: dict[str, list[str]] = {}
        for i in range(n):
            row = rows.nth(i)
            vid = None
            try:
                cells = await row.evaluate("el => [...el.querySelectorAll('.mat-mdc-cell')]"
                                           ".map(c => c.innerText.trim())")
                if vid_col is not None and vid_col < len(cells) and re.fullmatch(r"\d{10}", cells[vid_col]):
                    vid = cells[vid_col]
                else:
                    vid = next((c for c in cells if re.fullmatch(r"\d{10}", c)), None)
                if not vid:
                    continue
                errors[vid] = []
                b = row.locator('button[aria-label="View claim matching errors"]')
                if await b.count() == 0:
                    continue
                await safe_click(b.first, FORBIDDEN_PORTAL)
                box = page.locator(DIALOG).last
                await box.wait_for(timeout=8000)
                text = await box.inner_text()
                lines = [re.sub(r"\s*\t\s*", " ", ln).strip() for ln in text.splitlines()]
                errors[vid] = [ln for ln in lines if ln and not ln.lower().startswith("type")]
                close = box.get_by_role("button", name=re.compile(r"^\s*close\s*$", re.I))
                if await close.count():
                    await safe_click(close.first, FORBIDDEN_PORTAL)
                else:
                    await page.keyboard.press("Escape")
                await box.wait_for(state="hidden", timeout=6000)
            except SafetyStop:
                raise
            except Exception as e:
                if vid:
                    errors.pop(vid, None)  # unknown, which is not the same as "no errors"
                warnings.append(f"{label}: couldn't read the claim-error dialog for visit "
                                f"{vid or '?'} ({str(e).splitlines()[0][:80]})")
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
        print(f"portal [{label}]: read {len(errors)} claim-error details")

        print(f"portal [{label}]: downloading the export...")
        select_all = page.locator("table thead mat-checkbox, th mat-checkbox").first
        if await select_all.count():
            await safe_click(select_all, FORBIDDEN_PORTAL)
        export = page.locator('[data-automation="claims-export-button"]')
        if await export.count() == 0:
            export = page.get_by_role("button", name=re.compile(r"^\s*export\s*$", re.I))
        if await export.count() == 0:
            raise PortalChanged("Export button not found")
        export = export.first
        for _ in range(20):
            if (await export.get_attribute("aria-disabled")) != "true" and await export.is_enabled():
                break
            await asyncio.sleep(0.5)
        else:
            raise PortalChanged("Export button stayed disabled after selecting all rows")
        async with page.expect_download(timeout=60000) as dl:
            await safe_click(export, FORBIDDEN_PORTAL)
            ok = page.locator(DIALOG_BUTTONS).filter(has_text=OK).first
            try:
                await ok.wait_for(timeout=3000)
                await safe_click(ok, FORBIDDEN_PORTAL)
            except PWTimeout:
                pass
        download = await dl.value
        path = out_dir / f"export_{label}.csv"
        await download.save_as(str(path))
        return path, errors, warnings
