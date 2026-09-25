"""Fills the state's EVV Adjustment Request form. Never submits.

A person checks each tab, completes the captcha and clicks Submit. After that,
watch_for_submission() notices the form's thank-you screen so the ledger can
record the visit as submitted and skip it on later runs.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlencode, urlsplit

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

# The text a person sees for a radio button, however the page attaches it.
OPTION_TEXT_JS = """el => {
  const wrap = el.closest('label'); if (wrap) return wrap.innerText;
  if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return l.innerText; }
  const lb = el.getAttribute('aria-labelledby');
  if (lb) return lb.split(/\\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' ');
  return el.getAttribute('aria-label') || '';
}"""


class FormError(RuntimeError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


async def _find_option(page, value: str):
    """The listbox option for `value`: an exact text match, else the only partial one."""
    opts = page.locator("[role=option]")
    exact, partial = [], []
    for i in range(await opts.count()):
        o = opts.nth(i)
        if not await o.is_visible():
            continue
        text = _norm(await o.inner_text())
        if text == _norm(value):
            exact.append(o)
        elif _norm(value) in text:
            partial.append(o)
    if exact:
        return exact[0]
    return partial[0] if len(partial) == 1 else None


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
        # The option is chosen by clicking it, through the guard. Enter is never pressed
        # inside a field: in a <form> it can submit the page, and no guard would see that.
        option = await _find_option(page, str(value))
        if option is None:
            raise FormError(f"dropdown '{label}': no option matching '{value}' appeared — "
                            "pick it by hand")
        await safe_click(option, FORBIDDEN_FORM)
        await asyncio.sleep(0.3)
        landed = await inp.input_value()
        if landed and _norm(landed) != _norm(value):
            raise FormError(f"dropdown '{label}' selected '{landed}', wanted '{value}'")
        if not landed:
            chosen = page.locator('[aria-selected="true"]').filter(has_text=re.compile(re.escape(str(value)), re.I))
            if await chosen.count() == 0:
                problems.append(f"dropdown '{label}' shows blank after choosing '{value}' — confirm it")

    async def radio(group: str, value) -> None:
        if not value:
            return
        radios = page.locator(f'input[type=radio][name="{group}"]')
        for i in range(await radios.count()):
            r = radios.nth(i)
            if not _norm(await r.evaluate(OPTION_TEXT_JS)).startswith(_norm(value)):
                continue
            wrap = r.locator("xpath=ancestor::label[1]")
            if await wrap.count():
                target = wrap.first
            else:
                rid = await r.get_attribute("id")
                for_label = page.locator(f'label[for="{rid}"]') if rid else None
                target = for_label.first if for_label is not None and await for_label.count() else r
            await safe_click(target, FORBIDDEN_FORM)
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
    """True once the form is really gone and its confirmation shows.

    Guards against false positives (a start screen that merely mentions 'submitted'):
    the tab must still be on the form's site, show none of the form's fields and no
    Start/Reset button, and contain the confirmation text (form.submitted_text if the
    live test recorded it, else a generic match). Returns False if the tab is closed.
    """
    first = f"#{form['first_field_id']}"
    fields = ", ".join(f'[id="{fid}"]' for fid in form["text_fields"].values())
    done = (re.compile(re.escape(form["submitted_text"]), re.I) if form.get("submitted_text")
            else re.compile(r"thank|submitted|received|response has been", re.I))
    site = urlsplit(form["url"]).netloc
    start_reset = re.compile(r"^\s*(start|reset)\s*$", re.I)
    while not page.is_closed():
        try:
            if (urlsplit(page.url).netloc == site
                    and await page.locator(first).count() == 0
                    and await page.locator(fields).count() == 0
                    and await page.get_by_role("button", name=start_reset).count() == 0):
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


def prefill_params(payload: dict, form: dict, include_phi: bool = False) -> dict[str, tuple[str, str]]:
    """{payload key: (Monday column ID, value)} for the fields the prefilled link carries.

    Keys listed in form.prefill_phi_keys (client name and Medicaid ID) are left out
    unless include_phi is set: a URL is written to browser history and server logs,
    which the typed-in desktop path never does. The reason's follow-up answer goes to
    the column named by the reason's own sub_group, so it can't land in another
    reason's question.
    """
    phi = set(form.get("prefill_phi_keys") or [])
    out: dict[str, tuple[str, str]] = {}
    for key, column in (form.get("prefill_columns") or {}).items():
        if key in phi and not include_phi:
            continue
        value = payload.get(key)
        if value in (None, ""):
            continue
        if key == "reasonSubOption":
            column = payload.get("reasonSubGroup") or column
        out[key] = (str(column), str(value))
    if "reasonSubOption" not in out and payload.get("reasonSubGroup") and payload.get("reasonSubOption"):
        out["reasonSubOption"] = (str(payload["reasonSubGroup"]), str(payload["reasonSubOption"]))
    return out


def prefill_url(payload: dict, form: dict, include_phi: bool = False) -> str:
    """The state form's URL with as many fields as possible filled via query parameters.

    Experimental: Monday forms accept ?column_id=value for many field types. Which ones
    work for this form is confirmed by `alora-evv form prefill-test`; unconfirmed fields
    are simply left for the reviewer to fill.
    """
    params = {column: value for column, value in prefill_params(payload, form, include_phi).values()}
    return form["url"] + ("?" + urlencode(params) if params else "")
