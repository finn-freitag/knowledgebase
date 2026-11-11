# Build context: repository root (needs ./shared)
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.10.3

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-venv python3-pip curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# venv that can still import the GDAL bindings shipped with the base image
RUN python3 -m venv --system-site-packages /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Leaflet for the map embed viewer
ARG LEAFLET_VERSION=1.9.4
RUN mkdir -p /app/app/static/vendor/leaflet/images \
 && base="https://cdn.jsdelivr.net/npm/leaflet@${LEAFLET_VERSION}/dist" \
 && curl -fsSL "$base/leaflet.js" -o /app/app/static/vendor/leaflet/leaflet.js \
 && curl -fsSL "$base/leaflet.css" -o /app/app/static/vendor/leaflet/leaflet.css \
 && for f in layers.png layers-2x.png marker-icon.png marker-icon-2x.png marker-shadow.png; do \
      curl -fsSL "$base/images/$f" -o "/app/app/static/vendor/leaflet/images/$f"; done

COPY backend/app /app/app
COPY shared/pkdb-map.js /app/app/static/pkdb-map.js

RUN python3 -c "from osgeo import gdal; print('GDAL', gdal.__version__)"

VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD curl -fsS http://localhost:8000/pkdb/api/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
