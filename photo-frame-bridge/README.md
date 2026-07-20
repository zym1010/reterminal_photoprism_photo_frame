# photo-frame-bridge

Small self-hosted service with three endpoints:

- `GET /frame.png` → a random favorited JPEG photo from PhotoPrism, cropped to 800x480 and
  dithered to the reTerminal E1002's 6 native ink colors.
- `GET /stats.png` → a PhotoPrism library stats card (photo/video/favorite counts, last-added
  timestamp, etc.), rendered as an image using the same 800x480/6-color pipeline.
- `GET /weather.png` → current conditions for three fixed locations (see below), also rendered
  as an image.

Runs entirely on the LAN — no cloud, no PhotoPrism auth required (this instance has none
configured), except for `/weather.png`'s one external call (see below). Everything is rendered
as an image rather than exposed as JSON so the ESPHome side stays dead simple: no on-device JSON
parsing or native text widgets, just an `online_image` + LVGL page per feature, identical in
shape to the photo slideshow.

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

## `/stats.png`: cheap counts, not a full library scan

It's tempting to get a "total photos" count by requesting every photo and counting the array -
PhotoPrism's `X-Count` response header only reflects the true total once your `count` param
exceeds it, so a naive approach ends up downloading the entire library's JSON (tens of MB) just
to count it. Instead:

- `GET /api/v1/config` already carries a precomputed `count` object (`photos`, `videos`, `live`,
  `favorites`, `places`, `cameras`, etc.) - a single lightweight request.
- The "last added" timestamp comes from `GET /api/v1/photos?count=1&order=added`, which returns
  one record's `CreatedAt` (when it was imported, not when it was taken) - also a single cheap
  request.

## `/weather.png`: the one non-self-hosted piece

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

## Environment variables

| Var              | Default                         | Meaning                                      |
|-------------------|----------------------------------|-----------------------------------------------|
| `PHOTOPRISM_URL`  | `http://192.168.68.61:12342`    | Base URL of the PhotoPrism instance           |
| `WIDTH`           | `800`                           | Output image width (E1002 panel width)        |
| `HEIGHT`          | `480`                           | Output image height (E1002 panel height)      |
| `FAVORITES_ONLY`  | `true`                          | Only pick photos marked as favorite           |
| `THUMB_SIZE`      | `fit_1920`                      | Which PhotoPrism thumbnail rendition to fetch |
| `CANDIDATE_COUNT` | `25`                            | How many random candidates to fetch per request before filtering to JPEG and picking one |
| `PORT`            | `8090`                          | Port the Flask server listens on inside the container |

## Local testing (without Docker)

```bash
cd photo-frame-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py            # serves on http://0.0.0.0:8090
curl -o frame.png http://127.0.0.1:8090/frame.png
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
4. **Container Manager → Container → Create**, pick the imported `photo-frame-bridge:latest`
   image, and configure:
   - **Port Settings**: map a **fixed** local port (e.g. `8090`) to container port `8090`.
     ⚠️ Leaving this on "auto" lets DSM remap it to a random port (this happened on first deploy —
     it came up on `32770` instead of `8090`), which will silently break the ESPHome config later
     since it hardcodes the URL/port. Set it explicitly.
   - **Environment variables**: override any of the table above if needed (defaults already point
     at `192.168.68.61:12342`, 800x480, favorites-only).
   - **Auto-restart**: enable, so it comes back up after a NAS reboot.
5. Start the container.

## Verify

From any machine on the LAN:

```bash
curl -o frame.png -w "HTTP %{http_code}, %{size_download} bytes\n" \
  http://192.168.68.61:8090/frame.png
curl -o stats.png -w "HTTP %{http_code}, %{size_download} bytes\n" \
  http://192.168.68.61:8090/stats.png
curl -o weather.png -w "HTTP %{http_code}, %{size_download} bytes\n" \
  http://192.168.68.61:8090/weather.png
```

`frame.png` should return `HTTP 200` and an ~80-150KB PNG — an 800x480 image dithered into six
pure colors (black, white, red, yellow, blue, green), matching a random favorited photo from the
library. Repeated requests should return different photos.

`stats.png` should return `HTTP 200` and a much smaller (~5-10KB) PNG - a text card with photo,
video, favorite, and other library counts, plus a "last added" timestamp.

`weather.png` should return `HTTP 200` and a similarly small PNG - one row per configured
location, each with current temperature/condition, today's H/L, and that location's local time.

`GET /healthz` returns `ok` and can be used as a container health check.

## Troubleshooting

- **`HTTP 503`**: no favorited JPEG photos were found in the last batch of `CANDIDATE_COUNT`
  random candidates — check `docker logs` and confirm favorited photos with `Mime: image/jpeg`
  exist in PhotoPrism.
- **`HTTP 502`**: fetching/processing the chosen photo's thumbnail failed — check `docker logs`
  for the underlying error (e.g. PhotoPrism unreachable, thumbnail not yet generated).
- **Container port changed after a restart**: DSM only respects a *fixed* local port if it was
  explicitly set (not "auto") when the container was created — re-check Port Settings.
