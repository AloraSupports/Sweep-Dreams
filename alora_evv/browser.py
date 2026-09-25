"""Browser launching and the click safety guard.

The guard checks the text of the element actually being clicked (not a label
the code passes in), so a page change can't trick it into pressing Submit.
"""
from __future__ import annotations

import re

# Never clicked by this app, anywhere. Submit is always a person's job.
FORBIDDEN_FORM = re.compile(r"\b(submit|save as draft|sign)\b", re.I)
FORBIDDEN_PORTAL = re.compile(r"\b(submit|save|new claim|rematch|restore|delete|void|approve)\b", re.I)


class SafetyStop(RuntimeError):
    pass


async def element_text(locator) -> str:
    return (await locator.evaluate(
        "el => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()"
    ) or "")[:200]


async def safe_click(locator, forbidden: re.Pattern, **kw) -> None:
    text = await element_text(locator)
    if forbidden.search(text):
        raise SafetyStop(f"SAFETY GUARD: refusing to click '{text}'")
    await locator.click(**kw)


async def launch(pw, headless: bool, channel: str | None):
    """Prefer the installed Google Chrome; fall back to Playwright's Chromium."""
    if channel:
        try:
            return await pw.chromium.launch(headless=headless, channel=channel)
        except Exception as e:  # Chrome not installed, etc.
            print(f"note: couldn't start {channel} ({str(e).splitlines()[0]}); "
                  "trying Playwright's Chromium")
    try:
        return await pw.chromium.launch(headless=headless)
    except Exception as e:
        raise SystemExit("No browser available. Install Google Chrome, or run:\n"
                         "  python -m playwright install chromium") from e
