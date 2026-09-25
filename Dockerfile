FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NASITRON_DATA_DIR=/data \
    WEB_CONCURRENCY=1

WORKDIR /app

RUN addgroup --system nasitron && adduser --system --ingroup nasitron --home /app nasitron

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY app ./app
COPY scripts ./scripts
COPY README.md .
RUN mkdir -p /data && chown -R nasitron:nasitron /app /data

USER nasitron
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers"]
