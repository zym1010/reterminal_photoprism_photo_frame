FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8090

ENV PHOTOPRISM_URL=http://192.168.68.61:12342 \
    WIDTH=800 \
    HEIGHT=480 \
    FAVORITES_ONLY=true \
    THUMB_SIZE=fit_1920

CMD ["python", "app.py"]
