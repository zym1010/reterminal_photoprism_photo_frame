# ESPHome firmware

Firmware for the Seeed reTerminal E1002 (ESP32-S3, 7.3" Spectra 6 color e-paper). Two LVGL pages,
each just a full-screen image fetched from the `photo-frame-bridge` service - no on-device JSON
parsing or native text widgets:

- **Photo page** - a random photo from whichever photo source is currently selected.
- **Dashboard page** - weather or PhotoPrism library stats (also rendered as images, by the
  bridge; see `../photo-frame-bridge/README.md` for the weather source and why it's the one
  piece of this project that isn't fully self-hosted).

## How source selection works

The bridge exposes `GET /photo.png?index=N` and `GET /dashboard.png?index=N`; the source shown
is `sources[N % len(sources)]`, computed **server-side, fresh on every request**. The device
never knows how many sources exist in either category - it just keeps two ever-increasing
counters (`photo_index`, `dashboard_index`) and bumps one by 1 per relevant button press. The
bridge does the wraparound using whatever its *current* source list is, so adding a source there
(e.g. a new `adhoc_images` subfolder) never requires touching this firmware. See the bridge
README for the full explanation.

Each button press rewrites the relevant `online_image`'s URL at runtime via
`online_image.set_url` (a lambda building `.../photo.png?index=<counter>`), which **triggers its
own download automatically** - don't follow it with an explicit `component.update:` on the same
image. Doing so was tried and observed to actively break things: the extra call races the
auto-triggered download, gets rejected by `online_image`'s own concurrency guard ("Image already
being updated"), and that collision corrupts the completion callback chain - the download
finishes successfully but `on_download_finished` never fires, so nothing ever reaches the
display (symptom: pressing a button seems to do nothing, sometimes needing several presses).
`component.update:` is still correct - required, even - anywhere that *isn't* preceded by
`set_url` on the same image (the green button's refresh, and the initial `on_boot` fetch).

## Buttons

| Button | Pin | Action |
|--------|-----|--------|
| Green  | GPIO3 | Refresh whatever's currently showing - new random photo if on the photo page, fresh data if on the dashboard page. Never changes source. |
| White  | GPIO4 | Cycle to the next dashboard source |
| White  | GPIO5 | Cycle to the next photo source |

A `current_mode` global (0 = photo, 1 = dashboard) tracks which category is active so the green
button knows which image to refresh. Physical left/right for the two white buttons may be
swapped from what's listed here depending on the unit; swap the GPIO4/GPIO5 pin numbers in the
YAML if so.

Cycling (GPIO4/GPIO5) downloads the new source's image from the bridge (a second or two), *then*
pushes the result to the panel. The green button does the same for the current source - except
on the photo page, where "refresh" means a fresh random pick from the *same* source (not the next
one).

## A note on the panel's refresh speed

**A full refresh of this 6-color Spectra panel takes roughly 30 seconds.** This is a hardware
property of the panel itself (see `Display update took NNNN ms` in the logs), not something this
firmware controls. It has two consequences worth knowing about:

- **Button presses during an in-progress refresh are ignored entirely, not queued.** The
  `epaper_spi` driver's `update()` call rejects (logs an error, does nothing) if you call it
  again while a refresh is already running - there's no built-in "do it after this one
  finishes." Every refresh (both buttons and the periodic photo timer) goes through a
  `script: mode: single` wrapper (`refresh_display` in the YAML) that tracks the ~30-35s busy
  window. Each button's `on_press` checks `script.is_running: refresh_display` **before doing
  anything else** - if a refresh is in progress, the press is a complete no-op: no source
  cycling, no counter increment, no download. This matters because the counters
  (`photo_index`/`dashboard_index`) drive what the *next* request will show - incrementing them
  during a dropped refresh would desync the device's idea of "current index" from what's
  actually on screen, since the push that would have made them match never happened. In
  practice: if you press a button and nothing visibly happens, the panel was still finishing a
  previous refresh - wait about 30 seconds and press again, and that press is guaranteed to
  count.
- **This is why the dashboard page has no background timer** - a periodic refresh could silently
  eat a button press that arrived during it, on a page you weren't even looking at. Keeping it
  on-demand-only avoids that entirely.

If you want faster perceived response, the main lever is the photo page's `update_interval`
(currently `20min`) - a shorter interval means more frequent 30-second refresh windows where a
button press might land and get dropped; a longer interval means fewer of them.

Adapted from the ["Seeed reTerminal Art Display"](https://github.com/GuySie/random-things) config
by Guy Sie (itself building on work by Paul Krischer), which uses the `epaper_spi` component +
LVGL, including the `online_image.set_url` pattern used here for dynamic URLs. Simplified/adapted
for this project: no numbered-file browsing, no deep sleep (device is USB powered).

## Setup

1. Copy `secrets.yaml.example` to `secrets.yaml` and fill in your WiFi credentials. Generate your
   own random values for the API/OTA/AP passwords (commands included in the example file).
   `secrets.yaml` is gitignored - never commit it.
2. Edit the `bridge_url` substitution at the top of `eink-photo-frame.yaml` to point at your own
   `photo-frame-bridge` instance's LAN address (used to build both image URLs).
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
