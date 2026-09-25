"""Reads the state form's fields and options, and tests URL prefill. Never submits.

  alora-evv form inspect           list every field, dropdown option, radio label and
                                   reason-code follow-up question on the live form
  alora-evv form prefill-test      open the form with FAKE values in the URL and report
                                   which fields the form actually prefilled

Both commands only read the page, open dropdowns, and tick radio buttons to reveal
follow-up questions. Every click goes through the safety guard, and the fake values
are obviously fake (no real visit, client or caregiver data is used).
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlencode

from .browser import FORBIDDEN_FORM, safe_click
from .form import prefill_url

# Fake payload for the prefill test. Nothing here is real. The NPI is CMS's published example.
FAKE_PAYLOAD = {
    "visitId": "1000000001",
    "providerName": "TEST PROVIDER DO NOT SUBMIT",
    "providerMedicaidId": "12345678",
    "providerPhone": "5555550100",
    "providerEmail": "test@example.com",
    "evvVendor": "Axiscare",
    "ticketNumber": "0",
    "caregiverName": "Test Caregiver",
    "caregiverNpi": "1234567893",
    "recipientName": "Test Client",
    "recipientMedicaidId": "00000000000",
    "programType": "AD",
    "serviceCodeDropdown": "HCBS AD Waiver Services Code",
    "serviceCode": "Personal Care - 5761",
    "serviceAuth": "TESTAUTH001",
    "criticalError": "VVER",
    "dateOfService": "2020-01-01",
    "justification": "TEST ONLY. Do not submit.",
    "reasonCode": "Reason Code 140",
    "reasonSubOption": "A. Failure to Clock In, Clock Out or Both",
}

FIELDS_JS = """() => {
  const seen = new Set();
  const out = [];
  const labelFor = (el) => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText.trim(); }
    const wrap = el.closest('label'); if (wrap) return wrap.innerText.trim();
    const lb = el.getAttribute('aria-labelledby');
    if (lb) return lb.split(/\\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' ').trim();
    return '';
  };
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  document.querySelectorAll('input, textarea, select').forEach(el => {
    if (el.type === 'hidden') return;
    const key = el.type === 'radio' ? 'radio:' + el.name : (el.name || el.id || '');
    if (el.type === 'radio') {
      const existing = out.find(o => o.kind === 'radio' && o.name === el.name);
      const label = labelFor(el);
      if (existing) { existing.options.push(label); existing.visible = existing.visible || visible(el); return; }
      out.push({kind: 'radio', name: el.name, options: [label], visible: visible(el)});
      return;
    }
    if (key && seen.has(key)) return;
    if (key) seen.add(key);
    out.push({kind: el.tagName === 'SELECT' ? 'select' : (el.getAttribute('role') === 'combobox' ? 'combobox' : el.type || 'text'),
              id: el.id || '', name: el.name || '', label: labelFor(el),
              placeholder: el.placeholder || '', required: el.required || el.getAttribute('aria-required') === 'true',
              value: el.value || '', visible: visible(el)});
  });
  return out;
}"""

VALUES_JS = """() => {
  const out = {fields: {}, radios: {}};
  document.querySelectorAll('input, textarea, select').forEach(el => {
    if (el.type === 'hidden') return;
    if (el.type === 'radio') { if (el.checked) out.radios[el.name] = (el.closest('label') || el).innerText.trim(); return; }
    const key = el.name || el.id || el.getAttribute('aria-label') || '';
    if (key) out.fields[key] = el.value || '';
  });
  return out;
}"""

BUTTONS_JS = "() => [...document.querySelectorAll('button')].map(b => b.innerText.trim()).filter(Boolean)"
BODY_JS = "() => document.body ? document.body.innerText : ''"


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


async def open_form(page, url: str, form: dict) -> None:
    """Open the form and get past the Start screen, the same way the filler does."""
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_selector(
        f"button:has-text('Start'), button:has-text('Reset'), #{form['first_field_id']}",
        timeout=25000)
    start = page.get_by_role("button", name=re.compile(r"^start$", re.I))
    if await start.count():
        await safe_click(start.first, FORBIDDEN_FORM)
        await asyncio.sleep(0.5)
    await page.wait_for_selector(f"#{form['first_field_id']}", timeout=15000)


async def dropdown_options(page, field: dict) -> list[str]:
    """Open a dropdown and read its options. Works with a role=listbox or, failing that,
    with whatever new text appeared on the page after the click."""
    sel = (f'[aria-label="{field["label"]}"]' if field.get("label")
           else f'[id="{field["id"]}"]' if field.get("id") else f'[name="{field["name"]}"]')
    inp = page.locator(sel).first
    if await inp.count() == 0:
        return []
    before = set(_lines(await page.evaluate(BODY_JS)))
    await safe_click(inp, FORBIDDEN_FORM)
    await asyncio.sleep(0.5)
    opts = page.locator("[role=option]")
    if await opts.count():
        found = [t.strip() for t in await opts.all_inner_texts() if t.strip()]
    else:
        after = _lines(await page.evaluate(BODY_JS))
        found = [ln for ln in after if ln not in before]
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.2)
    return found


async def inspect(page, form: dict, url: str | None = None) -> dict:
    """Everything a person needs to fill in rules.yaml and state_form.yaml."""
    await open_form(page, url or form["url"], form)
    fields = await page.evaluate(FIELDS_JS)
    buttons = await page.evaluate(BUTTONS_JS)
    report = {"url": url or form["url"], "fields": [], "radio_groups": {}, "dropdowns": {},
              "buttons": buttons, "follow_ups": {}}
    for f in fields:
        if f["kind"] == "radio":
            report["radio_groups"][f["name"]] = f["options"]
        else:
            report["fields"].append(f)
    # Dropdown-like inputs: Monday renders them as text inputs with an aria-label.
    for f in report["fields"]:
        if not f["visible"]:
            continue
        if f["kind"] in ("combobox", "select") or (f["kind"] == "text" and f["label"] and not f["id"]):
            try:
                report["dropdowns"][f["label"] or f["id"] or f["name"]] = await dropdown_options(page, f)
            except Exception as e:  # a dropdown that won't open shouldn't stop the report
                report["dropdowns"][f["label"] or f["id"] or f["name"]] = [f"(couldn't open: {str(e).splitlines()[0]})"]
    # Follow-up questions: tick each Reason Code option and note what appears.
    group = form.get("radio_groups", {}).get("reasonCode")
    if group and group in report["radio_groups"]:
        def key_of(f):
            return "radio:" + f["name"] if f["kind"] == "radio" else (f["id"] or f["name"])
        base_ids = {key_of(f) for f in fields if f["visible"]}
        radios = page.locator(f'input[type=radio][name="{group}"]')
        for i in range(await radios.count()):
            if i:  # fresh page per option, so one option's follow-up isn't blamed on the next
                await open_form(page, url or form["url"], form)
            label = radios.nth(i).locator("xpath=ancestor::label[1]")
            target = label if await label.count() else radios.nth(i)
            await safe_click(target, FORBIDDEN_FORM)
            await asyncio.sleep(0.5)
            now = await page.evaluate(FIELDS_JS)
            new = []
            for f in now:
                if f["visible"] and key_of(f) not in base_ids:
                    entry = {k: v for k, v in f.items() if k != "value"}
                    if f["kind"] != "radio" and (f["kind"] in ("combobox", "select") or (f["label"] and not f["id"])):
                        try:
                            entry["options"] = await dropdown_options(page, f)
                        except Exception:
                            pass
                    new.append(entry)
            report["follow_ups"][report["radio_groups"][group][i]] = new
    return report


def column_id(field: dict) -> str | None:
    """The Monday column ID behind a field, from its id/name (short_text_x-input -> short_text_x)."""
    ident = field.get("id") or field.get("name") or ""
    m = re.match(r"^(?:radio-group-)?(.+?)(?:-phone-number)?-input$", ident) or \
        re.match(r"^radio-group-(.+)$", ident)
    return m.group(1) if m else None


def fake_value_for(field: dict, n: int, options: list[str] | None) -> str:
    kind = field.get("kind", "text")
    if options:
        return options[0]
    if kind in ("number",) or "number" in (field.get("id") or ""):
        return str(7000 + n)
    if kind == "date":
        return "2020-01-01"
    if kind == "email":
        return "test@example.com"
    if "phone" in (field.get("id") or ""):
        return "5555550100"
    return f"TEST{n:03d}"


async def prefill_test(page, form: dict, url: str | None = None) -> dict:
    """Two passes: (1) the app's own prefill_url with the fake payload — which keys land;
    (2) every column ID visible in the page, each with a distinctive fake value — which land.
    Returns a report plus a suggested prefill_columns block."""
    base = url or form["url"]
    report = {"url": base, "app_url": {}, "discovered": {}, "suggested_prefill_columns": {}}

    # Pass 0: learn the page's fields and options.
    scan = await inspect(page, form, base)
    fields = [f for f in scan["fields"] if column_id(f)]
    radio_groups = scan["radio_groups"]

    # Pass 1: the app's prefill_url, as the web version would open it.
    app_url = prefill_url(FAKE_PAYLOAD, dict(form, url=base))
    await open_form(page, app_url, form)
    vals = await page.evaluate(VALUES_JS)
    for key, col in (form.get("prefill_columns") or {}).items():
        want = str(FAKE_PAYLOAD.get(key, ""))
        if not want:
            continue
        landed = any(v.strip() == want for v in vals["fields"].values()) or \
            any(want.lower() in r.lower() for r in vals["radios"].values())
        report["app_url"][key] = {"column": col, "landed": landed}

    # Pass 2: every discoverable column, one distinctive value each.
    params: dict[str, str] = {}
    expect: dict[str, tuple[str, str]] = {}   # column -> (value, description)
    n = 1
    for f in fields:
        col = column_id(f)
        opts = scan["dropdowns"].get(f["label"] or f["id"] or f["name"])
        value = fake_value_for(f, n, opts)
        params[col] = value
        expect[col] = (value, f["label"] or f["id"] or f["name"])
        n += 1
    for name, options in radio_groups.items():
        col = column_id({"id": name})
        if col and options and options[0]:
            params[col] = options[0]
            expect[col] = (options[0], f"radio {name}")
    test_url = base + "?" + urlencode(params)
    await open_form(page, test_url, form)
    vals = await page.evaluate(VALUES_JS)
    for col, (value, desc) in expect.items():
        landed = any(v.strip() == value for v in vals["fields"].values()) or \
            any(value.lower() in r.lower() for r in vals["radios"].values())
        report["discovered"][col] = {"field": desc, "sent": value, "landed": landed}

    # Suggest prefill_columns: payload keys whose text_fields/radio_groups IDs map to a
    # column that accepted a value.
    working = {c for c, r in report["discovered"].items() if r["landed"]}
    for key, fid in (form.get("text_fields") or {}).items():
        col = column_id({"id": fid})
        if col in working:
            report["suggested_prefill_columns"][key] = col
    for key, group in (form.get("radio_groups") or {}).items():
        col = column_id({"id": group})
        if col in working:
            report["suggested_prefill_columns"][key] = col
    return report


def print_inspect(rep: dict) -> None:
    print(f"Form: {rep['url']}\n")
    print("FIELDS (id / name / label):")
    for f in rep["fields"]:
        col = column_id(f)
        print(f"  [{f['kind']:<8}] id={f['id'] or '-'}  name={f['name'] or '-'}  label={f['label'] or '-'}"
              f"{'  required' if f['required'] else ''}{f'  column={col}' if col else ''}")
    print("\nDROPDOWN OPTIONS:")
    for label, opts in rep["dropdowns"].items():
        print(f"  {label}:")
        for o in opts:
            print(f"    - {o}")
    print("\nRADIO GROUPS:")
    for name, opts in rep["radio_groups"].items():
        print(f"  {name}  (column={column_id({'id': name}) or '?'})")
        for o in opts:
            print(f"    - {o}")
    if rep["follow_ups"]:
        print("\nFOLLOW-UP QUESTIONS (what appears after picking each Reason Code):")
        for option, new in rep["follow_ups"].items():
            print(f"  {option}:")
            if not new:
                print("    (nothing new appears)")
            for f in new:
                ident = f.get("name") or f.get("id")
                print(f"    - {f['kind']} {ident}  label={f.get('label') or '-'}")
                for o in f.get("options", []):
                    print(f"        - {o}")
    print("\nBUTTONS ON THE PAGE:", ", ".join(rep["buttons"]))
    print("\nNothing was submitted.")


def print_prefill(rep: dict) -> None:
    print(f"Form: {rep['url']}\n")
    print("A) The app's prefill_url with a fake payload — did each prefill_columns entry land?")
    for key, r in rep["app_url"].items():
        print(f"  {'YES' if r['landed'] else 'no '}  {key:<20} -> {r['column']}")
    print("\nB) Every column ID visible on the page, sent with a distinctive fake value:")
    for col, r in rep["discovered"].items():
        print(f"  {'YES' if r['landed'] else 'no '}  {col:<28} {r['field']}  (sent {r['sent']!r})")
    print("\nSuggested state_form.yaml -> prefill_columns (only what worked):")
    if rep["suggested_prefill_columns"]:
        for key, col in rep["suggested_prefill_columns"].items():
            print(f"  {key}: {col}")
    else:
        print("  (none landed — URL prefill doesn't work on this form; see README 'Roadmap' for the extension fallback)")
    print("\nNothing was submitted.")


async def run(action: str, cfg, headless: bool, out: str | None, url: str | None = None) -> dict:
    from playwright.async_api import async_playwright
    from .browser import launch
    async with async_playwright() as pw:
        browser = await launch(pw, headless=headless, channel=cfg.settings["browser"].get("channel"))
        page = await browser.new_page()
        try:
            if action == "inspect":
                rep = await inspect(page, cfg.form, url)
                print_inspect(rep)
            else:
                rep = await prefill_test(page, cfg.form, url)
                print_prefill(rep)
        finally:
            if not headless:
                print("\nClose the browser window to finish.")
                try:
                    while not page.is_closed():
                        await asyncio.sleep(0.5)
                except Exception:
                    pass
            await browser.close()
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2)
        print(f"Saved {out}")
    return rep
