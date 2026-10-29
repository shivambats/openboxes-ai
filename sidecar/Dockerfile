# Vegath agent sidecar: FastAPI workflow service plus headless Chromium for map and web lookups.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    SIDECAR_STATE_PATH=/data/sidecar.sqlite3

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/false sidecar \
    && mkdir -p /data && chown sidecar:sidecar /data

COPY --chown=sidecar:sidecar . .

USER sidecar
EXPOSE 8091

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/api/health', timeout=4)"

CMD ["uvicorn", "workflow_api:app", "--host", "0.0.0.0", "--port", "8091"]
