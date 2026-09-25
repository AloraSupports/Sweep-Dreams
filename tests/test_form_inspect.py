"""The form inspector and prefill tester, run against the local stand-in form (fake data)."""
import asyncio
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.async_api import async_playwright  # noqa: E402

from alora_evv.forminspect import FAKE_PAYLOAD, column_id, inspect, prefill_test  # noqa: E402

MOCK = (Path(__file__).parent / "fixtures" / "mock_state_form.html").as_uri()


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


def test_column_id():
    assert column_id({"id": "short_text_mkk9gc42-input"}) == "short_text_mkk9gc42"
    assert column_id({"id": "phone__1-phone-number-input"}) == "phone__1"
    assert column_id({"id": "radio-group-label__1"}) == "label__1"
    assert column_id({"id": "", "name": ""}) is None


def test_inspect_reads_options_and_follow_ups(cfg):
    rep = asyncio.run(_with_page(lambda page: inspect(page, cfg.form, MOCK)))
    assert rep["dropdowns"]["Program Type"] == ["DD", "AD", "TBI", "PAS"]
    assert rep["radio_groups"]["radio-group-label__1"] == ["VLOC - Location", "VVER - Verification"]
    codes = rep["radio_groups"]["radio-group-single_select_mkk96g2w"]
    assert codes == ["Reason Code 140 - No clock", "Reason Code 150 - Location"]
    # picking 140 reveals the follow-up radio group; 150 reveals nothing new
    sub = rep["follow_ups"]["Reason Code 140 - No clock"]
    assert [f["name"] for f in sub] == ["radio-group-single_select_mkm8p2yf"]
    assert sub[0]["options"] == ["A. Failure to Clock In, Clock Out or Both", "B. Something else"]
    assert rep["follow_ups"]["Reason Code 150 - Location"] == []
    assert "Submit" in rep["buttons"]  # listed, never clicked


def test_prefill_test_reports_what_landed(cfg):
    rep = asyncio.run(_with_page(lambda page: prefill_test(page, cfg.form, MOCK)))
    app = rep["app_url"]
    assert app["providerName"]["landed"] and app["visitId"]["landed"]
    assert app["criticalError"]["landed"] and app["reasonCode"]["landed"] and app["reasonSubOption"]["landed"]
    disc = rep["discovered"]
    assert disc["short_text_mkk9gc42"]["landed"] and disc["label__1"]["landed"]
    assert "Country code" not in disc  # no column ID visible, so nothing to try
    sugg = rep["suggested_prefill_columns"]
    assert sugg["providerName"] == "short_text_mkk9gc42" and sugg["criticalError"] == "label__1"
    assert "TEST" in FAKE_PAYLOAD["providerName"]  # the payload is obviously fake
