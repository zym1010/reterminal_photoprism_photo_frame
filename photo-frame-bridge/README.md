# photo-frame-bridge

Small self-hosted service with two generic, index-based endpoints:

- `GET /photo.png?index=N` → a random photo from photo source `N` (server picks
  `sources[N % len(sources)]`), cropped to 800x480 and dithered to the reTerminal E1002's 6
  native ink colors. Sources: `["photoprism"]` (PhotoPrism favorites) plus every immediate
  subfolder of the `adhoc_images` mount (see below) - discovered fresh on every request, so
  dropping a new folder into the mount adds a source with no redeploy needed.
- `GET /dashboard.png?index=N` → dashboard source `N` (weather, PhotoPrism library stats, ...),
  also rendered as an image using the same pipeline.

The device never needs to know how many sources exist in either category — it just sends an
ever-increasing counter, and the bridge does the `% len(sources)` wraparound server-side using
whatever the *current* source list actually is. See `esphome/README.md` for how the device side
uses this.

Runs entirely on the LAN — no cloud, no PhotoPrism auth required (this instance has none
configured), except for the weather and Todoist dashboard sources' external calls (see below).
Everything is rendered as an image rather than exposed as JSON so the ESPHome side stays dead
simple: no on-device JSON parsing, just two `online_image`s (one per category) whose URLs get
rewritten at runtime.

## Photo sources: PhotoPrism + `adhoc_images` subfolders

`ADHOC_IMAGES_DIR` (default `/data/adhoc_images`) is meant to be a NAS shared folder mounted into
the container. Every **immediate** subfolder becomes a photo source automatically - e.g. mount a
shared folder containing a `kid_photos/` subfolder, and `kid_photos` shows up as source 1 (source
0 is always `photoprism`) without touching any code. Adding a third source later is just: create
another top-level subfolder. Within a source, images are picked recursively from any depth of
further nesting (e.g. `kid_photos/2026/vacation/photo.jpg` counts as part of the `kid_photos`
source, not a separate one) - organize each source's folder however you like on disk. NAS
housekeeping folders (`@eaDir`, `#recycle`, dotfolders) are skipped at every level.

Supported formats: JPEG, PNG, WEBP, AVIF, BMP, TIFF, HEIC/HEIF (AVIF needs the
`pillow-avif-plugin` dependency already in `requirements.txt` - stock Pillow doesn't decode AVIF
on its own).

## Dithering: idealized colors, not "realistic" ones

It's tempting to dither to a *measured* Spectra 6 palette (the panel's actual, somewhat muted
ink colors, e.g. green ≈ `(40, 82, 57)`) for a more realistic preview. **Don't** — the E1002's
on-device `epaper_spi` driver does not do real nearest-color matching against a rich palette. It
classifies each pixel into one of 8 RGB-cube corners with a naive threshold: each channel counts
as "on" only if it's > 128, and any pixel whose max/min channel spread is < 50 collapses straight
to black-or-white by luminance. A muted, measured green like `(40, 82, 57)` has only a 42-point
spread, so it gets misread as black on the actual hardware — same for muted reds. The fix is to
dither to pure, fully-saturated primaries (`(0,0,0)`, `(255,255,255)`, `(255,255,0)`, `(255,0,0)`,
`(0,0,255)`, `(0,255,0)` - see `DEVICE_PALETTE` in `app.py`), which the on-device classifier reads
back correctly every time. To claw back the perceptual quality that pure primaries would otherwise
sacrifice, we use `DitherMode.ATKINSON` with `tone="auto"` and `gamut="auto"` compression (only
available when passing a plain `ColorPalette`, not the library's `ColorScheme` enum, which forces
tone/gamut off).

## Dashboard source: `stats` — cheap counts, not a full library scan

Shows: Photos, Videos, Favorites, items Added in the last 30/60/90 days, and the Last Added
timestamp. `photos`, `live`, and `videos` from `count` are mutually exclusive categories that sum
exactly to `count.all` (verified against a live instance), so "Photos" here is `photos + live`
merged into one number rather than a separate "Live Photos" row - not a subset relationship,
just two categories combined for a shorter card. "Places" and "Cameras" were dropped as not
useful for an at-a-glance card.

It's tempting to get a "total photos" count by requesting every photo and counting the array -
PhotoPrism's `X-Count` response header only reflects the true total once your `count` param
exceeds it, so a naive approach ends up downloading the entire library's JSON (tens of MB) just
to count it. Instead:

- `GET /api/v1/config` already carries a precomputed `count` object (`photos`, `videos`, `live`,
  `favorites`, etc.) - a single lightweight request.
- The "last added" timestamp comes from `GET /api/v1/photos?count=1&order=added`, which returns
  one record's `CreatedAt` (when it was imported, not when it was taken) - also a single cheap
  request.
- The 30/60/90-day "added" counts use an undocumented-but-real search filter,
  `q=added:"<RFC3339 timestamp>"` (added at or after this time - confirmed via
  [photoprism/photoprism#4300](https://github.com/photoprism/photoprism/issues/4300), not in the
  official docs). Requesting a large `count` alongside it and reading the `X-Count` header gives
  an exact total without downloading the whole library - cheap in practice because a personal
  library only adds a handful of items in any given month, so the actual response body stays
  small regardless of how large `count` is set.

## Dashboard source: `weather` — the one non-self-hosted piece

A real weather forecast needs an external data source - there's no way around that without
owning your own weather station. This uses [Open-Meteo](https://open-meteo.com/), which needs no
API key or account signup (just lat/long in the request), keeping it as close to "no new cloud
accounts" as a weather feature can get.

Locations are hardcoded in `WEATHER_LOCATIONS` in `app.py` (currently Cupertino CA, Wuhan Hubei,
and Dalian Liaoning) - edit that list to change them. The main temperature/condition shown is
**live current conditions**, re-fetched fresh on every request (not a cached/stale forecast); the
H/L range is just today's expected high/low, same as any phone weather widget shows alongside
current conditions. Each location's time is shown in *that location's own local time* (Open-Meteo
returns `current.time` already localized when `timezone=auto` is passed - no conversion needed on
our end).

## Dashboard source: `todos` — the second non-self-hosted piece

Shows active [Todoist](https://todoist.com/) tasks in two columns (each filled top-to-bottom
before moving to the next, like a newspaper, so the sort order below still reads naturally),
numbered, with the due date on its own underlined line below each task name. There's no realistic
self-hosted alternative to a task manager you actually already use day to day, so this is treated
the same as weather: an explicit, acknowledged exception to "fully self-hosted." Layout follows
TRMNL's own Todoist plugin (a reference screenshot, not just their product page) - flat numbered
list, no project/section grouping - adapted per an explicit ask: **undated tasks are sorted first**,
then dated tasks by due date ascending (each group by priority descending as a tiebreak). Row
height is computed per task (undated tasks take one compact line; dated ones take two, for the due
date) rather than a fixed size, so the mix of undated/dated tasks determines how many actually fit
on screen - there's no single fixed answer to "how many tasks fit."

Requires a personal API token (**Todoist Settings → Integrations → Developer**) set as
`TODOIST_API_TOKEN`. Without it, the card just says so rather than erroring - a missing/empty
token is a normal, expected state (e.g. before you've configured it), not a failure.

Task text color is restricted to `DEVICE_PALETTE`'s pure primaries (red for overdue, blue for
high-priority-but-not-overdue, black otherwise) for the same reason described above under
"Dithering: idealized colors, not realistic ones" - an off-palette color, *including grays*, dithers
into a visibly speckled/near-invisible mess on thin strokes and lines instead of rendering as a
clean solid color. This was tried and confirmed directly: a light-gray column divider and gray
secondary text (index numbers, due-date labels) looked fine before dithering and were nearly gone
after. Pure black is the only safe "de-emphasized" choice here - visual hierarchy for secondary
text comes from size/weight instead. The bundled CJK font (see below) only ships one weight, so
task names are faux-bolded via `stroke_width` rather than a real bold font file. Long task names
are truncated with an ellipsis to fit the column width, and the whole list is capped to however
many rows actually fit (`+N more` shown if truncated) - this is a glance-at-your-phone-instead
device, not a full task manager. Markdown links in task content (Todoist's own default onboarding
tasks have these, e.g. `[Watch](https://...)`) are stripped down to just the link text - a raw URL
isn't useful on an e-ink card.

A handful of things confirmed only by testing against a real account and real Pillow/FreeType
rendering, not discoverable from docs alone:

- The REST API v2 endpoint this originally used (`/rest/v2/tasks`) was retired in early 2026
  (returns `410 Gone`) in favor of a unified, cursor-paginated `/api/v1/tasks` (`{"results": [...],
  "next_cursor": ...}`, not a bare array).
- That new `/api/v1/tasks` endpoint accepts a `filter` query param *without erroring* but silently
  ignores it, always returning every active task regardless of what filter string is passed
  (confirmed directly - `overdue`, `today`, and even a nonsense string all returned the identical
  full list). Real filtering is a separate endpoint, `/api/v1/tasks/filter` with a `query` param.
  Moot for this card specifically now (it shows everything, sorted, rather than filtering), but
  worth knowing if a filtered view is wanted again later.
- Pillow's built-in default font has no CJK glyph coverage at all, so any task containing
  Chinese/Japanese/Korean text rendered as broken missing-glyph boxes. Fixed by switching every
  card (not just `todos`) to WenQuanYi Micro Hei, installed via `apt` in the `Dockerfile`
  (`load_font()` in `app.py` falls back to Pillow's bitmap default if that font file isn't present,
  so `app.py` still runs standalone without Docker).
- Color emoji support depends heavily on the *specific font file's internal format*, not just
  "does Pillow support color emoji" in the abstract. Google's current Noto Color Emoji release uses
  a newer vector format (COLR/CPAL) that renders as completely blank in this Pillow/FreeType
  combination - no error, just silently invisible glyphs (confirmed via bounding-box inspection,
  not just "no exception raised"). The older CBDT bitmap-strike format does render correctly, but
  only at its one native pixel size (109px for this specific font) - Pillow raises "invalid pixel
  size" for any other size, so emoji glyphs are rendered at that native size and scaled down in
  code to match the surrounding text (see `render_emoji_glyph()` in `app.py`). The `Dockerfile`
  fetches this specific CBDT file directly from its source rather than trusting `apt`'s
  `fonts-noto-color-emoji`, since which format that package ships wasn't verified and Homebrew's
  current cask (used for local dev on macOS) has already moved to the broken COLR format.

## Environment variables

| Var                 | Default                       | Meaning                                          |
|---------------------|--------------------------------|--------------------------------------------------|
| `PHOTOPRISM_URL`    | `http://192.168.68.61:12342`  | Base URL of the PhotoPrism instance               |
| `ADHOC_IMAGES_DIR`  | `/data/adhoc_images`          | Mount point for extra local photo-source folders  |
| `WIDTH`             | `800`                         | Output image width (E1002 panel width)            |
| `HEIGHT`            | `480`                         | Output image height (E1002 panel height)          |
| `FAVORITES_ONLY`    | `true`                        | Only pick PhotoPrism photos marked as favorite    |
| `THUMB_SIZE`        | `fit_1920`                    | Which PhotoPrism thumbnail rendition to fetch     |
| `CANDIDATE_COUNT`   | `25`                          | How many random PhotoPrism candidates to fetch per request before filtering to JPEG and picking one |
| `TODOIST_API_TOKEN` | *(empty)*                     | Personal Todoist API token - `todos` dashboard source shows "not configured" if unset |
| `PORT`              | `8090`                        | Port the Flask server listens on inside the container |

## Local testing (without Docker)

```bash
cd photo-frame-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py            # serves on http://0.0.0.0:8090
curl -o photo.png "http://127.0.0.1:8090/photo.png?index=0"
```

To test local photo sources without Docker, point `ADHOC_IMAGES_DIR` at a real (non-symlinked)
directory containing subfolders of images:

```bash
ADHOC_IMAGES_DIR=/path/to/some/folder .venv/bin/python app.py
```

## Build the Docker image

Build for the NAS's CPU architecture. Most Synology models are `linux/amd64`; if yours is
ARM-based, use `linux/arm64` instead.

```bash
cd photo-frame-bridge
docker build --platform linux/amd64 -t photo-frame-bridge:latest .
```

## Export and deploy to Synology DSM (Container Manager)

This mirrors the "build locally, import on NAS" workflow already in use for other containers —
no docker-compose or registry needed.

1. Export the image to a tarball:
   ```bash
   docker save photo-frame-bridge:latest -o photo-frame-bridge.tar
   ```
2. Copy `photo-frame-bridge.tar` to the NAS (File Station or `scp`).
3. In DSM **Container Manager → Image → Add → Add From File**, select the tarball to import it.
4. Create a NAS shared folder for extra photo sources (e.g. `adhoc_images`), and inside it a
   subfolder per source (e.g. `adhoc_images/kid_photos/`). Upload images into those subfolders
   via File Station whenever you want to add more - no container changes needed.
5. **Container Manager → Container → Create**, pick the imported `photo-frame-bridge:latest`
   image, and configure:
   - **Port Settings**: map a **fixed** local port (e.g. `8090`) to container port `8090`.
     ⚠️ Leaving this on "auto" lets DSM remap it to a random port (this happened on first deploy —
     it came up on `32770` instead of `8090`), which will silently break the ESPHome config later
     since it hardcodes the URL/port. Set it explicitly.
   - **Volume/Folder mapping**: mount the `adhoc_images` shared folder from step 4 to
     `/data/adhoc_images` inside the container (read-only is fine, the bridge never writes to it).
   - **Environment variables**: `ADHOC_IMAGES_DIR` is baked into the image (declared in the
     `Dockerfile`), so it'll already show up in DSM's Environment tab set to `/data/adhoc_images`
     - edit it there if you'd rather mount to a different container path, but whatever value you
     set here must match your **Volume/Folder mapping**'s mount path above, or the bridge will
     look in the wrong place and only ever see `photoprism` as a source. Override any of the
     other env vars from the table above too if needed (defaults already point at
     `192.168.68.61:12342`, 800x480, favorites-only) - set `TODOIST_API_TOKEN` here if you want
     the `todos` dashboard source working (optional; it just shows "not configured" without one).
   - **Auto-restart**: enable, so it comes back up after a NAS reboot.
6. Start the container.

## Verify

From any machine on the LAN:

```bash
curl -o photo0.png -w "HTTP %{http_code}, %{size_download} bytes\n" \
  "http://192.168.68.61:8090/photo.png?index=0"
curl -o photo1.png -w "HTTP %{http_code}, %{size_download} bytes\n" \
  "http://192.168.68.61:8090/photo.png?index=1"
curl -o dashboard0.png -w "HTTP %{http_code}, %{size_download} bytes\n" \
  "http://192.168.68.61:8090/dashboard.png?index=0"
curl -o dashboard1.png -w "HTTP %{http_code}, %{size_download} bytes\n" \
  "http://192.168.68.61:8090/dashboard.png?index=1"
curl -o dashboard2.png -w "HTTP %{http_code}, %{size_download} bytes\n" \
  "http://192.168.68.61:8090/dashboard.png?index=2"
```

`index=0` should always be PhotoPrism favorites / weather respectively; `index=1` should be your
first `adhoc_images` subfolder / PhotoPrism stats; `index=2` for `dashboard.png` should be Todoist
(or "not configured" if `TODOIST_API_TOKEN` isn't set). Each `photo.png` response should be an
~80-150KB, 800x480 PNG dithered into six pure colors (black, white, red, yellow, blue, green).
Each `dashboard.png` response should be a much smaller (~5-10KB) text card. Repeated requests to
the same index should return different random photos (photo sources) or fresh data (dashboard
sources).

`GET /healthz` returns `ok` and can be used as a container health check.

## Troubleshooting

- **`HTTP 503` from `/photo.png`**: the resolved source had no usable images - for `photoprism`,
  check `docker logs` and confirm favorited photos with `Mime: image/jpeg` exist; for an
  `adhoc_images` subfolder, confirm it actually contains files with a supported extension and
  that the volume mount is correct (`docker exec <container> ls /data/adhoc_images`).
- **`HTTP 502`**: fetching/processing the chosen source failed — check `docker logs` for the
  underlying error (e.g. PhotoPrism unreachable, corrupt image file, weather API unreachable).
- **A new `adhoc_images` subfolder never shows up**: subfolders are sorted alphabetically after
  `photoprism`, so its index depends on how many other subfolders exist and their names - just
  keep cycling the button, or check `docker logs` for `Serving photo from source ...` to see
  what each index currently resolves to.
- **Container port changed after a restart**: DSM only respects a *fixed* local port if it was
  explicitly set (not "auto") when the container was created — re-check Port Settings.
