FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /root/.cache/pip

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Build-time checks — keep this
RUN python -c "from app import app" \
    && python -m gunicorn --check-config --chdir /app app:app

# NO CMD here — Procfile takes over in source deploy