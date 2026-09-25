"""Runs the form filler against a local stand-in of the state form (fake data).

This checks the filler's logic and the safety guard. The real form still needs
one supervised live run, since the state can change its page at any time.
"""
import asyncio
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.async_api import async_playwright  # noqa: E402

from alora_evv.browser import FORBIDDEN_FORM, SafetyStop, safe_click  # noqa: E402
from alora_evv.form import fill_form, watch_for_submission  # noqa: E402

MOCK = (Path(__file__).parent / "fixtures" / "mock_state_form.html").as_uri()


def run(coro):
    return asyncio.run(coro)


async def _with_page(fn):
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as e:
            pytest.skip(f"no browser: {e}")
        page = await browser.new_page()
        try:
            return await fn(page)
        finally:
            await browser.close()


def test_fill_verify_and_guard(cfg, fake_ax, export_rows):
    from alora_evv.pipeline import compile_visit
    p = compile_visit(next(r for r in export_rows if r["Internal Visit ID"] == "1000000001"),
                      [], cfg, fake_ax)
    form = dict(cfg.form, url=MOCK)

    async def go(page):
        problems = await fill_form(page, p.payload, form, sign_name="Test Signer")
        vals = await page.evaluate("""() => ({
            npi: document.getElementById('number65i2memq-input').value,
            svc: document.querySelector('[aria-label="HCBS AD Waiver Services Code"]').value,
            crit: [...document.querySelectorAll('[name="radio-group-label__1"]')].findIndex(r => r.checked),
            sub: [...document.querySelectorAll('[name="radio-group-single_select_mkm8p2yf"]')].findIndex(r => r.checked),
            date: document.querySelector('input[type=date]').value })""")
        with pytest.raises(SafetyStop):
            await safe_click(page.locator("#submit"), FORBIDDEN_FORM)
        # a person clicks Submit; the watcher should notice
        watcher = asyncio.create_task(watch_for_submission(page, form, poll=0.2))
        await page.locator("#submit").click()
        return problems, vals, await asyncio.wait_for(watcher, 5)

    problems, vals, submitted = run(_with_page(go))
    assert problems == []
    assert vals == {"npi": "1234567893", "svc": "Personal Care - 5761", "crit": 1,
                    "sub": 0, "date": "2026-09-21"}
    assert submitted is True
