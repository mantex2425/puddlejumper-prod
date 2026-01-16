# Pinned to bookworm for stability (or use bullseye if preferred)
FROM python:3.11-slim-bookworm AS base

# Set working directory
WORKDIR /app

# Copy only requirements first → better layer caching
COPY requirements.txt .

# Upgrade pip & install deps cleanly (no cache to keep image small)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Important: Tell google-genai to use Vertex AI backend
# (you can also pass project/location to genai.Client() in code)
ENV GOOGLE_GENAI_USE_VERTEXAI=True
# If not using ADC (e.g. service account attached to Cloud Run), set these:
# ENV GOOGLE_CLOUD_PROJECT=puddle-jumper-477316
# ENV GOOGLE_CLOUD_LOCATION=us-central1

# Build-time validation — keep this to catch import/code errors during build
RUN python -c "from app import app" \
    && python -m gunicorn --check-config --chdir /app app:app

# Expose port (Cloud Run uses $PORT env var, but good to declare)
EXPOSE 8080

# Healthcheck (optional but helps Cloud Run detect readiness faster)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1  # Assumes you add a /health route in app.py

# No CMD/ENTRYPOINT here — Google Cloud Run / buildpacks / Procfile will handle it
# (e.g. CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"])