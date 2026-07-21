FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY capture_logs.py .

# Written to a volume (see README) so logs survive container
# restarts/updates - not baked into the image layer.
VOLUME ["/logs"]

ENV DEVICE_PORT=6053 \
    LOG_FILE=/logs/device.log \
    LOG_MAX_BYTES=20971520 \
    LOG_BACKUP_COUNT=5

CMD ["python", "capture_logs.py"]
