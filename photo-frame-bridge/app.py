import io
import logging
import os
import random

import requests
from epaper_dithering import ColorPalette, DitherMode, dither_image
from flask import Flask, Response, abort
from PIL import Image, ImageOps

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
    dithered = dither_image(fitted, DEVICE_PALETTE, mode=DitherMode.ATKINSON, tone="auto", gamut="auto")
    buf = io.BytesIO()
    dithered.save(buf, format="PNG")
    return buf.getvalue()


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


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")), threaded=True)
