#!/usr/bin/env python
"""Rasterise the brand SVGs into every icon the project ships.

Run from the repository root:

    uv run --with playwright python tools/generate-assets.py

Chromium does the rasterising. That is not a preference — the reference machine
has no ImageMagick, no rsvg-convert, no Inkscape and no Pillow, and Playwright's
browser is the only renderer available. The `.ico` is therefore assembled by
hand from the PNGs, which is straightforward: an ICO is a short header plus one
directory entry per image, and PNG payloads are valid inside it.

Never hand-edit the generated PNGs. Edit the SVG, re-run this, commit both.
"""

from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path

from playwright.async_api import Browser, async_playwright

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# Sizes below this use the reduced mark: the legs and steam of the full icon
# collapse into noise, and the result reads as a grey smudge.
SMALL_MARK_MAX_PX = 48


def find_chromium() -> str | None:
    """Locate a Playwright-managed Chromium, newest build first.

    Returning None lets Playwright fall back to its own resolution, which is
    correct when the browser was installed for the current Playwright version.
    """
    cache = Path.home() / ".cache" / "ms-playwright"
    builds = sorted(
        cache.glob("chromium-*/chrome-linux64/chrome"),
        key=lambda p: int(p.parts[-3].split("-")[-1]),
        reverse=True,
    )
    return str(builds[0]) if builds else None


def page_html(svg: str, width: int, height: int, background: str = "transparent") -> str:
    return (
        f"<!doctype html><meta charset=utf-8><style>"
        f"html,body{{margin:0;padding:0;background:{background}}}"
        f"svg{{display:block;width:{width}px;height:{height}px}}"
        f"</style>{svg}"
    )


async def shoot(
    browser: Browser,
    html: str,
    width: int,
    height: int,
    out: Path,
    *,
    transparent: bool = True,
) -> None:
    page = await browser.new_page(
        viewport={"width": width, "height": height}, device_scale_factor=1
    )
    await page.set_content(html)
    # Gradients occasionally paint a frame late; a short settle avoids banding.
    await page.wait_for_timeout(150)
    await page.screenshot(path=str(out), omit_background=transparent)
    await page.close()
    sys.stdout.write(f"  {out.relative_to(ROOT)}  {out.stat().st_size} bytes\n")


def build_ico(pngs: list[tuple[int, Path]], out: Path) -> None:
    """Assemble a multi-resolution ICO from PNG payloads.

    Layout: a 6-byte ICONDIR, then one 16-byte ICONDIRENTRY per image, then the
    payloads. A dimension of 256 is encoded as 0; nothing here reaches that, but
    the modulo keeps the encoder honest if a larger size is ever added.
    """
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries = b""
    body = b""
    for size, path in pngs:
        blob = path.read_bytes()
        entries += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset
        )
        offset += len(blob)
        body += blob
    out.write_bytes(header + entries + body)
    sys.stdout.write(f"  {out.relative_to(ROOT)}  {out.stat().st_size} bytes"
                     f"  ({len(pngs)} resolutions)\n")


async def main() -> None:
    full = (ASSETS / "icon.svg").read_text()
    small = (ASSETS / "icon-small.svg").read_text()
    logo = (ASSETS / "logo.svg").read_text()

    executable = find_chromium()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=executable)

        for size in (16, 32, 48):
            mark = small if size <= SMALL_MARK_MAX_PX else full
            await shoot(
                browser, page_html(mark, size, size), size, size,
                ASSETS / f"favicon-{size}.png",
            )

        for size in (192, 512):
            await shoot(
                browser, page_html(full, size, size), size, size,
                ASSETS / f"icon-{size}.png",
            )

        # Opaque: iOS composites a transparent touch icon onto black.
        await shoot(
            browser, page_html(full, 180, 180), 180, 180,
            ASSETS / "apple-touch-icon.png", transparent=False,
        )

        # Maskable: the mark occupies ~66% of the canvas so Android's circular
        # and squircle crops never reach the handles.
        maskable = (
            "<!doctype html><meta charset=utf-8><style>"
            "html,body{margin:0;background:#F7F3EC}"
            ".w{width:512px;height:512px;display:flex;align-items:center;"
            "justify-content:center}svg{width:340px;height:340px;display:block}"
            "</style><div class=w>" + full + "</div>"
        )
        await shoot(
            browser, maskable, 512, 512,
            ASSETS / "icon-maskable-512.png", transparent=False,
        )

        # README header: mark + wordmark only. The tagline is real text in the
        # README, so baking it in here would show it twice.
        wordmark = (ASSETS / "wordmark.svg").read_text()
        await shoot(
            browser, page_html(wordmark, 660, 213), 660, 213, ASSETS / "wordmark.png"
        )

        await shoot(browser, page_html(logo, 720, 212), 720, 212, ASSETS / "logo.png")

        social = (
            "<!doctype html><meta charset=utf-8><style>html,body{margin:0}"
            ".c{width:1280px;height:640px;"
            "background:linear-gradient(135deg,#FBF7F0,#F2E9DC);"
            "display:flex;align-items:center;justify-content:center}"
            "svg{width:880px;height:259px;display:block}"
            "</style><div class=c>" + logo + "</div>"
        )
        await shoot(
            browser, social, 1280, 640,
            ASSETS / "social-preview.png", transparent=False,
        )

        await browser.close()

    build_ico(
        [(s, ASSETS / f"favicon-{s}.png") for s in (16, 32, 48)],
        ASSETS / "favicon.ico",
    )


if __name__ == "__main__":
    asyncio.run(main())
