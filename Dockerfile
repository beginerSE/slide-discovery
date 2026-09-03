# Container image for the 社内スライド検索 API server (Cloud Run).
#
# Runs the FastAPI app under uvicorn. In GCP mode the app authenticates to
# Cloud SQL, Vertex AI, and the Drive API via ADC (the attached Cloud Run
# service account) — no key files are baked into this image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# System packages needed by the ingest pipeline (pptx_pipeline.render_thumbnails):
# - libreoffice-impress: PPTX -> PDF conversion (provides the `soffice` binary)
# - poppler-utils:       PDF  -> PNG conversion (provides `pdftoppm`)
# - fonts-noto-cjk:      Japanese glyphs so slide thumbnails don't render as tofu
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-impress \
        poppler-utils \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY . .

EXPOSE 8080

# Cloud Run injects $PORT (defaults to 8080). Use shell form so it expands.
# --proxy-headers + --forwarded-allow-ips='*' make uvicorn trust Cloud Run's
# front-end X-Forwarded-Proto: https, so url_for() emits https:// asset links
# (otherwise static CSS/JS are http:// and blocked as mixed content on the
# https run.app page). Cloud Run ingress already restricts who can reach the
# container, so trusting all forwarded IPs here is safe.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
