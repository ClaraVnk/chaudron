# Brand assets

Everything here is generated from the two SVG sources. Edit the SVG, re-run the
generator, commit both — never hand-edit a PNG.

## Sources

| File | Purpose |
|---|---|
| `icon.svg` | The mark. Full detail: legs, steam, ingredients breaking the surface. Use at 48 px and above. |
| `icon-small.svg` | The same mark reduced for 16–32 px. **Not** a scaled copy — the legs, steam and ingredients are removed and the vessel is enlarged, because below ~48 px those details turn to noise and the icon reads as a grey smudge. |
| `logo.svg` | Horizontal lockup: mark, wordmark, tagline. |

## Generated

| File | Where it goes |
|---|---|
| `favicon.ico` | 16/32/48 in one file, PNG-encoded inside the ICO container |
| `favicon-16.png`, `favicon-32.png`, `favicon-48.png` | Individual sizes, if a target wants PNG rather than ICO |
| `icon-192.png`, `icon-512.png` | PWA manifest icons |
| `icon-maskable-512.png` | PWA maskable icon — opaque background, mark inside the 80 % safe zone so Android's circular crop never clips the handles |
| `apple-touch-icon.png` | 180 px, opaque (iOS composites onto black otherwise) |
| `logo.png` | README header |
| `social-preview.png` | 1280×640 GitHub social card. **Must be uploaded through the web UI** — Settings → General → Social preview. There is no API for it. |

Once the frontend exists, the favicon, the PWA icons and the Apple touch icon
are copied into `frontend/public/` and referenced from the manifest. They live
here in the meantime so the sources and the outputs stay in one place.

## Palette

| Role | Value | Notes |
|---|---|---|
| Cast iron | `#4A4E57` → `#22242A` | Lit from above-left; the belly falls into shadow |
| Rim | `#42464F` | A step lighter than the belly, so the opening separates |
| Contents | `#FFD166` → `#F59E0B` → `#D9480F` | The only saturated element. Everything else is neutral so this reads as heat |
| Steam | `#F0B93B` | Same family as the contents, at 40–70 % opacity |
| Ink (wordmark) | `#22242A` | |
| Muted text — logo only | `#6B7280` | Fine on the logo's own white/neutral ground |
| Muted text — UI | `#5D6470` | **Use this one in the interface.** `#6B7280` measures 4.47:1 against the warm background, below the WCAG AA floor of 4.5:1. Same family, 5.5:1. |

The two muted greys are not a mistake. The lighter one predates any interface
and sits on neutral ground in the lockup, where it passes; the darker one exists
because the same value failed against the warm surface once there was a real UI
to measure it on. If the logo is ever placed on the warm background, it takes
the darker value too.

Wordmark: **URW Gothic** (fallbacks: Century Gothic, Questrial, Futura). Chosen
because its circular letterforms echo the vessel; the serif candidates pulled
the mark towards a cookbook cover.

The wordmark in `logo.svg` is live text, not outlines. Anywhere the font is not
guaranteed, use `logo.png` rather than the SVG.

## Regenerating

Requires a Chromium that Playwright can drive — there is no ImageMagick, rsvg or
Pillow on the reference machine, and the `.ico` is assembled by hand from the
PNGs because of it.

```sh
uv run --with playwright python tools/generate-assets.py
```
