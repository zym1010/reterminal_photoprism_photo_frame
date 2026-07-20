import io
import logging
import os
import random
from datetime import datetime, timezone

import requests
from epaper_dithering import ColorPalette, DitherMode, dither_image
from flask import Flask, Response, abort
from PIL import Image, ImageDraw, ImageFont, ImageOps

PHOTOPRISM_URL = os.environ.get("PHOTOPRISM_URL", "http://192.168.68.61:12342").rstrip("/")
WIDTH = int(os.environ.get("WIDTH", "800"))
HEIGHT = int(os.environ.get("HEIGHT", "480"))
FAVORITES_ONLY = os.environ.get("FAVORITES_ONLY", "true").lower() != "false"
THUMB_SIZE = os.environ.get("THUMB_SIZE", "fit_1920")
CANDIDATE_COUNT = int(os.environ.get("CANDIDATE_COUNT", "25"))
REQUEST_TIMEOUT = 15

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

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = app.logger


def render_to_png(img):
    dithered = dither_image(img, DEVICE_PALETTE, mode=DitherMode.ATKINSON, tone="auto", gamut="auto")
    buf = io.BytesIO()
    dithered.save(buf, format="PNG")
    return buf.getvalue()


def pick_random_jpeg_photo():
    params = {"count": CANDIDATE_COUNT, "order": "random"}
    if FAVORITES_ONLY:
        params["favorite"] = "true"
    resp = requests.get(f"{PHOTOPRISM_URL}/api/v1/photos/view", params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    photos = resp.json()
    candidates = [p for p in photos if p.get("Mime") == "image/jpeg" and THUMB_SIZE in p.get("Thumbs", {})]
    return random.choice(candidates) if candidates else None


def fetch_and_process(photo):
    thumb_path = photo["Thumbs"][THUMB_SIZE]["src"]
    resp = requests.get(f"{PHOTOPRISM_URL}{thumb_path}", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content))
    img = ImageOps.exif_transpose(img).convert("RGB")
    fitted = ImageOps.fit(img, (WIDTH, HEIGHT), method=Image.LANCZOS, centering=(0.5, 0.5))
    return render_to_png(fitted)


def fetch_library_stats():
    """Cheap aggregate counts from /config, plus the most recently added item's timestamp.

    Avoids paging through the whole library just to count it - /api/v1/config already
    carries a precomputed `count` object, and the "last added" timestamp only needs a
    single count=1 request against /api/v1/photos (order=added), not the whole library.
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

    return counts, last_added


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


def render_stats_image(counts, last_added):
    img = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    title_font = ImageFont.load_default(size=44)
    label_font = ImageFont.load_default(size=30)

    margin = 40
    draw.text((margin, margin), "PhotoPrism Library", font=title_font, fill=(0, 0, 0))
    draw.line((margin, margin + 60, WIDTH - margin, margin + 60), fill=(0, 0, 0), width=2)

    rows = [
        ("Photos", counts.get("photos")),
        ("Videos", counts.get("videos")),
        ("Live Photos", counts.get("live")),
        ("Favorites", counts.get("favorites")),
        ("Places", counts.get("places")),
        ("Cameras", counts.get("cameras")),
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


@app.route("/frame.png")
def frame():
    photo = pick_random_jpeg_photo()
    if photo is None:
        log.warning("No matching JPEG photos found (favorites_only=%s)", FAVORITES_ONLY)
        abort(503, description="No matching photos available")
    try:
        png_bytes = fetch_and_process(photo)
    except Exception:
        log.exception("Failed to fetch/process photo %s", photo.get("Hash"))
        abort(502, description="Failed to fetch/process photo")
    log.info("Serving photo %s (%s)", photo.get("Hash"), photo.get("Title"))
    return Response(png_bytes, mimetype="image/png", headers={"Cache-Control": "no-store"})


@app.route("/stats.png")
def stats():
    try:
        counts, last_added = fetch_library_stats()
        png_bytes = render_to_png(render_stats_image(counts, last_added))
    except Exception:
        log.exception("Failed to fetch/render library stats")
        abort(502, description="Failed to fetch/render library stats")
    log.info(
        "Serving stats: %s photos, %s videos, %s favorites",
        counts.get("photos"),
        counts.get("videos"),
        counts.get("favorites"),
    )
    return Response(png_bytes, mimetype="image/png", headers={"Cache-Control": "no-store"})


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")), threaded=True)
