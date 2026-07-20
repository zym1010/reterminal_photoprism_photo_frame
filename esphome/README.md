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

Everything is on-demand only - **no background/periodic refresh of any kind**, on either page.
The device never touches the display unless a button was just pressed.

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

A background slideshow timer (auto-refresh the photo page every N minutes) used to exist here and
was removed - see "Known issue" below for why. It's a reasonable thing to want back, but needs to
be reintroduced carefully (gated on `refresh_busy`, not a bare `online_image` `update_interval`)
rather than just restoring the old approach.

## A note on the panel's refresh speed

**A full refresh of this 6-color Spectra panel takes roughly 30 seconds.** This is a hardware
property of the panel itself (see `Display update took NNNN ms` in the logs), not something this
firmware controls. It has two consequences worth knowing about:

**Button presses during an in-progress refresh are ignored entirely, not queued.** The
`epaper_spi` driver's `update()` call rejects (logs an error, does nothing) if you call it again
while a refresh is already running - there's no built-in "do it after this one finishes." Every
refresh goes through a `script: mode: single` wrapper (`refresh_display` in the YAML) that tracks
the ~30-35s busy window, plus a `refresh_busy` global that's set the instant a button press is
*accepted* and only cleared once that whole cycle finishes (or a download fails - see its
`on_error:` handler). Each button's `on_press` checks `refresh_busy` **before doing anything
else** - if true, the press is a complete no-op: no source cycling, no counter increment, no
download. Gating on this flag rather than only on `script.is_running: refresh_display` matters:
`online_image`'s own "already downloading" guard doesn't become active until it starts decoding,
not during the initial HTTP-connect phase (observed to take up to ~2.5s) - a second press landing
in that earlier window used to slip past every guard and silently overwrite the in-flight
download, abandoning the first request and showing whichever one happened to win the race.
`refresh_busy` closes that gap by blocking from the moment a press is accepted, not from whenever
the download happens to reach the decoding stage. In practice: if you press a button and nothing
visibly happens, the panel was still finishing a previous refresh - wait about 30 seconds and
press again, and that press is guaranteed to count.

## Known issue (resolved): silent failures traced to the old periodic photo timer

For a while, downloads would occasionally complete successfully (`Image fully downloaded...` in
the logs) but nothing would happen after that - no `Display update took...` line, and sometimes a
reboot a few seconds later. The likely explanation: the photo page used to have a background
`update_interval: 20min` slideshow timer, which called `component.update: photo_image` completely
outside the `refresh_busy` gating that every button respects. If it fired around the same time as
a button press, both chains would independently call `lvgl.page.show` and
`script.execute: refresh_display` - whichever one's `lvgl.page.show` ran last, right before the
winning `component.update: epaper_display` call, determined what actually got shown, regardless
of what was pressed. Confirmed symptom matching this theory: the panel getting stuck showing a
photo no matter how many times the dashboard button was pressed.

Fix: removed the periodic timer entirely (see "Buttons" above) - the photo page is now on-demand
only, same as the dashboard page always was, so there's no automatic trigger left to race a
button press. `sdkconfig_options: CONFIG_ESP_TASK_WDT_TIMEOUT_S` (raising the task watchdog
timeout to 15s) is still in the YAML as a low-risk safety margin from when a task-watchdog trip
was also suspected as a contributing cause, but was never confirmed independently (no crash
backtrace could be captured - the device's USB-serial link wasn't passing data through during
testing, despite the network logger working fine). If problems recur with everything now fully
on-demand, that's a real signal the watchdog theory (or something else entirely) needs a working
serial connection to actually diagnose.

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
