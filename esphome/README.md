# ESPHome firmware

Firmware for the Seeed reTerminal E1002 (ESP32-S3, 7.3" Spectra 6 color e-paper). Two LVGL pages,
both just full-screen images fetched from the `photo-frame-bridge` service - no on-device JSON
parsing or native text widgets:

- **Slideshow page** - a random photo, refreshing on a timer or on demand.
- **Stats page** - a PhotoPrism library stats card (also rendered as an image, by the bridge).

Buttons:

| Button | Pin | Action |
|--------|-----|--------|
| Green  | GPIO3 | Fetch a new random photo on demand |
| White  | GPIO4 | Switch to the slideshow page |
| White  | GPIO5 | Switch to the stats page (fetches fresh numbers first) |

The stats page has no periodic `update_interval` - it only fetches when you press the button, so
viewing the slideshow never triggers an unrelated background refresh/flash. Physical left/right
for the two white buttons may be swapped from what's listed here depending on the unit; swap the
GPIO4/GPIO5 pin numbers in the YAML if so.

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
