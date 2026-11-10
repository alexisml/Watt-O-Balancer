# Brand images

Home Assistant shows an icon and a logo for every integration — in the
integrations list, in the "Add integration" dialog, on each config entry card
and in the repair and diagnostics flows. This document records the brand, where
the images live, how to rebuild them, and how to submit them upstream.

## The brand

Watt-O-Balancer is named after the `-O-` product names of the 1950s-70s
(Veg-O-Matic and friends), so the artwork is deliberately period: a harvest
gold, burnt orange, rust and avocado palette on dark brown and cream, a
270-degree gauge dial borrowed from a 70s hi-fi VU meter, and a fat slab
wordmark set in American Typewriter.

On the dial face sits a pre-war coupe in profile -- long hood, upright cabin,
sweeping fenders over big wheels -- with a ray struck down the centre behind
it, which is what makes the mark read as *EV* load balancing rather than
electricity in general. The gauge spindle is a pale triangle riding on the
coloured band with its apex pointing outwards, rather than a needle pivoting
from the centre, which keeps the face clear for the car and the ray. It is pale
rather than dark so that it stays visible where it crosses the band instead of
reading as a notch cut out of it.
All three shapes are deliberately chunky, because the icon has to survive being
drawn at 24px in the integrations list. In the logo the whole dial stands in
for the middle "O" of WATT-O-BALANCER, which is also what the icon is on its
own.

| Token | Hex |
| --- | --- |
| Cream | `#F4E3C1` |
| Harvest gold | `#E8A33D` |
| Burnt orange | `#C75B29` |
| Rust | `#A33B1F` |
| Avocado | `#7D8C44` |
| Dark brown | `#3B2416` |

## Where the images live

| Path | Purpose |
| --- | --- |
| `scripts/build_brand_images.py` | The artwork itself, as code. Edit this to change the brand. |
| `assets/brand/*.svg` | Generated vector sources, written by the script. |
| `custom_components/watt_o_balancer/brand/*.png` | Shipped with the integration. Home Assistant 2026.3+ serves these directly. |

The set is:

| File | Size | Notes |
| --- | --- | --- |
| `icon.png` | 256x256 | The dial and its classic EV, on a rounded brown square. |
| `icon@2x.png` | 512x512 | hDPI icon. |
| `logo.png` | 1637x256 | Wordmark with the dial as its "O", for light backgrounds. |
| `logo@2x.png` | 3274x512 | hDPI logo. |
| `dark_logo.png` | 1637x256 | Same, with cream lettering for dark backgrounds. |
| `dark_logo@2x.png` | 3274x512 | hDPI dark logo. |

There is deliberately no `dark_icon.png`: the icon is a self-contained dark
panel, so it reads on both themes, and Home Assistant falls back to the light
variant when a dark one is absent.

## Rebuilding

```bash
python3 scripts/build_brand_images.py
```

Takes about ten seconds and rewrites both `assets/brand/` and
`custom_components/watt_o_balancer/brand/`.

The script is macOS-only and needs Google Chrome installed, which is the one
part worth explaining. Rasterising through `qlmanage` needs no extra tooling,
but Quick Look composites onto opaque white, so every PNG came out with a solid
white background — fine on the light theme, a white slab on the dark one.
Headless Chrome with `--default-background-color=00000000` gives true alpha.
Chrome also squashes the page when the window is very short, so each artboard
is rendered into a comfortably oversized window and the surplus is trimmed off
afterwards by a small pure-stdlib PNG trimmer in the same script. That trim is
also what satisfies the "no padding" rule below, and it is how text runs are
measured: a run is rendered on its own and trimmed to learn its exact ink
width, which is what positions the rule under the wordmark. The logo is then
rendered a second time at a corrected scale so its shortest side lands exactly
on 512px.

## Home Assistant brand rules

From the [brand images documentation](https://contributors.home-assistant.io/core/integrations/brands/)
and the [brands repository README](https://github.com/home-assistant/brands):

- PNG only, lossless, transparency preferred, trimmed with no padding.
- Icons are square: 256x256 and 512x512.
- Logos keep the brand aspect ratio, landscape preferred, shortest side
  128-256 px (256-512 px for `@2x`). Ours uses the top of that range.
- A missing logo falls back to the icon; a missing dark variant falls back to
  the light one.
- Custom integrations must not use Home Assistant branded imagery, so users do
  not mistake them for an official integration. Ours does not.

## Submitting to home-assistant/brands

Since Home Assistant 2026.3 the local `brand/` folder is enough and the brands
repository calls `custom_integrations/` a legacy folder. This integration still
declares `"homeassistant": "2024.7.0"` in `hacs.json`, so users on older cores
only get images from the CDN. Submitting upstream covers them.

The upstream directory name must match the integration domain in
`manifest.json` — that is `watt_o_balancer`, **not** the old `ev_lb`. As of this
writing neither `custom_integrations/watt_o_balancer` nor `core_integrations/watt_o_balancer`
exists upstream.

1. Fork and clone the brands repository:

   ```bash
   gh repo fork home-assistant/brands --clone --remote
   ```

2. From this repository, copy the rendered PNGs into the fork:

   ```bash
   mkdir -p ../brands/custom_integrations/watt_o_balancer && cp custom_components/watt_o_balancer/brand/*.png ../brands/custom_integrations/watt_o_balancer/
   ```

3. Commit and open the pull request:

   ```bash
   cd ../brands && git checkout -b add-watt-o-balancer && git add custom_integrations/watt_o_balancer && git commit -m "Add Watt-O-Balancer (watt_o_balancer)" && gh pr create --fill
   ```

The pull request body should state the integration name, the domain, and a link
to this repository so reviewers can confirm the integration exists.

After the pull request merges, images are cached for 24 hours by Cloudflare and
7 days by browsers, and the Cloudflare cache is flushed on each major Home
Assistant Core release — so expect a delay before they appear.

## If this integration is ever accepted into core

Delete `custom_components/watt_o_balancer/brand/` and submit the same images to
`core_integrations/watt_o_balancer/` in the brands repository instead.
