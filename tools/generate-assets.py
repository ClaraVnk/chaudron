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
import shutil
import struct
import sys
from pathlib import Path
from typing import Final

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
) -> Path:
    """Render one page to `out` and return it.

    Deliberately returns the path instead of reporting the file size here:
    `Path.stat()` is a blocking syscall, and calling it inside a coroutine stalls
    the event loop (ruff ASYNC240). Reporting happens synchronously afterwards.
    """
    page = await browser.new_page(
        viewport={"width": width, "height": height}, device_scale_factor=1
    )
    await page.set_content(html)
    # Gradients occasionally paint a frame late; a short settle avoids banding.
    await page.wait_for_timeout(150)
    await page.screenshot(path=str(out), omit_background=transparent)
    await page.close()
    return out


def report(paths: list[Path]) -> None:
    for path in paths:
        sys.stdout.write(f"  {path.relative_to(ROOT)}  {path.stat().st_size} bytes\n")


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
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        body += blob
    out.write_bytes(header + entries + body)
    sys.stdout.write(
        f"  {out.relative_to(ROOT)}  {out.stat().st_size} bytes  ({len(pngs)} resolutions)\n"
    )


async def render_all() -> list[Path]:
    full = (ASSETS / "icon.svg").read_text()
    small = (ASSETS / "icon-small.svg").read_text()
    logo = (ASSETS / "logo.svg").read_text()
    written: list[Path] = []

    executable = find_chromium()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=executable)

        for size in (16, 32, 48):
            mark = small if size <= SMALL_MARK_MAX_PX else full
            written.append(
                await shoot(
                    browser,
                    page_html(mark, size, size),
                    size,
                    size,
                    ASSETS / f"favicon-{size}.png",
                )
            )

        # Opaque, for the same reason `apple-touch-icon` below is — and this is
        # where that reasoning was missing.
        #
        # These two are the manifest's `purpose: "any"` icons, so they are what
        # iOS uses for a home-screen shortcut to a page inside `scope`, in
        # preference to the touch icon. Transparent, they were composited onto
        # black; the mark is dark charcoal, so it vanished, and iOS drew its own
        # letter fallback instead. The bug reads as "the favicon does not work"
        # and is really "the icon is there and invisible".
        #
        # `page_html` paints the background, so the PNG carries it rather than
        # relying on whatever composites it.
        for size in (192, 512):
            written.append(
                await shoot(
                    browser,
                    page_html(full, size, size, background="#F7F3EC"),
                    size,
                    size,
                    ASSETS / f"icon-{size}.png",
                    transparent=False,
                )
            )

        # Opaque: iOS composites a transparent touch icon onto black.
        written.append(
            await shoot(
                browser,
                page_html(full, 180, 180),
                180,
                180,
                ASSETS / "apple-touch-icon.png",
                transparent=False,
            )
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
        written.append(
            await shoot(
                browser,
                maskable,
                512,
                512,
                ASSETS / "icon-maskable-512.png",
                transparent=False,
            )
        )

        # README header: mark + wordmark only. The tagline is real text in the
        # README, so baking it in here would show it twice.
        wordmark = (ASSETS / "wordmark.svg").read_text()
        written.append(
            await shoot(
                browser,
                page_html(wordmark, 660, 213),
                660,
                213,
                ASSETS / "wordmark.png",
            )
        )

        written.append(
            await shoot(browser, page_html(logo, 720, 212), 720, 212, ASSETS / "logo.png")
        )

        # 1200x630, and the numbers are not arbitrary: that is 1.91:1, the ratio
        # Facebook, LinkedIn and X lay their large cards out on. This was
        # 1280x640 -- a 2.00 ratio, close enough to look right in a file manager
        # and wrong everywhere it is actually used, because each platform
        # centre-crops to 1.91:1 and takes roughly 4.5% off the height. With the
        # logo centred that is survivable; with anything near the top or bottom
        # edge it is a beheading, and it only shows up once the link is posted.
        #
        # The logo keeps its share of the frame: 880/1280 of the width becomes
        # 825/1200, and its height follows its own aspect ratio (3.398:1) rather
        # than being scaled independently.
        social = (
            "<!doctype html><meta charset=utf-8><style>html,body{margin:0}"
            ".c{width:1200px;height:630px;"
            "background:linear-gradient(135deg,#FBF7F0,#F2E9DC);"
            "display:flex;align-items:center;justify-content:center}"
            "svg{width:825px;height:243px;display:block}"
            "</style><div class=c>" + logo + "</div>"
        )
        written.append(
            await shoot(
                browser,
                social,
                1200,
                630,
                ASSETS / "social-preview.png",
                transparent=False,
            )
        )

        await browser.close()

    return written


#: The subset of `assets/` the site actually serves, and where it has to land.
#: Everything else here is a source artefact: the SVGs, the two wordmark
#: renderings, the three favicon sizes that only exist to be packed into the
#: `.ico`.
#:
#: This list exists because the copy used to be manual, and manual worked right
#: up until it didn't: `social-preview.png` was regenerated at 1200x630 and
#: `frontend/public/` kept serving the 1280x640 one, so the Open Graph card in
#: the shipped page was the old image while the file beside it was the new one.
#: Nothing reports that. The two are only ever compared by whoever happens to
#: post the link.
_WEB_SERVED: Final = (
    "favicon.ico",
    "icon.svg",
    "icon-192.png",
    "icon-512.png",
    "icon-maskable-512.png",
    "apple-touch-icon.png",
    "social-preview.png",
)


def sync_public() -> list[Path]:
    """Copy the served assets into `frontend/public/`, returning what changed."""
    public = ROOT / "frontend" / "public"
    changed: list[Path] = []
    for name in _WEB_SERVED:
        source, target = ASSETS / name, public / name
        if not source.exists():
            raise SystemExit(f"{source} is missing; generation did not complete")
        if not target.exists() or target.read_bytes() != source.read_bytes():
            shutil.copyfile(source, target)
            changed.append(target)
    return changed


def main() -> None:
    report(asyncio.run(render_all()))
    build_ico(
        [(s, ASSETS / f"favicon-{s}.png") for s in (16, 32, 48)],
        ASSETS / "favicon.ico",
    )
    updated = sync_public()
    if updated:
        sys.stdout.write("\nCopied into frontend/public/:\n")
        for path in updated:
            sys.stdout.write(f"  {path.relative_to(ROOT)}\n")
    else:
        sys.stdout.write("\nfrontend/public/ already matches assets/\n")


if __name__ == "__main__":
    main()
