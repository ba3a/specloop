# specloop brand assets

The logo, favicons and social preview image of specloop. Nothing here is installed into projects.

An ouroboros biting its own tail: the loop that keeps going until the work is done. The three chevrons cut into its body point in the direction of travel and stand for the phases: plan, implement, review.

## Colors

| Name | Hex | Use |
|------|-----|-----|
| Orange | `#F08A50` | Tile background, the snake on dark backgrounds |
| Navy | `#1B2433` | The snake, the "spec" half of the wordmark |
| Rust | `#C2521C` | The "loop" half of the wordmark on light backgrounds |
| Cream | `#FFF4EA` | The eye |

## Files

`export_png.py` writes `favicon.ico` and every PNG listed here. Only the PNGs the repository uses are committed (`png/logo-512.png`, `png/og.png`, `png/lockup.png`, `png/lockup-dark.png`); run the script to get the rest.

| File | Use |
|------|-----|
| `logo.svg`, `png/logo-512.png` | The main logo: navy snake on an orange tile |
| `logo-dark.svg`, `png/logo-dark-512.png` | Orange snake on a navy tile, for dark backgrounds |
| `mark.svg`, `png/mark-512.png` | Navy snake on a transparent background, for light pages (README header, docs) |
| `mark-light.svg`, `png/mark-light-512.png` | Orange snake on a transparent background, for dark pages |
| `lockup.svg`, `png/lockup.png` | Logo and wordmark, for light backgrounds |
| `lockup-dark.svg`, `png/lockup-dark.png` | Logo and wordmark, for dark backgrounds |
| `favicon.ico` | Browser favicon with 16, 32 and 48 px images |
| `favicon.svg` | Scalable favicon for modern browsers: thicker body, one chevron, no eye |
| `favicon-16.svg`, `png/favicon-16.png` | The 16 px drawing: ring, head and bite only |
| `png/favicon-32.png` | 32 px favicon |
| `apple-touch-icon.svg`, `png/apple-touch-icon.png` | iOS home screen icon, 180 px, square (iOS rounds the corners) |
| `png/icon-192.png`, `png/icon-512.png` | Web app manifest icons |
| `og.svg`, `png/og.png` | Social preview card, 1200 × 630 |

Do not use the full logo below 48 px: the chevrons and the eye turn into noise. Use the favicon drawings instead.

## Favicon markup

```html
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
```

## Regenerating

The SVGs are generated; edit `build_svg.py` rather than the SVG files.

```bash
cd assets/brand
python build_svg.py .
python export_png.py
```

`export_png.py` needs Chrome and ffmpeg. Set `CHROME` or `FFMPEG` if they are not found.

The lockup and the social card use JetBrains Mono, Inter and Segoe UI as live text, so the SVG versions only look right where those fonts are installed. Use the PNGs, or convert the text to outlines in a vector editor, before using the SVGs elsewhere.
