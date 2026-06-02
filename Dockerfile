# Container image for the 提案スライド検索 API server (Cloud Run).
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

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY . .

EXPOSE 8080

# Cloud Run injects $PORT (defaults to 8080). Use shell form so it expands.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
