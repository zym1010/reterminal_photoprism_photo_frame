import io
import logging
import os
import random
import re
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
# Which "Added (N days)" rows the stats card shows - comma-separated, any
# number of values (not fixed to three anymore).
STATS_ADDED_DAYS = [int(d.strip()) for d in os.environ.get("STATS_ADDED_DAYS", "30,60,90").split(",")]
# Mounted on the NAS side: each immediate subfolder becomes an additional photo
# source, discovered fresh on every request - drop a new folder in, no redeploy
# needed. "photoprism" (PhotoPrism favorites) is always source 0.
ADHOC_IMAGES_DIR = os.environ.get("ADHOC_IMAGES_DIR", "/data/adhoc_images")
LOCAL_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".tiff", ".heic", ".heif"}
REQUEST_TIMEOUT = 15

# Also mounted on the NAS side: a Procare-style kid-photo archive, dated by folder
# hierarchy rather than EXIF/mtime (a Procare export's own timestamps aren't
# reliably set) - immediate subfolders are named "YYYY-MM-DD", each with its own
# "photos" (and sibling "videos") subfolder. Candidates are pulled only from
# "<date>/photos/", recursively - not the whole date folder - so "<date>/videos/"
# is excluded by construction rather than relying on extension filtering alone.
# Always source 1 (right after "photoprism") when this directory exists, same
# "no redeploy needed" spirit as ADHOC_IMAGES_DIR above.
KID_PHOTOS_DIR = os.environ.get("KID_PHOTOS_DIR", "/data/kid_photos")
# Exponential-decay half-life (in days) for recency weighting - see
# pick_random_kid_photo() below. Smaller = more strongly favors recent photos.
KID_PHOTOS_RECENCY_HALF_LIFE_DAYS = float(os.environ.get("KID_PHOTOS_RECENCY_HALF_LIFE_DAYS", "60"))
KID_PHOTOS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Dashboard sources are code (each needs its own fetch+render logic), unlike photo
# sources which are just folders - adding one later means adding a branch in
# render_dashboard_image() below and appending here.
DASHBOARD_SOURCES = ["weather", "stats", "todos"]

# The second (after weather) deliberately non-self-hosted piece: there's no
# realistic self-hosted alternative to a task manager you actually use day to
# day. Just a personal API token, no OAuth app/account needed. Note: the old
# REST API v2 (/rest/v2/...) was retired in early 2026 (returns 410 Gone) in
# favor of this unified /api/v1/ one - confirmed directly against the live API
# since the docs describing it were inconsistent about the response shape.
TODOIST_API_URL = "https://api.todoist.com/api/v1"
TODOIST_API_TOKEN = os.environ.get("TODOIST_API_TOKEN", "")

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

# Pillow's ImageFont.load_default() is a tiny built-in bitmap font with no CJK
# (or broader Unicode) glyph coverage - confirmed by testing against real
# Todoist task content containing Chinese text, which rendered as broken
# missing-glyph boxes. WenQuanYi Micro Hei (installed via apt in the
# Dockerfile - see requirements.txt/Dockerfile) covers both Latin and CJK, so
# every card uses it consistently. Falls back to the bitmap default if the
# font file isn't present (e.g. running app.py locally without Docker) rather
# than crashing - degraded rendering, not a hard requirement, for local dev.
FONT_PATH = os.environ.get("FONT_PATH", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc")

# Noto Color Emoji (installed via apt in the Dockerfile) - a *separate* font
# from FONT_PATH above, since no single font file covers both CJK text and
# color emoji glyphs, and Pillow has no automatic font-fallback mechanism
# (unlike a browser or native text layout engine) - mixed text has to be
# manually split into runs and each run drawn with the right font. See
# split_text_runs()/draw_mixed_text() below.
EMOJI_FONT_PATH = os.environ.get("EMOJI_FONT_PATH", "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf")

# Common emoji Unicode ranges, plus variation selector (FE0F, forces
# emoji-style rendering of the preceding character) and ZWJ (200D, joins
# multiple codepoints into one compound emoji e.g. family/skin-tone variants).
# Not exhaustive of every Unicode emoji block that will ever be assigned, but
# covers everything in practice found in real Todoist task content.
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF"
    "\U00002300-\U000023FF"
    "\U0000FE0F"
    "\U0000200D"
    "]+"
)


def load_font(size):
    if os.path.exists(FONT_PATH):
        return ImageFont.truetype(FONT_PATH, size)
    return ImageFont.load_default(size=size)


# Noto Color Emoji ships color glyphs as CBDT bitmap "strikes" at one fixed
# pixel size (confirmed by inspecting the font's CBLC table directly - this
# build only has a single 109px strike). Pillow can only load a CBDT font at
# one of its actual strike sizes - anything else raises "invalid pixel size" -
# so glyphs have to be rendered at that native size and then scaled down to
# match the surrounding text, rather than requested at an arbitrary size like
# every other font here. Also confirmed empirically: a *different* Noto Color
# Emoji build using the newer COLR/CPAL vector format renders nothing at all
# in this Pillow/FreeType combination (zero-height bbox, no error) - CBDT is
# the one that actually works.
_EMOJI_NATIVE_SIZE = None


def emoji_native_size():
    global _EMOJI_NATIVE_SIZE
    if _EMOJI_NATIVE_SIZE is None and os.path.exists(EMOJI_FONT_PATH):
        for candidate in (109, 136, 128, 96, 64, 32):
            try:
                ImageFont.truetype(EMOJI_FONT_PATH, candidate)
                _EMOJI_NATIVE_SIZE = candidate
                break
            except OSError:
                continue
    return _EMOJI_NATIVE_SIZE


def load_emoji_font():
    size = emoji_native_size()
    if size is None:
        return None
    return ImageFont.truetype(EMOJI_FONT_PATH, size)


def render_emoji_glyph(emoji_font, run, target_height):
    """Renders `run` at the font's native (fixed) strike size, crops to the
    glyph's actual ink, and scales down to `target_height` to match the
    surrounding text's line height. Returns None if the font has no glyph for
    this run (rare, but a font update could drop something) rather than
    raising - missing an emoji is fine, crashing the whole card isn't.
    """
    size = emoji_font.size
    tmp = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    ImageDraw.Draw(tmp).text((0, 0), run, font=emoji_font, embedded_color=True)
    bbox = tmp.getbbox()
    if bbox is None:
        return None
    cropped = tmp.crop(bbox)
    scale = target_height / cropped.height
    new_size = (max(1, round(cropped.width * scale)), max(1, round(cropped.height * scale)))
    return cropped.resize(new_size, Image.LANCZOS)


def split_text_runs(text):
    """Splits into (substring, is_emoji) runs for mixed-font rendering."""
    runs = []
    pos = 0
    for m in EMOJI_RE.finditer(text):
        if m.start() > pos:
            runs.append((text[pos : m.start()], False))
        runs.append((text[m.start() : m.end()], True))
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], False))
    return runs


def mixed_text_width(draw, text, font, emoji_font, emoji_height):
    if emoji_font is None:
        return draw.textlength(text, font=font)
    total = 0
    for run, is_emoji in split_text_runs(text):
        if is_emoji:
            glyph = render_emoji_glyph(emoji_font, run, emoji_height)
            total += glyph.width if glyph is not None else 0
        else:
            total += draw.textlength(run, font=font)
    return total


def draw_mixed_text(img, draw, xy, text, font, emoji_font, fill, emoji_height, stroke_width=0):
    """Draws `text` left-aligned at `xy`, using `emoji_font` (color, scaled to
    `emoji_height`) for emoji runs and `font` (regular fill color) for
    everything else. Falls back to plain single-font rendering if `emoji_font`
    is None (not installed - see load_emoji_font()) rather than crashing.
    Needs the base `img` (not just `draw`) to paste scaled emoji bitmaps.
    """
    if emoji_font is None:
        draw.text(xy, text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=fill)
        return
    x, y = xy
    for run, is_emoji in split_text_runs(text):
        if is_emoji:
            glyph = render_emoji_glyph(emoji_font, run, emoji_height)
            if glyph is not None:
                img.paste(glyph, (round(x), round(y)), glyph)
                x += glyph.width
        else:
            draw.text((x, y), run, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=fill)
            x += draw.textlength(run, font=font)


app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = app.logger


def render_to_png(img):
    dithered = dither_image(img, DEVICE_PALETTE, mode=DitherMode.ATKINSON, tone="auto", gamut="auto")
    buf = io.BytesIO()
    dithered.save(buf, format="PNG")
    return buf.getvalue()


def list_photo_sources():
    """"photoprism", then "kid_photos" (if KID_PHOTOS_DIR exists), then every
    immediate subfolder of ADHOC_IMAGES_DIR, sorted.

    Skips folders starting with "@", "#", or "." - NAS-internal housekeeping
    folders (e.g. Synology's "@eaDir" thumbnail cache and "#recycle" bin), not
    real photo sources.
    """
    sources = ["photoprism"]
    if os.path.isdir(KID_PHOTOS_DIR):
        sources.append("kid_photos")
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


def list_images_recursive(folder):
    """Every image file (by extension) anywhere under `folder`, at any depth.

    Skips folders starting with "@", "#", or "." at every level - NAS-internal
    housekeeping folders (e.g. Synology's "@eaDir" thumbnail cache and
    "#recycle" bin), not real content.
    """
    files = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(("@", "#", "."))]
        files.extend(
            os.path.join(dirpath, f) for f in filenames if os.path.splitext(f)[1].lower() in LOCAL_IMAGE_EXTENSIONS
        )
    return files


def pick_random_local_image(source):
    """Random image from anywhere under this source's folder, recursively.

    The source is the immediate subfolder (e.g. "family_reunion"), but images
    can live in nested sub-subfolders under it - those all still count as the
    same source, just organized however you like on disk.
    """
    folder = os.path.join(ADHOC_IMAGES_DIR, source)
    files = list_images_recursive(folder)
    return random.choice(files) if files else None


def list_kid_photo_date_folders():
    """(date, photos_dir) for every immediate subfolder of KID_PHOTOS_DIR named
    "YYYY-MM-DD" that has a "photos" subfolder - i.e. only the date-named
    top-level folders this archive actually uses, and only the "photos"
    branch of each (siblings like "videos" are a different source of candidate
    files, not photos, so they're excluded by construction rather than relying
    solely on extension filtering).
    """
    folders = []
    for name in os.listdir(KID_PHOTOS_DIR):
        if not KID_PHOTOS_DATE_RE.match(name):
            continue
        try:
            date = datetime.strptime(name, "%Y-%m-%d").date()
        except ValueError:
            continue
        photos_dir = os.path.join(KID_PHOTOS_DIR, name, "photos")
        if os.path.isdir(photos_dir):
            folders.append((date, photos_dir))
    return folders


def pick_random_kid_photo():
    """Recency-weighted random photo from KID_PHOTOS_DIR.

    Two-stage pick - a date folder first (weighted by exponential decay on its
    age, via KID_PHOTOS_RECENCY_HALF_LIFE_DAYS), then a uniformly random photo
    within that date's "photos" folder. Two stages rather than one flat
    weighted pick over every file, so a date with hundreds of photos doesn't
    drown out one with only a handful - each date competes as a single unit,
    then contributes its own photos equally. Dates with no actual photo files
    (e.g. a day that only has videos) are dropped before weighting, not just
    given zero weight, so they can never "win" and return None.
    """
    today = datetime.now().date()
    candidates = []
    weights = []
    for date, photos_dir in list_kid_photo_date_folders():
        images = list_images_recursive(photos_dir)
        if not images:
            continue
        age_days = max(0, (today - date).days)
        weight = 0.5 ** (age_days / KID_PHOTOS_RECENCY_HALF_LIFE_DAYS)
        candidates.append(images)
        weights.append(weight)
    if not candidates:
        return None
    images = random.choices(candidates, weights=weights, k=1)[0]
    return random.choice(images)


def fetch_and_process_path(path):
    img = flatten_to_rgb(Image.open(path))
    fitted = ImageOps.fit(img, (WIDTH, HEIGHT), method=Image.LANCZOS, centering=(0.5, 0.5))
    return render_to_png(fitted)


def fetch_and_process_local(source):
    path = pick_random_local_image(source)
    if path is None:
        return None
    return fetch_and_process_path(path)


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
    if source == "kid_photos":
        path = pick_random_kid_photo()
        if path is None:
            return source, None
        return source, fetch_and_process_path(path)
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

    added_recent = {days: fetch_added_count(days) for days in STATS_ADDED_DAYS}

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
    title_font = load_font(44)
    label_font = load_font(30)

    margin = 40
    draw.text((margin, margin), "PhotoPrism Library", font=title_font, fill=(0, 0, 0))
    draw.line((margin, margin + 60, WIDTH - margin, margin + 60), fill=(0, 0, 0), width=2)

    # photos/live/videos are mutually exclusive categories that sum to count.all,
    # so Photos+Live Photos is a plain sum, not double-counting anything.
    rows = [
        ("Photos", counts.get("photos", 0) + counts.get("live", 0)),
        ("Videos", counts.get("videos")),
        ("Favorites", counts.get("favorites")),
    ]
    # STATS_ADDED_DAYS controls both which days get fetched (fetch_library_stats)
    # and the rows shown here - iterating it directly (not added_recent.keys())
    # keeps row order matching the configured order even with duplicate values.
    rows += [(f"Added ({days} days)", added_recent.get(days)) for days in STATS_ADDED_DAYS]
    rows.append(("Last Added", format_last_added(last_added)))

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
    title_font = load_font(44)
    city_font = load_font(34)
    detail_font = load_font(28)

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


def fetch_todoist_tasks():
    """All active (incomplete) tasks, sorted with undated tasks first, then
    dated tasks by due date ascending (each group by priority descending as a
    tiebreak) - per an explicit ask to show a general list rather than only
    today/overdue, with undated tasks surfaced first rather than buried last.

    Earlier version of this filtered server-side to "today | overdue" via
    Todoist's filter query language. Two things learned the hard way while
    building that: GET /tasks accepts a "filter" query param without erroring
    but silently ignores it (confirmed directly - "overdue", "today", and even
    a nonsense string all returned the identical full task list); the real
    filter endpoint is the separate /tasks/filter with a "query" param. Both
    are now moot for this specific card (no filtering happens here anymore),
    but left documented in case a future filtered view is wanted again.
    """
    headers = {"Authorization": f"Bearer {TODOIST_API_TOKEN}"}
    tasks = []
    cursor = None
    # Cursor-paginated ({"results": [...], "next_cursor": ...}), not a bare
    # array. A personal task list is never going to be huge, but follow the
    # cursor properly anyway rather than silently truncating someone's real
    # list.
    for _ in range(10):  # sane upper bound, not an expected real-world case
        params = {"cursor": cursor} if cursor else {}
        resp = requests.get(f"{TODOIST_API_URL}/tasks", headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        tasks.extend(data["results"])
        cursor = data.get("next_cursor")
        if not cursor:
            break

    def sort_key(task):
        due = task.get("due")
        has_due = 1 if due else 0
        due_date = due.get("date", "") if due else ""
        return (has_due, due_date, -task.get("priority", 1))

    tasks.sort(key=sort_key)
    return tasks


MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def strip_markdown_links(text):
    """Todoist task content can contain markdown links (e.g. its own default
    onboarding tasks do, like "[Watch](https://...)") - shown as the link text
    only, since there's no way to make raw URL text useful on an e-ink card.
    """
    return MARKDOWN_LINK_RE.sub(r"\1", text)


def truncate_mixed_to_width(draw, text, font, emoji_font, emoji_height, max_width):
    if mixed_text_width(draw, text, font, emoji_font, emoji_height) <= max_width:
        return text
    while text and mixed_text_width(draw, text + "…", font, emoji_font, emoji_height) > max_width:
        text = text[:-1]
    return text + "…"


def render_todos_image(tasks):
    """`tasks=None` means Todoist isn't configured; `[]` means configured but
    nothing active; a pure function of its argument otherwise (doesn't look at
    TODOIST_API_TOKEN itself) so it's easy to test/preview without a real token.

    Layout follows TRMNL's own Todoist plugin (a user-provided reference
    screenshot, not just the product page): a numbered flat list, bold task
    name, due date on its own line below with a thin underline, no
    project/section grouping. Deliberately not literally monochrome like that
    reference, though - since this panel actually has color, overdue tasks are
    red and high-priority ones are blue, on top of the borrowed layout.
    """
    img = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    title_font = load_font(38)
    task_font = load_font(26)
    due_font = load_font(18)
    index_font = load_font(16)
    emoji_font = load_emoji_font()
    emoji_height = 28  # matches task_font's approximate line height

    margin = 40
    draw.text((margin, margin), "To-Do", font=title_font, fill=(0, 0, 0))
    draw.line((margin, margin + 48, WIDTH - margin, margin + 48), fill=(0, 0, 0), width=2)

    if tasks is None:
        draw.text((margin, margin + 72), "Todoist not configured (TODOIST_API_TOKEN)", font=task_font, fill=(0, 0, 0))
        return img
    if not tasks:
        draw.text((margin, margin + 72), "Nothing to do", font=task_font, fill=(0, 0, 0))
        return img

    top = margin + 72
    bottom = HEIGHT - margin
    row_height_with_due = 56
    row_height_no_due = 32
    index_col_width = 32
    today_iso = datetime.now(timezone.utc).date().isoformat()

    # Two columns rather than one, to fit noticeably more tasks per screen -
    # each filled top-to-bottom before moving to the next (like a newspaper),
    # not interleaved, so the sort order (see fetch_todoist_tasks() - undated
    # first, then by due date) still reads top-to-bottom-then-across.
    column_gap = 30
    column_width = (WIDTH - 2 * margin - column_gap) // 2
    col1_x = margin
    col2_x = margin + column_width + column_gap

    def row_height_for(task):
        return row_height_with_due if task.get("due") else row_height_no_due

    def draw_task_row(x, y, index, task):
        due = task.get("due")
        overdue = bool(due) and due.get("date", "")[:10] < today_iso
        urgent = task.get("priority", 1) >= 3
        # Colors are restricted to DEVICE_PALETTE's pure primaries deliberately
        # (see "Dithering: idealized colors, not realistic ones" in the
        # README) - an off-palette color, including grays, gets
        # Atkinson-dithered into a speckled mix of pixels for thin strokes/
        # lines instead of rendering as a clean color. Confirmed directly: a
        # light-gray divider line and gray secondary text looked fine
        # pre-dither and were nearly invisible/rough post-dither. Pure black
        # is the only safe "de-emphasized" choice here - visual hierarchy
        # comes from size/weight instead (index/due text are smaller, due
        # text isn't bold).
        color = (255, 0, 0) if overdue else ((0, 0, 255) if urgent else (0, 0, 0))

        draw.text((x, y + 4), str(index), font=index_font, fill=(0, 0, 0))

        text_x = x + index_col_width
        content = strip_markdown_links(task["content"])
        max_width = x + column_width - text_x
        text = truncate_mixed_to_width(draw, content, task_font, emoji_font, emoji_height, max_width)
        # Faux-bold via stroke - the bundled CJK font (see FONT_PATH) only
        # ships one weight, no separate bold file.
        draw_mixed_text(img, draw, (text_x, y), text, task_font, emoji_font, color, emoji_height, stroke_width=1)

        if due and due.get("string"):
            due_text = f"due {due['string']}"
            due_y = y + 30
            draw.text((text_x, due_y), due_text, font=due_font, fill=(0, 0, 0))
            due_width = draw.textlength(due_text, font=due_font)
            draw.line((text_x, due_y + 20, text_x + due_width, due_y + 20), fill=(0, 0, 0), width=1)

    # Packed dynamically rather than a fixed row count per column, since
    # undated tasks take less vertical space than dated ones (no due-date
    # line/underline needed) - a fixed-height slice would waste that space.
    idx = 0
    col_ys = []
    col_counts = []
    for col_x in (col1_x, col2_x):
        y = top
        count = 0
        while idx < len(tasks):
            row_height = row_height_for(tasks[idx])
            if y + row_height > bottom:
                break
            draw_task_row(col_x, y, idx + 1, tasks[idx])
            y += row_height
            idx += 1
            count += 1
        col_ys.append(y)
        col_counts.append(count)

    if col_counts[1] > 0:
        draw.line((col2_x - column_gap // 2, top, col2_x - column_gap // 2, max(col_ys)), fill=(0, 0, 0), width=1)

    remaining = len(tasks) - idx
    if remaining > 0:
        # Wherever there's room for one more (short) line - prefer column 2
        # since it's the one more likely to have leftover space.
        for col_x, y in ((col2_x, col_ys[1]), (col1_x, col_ys[0])):
            if y + row_height_no_due <= bottom:
                draw.text((col_x, y), f"+ {remaining} more", font=task_font, fill=(0, 0, 0))
                break

    return img


def render_dashboard_image(index):
    """Dispatch to the dashboard source at `index` (wrapping around DASHBOARD_SOURCES)."""
    source = DASHBOARD_SOURCES[index % len(DASHBOARD_SOURCES)]
    if source == "weather":
        results = [fetch_weather(name, lat, lon) for name, lat, lon in WEATHER_LOCATIONS]
        return source, render_weather_image(results)
    if source == "todos":
        tasks = fetch_todoist_tasks() if TODOIST_API_TOKEN else None
        return source, render_todos_image(tasks)
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
