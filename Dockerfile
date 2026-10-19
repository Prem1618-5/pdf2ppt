# Small image for Render / Cloud Run / HF Spaces (CPU only, ~512 MB RAM is enough).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 \
    OMP_NUM_THREADS=1 OPENCV_NUM_THREADS=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY qdeck ./qdeck
COPY static ./static
COPY cli.py .
# GEMINI_API_KEYS (any number) must be set as a secret env var on the host, never baked in.
CMD ["sh", "-c", "uvicorn qdeck.server:app --host 0.0.0.0 --port ${PORT} --workers 1"]
