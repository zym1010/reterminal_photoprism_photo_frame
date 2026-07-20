# ESPHome firmware

Firmware for the Seeed reTerminal E1002 (ESP32-S3, 7.3" Spectra 6 color e-paper). Two LVGL pages,
both just full-screen images fetched from the `photo-frame-bridge` service - no on-device JSON
parsing or native text widgets:

- **Slideshow page** - a random photo, refreshing on a timer or on demand.
- **Stats page** - a PhotoPrism library stats card (also rendered as an image, by the bridge).

## Buttons

| Button | Pin | Action |
|--------|-----|--------|
| Green  | GPIO3 | Slideshow page if not already there; if already there, fetch a new random photo |
| White  | GPIO4 | Unassigned - reserved for a future page |
| White  | GPIO5 | Switch to the stats page, fetching fresh numbers first |

The green button's two behaviors are collapsed onto one button because they're mutually
exclusive in practice: there's no reason to "go to slideshow" while already on it, so that press
is repurposed to mean "refresh" instead. A `current_page` global tracks which page is active
(0 = slideshow, 1 = stats) since ESPHome/LVGL doesn't expose a way to query the active page
directly - update it if you add more pages.

The stats page has no periodic `update_interval` - it only fetches when you press GPIO5, so
viewing the slideshow never triggers an unrelated background refresh/flash. Physical left/right
for the two white buttons may be swapped from what's listed here depending on the unit; swap the
GPIO4/GPIO5 pin numbers in the YAML if so.

Pressing GPIO5 (stats) does two things in sequence: it downloads `/stats.png` from the bridge
(a second or two), *then* pushes the result to the panel. Pressing the green button while already
on the slideshow does the same (fetch, then push) for `/frame.png`; pressing it from the stats
page just pushes the slideshow's already-loaded photo - no network round-trip, so it responds
faster.

## A note on the panel's refresh speed

**A full refresh of this 6-color Spectra panel takes roughly 30 seconds.** This is a hardware
property of the panel itself (see `Display update took NNNN ms` in the logs), not something this
firmware controls. It has two consequences worth knowing about:

- **Button presses during an in-progress refresh are ignored, not queued.** The `epaper_spi`
  driver's `update()` call rejects (logs an error, does nothing) if you call it again while a
  refresh is already running - there's no built-in "do it after this one finishes." To avoid that
  error spamming the logs and to make the behavior predictable, every refresh (both buttons and
  the periodic photo timer) goes through a `script: mode: single` wrapper (`refresh_display` in
  the YAML) that silently drops overlapping requests instead. In practice: if you press a button
  and nothing visibly happens, the panel was still finishing a previous refresh - wait about 30
  seconds and press again.
- **This is why the stats page has no background timer** - a periodic stats refresh could
  silently eat a button press that arrived during it, on a page you weren't even looking at.
  Keeping it on-demand-only avoids that entirely.

If you want faster perceived response, the main lever is the photo slideshow's `update_interval`
(currently `20min`) - a shorter interval means more frequent 30-second refresh windows where a
button press might land and get dropped; a longer interval means fewer of them.

Adapted from the ["Seeed reTerminal Art Display"](https://github.com/GuySie/random-things) config
by Guy Sie (itself building on work by Paul Krischer), which uses the `epaper_spi` component +
LVGL. Simplified for this project: no numbered-file browsing and no deep sleep (device is USB
powered) - the bridge already returns a different random photo on every fetch, so the device just
re-downloads the same URL on a timer.

## Setup

1. Copy `secrets.yaml.example` to `secrets.yaml` and fill in your WiFi credentials. Generate your
   own random values for the API/OTA/AP passwords (commands included in the example file).
   `secrets.yaml` is gitignored - never commit it.
2. Edit `eink-photo-frame.yaml` and update both `image:` entries' `url` (`/frame.png` and
   `/stats.png`) to point at your own `photo-frame-bridge` instance's LAN address.
3. Install ESPHome and flash over USB (first flash only - OTA works after that):
   ```bash
   python3 -m venv .esphome-venv
   .esphome-venv/bin/pip install esphome
   .esphome-venv/bin/esphome run eink-photo-frame.yaml
   ```

## Notes

- `model: Seeed-reTerminal-E1002` in the `epaper_spi` display block auto-configures the panel's
  CS/DC/RESET pins - no manual pin mapping needed.
- Requires ESPHome 2025.11.1+ (uses the official `epaper_spi` Spectra 6 support).
- The panel's on-device color driver does **not** do real nearest-color matching - it buckets
  each pixel into one of the 6 native ink colors using a naive RGB-cube-corner threshold test. See
  `../photo-frame-bridge/README.md` for why images must be pre-dithered to pure/idealized
  primaries (not "realistic" muted ink colors) for correct on-device colors.
