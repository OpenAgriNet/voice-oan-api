# Use Python as the base image (K8s: run API and workers as separate deployments)
FROM python:3.10-slim

# Optional build-time args so CI/CD can inject JWT keys or paths.
# These are turned into runtime environment variables used by app.config.Settings.
ARG JWT_PUBLIC_KEY
ARG JWT_PUBLIC_KEY_PATH
ARG JWT_PRIVATE_KEY
ARG JWT_PRIVATE_KEY_PATH

# Set work directory
WORKDIR /app
ENV PYTHONPATH=/app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose JWT configuration as environment variables (value or path based).
# - JWT_PUBLIC_KEY / JWT_PRIVATE_KEY: PEM content (with newlines) as env vars.
# - JWT_PUBLIC_KEY_PATH / JWT_PRIVATE_KEY_PATH: filenames (e.g. jwt_public_key.pem) in the app root.
ENV JWT_PUBLIC_KEY=${JWT_PUBLIC_KEY}
ENV JWT_PUBLIC_KEY_PATH=${JWT_PUBLIC_KEY_PATH}
ENV JWT_PRIVATE_KEY=${JWT_PRIVATE_KEY}
ENV JWT_PRIVATE_KEY_PATH=${JWT_PRIVATE_KEY_PATH}

# Create non-root user for security (optional)
# RUN useradd --create-home --shell /bin/bash app
# RUN chown -R app:app /app
# USER app

# Expose FastAPI port
EXPOSE 8003

# Single process for K8s (override CMD in deployment for worker/celery pods)
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 8003 --workers ${AMUL_VOICE_UVICORN_WORKERS:-1}"]