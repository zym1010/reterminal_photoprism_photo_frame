FROM python:3.12-slim

WORKDIR /app

# WenQuanYi Micro Hei - Pillow's built-in default font has no CJK (or broader
# Unicode) glyph coverage, confirmed by testing against real Todoist task
# content containing Chinese text (rendered as broken missing-glyph boxes).
# This one covers both Latin and CJK, and every card uses it (see
# load_font() in app.py) for consistency.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

# Color emoji font - fetched directly rather than via apt's fonts-noto-color-emoji.
# Confirmed by testing (rendering to a bbox and checking for actual ink, not
# just "no exception"): the color-emoji font format matters a lot here.
# Google's *current* Noto Color Emoji release uses the newer COLR/CPAL vector
# format, which renders as completely blank in this Pillow/FreeType
# combination - no error, just silently invisible glyphs. The older CBDT
# bitmap-strike format (this specific file, from the same googlefonts/noto-emoji
# repo) is the one actually confirmed to render. Homebrew's current
# font-noto-color-emoji cask (used for local dev/testing on macOS) has already
# moved to the broken COLR format, so this is deliberately pulled from source
# rather than trusted to whatever a package manager currently ships.
RUN mkdir -p /usr/share/fonts/truetype/noto \
    && apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL -o /usr/share/fonts/truetype/noto/NotoColorEmoji.ttf \
       https://raw.githubusercontent.com/googlefonts/noto-emoji/main/fonts/NotoColorEmoji.ttf \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8090

ENV PHOTOPRISM_URL=http://192.168.68.61:12342 \
    ADHOC_IMAGES_DIR=/data/adhoc_images \
    WIDTH=800 \
    HEIGHT=480 \
    FAVORITES_ONLY=true \
    THUMB_SIZE=fit_1920 \
    TODOIST_API_TOKEN=

CMD ["python", "app.py"]
