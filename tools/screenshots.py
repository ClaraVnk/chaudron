#!/usr/bin/env python
"""Drive the running PWA with a real browser and capture the README screenshots.

    uv run --with playwright python tools/screenshots.py

Requires the whole stack up: PostgreSQL, the API, and the Vite dev server. It
takes pictures of a live application talking to a live backend — nothing here
renders a mockup. If a screen cannot be reached, the script fails loudly rather
than saving a half-loaded page, because a screenshot that lies is worse than a
missing one.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import Browser, Page, async_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"

APP_URL = os.environ.get("CHAUDRON_APP_URL", "http://127.0.0.1:5173/")

# iPhone 14-ish. The product is used standing in a kitchen, one-handed; a
# desktop-width capture would misrepresent what it is.
VIEWPORT = {"width": 390, "height": 844}


def find_chromium() -> str | None:
    """Locate a Playwright-managed Chromium, newest build first.

    The pinned Playwright release and the browsers already in the cache do not
    always agree on a build number; launching with an explicit path uses what is
    installed instead of demanding a fresh multi-hundred-megabyte download.
    Returning None lets Playwright resolve it as usual.
    """
    cache = Path.home() / ".cache" / "ms-playwright"
    builds = sorted(
        cache.glob("chromium-*/chrome-linux64/chrome"),
        key=lambda p: int(p.parts[-3].split("-")[-1]),
        reverse=True,
    )
    return str(builds[0]) if builds else None


async def new_page(browser: Browser, scheme: str = "light") -> Page:
    page = await browser.new_page(
        viewport=VIEWPORT,
        device_scale_factor=2,
        color_scheme=scheme,
        locale="fr-FR",
        timezone_id="Europe/Paris",
    )
    page.on("pageerror", lambda exc: sys.stderr.write(f"  page error: {exc}\n"))
    return page


async def settle(page: Page) -> None:
    await page.wait_for_load_state("networkidle")
    # The inventory renders from a debounced fetch; give the list a beat to paint
    # so captures do not catch a spinner.
    await page.wait_for_timeout(900)


async def shoot(page: Page, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    await page.screenshot(path=str(path))
    return path


async def tab(page: Page, label: str) -> None:
    await page.get_by_role("button", name=label).click()
    await settle(page)


async def main() -> None:
    written: list[Path] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=find_chromium())

        page = await new_page(browser)
        await page.goto(APP_URL)
        await settle(page)
        if "Inventaire" not in await page.inner_text("body"):
            raise SystemExit("the inventory screen did not render; is the stack up?")

        # The degraded banner is a real screen and worth its own capture, but it
        # is tall: left at the top of the page it buries the inventory it is
        # warning about. Shoot it first, then scroll past it for the picture of
        # the product itself.
        written.append(await shoot(page, "degraded-banner"))

        await page.evaluate(
            "() => { const b = document.querySelector('input[type=search], input');"
            " if (b) b.scrollIntoView({block: 'start'}); }"
        )
        await page.wait_for_timeout(500)
        written.append(await shoot(page, "inventory"))

        await tab(page, "Recettes")
        written.append(await shoot(page, "recipes"))

        await tab(page, "Ajouter")
        written.append(await shoot(page, "add"))
        await page.close()

        dark = await new_page(browser, scheme="dark")
        await dark.goto(APP_URL)
        await settle(dark)
        written.append(await shoot(dark, "inventory-dark"))
        await dark.close()

        await browser.close()

    for path in written:
        sys.stdout.write(f"  {path.relative_to(ROOT)}  {path.stat().st_size} bytes\n")


if __name__ == "__main__":
    asyncio.run(main())
