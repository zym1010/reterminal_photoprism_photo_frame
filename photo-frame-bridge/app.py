import io
import logging
import os
import random
from datetime import datetime, timedelta, timezone

import pillow_avif  # noqa: F401  (registers AVIF support with Pillow on import)
import requests
from epaper_dithering import ColorPalette, DitherMode, dither_image
from flask import Flask, Response, abort, request
from PIL import Image, ImageDraw, ImageFont, ImageOps

PHOTOPRISM_URL = os.environ.get("PHOTOPRISM_URL", "http://192.168.68.61:12342").rstrip("/")
WIDTH = int(os.environ.get("WIDTH", "800"))
HEIGHT = int(os.environ.get("HEIGHT", "480"))
FAVORITES_ONLY = os.environ.get("FAVORITES_ONLY", "true").lower() != "false"
THUMB_SIZE = os.environ.get("THUMB_SIZE", "fit_1920")
CANDIDATE_COUNT = int(os.environ.get("CANDIDATE_COUNT", "25"))
# Mounted on the NAS side: each immediate subfolder becomes an additional photo
# source, discovered fresh on every request - drop a new folder in, no redeploy
# needed. "photoprism" (PhotoPrism favorites) is always source 0.
ADHOC_IMAGES_DIR = os.environ.get("ADHOC_IMAGES_DIR", "/data/adhoc_images")
LOCAL_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".tiff", ".heic", ".heif"}
REQUEST_TIMEOUT = 15

# Dashboard sources are code (each needs its own fetch+render logic), unlike photo
# sources which are just folders - adding one later means adding a branch in
# render_dashboard_image() below and appending here.
DASHBOARD_SOURCES = ["weather", "stats"]

# The reTerminal E1002's epaper_spi driver does NOT do nearest-color matching
# against a rich/measured palette. It classifies each pixel into one of 8 RGB-cube
# corners using a naive threshold (each channel "on" if > 128, and anything with
# max-min channel spread < 50 collapses to black/white by luminance). Dithering to
# realistic "measured" ink colors (e.g. a muted green like (40,82,57)) falls inside
# that gray-collapse threshold and gets misread as black. Idealized, fully-saturated
# primaries classify correctly on-device, so we dither to those instead - tone/gamut
# compression (only available via a plain ColorPalette, not the ColorScheme enum)
# claws back most of the perceptual quality that pure primaries would otherwise lose.
DEVICE_PALETTE = ColorPalette(
    colors={
        "black": (0, 0, 0),
        "white": (255, 255, 255),
        "yellow": (255, 255, 0),
        "red": (255, 0, 0),
        "blue": (0, 0, 255),
        "green": (0, 255, 0),
    },
    accent="red",
)

# The one non-self-hosted piece of this project: a real weather forecast needs an
# external data source. Open-Meteo needs no API key/account (just lat/long), which
# keeps this as close to "no new cloud accounts" as a weather feature can get.
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_LOCATIONS = [
    ("Cupertino, CA", 37.3230, -122.0322),
    ("Wuhan, Hubei", 30.5928, 114.3055),
    ("Dalian, Liaoning", 38.9140, 121.6147),
]

# WMO weather codes -> human-readable condition, per Open-Meteo's docs.
WMO_CONDITIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Dense drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Violent showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm, hail",
    99: "Thunderstorm, heavy hail",
}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = app.logger


def render_to_png(img):
    dithered = dither_image(img, DEVICE_PALETTE, mode=DitherMode.ATKINSON, tone="auto", gamut="auto")
    buf = io.BytesIO()
    dithered.save(buf, format="PNG")
    return buf.getvalue()


def list_photo_sources():
    """"photoprism" plus every immediate subfolder of ADHOC_IMAGES_DIR, sorted.

    Skips folders starting with "@", "#", or "." - NAS-internal housekeeping
    folders (e.g. Synology's "@eaDir" thumbnail cache and "#recycle" bin), not
    real photo sources.
    """
    sources = ["photoprism"]
    if os.path.isdir(ADHOC_IMAGES_DIR):
        sources.extend(
            sorted(
                name
                for name in os.listdir(ADHOC_IMAGES_DIR)
                if not name.startswith(("@", "#", "."))
                and os.path.isdir(os.path.join(ADHOC_IMAGES_DIR, name))
            )
        )
    return sources


def pick_random_jpeg_photo():
    params = {"count": CANDIDATE_COUNT, "order": "random"}
    if FAVORITES_ONLY:
        params["favorite"] = "true"
    resp = requests.get(f"{PHOTOPRISM_URL}/api/v1/photos/view", params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    photos = resp.json()
    candidates = [p for p in photos if p.get("Mime") == "image/jpeg" and THUMB_SIZE in p.get("Thumbs", {})]
    return random.choice(candidates) if candidates else None


def flatten_to_rgb(img):
    """Correctly-oriented, opaque RGB version of `img`.

    A plain `.convert("RGB")` on an image with an alpha channel just drops the
    alpha and exposes whatever RGB values happen to sit under transparent
    pixels - often leftover/garbage data in image editing tools, which then
    shows up as visible noise once dithered. Composite onto white first for
    anything with real transparency (RGBA/LA, or "P" mode with a transparency
    entry) instead.
    """
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        return background
    return img.convert("RGB")


def fetch_and_process(photo):
    thumb_path = photo["Thumbs"][THUMB_SIZE]["src"]
    resp = requests.get(f"{PHOTOPRISM_URL}{thumb_path}", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    img = flatten_to_rgb(Image.open(io.BytesIO(resp.content)))
    fitted = ImageOps.fit(img, (WIDTH, HEIGHT), method=Image.LANCZOS, centering=(0.5, 0.5))
    return render_to_png(fitted)


def pick_random_local_image(source):
    """Random image from anywhere under this source's folder, recursively.

    The source is the immediate subfolder (e.g. "kid_photos"), but images can
    live in nested sub-subfolders under it - those all still count as the same
    source, just organized however you like on disk.
    """
    folder = os.path.join(ADHOC_IMAGES_DIR, source)
    files = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(("@", "#", "."))]
        files.extend(
            os.path.join(dirpath, f) for f in filenames if os.path.splitext(f)[1].lower() in LOCAL_IMAGE_EXTENSIONS
        )
    return random.choice(files) if files else None


def fetch_and_process_local(source):
    path = pick_random_local_image(source)
    if path is None:
        return None
    img = flatten_to_rgb(Image.open(path))
    fitted = ImageOps.fit(img, (WIDTH, HEIGHT), method=Image.LANCZOS, centering=(0.5, 0.5))
    return render_to_png(fitted)


def render_photo_image(index):
    """Dispatch to the photo source at `index` (wrapping around the current source
    list), returning (source_name, png_bytes) or (source_name, None) if empty."""
    sources = list_photo_sources()
    source = sources[index % len(sources)]
    if source == "photoprism":
        photo = pick_random_jpeg_photo()
        if photo is None:
            return source, None
        return source, fetch_and_process(photo)
    return source, fetch_and_process_local(source)


def fetch_added_count(since_days):
    """Count of items added (imported) in the last `since_days` days.

    PhotoPrism's search syntax has an undocumented `added:"<RFC3339>"` filter meaning
    "added at or after this timestamp" - not in the official docs, but confirmed via
    photoprism/photoprism#4300. Response count comes from the `X-Count` header, which
    only reflects the *true* total once the requested `count` exceeds it - a large
    `count` here is still cheap because personal libraries only add a handful of items
    in any given week/month, so the actual response body stays small regardless.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = requests.get(
        f"{PHOTOPRISM_URL}/api/v1/photos",
        params={"count": 20000, "q": f'added:"{cutoff}"'},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return int(resp.headers.get("X-Count", len(resp.json())))


def fetch_library_stats():
    """Cheap aggregate counts from /config, the most recently added item's timestamp,
    and how many items were added in the last 7/15/30 days.

    Avoids paging through the whole library just to count it - /api/v1/config already
    carries a precomputed `count` object, and everything else here is either a single
    count=1 request or relies on the X-Count header trick above.
    """
    config_resp = requests.get(f"{PHOTOPRISM_URL}/api/v1/config", timeout=REQUEST_TIMEOUT)
    config_resp.raise_for_status()
    counts = config_resp.json().get("count", {})

    latest_resp = requests.get(
        f"{PHOTOPRISM_URL}/api/v1/photos",
        params={"count": 1, "order": "added"},
        timeout=REQUEST_TIMEOUT,
    )
    latest_resp.raise_for_status()
    latest = latest_resp.json()
    last_added = latest[0]["CreatedAt"] if latest else None

    added_recent = {days: fetch_added_count(days) for days in (30, 60, 90)}

    return counts, last_added, added_recent


def format_last_added(iso_ts):
    if not iso_ts:
        return "Unknown"
    dt = datetime.strptime(iso_ts.split(".")[0], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - dt).days
    if days <= 0:
        rel = "today"
    elif days == 1:
        rel = "1 day ago"
    else:
        rel = f"{days} days ago"
    return f"{dt.strftime('%Y-%m-%d %H:%M UTC')} ({rel})"


def render_stats_image(counts, last_added, added_recent):
    img = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    title_font = ImageFont.load_default(size=44)
    label_font = ImageFont.load_default(size=30)

    margin = 40
    draw.text((margin, margin), "PhotoPrism Library", font=title_font, fill=(0, 0, 0))
    draw.line((margin, margin + 60, WIDTH - margin, margin + 60), fill=(0, 0, 0), width=2)

    # photos/live/videos are mutually exclusive categories that sum to count.all,
    # so Photos+Live Photos is a plain sum, not double-counting anything.
    rows = [
        ("Photos", counts.get("photos", 0) + counts.get("live", 0)),
        ("Videos", counts.get("videos")),
        ("Favorites", counts.get("favorites")),
        ("Added (30 days)", added_recent.get(30)),
        ("Added (60 days)", added_recent.get(60)),
        ("Added (90 days)", added_recent.get(90)),
        ("Last Added", format_last_added(last_added)),
    ]

    top = margin + 90
    bottom = HEIGHT - margin
    row_height = (bottom - top) // len(rows)
    y = top
    for label, value in rows:
        draw.text((margin, y), f"{label}", font=label_font, fill=(0, 0, 0))
        draw.text((WIDTH - margin, y), str(value), font=label_font, fill=(0, 0, 0), anchor="ra")
        y += row_height

    return img


def fetch_weather(name, lat, lon):
    resp = requests.get(
        WEATHER_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "temperature_unit": "celsius",
            "timezone": "auto",
            "forecast_days": 1,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    current = data["current"]
    daily = data["daily"]
    # timezone=auto makes Open-Meteo return `current.time` in the location's own
    # local time (not UTC) - exactly what we want to display, no conversion needed.
    local_time = datetime.strptime(current["time"], "%Y-%m-%dT%H:%M").strftime("%b %d, %-I:%M %p")
    return {
        "name": name,
        "temp": round(current["temperature_2m"]),
        "condition": WMO_CONDITIONS.get(current["weather_code"], "Unknown"),
        "high": round(daily["temperature_2m_max"][0]),
        "low": round(daily["temperature_2m_min"][0]),
        "local_time": local_time,
    }


def render_weather_image(results):
    img = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    title_font = ImageFont.load_default(size=44)
    city_font = ImageFont.load_default(size=34)
    detail_font = ImageFont.load_default(size=28)

    margin = 40
    draw.text((margin, margin), "Weather", font=title_font, fill=(0, 0, 0))
    draw.line((margin, margin + 60, WIDTH - margin, margin + 60), fill=(0, 0, 0), width=2)

    top = margin + 90
    bottom = HEIGHT - margin
    row_height = (bottom - top) // len(results)
    y = top
    for r in results:
        draw.text((margin, y), r["name"], font=city_font, fill=(0, 0, 0))
        draw.text((WIDTH - margin, y + 4), f"{r['temp']}°C", font=city_font, fill=(0, 0, 0), anchor="ra")
        detail = f"As of {r['local_time']}  ·  {r['condition']}  ·  H:{r['high']}° L:{r['low']}°"
        draw.text((margin, y + 46), detail, font=detail_font, fill=(0, 0, 0))
        if y + row_height < bottom:
            draw.line((margin, y + row_height - 15, WIDTH - margin, y + row_height - 15), fill=(0, 0, 0), width=1)
        y += row_height

    return img


def render_dashboard_image(index):
    """Dispatch to the dashboard source at `index` (wrapping around DASHBOARD_SOURCES)."""
    source = DASHBOARD_SOURCES[index % len(DASHBOARD_SOURCES)]
    if source == "weather":
        results = [fetch_weather(name, lat, lon) for name, lat, lon in WEATHER_LOCATIONS]
        return source, render_weather_image(results)
    counts, last_added, added_recent = fetch_library_stats()
    return source, render_stats_image(counts, last_added, added_recent)


@app.route("/photo.png")
def photo():
    index = request.args.get("index", 0, type=int) or 0
    try:
        source, img = render_photo_image(index)
    except Exception:
        log.exception("Failed to fetch/process photo (index=%s)", index)
        abort(502, description="Failed to fetch/process photo")
    if img is None:
        log.warning("Photo source %r had no usable images (index=%s)", source, index)
        abort(503, description=f"No images available from source {source!r}")
    log.info("Serving photo from source %r (index=%s)", source, index)
    return Response(img, mimetype="image/png", headers={"Cache-Control": "no-store"})


@app.route("/dashboard.png")
def dashboard():
    index = request.args.get("index", 0, type=int) or 0
    try:
        source, img = render_dashboard_image(index)
        png_bytes = render_to_png(img)
    except Exception:
        log.exception("Failed to fetch/render dashboard (index=%s)", index)
        abort(502, description="Failed to fetch/render dashboard")
    log.info("Serving dashboard source %r (index=%s)", source, index)
    return Response(png_bytes, mimetype="image/png", headers={"Cache-Control": "no-store"})


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")), threaded=True)
