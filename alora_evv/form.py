"""Fills the state's EVV Adjustment Request form. Never submits.

A person checks each tab, completes the captcha and clicks Submit. After that,
watch_for_submission() notices the form's thank-you screen so the ledger can
record the visit as submitted and skip it on later runs.
"""
from __future__ import annotations

import asyncio
import re

from playwright.async_api import TimeoutError as PWTimeout

from .browser import FORBIDDEN_FORM, safe_click

DUMP_JS = """() => {
  const out = {radios: [], fields: {}};
  document.querySelectorAll('input,textarea').forEach(el => {
    if (el.type === 'radio') {
      if (el.checked) out.radios.push(el.name);
    } else if (el.name || el.id) {
      out.fields[el.name || el.id] = el.value;
    }
  });
  return out;
}"""

CANVAS_JS = """() => {
  const cv = document.querySelector('canvas');
  if (!cv) return -1;
  const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
  let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;
  return n;
}"""


class FormError(RuntimeError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


async def fill_form(page, payload: dict, form: dict, sign_name: str = "") -> list[str]:
    """Fill every field in `payload`. Returns a list of problems (empty = fully verified)."""
    problems: list[str] = []
    await page.goto(form["url"], wait_until="domcontentloaded")
    await page.wait_for_selector(
        f"button:has-text('Start'), button:has-text('Reset'), #{form['first_field_id']}",
        timeout=25000)
    for name in ("reset", "start"):
        btn = page.get_by_role("button", name=re.compile(rf"^{name}$", re.I))
        if await btn.count():
            await safe_click(btn.first, FORBIDDEN_FORM)
            await asyncio.sleep(0.5)
    await page.wait_for_selector(f"#{form['first_field_id']}", timeout=15000)

    tf = form["text_fields"]

    async def text(key: str, value) -> None:
        if value in (None, ""):
            return
        fid = tf[key]
        loc = page.locator(f'[name="{fid}"], [id="{fid}"]').first
        if await loc.count() == 0:
            raise FormError(f"field {key} ({fid}) not found — update state_form.yaml")
        await loc.fill(str(value))

    async def dropdown(label: str, value) -> None:
        if not value:
            return
        inp = page.locator(f'input[aria-label="{label}"]').first
        if await inp.count() == 0:
            raise FormError(f"dropdown '{label}' not found — update state_form.yaml")
        await safe_click(inp, FORBIDDEN_FORM)
        await inp.fill(str(value))
        await asyncio.sleep(0.4)
        await page.keyboard.press("ArrowDown")
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.3)
        landed = await inp.input_value()
        if landed and _norm(landed) != _norm(value):
            raise FormError(f"dropdown '{label}' selected '{landed}', wanted '{value}'")

    async def radio(group: str, value) -> None:
        if not value:
            return
        radios = page.locator(f'input[type=radio][name="{group}"]')
        for i in range(await radios.count()):
            label = radios.nth(i).locator("xpath=ancestor::label[1]")
            if _norm(await label.inner_text()).startswith(_norm(value)):
                await safe_click(label, FORBIDDEN_FORM)
                return
        raise FormError(f"option '{value}' not found in radio group {group}")

    dd, rg = form["dropdowns"], form["radio_groups"]
    await text("providerName", payload["providerName"])
    await text("providerMedicaidId", payload["providerMedicaidId"])
    await dropdown(dd["phoneCountry"]["label"], dd["phoneCountry"]["value"])
    await text("providerPhone", payload["providerPhone"])
    await page.locator(form["email_selector"]).first.fill(payload["providerEmail"])
    await dropdown(dd["evvVendor"]["label"], payload["evvVendor"])
    await text("ticketNumber", payload["ticketNumber"])
    await text("caregiverName", payload["caregiverName"])
    await text("caregiverNpi", payload["caregiverNpi"])
    await text("recipientName", payload["recipientName"])
    await text("recipientMedicaidId", payload["recipientMedicaidId"])
    await dropdown(dd["programType"]["label"], payload["programType"])
    await dropdown(payload["serviceCodeDropdown"], payload["serviceCode"])
    await text("serviceAuth", payload["serviceAuth"])
    await text("visitId", payload["visitId"])
    await radio(rg["criticalError"], payload.get("criticalError"))
    await page.locator(form["date_selector"]).first.fill(payload["dateOfService"])
    await text("justification", payload.get("justification"))
    await radio(rg["reasonCode"], payload.get("reasonCode"))

    if payload.get("reasonSubGroup") and payload.get("reasonSubOption"):
        await asyncio.sleep(0.3)  # follow-up question appears after the reason code
        group = f"radio-group-{payload['reasonSubGroup']}"
        if await page.locator(f'input[type=radio][name="{group}"]').count():
            await radio(group, payload["reasonSubOption"])
        else:
            await dropdown(payload.get("reasonSubLabel") or "", payload["reasonSubOption"])
    await text("reasonFreeText", payload.get("reasonFreeText"))

    if sign_name:
        sig = page.locator(form["signature_input"]).first
        if await sig.count():
            await sig.fill(sign_name)
            use = page.get_by_role("button", name=re.compile(r"^use signature$", re.I)).first
            for _ in range(12):
                if await use.count() and await use.is_enabled():
                    break
                await asyncio.sleep(0.25)
            if await use.count() and await use.is_enabled():
                await safe_click(use, FORBIDDEN_FORM)
                await asyncio.sleep(0.6)
                if await page.evaluate(CANVAS_JS) == 0:
                    problems.append("signature box looks empty")
            else:
                problems.append("'Use signature' didn't enable — signature not applied")
        else:
            problems.append("signature field not found")

    # Re-read the page and confirm the key values stuck.
    dump = await page.evaluate(DUMP_JS)
    for key in form.get("verify_fields", []):
        got = dump["fields"].get(tf[key], "")
        if _norm(got) != _norm(str(payload.get(key, ""))):
            problems.append(f"{key} shows '{got}'")
    if payload.get("criticalError") and rg["criticalError"] not in dump["radios"]:
        problems.append("Critical Error not selected")
    if payload.get("reasonCode") and rg["reasonCode"] not in dump["radios"]:
        problems.append("Reason Code not selected")
    if not payload.get("criticalError"):
        problems.append("Critical Error left blank for you to choose")
    if not payload.get("reasonCode"):
        problems.append("Reason Code left blank for you to choose")
    return problems


async def watch_for_submission(page, form: dict, poll: float = 2.0) -> bool:
    """True once the form's fields are gone and a thank-you/confirmation shows.

    Returns False if the tab is closed first.
    """
    first = f"#{form['first_field_id']}"
    done = re.compile(r"thank|submitted|received|response has been", re.I)
    while not page.is_closed():
        try:
            if await page.locator(first).count() == 0:
                body = await page.evaluate("() => document.body ? document.body.innerText : ''")
                if done.search(body or ""):
                    return True
        except Exception:
            if page.is_closed():
                return False
        try:
            await page.wait_for_timeout(poll * 1000)
        except (PWTimeout, Exception):
            return False
    return False


def prefill_url(payload: dict, form: dict) -> str:
    """The state form's URL with as many fields as possible filled via query parameters.

    Experimental: Monday forms accept ?column_id=value for many field types. Which ones
    work for this form is confirmed by a live test; unconfirmed fields are simply left
    for the reviewer to fill.
    """
    from urllib.parse import urlencode
    params = {}
    for key, column in (form.get("prefill_columns") or {}).items():
        value = payload.get(key)
        if value not in (None, ""):
            params[column] = str(value)
    return form["url"] + ("?" + urlencode(params) if params else "")
