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

from .browser import FORBIDDEN_PORTAL, safe_click
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

    async def harvest(self, label: str, out_dir: Path, archived: bool = False
                      ) -> tuple[Path | None, dict[str, list[str]]]:
        """Search the Work List, read claim-error dialogs, download the export."""
        page = self.page
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
                return None, {}
            raise PortalChanged(f"unexpected dialog after Search: {text[:120]}")

        rows = await page.locator(".mat-mdc-row").count()
        if rows >= 100:
            print(f"WARNING [{label}]: 100+ rows — only the first page is read. "
                  "Paging isn't built yet; work the queue down and run again.")

        errors: dict[str, list[str]] = {}
        buttons = page.locator('button[aria-label="View claim matching errors"]')
        try:
            for i in range(await buttons.count()):
                b = buttons.nth(i)
                cells = await b.locator("xpath=ancestor::*[contains(@class,'mat-mdc-row')][1]") \
                    .evaluate("el => [...el.querySelectorAll('.mat-mdc-cell')]"
                              ".map(c => c.innerText.trim())")
                vid = next((c for c in cells if re.fullmatch(r"\d{10}", c)), None)
                await safe_click(b, FORBIDDEN_PORTAL)
                box = page.locator(DIALOG).last
                await box.wait_for(timeout=8000)
                text = await box.inner_text()
                lines = [re.sub(r"\s*\t\s*", " ", ln).strip() for ln in text.splitlines()]
                lines = [ln for ln in lines if ln and not ln.lower().startswith("type")]
                if vid:
                    errors[vid] = lines
                close = page.get_by_role("button", name=re.compile(r"^\s*close\s*$", re.I))
                if await close.count():
                    await safe_click(close.first, FORBIDDEN_PORTAL)
                else:
                    await page.keyboard.press("Escape")
                await box.wait_for(state="hidden", timeout=6000)
            print(f"portal [{label}]: read {len(errors)} claim-error details")
        except Exception as e:
            print(f"note [{label}]: couldn't read claim-error dialogs ({e}) — continuing")

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
        return path, errors
